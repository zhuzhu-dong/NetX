"""Real-time resource ledger separating reservations from measured load."""
from __future__ import annotations

import os
from typing import Any

from api.store import IntentStore, get_store
from infra.resource_registry import ResourceRegistry, get_resource_registry
from models.resources import (
    AdmissionDecision,
    AdmissionResult,
    LinkLedgerEntry,
)
from infra.stable_capacity import StableCapacityRepository


class ResourceLedger:
    def __init__(
        self, store: IntentStore | None = None, registry: ResourceRegistry | None = None,
    ):
        self.store = store or get_store()
        self.registry = registry or get_resource_registry()

    def _reservations(self) -> list[dict[str, Any]]:
        with self.store._lock, self.store._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM resource_reservations WHERE active=1"
            ).fetchall()]

    def _authorized_scheduler_cap(
        self,
        *,
        resource_id: str,
        capacity_record_id: str | None,
        reservations: list[dict[str, Any]],
        evidence_cap_mbps: float,
    ) -> float | None:
        """Return the active durable cap for this exact resource, if any.

        Calibration evidence may legitimately be fractional, while a
        materialized ingress plan owns a conservative whole-Mbit scheduler
        contract.  Match the plan through its binding-derived reservation and
        capacity record, never through a service name or selector text.
        """
        if not capacity_record_id:
            return None

    def _durable_scheduler_contract(
        self, *, resource_id: str, reservations: list[dict[str, Any]],
    ) -> float | None:
        """Read a scheduler cap only from committed ingress desired state.

        It deliberately does not inspect a calibration record, TC readback or
        binding runtime.  A stale runtime health observation may degrade new
        admission, but cannot silently expand or shrink an existing contract.
        """
        try:
            from infra.ingress_admission_state import IngressAdmissionDesiredRepository

            reservation_ids = {
                str(item.get("reservation_id")) for item in reservations
                if str(item.get("resource_id")) == str(resource_id)
            }
            caps = []
            for desired in IngressAdmissionDesiredRepository(self.store).list_active():
                binding_id = str(desired.get("resource_binding_id") or "")
                if not binding_id or f"RES-{binding_id}" not in reservation_ids:
                    continue
                cap = float((desired.get("desired") or {}).get("schedulable_capacity_mbps") or 0)
                if cap > 0:
                    caps.append(cap)
            return min(caps) if caps else None
        except Exception:
            return None
        try:
            from infra.ingress_admission_state import IngressAdmissionDesiredRepository

            reservation_ids = {
                str(item.get("reservation_id"))
                for item in reservations
                if str(item.get("resource_id")) == str(resource_id)
            }
            caps: list[float] = []
            for desired in IngressAdmissionDesiredRepository(self.store).list_active():
                if str(desired.get("capacity_record_id")) != str(capacity_record_id):
                    continue
                binding_id = str(desired.get("resource_binding_id") or "")
                if not binding_id or f"RES-{binding_id}" not in reservation_ids:
                    continue
                cap = float((desired.get("desired") or {}).get("schedulable_capacity_mbps") or 0)
                if 0 < cap <= float(evidence_cap_mbps):
                    caps.append(cap)
            return min(caps) if caps else None
        except Exception:
            # No materialized ingress plan is a normal condition for other
            # resources; retain the calibrated limit in that case.
            return None

    def entries(self, measured_loads: dict[str, float] | None = None) -> list[LinkLedgerEntry]:
        measured_loads = measured_loads or {}
        reservations = self._reservations()
        margin_ratio = max(0.0, min(float(os.getenv("IDN_CAPACITY_SAFETY_MARGIN_RATIO", "0.05")), 0.9))
        capacity_records = StableCapacityRepository(self.store)
        result = []
        for link in self.registry.snapshot().links:
            aliases = {link.resource_id, link.physical_link_id}
            related = [item for item in reservations if item["resource_id"] in aliases]
            guaranteed = sum(
                float(item["bandwidth_mbps"]) for item in related
                if item["reservation_kind"] == "GUARANTEED"
            )
            rate_limit = sum(
                float(item["bandwidth_mbps"]) for item in related
                if item["reservation_kind"] == "RATE_LIMIT"
            )
            record = capacity_records.active_for(
                bottleneck_egress=link.resource_id,
                topology_id=self.registry.snapshot().topology_name,
            )
            # Legacy links retain the existing physical model.  A calibrated
            # record replaces it only for that exact bottleneck and topology.
            stable = record.stable_capacity_mbps if record else None
            safety = record.safety_margin_mbps if record else link.capacity_mbps * margin_ratio
            evidence_schedulable = (
                StableCapacityRepository.schedulable_capacity(record)
                if record else link.capacity_mbps
            )
            durable_contract = self._durable_scheduler_contract(
                resource_id=link.resource_id, reservations=reservations,
            )
            authorized_cap = durable_contract or self._authorized_scheduler_cap(
                resource_id=link.resource_id,
                capacity_record_id=record.capacity_record_id if record else None,
                reservations=reservations,
                evidence_cap_mbps=float(evidence_schedulable),
            )
            schedulable = (
                min(float(evidence_schedulable), authorized_cap)
                if record and authorized_cap is not None
                else (authorized_cap if authorized_cap is not None else evidence_schedulable)
            )
            margin = safety
            available = max(0.0, schedulable - guaranteed) if record else max(0.0, link.capacity_mbps - guaranteed - margin)
            measured = measured_loads.get(link.resource_id)
            tenants: dict[str, float] = {}
            for item in related:
                if item["reservation_kind"] == "GUARANTEED":
                    tenants[item["tenant_id"]] = tenants.get(item["tenant_id"], 0.0) + float(item["bandwidth_mbps"])
            result.append(LinkLedgerEntry(
                resource_id=link.resource_id,
                physical_capacity_mbps=link.capacity_mbps,
                measured_load_mbps=measured,
                guaranteed_reservation_mbps=guaranteed,
                rate_limit_commitment_mbps=rate_limit,
                safety_margin_mbps=margin,
                available_guaranteed_mbps=available,
                utilization_pct=(measured / link.capacity_mbps * 100.0) if measured is not None else None,
                per_tenant_reservation_mbps=tenants,
                policy_ids=sorted({str(item["policy_id"]) for item in related}),
                quality_status="CALIBRATED" if record else ("DEGRADED" if durable_contract is not None else ("MEASURED" if measured is not None else "NO_SAMPLE")),
                stable_capacity_mbps=stable,
                schedulable_capacity_mbps=schedulable,
                authorized_schedulable_capacity_mbps=authorized_cap,
                committed_guaranteed_capacity_mbps=guaranteed,
                remaining_guaranteed_capacity_mbps=available,
                remaining_best_effort_capacity_mbps=available,
                capacity_record_id=record.capacity_record_id if record else None,
                capacity_confidence=record.confidence if record else None,
                capacity_freshness="FRESH" if record else ("DEGRADED" if durable_contract is not None else "UNKNOWN"),
            ))
        return result

    def admission(self, resource_id: str, requested_mbps: float) -> AdmissionResult:
        entry = next((item for item in self.entries() if item.resource_id == resource_id), None)
        if entry is None:
            raise KeyError(resource_id)
        if requested_mbps <= 0:
            raise ValueError("requested_mbps must be positive")
        if requested_mbps <= entry.available_guaranteed_mbps:
            decision = AdmissionDecision.ACCEPT
            reason = "ADMITTED_STABLE_CAPACITY_AVAILABLE" if entry.capacity_record_id else "guaranteed capacity is available after reservations and safety margin"
        else:
            alternatives = [
                item.resource_id for item in self.entries()
                if item.resource_id != resource_id and item.available_guaranteed_mbps >= requested_mbps
            ]
            decision = AdmissionDecision.ACCEPT_WITH_ADJUSTMENT if alternatives else AdmissionDecision.REJECT
            reason = "INSUFFICIENT_STABLE_DATAPLANE_CAPACITY" if entry.capacity_record_id else ("primary resource lacks guaranteed capacity" if alternatives else "no resource has sufficient guaranteed capacity")
            return AdmissionResult(
                decision=decision, requested_mbps=requested_mbps,
                available_mbps=entry.available_guaranteed_mbps, resource_id=resource_id,
                reason=reason, alternative_resource_ids=alternatives,
            )
        return AdmissionResult(
            decision=decision, requested_mbps=requested_mbps,
            available_mbps=entry.available_guaranteed_mbps,
            resource_id=resource_id, reason=reason,
        )

"""Build deterministic, source-attributed network context for planning."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from api.store import IntentStore, get_store
from infra.resource_ledger import ResourceLedger
from infra.resource_registry import ResourceRegistry, get_resource_registry
from models.intent_domain import (
    ContextEvidenceQuality,
    ContextSource,
    ContextSourceState,
    EvidenceQuality,
    NetworkContext,
)

LiveSourceProvider = Callable[[], dict[str, dict[str, Any]]]


class NetworkContextBuilder:
    """Combine live evidence first and use configured data only as fallback.

    A static registry can describe desired topology and physical capacity, but it
    cannot prove current reachability or load.  Therefore a static-only snapshot
    is explicitly DEGRADED and never TRUSTED.
    """

    def __init__(
        self,
        store: IntentStore | None = None,
        registry: ResourceRegistry | None = None,
        live_source_provider: LiveSourceProvider | None = None,
    ):
        self.store = store or get_store()
        self.registry = registry or get_resource_registry()
        self.ledger = ResourceLedger(self.store, self.registry)
        self.live_source_provider = live_source_provider

    def build(
        self,
        live_observations: dict[str, dict[str, Any]] | None = None,
        *,
        snapshot_nonce: str | None = None,
    ) -> NetworkContext:
        observations = live_observations
        if observations is None and self.live_source_provider is not None:
            try:
                observations = self.live_source_provider()
            except Exception as exc:  # source failure becomes evidence, not fiction
                observations = {"TELEMETRY": {"available": False, "error": str(exc)}}
        observations = {
            str(key).upper(): value for key, value in (observations or {}).items()
        }

        snapshot = self.registry.snapshot()
        configured_devices = [item.model_dump(mode="json") for item in snapshot.devices]
        configured_endpoints = [
            item.model_dump(mode="json") for item in snapshot.endpoints
        ]
        configured_links = [self._configured_link(item) for item in snapshot.links]

        onos = observations.get(ContextSource.ONOS.value, {})
        mininet = observations.get(ContextSource.MININET.value, {})
        topology_observation = onos if onos.get("available") else mininet
        live_topology = topology_observation.get("topology") or {}
        live_inventory = topology_observation.get("inventory") or {}

        measured_loads: dict[str, float] = {}
        for source in (
            ContextSource.TELEMETRY,
            ContextSource.P4RUNTIME,
            ContextSource.TC,
        ):
            values = observations.get(source.value, {}).get("measured_loads") or {}
            measured_loads.update(
                {str(key): float(value) for key, value in values.items()}
            )
        ledger = self.ledger.entries(measured_loads=measured_loads)

        database_error: str | None = None
        try:
            active_faults = self._rows(
                "SELECT fault_id, fault_type, target_resource_id, lifecycle_state, definition_json, observed_json "
                "FROM faults WHERE lifecycle_state IN ('ACTIVE','APPLYING','RESIDUAL')"
            )
            policies = self._rows(
                "SELECT policy_id, intent_id, action_type, yang_json, updated_at FROM policies ORDER BY updated_at DESC"
            )
            reservations = self._rows(
                "SELECT * FROM resource_reservations WHERE active=1 ORDER BY updated_at DESC"
            )
            runtime_rows = self._rows(
                "SELECT intent_id, lifecycle, runtime_json, updated_at FROM intent_runtime_v1 "
                "WHERE lifecycle NOT IN ('WITHDRAWN','FAILED') ORDER BY updated_at DESC"
            )
            spec_rows = self._rows(
                "SELECT intent_id, revision, spec_json FROM intent_specs_v1 "
                "ORDER BY intent_id, revision DESC"
            )
        except Exception as exc:
            database_error = str(exc)
            active_faults, policies, reservations = [], [], []
            runtime_rows = []
            spec_rows = []
        try:
            capability_rows = self._rows(
                "SELECT capability_json FROM empirical_capabilities_v1 ORDER BY observed_at DESC"
            )
        except Exception:
            capability_rows = []

        active_intents: list[dict[str, Any]] = []
        current_paths: dict[str, list[str]] = {}
        latest_specs: dict[str, dict[str, Any]] = {}
        for row in spec_rows:
            intent_id = str(row.get("intent_id") or "")
            if not intent_id or intent_id in latest_specs:
                continue
            try:
                latest_specs[intent_id] = json.loads(row.get("spec_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                latest_specs[intent_id] = {}
        for row in runtime_rows:
            try:
                runtime = json.loads(row.get("runtime_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                runtime = {}
            intent_id = str(row.get("intent_id") or "")
            spec = latest_specs.get(intent_id, {})
            validity = spec.get("validity") or {}
            item = {
                "intent_id": row.get("intent_id"),
                "lifecycle": row.get("lifecycle"),
                "priority": spec.get("priority") or runtime.get("priority"),
                "preemptible": bool(
                    validity.get("preemptible", runtime.get("preemptible", False))
                ),
                "current_path": list(runtime.get("current_path") or []),
                "active_plan_id": runtime.get("active_plan_id"),
                "active_execution_id": runtime.get("active_execution_id"),
                "selector": spec.get("selector") or {},
                "objectives": spec.get("objectives") or [],
                "current_usage_mbps": (runtime.get("observed_metrics") or {}).get(
                    "throughput_mbps"
                ),
            }
            active_intents.append(item)
            if item["current_path"]:
                current_paths[str(item["intent_id"])] = item["current_path"]

        empirical_capabilities: list[dict[str, Any]] = []
        for row in capability_rows:
            try:
                empirical_capabilities.append(
                    json.loads(row.get("capability_json") or "{}")
                )
            except (TypeError, json.JSONDecodeError):
                continue

        source_states = self._source_states(
            observations,
            database_error=database_error,
            registry_version=snapshot.schema_version,
            measured=bool(measured_loads),
        )
        evidence_quality, unavailable = self._quality(source_states)
        legacy_quality = {
            ContextEvidenceQuality.TRUSTED: EvidenceQuality.TRUSTED,
            ContextEvidenceQuality.DEGRADED: EvidenceQuality.DEGRADED,
            ContextEvidenceQuality.STALE: EvidenceQuality.STALE,
            ContextEvidenceQuality.UNTRUSTED: EvidenceQuality.UNTRUSTED,
            ContextEvidenceQuality.UNAVAILABLE: EvidenceQuality.MISSING,
        }[evidence_quality]

        topology = {
            "name": live_topology.get("name") or snapshot.topology_name,
            "origin": "LIVE" if live_topology else "CONFIGURED_FALLBACK",
            "nodes": live_topology.get("nodes")
            or [
                *[
                    {"id": item["switch_name"], "kind": "device", **item}
                    for item in configured_devices
                ],
                *[
                    {"id": item["host_name"], "kind": "endpoint", **item}
                    for item in configured_endpoints
                ],
            ],
            "links": live_topology.get("links") or configured_links,
            "aliases": snapshot.aliases,
        }
        inventory = live_inventory or {
            "origin": "CONFIGURED_FALLBACK",
            "devices": configured_devices,
            "endpoints": configured_endpoints,
        }
        state_payload = {
            "source_states": {
                source.value: state.model_dump(
                    mode="json", exclude={"observed_at", "epoch", "version"}
                )
                for source, state in sorted(
                    source_states.items(), key=lambda item: item[0].value
                )
            },
            "topology": topology,
            "inventory": inventory,
            "capacities": {
                item.resource_id: item.model_dump(mode="json") for item in ledger
            },
            "utilization": {
                item.resource_id: item.utilization_pct
                for item in ledger
                if item.utilization_pct is not None
            },
            "faults": active_faults,
            "policies": policies,
            "reservations": reservations,
            "active_intents": active_intents,
            "current_paths": current_paths,
            "empirical_capabilities": empirical_capabilities,
            "int_observations": (
                observations.get(ContextSource.TELEMETRY.value, {}).get(
                    "int_observations"
                )
                or {}
            ),
            "tc_state": (
                observations.get(ContextSource.TC.value, {}).get("runtime_state")
                or {}
            ),
            "p4_state": (
                observations.get(ContextSource.P4RUNTIME.value, {}).get(
                    "runtime_state"
                )
                or {}
            ),
            "queue_state": (
                observations.get(ContextSource.TC.value, {}).get("queue_state")
                or {}
            ),
            "kpi": (
                observations.get(ContextSource.TELEMETRY.value, {}).get("kpi")
                or {}
            ),
            "bottleneck_candidates": (
                observations.get(ContextSource.TELEMETRY.value, {}).get(
                    "bottleneck_candidates"
                )
                or []
            ),
        }
        material_canonical = json.dumps(
            state_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        material_state_hash = hashlib.sha256(
            material_canonical.encode("utf-8")
        ).hexdigest()
        if snapshot_nonce:
            # Async operational captures are distinct evidence snapshots even
            # when their material state is unchanged.  Direct configured-only
            # builds remain content-addressed and deterministic.
            state_payload["snapshot_nonce"] = snapshot_nonce
        canonical = json.dumps(
            state_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        state_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        congestion = [
            {
                "resource_id": resource_id,
                "utilization_percent": utilization,
                "status": "CONGESTED",
            }
            for resource_id, utilization in state_payload["utilization"].items()
            if float(utilization) >= 80.0
        ]
        freshness = {
            source.value: {
                "observed_at": state.observed_at.isoformat()
                if state.observed_at
                else None,
                "stale": state.stale,
                "available": state.available,
            }
            for source, state in source_states.items()
        }
        epochs = sorted(
            {state.epoch for state in source_states.values() if state.epoch}
        )
        return NetworkContext(
            context_id=f"CTX-{state_hash[:12].upper()}",
            version=int(state_hash[:12], 16),
            captured_at=now,
            source_versions={
                source.value: state.version
                for source, state in source_states.items()
                if state.version is not None
            },
            quality=legacy_quality,
            evidence_quality=evidence_quality,
            source_states=source_states,
            state_hash=state_hash,
            material_state_hash=material_state_hash,
            epoch="|".join(epochs) if epochs else None,
            decision_usable=evidence_quality
            in {
                ContextEvidenceQuality.TRUSTED,
                ContextEvidenceQuality.DEGRADED,
            },
            unavailable_reasons=unavailable,
            topology=topology,
            inventory=inventory,
            capacities=state_payload["capacities"],
            utilization=state_payload["utilization"],
            faults=active_faults,
            policies=policies,
            reservations=reservations,
            active_intents=active_intents,
            current_paths=current_paths,
            congestion=congestion,
            bottleneck_candidates=state_payload["bottleneck_candidates"],
            int_observations=state_payload["int_observations"],
            tc_state=state_payload["tc_state"],
            p4_state=state_payload["p4_state"],
            queue_state=state_payload["queue_state"],
            kpi=state_payload["kpi"],
            empirical_capabilities=empirical_capabilities,
            freshness=freshness,
            evidence_refs=[
                source.value
                for source, state in source_states.items()
                if state.available
            ],
        )

    async def build_async(self) -> NetworkContext:
        """Collect live controller, emulator and telemetry evidence concurrently."""
        from api.telemetry import collect_current_telemetry
        from api.topology import collect_current_topology

        capture_epoch = f"CAPTURE-{uuid.uuid4().hex}"
        collection_results = await asyncio.gather(
            collect_current_topology(fields="full"),
            collect_current_telemetry(include_probe=True, include_iperf=False),
            return_exceptions=True,
        )
        topology_result: Any = collection_results[0]
        telemetry_result: Any = collection_results[1]
        observations: dict[str, dict[str, Any]] = {}
        if isinstance(topology_result, Exception):
            error = str(topology_result)
            observations["ONOS"] = {"available": False, "error": error}
            observations["MININET"] = {"available": False, "error": error}
        else:
            source = str(topology_result.get("source") or "")
            frontend = topology_result.get("frontend") or {}
            canonical = topology_result.get("canonical") or {}
            live_topology = {
                "name": str(
                    canonical.get("topology_name")
                    or (topology_result.get("mininet") or {}).get("topology_name")
                    or "runtime"
                ),
                "schema": canonical.get("schema") or "idn.canonical_topology.v1",
                "nodes": canonical.get("nodes") or [],
                "links": canonical.get("links") or [],
            }
            onos_by_id = {
                str(item.get("id") or item.get("device_id")): item
                for item in (topology_result.get("onos") or {}).get("devices") or []
                if item.get("id") or item.get("device_id")
            }
            runtime_devices: list[dict[str, Any]] = []
            from models.network import parse_device_availability

            for switch in topology_result.get("switches") or []:
                device = dict(switch)
                device_id = str(device.get("device_id") or device.get("id") or "")
                raw_device = onos_by_id.get(device_id) or {}
                raw_available = raw_device.get("available")
                availability = parse_device_availability(raw_available)
                device.update({
                    "present_in_topology": True,
                    "availability_status": availability.value,
                    "controller_available": (
                        True if availability.value == "AVAILABLE"
                        else False if availability.value == "UNAVAILABLE"
                        else None
                    ),
                    "availability_source": (
                        "ONOS_DEVICE_AVAILABLE" if raw_device
                        else "MININET_TOPOLOGY_WITHOUT_ONOS_AVAILABILITY"
                    ),
                    "availability_reason_code": (
                        None if raw_device else "DEVICE_AVAILABILITY_NOT_OBSERVED"
                    ),
                    "raw_availability": raw_available,
                })
                runtime_devices.append(device)
            inventory = {
                "origin": "LIVE",
                "devices": runtime_devices,
                "endpoints": topology_result.get("hosts") or [],
            }
            for name, marker in (("ONOS", "onos_rest"), ("MININET", "mininet_probe")):
                available = marker in source and topology_result.get("status") == "ok"
                observations[name] = {
                    "available": available,
                    "trusted": available,
                    "epoch": capture_epoch,
                    "version": str(topology_result.get("timestamp") or "runtime"),
                    "observed_at": topology_result.get("timestamp"),
                    "topology": live_topology if available else {},
                    "inventory": inventory if available else {},
                    "error": (
                        None
                        if available
                        else f"{name} absent from topology source {source or 'unavailable'}"
                    ),
                    "evidence_refs": [f"topology:{marker}"],
                }
        if isinstance(telemetry_result, Exception):
            observations["TELEMETRY"] = {
                "available": False,
                "error": str(telemetry_result),
            }
        else:
            telemetry_source = str(telemetry_result.get("source") or "")
            available = telemetry_result.get("status") == "ok"
            trusted = available and "demo" not in telemetry_source.lower()
            observations["TELEMETRY"] = {
                "available": available,
                "trusted": trusted,
                "epoch": capture_epoch,
                "version": str(telemetry_result.get("timestamp") or "runtime"),
                "observed_at": telemetry_result.get("timestamp"),
                "error": (
                    None
                    if available
                    else str(telemetry_result.get("error") or "telemetry unavailable")
                ),
                "evidence_refs": [f"telemetry:{telemetry_source or 'unknown'}"],
                "int_observations": (
                    telemetry_result.get("int_analysis")
                    or telemetry_result.get("int")
                    or (telemetry_result.get("infrastructure") or {}).get("int")
                    or {}
                ),
                "kpi": telemetry_result.get("kpi") or {},
                "bottleneck_candidates": self._bottleneck_candidates(
                    telemetry_result
                ),
                "measured_loads": telemetry_result.get("measured_loads") or {},
            }
            infrastructure = telemetry_result.get("infrastructure") or {}
            for name, field in (("P4RUNTIME", "p4_counters"), ("TC", "queues")):
                evidence = infrastructure.get(field) or {}
                measured = str(evidence.get("status") or "").upper() in {
                    "MEASURED",
                    "OK",
                    "TRUSTED",
                }
                observations[name] = {
                    "available": measured,
                    "trusted": measured and trusted,
                    "epoch": capture_epoch,
                    "version": str(telemetry_result.get("timestamp") or "runtime"),
                    "observed_at": telemetry_result.get("timestamp"),
                    "error": (
                        None if measured else f"{field} has no current measured sample"
                    ),
                    "evidence_refs": [f"telemetry:{field}"],
                    "runtime_state": evidence,
                    "queue_state": evidence if name == "TC" else {},
                    "measured_loads": evidence.get("measured_loads") or {},
                }
        return self.build(observations, snapshot_nonce=capture_epoch)

    @staticmethod
    def _bottleneck_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract explicit bottleneck identifiers without inventing topology."""

        found: list[dict[str, Any]] = []

        def visit(value: Any, trail: str = "telemetry") -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    lowered = str(key).lower()
                    if lowered in {
                        "bottleneck_link",
                        "bottleneck_link_id",
                        "blocking_link",
                    } and item:
                        candidate = {
                            "link_id": str(item),
                            "source": f"{trail}.{key}",
                        }
                        if candidate not in found:
                            found.append(candidate)
                    elif isinstance(item, (dict, list)):
                        visit(item, f"{trail}.{key}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    visit(item, f"{trail}[{index}]")

        visit(payload)
        return found

    @staticmethod
    def _configured_link(item) -> dict[str, Any]:
        data = item.model_dump(mode="json")
        raw = str(item.resource_id).removeprefix("link:")
        peers = raw.split("--", 1)
        data.update(
            {
                "id": item.resource_id,
                "state": "UNKNOWN_CONFIGURED",
                "bandwidth_mbps": item.capacity_mbps,
                "endpoints": (
                    [{"node": peer} for peer in peers] if len(peers) == 2 else []
                ),
            }
        )
        return data

    @staticmethod
    def _state(source: ContextSource, raw: dict[str, Any]) -> ContextSourceState:
        observed_at = raw.get("observed_at")
        return ContextSourceState(
            source=source,
            available=bool(raw.get("available")),
            trusted=bool(raw.get("trusted")),
            stale=bool(raw.get("stale")),
            observed_at=observed_at,
            epoch=raw.get("epoch"),
            version=raw.get("version"),
            error=raw.get("error"),
            evidence_refs=list(raw.get("evidence_refs") or []),
        )

    def _source_states(
        self,
        observations: dict[str, dict[str, Any]],
        *,
        database_error: str | None,
        registry_version: str,
        measured: bool,
    ) -> dict[ContextSource, ContextSourceState]:
        states = {
            source: self._state(source, observations.get(source.value, {}))
            for source in (
                ContextSource.ONOS,
                ContextSource.MININET,
                ContextSource.P4RUNTIME,
                ContextSource.TC,
                ContextSource.TELEMETRY,
            )
        }
        states[ContextSource.DATABASE] = ContextSourceState(
            source=ContextSource.DATABASE,
            available=database_error is None,
            trusted=database_error is None,
            version="1.0",
            error=database_error,
            evidence_refs=["fault_store", "policy_store", "reservation_store"],
        )
        states[ContextSource.RESOURCE_LEDGER] = ContextSourceState(
            source=ContextSource.RESOURCE_LEDGER,
            available=True,
            trusted=measured,
            version=registry_version,
            error=None if measured else "configured capacity only; no live load sample",
            evidence_refs=["resource_registry", "resource_ledger"],
        )
        return states

    @staticmethod
    def _quality(
        states: dict[ContextSource, ContextSourceState],
    ) -> tuple[ContextEvidenceQuality, list[str]]:
        live = [
            states[source]
            for source in (
                ContextSource.ONOS,
                ContextSource.MININET,
                ContextSource.P4RUNTIME,
                ContextSource.TC,
                ContextSource.TELEMETRY,
            )
        ]
        available = [state for state in live if state.available]
        unavailable = [
            f"{state.source.value}: {state.error or 'unavailable'}"
            for state in live
            if not state.available
        ]
        if any(state.stale for state in available):
            return ContextEvidenceQuality.STALE, unavailable
        if available and any(not state.trusted for state in available):
            return ContextEvidenceQuality.UNTRUSTED, unavailable
        topology_live = any(
            states[source].available and states[source].trusted
            for source in (ContextSource.ONOS, ContextSource.MININET)
        )
        measurement_live = any(
            states[source].available and states[source].trusted
            for source in (
                ContextSource.P4RUNTIME,
                ContextSource.TC,
                ContextSource.TELEMETRY,
            )
        )
        if topology_live and measurement_live:
            epochs = {state.epoch for state in available if state.epoch}
            if len(epochs) <= 1:
                return ContextEvidenceQuality.TRUSTED, unavailable
            return ContextEvidenceQuality.DEGRADED, [
                *unavailable,
                "live sources span multiple epochs",
            ]
        if available or states[ContextSource.RESOURCE_LEDGER].available:
            return ContextEvidenceQuality.DEGRADED, unavailable
        return ContextEvidenceQuality.UNAVAILABLE, unavailable

    def _rows(self, query: str) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            return [dict(row) for row in connection.execute(query).fetchall()]

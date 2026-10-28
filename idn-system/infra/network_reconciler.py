"""Periodic desired/observed reconciliation for policies and workloads."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from api._probe_session import get_probe_session
from api.store import IntentStore, get_store
from infra.qos_resource_allocator import QoSResourceAllocator
from infra.ingress_admission_state import (
    IngressAdmissionDesiredRepository, declared_contention_allocation, idle_borrowing_allocation,
)
from infra.egress_qos_desired import (
    EgressQosDesiredRepository, resolve_egress_qos_desired_for_scope,
)
from infra.network_write_fencing import (
    NetworkWriteFencing, current_write_context, require_network_write,
    resource_write_scope, write_context,
)
from infra.reconcile_semantics import (
    VOLATILE_EGRESS_FIELDS, VOLATILE_P4_FIELDS, canonicalize_egress_desired,
    canonicalize_egress_observed, canonicalize_p4_desired,
    canonicalize_p4_observed, reconcile_semantic_diff,
)
from models.policy_update import effective_qos_mode, qos_values
from models.reconciliation import ReconciliationSummary
from models.yang_policy import YANGPolicy
from orchestrator.traffic_orchestrator import TrafficOrchestrator, get_traffic_orchestrator
from persistence.policy_store import PolicyStore

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReconciliationRetryDeferred(RuntimeError):
    """The retry budget is exhausted and no repair was attempted this cycle."""


class NetworkReconciler:
    def __init__(
        self, *, store: IntentStore | None = None, probe: Any = None,
        workloads: TrafficOrchestrator | None = None, flow_delivery: Any = None,
    ):
        self.store = store or get_store()
        self.allocator = QoSResourceAllocator(self.store)
        self.probe = probe
        self.workloads = workloads or get_traffic_orchestrator()
        self.flow_delivery = flow_delivery
        self.ingress_desired = IngressAdmissionDesiredRepository(self.store)
        self.egress_desired = EgressQosDesiredRepository(self.store)
        self._lock = asyncio.Lock()
        self._mininet_runtime_id: str | None = None

    def resident_fencing_state(self, resource_key: str | None = None) -> dict[str, Any]:
        """Resident reconciliation remains observable while recovery owns writes."""
        return NetworkWriteFencing(self.store).resident_state(resource_write_scope(resource_key))

    def _current_recovery_owns_lease(self, fencing: dict[str, Any]) -> bool:
        """Allow the Recovery Worker itself through its own active lease.

        A resident loop with no matching context still defers.  This is the
        distinction that prevents an active recovery from fencing itself out.
        """
        active = fencing.get("active_lease") or {}
        context = current_write_context() or {}
        return bool(
            active.get("owner_type") == "RESTART_RECOVERY"
            and context.get("owner_type") == "RESTART_RECOVERY"
            and context.get("owner_id") == active.get("owner_id")
            and int(context.get("fencing_token", -1)) == int(active.get("fencing_token", -2))
        )

    def _shared_resource_authorized_root(self, binding) -> float | None:
        """Return the strictest durable ingress root for a shared resource.

        A generic QoS scope can share a TC root with an ingress-admission
        scope.  It must inherit that root contract rather than independently
        widening the same qdisc to the raw calibrated capacity.
        """
        roots: list[float] = []
        for desired in self.ingress_desired.list_active():
            sibling = self.allocator.get_active_for_scope(desired["traffic_scope_key"])
            if sibling is None or sibling.resource_key != binding.resource_key:
                continue
            try:
                resolution = resolve_egress_qos_desired_for_scope(
                    desired["traffic_scope_key"], store=self.store,
                    capacity_purpose="RECONCILE_EXISTING",
                )
            except TypeError as exc:
                # Keep injected legacy adapters testable; production resolver
                # always receives the explicit reconcile purpose above.
                if "capacity_purpose" not in str(exc):
                    raise
                resolution = resolve_egress_qos_desired_for_scope(
                    desired["traffic_scope_key"], store=self.store,
                )
            if resolution.accepted and resolution.root_rate_mbps is not None:
                roots.append(float(resolution.root_rate_mbps))
        return min(roots) if roots else None

    def resident_write_attempt_for_fencing_test(self, *, trace_id: str) -> dict[str, Any]:
        """Exercise the same service authorization boundary without a write.

        This is intentionally useful to an isolated fencing preflight: it
        records a resident attempt with no token and proves no actuator is
        reached while a recovery lease owns the network.
        """
        decision = NetworkWriteFencing(self.store).authorize_network_configuration_write(
            owner_type="RESIDENT_RECONCILER", owner_id="network-reconciler",
            fencing_token=None, resource_scope="GLOBAL_NETWORK_CONFIGURATION",
            operation="RECONCILER_QOS_REPAIR", trace_id=trace_id,
        )
        self.store.add_audit_event(event_type="RESIDENT_RECONCILER_WRITE_REJECTED_BY_FENCING",
            resource_type="network_write", resource_id="GLOBAL_NETWORK_CONFIGURATION", expected={}, observed={},
            action={"writer_type": "RESIDENT_RECONCILER", "operation": "REPAIR", "authorization": decision},
            result={"write_executed": False, "status": decision["reason"]})
        return {"authorization": decision, "write_executed": False}

    async def _probe(self):
        return self.probe or await get_probe_session()

    async def _refresh_runtime_epoch(self) -> dict[str, Any]:
        """Open one fresh repair window whenever the Mininet runtime changes.

        Retry budgets protect one unhealthy runtime from a tight loop.  They
        must not carry over to a newly started data plane, where TC state and
        P4 entries are intentionally ephemeral.  The Mininet runtime exposes
        a process-stable UUID, so this remains topology/service agnostic.
        """
        try:
            probe = await self._probe()
            identity = await probe.get("/runtime/identity") or {}
        except Exception as exc:
            return {"status": "unavailable", "error": str(exc)}
        runtime_id = str(identity.get("runtime_id") or "")
        if identity.get("status") != "ok" or not runtime_id:
            return {"status": "unavailable", "observed": identity}
        previous = self._mininet_runtime_id
        if previous == runtime_id:
            return {"status": "unchanged", "runtime_id": runtime_id}
        self._mininet_runtime_id = runtime_id
        with self.store._lock, self.store._connect() as connection:
            reset = connection.execute(
                """UPDATE observed_policy_states
                   SET repair_attempts=0, last_repair_at=NULL
                   WHERE policy_id IN (
                       SELECT policy_id FROM policies
                       WHERE status IN ('ACTIVE','DEGRADED','RECONCILING')
                   )"""
            ).rowcount
        result = {
            "status": "changed", "previous_runtime_id": previous,
            "runtime_id": runtime_id, "started_at": identity.get("started_at"),
            "retry_windows_reset": max(int(reset or 0), 0),
        }
        self.store.add_audit_event(
            event_type="DATA_PLANE_RUNTIME_CHANGED", resource_type="mininet_runtime",
            resource_id=runtime_id, expected={"runtime_id": previous},
            observed=identity, action={"operation": "RESET_REPAIR_WINDOWS"},
            result=result,
        )
        return result

    def _active_policies(self) -> dict[str, dict[str, Any]]:
        # Do not duplicate the desired-owner query here.  Restart recovery's
        # egress readback consumes the same PolicyStore resolver, so a
        # degraded-but-committed scope cannot be interpreted as orphaned by
        # one consumer and current by another.
        return PolicyStore(self.store).resolve_managed_desired_policies()

    def _save_desired(self, row: dict[str, Any], binding) -> None:
        expected = {
            "policy": json.loads(row["yang_json"]),
            "runtime": json.loads(row.get("runtime_json") or "{}"),
            "resource_binding": binding.model_dump(mode="json"),
        }
        now = _now()
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                """INSERT INTO desired_policy_states(
                    policy_id, traffic_scope_key, desired_state, version, expected_json, updated_at
                ) VALUES (?, ?, 'ACTIVE', ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    traffic_scope_key=excluded.traffic_scope_key,
                    desired_state=excluded.desired_state, version=excluded.version,
                    expected_json=excluded.expected_json, updated_at=excluded.updated_at""",
                (row["policy_id"], row["traffic_scope_key"], int(row.get("version") or 1),
                 json.dumps(expected, default=str), now),
            )

    def _restore_binding(self, row: dict[str, Any]):
        """Recreate a lost allocation from the durable policy desired state.

        TC state is intentionally ephemeral and a Mininet restart may also be
        accompanied by a cleared allocation table.  The policy document is
        therefore the source of truth; recovery must not depend on an existing
        runtime binding in order to create that binding.
        """
        policy_model = YANGPolicy(**json.loads(row["yang_json"]))
        policy = policy_model.network_policy
        qos = qos_values(policy)
        bandwidth = qos.get("bandwidth")
        if bandwidth is None:
            raise RuntimeError("QOS_BINDING_RECOVERY_REQUIRES_BANDWIDTH")
        mode = effective_qos_mode(policy)
        service_id = policy_model.service_id
        if not service_id:
            try:
                from api.service_profiles import resolve_service_profile_for_policy

                service_id = resolve_service_profile_for_policy(policy_model).service_id
            except (KeyError, ValueError):
                logger.info(
                    "No unique service profile for recovered policy_id=%s",
                    row["policy_id"],
                )
        tenant_id = "default"
        if service_id:
            try:
                from api.service_profiles import get_service_profile

                tenant_id = get_service_profile(service_id).tenant_id
            except Exception:
                logger.warning("Unable to resolve tenant for recovered service_id=%s", service_id)
        return self.allocator.allocate(
            policy_id=row["policy_id"],
            service_id=service_id,
            traffic_scope_key=row["traffic_scope_key"],
            rate_mbps=float(bandwidth),
            ceil_mbps=(
                float(bandwidth)
                if mode == "RATE_LIMIT"
                else float(os.getenv("IDN_QOS_LINK_CEIL_MBPS", "100"))
            ),
            dscp=qos.get("dscp_value"),
            reservation_kind="RATE_LIMIT" if mode == "RATE_LIMIT" else "GUARANTEED",
            tenant_id=tenant_id,
        )

    def _save_observed(
        self, row: dict[str, Any], drift_type: str, observed: dict[str, Any],
        *, repaired: bool = False,
    ) -> None:
        now = _now()
        observed_state = "ACTIVE" if drift_type == "IN_SYNC" else "DEGRADED"
        with self.store._lock, self.store._connect() as connection:
            current = connection.execute(
                "SELECT repair_attempts, last_repair_at FROM observed_policy_states WHERE policy_id=?",
                (row["policy_id"],),
            ).fetchone()
            attempts = int((current or {"repair_attempts": 0})["repair_attempts"] or 0)
            if repaired:
                attempts += 1
            elif drift_type == "IN_SYNC":
                attempts = 0
            last_repair = now if repaired else ((current or {"last_repair_at": None})["last_repair_at"])
            connection.execute(
                """INSERT INTO observed_policy_states(
                    policy_id, traffic_scope_key, observed_state, drift_type, observed_json,
                    checked_at, last_repair_at, repair_attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    traffic_scope_key=excluded.traffic_scope_key,
                    observed_state=excluded.observed_state, drift_type=excluded.drift_type,
                    observed_json=excluded.observed_json, checked_at=excluded.checked_at,
                    last_repair_at=excluded.last_repair_at, repair_attempts=excluded.repair_attempts""",
                (row["policy_id"], row["traffic_scope_key"], observed_state, drift_type,
                 json.dumps(observed, default=str), now, last_repair, attempts),
            )
            connection.execute(
                "UPDATE policies SET status=?, updated_at=? WHERE policy_id=?",
                (observed_state, now, row["policy_id"]),
            )

    def _tc_payload(self, binding, row: dict[str, Any] | None = None) -> dict[str, Any]:
        runtime = dict(binding.runtime or {})
        selector = dict(runtime.get("selector") or {})
        policy = None
        if row is not None:
            policy = YANGPolicy(**json.loads(row["yang_json"])).network_policy
        match = policy.match if policy is not None else None
        payload = {
            "scope_key": binding.traffic_scope_key,
            "policy_id": binding.policy_id,
            "source_ip": selector.get("source_ip") or getattr(match, "source_ip", None),
            "destination_ip": selector.get("destination_ip") or getattr(match, "destination_ip", None),
            "protocol": selector.get("protocol") or getattr(match, "protocol", None) or "ANY",
            "source_port": (
                selector.get("source_port")
                if selector.get("source_port") is not None
                else getattr(match, "source_port", None)
            ),
            "destination_port": (
                selector.get("destination_port")
                if selector.get("destination_port") is not None
                else getattr(match, "destination_port", None)
            ),
            "bandwidth_mbps": runtime.get("configured_bandwidth_mbps") or binding.rate_mbps,
            "burst_kb": 64,
            "qos_mode": runtime.get("qos_mode") or (
                "RATE_LIMIT" if binding.reservation_kind == "RATE_LIMIT" else "MIN_BANDWIDTH"
            ),
            "dscp": binding.dscp if binding.dscp is not None else 0,
            "priority": 3 if binding.reservation_kind == "RATE_LIMIT" else 1,
            "class_id": binding.class_id,
            "filter_id": binding.filter_id,
        }
        # Runtime is readback/audit evidence, not Desired State.  In
        # particular, a rolled-back staged update can retain the candidate's
        # historical QoS mode in runtime_json.  Recovery must restore the
        # committed YANG action rather than replay that abandoned candidate.
        if policy is not None and policy.action.qos_params is not None:
            from models.yang_policy import ActionType

            declared_qos = policy.action.qos_params
            # Preference-only priority policies legitimately omit a numeric
            # QoS target.  Their active binding remains the committed
            # resource authority; attempting to coerce ``None`` prevented
            # the formal reconciler from even observing or repairing them.
            if declared_qos.bandwidth is not None:
                payload["bandwidth_mbps"] = float(declared_qos.bandwidth)
            payload["qos_mode"] = (
                "RATE_LIMIT"
                if policy.action.action_type == ActionType.RATE_LIMIT
                else "MIN_BANDWIDTH"
            )
        # Reconciliation is allowed to repair a desired tree, but it must not
        # silently fall back to nominal physical capacity.  The record lookup
        # is keyed by the durable egress resource, never a service identity.
        if binding.resource_key != "UNBOUND" and payload["qos_mode"] == "MIN_BANDWIDTH":
            from infra.resource_registry import get_resource_registry
            from infra.stable_capacity import StableCapacityRepository

            record = StableCapacityRepository(self.store).active_for(
                bottleneck_egress=binding.resource_key,
                topology_id=get_resource_registry().snapshot().topology_name,
            )
            if record is not None:
                payload["stable_capacity"] = {
                    **record.model_dump(mode="json"),
                    # The Mininet actuator validates this evidence as the
                    # unrounded calibrated equation (stable minus safety).
                    # Its separate whole-Mbit scheduler conversion happens
                    # only after ``authorized_schedulable_capacity_mbps`` is
                    # applied.  Supplying the rounded HTB value here made a
                    # valid fractional calibration look self-inconsistent
                    # for scopes without an ingress-desired override.
                    "schedulable_capacity_mbps": StableCapacityRepository.schedulable_capacity(record),
                }
        # A current ingress desired state has exactly one authority for egress
        # QoS.  Do not recover from a former egress snapshot, binding runtime,
        # or a TC observation: all three are mutable or historical evidence.
        resolution = resolve_egress_qos_desired_for_scope(
            binding.traffic_scope_key, store=self.store,
            capacity_purpose="RECONCILE_EXISTING",
        )
        if resolution.status != "INGRESS_DESIRED_MISSING":
            payload["egress_resolution"] = resolution.model_dump(mode="json")
        if resolution.accepted:
            selector = resolution.selector
            payload.update({
                "source_ip": selector["source_prefix"], "destination_ip": selector["destination_prefix"],
                "protocol": {1: "icmp", 6: "tcp", 17: "udp"}.get(int(selector["ip_protocol"]), str(selector["ip_protocol"])),
                "destination_port": int(selector["destination_port"]), "dscp": int(selector["dscp"]),
                "bandwidth_mbps": float(resolution.target_rate_mbps), "class_id": binding.class_id,
                "authorized_schedulable_capacity_mbps": resolution.root_rate_mbps,
                "durable_scheduler_contract_mbps": resolution.root_rate_mbps,
                "egress_desired_generation": resolution.ingress_desired_generation_id,
            })
            capacity_provenance = dict(resolution.source_provenance.get("capacity") or {})
            # A degraded recovery is driven by the durable scheduler contract.
            # Do not feed an absent runtime measurement into the actuator's
            # stable-capacity numeric validator.
            if capacity_provenance.get("runtime_capacity_status") == "CURRENT":
                payload["stable_capacity"] = {"status": "ACTIVE", **capacity_provenance}
        else:
            shared_root = self._shared_resource_authorized_root(binding)
            if shared_root is not None:
                payload["authorized_schedulable_capacity_mbps"] = shared_root
                # This scope shares an HTB tree with an ingress-desired
                # sibling.  The sibling's committed contract is therefore
                # the durable authority for recovery as well; sending only
                # ``authorized_*`` lets the actuator fall back to physical
                # capacity when runtime calibration is degraded.
                payload["durable_scheduler_contract_mbps"] = shared_root
            elif (binding.resource_key != "UNBOUND" and payload["qos_mode"] == "MIN_BANDWIDTH"
                  and not payload.get("stable_capacity")):
                # An unprotected MIN_BANDWIDTH scope still cannot borrow more
                # than its resolved physical link.  The process-level ceiling
                # is an upper deployment limit, not evidence that a 100 Mbps
                # binding may be read back as 1000 Mbps.  Using that raw env
                # value here made an already-correct TC 10/100 class churn
                # through the resident retry budget after a restart.
                try:
                    from infra.resource_registry import get_resource_registry
                    physical = next(
                        (float(item.capacity_mbps)
                         for item in get_resource_registry().snapshot().links
                         if item.resource_id == binding.resource_key),
                        None,
                    )
                    if physical is not None:
                        payload["authorized_schedulable_capacity_mbps"] = min(
                            physical, float(os.getenv("IDN_QOS_LINK_CEIL_MBPS", "100")),
                        )
                except Exception:
                    # Observation remains available when topology refresh is
                    # unavailable; the actuator will resolve the path again
                    # before any eventual write.
                    pass
        return payload

    async def _observe_policy(self, row: dict[str, Any], binding, *, reconcile_id: str = "", recovery_id: str | None = None) -> tuple[str, dict[str, Any]]:
        payload = self._tc_payload(binding, row)
        if not payload["source_ip"] or not payload["destination_ip"]:
            return "UNKNOWN", {"error": "desired selector is absent from runtime binding"}
        probe = await self._probe()
        resolution_data = payload.get("egress_resolution") or {}
        egress_resolution = None
        egress_diff = None
        if resolution_data:
            from infra.egress_qos_desired import EgressQosDesiredResolution
            egress_resolution = EgressQosDesiredResolution.model_validate(resolution_data)
        # A capacity guard may correctly block TC re-application, but it must
        # not suppress P4 classifier observation.  The classifier is part of
        # the durable baseline and is also the tuple a later capacity probe
        # must exercise.  Returning here used to strand a missing research
        # classifier while an unrelated egress admission refresh was blocked.
        capacity_guard_blocked = bool(egress_resolution and not egress_resolution.accepted)
        if capacity_guard_blocked:
            tc = {
                "status": "CAPACITY_GUARD_BLOCKED",
                "config_applied": False,
                "reason": egress_resolution.status,
            }
            tc_ok = False
        elif egress_resolution:
            # The protected recovery scope is read through the dedicated GET
            # operation.  It must never use legacy VERIFY, which can write a
            # default class after a successful comparison.
            tc = await probe.get("/policy/qos-readback", {"scope_key": binding.traffic_scope_key})
            desired_semantic = canonicalize_egress_desired(
                root_rate=egress_resolution.root_rate_mbps, root_ceil=egress_resolution.root_ceil_mbps,
                target_classid=payload["class_id"], target_rate=egress_resolution.target_rate_mbps, target_ceil=egress_resolution.target_ceil_mbps,
                default_classid="1:fffe", default_rate=egress_resolution.default_rate_mbps, default_ceil=egress_resolution.default_ceil_mbps,
                filter_kind="flower_dscp_plus_five_tuple", filter_pref=payload.get("filter_id"),
                selector=payload, dscp=payload.get("dscp"), owner=binding.policy_id,
                scope=binding.traffic_scope_key, generation=egress_resolution.ingress_desired_generation_id,
            )
            observed_semantic = canonicalize_egress_observed(tc or {}, owner=binding.policy_id,
                scope=binding.traffic_scope_key, generation=egress_resolution.ingress_desired_generation_id)
            egress_diff = reconcile_semantic_diff(reconcile_id=reconcile_id, recovery_id=recovery_id,
                resource_kind="EGRESS", desired=desired_semantic, observed=observed_semantic,
                ignored_volatile_fields=VOLATILE_EGRESS_FIELDS)
            tc_ok = bool(tc and tc.get("status") == "ok" and tc.get("config_applied") and not egress_diff["write_required"])
        else:
            # VERIFY used to be a POST that could reconcile TC state.  A
            # resident observation must not turn a successful comparison into
            # an actuator call, regardless of whether this scope has an
            # ingress-desired record.  The probe's GET primitive reads the
            # currently selected class/filter only.
            tc = await probe.get("/policy/qos-readback", {"scope_key": binding.traffic_scope_key})
            rate_ok = bool(tc and float(tc.get("rate_mbps") or -1) == float(binding.rate_mbps))
            # A minimum-bandwidth binding may carry a historic admission
            # ceiling.  The current TC ceiling is governed by the current
            # stable-capacity contract (or the explicit deployment default),
            # never by that mutable historical allocation field.
            ceil_expected = (
                float(binding.rate_mbps) if payload["qos_mode"] == "RATE_LIMIT"
                else float(payload.get("authorized_schedulable_capacity_mbps") or (payload.get("stable_capacity") or {}).get(
                    "schedulable_capacity_mbps", os.getenv("IDN_QOS_LINK_CEIL_MBPS", "100")
                ))
            )
            ceil_ok = bool(tc and float(tc.get("ceil_mbps") or -1) == ceil_expected)
            class_ok = bool(tc and str(tc.get("class_id") or "").lower() == str(binding.class_id).lower())
            observed_binding = dict((tc or {}).get("qos_binding") or {})
            binding_on_active_path = bool(
                observed_binding
                and observed_binding.get("device_id") == binding.device_id
                and observed_binding.get("interface_name") == binding.interface_id
                and observed_binding.get("active", True)
            )
            if tc:
                tc = {**tc, "readback_checks": {
                    "rate_ok": rate_ok, "ceil_ok": ceil_ok, "class_ok": class_ok,
                    "binding_on_active_path": binding_on_active_path,
                    "expected_rate_mbps": float(binding.rate_mbps),
                    "expected_ceil_mbps": ceil_expected,
                    "expected_class_id": binding.class_id,
                }}
            tc_ok = bool(
                tc and tc.get("status") == "ok" and tc.get("config_applied")
                and rate_ok and ceil_ok and class_ok and binding_on_active_path
            )
        p4: dict[str, Any] = {"status": "NOT_CHECKED"}
        p4_ok = True
        runtime = json.loads(row.get("runtime_json") or "{}")
        p4_diff = None
        if runtime.get("installed_flows"):
            try:
                if egress_resolution and egress_resolution.accepted:
                    # Protected recovery scope shares the direct P4Runtime
                    # readback canonicalizer with startup recovery.
                    from api.service_profiles import get_service_profile
                    from infra.recovery_readback import P4RecoveryReadbackService
                    profile = get_service_profile(egress_resolution.service_profile_id)
                    p4 = await P4RecoveryReadbackService().collect(recovery_id=recovery_id or f"OBS-{reconcile_id}", verification_phase="RECONCILE_OBSERVE", execution_generation=egress_resolution.ingress_desired_generation_id, runtime_instance_epoch="OBSERVE", profile=profile)
                    desired_p4 = canonicalize_p4_desired(device_id=(binding.runtime or {}).get("qos_binding", {}).get("classifier_device_id") or binding.device_id, table=(profile.p4_counter_binding or {}).get("table"), selector={"source_ip": payload["source_ip"], "destination_ip": payload["destination_ip"], "protocol": payload["protocol"], "destination_port": payload["destination_port"]}, priority=101, action="MyIngress.set_dscp", action_params=[{"param_id": 1, "value": f"{int(payload['dscp']):02x}"}], dscp=payload["dscp"], counter_binding=(profile.p4_counter_binding or {}).get("counter"), owner=binding.policy_id, generation=egress_resolution.ingress_desired_generation_id, scope=binding.traffic_scope_key)
                    observed_p4 = canonicalize_p4_observed(p4, owner=binding.policy_id, generation=egress_resolution.ingress_desired_generation_id, scope=binding.traffic_scope_key)
                    p4_diff = reconcile_semantic_diff(reconcile_id=reconcile_id, recovery_id=recovery_id, resource_kind="P4", desired=desired_p4, observed=observed_p4, ignored_volatile_fields=VOLATILE_P4_FIELDS)
                    p4_ok = p4.get("status") == "P4_RECOVERY_READBACK_VERIFIED" and not p4_diff["write_required"]
                else:
                    from api.qos import verify_p4_classifier
                    p4 = await verify_p4_classifier(json.loads(row["yang_json"]), runtime)
                    p4_ok = bool((p4.get("status") == "ok" and p4.get("verification_state") == "P4_CLASSIFIER_VERIFIED" and p4.get("config_applied")) or p4.get("status") == "VERIFIED")
            except Exception as exc:
                p4, p4_ok = {"status": "MISSING", "error": str(exc)}, False
        if tc_ok and p4_ok:
            drift = "IN_SYNC"
        elif (tc or {}).get("status") in {"not_found", "unavailable", "error"} or p4.get("status") == "MISSING":
            drift = "MISSING"
        else:
            drift = "DRIFTED"
        return drift, {
            "tc": tc or {}, "p4": p4, "egress_resolution": resolution_data,
            "checks": {"tc_ok": tc_ok, "p4_ok": p4_ok},
            "semantic_diffs": {"p4": p4_diff, "egress": egress_diff if egress_resolution else None},
            "capacity_guard_blocked": capacity_guard_blocked,
        }

    async def _reconcile_ingress_admission(self, runtime_epoch: dict[str, Any], *, auto_repair: bool) -> dict[str, Any]:
        """Restore an ingress tree only from durable rules, never a prior PID.

        The Mininet actuator resolves its attachment from the current topology
        on every APPLY.  A restart with no workload is deliberately valid: the
        deterministic IDLE_BORROWING tree is the recovery baseline.
        """
        plan_id = "INGRESS-WC-20260805-03"
        desired = self.ingress_desired.active(plan_id)
        if desired is None:
            return {"status": "NO_DESIRED_STATE"}
        synchronized = self.ingress_desired.synchronize_committed_minimum_policy(plan_id)
        desired = synchronized.get("record") or desired
        probe = await self._probe()
        observed = await probe.get("/policy/ingress-admission", {"admission_plan_id": plan_id}) or {}
        observed_floor = float(observed.get("implementation_reserved_mbps") or 0)
        desired_floor = float((desired.get("desired") or {}).get("guaranteed_floor_mbps") or 0)
        observed_current = (
            observed.get("status") == "VERIFIED"
            and observed.get("desired_generation") == desired["generation_id"]
            and observed_floor == desired_floor
        )
        if observed_current and not synchronized.get("changed"):
            return {"status": "VERIFIED_NO_CHANGE", "desired_generation": desired["generation_id"], "observed": observed}
        if observed.get("status") not in {"NOT_FOUND", "VERIFIED"} and not synchronized.get("changed"):
            return {"status": "DRIFTED", "reason": "INGRESS_ADMISSION_READBACK_INVALID", "observed": observed}
        reason = (
            "INGRESS_ADMISSION_COMMITTED_POLICY_REVISION"
            if synchronized.get("changed") or observed.get("status") == "VERIFIED"
            else "INGRESS_ADMISSION_MISSING_AFTER_RUNTIME_RESTART"
            if runtime_epoch.get("status") == "changed"
            else "INGRESS_ADMISSION_MISSING"
        )
        self.store.add_audit_event(
            event_type="INGRESS_ADMISSION_DRIFT_DETECTED", resource_type="ingress_admission",
            resource_id=plan_id, correlation={"desired_generation": desired["generation_id"]},
            expected=desired["desired"], observed=observed, action={"reason": reason}, result={"status": "DRIFTED"},
        )
        if not auto_repair:
            return {"status": "DRIFTED", "reason": reason, "observed": observed}
        fencing = self.resident_fencing_state()
        if fencing["status"] == "DEFERRED_BY_RECOVERY" and not self._current_recovery_owns_lease(fencing):
            self.store.add_audit_event(event_type="RESIDENT_RECONCILER_DEFERRED_BY_RECOVERY",
                resource_type="network_write", resource_id=plan_id, expected=desired["desired"], observed=observed,
                action={"operation": "INGRESS_ADMISSION_REPAIR", "write_executed": False,
                        "active_fencing_token": (fencing.get("active_lease") or {}).get("fencing_token")},
                result={"status": "DEFERRED_BY_RECOVERY"})
            return {"status": "DEFERRED_BY_RECOVERY", "reason": "ACTIVE_RESTART_RECOVERY_LEASE", "observed": observed}
        authorization = None
        if os.getenv("IDN_NETWORK_WRITE_FENCING_REQUIRED", "false").lower() == "true":
            authorization = require_network_write(operation="RECONCILER_INGRESS_REPAIR", store=self.store)
            if not authorization["authorized"]:
                return {"status": "FENCED", "reason": authorization["reason"], "authorization": authorization,
                        "write_executed": False, "observed": observed}
        allocation = idle_borrowing_allocation(desired)
        self.store.add_audit_event(
            event_type="INGRESS_ADMISSION_RECOVERY_STARTED", resource_type="ingress_admission",
            resource_id=plan_id, correlation={"desired_generation": desired["generation_id"]},
            expected=desired["desired"], observed=observed,
            action={"operation": "APPLY_DYNAMIC_ATTACHMENT", "allocation_mode": "IDLE_BORROWING"}, result={"status": "STARTED"},
        )
        applied = await probe.post("/policy/ingress-admission", allocation)
        if authorization is not None:
            NetworkWriteFencing(self.store).record_execution(authorization, write_executed=bool(applied and applied.get("status") == "VERIFIED"))
        if not applied or applied.get("status") != "VERIFIED":
            self.store.add_audit_event(
                event_type="INGRESS_ADMISSION_RECOVERY_FAILED", resource_type="ingress_admission",
                resource_id=plan_id, correlation={"desired_generation": desired["generation_id"]},
                expected=allocation, observed=applied or {}, action={"operation": "APPLY_DYNAMIC_ATTACHMENT"},
                result={"status": "FAILED"},
            )
            return {"status": "FAILED", "reason": "INGRESS_ADMISSION_APPLY_FAILED", "observed": applied}
        allocation_record = self.ingress_desired.append_allocation(desired["generation_id"], allocation, "APPLIED")
        readback = await probe.get("/policy/ingress-admission", {"admission_plan_id": plan_id}) or {}
        verified = readback.get("status") == "VERIFIED"
        self.store.add_audit_event(
            event_type="INGRESS_ADMISSION_READBACK_VERIFIED" if verified else "INGRESS_ADMISSION_RECOVERY_FAILED",
            resource_type="ingress_admission", resource_id=plan_id,
            correlation={"desired_generation": desired["generation_id"], "allocation_id": allocation_record["allocation_id"]},
            expected=allocation, observed=readback, action={"operation": "READBACK"},
            result={"status": "VERIFIED" if verified else "FAILED"},
        )
        return {"status": "RECOVERED" if verified else "FAILED", "desired_generation": desired["generation_id"],
                "allocation": allocation_record, "observed": readback}

    async def activate_ingress_runtime_allocation(
        self, *, target_demand_mbps: float, active_best_effort_scope_ids: set[str],
        traffic_scope_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply a declared validation allocation through the reconciler only.

        A validation workload may activate one or more best-effort contenders.
        Resolve the admission generation from the policy scope in that case;
        the historical plan identifier is only a compatibility fallback for
        callers that predate scoped desired state.
        """
        desired = (
            self.ingress_desired.active_for_scope(str(traffic_scope_key))
            if traffic_scope_key else None
        )
        if desired is None:
            desired = self.ingress_desired.active("INGRESS-WC-20260805-03")
        if desired is None:
            raise RuntimeError("INGRESS_ADMISSION_DESIRED_STATE_MISSING")
        plan_id = str(desired["admission_plan_id"])
        fencing_required = os.getenv("IDN_NETWORK_WRITE_FENCING_REQUIRED", "false").lower() == "true"
        # Status assurance runs after the execution node has released its
        # policy-write lease.  Runtime admission allocation is still a real
        # southbound write, so it must acquire its own narrowly scoped lease
        # instead of failing closed or bypassing fencing.
        if fencing_required and not current_write_context():
            lease = NetworkWriteFencing(self.store).acquire(
                owner_type="VALIDATION_INGRESS_ALLOCATION",
                owner_id=plan_id,
                recovery_id=f"INGRESS-ALLOCATION-{plan_id}",
                resource_scope=resource_write_scope(f"ingress_admission:{plan_id}"),
                ttl_seconds=120,
            )
            try:
                with write_context(
                    owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                    fencing_token=int(lease["fencing_token"]),
                    resource_scope=lease["resource_scope"],
                ):
                    return await self.activate_ingress_runtime_allocation(
                        target_demand_mbps=target_demand_mbps,
                        active_best_effort_scope_ids=active_best_effort_scope_ids,
                        traffic_scope_key=traffic_scope_key,
                    )
            finally:
                NetworkWriteFencing(self.store).release(lease["lease_id"])
        allocation = declared_contention_allocation(
            desired, target_demand_mbps=target_demand_mbps,
            active_best_effort_scope_ids=active_best_effort_scope_ids,
        )
        authorization = None
        if os.getenv("IDN_NETWORK_WRITE_FENCING_REQUIRED", "false").lower() == "true":
            authorization = require_network_write(operation="RECONCILER_INGRESS_RUNTIME_ALLOCATION", store=self.store)
            if not authorization["authorized"]:
                raise ReconciliationRetryDeferred(authorization["reason"])
        applied = await (await self._probe()).post("/policy/ingress-admission", allocation)
        if authorization is not None:
            NetworkWriteFencing(self.store).record_execution(authorization, write_executed=bool(applied and applied.get("status") == "VERIFIED"))
        if not applied or applied.get("status") != "VERIFIED":
            raise RuntimeError(f"INGRESS_ADMISSION_RUNTIME_ALLOCATION_FAILED:{applied}")
        saved = self.ingress_desired.append_allocation(desired["generation_id"], allocation, "APPLIED")
        self.store.add_audit_event(
            event_type="INGRESS_ADMISSION_RUNTIME_ALLOCATION_APPLIED", resource_type="ingress_admission",
            resource_id=plan_id, correlation={"desired_generation": desired["generation_id"], "allocation_id": saved["allocation_id"]},
            expected=allocation, observed=applied, action={"operation": "DECLARED_VALIDATION_ALLOCATION"},
            result={"status": "VERIFIED"},
        )
        return {"allocation": saved, "readback": applied}

    async def _repair(self, row: dict[str, Any], binding, observed: dict[str, Any]) -> dict[str, Any]:
        """Repair under a resident lease when fencing is enabled.

        Recovery may call us with its own write context.  Ordinary resident
        reconciliation instead acquires its short-lived lease explicitly so a
        real drift repair is never downgraded into an unauthorised no-op.
        """
        required = os.getenv("IDN_NETWORK_WRITE_FENCING_REQUIRED", "false").lower() == "true"
        if not required or current_write_context():
            return await self._repair_under_context(row, binding, observed)
        try:
            lease = NetworkWriteFencing(self.store).acquire(
                owner_type="RESIDENT_RECONCILER", owner_id="network-reconciler",
                recovery_id=f"REC-REPAIR-{row['policy_id']}",
                resource_scope=resource_write_scope(binding.resource_key), ttl_seconds=120,
            )
        except RuntimeError as exc:
            if str(exc) == "NETWORK_WRITE_LEASE_HELD_BY_CAPACITY_CALIBRATION":
                raise ReconciliationRetryDeferred("SKIPPED_RESOURCE_BUSY") from exc
            if str(exc) in {"NETWORK_WRITE_LEASE_HELD_BY_RECOVERY", "NETWORK_WRITE_LEASE_HELD_BY_OWNER", "NETWORK_WRITE_LEASE_HELD_BY_ACTIVE_WRITER"}:
                raise ReconciliationRetryDeferred("DEFERRED_BY_ACTIVE_RESTART_RECOVERY") from exc
            raise
        try:
            with write_context(
                owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                fencing_token=int(lease["fencing_token"]),
                resource_scope=lease["resource_scope"],
            ):
                return await self._repair_under_context(row, binding, observed)
        finally:
            NetworkWriteFencing(self.store).release(lease["lease_id"])

    async def _repair_under_context(self, row: dict[str, Any], binding, observed: dict[str, Any]) -> dict[str, Any]:
        # ``run_once`` observes a snapshot while a workflow may commit a
        # newer revision.  Acquiring a fenced lease does not by itself make
        # that old snapshot current: an already-running resident must verify
        # authority again immediately before its first southbound write.
        # Otherwise a long observation can overwrite a just-committed update
        # (for example 20->10) with its earlier 20 Mbps target.
        from persistence.policy_store import PolicyStore
        current_row = PolicyStore(self.store).resolve_managed_desired_for_scope(
            row["traffic_scope_key"]
        )
        current_binding = self.allocator.get_active_for_scope(row["traffic_scope_key"])
        if (
            current_row is None
            or current_binding is None
            or str(current_row.get("policy_id")) != str(row.get("policy_id"))
            or int(current_row.get("version") or 0) != int(row.get("version") or 0)
            or str(current_row.get("yang_json")) != str(row.get("yang_json"))
            or str(current_binding.binding_id) != str(binding.binding_id)
            or float(current_binding.rate_mbps) != float(binding.rate_mbps)
        ):
            self.store.add_audit_event(
                event_type="RECONCILIATION_STALE_SNAPSHOT_DEFERRED",
                resource_type="policy", resource_id=row["policy_id"],
                correlation={"traffic_scope_key": row["traffic_scope_key"]},
                expected={"policy_id": row["policy_id"], "version": row.get("version"),
                          "binding_id": binding.binding_id, "binding_rate_mbps": binding.rate_mbps},
                observed={"policy_id": (current_row or {}).get("policy_id"),
                          "version": (current_row or {}).get("version"),
                          "binding_id": getattr(current_binding, "binding_id", None),
                          "binding_rate_mbps": getattr(current_binding, "rate_mbps", None)},
                action={"operation": "REPAIR", "write_executed": False},
                result={"status": "STALE_POLICY_SNAPSHOT"},
            )
            raise ReconciliationRetryDeferred("STALE_POLICY_SNAPSHOT")
        fencing = self.resident_fencing_state(binding.resource_key)
        if fencing["status"] == "SKIPPED_RESOURCE_BUSY":
            self.store.add_audit_event(event_type="RESIDENT_RECONCILER_SKIPPED_RESOURCE_BUSY",
                resource_type="network_write", resource_id=binding.resource_key, expected={}, observed=observed,
                action={"operation": "REPAIR", "write_executed": False,
                        "active_fencing_token": (fencing.get("active_lease") or {}).get("fencing_token")},
                result={"status": "SKIPPED_RESOURCE_BUSY"})
            raise ReconciliationRetryDeferred("SKIPPED_RESOURCE_BUSY")
        if fencing["status"] == "DEFERRED_BY_RECOVERY" and not self._current_recovery_owns_lease(fencing):
            event = {
                "writer_type": "RESIDENT_RECONCILER", "writer_id": "network-reconciler",
                "route": "/policy/qos", "operation": "REPAIR", "write_executed": False,
                "authorization_result": "DEFERRED_BY_RECOVERY",
                "active_fencing_token": (fencing.get("active_lease") or {}).get("fencing_token"),
            }
            self.store.add_audit_event(event_type="RESIDENT_RECONCILER_DEFERRED_BY_RECOVERY",
                resource_type="network_write", resource_id=row["policy_id"], expected={}, observed=observed,
                action=event, result={"status": "DEFERRED_BY_RECOVERY"})
            raise ReconciliationRetryDeferred("DEFERRED_BY_RECOVERY")
        if os.getenv("IDN_NETWORK_WRITE_FENCING_REQUIRED", "false").lower() == "true":
            authorization = require_network_write(operation="RECONCILER_QOS_REPAIR", store=self.store)
            if not authorization["authorized"]:
                self.store.add_audit_event(event_type="RESIDENT_RECONCILER_WRITE_REJECTED_BY_FENCING",
                    resource_type="network_write", resource_id=row["policy_id"], expected={}, observed=observed,
                    action={"writer_type": "RESIDENT_RECONCILER", "operation": "REPAIR", "authorization": authorization},
                    result={"write_executed": False, "status": authorization["reason"]})
                raise ReconciliationRetryDeferred(authorization["reason"])
        max_attempts = max(int(os.getenv("IDN_RECONCILE_MAX_ATTEMPTS", "3")), 1)
        runtime = json.loads(row.get("runtime_json") or "{}")
        with self.store._lock, self.store._connect() as connection:
            state = connection.execute(
                "SELECT repair_attempts, last_repair_at FROM observed_policy_states WHERE policy_id=?",
                (row["policy_id"],),
            ).fetchone()
        recovery_context = (current_write_context() or {}).get("owner_type") == "RESTART_RECOVERY"
        if state and int(state["repair_attempts"] or 0) >= max_attempts and not recovery_context:
            # A retry ceiling protects an unhealthy data plane from a tight
            # repair loop; it must not permanently brick durable desired
            # state.  A newly reconstructed/unbound allocation is a material
            # state change and starts a fresh retry window immediately.  An
            # unchanged binding is retried after a bounded cool-down.
            schema_repair_pending = bool(
                (observed.get("p4") or {}).get("status") == "MISSING"
                and (runtime.get("installed_flows") or runtime.get("delivered_rules"))
                and not runtime.get("classifier_schema_rebuilt")
            )
            fresh_binding = (
                binding.resource_key == "UNBOUND" or not binding.runtime
                or schema_repair_pending
            )
            retry_after = max(
                float(os.getenv("IDN_RECONCILE_RETRY_COOLDOWN_SECONDS", "60")), 1.0
            )
            last_repair = state["last_repair_at"]
            cooled_down = False
            if last_repair:
                try:
                    repaired_at = datetime.fromisoformat(str(last_repair).replace("Z", "+00:00"))
                    cooled_down = (datetime.now(timezone.utc) - repaired_at).total_seconds() >= retry_after
                except (TypeError, ValueError):
                    cooled_down = True
            if not fresh_binding and not cooled_down:
                raise ReconciliationRetryDeferred("RECONCILIATION_RETRY_LIMIT")
            with self.store._lock, self.store._connect() as connection:
                connection.execute(
                    "UPDATE observed_policy_states SET repair_attempts=0 WHERE policy_id=?",
                    (row["policy_id"],),
                )
        elif state and int(state["repair_attempts"] or 0) >= max_attempts and recovery_context:
            # A fenced restart recovery is an isolated bounded repair window,
            # not another resident retry.  It may restore committed desired
            # state once, but cannot create reservations or bypass its lease.
            with self.store._lock, self.store._connect() as connection:
                connection.execute(
                    "UPDATE observed_policy_states SET repair_attempts=0 WHERE policy_id=?",
                    (row["policy_id"],),
                )
        # A cold P4 entry must not cause an otherwise equivalent egress tree
        # to be re-applied.  Keep the repair set resource-specific.
        tc_observed = observed.get("tc") or {}
        egress_diff = ((observed.get("semantic_diffs") or {}).get("egress") or {})
        # A successful GET only proves TC is configured, not that it matches
        # the resolver's desired contract.  Canonical semantic drift requires
        # the official actuator path to run.
        tc_in_sync = bool(
            self.egress_desired.active(binding.traffic_scope_key)
            and tc_observed.get("status") == "ok"
            and tc_observed.get("config_applied")
            and not egress_diff.get("write_required", True)
        )
        tc_result = tc_observed
        # Capacity admission remains authoritative for egress QoS.  A blocked
        # admission may repair its P4 baseline below, but it cannot install or
        # mutate an HTB tree from stale/insufficient capacity evidence.
        tc_repair_allowed = not bool(observed.get("capacity_guard_blocked"))
        if not tc_in_sync and tc_repair_allowed:
            probe = await self._probe()
            tc_result = await probe.post(
                "/policy/qos", {**self._tc_payload(binding, row), "operation": "APPLY"}
            )
            if not tc_result or tc_result.get("status") != "ok" or not tc_result.get("config_applied"):
                raise RuntimeError(f"TC_REPAIR_FAILED: {tc_result}")
            self.allocator.bind_runtime(binding.binding_id, tc_result)

        p4_result: dict[str, Any] = {"status": "NOT_REQUIRED"}
        p4_missing = (observed.get("p4") or {}).get("status") in {
            "MISSING", "P4_ENTRY_NOT_FOUND", "P4_SELECTOR_MISMATCH",
            "P4_ACTION_MISMATCH", "P4_DSCP_MISMATCH", "P4_DIRECT_COUNTER_UNREADABLE",
        }
        if p4_missing and (
            runtime.get("installed_flows") or runtime.get("delivered_rules")
        ):
            from api.qos import ensure_p4_classifier
            from persistence.policy_store import PolicyStore

            _verified, runtime, p4_result = await ensure_p4_classifier(
                json.loads(row["yang_json"]), runtime, store=self.store,
            )
            PolicyStore(self.store).commit_staged(
                row["traffic_scope_key"],
                intent_id=str(row.get("intent_id") or "NETWORK_RECONCILER"),
                runtime=runtime,
            )
        return {"tc": tc_result, "p4": p4_result, "tc_write_executed": bool(not tc_in_sync and tc_repair_allowed),
                "p4_restore_planned": p4_missing}

    async def run_once(self, *, auto_repair: bool = True, scope_filter: set[str] | None = None,
                       dry_run: bool = False, recovery_id: str | None = None) -> ReconciliationSummary:
        if self._lock.locked():
            now = _now()
            return ReconciliationSummary(
                run_id=f"REC-{uuid.uuid4().hex[:12]}", status="SKIPPED_SINGLE_FLIGHT",
                started_at=now, finished_at=now,
            )
        async with self._lock:
            started = _now()
            run_id = f"REC-{uuid.uuid4().hex[:12]}"
            summary = ReconciliationSummary(
                run_id=run_id, status="RUNNING", started_at=started, finished_at=started,
            )
            try:
                from persistence.policy_store import PolicyStore
                pending_recovery = PolicyStore(self.store).recover_stale_pending()
                summary.infrastructure["stale_pending_recovery"] = {
                    "status": "RECOVERED" if pending_recovery else "NONE",
                    "recoveries": pending_recovery,
                    "write_executed": bool(pending_recovery),
                }
                for recovery in pending_recovery:
                    self.store.add_audit_event(
                        event_type="STALE_PENDING_POLICY_RECOVERED",
                        resource_type="policy", resource_id=recovery["policy_id"],
                        correlation={"run_id": run_id, "intent_id": recovery.get("pending_intent_id")},
                        expected={"pending_update": "bounded_timeout"}, observed=recovery,
                        action={"operation": "ROLLBACK_STALE_PENDING"},
                        result={"status": recovery["result"]},
                    )
            except Exception as exc:
                summary.infrastructure["stale_pending_recovery"] = {"status": "FAILED", "error": str(exc)}
            policies = self._active_policies()
            if scope_filter is not None:
                policies = {scope: row for scope, row in policies.items() if scope in scope_filter}
            try:
                ownership = self.allocator.reconcile_binding_ownership(
                    policies, scopes=scope_filter,
                )
                summary.infrastructure["resource_binding_ownership"] = ownership
                if ownership["write_executed"]:
                    self.store.add_audit_event(
                        event_type="RESOURCE_BINDING_OWNERSHIP_RECONCILED",
                        resource_type="qos_resource_binding", resource_id="active_bindings",
                        correlation={"run_id": run_id}, expected={"owner": "committed_stable_policy"},
                        observed=ownership, action={"operation": "PROMOTE_COMMITTED_OWNER"},
                        result={"status": "RECONCILED"},
                    )
            except Exception as exc:
                summary.infrastructure["resource_binding_ownership"] = {"status": "FAILED", "error": str(exc)}
            try:
                reservation_integrity = self.allocator.reconcile_reservation_integrity()
                summary.infrastructure["resource_reservation_integrity"] = reservation_integrity
                if reservation_integrity["write_executed"]:
                    self.store.add_audit_event(
                        event_type="RESOURCE_RESERVATION_INTEGRITY_RECONCILED",
                        resource_type="resource_reservation",
                        resource_id="qos_resource_bindings",
                        correlation={"run_id": run_id},
                        expected={"active_reservation_requires_active_binding": True},
                        observed=reservation_integrity,
                        action={"operation": "RETIRE_OR_SYNCHRONIZE_DERIVED_RESERVATIONS"},
                        result={"status": "RECONCILED"},
                    )
            except Exception as exc:
                summary.infrastructure["resource_reservation_integrity"] = {
                    "status": "FAILED", "error": str(exc),
                }
            summary.infrastructure["mininet_runtime"] = await self._refresh_runtime_epoch()
            try:
                summary.infrastructure["ingress_admission"] = await self._reconcile_ingress_admission(
                    summary.infrastructure["mininet_runtime"], auto_repair=auto_repair,
                )
            except Exception as exc:
                summary.infrastructure["ingress_admission"] = {"status": "FAILED", "error": str(exc)}
            bindings = {item.traffic_scope_key: item for item in self.allocator.list_active()}
            for scope, row in policies.items():
                binding = bindings.get(scope)
                summary.checked += 1
                if binding is None:
                    runtime = json.loads(row.get("runtime_json") or "{}")
                    drift, observed = "MISSING", {
                        "resource_binding": None,
                        "p4": {
                            "status": "MISSING" if runtime.get("delivered_rules") else "NOT_REQUIRED"
                        },
                    }
                else:
                    if not dry_run:
                        self._save_desired(row, binding)
                    try:
                        drift, observed = await asyncio.wait_for(
                            self._observe_policy(row, binding, reconcile_id=run_id, recovery_id=recovery_id),
                            # Direct ONOS/P4Runtime convergence readback has
                            # its own bounded retry window.  A shorter outer
                            # timeout used to cancel it at 20 seconds and
                            # silently turn a cold-start missing classifier
                            # into an uncounted UNKNOWN observation.
                            timeout=float(os.getenv("IDN_RECONCILE_ITEM_TIMEOUT_SECONDS", "60")),
                        )
                    except Exception as exc:
                        drift, observed = "UNKNOWN", {"error": str(exc)}
                if drift == "IN_SYNC":
                    summary.in_sync += 1
                    if not dry_run:
                        self._save_observed(row, drift, observed)
                    continue
                if drift == "MISSING":
                    summary.missing += 1
                elif drift in {"DRIFTED", "UNKNOWN"}:
                    summary.drifted += 1
                if drift == "UNKNOWN":
                    summary.repair_failed.append({
                        "policy_id": row["policy_id"],
                        "error": f"OBSERVATION_FAILED:{observed.get('error', 'UNKNOWN')}",
                    })
                    self.store.add_audit_event(
                        event_type="RECONCILIATION_OBSERVATION_FAILED",
                        resource_type="policy", resource_id=row["policy_id"],
                        correlation={"run_id": run_id}, expected={"state": "ACTIVE"},
                        observed=observed, action={"operation": "OBSERVE", "write_executed": False},
                        result={"status": "UNKNOWN"},
                    )
                    continue
                # A capacity guard is a deliberate admission decision, not a
                # broken dataplane.  Once the independently durable P4
                # classifier is verified, retrying cannot make TC safe and
                # only burns the repair budget.  Preserve the degraded
                # observed fact and an audit trail, but make no southbound
                # mutation and leave the retry counter untouched.  A missing
                # P4 entry intentionally does not take this branch: it still
                # needs the official classifier-only repair below.
                if observed.get("capacity_guard_blocked") and (observed.get("checks") or {}).get("p4_ok"):
                    summary.infrastructure.setdefault("capacity_guarded_scopes", []).append({
                        "traffic_scope_key": scope, "policy_id": row["policy_id"],
                        "reason": ((observed.get("egress_resolution") or {}).get("status")
                                   or "CAPACITY_GUARD_BLOCKED"),
                        "write_executed": False,
                    })
                    if not dry_run:
                        self._save_observed(row, drift, observed, repaired=False)
                    self.store.add_audit_event(
                        event_type="RECONCILIATION_CAPACITY_GUARD_DEFERRED",
                        resource_type="policy", resource_id=row["policy_id"],
                        correlation={"run_id": run_id}, expected={"tc_write": "ADMISSION_REQUIRED"},
                        observed=observed, action={"operation": "OBSERVE_ONLY", "write_executed": False},
                        result={"status": "CAPACITY_GUARD_BLOCKED"},
                    )
                    continue
                if not dry_run:
                    self._save_observed(row, drift, observed)
                if auto_repair and not dry_run and drift in {"MISSING", "DRIFTED"}:
                    try:
                        if binding is None:
                            binding = self._restore_binding(row)
                            bindings[scope] = binding
                            observed["resource_binding_recreated"] = binding.model_dump(mode="json")
                            self._save_desired(row, binding)
                        repair = await self._repair(row, binding, observed)
                        summary.repaired.append(row["policy_id"])
                        self._save_observed(row, drift, {**observed, "repair": repair}, repaired=True)
                        self.store.add_audit_event(
                            event_type="RECONCILIATION_REPAIR", resource_type="policy",
                            resource_id=row["policy_id"], correlation={"run_id": run_id},
                            expected={"state": "ACTIVE"}, observed=observed,
                            action=repair, result={"status": "ok"},
                        )
                    except Exception as exc:
                        summary.repair_failed.append({"policy_id": row["policy_id"], "error": str(exc)})
                        # A deferred cycle did not touch the data plane and
                        # must not refresh last_repair_at, otherwise the
                        # cooldown can never expire.
                        self._save_observed(
                            row, drift, {**observed, "repair_error": str(exc)},
                            repaired=not isinstance(exc, ReconciliationRetryDeferred),
                        )

            # Runtime-only TC scopes are stale resources and are cleaned by scope.
            try:
                if dry_run or scope_filter is not None:
                    raise ReconciliationRetryDeferred("SCOPED_OR_DRY_RUN_NO_CLEANUP")
                probe = await self._probe()
                runtime_index = await probe.get("/policy/qos")
                extra_scopes = set((runtime_index or {}).get("scopes") or []) - set(bindings)
                for scope in sorted(extra_scopes):
                    fencing = self.resident_fencing_state()
                    if fencing["status"] == "DEFERRED_BY_RECOVERY" and not self._current_recovery_owns_lease(fencing):
                        raise ReconciliationRetryDeferred("DEFERRED_BY_RECOVERY")
                    authorization = None
                    lease = None
                    context = None
                    try:
                        if os.getenv("IDN_NETWORK_WRITE_FENCING_REQUIRED", "false").lower() == "true":
                            if not current_write_context():
                                lease = NetworkWriteFencing(self.store).acquire(
                                    owner_type="RESIDENT_RECONCILER", owner_id="network-reconciler",
                                    recovery_id=f"REC-CLEANUP-{scope}", ttl_seconds=120,
                                )
                                context = write_context(
                                    owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                                    fencing_token=int(lease["fencing_token"]),
                                )
                                context.__enter__()
                            authorization = require_network_write(operation="RECONCILER_QOS_CLEANUP", store=self.store)
                            if not authorization["authorized"]:
                                raise ReconciliationRetryDeferred(authorization["reason"])
                        await probe.post("/policy/clear", {"scope_key": scope})
                        if authorization is not None:
                            NetworkWriteFencing(self.store).record_execution(authorization, write_executed=True)
                    finally:
                        if context is not None:
                            context.__exit__(None, None, None)
                        if lease is not None:
                            NetworkWriteFencing(self.store).release(lease["lease_id"])
                    summary.extra += 1
                    self.store.add_audit_event(
                        event_type="RECONCILIATION_CLEANUP", resource_type="qos_scope",
                        resource_id=scope, correlation={"run_id": run_id},
                        observed={"state": "EXTRA"}, action={"operation": "CLEAR"},
                        result={"status": "ok"},
                    )
            except Exception as exc:
                summary.infrastructure["runtime_index_error"] = str(exc)

            try:
                summary.workload_result = await self.workloads.reconcile()
            except Exception as exc:
                summary.workload_result = {"status": "error", "error": str(exc)}
            summary.status = "COMPLETED" if not summary.repair_failed else "PARTIAL"
            summary.finished_at = _now()
            if not dry_run:
                with self.store._lock, self.store._connect() as connection:
                    connection.execute(
                        "INSERT INTO reconciliation_runs VALUES (?, ?, ?, ?, ?)",
                        (run_id, summary.status, summary.model_dump_json(), started, summary.finished_at),
                    )
            return summary

    async def explain_scope(self, scope: str, *, recovery_id: str | None = None) -> dict[str, Any]:
        """Read a fresh protected-scope snapshot and return structured diffs.

        No desired/observed projection, audit record, actuator, P4 write or TC
        command is issued.  This is the post-failure preflight surface.
        """
        row = self._active_policies().get(scope)
        binding = {item.traffic_scope_key: item for item in self.allocator.list_active()}.get(scope)
        if row is None or binding is None:
            return {"status": "MISSING_DESIRED_OR_BINDING", "scope": scope, "write_required": False}
        reconcile_id = f"REX-{uuid.uuid4().hex[:12]}"
        drift, observed = await self._observe_policy(row, binding, reconcile_id=reconcile_id, recovery_id=recovery_id)
        diffs = (observed.get("semantic_diffs") or {})
        return {"status": "NO_CHANGE" if drift == "IN_SYNC" else "CHANGED", "reconcile_id": reconcile_id,
                "recovery_id": recovery_id, "scope": scope, "drift": drift, "p4": diffs.get("p4"),
                "egress": diffs.get("egress"), "ingress": {"status": "READ_ONLY_SEPARATE"},
                "write_required": any(bool((item or {}).get("write_required")) for item in diffs.values())}

    def status(self) -> dict[str, Any]:
        with self.store._lock, self.store._connect() as connection:
            desired = [dict(row) for row in connection.execute(
                "SELECT * FROM desired_policy_states ORDER BY policy_id"
            ).fetchall()]
            observed = [dict(row) for row in connection.execute(
                "SELECT * FROM observed_policy_states ORDER BY policy_id"
            ).fetchall()]
            last = connection.execute(
                "SELECT * FROM reconciliation_runs ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        for item in desired:
            item["expected"] = json.loads(item.pop("expected_json"))
        for item in observed:
            item["observed"] = json.loads(item.pop("observed_json"))
        return {"desired": desired, "observed": observed,
                "last_run": json.loads(last["summary_json"]) if last else None,
                "running": self._lock.locked()}

    async def run_loop(self, shutdown: asyncio.Event) -> None:
        interval = max(float(os.getenv("IDN_NETWORK_RECONCILE_SECONDS", "15")), 2.0)
        while not shutdown.is_set():
            try:
                await self.run_once(auto_repair=True)
            except Exception:
                logger.exception("Network reconciliation cycle failed")
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


_reconciler: NetworkReconciler | None = None


def get_network_reconciler() -> NetworkReconciler:
    global _reconciler
    current = get_store()
    if _reconciler is None or _reconciler.store is not current:
        _reconciler = NetworkReconciler(store=current)
    return _reconciler

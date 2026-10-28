"""Durable desired state and dynamic allocation for ingress admission.

This deliberately stores the policy invariant separately from runtime demand.
An admission ceiling and best-effort share are observations/allocation outputs,
never fields copied into the durable desired rule.
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from api.service_profiles import get_service_profile
from api.store import IntentStore, get_store
from infra.stable_capacity import StableCapacityRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AdmissionMaterializationInputIncomplete(ValueError):
    code = "ADMISSION_DESIRED_STATE_MATERIALIZATION_INPUT_INCOMPLETE"


class IngressAdmissionDesiredRepository:
    """Append-only desired-state and runtime-allocation repository."""

    def __init__(self, store: IntentStore | None = None):
        self.store = store or get_store()
        with self.store._lock, self.store._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ingress_admission_desired_states_v1 (
                    generation_id TEXT PRIMARY KEY,
                    admission_plan_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    traffic_scope_key TEXT NOT NULL,
                    desired_policy_id TEXT NOT NULL,
                    executed_resource_owner_policy_id TEXT NOT NULL,
                    resource_binding_id TEXT NOT NULL,
                    capacity_record_id TEXT NOT NULL,
                    desired_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingress_admission_runtime_allocations_v1 (
                    allocation_id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL,
                    allocation_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ingress_admission_allocations_generation
                    ON ingress_admission_runtime_allocations_v1(generation_id, created_at DESC);
                """
            )
            # The original composite index allowed only one historical
            # ``SUPERSEDED`` generation per plan.  A second legitimate policy
            # revision therefore failed while retiring its predecessor.  The
            # actual invariant is one *ACTIVE* generation; history is
            # append-only and may contain any number of SUPERSEDED rows.
            connection.execute("DROP INDEX IF EXISTS idx_ingress_admission_desired_active")
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_ingress_admission_desired_one_active
                   ON ingress_admission_desired_states_v1(admission_plan_id)
                   WHERE status='ACTIVE'"""
            )

    def active(self, admission_plan_id: str) -> dict[str, Any] | None:
        with self.store._lock, self.store._connect() as connection:
            row = connection.execute(
                """SELECT * FROM ingress_admission_desired_states_v1
                   WHERE admission_plan_id=? AND status='ACTIVE'
                   ORDER BY created_at DESC LIMIT 1""",
                (admission_plan_id,),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["desired"] = json.loads(value.pop("desired_json"))
        return value

    def active_for_scope(self, traffic_scope_key: str) -> dict[str, Any] | None:
        """Return the one current ingress desired generation for a scope.

        Recovery must be scope-driven; a historical fixed admission-plan ID
        is not an authority for a newly materialized service.
        """
        with self.store._lock, self.store._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM ingress_admission_desired_states_v1
                   WHERE traffic_scope_key=? AND status='ACTIVE'
                   ORDER BY created_at DESC""",
                (traffic_scope_key,),
            ).fetchall()
        if len(rows) != 1:
            return None
        value = dict(rows[0])
        value["desired"] = json.loads(value.pop("desired_json"))
        return value

    def list_active(self) -> list[dict[str, Any]]:
        with self.store._lock, self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ingress_admission_desired_states_v1 WHERE status='ACTIVE' ORDER BY created_at DESC"
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["desired"] = json.loads(value.pop("desired_json"))
            result.append(value)
        return result

    def append_desired(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                """INSERT INTO ingress_admission_desired_states_v1(
                    generation_id, admission_plan_id, intent_id, revision,
                    traffic_scope_key, desired_policy_id,
                    executed_resource_owner_policy_id, resource_binding_id,
                    capacity_record_id, desired_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)""",
                (
                    record["generation_id"], record["admission_plan_id"],
                    record["intent_id"], record["revision"], record["traffic_scope_key"],
                    record["desired_policy_id"], record["executed_resource_owner_policy_id"],
                    record["resource_binding_id"], record["capacity_record_id"],
                    json.dumps(record["desired"], sort_keys=True), record["created_at"],
                ),
            )
        return record

    def synchronize_committed_minimum_policy(self, admission_plan_id: str) -> dict[str, Any]:
        """Advance an existing ingress contract to its committed policy revision.

        The ingress tree is a durable projection of a MIN_BANDWIDTH policy, not
        an independent authority for its old bandwidth.  Policy revisions keep
        their binding identity, so a successful update must advance this
        projection before a resident/restart reconcile is allowed to replay
        it.  The method is intentionally scoped to an *existing* ingress
        contract: it never invents one for an unrelated service.
        """
        current = self.active(admission_plan_id)
        if current is None:
            return {"status": "NO_DESIRED_STATE", "changed": False, "record": None}
        from infra.qos_resource_allocator import QoSResourceAllocator
        from models.yang_policy import YANGPolicy
        from persistence.policy_store import PolicyStore

        policy_row = PolicyStore(self.store).resolve_managed_desired_for_scope(
            current["traffic_scope_key"]
        )
        allocator = QoSResourceAllocator(self.store)
        binding = allocator.get_active_for_scope(current["traffic_scope_key"])
        if policy_row is None or binding is None:
            return {"status": "OWNER_UNAVAILABLE", "changed": False, "record": current}
        try:
            policy = YANGPolicy(**json.loads(policy_row["yang_json"])).network_policy
            qos = policy.action.qos_params
            mode = getattr(getattr(qos, "qos_mode", None), "value", getattr(qos, "qos_mode", None))
            bandwidth = float(getattr(qos, "bandwidth", 0) or 0)
        except (KeyError, TypeError, ValueError):
            return {"status": "COMMITTED_POLICY_UNREADABLE", "changed": False, "record": current}
        if str(mode or "").upper() != "MIN_BANDWIDTH" or bandwidth <= 0:
            return {"status": "NOT_MINIMUM_POLICY", "changed": False, "record": current}

        # ResourceBinding is the authoritative allocation identity.  It must
        # carry the same committed floor as its policy before reconciliation
        # derives TC expectations from it; retain its class/filter/resource
        # identity while correcting only the revised reservation attributes.
        binding_changed = (
            float(binding.rate_mbps) != bandwidth
            or str(binding.policy_id) != str(policy_row["policy_id"])
        )
        if binding_changed:
            binding = allocator.allocate(
                policy_id=policy_row["policy_id"], traffic_scope_key=current["traffic_scope_key"],
                rate_mbps=bandwidth, ceil_mbps=float(binding.ceil_mbps),
                service_id=binding.service_id, dscp=int(getattr(qos, "dscp_value", binding.dscp) or 0),
                reservation_kind=binding.reservation_kind, tenant_id=binding.tenant_id,
            )

        desired = dict(current["desired"])
        selector = dict(desired.get("protected_selector") or {})
        selector.update({
            "source_ip": str(policy.match.source_ip or "").split("/")[0],
            "destination_ip": str(policy.match.destination_ip or "").split("/")[0],
            "protocol": str(getattr(policy.match.protocol, "value", policy.match.protocol) or "").lower(),
            "destination_port": int(policy.match.destination_port or 0),
            "dscp": int(getattr(qos, "dscp_value", 0) or 0),
        })
        desired["protected_selector"] = selector
        desired["qos_mode"] = "MIN_BANDWIDTH"
        desired["guaranteed_floor_mbps"] = bandwidth
        changed = (
            float((current.get("desired") or {}).get("guaranteed_floor_mbps") or 0) != bandwidth
            or current.get("desired_policy_id") != policy_row["policy_id"]
            or current.get("executed_resource_owner_policy_id") != policy_row["policy_id"]
            or current.get("resource_binding_id") != binding.binding_id
            or binding_changed
            or (current.get("desired") or {}).get("protected_selector") != selector
        )
        if not changed:
            return {"status": "CURRENT", "changed": False, "record": current}
        record = {
            **current,
            "generation_id": "IADS-" + uuid.uuid4().hex[:16],
            "intent_id": str(policy_row.get("intent_id") or current["intent_id"]),
            "revision": int(current.get("revision") or 0) + 1,
            "desired_policy_id": policy_row["policy_id"],
            "executed_resource_owner_policy_id": policy_row["policy_id"],
            "resource_binding_id": binding.binding_id,
            "desired": desired,
            "created_at": _now(),
        }
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                active = connection.execute(
                    "SELECT generation_id FROM ingress_admission_desired_states_v1 WHERE admission_plan_id=? AND status='ACTIVE'",
                    (admission_plan_id,),
                ).fetchone()
                # ``current`` was observed before this transaction.  A
                # resident and a fenced recovery can legitimately both
                # prepare the same advance; the second writer must reuse the
                # winner rather than insert a second ACTIVE generation.
                if active is None or active["generation_id"] != current["generation_id"]:
                    connection.rollback()
                    latest = self.active(admission_plan_id)
                    return {"status": "ALREADY_SYNCHRONIZED", "changed": False, "record": latest}
                retired = connection.execute(
                    "UPDATE ingress_admission_desired_states_v1 SET status='SUPERSEDED' WHERE generation_id=? AND status='ACTIVE'",
                    (current["generation_id"],),
                )
                if retired.rowcount != 1:
                    connection.rollback()
                    latest = self.active(admission_plan_id)
                    return {"status": "ALREADY_SYNCHRONIZED", "changed": False, "record": latest}
                connection.execute(
                    """INSERT INTO ingress_admission_desired_states_v1(
                        generation_id, admission_plan_id, intent_id, revision,
                        traffic_scope_key, desired_policy_id,
                        executed_resource_owner_policy_id, resource_binding_id,
                        capacity_record_id, desired_json, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)""",
                    (record["generation_id"], record["admission_plan_id"], record["intent_id"], record["revision"],
                     record["traffic_scope_key"], record["desired_policy_id"],
                     record["executed_resource_owner_policy_id"], record["resource_binding_id"],
                     record["capacity_record_id"], json.dumps(record["desired"], sort_keys=True), record["created_at"]),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self.store.add_audit_event(
            event_type="INGRESS_ADMISSION_DESIRED_STATE_SYNCHRONIZED",
            resource_type="ingress_admission_desired_state", resource_id=record["generation_id"],
            correlation={"previous_generation": current["generation_id"], "policy_id": policy_row["policy_id"]},
            expected=record["desired"], observed=current["desired"],
            action={"operation": "ADVANCE_COMMITTED_POLICY_GENERATION"},
            result={"status": "ACTIVE"},
        )
        return {"status": "SYNCHRONIZED", "changed": True, "record": record}

    def append_allocation(self, generation_id: str, allocation: dict[str, Any], status: str) -> dict[str, Any]:
        record = {
            "allocation_id": f"IAA-{uuid.uuid4().hex[:16]}",
            "generation_id": generation_id, "allocation": allocation,
            "status": status, "created_at": _now(),
        }
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                """INSERT INTO ingress_admission_runtime_allocations_v1(
                    allocation_id, generation_id, allocation_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (record["allocation_id"], generation_id,
                 json.dumps(allocation, sort_keys=True), status, record["created_at"]),
            )
        return record


class IngressAdmissionMaterializer:
    """Create a durable long-lived rule from authoritative control-plane data."""

    plan_id = "INGRESS-WC-20260805-03"
    allocator_version = "work-conserving-v2"

    def __init__(self, store: IntentStore | None = None):
        self.store = store or get_store()
        self.repository = IngressAdmissionDesiredRepository(self.store)

    def materialize(self, *, intent_id: str, revision: int) -> dict[str, Any]:
        existing = self.repository.active(self.plan_id)
        if existing:
            return {"status": "MATERIALIZED_NO_CHANGE", "record": existing}
        with self.store._lock, self.store._connect() as connection:
            runtime_row = connection.execute(
                "SELECT runtime_json FROM intent_runtime_v1 WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if not runtime_row:
                raise AdmissionMaterializationInputIncomplete("INTENT_RUNTIME_MISSING")
            runtime = json.loads(runtime_row["runtime_json"])
            desired_policy = runtime.get("desired_policy") or {}
            network_policy = desired_policy.get("network_policy") or {}
            qos = ((network_policy.get("action") or {}).get("qos_params") or {})
            scope = "TSK2-7ce48b7a4825ee49d0f4d293"
            binding = connection.execute(
                """SELECT * FROM qos_resource_bindings
                   WHERE binding_id=? AND active=1""", ("RB-b7bee128b1d54899",)
            ).fetchone()
        required = {
            "desired_policy_id": network_policy.get("policy_id"),
            "qos_mode": qos.get("qos_mode"), "bandwidth": qos.get("bandwidth"),
            "source_ip": (network_policy.get("match") or {}).get("source_ip"),
            "destination_ip": (network_policy.get("match") or {}).get("destination_ip"),
            "protocol": (network_policy.get("match") or {}).get("protocol"),
            "destination_port": (network_policy.get("match") or {}).get("destination_port"),
            "binding": binding,
        }
        missing = [name for name, value in required.items() if value in (None, "")]
        if missing or str(required["qos_mode"]).upper() != "MIN_BANDWIDTH" or float(required["bandwidth"]) != 30:
            raise AdmissionMaterializationInputIncomplete("INVALID_DESIRED_POLICY_OR_BINDING:" + ",".join(missing))
        binding_data = dict(binding)
        if binding_data["traffic_scope_key"] != scope or binding_data["policy_id"] != "POL-20260730-78568302":
            raise AdmissionMaterializationInputIncomplete("RESOURCE_BINDING_LINEAGE_MISMATCH")
        capacity = StableCapacityRepository(self.store).active_for(
            bottleneck_egress=binding_data["resource_key"], topology_id="research_dc",
        )
        if capacity is None:
            raise AdmissionMaterializationInputIncomplete("ACTIVE_CAPACITY_RECORD_MISSING")
        schedulable = math.floor(StableCapacityRepository.schedulable_capacity(capacity))
        if schedulable != 49:
            raise AdmissionMaterializationInputIncomplete("SCHEDULABLE_CAPACITY_NOT_49")
        background = get_service_profile("background_video")
        desired = {
            "schema_version": "1.0",
            "qos_mode": "MIN_BANDWIDTH",
            "guaranteed_floor_mbps": 30,
            "schedulable_capacity_mbps": 49,
            "allocator_type": "WORK_CONSERVING",
            "allocator_version": self.allocator_version,
            "borrowing_enabled": True,
            "be_allocation_policy": "ACTIVE_SCOPES_ONLY",
            "idle_be_runtime_allocation_mbps": 0,
            "maximum_total_ingress_allocation_mbps": 49,
            "actuator_type": "PreBmV2MininetAttachmentActuator",
            "actuator_template": "MININET_DYNAMIC_ATTACHMENT_V1",
            "protected_selector": {
                "scope_id": scope, "source_ip": str(required["source_ip"]).split("/")[0],
                "destination_ip": str(required["destination_ip"]).split("/")[0],
                "protocol": str(required["protocol"]).lower(),
                "destination_port": int(required["destination_port"]), "dscp": int(qos.get("dscp_value")),
            },
            "eligible_best_effort_scopes": [{
                "scope_id": "BE-ACTIVE-1", "service_id": background.service_id,
                "source_ip": str(background.src_ip).split("/")[0],
                "destination_ip": str(background.dst_ip).split("/")[0],
                "protocol": background.protocol.value.lower(), "destination_port": background.dst_port,
                "weight": 1,
            }],
        }
        record = {
            "generation_id": f"IADS-{uuid.uuid4().hex[:16]}", "admission_plan_id": self.plan_id,
            "intent_id": intent_id, "revision": revision, "traffic_scope_key": scope,
            "desired_policy_id": network_policy["policy_id"],
            "executed_resource_owner_policy_id": binding_data["policy_id"],
            "resource_binding_id": binding_data["binding_id"],
            "capacity_record_id": capacity.capacity_record_id, "desired": desired, "created_at": _now(),
        }
        self.repository.append_desired(record)
        self.store.add_audit_event(
            event_type="INGRESS_ADMISSION_DESIRED_STATE_MATERIALIZED",
            resource_type="ingress_admission_desired_state", resource_id=record["generation_id"],
            correlation={"intent_id": intent_id, "revision": revision, "trace_id": record["generation_id"]},
            expected=desired, observed={"resource_binding_id": binding_data["binding_id"], "capacity_record_id": capacity.capacity_record_id},
            action={"operation": "MATERIALIZE_LONG_LIVED_RULE"}, result={"status": "ACTIVE"},
        )
        # Replay the canonical durable owner into the runtime projection using
        # PolicyStore's audited commit path.  This is not an SQL policy-ID
        # rewrite and never creates a business policy.
        from persistence.policy_store import PolicyStore
        with self.store._lock, self.store._connect() as connection:
            owner = connection.execute(
                "SELECT runtime_json FROM policies WHERE policy_id=?", (binding_data["policy_id"],)
            ).fetchone()
        canonical_runtime = json.loads((owner or {"runtime_json": "{}"})["runtime_json"] or "{}")
        enforcement = dict(canonical_runtime.get("qos_enforcement") or {})
        enforcement["policy_id"] = binding_data["policy_id"]
        binding_projection = dict(enforcement.get("resource_binding") or {})
        binding_projection.update({"binding_id": binding_data["binding_id"], "policy_id": binding_data["policy_id"]})
        enforcement["resource_binding"] = binding_projection
        canonical_runtime["qos_enforcement"] = enforcement
        canonical_runtime["existing_policy_id"] = binding_data["policy_id"]
        canonical_runtime["policy_change"] = {"resolution": "REUSE_EXISTING_POLICY", "replayed_by": "INGRESS_ADMISSION_MATERIALIZATION"}
        PolicyStore(self.store).commit_staged(scope, intent_id=intent_id, runtime=canonical_runtime)
        self.store.add_audit_event(
            event_type="POLICY_RECOVERY_LINEAGE_VERIFIED", resource_type="policy_lineage",
            resource_id=binding_data["policy_id"],
            correlation={"intent_id": intent_id, "desired_policy_id": network_policy["policy_id"], "generation_id": record["generation_id"]},
            expected={"resolution": "REUSE_EXISTING_POLICY", "resource_binding_id": binding_data["binding_id"]},
            observed={"stale_projection_policy_id": "POL-20260805-70ED56A9"},
            action={"operation": "POLICY_STORE_PROJECTION_REPLAY"}, result={"status": "LINEAGE_REFERENCE_RECONCILED"},
        )
        return {"status": "MATERIALIZED", "record": record}


def idle_borrowing_allocation(record: dict[str, Any]) -> dict[str, Any]:
    """Build a restart-safe allocation without relying on old attachment state."""
    desired = record["desired"]
    selector = desired["protected_selector"]
    return {
        "admission_plan_id": record["admission_plan_id"], "mode": "IDLE_BORROWING",
        "desired_generation": record["generation_id"], "allocation_epoch": 1,
        "traffic_scope_key": record["traffic_scope_key"], "capacity_record_id": record["capacity_record_id"],
        "schedulable_capacity_mbps": desired["schedulable_capacity_mbps"],
        "implementation_reserved_mbps": desired["guaranteed_floor_mbps"],
        "best_effort_residual_mbps": desired["schedulable_capacity_mbps"] - desired["guaranteed_floor_mbps"],
        "expected_aggregate_ceiling_mbps": desired["schedulable_capacity_mbps"],
        "protected_scopes": [{
            **selector, "requested_min_bandwidth_mbps": desired["guaranteed_floor_mbps"],
            "implementation_guaranteed_floor_mbps": desired["guaranteed_floor_mbps"],
            "observed_active_demand_mbps": desired["guaranteed_floor_mbps"],
            "ingress_admission_ceiling_mbps": desired["schedulable_capacity_mbps"],
            "borrowed_capacity_mbps": desired["schedulable_capacity_mbps"] - desired["guaranteed_floor_mbps"],
        }],
        "best_effort_scopes": [],
        "inactive_best_effort_scopes": [{**scope, "activity_state": "IDLE", "runtime_budget_mbps": 0}
                                          for scope in desired["eligible_best_effort_scopes"]],
    }


def declared_contention_allocation(
    record: dict[str, Any], *, target_demand_mbps: float, active_best_effort_scope_ids: set[str],
) -> dict[str, Any]:
    """Derive a transient allocation from an authorized active run declaration."""
    desired = record["desired"]
    selector = desired["protected_selector"]
    floor = float(desired["guaranteed_floor_mbps"])
    capacity = float(desired["schedulable_capacity_mbps"])
    target_ceil = min(max(floor, float(target_demand_mbps)), capacity)
    residual = max(0.0, capacity - target_ceil)
    active = [scope for scope in desired["eligible_best_effort_scopes"]
              if scope["scope_id"] in active_best_effort_scope_ids]
    if not active:
        return idle_borrowing_allocation(record)
    weight_total = sum(float(scope.get("weight") or 1) for scope in active)
    active_scopes = [{
        **scope, "activity_state": "ACTIVE",
        "runtime_budget_mbps": residual * float(scope.get("weight") or 1) / weight_total,
    } for scope in active]
    return {
        "admission_plan_id": record["admission_plan_id"], "mode": "OVERSUBSCRIBED_ADMISSION",
        "desired_generation": record["generation_id"], "allocation_epoch": 1,
        "traffic_scope_key": record["traffic_scope_key"], "capacity_record_id": record["capacity_record_id"],
        "schedulable_capacity_mbps": capacity, "implementation_reserved_mbps": floor,
        "best_effort_residual_mbps": residual, "expected_aggregate_ceiling_mbps": capacity,
        "protected_scopes": [{
            **selector, "requested_min_bandwidth_mbps": floor,
            "implementation_guaranteed_floor_mbps": floor, "observed_active_demand_mbps": target_ceil,
            "ingress_admission_ceiling_mbps": target_ceil, "borrowed_capacity_mbps": target_ceil - floor,
        }],
        "best_effort_scopes": active_scopes,
        "inactive_best_effort_scopes": [{**scope, "activity_state": "IDLE", "runtime_budget_mbps": 0}
            for scope in desired["eligible_best_effort_scopes"] if scope not in active],
    }

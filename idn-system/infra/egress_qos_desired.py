"""Authoritative egress QoS desired-state resolution.

The resolver is deliberately separate from persistence and actuation.  A
ResourceBinding is an allocation identity, not a QoS contract: its mutable
runtime JSON and any prior TC readback are therefore never inputs here.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from datetime import datetime, timezone
from ipaddress import ip_network
from typing import Any

from pydantic import BaseModel, Field

from api.store import IntentStore, get_store
from infra.ingress_admission_state import IngressAdmissionDesiredRepository
from infra.qos_resource_allocator import QoSResourceAllocator
from infra.stable_capacity import StableCapacityRepository
from models.resources import ResourceBinding, StableDataplaneCapacityRecord
from models.yang_policy import YANGPolicy
from persistence.policy_store import PolicyStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _prefix(value: Any) -> str | None:
    if value in (None, ""):
        return None
    value = str(value)
    return str(ip_network(value if "/" in value else f"{value}/32", strict=False))


def _protocol(value: Any) -> int | None:
    if value in (None, ""):
        return None
    names = {"icmp": 1, "tcp": 6, "udp": 17}
    text = str(getattr(value, "value", value)).lower()
    return names.get(text, int(text, 0) if text not in names else None)


def _capacity_is_current(record: StableDataplaneCapacityRecord) -> bool:
    try:
        return datetime.fromisoformat(record.valid_until.replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except (AttributeError, ValueError):
        return False


class EgressQosDesiredResolution(BaseModel):
    """The one canonical desired contract delivered to egress QoS.

    ``resolution_errors`` is explicit rather than exceptional so callers can
    make a no-write decision and surface the same reason in recovery evidence.
    """

    status: str
    traffic_scope_key: str
    intent_id: str | None = None
    policy_id: str | None = None
    binding_id: str | None = None
    ingress_desired_generation_id: str | None = None
    capacity_record_id: str | None = None
    service_profile_id: str | None = None
    selector: dict[str, Any] = Field(default_factory=dict)
    protocol: int | None = None
    destination_port: int | None = None
    dscp: int | None = None
    egress_device: str | None = None
    egress_interface: str | None = None
    root_rate_mbps: float | None = None
    root_ceil_mbps: float | None = None
    target_rate_mbps: float | None = None
    target_ceil_mbps: float | None = None
    default_rate_mbps: float | None = None
    default_ceil_mbps: float | None = None
    source_provenance: dict[str, Any] = Field(default_factory=dict)
    ignored_bindings: list[dict[str, Any]] = Field(default_factory=list)
    resolution_errors: list[str] = Field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status == "RESOLVED" and not self.resolution_errors

    def desired_payload(self, *, binding: ResourceBinding) -> dict[str, Any]:
        if not self.accepted:
            raise ValueError("EGRESS_QOS_DESIRED_NOT_RESOLVED")
        return {
            "root": {"rate_mbps": self.root_rate_mbps, "ceil_mbps": self.root_ceil_mbps, "handle": "1:"},
            "target": {"rate_mbps": self.target_rate_mbps, "ceil_mbps": self.target_ceil_mbps, "classid": binding.class_id},
            "default": {"rate_mbps": self.default_rate_mbps, "ceil_mbps": self.default_ceil_mbps, "classid": "1:fffe"},
            "selector": self.selector,
            "capacity": dict(self.source_provenance.get("capacity") or {}),
            "source_provenance": self.source_provenance,
        }


def _failure(scope: str, errors: list[str], *, ignored: list[dict[str, Any]] | None = None,
             provenance: dict[str, Any] | None = None, **values: Any) -> EgressQosDesiredResolution:
    return EgressQosDesiredResolution(
        status=errors[0], traffic_scope_key=scope, resolution_errors=errors,
        ignored_bindings=ignored or [], source_provenance=provenance or {}, **values,
    )


def resolve_egress_qos_desired(
    *, traffic_scope_key: str, ingress_desired: dict[str, Any] | None,
    bindings: list[ResourceBinding], current_policy: dict[str, Any] | None,
    capacity_records: list[StableDataplaneCapacityRecord], topology_id: str | None,
    service_profile: Any | None = None, default_min_rate_mbps: float | None = None,
    aggregate_guaranteed_mbps: float | None = None,
    capacity_purpose: str = "NEW_ADMISSION",
) -> EgressQosDesiredResolution:
    """Resolve from current ingress desired state, never observed runtime state.

    This pure function is also the test seam.  Its production wrapper below
    gathers records through repositories only; neither path reads binding
    runtime JSON, old egress desired snapshots, or TC/P4 observations.
    """
    ignored = [
        {"binding_id": item.binding_id, "reason": "SUPERSEDED_OR_NON_AUTHORITATIVE_BINDING"}
        for item in bindings if not item.active
    ]
    active = [item for item in bindings if item.active]
    if not ingress_desired:
        return _failure(traffic_scope_key, ["INGRESS_DESIRED_MISSING"], ignored=ignored)
    if str(ingress_desired.get("traffic_scope_key")) != traffic_scope_key:
        return _failure(traffic_scope_key, ["INGRESS_SCOPE_MISMATCH"], ignored=ignored)
    if not active:
        return _failure(traffic_scope_key, ["ACTIVE_BINDING_MISSING"], ignored=ignored)
    if len(active) != 1:
        return _failure(traffic_scope_key, ["AMBIGUOUS_ACTIVE_BINDING"], ignored=ignored)
    binding = active[0]
    if binding.binding_id != ingress_desired.get("resource_binding_id"):
        return _failure(traffic_scope_key, ["ACTIVE_BINDING_LINEAGE_MISMATCH"], ignored=ignored,
                        binding_id=binding.binding_id)
    if not current_policy or str(current_policy.get("status")) not in {"ACTIVE", "DEGRADED", "RECONCILING"}:
        return _failure(traffic_scope_key, ["CURRENT_POLICY_MISSING"], ignored=ignored,
                        binding_id=binding.binding_id)
    if str(current_policy.get("policy_id")) != str(ingress_desired.get("executed_resource_owner_policy_id")):
        return _failure(traffic_scope_key, ["CURRENT_POLICY_OWNERSHIP_MISMATCH"], ignored=ignored,
                        binding_id=binding.binding_id, policy_id=current_policy.get("policy_id"))
    try:
        policy_model = YANGPolicy(**json.loads(current_policy["yang_json"]))
        policy = policy_model.network_policy
        from api.service_profiles import resolve_service_profile_for_policy
        profile = service_profile or resolve_service_profile_for_policy(policy_model)
    except Exception as exc:
        return _failure(traffic_scope_key, ["SERVICE_PROFILE_UNRESOLVED"], ignored=ignored,
                        binding_id=binding.binding_id, policy_id=current_policy.get("policy_id"),
                        provenance={"service_profile_error": str(exc)})
    desired = dict(ingress_desired.get("desired") or {})
    selector_input = dict(desired.get("protected_selector") or {})
    selector = {
        "source_prefix": _prefix(selector_input.get("source_ip")),
        "destination_prefix": _prefix(selector_input.get("destination_ip")),
        "ip_protocol": _protocol(selector_input.get("protocol")),
        "destination_port": selector_input.get("destination_port"),
        "dscp": selector_input.get("dscp"),
    }
    # An explicit SLA selector has precedence; the profile fills only absent
    # metadata and is independently validated against the current policy.
    selector["source_prefix"] = selector["source_prefix"] or _prefix(profile.src_ip)
    selector["destination_prefix"] = selector["destination_prefix"] or _prefix(profile.dst_ip)
    selector["ip_protocol"] = selector["ip_protocol"] or _protocol(profile.protocol)
    selector["destination_port"] = selector["destination_port"] if selector["destination_port"] is not None else profile.dst_port
    selector["dscp"] = selector["dscp"] if selector["dscp"] is not None else profile.dscp
    if any(selector[key] is None for key in ("source_prefix", "destination_prefix", "ip_protocol", "destination_port", "dscp")):
        return _failure(traffic_scope_key, ["SELECTOR_INCOMPLETE"], ignored=ignored,
                        binding_id=binding.binding_id, policy_id=current_policy.get("policy_id"))
    purpose = str(capacity_purpose or "NEW_ADMISSION").upper()
    recovery_or_reconcile = purpose in {"RECOVERY_EXISTING", "RECONCILE_EXISTING"}
    expected_capacity_id = str(ingress_desired.get("capacity_record_id") or "")
    capacity = next((item for item in capacity_records if item.capacity_record_id == expected_capacity_id), None)
    capacity_current = bool(
        capacity is not None and capacity.status == "ACTIVE"
        and _capacity_is_current(capacity)
        and (not topology_id or capacity.topology_id == topology_id)
    )
    if not capacity_current and not recovery_or_reconcile:
        return _failure(traffic_scope_key, ["STABLE_CAPACITY_EVIDENCE_MISSING"], ignored=ignored,
                        binding_id=binding.binding_id, policy_id=current_policy.get("policy_id"),
                        capacity_record_id=expected_capacity_id or None)
    if capacity is not None and binding.resource_key not in (None, "", "UNBOUND") and capacity.bottleneck_egress != binding.resource_key:
        return _failure(traffic_scope_key, ["CAPACITY_RESOURCE_SCOPE_MISMATCH"], ignored=ignored,
                        binding_id=binding.binding_id, policy_id=current_policy.get("policy_id"),
                        capacity_record_id=capacity.capacity_record_id)
    declared_capacity = desired.get("schedulable_capacity_mbps")
    declared_root = float(declared_capacity or 0)
    if declared_root <= 0:
        return _failure(traffic_scope_key, ["STABLE_CAPACITY_EVIDENCE_MISSING"], ignored=ignored,
                        binding_id=binding.binding_id, policy_id=current_policy.get("policy_id"),
                        capacity_record_id=expected_capacity_id or None)
    schedulable = float(StableCapacityRepository.schedulable_capacity(capacity)) if capacity_current else None
    if not recovery_or_reconcile and (schedulable is None or schedulable < declared_root):
        return _failure(traffic_scope_key, ["STABLE_CAPACITY_EVIDENCE_MISSING"], ignored=ignored,
                        binding_id=binding.binding_id, policy_id=current_policy.get("policy_id"),
                        capacity_record_id=expected_capacity_id or None)
    # Mininet's HTB actuator uses whole-Mbit values.  Preserve the fractional
    # calibration in provenance, but floor the actual scheduler contract so a
    # controller never claims a rate it did not configure.
    root = float(max(1, math.floor(declared_root)))
    if capacity_current:
        root = min(root, float(StableCapacityRepository.htb_schedulable_capacity(capacity)))
    qos_value = policy.action.qos_params
    qos = qos_value.model_dump(mode="json") if qos_value is not None else {}
    mode = str(qos.get("qos_mode") or desired.get("qos_mode") or "").upper()
    if mode == "MIN_BANDWIDTH":
        target_rate = float(desired.get("guaranteed_floor_mbps") or 0)
        target_ceil = root
    elif mode == "RATE_LIMIT":
        target_rate = float(qos.get("bandwidth") or 0)
        target_ceil = min(root, target_rate)
    else:
        return _failure(traffic_scope_key, ["QOS_MODE_UNRESOLVED"], ignored=ignored,
                        binding_id=binding.binding_id, policy_id=current_policy.get("policy_id"))
    if target_rate <= 0 or target_rate > root:
        return _failure(traffic_scope_key, ["QOS_RATE_EXCEEDS_SCHEDULABLE_CAPACITY"], ignored=ignored,
                        binding_id=binding.binding_id, policy_id=current_policy.get("policy_id"),
                        capacity_record_id=capacity.capacity_record_id)
    policy_default = desired.get("default_resource_policy") or {}
    default_rate = float(default_min_rate_mbps if default_min_rate_mbps is not None else policy_default.get(
        "min_rate_mbps", os.getenv("IDN_QOS_DEFAULT_MIN_RATE_MBPS", "1")))
    # The HTB default class is one physical egress resource, not one policy.
    # Its ceiling must leave room for every current MIN_BANDWIDTH reservation
    # on that resource.  A per-scope ``root - target`` calculation is wrong
    # as soon as two independently selected services share the same path.
    total_guaranteed = float(aggregate_guaranteed_mbps if aggregate_guaranteed_mbps is not None else target_rate)
    if total_guaranteed < target_rate or total_guaranteed > root:
        return _failure(traffic_scope_key, ["QOS_AGGREGATE_RESERVATION_INVALID"], ignored=ignored,
                        binding_id=binding.binding_id, policy_id=current_policy.get("policy_id"),
                        capacity_record_id=capacity.capacity_record_id)
    default_ceil = max(default_rate, root - total_guaranteed)
    capacity_data = capacity.model_dump(mode="json") if capacity is not None else {
        "capacity_record_id": expected_capacity_id or None,
    }
    capacity_data.update({
        "runtime_capacity_status": "CURRENT" if capacity_current else "DEGRADED",
        "runtime_schedulable_mbps": schedulable,
        "durable_scheduler_contract_mbps": declared_root,
        "evaluation_purpose": purpose,
        "schedulable_capacity_mbps": schedulable,
    })
    return EgressQosDesiredResolution(
        status="RESOLVED", traffic_scope_key=traffic_scope_key, intent_id=ingress_desired.get("intent_id"),
        policy_id=current_policy.get("policy_id"), binding_id=binding.binding_id,
        ingress_desired_generation_id=ingress_desired.get("generation_id"), capacity_record_id=expected_capacity_id or None,
        service_profile_id=profile.service_id, selector=selector, protocol=int(selector["ip_protocol"]),
        destination_port=int(selector["destination_port"]), dscp=int(selector["dscp"]),
        egress_device=binding.device_id, egress_interface=binding.interface_id,
        root_rate_mbps=root, root_ceil_mbps=root, target_rate_mbps=target_rate,
        target_ceil_mbps=target_ceil, default_rate_mbps=default_rate, default_ceil_mbps=default_ceil,
        source_provenance={
            "binding": {"binding_id": binding.binding_id, "active": True, "resource_key": binding.resource_key},
            "ingress_desired": {"generation_id": ingress_desired.get("generation_id"), "plan_id": ingress_desired.get("admission_plan_id")},
            "policy": {"policy_id": current_policy.get("policy_id"), "status": current_policy.get("status")},
            "capacity": capacity_data,
            "aggregate_guaranteed_mbps": total_guaranteed,
            "service_profile": {"service_id": profile.service_id},
            "default_resource_policy": {"min_rate_mbps": default_rate},
        },
        ignored_bindings=ignored,
    )


def resolve_egress_qos_desired_for_scope(
    traffic_scope_key: str, *, store: IntentStore | None = None,
    capacity_purpose: str = "NEW_ADMISSION",
) -> EgressQosDesiredResolution:
    """Production repository adapter for :func:`resolve_egress_qos_desired`."""
    store = store or get_store()
    ingress = IngressAdmissionDesiredRepository(store).active_for_scope(traffic_scope_key)
    bindings = QoSResourceAllocator(store).list_for_scope(traffic_scope_key)
    # Keep restart recovery on the same durable owner selection as the
    # resident reconciler.  ``get_by_scope`` is intentionally a raw latest
    # record helper for transaction management and may see a staged document.
    policy = PolicyStore(store).resolve_managed_desired_for_scope(traffic_scope_key)
    topology_id: str | None = None
    try:
        from infra.resource_registry import get_resource_registry
        topology_id = get_resource_registry().snapshot().topology_name
    except Exception:
        pass
    current_binding = next((item for item in bindings if item.active), None)
    aggregate_guaranteed = _aggregate_current_minimum_reservations(
        store=store, resource_key=(current_binding.resource_key if current_binding else None),
    )
    return resolve_egress_qos_desired(
        traffic_scope_key=traffic_scope_key, ingress_desired=ingress, bindings=bindings,
        current_policy=policy, capacity_records=StableCapacityRepository(store).list(), topology_id=topology_id,
        aggregate_guaranteed_mbps=aggregate_guaranteed, capacity_purpose=capacity_purpose,
    )


def _aggregate_current_minimum_reservations(*, store: IntentStore, resource_key: str | None) -> float:
    """Sum durable MIN_BANDWIDTH owners for one egress resource only.

    This uses the same current-policy and active-binding ownership resolver as
    recovery.  It deliberately excludes TC observations and historical rows.
    """
    if not resource_key or resource_key == "UNBOUND":
        return 0.0
    allocator = QoSResourceAllocator(store)
    total = 0.0
    for scope, row in PolicyStore(store).resolve_managed_desired_policies().items():
        binding = allocator.get_active_for_scope(scope)
        if binding is None or binding.resource_key != resource_key:
            continue
        try:
            qos = YANGPolicy(**json.loads(row["yang_json"])).network_policy.action.qos_params
            mode = getattr(getattr(qos, "qos_mode", None), "value", getattr(qos, "qos_mode", None))
            if qos is not None and str(mode or "").upper() == "MIN_BANDWIDTH":
                # A preference-only SET_PRIORITY policy has no numeric
                # override.  Its active binding is the durable committed
                # reservation, so treating the absent field as zero makes
                # aggregate admission incorrectly reject a valid baseline.
                total += float(qos.bandwidth if qos.bandwidth is not None else binding.rate_mbps)
        except (KeyError, TypeError, ValueError):
            continue
    return total


class EgressQosDesiredRepository:
    """Persistence for an already-resolved contract; never a resolver."""

    def __init__(self, store: IntentStore | None = None):
        self.store = store or get_store()
        with self.store._lock, self.store._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS egress_qos_desired_states_v1 (
                    desired_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL,
                    desired_policy_id TEXT NOT NULL, owner_policy_id TEXT NOT NULL,
                    resource_binding_id TEXT NOT NULL, traffic_scope_key TEXT NOT NULL,
                    capacity_record_id TEXT NOT NULL, generation_id TEXT NOT NULL,
                    desired_json TEXT NOT NULL, checksum TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_egress_qos_desired_active_v1
                    ON egress_qos_desired_states_v1(traffic_scope_key, status);
            """)

    def active(self, traffic_scope_key: str) -> dict[str, Any] | None:
        with self.store._lock, self.store._connect() as connection:
            row = connection.execute("""SELECT * FROM egress_qos_desired_states_v1
                WHERE traffic_scope_key=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1""", (traffic_scope_key,)).fetchone()
        if not row:
            return None
        value = dict(row); value["desired"] = json.loads(value.pop("desired_json")); return value

    def materialize_authorized(self, resolution: EgressQosDesiredResolution, *, binding: ResourceBinding) -> dict[str, Any]:
        """Persist a fully resolved authorized contract without deriving it."""
        if not resolution.accepted or resolution.binding_id != binding.binding_id:
            raise ValueError("EGRESS_QOS_DESIRED_RESOLUTION_NOT_AUTHORIZED")
        desired = resolution.desired_payload(binding=binding)
        checksum = hashlib.sha256(_canon(desired).encode("utf-8")).hexdigest()
        existing = self.active(resolution.traffic_scope_key)
        if existing and existing["checksum"] == checksum:
            return {"status": "ALREADY_MATERIALIZED", "record": existing}
        record = {
            "desired_id": "EQDS-" + uuid.uuid4().hex[:16], "intent_id": resolution.intent_id,
            "desired_policy_id": resolution.policy_id, "owner_policy_id": resolution.policy_id,
            "resource_binding_id": resolution.binding_id, "traffic_scope_key": resolution.traffic_scope_key,
            "capacity_record_id": resolution.capacity_record_id, "generation_id": resolution.ingress_desired_generation_id,
            "desired": desired, "checksum": checksum, "status": "ACTIVE", "created_at": _now(),
        }
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("UPDATE egress_qos_desired_states_v1 SET status='SUPERSEDED' WHERE traffic_scope_key=? AND status='ACTIVE'", (resolution.traffic_scope_key,))
                connection.execute("""INSERT INTO egress_qos_desired_states_v1(
                    desired_id,intent_id,desired_policy_id,owner_policy_id,resource_binding_id,
                    traffic_scope_key,capacity_record_id,generation_id,desired_json,checksum,status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    record["desired_id"], record["intent_id"], record["desired_policy_id"], record["owner_policy_id"], record["resource_binding_id"],
                    record["traffic_scope_key"], record["capacity_record_id"], record["generation_id"], _canon(desired), checksum, "ACTIVE", record["created_at"],
                ))
                connection.commit()
            except Exception:
                connection.rollback(); raise
        self.store.add_audit_event(event_type="EGRESS_QOS_DESIRED_STATE_MATERIALIZED", resource_type="egress_qos_desired_state", resource_id=record["desired_id"], correlation={"intent_id": record["intent_id"], "generation_id": record["generation_id"]}, expected=desired, observed={"resource_binding_id": record["resource_binding_id"], "capacity_record_id": record["capacity_record_id"]}, action={"operation": "PERSIST_RESOLVED_DESIRED"}, result={"status": "ACTIVE", "checksum": checksum})
        return {"status": "MATERIALIZED", "record": record}

"""Runtime QoS desired-state adapter.

P4/ONOS classifies and marks the business five-tuple.  Linux TC/HTB then
schedules that mark on the dynamically selected shared switch egress.  Every
operation is read back, so a database-only change can never be reported as
applied.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from copy import deepcopy
from datetime import datetime, timezone
from ipaddress import ip_network
from typing import Any

from models.policy_update import effective_qos_mode, qos_values, traffic_scope_key
from models.flow_table import ActionParam, MatchField, TableEntry
from models.yang_policy import ActionType, YANGPolicy
from api.store import get_store
from infra.qos_resource_allocator import QoSResourceAllocator
from infra.resource_registry import get_resource_registry
from infra.stable_capacity import StableCapacityRepository
from infra.network_write_fencing import NetworkWriteFencing, current_write_context, require_network_write, write_context

logger = logging.getLogger(__name__)


def _stable_capacity_payload(record: Any) -> dict[str, Any]:
    """Serialize evidence in its unrounded form for the Mininet validator.

    The validator first proves ``stable - safety == schedulable`` and only
    then applies an authorized whole-Mbit scheduler cap.  Supplying the HTB
    floor in this evidence field makes a perfectly valid fractional record
    fail that invariant (for example 58.576 - 5 != 53).
    """
    return {
        **record.model_dump(mode="json"),
        "schedulable_capacity_mbps": StableCapacityRepository.schedulable_capacity(record),
    }


def _active_ingress_scheduler_cap(scope: str, record: Any) -> float | None:
    """Return an existing durable ingress cap that the evidence supports."""
    from infra.ingress_admission_state import IngressAdmissionDesiredRepository

    desired = IngressAdmissionDesiredRepository(get_store()).active_for_scope(scope)
    if not desired or str(desired.get("capacity_record_id") or "") != str(record.capacity_record_id):
        return None
    cap = float((desired.get("desired") or {}).get("schedulable_capacity_mbps") or 0)
    supported = StableCapacityRepository.htb_schedulable_capacity(record)
    return cap if cap > 0 and cap <= supported else None


def _shared_ingress_scheduler_contract(resource_key: str) -> float | None:
    """Return the strictest committed ingress root on one TC resource.

    A newly allocated best-effort or rate-limit scope is still attached to an
    existing physical HTB tree.  It therefore cannot silently make that tree
    revert to its nominal link rate merely because it has no binding yet.
    The service profile declares the resource before runtime discovery; active
    ingress desired state is the durable authority for that resource's root.
    """
    if not resource_key or resource_key == "UNBOUND":
        return None
    from infra.egress_qos_desired import resolve_egress_qos_desired_for_scope
    from infra.ingress_admission_state import IngressAdmissionDesiredRepository

    store = get_store()
    allocator = QoSResourceAllocator(store)
    roots: list[float] = []
    for desired in IngressAdmissionDesiredRepository(store).list_active():
        sibling = allocator.get_active_for_scope(desired["traffic_scope_key"])
        if sibling is None or sibling.resource_key != resource_key:
            continue
        try:
            resolution = resolve_egress_qos_desired_for_scope(
                desired["traffic_scope_key"], store=store,
                capacity_purpose="RECONCILE_EXISTING",
            )
        except TypeError as exc:
            # Production always supports the explicit purpose.  Retain
            # compatibility for injected legacy adapters used by unit tests.
            if "capacity_purpose" not in str(exc):
                raise
            resolution = resolve_egress_qos_desired_for_scope(
                desired["traffic_scope_key"], store=store,
            )
        if resolution.accepted and resolution.root_rate_mbps is not None:
            roots.append(float(resolution.root_rate_mbps))
    return min(roots) if roots else None


def _declared_qos_resource(service_id: str | None) -> str | None:
    """Resolve a profile-declared QoS resource without selector heuristics."""
    if not service_id:
        return None
    try:
        from api.service_profiles import get_service_profile

        profile = get_service_profile(service_id)
        link_id = (profile.qos_binding or {}).get("bottleneck_link")
        return get_resource_registry().resolve(link_id) if link_id else None
    except (KeyError, ValueError, AttributeError):
        logger.info("No declared QoS resource for service_id=%s", service_id)
        return None


def _as_policy(value: YANGPolicy | dict[str, Any]) -> YANGPolicy:
    return value if isinstance(value, YANGPolicy) else YANGPolicy(**value)


def _classifier_device_id(policy: YANGPolicy, execution_result: dict[str, Any]) -> str:
    """Resolve the classifier device from durable runtime or profile binding.

    Older active QoS records can retain a valid service-profile snapshot while
    losing their transient ``qos_enforcement`` runtime during a commit.  The
    profile's P4 counter binding is durable desired state, so it is the safe
    fallback for an explicit read/repair; it never invents a device id.
    """
    runtime = execution_result or {}
    binding = (runtime.get("qos_enforcement") or {}).get("qos_binding") or {}
    device_id = binding.get("classifier_device_id") or binding.get("device_id")
    if not device_id:
        installed = list(runtime.get("installed_flows") or [])
        if installed:
            device_id = installed[0].get("device_id")
    if not device_id:
        profile = getattr(policy, "service_profile_snapshot", None) or {}
        device_id = (profile.get("p4_counter_binding") or {}).get("device_id")
    return str(device_id or "")


def _payload(
    yang_policy: YANGPolicy, operation: str, resource_binding: Any | None = None,
    selected_path: list[str] | None = None,
) -> dict[str, Any]:
    policy = yang_policy.network_policy
    match = policy.match
    qos = qos_values(policy)
    payload = {
        "operation": operation,
        "scope_key": traffic_scope_key(policy).key,
        "policy_id": policy.policy_id,
        "source_ip": match.source_ip,
        "destination_ip": match.destination_ip,
        "protocol": getattr(match.protocol, "value", match.protocol),
        "source_port": match.source_port,
        "destination_port": match.destination_port,
        "vlan_id": match.vlan_id,
        "bandwidth_mbps": qos.get("bandwidth"),
        "burst_kb": qos.get("burst_size") or 64,
        "qos_mode": effective_qos_mode(policy),
        "dscp": qos.get("dscp_value") if qos.get("dscp_value") is not None else 46,
        "priority": 1 if effective_qos_mode(policy) == "MIN_BANDWIDTH" else 3,
    }
    if resource_binding is not None:
        payload.update({
            "resource_binding_id": resource_binding.binding_id,
            "class_id": resource_binding.class_id,
            "filter_id": resource_binding.filter_id,
        })
    if selected_path:
        payload["selected_path"] = list(selected_path)
    return payload


def qos_probe_payload(yang_policy: YANGPolicy | dict[str, Any], operation: str = "VERIFY") -> dict[str, Any]:
    return _payload(_as_policy(yang_policy), operation)


async def apply_qos_policy(
    yang_policy: YANGPolicy | dict[str, Any] | None,
    *,
    operation: str = "APPLY",
    selected_path: list[str] | None = None,
) -> dict[str, Any]:
    """Apply the exact bandwidth resource for a QoS policy.

    Verification is intentionally absent from this write path.  The legacy
    TC VERIFY operation could mutate a default class; callers must use the
    dedicated GET-only readback instead.
    """
    if yang_policy is None:
        return {"status": "skipped", "reason": "no policy"}
    policy_model = _as_policy(yang_policy)
    policy = policy_model.network_policy
    if policy.action.action_type not in {ActionType.RATE_LIMIT, ActionType.SET_PRIORITY}:
        return {"status": "skipped", "reason": f"non-QoS action: {policy.action.action_type.value}"}
    qos = qos_values(policy)
    if qos.get("bandwidth") is None:
        return {
            "status": "ok",
            "config_applied": True,
            "scope_key": traffic_scope_key(policy).key,
            "mechanism": "p4_dscp_only",
            "enforcement_path": "onos_p4_dscp",
            "reason": "policy has no bandwidth resource",
        }

    operation = operation.upper()
    if operation == "VERIFY":
        return {
            "status": "error", "reason_code": "LEGACY_QOS_VERIFY_DISABLED",
            "error": "READBACK_MUST_USE_GET_ENDPOINT", "write_executed": False,
        }
    if operation not in {"APPLY", "UPDATE", "CLEAR", "PREPARE", "COMMIT", "ROLLBACK"}:
        return {"status": "error", "reason_code": "READ_ONLY_OPERATION_REQUIRED", "write_executed": False}
    # Direct service callers (including controlled API operations) still use
    # the same durable lease/token boundary as a workflow.  A caller that
    # already owns a context, such as ExecuteFlowNode or recovery, retains
    # that owner rather than creating a competing lease.
    fencing_required = os.getenv("IDN_NETWORK_WRITE_FENCING_REQUIRED", "false").lower() == "true"
    if fencing_required and not current_write_context():
        lease = NetworkWriteFencing(get_store()).acquire(
            owner_type="QOS_API", owner_id=str(policy.policy_id),
            recovery_id=f"QOS-{policy.policy_id}", ttl_seconds=120,
        )
        try:
            with write_context(
                owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                fencing_token=int(lease["fencing_token"]),
            ):
                return await apply_qos_policy(
                    policy_model, operation=operation, selected_path=selected_path
                )
        finally:
            NetworkWriteFencing(get_store()).release(lease["lease_id"])
    authorization = None
    if fencing_required:
        authorization = require_network_write(operation=f"QOS_{operation}")
        if not authorization["authorized"]:
            return {"status": "error", "reason_code": authorization["reason"],
                    "authorization": authorization, "write_executed": False}

    from api._probe_session import get_probe_session

    scope = traffic_scope_key(policy).key
    mode = effective_qos_mode(policy)
    allocator = QoSResourceAllocator(get_store())
    existing_binding = allocator.get(scope)
    # Reuse only a fresh record tied to the already selected bottleneck.  A
    # policy without a live binding remains on the normal discovery path; it
    # is never guessed from a service name or transport port.
    stable_record = None
    if existing_binding is not None and existing_binding.resource_key != "UNBOUND":
        stable_record = StableCapacityRepository(get_store()).active_for(
            bottleneck_egress=existing_binding.resource_key,
            topology_id=get_resource_registry().snapshot().topology_name,
        )
    # Older durable policy documents did not persist ``service_id``.  Never
    # erase the identity already attached to a live allocation during a
    # reapply, and recover it from the selector only when there is no durable
    # binding to use.  Service profile matching is data driven; it does not
    # contain business/port-specific branches.
    service_id = getattr(policy_model, "service_id", None)
    if not service_id and existing_binding is not None:
        service_id = existing_binding.service_id
    if not service_id:
        try:
            from api.service_profiles import resolve_service_profile_for_policy

            service_id = resolve_service_profile_for_policy(policy_model).service_id
        except (KeyError, ValueError):
            logger.info(
                "No unique service profile for policy_id=%s; retaining an unscoped allocation",
                policy.policy_id,
            )
    tenant_id = "default"
    if service_id:
        try:
            from api.service_profiles import get_service_profile

            tenant_id = get_service_profile(service_id).tenant_id
        except Exception:
            logger.warning("Unable to resolve tenant for service_id=%s", service_id)
    binding = allocator.allocate(
        policy_id=policy.policy_id, traffic_scope_key=scope,
        rate_mbps=float(qos["bandwidth"]),
        ceil_mbps=(
            float(qos["bandwidth"]) if mode == "RATE_LIMIT"
            else (
                StableCapacityRepository.htb_schedulable_capacity(stable_record)
                if stable_record else float(os.getenv("IDN_QOS_LINK_CEIL_MBPS", "100"))
            )
        ),
        service_id=service_id, dscp=qos.get("dscp_value"),
        reservation_kind="RATE_LIMIT" if mode == "RATE_LIMIT" else "GUARANTEED",
        tenant_id=tenant_id,
    )
    probe = await get_probe_session()
    probe_payload = _payload(policy_model, operation, binding, selected_path=selected_path)
    # A fresh scope has no runtime binding before its first actuator write,
    # but it can share an already managed ingress HTB tree.  Propagate the
    # sibling's committed root contract for every QoS mode so a RATE_LIMIT
    # create does not expect the nominal 100 Mbps root while TC correctly
    # preserves the durable 49 Mbps scheduler.
    shared_resource = (
        existing_binding.resource_key
        if existing_binding is not None and existing_binding.resource_key != "UNBOUND"
        else _declared_qos_resource(service_id)
    )
    shared_root = _shared_ingress_scheduler_contract(shared_resource or "")
    if shared_root is not None:
        probe_payload["authorized_schedulable_capacity_mbps"] = shared_root
        probe_payload["durable_scheduler_contract_mbps"] = shared_root
    if stable_record is not None and mode == "MIN_BANDWIDTH":
        probe_payload["stable_capacity"] = _stable_capacity_payload(stable_record)
        # Keep the durable ingress contract (49 Mbps here) distinct from the
        # fractional evidence it is admitted against (53.576 Mbps here).
        # The Mininet actuator validates the latter before applying the former.
        authorized_cap = _active_ingress_scheduler_cap(scope, stable_record)
        if authorized_cap is not None:
            probe_payload["authorized_schedulable_capacity_mbps"] = authorized_cap
    result = await probe.post("/policy/qos", json_data=probe_payload)
    result = result or {"status": "error", "error": "empty Mininet probe response"}
    if authorization is not None:
        NetworkWriteFencing(get_store()).record_execution(
            authorization, write_executed=bool(result.get("status") == "ok" and result.get("config_applied"))
        )
    path = "onos_p4_classifier+switch_egress_htb"
    enriched = {
        **result,
        "policy_id": policy.policy_id,
        "scope_key": traffic_scope_key(policy).key,
        "enforcement_path": path,
        "operation": operation,
        "flow_ids": [],
    }
    if result.get("status") != "ok" or not result.get("config_applied"):
        if existing_binding is None and operation == "APPLY":
            allocator.release(scope)
        raise RuntimeError(f"QoS {operation} readback failed: {enriched}")
    bound = allocator.bind_runtime(binding.binding_id, result)
    enriched["resource_binding"] = bound.model_dump(mode="json")
    return enriched


async def verify_qos_policy(
    yang_policy: YANGPolicy | dict[str, Any] | None,
    *,
    selected_path: list[str] | None = None,
) -> dict[str, Any]:
    """Read QoS state without allocating, applying, or posting to TC."""
    if yang_policy is None:
        return {"status": "skipped", "reason": "no policy"}
    policy_model = _as_policy(yang_policy)
    from api._probe_session import get_probe_session
    observed = await (await get_probe_session()).get(
        "/policy/qos-readback", {"scope_key": traffic_scope_key(policy_model.network_policy).key}
    ) or {}
    return {**observed, "operation": "GET_READBACK", "write_executed": False,
            "policy_id": policy_model.network_policy.policy_id,
            "scope_key": traffic_scope_key(policy_model.network_policy).key}


def _pi_matches(flow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    matches: dict[str, dict[str, Any]] = {}
    for criterion in (flow.get("selector") or {}).get("criteria", []) or []:
        for item in criterion.get("matches", []) or []:
            field = item.get("field")
            if field:
                matches[str(field)] = item
    return matches


def _pi_action(flow: dict[str, Any]) -> dict[str, Any]:
    for instruction in (flow.get("treatment") or {}).get("instructions", []) or []:
        if instruction.get("subtype") == "ACTION" or instruction.get("actionId"):
            return instruction
    return {}


def canonical_classifier_identity(
    policy: YANGPolicy | dict[str, Any],
    device_id: str,
) -> dict[str, Any]:
    """Return the stable identity of a P4 traffic classifier.

    The identity deliberately excludes priority, policy/execution generation,
    flow ID, rate and action parameters.  Those values describe ownership or
    desired treatment *of* a selector and must never turn a duplicate
    five-tuple into an independent classifier.
    """
    from mcp_servers.onos_server import canonicalize_onos_device_id

    expected = _expected_classifier(_as_policy(policy))
    return {
        "device_id": canonicalize_onos_device_id(device_id),
        "table_name": "MyIngress.sla_enforcement",
        "selector": {
            key: {"value": value["value"], "mask": value.get("mask", "")}
            for key, value in sorted(expected.items())
            if key != "dscp"
        },
    }


def classify_classifier_overlap(
    policy: YANGPolicy | dict[str, Any],
    device_id: str,
    flows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Classify controller entries against one canonical classifier identity.

    ``EXACT_SAME_SELECTOR`` is the supersession/reuse path.  It is intentionally
    independent of priority and action; callers can then decide whether an
    identical treatment is reusable or a staged replacement is required.
    """
    policy_model = _as_policy(policy)
    identity = canonical_classifier_identity(policy_model, device_id)
    expected = _expected_classifier(policy_model)
    # Only entries carrying the complete P4 SLA selector are candidates for
    # classifier ownership.  A forwarding-table rule often shares a
    # destination address with a classifier, but it has neither the source
    # address nor L4 selector and must not be treated as an SLA overlap.
    # The P4 program materialises these keys for sla_enforcement entries even
    # when a value is wildcarded, so this still detects a genuinely broader
    # SLA classifier as PARTIAL_OVERLAP.
    selector_fields = {field for field in expected if field != "dscp"}
    observations: list[dict[str, Any]] = []
    for flow in flows:
        matches = _pi_matches(flow)
        if not selector_fields.issubset(matches):
            continue
        exact = bool(matches) and all(
            _pi_field_value_matches(field, (matches.get(field) or {}).get("value"), values["value"])
            and (
                (matches.get(field) or {}).get("match") != "ternary"
                or str((matches.get(field) or {}).get("mask", "")).lower() == values.get("mask", "")
            )
            for field, values in expected.items()
            if field != "dscp"
        )
        if not exact:
            # A classifier overlaps only when every selector field can match
            # the same packet. Requiring merely one equal key turned distinct
            # source /32 services that share UDP/5004 into false conflicts.
            # Malformed controller evidence remains conservative.
            overlapping_fields = [
                field for field, values in expected.items()
                if field != "dscp"
                and _pi_ternary_ranges_overlap(matches.get(field) or {}, values)
            ]
            if len(overlapping_fields) == len(selector_fields):
                observations.append({
                    "classification": "PARTIAL_OVERLAP",
                    "flow_id": str(flow.get("id") or ""),
                    "device_id": flow.get("deviceId") or identity["device_id"],
                    "priority": flow.get("priority"),
                    "state": flow.get("state"),
                    "overlapping_match_fields": overlapping_fields,
                })
            continue
        action = _pi_action(flow)
        raw_dscp = (action.get("actionParams") or {}).get("dscp_value")
        try:
            dscp = int(str(raw_dscp), 16)
        except (TypeError, ValueError):
            dscp = None
        observations.append({
            "classification": "EXACT_SAME_SELECTOR",
            "flow_id": str(flow.get("id") or ""),
            "device_id": flow.get("deviceId") or identity["device_id"],
            "priority": flow.get("priority"),
            "state": flow.get("state"),
            "action_name": action.get("actionId"),
            "dscp": dscp,
            "treatment_equivalent": (
                action.get("actionId") == "MyIngress.set_dscp"
                and dscp == expected["dscp"]
            ),
            "canonical_identity": identity,
        })
    return observations


def _pi_field_value_matches(field: str, actual: Any, expected_hex: str) -> bool:
    from api.p4_evidence import canonical_pi_value_matches

    return canonical_pi_value_matches(field, actual, expected_hex)


def _pi_ternary_ranges_overlap(
    actual: dict[str, Any], expected: dict[str, Any],
) -> bool:
    """Return whether two PI ternary field constraints can match one value.

    For ternary constraints, only bits constrained by *both* masks need to
    agree. A malformed controller value is treated as overlapping so it stays
    on the safe review path rather than opening an append path.
    """
    def _decode(value: Any, mask: Any | None = None) -> tuple[bytes, bytes]:
        raw = str(value or "").lower().removeprefix("0x")
        raw_mask = str(mask or "").lower().removeprefix("0x")
        if not raw:
            raise ValueError("empty PI match value")
        if len(raw) % 2:
            raw = f"0{raw}"
        if not raw_mask:
            raw_mask = "ff" * (len(raw) // 2)
        if len(raw_mask) % 2:
            raw_mask = f"0{raw_mask}"
        value_bytes = bytes.fromhex(raw)
        mask_bytes = bytes.fromhex(raw_mask)
        if len(value_bytes) != len(mask_bytes):
            raise ValueError("PI value/mask length mismatch")
        return value_bytes, mask_bytes

    try:
        actual_value, actual_mask = _decode(actual.get("value"), actual.get("mask"))
        expected_value, expected_mask = _decode(expected.get("value"), expected.get("mask"))
        if len(actual_value) != len(expected_value):
            raise ValueError("PI field width mismatch")
        shared_mask = bytes(a & b for a, b in zip(actual_mask, expected_mask))
        return all(
            (actual_byte & mask) == (expected_byte & mask)
            for actual_byte, expected_byte, mask in zip(
                actual_value, expected_value, shared_mask,
            )
        )
    except (TypeError, ValueError):
        return True


def _expected_classifier(policy: YANGPolicy) -> dict[str, Any]:
    match = policy.network_policy.match
    qos = qos_values(policy.network_policy)
    source = ip_network(str(match.source_ip), strict=False)
    destination = ip_network(str(match.destination_ip), strict=False)
    protocol = getattr(match.protocol, "value", match.protocol)
    expected = {
        "hdr.ipv4.srcAddr": {
            "value": source.network_address.packed.hex(),
            "mask": source.netmask.packed.hex(),
        },
        "hdr.ipv4.dstAddr": {
            "value": destination.network_address.packed.hex(),
            "mask": destination.netmask.packed.hex(),
        },
        "hdr.ipv4.protocol": {
            "value": format({"TCP": 6, "UDP": 17}.get(str(protocol).upper(), 0), "02x"),
            "mask": "ff",
        },
        "dscp": int(qos.get("dscp_value") if qos.get("dscp_value") is not None else 46),
    }
    if str(protocol).upper() in {"TCP", "UDP"} and match.destination_port is not None:
        expected[f"hdr.{str(protocol).lower()}.dstPort"] = {
            "value": format(int(match.destination_port), "04x"),
            "mask": "ffff",
        }
    return expected


def build_current_classifier_rule(
    yang_policy: YANGPolicy | dict[str, Any], device_id: str,
    *, template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile one SLA classifier against the currently loaded P4Info schema.

    Persisted ONOS JSON is evidence of an earlier deployment, not durable
    desired state.  Reconciliation therefore rebuilds selector fields from
    YANG and filters them through the current P4Info registry.  This makes a
    TCP/UDP table-schema evolution recoverable after controller restarts.
    """
    from mcp_servers.flow_adapter import FlowAdapter
    from mcp_servers.p4info_registry import p4info_registry

    policy = _as_policy(yang_policy)
    expected = _expected_classifier(policy)
    table_name = "MyIngress.sla_enforcement"
    schema_fields = p4info_registry.table_match_fields(table_name)
    match_fields: list[MatchField] = []
    for field_name, values in expected.items():
        if field_name == "dscp" or field_name not in schema_fields:
            continue
        field_schema = schema_fields[field_name]
        match_type = str(field_schema.get("match_type") or "ternary").lower()
        match_fields.append(MatchField(
            field_name=field_name,
            match_type=match_type,
            value=values["value"],
            mask=values.get("mask") if match_type == "ternary" else None,
        ))
    required = {
        "hdr.ipv4.srcAddr", "hdr.ipv4.dstAddr", "hdr.ipv4.protocol",
        f"hdr.{str(getattr(policy.network_policy.match.protocol, 'value', policy.network_policy.match.protocol)).lower()}.dstPort",
    }
    missing = sorted(required - {item.field_name for item in match_fields})
    if missing:
        raise RuntimeError(
            f"current P4Info cannot represent classifier fields: {', '.join(missing)}"
        )
    priority = int(
        (template or {}).get("priority")
        or getattr(policy.network_policy.metadata, "priority", 100)
        or 100
    )
    entry = TableEntry(
        table_name=table_name,
        match_fields=match_fields,
        action_name="MyIngress.set_dscp",
        action_params=[ActionParam(param_name="dscp_value", value=expected["dscp"])],
        priority=max(priority, 1),
    )
    return FlowAdapter(p4info_registry).to_onos_flow_rule(entry, device_id)


async def verify_p4_classifier(
    yang_policy: YANGPolicy | dict[str, Any],
    execution_result: dict[str, Any],
    *,
    require_counter_hit: bool = False,
    minimum_packets: int | None = None,
    minimum_bytes: int | None = None,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float | None = None,
) -> dict[str, Any]:
    """Read the controller-owned classifier and validate selector/treatment."""
    policy = _as_policy(yang_policy)
    expected = _expected_classifier(policy)
    binding = ((execution_result or {}).get("qos_enforcement") or {}).get("qos_binding") or {}
    device_id = _classifier_device_id(policy, execution_result)
    installed = list((execution_result or {}).get("installed_flows") or [])
    if not device_id:
        raise RuntimeError("classifier device is absent from runtime binding")

    from mcp_servers.onos_server import get_onos_client, onos_client as shared_onos_client

    from mcp_servers.onos_server import canonicalize_onos_device_id, onos_device_path_segment
    device_id = canonicalize_onos_device_id(device_id)
    installed_ids = {
        str(item.get("flow_id")) for item in installed
        if item.get("device_id") == device_id and item.get("flow_id")
    }
    # ONOS 2.7 confirms P4Runtime FlowRules on its device polling cycle.  The
    # REST create call can therefore be successful while the rule remains in
    # PENDING_ADD for up to one fallback polling interval (30 seconds in the
    # bundled controller).  Wait for concrete ADDED readback instead of either
    # treating PENDING_ADD as success or rolling a healthy device write back.
    timeout_env = (
        "ONOS_COUNTER_VERIFY_TIMEOUT_SECONDS"
        if require_counter_hit else "ONOS_FLOW_VERIFY_TIMEOUT_SECONDS"
    )
    timeout_default = "35" if require_counter_hit else "40"
    timeout = max(
        float(timeout_seconds)
        if timeout_seconds is not None
        else float(os.getenv(timeout_env, timeout_default)),
        0.0,
    )
    # Keep the production default deliberately modest, while allowing focused
    # convergence tests and short controller polling windows to request a
    # millisecond-scale retry.  The previous 100-ms floor could consume an
    # entire explicitly bounded verification deadline before the second ONOS
    # observation was made.
    interval = max(
        float(poll_interval_seconds)
        if poll_interval_seconds is not None
        else float(os.getenv("ONOS_FLOW_VERIFY_INTERVAL_SECONDS", "2")),
        0.001,
    )
    deadline = time.monotonic() + timeout
    evidence: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    expected_semantic = {
        "device_id": device_id,
        "selector": {key: {"value": value["value"], "mask": value.get("mask", "")}
                     for key, value in sorted(expected.items()) if key != "dscp"},
        "action": "MyIngress.set_dscp", "dscp": expected["dscp"],
    }
    expected_semantic_hash = hashlib.sha256(json.dumps(
        expected_semantic, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    client = get_onos_client()
    owns_client = client is not shared_onos_client
    try:
        while True:
            attempt_started = time.monotonic()
            scoped_error = None
            inventory_source = "device_scoped"
            try:
                # A shared ONOS session can retain a stalled connection while
                # the controller itself is healthy.  Readback is evidence,
                # not an unbounded background operation: each scoped/global
                # request is charged to this classifier's remaining deadline.
                # Without this bound a node-level timeout cancels the workflow
                # mid-read and hides a controller result that is already live.
                remaining = max(deadline - time.monotonic(), 0.0)
                if remaining <= 0:
                    raise TimeoutError("ONOS_FLOW_READBACK_DEADLINE_EXPIRED")
                raw = await asyncio.wait_for(client.get_flows(device_id), timeout=remaining)
                flows = list(raw.get("flows") or [])
            except Exception as exc:
                # A device-scoped query is an optimization, never the sole
                # ownership proof.  ONOS can expose /flows globally while its
                # per-device route/store view is still converging after a
                # restart.  Parse the global inventory and filter by the
                # canonical domain identity instead of treating HTTP 404 as a
                # completed repair failure.
                scoped_error = str(exc)
                inventory_source = "global_fallback"
                try:
                    remaining = max(deadline - time.monotonic(), 0.0)
                    if remaining <= 0:
                        raise TimeoutError("ONOS_FLOW_READBACK_DEADLINE_EXPIRED")
                    raw = await asyncio.wait_for(client.get_flows(), timeout=remaining)
                    flows = [item for item in (raw.get("flows") or [])
                             if canonicalize_onos_device_id(item.get("deviceId") or "") == device_id]
                except Exception as global_exc:
                    flows = []
                    scoped_error = f"scoped={scoped_error}; global={global_exc}"
            attempts.append({
                "attempt": len(attempts) + 1,
                "at": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int((time.monotonic() - attempt_started) * 1000),
                "verification_method": inventory_source,
                "request_url": f"/onos/v1/flows/{onos_device_path_segment(device_id)}",
                "scoped_error": scoped_error,
                "flow_count": len(flows),
            })
            # A matching controller rule is not sufficient ownership proof:
            # priority is deliberately outside the classifier identity, so two
            # ADDED rules with the same selector/action are an unsafe shadow,
            # not two independent classifiers.  Report the full exact count
            # even when runtime lineage only remembers one of their IDs; the
            # workflow's reuse path will then enter its formal supersession
            # cleanup rather than silently accepting the first match.
            exact_matches = [
                item for item in classify_classifier_overlap(policy, device_id, flows)
                if item.get("classification") == "EXACT_SAME_SELECTOR"
                and str(item.get("state") or "").upper() == "ADDED"
                and bool(item.get("treatment_equivalent"))
            ]
            exact_match_count = len(exact_matches)
            candidates = [flow for flow in flows if str(flow.get("id")) in installed_ids]
            # Controller-assigned flow IDs are not stable across ONOS or
            # Stratum restarts.  Fall back to the exact canonical selector and
            # treatment scan instead of treating a stale persisted ID as proof
            # that no matching entry can exist.
            if not candidates:
                candidates = flows
            evidence = []
            pending = bool(scoped_error) or not candidates
            for flow in candidates:
                matches = _pi_matches(flow)
                action = _pi_action(flow)
                selector_ok = True
                for field, values in expected.items():
                    if field == "dscp":
                        continue
                    actual = matches.get(field) or {}
                    selector_ok = selector_ok and _pi_field_value_matches(
                        field, actual.get("value"), values["value"]
                    )
                    if actual.get("match") == "ternary":
                        selector_ok = selector_ok and str(actual.get("mask", "")).lower() == values["mask"]
                params = action.get("actionParams") or {}
                dscp_raw = params.get("dscp_value")
                try:
                    dscp_value = int(str(dscp_raw), 16)
                except (TypeError, ValueError):
                    dscp_value = None
                action_ok = action.get("actionId") == "MyIngress.set_dscp" and dscp_value == expected["dscp"]
                flow_state = str(flow.get("state") or "").upper()
                state_ok = flow_state == "ADDED"
                pending = pending or flow_state in {"PENDING_ADD", "PENDING"}
                item = {
                    "flow_id": str(flow.get("id") or ""),
                    "device_id": flow.get("deviceId") or device_id,
                    "state": flow.get("state"),
                    "selector": flow.get("selector") or {},
                    "treatment": flow.get("treatment") or {},
                    "packets": int(flow.get("packets") or 0),
                    "bytes": int(flow.get("bytes") or 0),
                    "selector_verified": selector_ok,
                    "treatment_verified": action_ok,
                    "state_verified": state_ok,
                }
                evidence.append(item)
                if selector_ok and action_ok and state_ok:
                    if minimum_packets is not None or minimum_bytes is not None:
                        counter_hit = (
                            item["packets"] > int(minimum_packets or 0)
                            or item["bytes"] > int(minimum_bytes or 0)
                        )
                    else:
                        counter_hit = item["packets"] > 0 or item["bytes"] > 0
                    if require_counter_hit and not counter_hit:
                        # State/selector/action are already correct, but ONOS
                        # refreshes BMv2 direct counters on a later device poll.
                        # Keep polling so the final report contains observed
                        # packet evidence instead of an early, misleading zero.
                        pending = True
                        continue
                    observed_semantic_hash = hashlib.sha256(json.dumps({
                        "device_id": canonicalize_onos_device_id(flow.get("deviceId") or device_id),
                        "selector": {key: {"value": matches[key].get("value"), "mask": matches[key].get("mask", "")}
                                     for key in sorted(expected) if key != "dscp"},
                        "action": action.get("actionId"), "dscp": dscp_value,
                    }, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
                    return {
                        "status": "ok",
                        "verification_state": "P4_CLASSIFIER_VERIFIED",
                        "device_id": device_id,
                        "flow_id": item["flow_id"],
                        "flow_state": item["state"],
                        "switch_name": binding.get("switch_name"),
                        "app_id": flow.get("appId"),
                        "priority": flow.get("priority"),
                        "selector": item["selector"],
                        "treatment": item["treatment"],
                        "table_name": "MyIngress.sla_enforcement",
                        "action_name": "MyIngress.set_dscp",
                        "dscp": expected["dscp"],
                        "source_ip": str(policy.network_policy.match.source_ip),
                        "destination_ip": str(policy.network_policy.match.destination_ip),
                        "protocol": str(getattr(
                            policy.network_policy.match.protocol,
                            "value",
                            policy.network_policy.match.protocol,
                        )).upper(),
                        "destination_port": policy.network_policy.match.destination_port,
                        "classifier_reused": bool((execution_result or {}).get("classifier_reused")),
                        "packets": item["packets"],
                        "bytes": item["bytes"],
                        "counter_hit": counter_hit,
                        "matching_entry_count": exact_match_count,
                        "duplicate_exact_selector": exact_match_count > 1,
                        "duplicate_flow_ids": [str(item.get("flow_id") or "") for item in exact_matches],
                        "read_at": datetime.now(timezone.utc).isoformat(),
                        "config_applied": True,
                        "onos_flow_verification": {
                            "device_id": device_id,
                            "canonical_device_id": device_id,
                            "encoded_device_id": onos_device_path_segment(device_id),
                            "verification_method": inventory_source,
                            "request_url": f"/onos/v1/flows/{onos_device_path_segment(device_id)}",
                            "http_status": 200,
                            "flow_inventory_source": inventory_source,
                            "expected_flow_semantic_hash": expected_semantic_hash,
                            "observed_flow_semantic_hash": observed_semantic_hash,
                            "matched_flow_id": item["flow_id"],
                            "matched_flow_state": item["state"],
                            "matched_app_id": flow.get("appId"),
                            "matched_device_id": flow.get("deviceId"),
                            "control_plane_verified": True,
                            "transient": False, "retryable": False,
                            "reason_code": "ONOS_FLOW_VERIFIED",
                            "attempts": attempts,
                            "first_attempt_at": attempts[0]["at"],
                            "verified_at": datetime.now(timezone.utc).isoformat(),
                            "duration_ms": int((time.monotonic() - (deadline - timeout)) * 1000),
                        },
                    }
            if time.monotonic() >= deadline:
                break
            # Fast bounded convergence poll: it is intentionally charged to
            # the caller's deadline, never a separate fixed 30-second wait.
            delay = (0.25, 0.5, 1.0, 1.0, 2.0)[min(len(attempts) - 1, 4)]
            await asyncio.sleep(min(delay, interval, max(deadline - time.monotonic(), 0.0)))
    finally:
        if owns_client:
            await client.close()
    reason = "ONOS_FLOW_NOT_YET_VISIBLE" if attempts and any(item.get("scoped_error") for item in attempts) else "ONOS_FLOW_SEMANTIC_MISMATCH"
    raise RuntimeError(f"{reason}: ONOS classifier readback mismatch on {device_id}: {evidence}")


async def ensure_p4_classifier(
    yang_policy: YANGPolicy | dict[str, Any],
    execution_result: dict[str, Any],
    *,
    store: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Verify an exact classifier and repair it from persisted desired rules.

    This is used only by an explicit service validation run.  A GET evidence
    request remains read-only.  Repair is bounded by the existing ONOS
    read-after-write timeout in :func:`verify_p4_classifier`.
    """
    runtime = deepcopy(execution_result or {})
    try:
        verified = await verify_p4_classifier(yang_policy, runtime)
        return verified, runtime, {"status": "VERIFIED", "operation": "READ_ONLY"}
    except Exception as initial_exc:
        authorization = None
        if os.getenv("IDN_NETWORK_WRITE_FENCING_REQUIRED", "false").lower() == "true":
            authorization = require_network_write(
                operation="P4_CLASSIFIER_REPAIR", store=store or get_store(),
            )
            if not authorization["authorized"]:
                raise RuntimeError(f"P4_CLASSIFIER_REPAIR_FENCED:{authorization['reason']}") from initial_exc
        device_id = _classifier_device_id(_as_policy(yang_policy), runtime)
        persisted_rules = list((runtime.get("delivered_rules") or {}).get(device_id) or [])
        if not device_id:
            raise RuntimeError(
                f"classifier repair unavailable: device={device_id or 'missing'}, "
                f"persisted_rules={len(persisted_rules)}, initial={initial_exc}"
            ) from initial_exc

        # Keep the recovered binding with the rebuilt rule so a later ONOS
        # restart can reconcile the same classifier from persisted state.
        runtime.setdefault("qos_enforcement", {}).setdefault("qos_binding", {})[
            "classifier_device_id"
        ] = device_id

        # Always compile from current desired state/P4Info.  Replaying
        # persisted_rules here would keep an obsolete transport field alive
        # after a pipeline upgrade.
        rules = [build_current_classifier_rule(
            yang_policy, device_id,
            template=persisted_rules[0] if persisted_rules else None,
        )]

        from mcp_servers.flow_delivery import FlowDeliveryFactory

        delivery = FlowDeliveryFactory.create()
        posted = await delivery.post_flows(device_id, rules)
        if authorization is not None:
            NetworkWriteFencing(store or get_store()).record_execution(
                authorization, write_executed=bool(posted and posted.get("status") == "SUCCESS" and not posted.get("errors"))
            )
        if posted.get("status") != "SUCCESS" or posted.get("errors"):
            raise RuntimeError(
                f"classifier repair write failed on {device_id}: {posted}"
            ) from initial_exc
        new_ids = [str(item) for item in posted.get("flow_ids") or [] if item]
        runtime["installed_flows"] = [
            item for item in runtime.get("installed_flows") or []
            if item.get("device_id") != device_id
        ] + [{"device_id": device_id, "flow_id": item} for item in new_ids]
        runtime.setdefault("superseded_flow_ids", []).extend([
            str(item.get("flow_id")) for item in (execution_result or {}).get("installed_flows") or []
            if item.get("device_id") == device_id and item.get("flow_id")
        ])
        runtime["delivered_rules"] = {
            **(runtime.get("delivered_rules") or {}), device_id: rules,
        }
        runtime["classifier_schema_rebuilt"] = True
        verified = await verify_p4_classifier(yang_policy, runtime)
        return verified, runtime, {
            "status": "RESTORED",
            "operation": "ONOS_REINSTALL_AND_POLL",
            "device_id": device_id,
            "flow_ids": new_ids,
            "initial_error": {
                "type": type(initial_exc).__name__,
                "detail": str(initial_exc),
            },
        }


async def verify_execution_plane(
    yang_policy: YANGPolicy | dict[str, Any],
    execution_result: dict[str, Any],
) -> dict[str, Any]:
    classifier = await verify_p4_classifier(yang_policy, execution_result)
    switch_qos = await verify_qos_policy(yang_policy)
    return {
        "status": "ok",
        "execution_architecture": "P4_CLASSIFIER + SWITCH_EGRESS_HTB",
        "p4_classifier": classifier,
        "switch_egress_qos": switch_qos,
        "config_applied": bool(classifier.get("config_applied") and switch_qos.get("config_applied")),
        "verification_states": [
            classifier.get("verification_state"),
            switch_qos.get("verification_state"),
        ],
    }


def p4_counter_evidence(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Build one-run direct-counter evidence; cumulative totals alone are insufficient."""
    before_packets = int(before.get("packets") or 0)
    before_bytes = int(before.get("bytes") or 0)
    after_packets = int(after.get("packets") or 0)
    after_bytes = int(after.get("bytes") or 0)
    packets_delta = max(after_packets - before_packets, 0)
    bytes_delta = max(after_bytes - before_bytes, 0)
    after_status = str(after.get("status") or "").upper()
    # Older internal callers passed a verified classifier snapshot containing
    # only its packet/byte values.  Preserve that input contract while making
    # every new read explicitly carry matching_entry_count/status.
    implicit_match = "packets" in after or "bytes" in after
    matching_entry_count = int(
        after.get("matching_entry_count")
        if after.get("matching_entry_count") is not None
        else (1 if implicit_match and after_status not in {"NO_MATCHING_ENTRY", "READ_FAILED", "ERROR"} else 0)
    )
    terminal_evidence_statuses = {
        "READ_FAILED", "DEVICE_UNAVAILABLE", "TABLE_NOT_FOUND",
        "COUNTER_NOT_BOUND", "SELECTOR_MISMATCH", "P4RUNTIME_ERROR",
    }
    read_failed = after_status in {"ERROR", *terminal_evidence_statuses}
    increased = packets_delta > 0 or bytes_delta > 0
    if read_failed:
        status = after_status if after_status in terminal_evidence_statuses else "READ_FAILED"
    elif after_status in {"UNSUPPORTED", "NOT_APPLICABLE"}:
        status = after_status
    elif matching_entry_count <= 0:
        status = "NO_MATCHING_ENTRY"
    elif increased:
        status = "VERIFIED"
    else:
        status = "MATCHING_ENTRY_NO_TRAFFIC"
    result = {
        "status": status,
        "verification_state": (
            "P4_COUNTER_VERIFIED" if status == "VERIFIED"
            else "P4_COUNTER_ZERO" if status == "MATCHING_ENTRY_NO_TRAFFIC"
            else status
        ),
        "before_packets": before_packets,
        "after_packets": after_packets,
        "delta_packets": packets_delta,
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
        "delta_bytes": bytes_delta,
        # Backward-compatible field names used by existing reports.
        "packets_before": before_packets,
        "packets_after": after_packets,
        "packets_delta": packets_delta,
        "bytes_before": before_bytes,
        "bytes_after": after_bytes,
        "bytes_delta": bytes_delta,
        "matching_entry_count": matching_entry_count,
        "nonzero": after_packets > 0 or after_bytes > 0,
        "counter_hit": status == "VERIFIED",
        "read_at": after.get("read_at") or datetime.now(timezone.utc).isoformat(),
    }
    for key in (
        "service_id", "device_id", "switch", "logical_device_number",
        "grpc_host", "grpc_port", "grpc_endpoint", "p4runtime_node_id",
        "table_name", "table_id", "action_name", "counter_name", "counter_id",
        "selector", "reason_code", "reason_detail", "read_request",
        "read_response", "exception", "validation_run_id",
    ):
        if key in after:
            result[key] = deepcopy(after.get(key))
    result["before_read_at"] = before.get("read_at")
    result["after_read_at"] = after.get("read_at")
    return result


async def run_qos_demo_validation(
    yang_policy: YANGPolicy | dict[str, Any],
    execution_result: dict[str, Any],
    *,
    scenario: str = "CURRENT",
    duration: int = 15,
    target_offered_mbps: int | None = None,
    background_offered_mbps: int | None = None,
) -> dict[str, Any]:
    """Run a bounded, non-mutating competition demonstration.

    The endpoint deliberately refuses a scenario whose semantic mode differs
    from the active policy.  Mode changes must continue through the formal
    intent transaction chain.
    """
    policy = _as_policy(yang_policy)
    mode = effective_qos_mode(policy)
    scenario = str(scenario or "CURRENT").upper()
    if scenario == "CURRENT":
        scenario = "RATE_LIMIT_NO_BACKGROUND" if mode == "RATE_LIMIT" else "MIN_BANDWIDTH_COMPETITION"
    required_mode = {
        "RATE_LIMIT_NO_BACKGROUND": "RATE_LIMIT",
        "MIN_BANDWIDTH_COMPETITION": "MIN_BANDWIDTH",
    }.get(scenario)
    if required_mode is None:
        raise ValueError(f"unsupported QoS demo scenario: {scenario}")
    if mode != required_mode:
        raise ValueError(
            f"{scenario} requires active {required_mode}; switch mode through the formal intent API first"
        )

    qos = qos_values(policy.network_policy)
    bandwidth = int(qos.get("bandwidth") or 0)
    before_plane = await verify_execution_plane(policy, execution_result)
    before_classifier = before_plane["p4_classifier"]
    probe_payload = qos_probe_payload(policy)
    include_background = scenario == "MIN_BANDWIDTH_COMPETITION"
    probe_payload.update({
        "test_profile": scenario,
        "include_background": include_background,
        "duration": max(15, min(int(duration), 30)),
        "target_offered_mbps": int(target_offered_mbps or max(bandwidth * 3, 30)),
        "background_offered_mbps": int(
            background_offered_mbps or (100 if include_background else 0)
        ),
    })
    from api._probe_session import get_probe_session

    probe = await get_probe_session()
    traffic = await probe.post("/probe/qos-kpi", json_data=probe_payload)
    if not isinstance(traffic, dict) or traffic.get("status") != "ok":
        raise RuntimeError(f"QoS demo traffic failed: {traffic}")
    after_classifier = await verify_p4_classifier(
        policy,
        execution_result,
        require_counter_hit=True,
        minimum_packets=int(before_classifier.get("packets") or 0),
        minimum_bytes=int(before_classifier.get("bytes") or 0),
    )
    counter = p4_counter_evidence(before_classifier, after_classifier)
    switch_qos = await verify_qos_policy(policy)
    throughput = float(traffic.get("throughput_mbps") or 0)
    if mode == "RATE_LIMIT":
        minimum = bandwidth * 0.90
        maximum = bandwidth * 1.10
        kpi_passed = (
            traffic.get("background_started") is False
            and minimum <= throughput <= maximum
        )
        threshold = {"operator": "between", "minimum_mbps": minimum, "maximum_mbps": maximum}
        verification_state = "RATE_LIMIT_KPI_VERIFIED" if kpi_passed else "KPI_FAILED"
        conclusion = (
            "无背景流量条件下发送速率高于限速上限，实际接收速率由 HTB ceil 限制在目标附近，"
            "该结果不依赖共享链路拥塞。"
        )
    else:
        minimum = bandwidth * 0.95
        kpi_passed = bool(
            traffic.get("background_started")
            and traffic.get("congestion_created")
            and throughput >= minimum
            and traffic.get("same_bottleneck_binding")
        )
        threshold = {"operator": ">=", "minimum_mbps": minimum}
        verification_state = "MIN_BANDWIDTH_KPI_VERIFIED" if kpi_passed else "KPI_FAILED"
        conclusion = (
            "目标流与背景流经过同一共享出口；最低带宽按 rate 保障，空闲容量可借用至 ceil。"
        )

    flow_ok = before_classifier.get("flow_state") == "ADDED" and after_classifier.get("flow_state") == "ADDED"
    tc_ok = bool(switch_qos.get("config_applied") and traffic.get("tc_class_counter_increased"))
    all_verified = bool(flow_ok and tc_ok and counter["counter_hit"] and kpi_passed)
    statuses = [
        "P4_CLASSIFIER_VERIFIED" if flow_ok else "P4_CLASSIFIER_NOT_FOUND",
        counter["verification_state"],
        "SWITCH_EGRESS_QOS_VERIFIED" if tc_ok else "SWITCH_EGRESS_QOS_FAILED",
        verification_state,
    ]
    return {
        "status": "ok" if all_verified else "failed",
        "scenario": scenario,
        "qos_mode": mode,
        "verification_state": verification_state if all_verified else "KPI_FAILED",
        "verification_states": statuses,
        "all_evidence_consistent": all_verified,
        "p4_classifier": after_classifier,
        "p4_counter": counter,
        "switch_egress_qos": switch_qos,
        "active_kpi": {**traffic, "threshold": threshold, "passed": kpi_passed},
        "conclusion": conclusion,
        "tested_at": datetime.now(timezone.utc).isoformat(),
    }


async def cleanup_qos_demo_processes() -> dict[str, Any]:
    from api._probe_session import get_probe_session

    probe = await get_probe_session()
    result = await probe.post("/probe/qos-cleanup", json_data={})
    return result or {"status": "error", "error": "empty cleanup response"}


async def rollback_qos_policy(
    old_policy: YANGPolicy | dict[str, Any] | None,
    new_policy: YANGPolicy | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Restore the prior class, or release a failed new class."""
    if old_policy is not None:
        return await apply_qos_policy(old_policy, operation="APPLY")
    if new_policy is None:
        return {"status": "skipped", "reason": "nothing to rollback"}
    return await clear_qos_policies(yang_policy=new_policy)


async def clear_qos_policies(
    policy_id: str | None = None,
    *,
    yang_policy: YANGPolicy | dict[str, Any] | None = None,
    scope_key: str | None = None,
    source_ip: str | None = None,
) -> dict[str, Any]:
    """Release only the requested TC class/filter; never delete unrelated flows."""
    if yang_policy is not None:
        policy_model = _as_policy(yang_policy)
        scope_key = traffic_scope_key(policy_model.network_policy).key
        source_ip = policy_model.network_policy.match.source_ip
        policy_id = policy_model.network_policy.policy_id
    if not scope_key:
        return {"status": "error", "error": "scope_key is required", "policy_id": policy_id}
    authorization = None
    if os.getenv("IDN_NETWORK_WRITE_FENCING_REQUIRED", "false").lower() == "true":
        authorization = require_network_write(operation="QOS_CLEAR")
        if not authorization["authorized"]:
            return {"status": "error", "reason_code": authorization["reason"],
                    "authorization": authorization, "write_executed": False}

    from api._probe_session import get_probe_session

    probe = await get_probe_session()
    allocator = QoSResourceAllocator(get_store())
    binding = allocator.get(scope_key)
    result = await probe.post(
        "/policy/clear",
        json_data={
            "scope_key": scope_key, "source_ip": source_ip,
            "class_id": binding.class_id if binding else None,
            "filter_id": binding.filter_id if binding else None,
        },
    )
    if authorization is not None:
        NetworkWriteFencing(get_store()).record_execution(
            authorization, write_executed=bool(result and result.get("status") in {"ok", "not_found"})
        )
    if result and result.get("status") in {"ok", "not_found"}:
        allocator.release(scope_key)
    return {**(result or {}), "policy_id": policy_id, "scope_key": scope_key}

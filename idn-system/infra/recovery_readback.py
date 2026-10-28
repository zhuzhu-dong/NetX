"""Read-only, structured recovery evidence for P4, ingress and egress QoS.

The services here deliberately compose the production P4Runtime and Mininet
adapters; they never issue a P4Runtime WriteRequest or a TC mutating command.
They are also useful outside startup recovery for the isolated PREFLIGHT phase.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from ipaddress import ip_network
from pathlib import Path
from typing import Any, Awaitable, Callable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def checksum(value: Any) -> str:
    item = dict(value) if isinstance(value, dict) else value
    if isinstance(item, dict):
        item.pop("checksum", None)
    return hashlib.sha256(canonical_json(item).encode("utf-8")).hexdigest()


def normalized_prefix(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(ip_network(str(value), strict=False))


def normalized_protocol(value: Any) -> int | None:
    if value in (None, ""):
        return None
    names = {"icmp": 1, "tcp": 6, "udp": 17}
    text = str(getattr(value, "value", value)).lower().strip()
    return names.get(text, int(text, 0) if text not in names else None)


def normalized_mbps(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip().lower().replace(" ", "")
    units = (("gbit", 1000.0), ("gbps", 1000.0), ("mbit", 1.0), ("mbps", 1.0), ("kbit", 0.001), ("kbps", 0.001), ("bit", 0.000001), ("bps", 0.000001))
    for unit, multiplier in units:
        if raw.endswith(unit):
            return float(raw[: -len(unit)]) * multiplier
    return float(raw)


def normalized_classid(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().lower().removeprefix("0x")
    if ":" not in text:
        return text
    major, minor = text.split(":", 1)
    return f"{int(major, 16):x}:{int(minor, 16):x}"


def _error_for(required: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    return [name for name, expected in required.items() if expected in (None, "") or actual.get(name) in (None, "")]


class P4RecoveryReadbackService:
    """Resolve current P4Info IDs and read the exact live table/counter key."""

    def __init__(self, reader: Callable[[Any], Awaitable[dict[str, Any]]] | None = None, registry: Any | None = None):
        self._reader = reader
        self._registry = registry

    async def collect(
        self, *, recovery_id: str, verification_phase: str, execution_generation: str,
        runtime_instance_epoch: str, profile: Any, expected_p4_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        from api.p4_evidence import read_p4_snapshot
        if self._registry is None:
            from mcp_servers.p4info_registry import p4info_registry
            registry = p4info_registry
        else:
            registry = self._registry

        reader = self._reader or read_p4_snapshot
        selector = {
            "source_prefix": normalized_prefix(profile.src_ip),
            "destination_prefix": normalized_prefix(profile.dst_ip),
            "ip_protocol": normalized_protocol(profile.protocol),
            "destination_port": int(profile.dst_port) if profile.dst_port is not None else None,
        }
        table_name = str((profile.p4_counter_binding or {}).get("table") or "")
        action_name = "MyIngress.set_dscp"
        counter_name = str((profile.p4_counter_binding or {}).get("counter") or "")
        raw = await reader(profile)
        table_id = registry.table_id(table_name) if table_name in registry.table_names() else None
        action_id = registry.action_id(action_name) if action_name in registry.action_names() else None
        counter_id = registry.direct_counter_id(counter_name)
        expected = {
            "source_prefix": selector["source_prefix"], "destination_prefix": selector["destination_prefix"],
            "ip_protocol": selector["ip_protocol"], "destination_port": selector["destination_port"],
            "dscp": int(profile.dscp) if profile.dscp is not None else None,
            "table_id": table_id, "action_id": action_id, "direct_counter_id": counter_id,
        }
        errors = _error_for(expected, expected)
        raw_status = str(raw.get("status") or "")
        status = "P4_RECOVERY_READBACK_INCOMPLETE"
        if raw_status in {"DEVICE_UNAVAILABLE", "P4RUNTIME_ERROR", "READ_FAILED"}:
            status = "P4RUNTIME_UNAVAILABLE"
            errors.append(raw.get("reason_code") or raw_status)
        elif raw_status in {"NO_MATCHING_ENTRY"}:
            status = "P4_ENTRY_NOT_FOUND"
        elif raw_status == "SELECTOR_MISMATCH":
            status = "P4_SELECTOR_MISMATCH"
        elif raw_status == "COUNTER_NOT_BOUND":
            status = "P4_DIRECT_COUNTER_UNREADABLE"
        elif raw_status == "VERIFIED":
            entry = ((raw.get("read_response") or {}).get("matching_table_entries") or [{}])[0]
            actual_action = entry.get("action_name")
            params = entry.get("action_params") or []
            dscp_param = registry.param_id(action_name, "dscp_value")
            observed_dscp = next((int(str(item.get("value") or "0"), 16) for item in params if int(item.get("param_id") or -1) == int(dscp_param or -2)), None)
            if actual_action != action_name or int(entry.get("action_id") or -1) != int(action_id or -2):
                status = "P4_ACTION_MISMATCH"
                errors.append("ACTION_MISMATCH")
            elif observed_dscp != expected["dscp"]:
                status = "P4_DSCP_MISMATCH"
                errors.append("DSCP_MISMATCH")
            elif errors:
                status = "P4_RECOVERY_READBACK_INCOMPLETE"
            else:
                status = "P4_RECOVERY_READBACK_VERIFIED"
        action_parameters = ((raw.get("read_response") or {}).get("matching_table_entries") or [{}])[0].get("action_params") or []
        p4info_candidates = (
            Path(__file__).resolve().parents[2] / "p4" / "build" / "idn_basic.p4info.txt",
            Path(__file__).resolve().parents[1] / "p4" / "build" / "idn_basic.p4info.txt",
        )
        p4info_path = next((path for path in p4info_candidates if path.exists()), None)
        p4info_fingerprint = hashlib.sha256(p4info_path.read_bytes()).hexdigest() if p4info_path else "UNAVAILABLE"
        payload = {
            "recovery_id": recovery_id, "verification_phase": verification_phase,
            "device_id": raw.get("device_id") or (profile.p4_counter_binding or {}).get("device_id"),
            "grpc_endpoint": raw.get("grpc_endpoint"), "table_name": table_name, "table_id": table_id,
            "canonical_entry_key": canonical_json(selector), "selector": selector, "selector_hash": checksum(selector),
            **selector, "action_name": action_name, "action_id": action_id,
            "action_parameters": action_parameters, "dscp": expected["dscp"],
            "direct_counter_name": counter_name, "direct_counter_id": counter_id,
            "counter_packets": int(raw.get("packets") or 0), "counter_bytes": int(raw.get("bytes") or 0),
            "p4_program_fingerprint": expected_p4_fingerprint or p4info_fingerprint,
            "p4info_fingerprint": p4info_fingerprint, "device_epoch": raw.get("read_at"),
            "execution_generation": execution_generation, "readback_generation": execution_generation,
            "runtime_instance_epoch": runtime_instance_epoch, "observed_at": raw.get("read_at") or utc_now(),
            "status": status, "validation_errors": sorted(set(str(item) for item in errors)),
            "raw_readback_reference": {"read_request": raw.get("read_request"), "read_response": raw.get("read_response")},
        }
        payload["checksum"] = checksum(payload)
        return payload


class IngressRecoveryReadbackService:
    async def collect(self, *, recovery_id: str, verification_phase: str, desired: dict[str, Any], runtime_instance_epoch: str) -> dict[str, Any]:
        from api._probe_session import get_probe_session
        observed = await (await get_probe_session()).get("/policy/ingress-admission", {"admission_plan_id": desired["admission_plan_id"]}) or {}
        rule = desired["desired"]
        scope = rule.get("protected_selector") or {}
        actual_scope = (observed.get("protected_scopes") or [{}])[0]
        errors = []
        if observed.get("status") != "VERIFIED": errors.append("INGRESS_STATUS_NOT_VERIFIED")
        for name, expected in (("traffic_scope_key", desired.get("traffic_scope_key")), ("generation", desired.get("generation_id"))):
            actual = observed.get(name) if name != "generation" else observed.get("desired_generation")
            if actual not in (None, expected): errors.append(f"{name.upper()}_MISMATCH")
        checks = {"floor": (observed.get("implementation_reserved_mbps"), rule.get("guaranteed_floor_mbps")), "capacity": (observed.get("schedulable_capacity_mbps"), rule.get("schedulable_capacity_mbps")), "idle": (observed.get("idle_be_runtime_allocation_mbps", 0), 0)}
        errors.extend(f"{name.upper()}_MISMATCH" for name, (actual, expected) in checks.items() if normalized_mbps(actual) != normalized_mbps(expected))
        payload = {"recovery_id": recovery_id, "verification_phase": verification_phase, "runtime_instance_epoch": runtime_instance_epoch,
            "desired_generation": desired["generation_id"], "readback_generation": desired["generation_id"], "traffic_scope_key": desired["traffic_scope_key"],
            "admission_plan_id": desired["admission_plan_id"], "attachment": observed.get("attachments") or [], "selector": scope,
            "rate_mbps": observed.get("implementation_reserved_mbps"), "ceil_mbps": actual_scope.get("ingress_admission_ceiling_mbps"), "capacity_mbps": observed.get("schedulable_capacity_mbps"),
            "idle_allocation_mbps": observed.get("idle_be_runtime_allocation_mbps", 0), "status": "INGRESS_RECOVERY_READBACK_VERIFIED" if not errors else "INGRESS_RECOVERY_READBACK_INCOMPLETE",
            "validation_errors": errors, "observed_at": utc_now(), "raw_readback_reference": observed}
        payload["checksum"] = checksum(payload)
        return payload


class EgressQosRecoveryReadbackService:
    """Use the existing QoS VERIFY call, which runs tc qdisc/class/filter reads."""

    async def collect(self, *, recovery_id: str, verification_phase: str, desired: dict[str, Any], runtime_instance_epoch: str, binding: Any, profile: Any, resolution: Any = None) -> dict[str, Any]:
        from api._probe_session import get_probe_session
        # Desired selector/rates must be the resolver result.  The binding
        # runtime is observed history and cannot be used to validate itself.
        qos = {}
        if resolution is None:
            resolved = {}
        elif hasattr(resolution, "model_dump"):
            resolved = resolution.model_dump(mode="json")
        elif isinstance(resolution, dict):
            resolved = dict(resolution)
        else:
            resolved = vars(resolution)
        selector = dict(resolved.get("selector") or {})
        source = normalized_prefix(selector.get("source_prefix") or profile.src_ip)
        destination = normalized_prefix(selector.get("destination_prefix") or profile.dst_ip)
        protocol = normalized_protocol(selector.get("ip_protocol") if selector.get("ip_protocol") is not None else profile.protocol)
        destination_port = int(selector.get("destination_port") if selector.get("destination_port") is not None else profile.dst_port)
        dscp = int(selector.get("dscp") if selector.get("dscp") is not None else profile.dscp)
        # POST /policy/qos VERIFY can reconcile a default class on success.
        # Recovery preflight instead calls the dedicated GET primitive, which
        # executes only tc qdisc/class/filter show commands.
        observed = await (await get_probe_session()).get("/policy/qos-readback", {"scope_key": binding.traffic_scope_key}) or {}
        raw = observed.get("raw") or {}
        errors: list[str] = []
        if observed.get("status") not in {"ok", "VERIFIED"}: errors.append("TC_COMMAND_FAILED")
        expected = {
            "root_rate_mbps": resolved.get("root_rate_mbps"), "root_ceil_mbps": resolved.get("root_ceil_mbps"),
            "rate_mbps": resolved.get("target_rate_mbps"), "ceil_mbps": resolved.get("target_ceil_mbps"),
            "default_rate_mbps": resolved.get("default_rate_mbps"), "default_ceil_mbps": resolved.get("default_ceil_mbps"),
        }
        for name, value in expected.items():
            if normalized_mbps(observed.get(name)) != float(value): errors.append(name.upper() + "_MISMATCH")
        actual_key = observed.get("classification_key") or {}
        actual_source, actual_destination = normalized_prefix(actual_key.get("source_ip")), normalized_prefix(actual_key.get("destination_ip"))
        if (actual_source, actual_destination, normalized_protocol(actual_key.get("protocol")), actual_key.get("destination_port"), actual_key.get("dscp")) != (source, destination, protocol, destination_port, dscp): errors.append("FILTER_SELECTOR_MISMATCH")
        if normalized_classid(observed.get("class_id")) != normalized_classid(binding.class_id): errors.append("TARGET_CLASS_MISMATCH")
        if normalized_classid(observed.get("default_class_id")) != "1:fffe": errors.append("DEFAULT_CLASS_MISMATCH")
        if not observed.get("config_applied"): errors.append("TC_CONFIG_NOT_APPLIED")
        status = "EGRESS_RECOVERY_READBACK_VERIFIED"
        if errors:
            status = "EGRESS_TC_COMMAND_FAILED" if "TC_COMMAND_FAILED" in errors else "EGRESS_RECOVERY_READBACK_INCOMPLETE" if any("MISMATCH" not in item for item in errors) else "EGRESS_FILTER_MISMATCH" if "FILTER_SELECTOR_MISMATCH" in errors else "EGRESS_TARGET_CLASS_MISMATCH"
        target_classid = normalized_classid(observed.get("class_id") or binding.class_id)
        egress = {"recovery_id": recovery_id, "verification_phase": verification_phase, "device_id": resolved.get("egress_device") or getattr(binding, "device_id", None),
            "interface": resolved.get("egress_interface") or getattr(binding, "interface_id", None), "interface_index": None,
            "root_qdisc_kind": "htb" if observed.get("root_qdisc_present") else None, "root_handle": "1:", "root_rate_mbps": observed.get("root_rate_mbps"), "root_ceil_mbps": observed.get("root_ceil_mbps"),
            "target_classid": target_classid, "target_rate_mbps": observed.get("rate_mbps"), "target_ceil_mbps": observed.get("ceil_mbps"),
            "default_classid": normalized_classid(observed.get("default_class_id")), "default_rate_mbps": observed.get("default_rate_mbps"), "default_ceil_mbps": observed.get("default_ceil_mbps"),
            "target_filter_kind": observed.get("filter_kind"), "target_filter_pref": observed.get("filter_priority"), "target_filter_handle": observed.get("filter_id"),
            "source_prefix": source, "destination_prefix": destination, "ip_protocol": protocol, "destination_port": destination_port, "dscp": dscp,
            "target_filter_classid": target_classid, "owner": binding.policy_id, "traffic_scope_key": binding.traffic_scope_key,
            "execution_generation": desired["generation_id"], "readback_generation": desired["generation_id"], "runtime_instance_epoch": runtime_instance_epoch,
            "observed_at": utc_now(), "status": status, "validation_errors": sorted(set(errors)),
            "raw_qdisc_reference": raw.get("qdisc"), "raw_class_reference": raw.get("class"), "raw_filter_reference": raw.get("filter")}
        egress["checksum"] = checksum(egress)
        return egress


class RestartRecoveryReadbackBundleService:
    def build(self, *, recovery_id: str, verification_phase: str, runtime_instance_epoch: str, environment_epoch: str, desired: dict[str, Any], p4: dict[str, Any] | None, ingress: dict[str, Any] | None, egress: dict[str, Any] | None) -> dict[str, Any]:
        items = {"p4": p4, "ingress": ingress, "egress": egress}
        errors = [f"{name.upper()}_MISSING" for name, item in items.items() if not item]
        if not errors:
            for name, item in items.items():
                if item.get("recovery_id") != recovery_id: errors.append(f"{name.upper()}_RECOVERY_ID_MISMATCH")
                if item.get("runtime_instance_epoch") != runtime_instance_epoch: errors.append(f"{name.upper()}_RUNTIME_EPOCH_MISMATCH")
                if item.get("execution_generation") != desired["generation_id"] and item.get("desired_generation") != desired["generation_id"]: errors.append(f"{name.upper()}_GENERATION_MISMATCH")
                if not item.get("checksum") or checksum(item) != item.get("checksum"): errors.append(f"{name.upper()}_CHECKSUM_INVALID")
            if ingress and ingress.get("traffic_scope_key") != desired["traffic_scope_key"]: errors.append("INGRESS_SCOPE_MISMATCH")
            if egress and egress.get("traffic_scope_key") != desired["traffic_scope_key"]: errors.append("EGRESS_SCOPE_MISMATCH")
        passed = not errors and p4.get("status") == "P4_RECOVERY_READBACK_VERIFIED" and ingress.get("status") == "INGRESS_RECOVERY_READBACK_VERIFIED" and egress.get("status") == "EGRESS_RECOVERY_READBACK_VERIFIED"
        bundle = {"recovery_id": recovery_id, "verification_phase": verification_phase, "runtime_instance_epoch": runtime_instance_epoch, "environment_epoch": environment_epoch,
            "desired_generation": desired["generation_id"], "policy_lineage": {"intent_id": desired["intent_id"], "desired_policy_id": desired["desired_policy_id"], "owner_policy_id": desired["executed_resource_owner_policy_id"], "resource_binding_id": desired["resource_binding_id"], "traffic_scope_key": desired["traffic_scope_key"]},
            "p4_readback": p4, "ingress_readback": ingress, "egress_readback": egress, "bundle_status": "VERIFIED" if passed else "INCOMPLETE", "validation_errors": sorted(set(errors)), "observed_at": utc_now()}
        bundle["checksum"] = checksum(bundle)
        return bundle

"""Shared, configuration-only reconciliation semantics.

Both recovery readback and reconciliation use this module.  Counters, packet
statistics, timestamps, raw TC text and protobuf/object identity are evidence,
not desired configuration, so they cannot turn an equivalent configuration into
a write.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from infra.recovery_readback import normalized_classid, normalized_mbps, normalized_prefix, normalized_protocol


VOLATILE_P4_FIELDS = ("counter_packets", "counter_bytes", "observed_at", "device_epoch", "read_at", "trace_id", "checksum", "raw_readback_reference")
VOLATILE_EGRESS_FIELDS = ("observed_at", "checksum", "raw_qdisc_reference", "raw_class_reference", "raw_filter_reference", "stats", "filter_stats", "root_stats", "default_stats")


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def semantic_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def canonicalize_p4_desired(*, device_id: Any, table: Any, selector: dict[str, Any], priority: Any, action: Any, action_params: Any, dscp: Any, counter_binding: Any, owner: Any, generation: Any, scope: Any) -> dict[str, Any]:
    return {
        "device_id": str(device_id or ""), "table": str(table or ""),
        "selector": {"source_prefix": normalized_prefix(selector.get("source_prefix") or selector.get("source_ip")), "destination_prefix": normalized_prefix(selector.get("destination_prefix") or selector.get("destination_ip")), "ip_protocol": normalized_protocol(selector.get("ip_protocol") or selector.get("protocol")), "destination_port": int(selector["destination_port"]) if selector.get("destination_port") not in (None, "") else None},
        "priority": int(priority) if priority is not None else None, "action": str(action or ""),
        "action_params": sorted([{str(k): str(v) for k, v in item.items()} for item in (action_params or [])], key=_canon),
        "dscp": int(dscp) if dscp is not None else None, "direct_counter_binding": str(counter_binding or ""),
        "owner": str(owner or ""), "generation": str(generation or ""), "scope": str(scope or ""),
    }


def canonicalize_p4_observed(payload: dict[str, Any], *, owner: Any, generation: Any, scope: Any) -> dict[str, Any]:
    return canonicalize_p4_desired(device_id=payload.get("device_id"), table=payload.get("table_name") or payload.get("table"), selector=payload.get("selector") or {}, priority=payload.get("priority", 101), action=payload.get("action_name") or payload.get("action"), action_params=payload.get("action_parameters") or payload.get("action_params"), dscp=payload.get("dscp"), counter_binding=payload.get("direct_counter_name") or payload.get("direct_counter_id"), owner=owner, generation=generation, scope=scope)


def _tos(dscp: Any) -> int | None:
    if dscp in (None, ""): return None
    raw = str(dscp).split("/")[0]
    try: value = int(raw, 0)
    except ValueError: return int(dscp)
    return value >> 2 if value > 63 else value


def canonicalize_egress_desired(*, root_rate: Any, root_ceil: Any, target_classid: Any, target_rate: Any, target_ceil: Any, default_classid: Any, default_rate: Any, default_ceil: Any, filter_kind: Any, filter_pref: Any, selector: dict[str, Any], dscp: Any, owner: Any, scope: Any, generation: Any) -> dict[str, Any]:
    return {"root": {"kind": "htb", "handle": "1:0", "rate_mbps": normalized_mbps(root_rate), "ceil_mbps": normalized_mbps(root_ceil)}, "target": {"classid": normalized_classid(target_classid), "rate_mbps": normalized_mbps(target_rate), "ceil_mbps": normalized_mbps(target_ceil)}, "default": {"classid": normalized_classid(default_classid), "rate_mbps": normalized_mbps(default_rate), "ceil_mbps": normalized_mbps(default_ceil)}, "filter": {"kind": str(filter_kind or "flower_dscp_plus_five_tuple"), "pref": int(filter_pref) if filter_pref is not None else None, "selector": {"source_prefix": normalized_prefix(selector.get("source_prefix") or selector.get("source_ip")), "destination_prefix": normalized_prefix(selector.get("destination_prefix") or selector.get("destination_ip")), "ip_protocol": normalized_protocol(selector.get("ip_protocol") or selector.get("protocol")), "destination_port": int(selector["destination_port"]) if selector.get("destination_port") not in (None, "") else None, "dscp": _tos(dscp)}}, "owner": str(owner or ""), "scope": str(scope or ""), "generation": str(generation or "")}


def canonicalize_egress_observed(payload: dict[str, Any], *, owner: Any, scope: Any, generation: Any) -> dict[str, Any]:
    selector = payload.get("classification_key") or payload.get("selector") or {}
    return canonicalize_egress_desired(root_rate=payload.get("root_rate_mbps"), root_ceil=payload.get("root_ceil_mbps"), target_classid=payload.get("class_id") or payload.get("target_classid"), target_rate=payload.get("rate_mbps") or payload.get("target_rate_mbps"), target_ceil=payload.get("ceil_mbps") or payload.get("target_ceil_mbps"), default_classid=payload.get("default_class_id") or payload.get("default_classid"), default_rate=payload.get("default_rate_mbps"), default_ceil=payload.get("default_ceil_mbps"), filter_kind=payload.get("filter_kind") or payload.get("target_filter_kind"), filter_pref=payload.get("filter_priority") or payload.get("target_filter_pref"), selector=selector, dscp=selector.get("dscp") if isinstance(selector, dict) else None, owner=owner, scope=scope, generation=generation)


def reconcile_semantic_diff(*, reconcile_id: str, recovery_id: str | None, resource_kind: str, desired: dict[str, Any], observed: dict[str, Any], ignored_volatile_fields: tuple[str, ...], proposed_operation: str = "APPLY") -> dict[str, Any]:
    fields = sorted(key for key in set(desired) | set(observed) if desired.get(key) != observed.get(key))
    equal = not fields
    payload = {"reconcile_id": reconcile_id, "recovery_id": recovery_id, "resource_kind": resource_kind, "desired_generation": desired.get("generation"), "desired_owner": desired.get("owner"), "desired_scope": desired.get("scope"), "observed_owner": observed.get("owner"), "observed_scope": observed.get("scope"), "desired_canonical_payload": desired, "observed_canonical_payload": observed, "desired_semantic_hash": semantic_hash(desired), "observed_semantic_hash": semantic_hash(observed), "differing_fields": fields, "ignored_volatile_fields": list(ignored_volatile_fields), "normalization_rules": ["sorted canonical JSON", "rate units => Mbps", "protocol => number", "prefix => CIDR", "DSCP/TOS => DSCP", "class/handle normalized", "volatile statistics excluded"], "proposed_operation": "NONE" if equal else proposed_operation, "write_required": not equal, "reason_codes": [] if equal else [f"SEMANTIC_{resource_kind}_MISMATCH"], "observed_at": observed.get("observed_at")}
    payload["checksum"] = semantic_hash({k: v for k, v in payload.items() if k != "checksum"})
    return payload

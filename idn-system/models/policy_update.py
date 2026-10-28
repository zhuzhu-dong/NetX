"""Deterministic identity and update semantics for installed policies.

``TrafficScopeKey`` deliberately excludes action parameters such as bandwidth,
DSCP and burst size.  Those values describe the desired state *of* a flow, not
the identity of the flow.  Consequently a second QoS intent for the same exact
scope is an idempotent reuse or an in-place update, rather than a conflicting
new policy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from ipaddress import ip_network
from typing import Any


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _prefix(value: Any) -> str | None:
    if value in (None, "", "null"):
        return None
    try:
        return str(ip_network(str(value), strict=False))
    except ValueError:
        return str(value).strip().lower()


@dataclass(frozen=True)
class TrafficScopeKey:
    """Stable identity for an exact traffic selector.

    Tenant/VRF/direction are included for future multi-tenant deployments and
    default to the current single-tenant ingress behaviour.
    """

    source_ip: str | None
    destination_ip: str | None
    protocol: str
    source_port: int | None
    destination_port: int | None
    vlan_id: int | None
    action_family: str
    tenant: str = "default"
    vrf: str = "default"
    direction: str = "ingress"
    # Service identity is retained for audit and validation routing.  The hash
    # remains selector-based so renaming a profile cannot duplicate an
    # already-active five-tuple policy.
    service_id: str | None = None

    @classmethod
    def from_match(
        cls,
        match: Any,
        *,
        tenant: str = "default",
        vrf: str = "default",
        direction: str = "ingress",
        action_family: str = "UNSPECIFIED",
        service_id: str | None = None,
    ) -> "TrafficScopeKey":
        data = match.model_dump() if hasattr(match, "model_dump") else dict(match or {})
        protocol = str(_value(data.get("protocol") or "ANY")).upper()
        return cls(
            source_ip=_prefix(data.get("source_ip")),
            destination_ip=_prefix(data.get("destination_ip")),
            protocol=protocol,
            source_port=_optional_int(data.get("source_port")),
            destination_port=_optional_int(data.get("destination_port")),
            vlan_id=_optional_int(data.get("vlan_id")),
            action_family=str(action_family or "UNSPECIFIED").strip().upper(),
            tenant=str(tenant or "default").strip().lower(),
            vrf=str(vrf or "default").strip().lower(),
            direction=str(direction or "ingress").strip().lower(),
            service_id=str(service_id).strip() if service_id else None,
        )

    @property
    def canonical(self) -> str:
        data = asdict(self)
        data.pop("service_id", None)
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def key(self) -> str:
        return "TSK2-" + hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()[:24]


def _optional_int(value: Any) -> int | None:
    if value in (None, "", "null"):
        return None
    return int(value)


def traffic_scope_key(policy: Any) -> TrafficScopeKey:
    service_id = getattr(policy, "service_id", None)
    if service_id is None and isinstance(policy, dict):
        service_id = policy.get("service_id")
    network_policy = getattr(policy, "network_policy", policy)
    match = getattr(network_policy, "match", None)
    if match is None and isinstance(network_policy, dict):
        match = network_policy.get("match") or {}
    metadata = getattr(network_policy, "metadata", None)
    metadata_data = metadata.model_dump() if hasattr(metadata, "model_dump") else dict(metadata or {})
    action_type = action_type_value(network_policy)
    action_family = {
        "SET_PRIORITY": "QOS",
        # QoS mode is desired state, not flow identity.  A MIN_BANDWIDTH to
        # RATE_LIMIT transition for the same five-tuple updates the existing
        # classifier/class instead of creating a second policy scope.
        "RATE_LIMIT": "QOS",
        "DROP": "DROP",
        "ACL_BLOCK": "DROP",
        "FORWARD": "ALLOW",
        "MIRROR": "MIRROR",
    }.get(action_type, action_type or "UNSPECIFIED")
    return TrafficScopeKey.from_match(
        match,
        tenant=metadata_data.get("tenant", "default"),
        vrf=metadata_data.get("vrf", "default"),
        direction=metadata_data.get("direction", "ingress"),
        action_family=action_family,
        service_id=service_id,
    )


def qos_values(policy: Any) -> dict[str, Any]:
    network_policy = getattr(policy, "network_policy", policy)
    action = getattr(network_policy, "action", None)
    if action is None and isinstance(network_policy, dict):
        action = network_policy.get("action") or {}
    qos = getattr(action, "qos_params", None)
    if qos is None and isinstance(action, dict):
        qos = action.get("qos_params") or {}
    data = qos.model_dump() if hasattr(qos, "model_dump") else dict(qos or {})
    values = {
        name: _optional_int(data.get(name))
        for name in ("dscp_value", "bandwidth", "burst_size")
    }
    values["qos_mode"] = effective_qos_mode(policy)
    return values


def effective_qos_mode(policy: Any) -> str:
    """Return explicit mode, with a compatibility inference for old rows."""
    network_policy = getattr(policy, "network_policy", policy)
    action = getattr(network_policy, "action", None)
    if action is None and isinstance(network_policy, dict):
        action = network_policy.get("action") or {}
    qos = getattr(action, "qos_params", None)
    if qos is None and isinstance(action, dict):
        qos = action.get("qos_params") or {}
    data = qos.model_dump() if hasattr(qos, "model_dump") else dict(qos or {})
    explicit = _value(data.get("qos_mode"))
    if explicit:
        return str(explicit).upper()
    return "RATE_LIMIT" if action_type_value(network_policy) == "RATE_LIMIT" else "MIN_BANDWIDTH"


def action_type_value(policy: Any) -> str:
    network_policy = getattr(policy, "network_policy", policy)
    action = getattr(network_policy, "action", None)
    if action is None and isinstance(network_policy, dict):
        action = network_policy.get("action") or {}
    value = getattr(action, "action_type", None)
    if value is None and isinstance(action, dict):
        value = action.get("action_type")
    return str(_value(value) or "").upper()


def qos_change(old_policy: Any, new_policy: Any) -> dict[str, Any]:
    old = qos_values(old_policy)
    new = qos_values(new_policy)
    # ``burst_size`` is an optional tuning hint, not a classifier or SLA
    # value.  Older persisted policies frequently contain a historical
    # default while a newly parsed natural-language policy leaves it unset.
    # Treating ``128 -> None`` as a semantic update turns an idempotent
    # request into an unnecessary southbound write.  A user-specified value
    # still remains meaningful when both sides explicitly provide one.
    changed = [
        name for name in old
        if old[name] != new[name]
        and not (name == "burst_size" and (old[name] is None or new[name] is None))
    ]
    old_bw, new_bw = old.get("bandwidth"), new.get("bandwidth")
    if old_bw is not None and new_bw is not None and new_bw != old_bw:
        direction = "UPGRADE" if new_bw > old_bw else "DOWNGRADE"
    else:
        direction = "PARAMETER_CHANGE" if changed else "UNCHANGED"
    return {
        "old_qos": old,
        "new_qos": new,
        "changed_fields": changed,
        "old_bandwidth_mbps": old_bw,
        "new_bandwidth_mbps": new_bw,
        "change_direction": direction,
        "mode_change": (
            f"{old.get('qos_mode')} -> {new.get('qos_mode')}"
            if old.get("qos_mode") != new.get("qos_mode") else None
        ),
        "released_bandwidth_mbps": (
            old_bw - new_bw
            if old_bw is not None and new_bw is not None and old_bw > new_bw
            else 0
        ),
    }

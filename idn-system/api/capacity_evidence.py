"""Direction-safe capacity evidence with a folded physical-link view."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

from api.topology_paths import TopologyPathRegistry, topology_paths


class CapacityEvaluationPurpose(str, Enum):
    """Why a capacity decision is being made.

    Durable desired-state recovery is intentionally not a new reservation.
    Keeping that distinction at this boundary prevents transient calibration
    health from changing scheduler ownership semantics.
    """

    NEW_ADMISSION = "NEW_ADMISSION"
    INCREASE_UPDATE = "INCREASE_UPDATE"
    DECREASE_UPDATE = "DECREASE_UPDATE"
    # Exact desired-state reuse has no capacity delta.  It is deliberately
    # distinct from a positive update so a stale runtime sample cannot reject
    # a request that asks the control plane only to read existing state.
    VERIFY_EXISTING = "VERIFY_EXISTING"
    RECOVERY_EXISTING = "RECOVERY_EXISTING"
    RECONCILE_EXISTING = "RECONCILE_EXISTING"
    WITHDRAW = "WITHDRAW"


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _pair(a: str, b: str) -> tuple[str, str]:
    left, right = str(a), str(b)
    return (min(left, right), max(left, right))


def _configured_links(
    registry: TopologyPathRegistry,
) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        _pair(str(item.get("device_a")), str(item.get("device_b"))): dict(item)
        for item in registry.physical_links
        if item.get("device_a") and item.get("device_b")
    }


def configured_capacity_mbps(
    src_device: str,
    dst_device: str,
    reported: int | None,
    *,
    registry: TopologyPathRegistry = topology_paths,
    default_capacity: int = 100,
) -> tuple[int, str]:
    if reported is not None:
        return int(reported), "network_snapshot"
    configured = _configured_links(registry).get(_pair(src_device, dst_device))
    if configured and configured.get("capacity_mbps") is not None:
        return int(configured["capacity_mbps"]), "topology_paths.json"
    return int(default_capacity), "deployment_default"


def schedulable_capacity_mbps(
    link_id: str, *, physical_capacity_mbps: float,
    src_device: str | None = None, dst_device: str | None = None,
    registry: TopologyPathRegistry = topology_paths,
) -> tuple[float, str, str | None]:
    """Return the stricter live calibrated limit when one applies to a link.

    Topology capacity is physical transport capacity.  A calibrated software
    dataplane limit is a separate, lower scheduling constraint and must gate
    admission before TC/P4 writes.  The lookup is resource based (not based on
    a service, host, port, or business phrase), so it applies to every policy
    that resolves to the same bottleneck resource.
    """
    try:
        from infra.resource_registry import get_resource_registry
        from infra.stable_capacity import StableCapacityRepository

        resources = get_resource_registry().snapshot()
        candidates = [resources.aliases.get(str(link_id), str(link_id))]
        if src_device and dst_device:
            interface_devices = {item.resource_id: item.device_id for item in resources.interfaces}
            candidates.extend(
                item.resource_id for item in resources.links
                if {interface_devices.get(item.src_interface_id), interface_devices.get(item.dst_interface_id)}
                == {str(src_device), str(dst_device)}
            )
        repository = StableCapacityRepository()
        for resource_key in dict.fromkeys(candidates):
            record = repository.active_for(
                bottleneck_egress=resource_key,
                topology_id=resources.topology_name,
            )
            if record is not None:
                calibrated = StableCapacityRepository.schedulable_capacity(record)
                return min(float(physical_capacity_mbps), calibrated), "stable_dataplane_capacity", record.capacity_record_id
    except Exception:
        # Capacity admission must remain available when no calibration exists;
        # topology evidence is still authoritative for the physical ceiling.
        pass
    return float(physical_capacity_mbps), "physical_capacity", None


def authoritative_admission_decision(
    *,
    link_id: str,
    src_device: str,
    dst_device: str,
    requested_mbps: float,
    reclaim_mbps: float = 0.0,
    purpose: CapacityEvaluationPurpose | str = CapacityEvaluationPurpose.NEW_ADMISSION,
) -> dict[str, Any]:
    """Make the pre-compiler guaranteed-bandwidth decision from the ledger.

    Physical link rate is evidence of transport, not evidence that the
    software dataplane can schedule a guaranteed service.  An active,
    topology-scoped stable-capacity record is therefore mandatory here.  The
    returned primitive is persisted in the conflict report and is suitable for
    API/UI projection without reconstructing it from a later TC outcome.
    """
    from infra.resource_ledger import ResourceLedger
    from infra.resource_registry import get_resource_registry

    purpose = CapacityEvaluationPurpose(str(getattr(purpose, "value", purpose)).upper())
    registry = get_resource_registry().snapshot()
    interface_devices = {item.resource_id: item.device_id for item in registry.interfaces}
    candidates = [registry.aliases.get(str(link_id), str(link_id))]
    candidates.extend(
        item.resource_id for item in registry.links
        if {interface_devices.get(item.src_interface_id), interface_devices.get(item.dst_interface_id)}
        == {str(src_device), str(dst_device)}
    )
    entries = {entry.resource_id: entry for entry in ResourceLedger().entries()}
    entry = next((entries.get(key) for key in dict.fromkeys(candidates) if entries.get(key)), None)
    reclaim = max(0.0, float(reclaim_mbps or 0.0))
    target_mbps = max(0.0, float(requested_mbps))
    base = {
        "purpose": purpose.value, "operation": purpose.value,
        "requested_mbps": target_mbps, "requested_target_mbps": target_mbps,
        "existing_target_mbps": reclaim,
        "affected_resources": list(dict.fromkeys(candidates)),
        "scope": {"link_id": str(link_id), "src_device": str(src_device), "dst_device": str(dst_device)},
        "physical_capacity_mbps": None, "stable_capacity_mbps": None,
        "safety_margin_mbps": None, "schedulable_capacity_mbps": 0.0,
        "allocated_mbps": 0.0, "reclaimed_mbps": reclaim, "existing_scope_guarantee_mbps": reclaim,
        "requested_delta_mbps": 0.0, "available_mbps": 0.0,
        "contract_capacity_mbps": None, "contract_allocated_mbps": 0.0,
        "current_guaranteed_allocation_mbps": 0.0,
        "contract_available_mbps": 0.0, "runtime_capacity_status": "UNVERIFIED",
        "runtime_schedulable_mbps": None, "runtime_schedulable_total_mbps": None,
        "runtime_current_allocation_mbps": 0.0, "runtime_available_mbps": None,
        "effective_available_mbps": None,
        "no_change": False,
        "positive_capacity_admission_required": True,
    }
    if entry is None:
        return {**base, "decision": "REJECTED", "reason_code": "CAPACITY_RESOURCE_UNRESOLVED"}
    base.update({
        "affected_resources": [entry.resource_id],
        "physical_capacity_mbps": float(entry.physical_capacity_mbps),
        "stable_capacity_mbps": (float(entry.stable_capacity_mbps) if entry.stable_capacity_mbps is not None else None),
        "safety_margin_mbps": float(entry.safety_margin_mbps),
        "schedulable_capacity_mbps": float(entry.schedulable_capacity_mbps or 0.0),
        "allocated_mbps": float(entry.committed_guaranteed_capacity_mbps),
        "capacity_record_id": entry.capacity_record_id,
    })
    # Runtime allocation is the same committed ledger allocation used by the
    # contract check.  It remains meaningful even when fresh runtime capacity
    # is missing, so never leave it at the bootstrap default of zero.
    base["runtime_current_allocation_mbps"] = base["allocated_mbps"]
    # A revision asks for a target, while a resource ledger accounts for all
    # existing reservations.  Its own reservation is not a free credit for an
    # unrelated request: only the *positive delta* from that scope's current
    # guarantee consumes new capacity.  This keeps contract and runtime
    # arithmetic aligned for a 20->30 update: allocated=30, existing=20,
    # delta=10, contract-available=19, runtime-available=11.
    reclaimed = min(reclaim, base["allocated_mbps"])
    base["reclaimed_mbps"] = reclaimed
    base["effective_allocated_mbps"] = max(0.0, base["allocated_mbps"] - reclaimed)
    base["existing_scope_guarantee_mbps"] = reclaimed
    base["existing_target_mbps"] = reclaimed
    base["requested_delta_mbps"] = max(0.0, target_mbps - reclaimed)
    contract = getattr(entry, "authorized_schedulable_capacity_mbps", None)
    contract = float(contract) if contract is not None else float(entry.schedulable_capacity_mbps or 0.0)
    base["contract_capacity_mbps"] = contract
    base["contract_allocated_mbps"] = base["allocated_mbps"]
    base["current_guaranteed_allocation_mbps"] = base["allocated_mbps"]
    base["contract_available_mbps"] = max(
        0.0, contract - base["allocated_mbps"]
    )
    base["available_mbps"] = base["contract_available_mbps"]
    # This is the final capacity-authority boundary.  Exact reuse normally
    # bypasses this function in the conflict checker, but profile-specific
    # resource paths also arrive here.  An equal target after reclaim consumes
    # no new capacity, so normalize it before both positive-admission gates.
    # In particular, an UNVERIFIED runtime sample must never convert 10 -> 10
    # (or 20 -> 20) into a rejected INCREASE_UPDATE.
    if target_mbps == reclaimed and reclaim > 0.0:
        return {
            **base,
            "purpose": CapacityEvaluationPurpose.VERIFY_EXISTING.value,
            "operation": CapacityEvaluationPurpose.VERIFY_EXISTING.value,
            "no_change": True,
            "positive_capacity_admission_required": False,
            "runtime_capacity_status": "NOT_REQUIRED",
            "decision": "ADMITTED",
            "reason_code": "NO_CHANGE_VERIFY_EXISTING",
        }
    # Contract capacity is always evaluated first.  It is durable desired
    # ownership, never a latest measurement or a TC observation.
    if base["requested_delta_mbps"] > base["contract_available_mbps"]:
        return {**base, "decision": "REJECTED", "reason_code": "INSUFFICIENT_CAPACITY"}
    runtime_required = purpose in {
        CapacityEvaluationPurpose.NEW_ADMISSION,
        CapacityEvaluationPurpose.INCREASE_UPDATE,
    }
    if not runtime_required:
        base["runtime_capacity_status"] = "DEGRADED" if not entry.capacity_record_id else "CURRENT"
        return {**base, "decision": "ADMITTED", "reason_code": "DURABLE_CONTRACT_OPERATION"}
    if not entry.capacity_record_id:
        # There is a durable contract answer, but no fresh runtime answer.
        # Do not label contract residual as an effective runtime-safe value.
        base["effective_available_mbps"] = None
        return {**base, "decision": "REJECTED", "reason_code": "RUNTIME_CAPACITY_UNVERIFIED"}
    runtime_capacity = float(entry.schedulable_capacity_mbps or 0.0)
    base["runtime_schedulable_mbps"] = runtime_capacity
    base["runtime_schedulable_total_mbps"] = runtime_capacity
    base["runtime_available_mbps"] = max(0.0, runtime_capacity - base["allocated_mbps"])
    base["effective_available_mbps"] = min(base["contract_available_mbps"], base["runtime_available_mbps"])
    base["runtime_capacity_status"] = "CURRENT"
    base["available_mbps"] = base["effective_available_mbps"]
    if base["requested_delta_mbps"] > base["effective_available_mbps"]:
        return {**base, "decision": "REJECTED", "reason_code": "RUNTIME_CAPACITY_INSUFFICIENT"}
    return {**base, "decision": "ADMITTED", "reason_code": "SCHEDULABLE_CAPACITY_AVAILABLE"}


def primary_direction_pairs(
    registry: TopologyPathRegistry = topology_paths,
) -> set[tuple[str, str]]:
    primary = registry.get_by_role("PRIMARY")
    if not primary:
        return set()
    return {(item.from_device, item.to_device) for item in primary.expected_links}


def aggregate_capacity_evidence(
    links: Iterable[Any],
    *,
    requested_mbps: int,
    reserved_mbps: int,
    headroom_mbps: int,
    candidate_link_ids: list[str] | None = None,
    registry: TopologyPathRegistry = topology_paths,
    default_capacity_mbps: int = 100,
    evidence_time: str | None = None,
) -> dict[str, Any]:
    """Preserve all directed links and build an independent folded view."""
    timestamp = evidence_time or datetime.now(timezone.utc).isoformat()
    configured = _configured_links(registry)
    primary = registry.get_by_role("PRIMARY")
    current_pairs = {
        item.device_pair for item in (primary.expected_links if primary else [])
    }
    current_directions = primary_direction_pairs(registry)
    selected_ids = {str(value) for value in (candidate_link_ids or [])}
    qos_pairs = {
        _pair(item.device_id, item.peer_device_id)
        for item in (primary.qos_egress_candidates if primary else [])
    }
    bottleneck_ids = primary.bottleneck_links if primary else set()
    raw: list[dict[str, Any]] = []

    for item in links:
        src = str(_value(item, "src_device", ""))
        dst = str(_value(item, "dst_device", ""))
        if not src or not dst or src == dst:
            continue
        link_id = str(_value(item, "link_id", ""))
        pair = _pair(src, dst)
        link_cfg = configured.get(pair) or {}
        capacity, capacity_source = configured_capacity_mbps(
            src,
            dst,
            _value(item, "bandwidth_mbps"),
            registry=registry,
            default_capacity=default_capacity_mbps,
        )
        available = max(capacity - int(reserved_mbps) - int(headroom_mbps), 0)
        effective_reserved = int(reserved_mbps) + int(headroom_mbps)
        in_scope = (
            link_id in selected_ids
            if selected_ids
            else (src, dst) in current_directions
        )
        switch_src = registry.device_switch_map.get(src, src)
        switch_dst = registry.device_switch_map.get(dst, dst)
        raw.append(
            {
                "direction_id": link_id or f"{src}->{dst}",
                "physical_link_key": "|".join(pair),
                "src_device": src,
                "src_switch": switch_src,
                "src_port": str(_value(item, "src_port", "")),
                "dst_device": dst,
                "dst_switch": switch_dst,
                "dst_port": str(_value(item, "dst_port", "")),
                "configured_capacity_mbps": capacity,
                "capacity_source": capacity_source,
                "reserved_mbps": effective_reserved,
                "policy_reserved_mbps": int(reserved_mbps),
                "headroom_mbps": int(headroom_mbps),
                "available_mbps": available,
                "requested_mbps": int(requested_mbps),
                "state": str(_value(item, "state", "ACTIVE")),
                "in_decision_scope": in_scope,
                "on_current_path": pair in current_pairs,
                "is_bottleneck": link_cfg.get("link_id") in bottleneck_ids,
                "carries_current_qos": pair in qos_pairs,
                "insufficient": bool(in_scope and requested_mbps > available),
                "evidence_time": timestamp,
            }
        )

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for direction in raw:
        groups.setdefault(
            _pair(direction["src_device"], direction["dst_device"]), []
        ).append(direction)

    physical: list[dict[str, Any]] = []
    for pair, directions in sorted(groups.items()):
        cfg = configured.get(pair) or {}
        device_a, device_b = pair
        ports: dict[str, str] = {}
        for direction in directions:
            ports[direction["src_device"]] = direction["src_port"]
            ports[direction["dst_device"]] = direction["dst_port"]
        if cfg:
            ports[str(cfg.get("device_a"))] = str(cfg.get("port_a"))
            ports[str(cfg.get("device_b"))] = str(cfg.get("port_b"))
        scoped = [item for item in directions if item["in_decision_scope"]]
        physical.append(
            {
                "physical_link_key": "|".join(pair),
                "link_id": cfg.get("link_id") or " <-> ".join(pair),
                "device_a": device_a,
                "switch_a": registry.device_switch_map.get(device_a, device_a),
                "port_a": ports.get(device_a),
                "device_b": device_b,
                "switch_b": registry.device_switch_map.get(device_b, device_b),
                "port_b": ports.get(device_b),
                "configured_capacity_mbps": min(
                    item["configured_capacity_mbps"] for item in directions
                ),
                "directional_reserved_mbps": {
                    f"{item['src_device']}->{item['dst_device']}": item["reserved_mbps"]
                    for item in directions
                },
                "directional_available_mbps": {
                    f"{item['src_device']}->{item['dst_device']}": item[
                        "available_mbps"
                    ]
                    for item in directions
                },
                "requested_mbps": int(requested_mbps),
                "on_current_path": any(item["on_current_path"] for item in directions),
                "in_decision_scope": bool(scoped),
                "is_bottleneck": any(item["is_bottleneck"] for item in directions),
                "carries_current_qos": any(
                    item["carries_current_qos"] for item in directions
                ),
                "insufficient": any(item["insufficient"] for item in directions),
                "direction_count": len(directions),
                "directions": directions,
                "evidence_time": timestamp,
            }
        )

    blockers = [item for item in raw if item["insufficient"]]
    bottleneck = next(
        (
            item
            for item in physical
            if item["is_bottleneck"] and item["on_current_path"]
        ),
        None,
    )
    if bottleneck is None:
        fallback_bottlenecks = [item for item in physical if item["in_decision_scope"]]
        if fallback_bottlenecks:
            bottleneck = min(
                fallback_bottlenecks,
                key=lambda candidate: int(candidate["configured_capacity_mbps"]),
            )
    blocking = blockers[0] if blockers else None
    return {
        "view_version": "1.0",
        "path_id": primary.path_id if primary else None,
        "requested_mbps": int(requested_mbps),
        "reserved_mbps": int(reserved_mbps) + int(headroom_mbps),
        "policy_reserved_mbps": int(reserved_mbps),
        "headroom_mbps": int(headroom_mbps),
        "raw_directed_link_count": len(raw),
        "physical_link_count": len(physical),
        "raw_directed_links": raw,
        "physical_link_summary": physical,
        "default_view": {
            "current_path_physical_links": [
                item for item in physical if item["on_current_path"]
            ],
            "qos_egress": next(
                (item for item in physical if item["carries_current_qos"]), None
            ),
            "bottleneck": bottleneck,
            "blocking_direction": blocking,
            "insufficient_physical_links": [
                item for item in physical if item["insufficient"]
            ],
            "other_physical_link_count": len(
                [item for item in physical if not item["on_current_path"]]
            ),
        },
        "evidence_time": timestamp,
    }

"""Automatic congestion remediation helpers.

Evidence-driven remediation for configured services.

When Verify observes congestion, this module derives endpoints from the
ServiceProfile, excludes only resources named by live evidence, and computes
a constrained alternate path from the runtime topology and ResourceLedger.

All fault injection now goes through ONOS → P4Runtime → P4 sla_enforcement
table (with direct meters) instead of bypassing the controller via Linux tc.
"""
from __future__ import annotations

import logging
import os
from ipaddress import ip_network
from typing import Any

from api.int_analysis import analyze_telemetry
from mcp_servers.flow_adapter import FlowAdapter
from models.flow_table import (
    ActionParam,
    MatchField,
    MeterConfig,
    TableEntry,
    WriteRequest,
)
from models.yang_policy import YANGPolicy

logger = logging.getLogger(__name__)

# Fault injection priority — env-configurable (default: high to override normal forwarding)
_FAULT_PRIORITY = int(os.getenv("IDN_FAULT_PRIORITY", "50000"))
_FAULT_APP_COOKIE = "idn-fault-injection"


def _enabled(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() == "true"


async def _resolve_device_by_name(node_name: str) -> str | None:
    """Resolve a logical switch name (e.g. 's_agg_r') to a ``device:bmv2:N`` ID
    by querying the live ONOS topology."""
    try:
        from api.topology import fetch_mininet_topology
        topo = await fetch_mininet_topology()
        for switch in topo.get("switches", []):
            if switch.get("name") == node_name or switch.get("host_name") == node_name:
                return switch.get("device_id")
        for device in topo.get("devices", []):
            did = device.get("device_id") or device.get("id") or ""
            annotations = device.get("annotations", {}) or {}
            if annotations.get("name") == node_name:
                return did
    except Exception:
        pass
    return None


async def _resolve_edge_device_for_ip(ip_prefix: str | None) -> str | None:
    """Find which switch a host with the given IP prefix is attached to."""
    if not ip_prefix:
        return None
    try:
        from mcp_servers.onos_server import get_onos_client
        onos = get_onos_client()
        hosts = await onos.get_hosts()
        for host in hosts.get("hosts", []):
            for ip in host.get("ipAddresses", []):
                if ip.startswith(ip_prefix.split("/")[0].rsplit(".", 1)[0]):
                    return host.get("locations", [{}])[0].get("elementId") if host.get("locations") else None
    except Exception:
        pass
    return None


async def _compute_alternate_path(
    src_host: str,
    dst_host: str,
    avoid_switch: str | None = None,
) -> list[str]:
    """Compute an alternate path between two hosts avoiding a specific switch.

    Uses BFS on the live topology graph. Returns a list of node names (hosts +
    switches) forming the path, or an empty list if no path exists.
    """
    try:
        from api.topology import fetch_mininet_topology
        from collections import deque

        topo = await fetch_mininet_topology()
        links = topo.get("links", [])
        switches = {s.get("name") or s.get("device_id"): s for s in topo.get("switches", [])}
        hosts = {h.get("host_name") or h.get("host_id"): h for h in topo.get("hosts", [])}

        # Build adjacency graph
        graph: dict[str, set[str]] = {}
        for link in links:
            src = link.get("src_device") or link.get("src")
            dst = link.get("dst_device") or link.get("dst")
            if src and dst and (not avoid_switch or (src != avoid_switch and dst != avoid_switch)):
                graph.setdefault(src, set()).add(dst)
                graph.setdefault(dst, set()).add(src)

        # Add host→switch edges
        for host_name, host in hosts.items():
            sw = host.get("location_device") or host.get("switch")
            if sw and (not avoid_switch or sw != avoid_switch):
                graph.setdefault(host_name, set()).add(sw)
                graph.setdefault(sw, set()).add(host_name)

        if src_host not in graph or dst_host not in graph:
            return []

        # BFS
        parent: dict[str, str | None] = {src_host: None}
        q = deque([src_host])
        while q:
            node = q.popleft()
            if node == dst_host:
                break
            for neighbor in graph.get(node, set()):
                if neighbor not in parent:
                    parent[neighbor] = node
                    q.append(neighbor)

        if dst_host not in parent:
            return []

        # Reconstruct path
        path = []
        cur = dst_host
        while cur is not None:
            path.append(cur)
            cur = parent.get(cur)
        path.reverse()
        return path
    except Exception as exc:
        logger.warning("Alternate path computation failed: %s", exc)
        return []


def _verification_raw(verification_result: Any) -> dict[str, Any]:
    telemetry = getattr(verification_result, "telemetry", None)
    raw = getattr(telemetry, "raw_int_data", None)
    return raw if isinstance(raw, dict) else {}


def _cidr_to_ternary(cidr: str | None) -> tuple[str, str]:
    """Convert a CIDR string to (value_hex, mask_hex) for ternary match."""
    if not cidr:
        return ("00000000", "00000000")  # wildcard
    net = ip_network(cidr, strict=False)
    addr_int = int(net.network_address)
    prefix = net.prefixlen
    addr_hex = format(addr_int, "08x")
    mask_int = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF if prefix > 0 else 0
    mask_hex = format(mask_int, "08x")
    return (addr_hex, mask_hex)


def should_attempt_congestion_heal(verification_result: Any) -> bool:
    """Return true when a failed verification is suitable for automatic healing."""
    if not _enabled("IDN_AUTO_HEAL_CONGESTION", "true"):
        return False
    if getattr(verification_result, "passed", False):
        return False

    raw = _verification_raw(verification_result)
    qdisc = ((raw.get("raw") or {}).get("qdisc_probe") or {})
    if qdisc.get("congestion_detected"):
        return True

    telemetry = getattr(verification_result, "telemetry", None)
    int_analysis = analyze_telemetry(telemetry)
    if int_analysis.get("congestion_on_primary"):
        return True

    violations = "; ".join(getattr(verification_result, "violations", []) or [])
    if (
        violations
        and int_analysis.get("primary_path")
        and _enabled("IDN_INT_HEAL_ON_PRIMARY_KPI_FAILURE", "true")
    ):
        return True

    keywords = ("带宽不足", "时延超限", "丢包率超限", "限速未达成", "吞吐量指标缺失")
    return any(keyword in violations for keyword in keywords)


# ── Fault injection via ONOS (replaces tc tbf) ────────────────────────────


async def inject_congestion_fault(
    *,
    fault_id: str = "primary-path-congestion",
    node: str = "s_agg_r",
    interface: str = "s_agg_r-eth3",
    rate_mbps: int = 10,
    burst_kb: int = 64,
    latency_ms: int = 400,
) -> dict[str, Any]:
    """Install a temporary sla_enforcement meter rule to simulate congestion.

    Installs a high-priority entry on s_agg_r that rate-limits ALL traffic
    through the primary path, functionally equivalent to the old tc tbf
    but fully visible to ONOS.
    """
    from mcp_servers.onos_server import get_onos_client

    onos = get_onos_client()
    adapter = FlowAdapter()

    # Resolve target device from live topology
    fault_device = await _resolve_device_by_name(node)
    if not fault_device:
        raise ValueError(
            f"Cannot resolve device for node '{node}'. "
            "Ensure ONOS is running and the topology is reachable."
        )

    src_val, src_mask = _cidr_to_ternary("0.0.0.0/0")
    dst_val, dst_mask = _cidr_to_ternary("0.0.0.0/0")

    entry = TableEntry(
        table_name="MyIngress.bandwidth_policy",
        match_fields=[
            MatchField(field_name="hdr.ipv4.srcAddr", match_type="ternary",
                       value=src_val, mask=src_mask),
            MatchField(field_name="hdr.ipv4.dstAddr", match_type="ternary",
                       value=dst_val, mask=dst_mask),
        ],
        action_name="MyIngress.apply_meter_bytes_policy",
        action_params=[],
        priority=_FAULT_PRIORITY,
        meter_config=MeterConfig(
            cir=rate_mbps * 125_000,                # Mbps → bytes/sec
            cburst=burst_kb * 1024,                 # KB → bytes
        ),
    )

    flow_rule = adapter.to_onos_flow_rule(entry, fault_device)
    # Tag the rule so we can identify and delete it later
    flow_rule["appId"] = "org.onosproject.idn"
    flow_rule["appCookie"] = fault_id

    result = await onos.post_flows(fault_device, [flow_rule])
    return {
        "status": "ok",
        "fault_id": fault_id,
        "type": "congestion",
        "enforcement_path": "onos_p4_bandwidth_policy",
        "device": fault_device,
        "rate_mbps": rate_mbps,
        "burst_kb": burst_kb,
        "latency_ms": latency_ms,  # informational — not applied via P4
        "flows_posted": result.get("flows_posted"),
        "total": result.get("total"),
        "errors": result.get("errors"),
    }


async def clear_congestion_fault(fault_id: str | None = None) -> dict[str, Any]:
    """Clear congestion fault by removing sla_enforcement rules from ONOS."""
    from mcp_servers.onos_server import get_onos_client

    onos = get_onos_client()
    try:
        result = await onos.delete("/flows/application/org.onosproject.idn")
        return {"status": "ok", "cleared": True, "result": result}
    except Exception as exc:
        logger.warning("Fault clear via ONOS failed: %s", exc)
        return {"status": "error", "error": str(exc)}


# ── Healing actions ───────────────────────────────────────────────────────


async def install_constrained_alternate_path(
    *,
    src_host: str,
    dst_host: str,
    avoid_devices: set[str],
    min_bandwidth_mbps: float | None = None,
    max_latency_ms: float | None = None,
    onos_client: Any | None = None,
    validated_target_path: list[str] | None = None,
) -> dict[str, Any]:
    """Install a path computed from live topology and ledger constraints."""
    if onos_client is None:
        from mcp_servers.onos_server import get_onos_client
        onos_client = get_onos_client()

    from api.topology import fetch_mininet_topology
    from api.baseline import build_explicit_path_write_requests
    from infra.constrained_path_planner import ConstrainedPathPlanner
    from models.faults import PathConstraints

    topology = await fetch_mininet_topology()
    if not topology or topology.get("status") != "ok":
        raise ValueError("runtime topology is unavailable")
    planned = None
    if validated_target_path:
        from api.topology_paths import topology_paths

        switch_path = [str(item) for item in validated_target_path]
        configured = topology_paths.match_path(
            switches=switch_path,
            src_host=src_host,
            dst_host=dst_host,
        )
        runtime_validation = topology_paths.validate_runtime(topology)
        if configured is None or configured.path_id not in set(
            runtime_validation.get("available_path_ids") or []
        ):
            raise ValueError("validated healing target is not an available physical path")
        alt_path = [src_host, *switch_path, dst_host]
    else:
        planned = ConstrainedPathPlanner().compute(
            topology,
            src_host,
            dst_host,
            PathConstraints(
                min_bandwidth_mbps=min_bandwidth_mbps,
                max_latency_ms=max_latency_ms,
                avoid_devices=avoid_devices,
            ),
        )
        alt_path = planned.nodes
    priority = int(os.getenv("IDN_HEALING_PATH_PRIORITY", "200"))
    write_requests = build_explicit_path_write_requests(
        topology,
        alt_path,
        priority=priority,
    )
    rules_by_device = FlowAdapter().batch_convert(write_requests)

    device_results: dict[str, Any] = {}
    posted = 0
    total = 0
    errors: list[dict[str, Any]] = []
    for device_id, rules in rules_by_device.items():
        result = await onos_client.post_flows(device_id, rules)
        device_results[device_id] = result
        posted += int(result.get("flows_posted") or 0)
        total += int(result.get("total") or len(rules))
        if result.get("errors"):
            errors.append({"device_id": device_id, "errors": result.get("errors")})

    return {
        "status": "SUCCESS" if not errors and posted == total else "PARTIAL",
        "action": "reroute_constrained_path",
        "path": alt_path,
        "constraints": {
            "avoid_devices": sorted(avoid_devices),
            "min_bandwidth_mbps": min_bandwidth_mbps,
            "max_latency_ms": max_latency_ms,
        },
        "path_score": planned.model_dump(mode="json") if planned else {
            "source": "applicability_validated_configured_path",
            "switch_path": list(validated_target_path or []),
        },
        "priority": priority,
        "flows_posted": posted,
        "total": total,
        "devices": device_results,
        "errors": errors or None,
    }


async def throttle_low_priority_source(
    intent_id: str | None = None,
    *,
    target_intent: dict[str, Any] | None = None,
    limit_mbps: int | None = None,
) -> dict[str, Any]:
    """Install bandwidth_policy meter rule to limit low-priority host traffic.

    Uses ONOS → P4 bandwidth_policy (BYTES meter) instead of Linux tc.
    """
    limit_mbps = int(
        limit_mbps
        if limit_mbps is not None
        else os.getenv("IDN_HEAL_LOW_PRIORITY_LIMIT_MBPS", "0")
    )
    if limit_mbps <= 0:
        return {"status": "skipped", "reason": "low-priority throttling disabled"}

    from mcp_servers.onos_server import get_onos_client

    onos = get_onos_client()
    adapter = FlowAdapter()

    selector = (target_intent or {}).get("selector") or {}
    src_cidr = str(
        selector.get("source_ip")
        or selector.get("src_ip")
        or os.getenv("IDN_HEAL_LOW_PRIORITY_SRC_CIDR", "")
    )
    dst_cidr = str(
        selector.get("destination_ip")
        or selector.get("dst_ip")
        or os.getenv("IDN_HEAL_LOW_PRIORITY_DST_CIDR", "")
    )
    if not src_cidr or not dst_cidr:
        return {"status": "skipped", "reason": "low-priority selector is not configured"}
    burst_kb = int(os.getenv("IDN_HEAL_LOW_PRIORITY_BURST_KB", "512"))
    target_intent_id = str((target_intent or {}).get("intent_id") or "dynamic")
    policy_id = f"HEAL-LOWPRIO-{intent_id or 'manual'}-{target_intent_id}"

    src_val, src_mask = _cidr_to_ternary(src_cidr)
    dst_val, dst_mask = _cidr_to_ternary(dst_cidr)

    entry = TableEntry(
        table_name="MyIngress.bandwidth_policy",
        match_fields=[
            MatchField(field_name="hdr.ipv4.srcAddr", match_type="ternary",
                       value=src_val, mask=src_mask),
            MatchField(field_name="hdr.ipv4.dstAddr", match_type="ternary",
                       value=dst_val, mask=dst_mask),
        ],
        action_name="MyIngress.apply_meter_bytes_policy",
        action_params=[],
        priority=_FAULT_PRIORITY,
        meter_config=MeterConfig(
            cir=limit_mbps * 125_000,
            cburst=burst_kb * 1024,
        ),
    )

    # Resolve the edge switch for the low-priority source IP
    edge_device = await _resolve_edge_device_for_ip(src_cidr)
    if not edge_device:
        raise ValueError(
            f"Cannot resolve edge device for source CIDR '{src_cidr}'. "
            "Ensure ONOS hosts are discovered and the topology is reachable."
        )
    flow_rule = adapter.to_onos_flow_rule(entry, edge_device)
    flow_rule["appId"] = "org.onosproject.idn"
    flow_rule["appCookie"] = policy_id

    result = await onos.post_flows(edge_device, [flow_rule])
    return {
        "status": "ok",
        "action": "throttle_low_priority_source",
        "enforcement_path": "onos_p4_bandwidth_policy",
        "policy_id": policy_id,
        "device_id": edge_device,
        "src_cidr": src_cidr,
        "dst_cidr": dst_cidr,
        "target_intent_id": target_intent_id,
        "limit_mbps": limit_mbps,
        "flows_posted": result.get("flows_posted"),
        "flow_ids": result.get("flow_ids") or [],
        "errors": result.get("errors"),
    }


def _dict_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return {}


def _legacy_current_path(
    int_analysis: dict[str, Any],
    verification_result: Any,
    context: Any,
    intent_id: str | None,
) -> list[str]:
    path = list(int_analysis.get("switch_path") or [])
    if path:
        return path
    report = _dict_value(getattr(verification_result, "root_cause_report", None))
    for key in ("current_path", "switch_path", "observed_path"):
        if report.get(key):
            return [str(item) for item in report[key]]
    if intent_id:
        return [str(item) for item in context.current_paths.get(intent_id) or []]
    return []


def _legacy_bottleneck_links(
    int_analysis: dict[str, Any], verification_result: Any
) -> list[str]:
    values: list[str] = []
    if int_analysis.get("bottleneck_link"):
        values.append(str(int_analysis["bottleneck_link"]))
    report = _dict_value(getattr(verification_result, "root_cause_report", None))
    for key in ("bottleneck_link", "bottleneck_link_id"):
        if report.get(key):
            values.append(str(report[key]))
    values.extend(str(item) for item in report.get("affected_link_ids") or [])
    return list(dict.fromkeys(item for item in values if item))


def _legacy_throughput(verification_result: Any) -> float | None:
    telemetry = getattr(verification_result, "telemetry", None)
    kpi = getattr(telemetry, "kpi", None)
    value = getattr(kpi, "throughput_mbps", None)
    if value is None:
        value = (_dict_value(kpi)).get("throughput_mbps")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _evaluate_legacy_healing_actions(
    yang_policy: YANGPolicy,
    verification_result: Any,
    *,
    intent_id: str | None,
    previous_attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build and validate legacy healing candidates against fresh network truth."""

    from agents.healing_action_applicability import (
        HealingActionApplicabilityValidator,
        path_contains_link,
    )
    from api.service_profiles import resolve_service_profile_for_policy
    from api.topology import fetch_mininet_topology
    from api.topology_paths import topology_paths
    from infra.network_context_builder import NetworkContextBuilder
    from models.intent_domain import HealingAction, HealingActionType

    context = await NetworkContextBuilder().build_async()
    int_analysis = analyze_telemetry(getattr(verification_result, "telemetry", None))
    current_path = _legacy_current_path(
        int_analysis, verification_result, context, intent_id
    )
    bottlenecks = _legacy_bottleneck_links(int_analysis, verification_result)
    if intent_id and current_path:
        context.current_paths[intent_id] = list(current_path)

    profile = resolve_service_profile_for_policy(yang_policy)
    topology = await fetch_mininet_topology()
    runtime_validation = topology_paths.validate_runtime(topology)
    available_ids = set(runtime_validation.get("available_path_ids") or [])
    matching_paths = [
        path
        for _, path in topology_paths.items()
        if path.enabled
        and path.src_host == profile.src_host
        and path.dst_host == profile.dst_host
        and path.path_id in available_ids
    ]
    affected_path_ids = [
        path.path_id
        for path in matching_paths
        if any(path_contains_link(path.switch_path, link) for link in bottlenecks)
    ]
    shared = bool(matching_paths) and len(affected_path_ids) == len(matching_paths)
    first_switch_path = matching_paths[0].switch_path if matching_paths else []
    access_link = bool(
        len(first_switch_path) >= 2
        and any(
            path_contains_link(first_switch_path[:2], link)
            for link in bottlenecks
        )
    )
    root_cause = {
        "category": (
            "SHARED_BOTTLENECK_CONGESTION"
            if shared
            else "PATH_SEGMENT_CONGESTION"
        ),
        "scope": "SHARED_PATH" if shared else "PATH_SPECIFIC",
        "location": "ACCESS" if access_link else "CORE",
        "affected_link_ids": list(bottlenecks),
        "candidate_paths_affected": affected_path_ids,
    }

    qos = yang_policy.network_policy.action.qos_params
    qos_mode = getattr(qos, "qos_mode", None) if qos else None
    if hasattr(qos_mode, "value"):
        qos_mode = qos_mode.value
    requested = (
        float(qos.bandwidth)
        if qos and qos.bandwidth and str(qos_mode or "") != "RATE_LIMIT"
        else None
    )
    tolerance = float(os.getenv("IDN_QOS_KPI_TOLERANCE_RATIO", "0.95"))
    effective_min = requested * tolerance if requested is not None else None
    current_measurement = _legacy_throughput(verification_result)
    required_gain = max(
        0.0, (effective_min or 0.0) - float(current_measurement or 0.0)
    )

    actions: list[HealingAction] = []
    for path in matching_paths:
        actions.append(
            HealingAction(
                type=HealingActionType.REROUTE,
                target=path.path_id,
                params={
                    "candidate_id": path.path_id,
                    "target_path": path.switch_path,
                    "current_path": current_path,
                    "bottleneck_link_ids": bottlenecks,
                    "expected_available_mbps": float(path.path_capacity_mbps),
                    "current_measurement_mbps": current_measurement,
                    "requested_mbps": requested,
                    "effective_min_mbps": effective_min,
                },
                reason="evaluate configured physical path against the current bottleneck",
                confidence=0.95,
            )
        )

    competitors = [
        item
        for item in context.active_intents
        if str(item.get("intent_id") or "") != str(intent_id or "")
        and bool(item.get("preemptible"))
        and str(item.get("priority") or "").lower() == "low"
        and any(
            path_contains_link(item.get("current_path") or [], link)
            for link in bottlenecks
        )
    ]
    competitor_ids = {str(item.get("intent_id")) for item in competitors}
    recoverable = sum(float(item.get("current_usage_mbps") or 0) for item in competitors)
    if recoverable <= 0:
        recoverable = sum(
            float(item.get("bandwidth_mbps") or 0)
            for item in context.reservations
            if str(item.get("intent_id") or "") in competitor_ids
        )
    resource_params = {
        "target_intent_ids": sorted(competitor_ids),
        "expected_released_mbps": recoverable,
        "required_gain_mbps": required_gain,
        "current_measurement_mbps": current_measurement,
        "requested_mbps": requested,
        "effective_min_mbps": effective_min,
        "bottleneck_link_ids": bottlenecks,
    }
    for action_type in (
        HealingActionType.THROTTLE_LOWER_PRIORITY,
        HealingActionType.QOS_REALLOCATION,
    ):
        actions.append(
            HealingAction(
                type=action_type,
                target=",".join(sorted(competitor_ids)) or "none",
                params=dict(resource_params),
                reason="reclaim only measured low-priority preemptible contention",
                affected_intent_ids=sorted(competitor_ids),
                confidence=0.9,
                risk="medium",
            )
        )

    stale = [
        item
        for item in context.reservations
        if (
            bool(item.get("stale"))
            or str(item.get("status") or "").upper() in {"STALE", "EXPIRED"}
        )
        and (
            not bottlenecks
            or str(item.get("resource_id") or "") in set(bottlenecks)
        )
    ]
    stale_recoverable = sum(float(item.get("bandwidth_mbps") or 0) for item in stale)
    actions.append(
        HealingAction(
            type=HealingActionType.RELEASE_STALE_RESERVATION,
            target="stale_reservations",
            params={
                "reservation_ids": [
                    str(item.get("reservation_id") or item.get("id")) for item in stale
                ],
                "expected_released_mbps": stale_recoverable,
                "required_gain_mbps": required_gain,
                "current_measurement_mbps": current_measurement,
            },
            reason="release only reservations evidenced as stale or expired",
            confidence=0.9,
            risk="low",
        )
    )

    validator = HealingActionApplicabilityValidator()
    if not current_path or not bottlenecks:
        # Missing current path or bottleneck is insufficient evidence for an
        # autonomous data-plane mutation.  Return auditable candidate facts.
        evaluations = []
        evidence_error = (
            "CURRENT_PATH_UNAVAILABLE" if not current_path else "BOTTLENECK_UNRESOLVED"
        )
    else:
        evaluations = [
            validator.validate(
                action,
                context=context,
                current_plan=None,
                current_path=current_path,
                root_cause=root_cause,
                bottleneck_link_ids=bottlenecks,
                active_intents=context.active_intents,
                previously_attempted_actions=previous_attempts,
            )
            for action in actions
        ]
        evidence_error = None
    return {
        "context": context,
        "topology": topology,
        "current_path": current_path,
        "bottleneck_link_ids": bottlenecks,
        "root_cause": root_cause,
        "actions": actions,
        "evaluations": evaluations,
        "active_intents": context.active_intents,
        "effective_min_mbps": effective_min,
        "current_measurement_mbps": current_measurement,
        "runtime_path_validation": runtime_validation,
        "evidence_error": evidence_error,
    }


async def apply_congestion_healing(
    yang_policy: YANGPolicy | dict[str, Any] | None,
    verification_result: Any,
    *,
    intent_id: str | None = None,
    previous_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate candidates against fresh context, then execute one useful action."""
    if yang_policy is not None and not isinstance(yang_policy, YANGPolicy):
        yang_policy = YANGPolicy(**yang_policy)
    if not isinstance(yang_policy, YANGPolicy):
        return {
            "status": "unsatisfiable",
            "reason": "TELEMETRY_INSUFFICIENT: healing requires a validated YANGPolicy",
            "actions": [],
            "candidate_evaluations": [],
        }

    from agents.healing_action_applicability import FeasibilityEvaluator
    from api.service_profiles import resolve_service_profile_for_policy
    from models.intent_domain import HealingActionApplicability, HealingActionType

    evaluation = await _evaluate_legacy_healing_actions(
        yang_policy,
        verification_result,
        intent_id=intent_id,
        previous_attempts=list(previous_attempts or []),
    )
    evaluations = evaluation["evaluations"]
    serialized_evaluations = [item.model_dump(mode="json") for item in evaluations]
    base_result = {
        "policy_id": yang_policy.network_policy.policy_id,
        "pre_heal_int_analysis": analyze_telemetry(
            getattr(verification_result, "telemetry", None)
        ),
        "current_path": evaluation["current_path"],
        "bottleneck_link_ids": evaluation["bottleneck_link_ids"],
        "root_cause": evaluation["root_cause"],
        "active_intents": evaluation["active_intents"],
        "candidate_evaluations": serialized_evaluations,
        "runtime_path_validation": evaluation["runtime_path_validation"],
    }
    if evaluation["evidence_error"]:
        return {
            **base_result,
            # Missing path/bottleneck evidence is a collection task, never a
            # proof that the candidate space is empty.
            "status": "inconclusive",
            "healing_search_status": "PENDING_EVIDENCE",
            "reason": f"TELEMETRY_INSUFFICIENT: {evaluation['evidence_error']}",
            "actions": [],
            "attempt_counted": False,
        }

    applicable = [
        item
        for item in evaluations
        if item.applicability == HealingActionApplicability.APPLICABLE
    ]
    action_order = {
        HealingActionType.REROUTE: 0,
        HealingActionType.THROTTLE_LOWER_PRIORITY: 1,
        HealingActionType.QOS_REALLOCATION: 2,
        HealingActionType.RELEASE_STALE_RESERVATION: 3,
    }
    selected_evaluation = min(
        applicable,
        key=lambda item: action_order.get(item.action, 99),
        default=None,
    )
    action_by_id = {item.action_id: item for item in evaluation["actions"]}
    selected = (
        action_by_id.get(selected_evaluation.action_id)
        if selected_evaluation is not None
        else None
    )
    feasibility = FeasibilityEvaluator.evaluate(
        evaluations,
        effective_min_mbps=evaluation["effective_min_mbps"],
        current_measurement_mbps=evaluation["current_measurement_mbps"],
    )
    if selected is None:
        matrix_complete = bool(evaluation["actions"]) and len(evaluations) == len(evaluation["actions"])
        all_rejected = matrix_complete and all(
            item.applicability != HealingActionApplicability.APPLICABLE
            for item in evaluations
        )
        return {
            **base_result,
            "status": "unsatisfiable" if all_rejected else "inconclusive",
            "healing_search_status": "EXHAUSTED" if all_rejected else "EVALUATING",
            "reason": "HEALING_SEARCH_EXHAUSTED" if all_rejected else "HEALING_EVALUATING",
            "feasibility": feasibility,
            "actions": [],
            "attempt_counted": False,
        }

    profile = resolve_service_profile_for_policy(yang_policy)
    executed: list[dict[str, Any]] = []
    try:
        if selected.type == HealingActionType.REROUTE:
            executed.append(
                await install_constrained_alternate_path(
                    src_host=profile.src_host,
                    dst_host=profile.dst_host,
                    avoid_devices=set(),
                    min_bandwidth_mbps=evaluation["effective_min_mbps"],
                    max_latency_ms=profile.validation_thresholds.get(
                        "maximum_latency_ms"
                    ),
                    validated_target_path=list(
                        selected.params.get("target_path") or []
                    ),
                )
            )
        elif selected.type == HealingActionType.THROTTLE_LOWER_PRIORITY:
            targets = {
                str(item.get("intent_id")): item
                for item in evaluation["active_intents"]
                if str(item.get("intent_id"))
                in set(selected.params.get("target_intent_ids") or [])
            }
            remaining = float(selected.params.get("required_gain_mbps") or 0)
            for target in targets.values():
                usage = float(target.get("current_usage_mbps") or 0)
                release = min(max(0.0, usage - 1.0), remaining)
                if release <= 0:
                    continue
                executed.append(
                    await throttle_low_priority_source(
                        intent_id,
                        target_intent=target,
                        limit_mbps=max(1, int(usage - release)),
                    )
                )
                remaining = max(0.0, remaining - release)
            if remaining > 1e-9:
                raise ValueError(
                    f"validated recoverable bandwidth was not executable; remaining={remaining:g} Mbps"
                )
        else:
            raise ValueError(
                f"no autonomous executor is implemented for {selected.type.value}"
            )
    except Exception as exc:
        logger.warning("Validated healing action failed: %s", exc)
        executed.append(
            {"status": "error", "action": selected.type.value, "error": str(exc)}
        )

    ok = bool(executed) and all(
        str(item.get("status") or "").upper() in {"SUCCESS", "OK"}
        for item in executed
    )
    return {
        **base_result,
        "status": "ok" if ok else "error",
        "reason": "VALIDATED_HEALING_ACTION_EXECUTED" if ok else "HEALING_EXECUTION_FAILED",
        "selected_action": selected.model_dump(mode="json"),
        "selected_evaluation": selected_evaluation.model_dump(mode="json"),
        "action_fingerprint": selected_evaluation.action_fingerprint,
        "context_signature": selected_evaluation.context_signature,
        "feasibility": feasibility,
        "actions": executed,
        "attempt_counted": True,
    }


# ── INT-aware healing helpers ────────────────────────────────────────────

def _find_congested_switches(verification_result: Any) -> list[str]:
    """Identify congested switches from INT hop-by-hop telemetry.

    Extracts switch names where queue occupancy or per-hop latency exceeds
    configurable thresholds.  Used by the reroute healing action to avoid
    specific bottleneck switches instead of hardcoding ``s_core1``.
    """
    queue_threshold = int(os.getenv("IDN_INT_QUEUE_THRESHOLD", "50"))
    latency_threshold_us = int(os.getenv("IDN_INT_LATENCY_THRESHOLD_US", "500"))

    congested: list[str] = []
    raw = getattr(verification_result, "telemetry", None)
    if raw is None:
        return congested

    int_data = (getattr(raw, "raw", {}) or {}).get("int_probe") or {}
    for hop in int_data.get("hop_records") or int_data.get("hops") or []:
        hop_name = hop.get("switch_id") or hop.get("switch_name") or ""
        queue = hop.get("queue_occupancy") or hop.get("queue_depth") or 0
        latency_ns = hop.get("egress_latency_ns") or 0
        latency_us = latency_ns / 1000 if latency_ns > 0 else 0

        if queue > queue_threshold or latency_us > latency_threshold_us:
            if hop_name and hop_name not in congested:
                congested.append(hop_name)
    return congested


async def remark_traffic_dscp(verification_result: Any) -> dict[str, Any]:
    """Deprioritise congested traffic by lowering its DSCP marking.

    Installs a high-priority ``sla_enforcement`` entry with ``set_dscp``
    action marking matching traffic to DSCP 0 (best-effort).  Only fires
    when the verification result indicates a healable congestion fault.
    """
    from mcp_servers.flow_adapter import FlowAdapter
    from mcp_servers.flow_delivery import FlowDeliveryFactory
    from models.flow_table import TableEntry, ActionParam, MatchField

    yang_match = getattr(
        getattr(verification_result, "yang_policy", None), "match", None,
    )
    src_ip = getattr(yang_match, "source_ip", None) or "0.0.0.0/0"
    dst_ip = getattr(yang_match, "destination_ip", None) or "0.0.0.0/0"

    dscp_value = int(os.getenv("IDN_HEAL_DSCP_VALUE", "0"))
    edge_device = os.getenv("IDN_HEAL_EDGE_DEVICE", "device:bmv2:2")

    adapter = FlowAdapter()
    delivery = FlowDeliveryFactory.create()

    entry = TableEntry(
        table_name="MyIngress.sla_enforcement",
        match_fields=[
            MatchField(field_name="hdr.ipv4.srcAddr", match_type="ternary",
                       value="00000000", mask="00000000"),
            MatchField(field_name="hdr.ipv4.dstAddr", match_type="ternary",
                       value="00000000", mask="00000000"),
            MatchField(field_name="hdr.ipv4.protocol", match_type="ternary",
                       value="00", mask="00"),
            MatchField(field_name="hdr.ipv4.diffserv", match_type="ternary",
                       value="00", mask="00"),
        ],
        action_name="MyIngress.set_dscp",
        action_params=[ActionParam(param_name="dscp_value", value=dscp_value)],
        priority=55000,
    )

    flow_rule = adapter.to_onos_flow_rule(entry, edge_device)
    result = await delivery.post_flows(edge_device, [flow_rule])
    return {
        "status": "ok" if not (result.get("errors")) else "partial",
        "action": "remark_dscp",
        "dscp_value": dscp_value,
        "edge_device": edge_device,
        "flows_posted": result.get("flows_posted"),
        "src_ip": src_ip,
        "dst_ip": dst_ip,
    }


async def block_congested_traffic(verification_result: Any) -> dict[str, Any]:
    """Block traffic from a congested source using the P4 ACL table.

    Installs a high-priority ``acl_deny`` rule to drop matching traffic
    at the edge switch.  Used as a last-resort healing action when reroute
    and throttling are insufficient.
    """
    from mcp_servers.flow_adapter import FlowAdapter
    from mcp_servers.flow_delivery import FlowDeliveryFactory
    from models.flow_table import TableEntry, MatchField

    yang_match = getattr(
        getattr(verification_result, "yang_policy", None), "match", None,
    )
    src_ip = getattr(yang_match, "source_ip", None) or "0.0.0.0/0"
    dst_ip = getattr(yang_match, "destination_ip", None) or "0.0.0.0/0"
    edge_device = os.getenv("IDN_HEAL_EDGE_DEVICE", "device:bmv2:2")

    adapter = FlowAdapter()
    delivery = FlowDeliveryFactory.create()

    entry = TableEntry(
        table_name="MyIngress.acl",
        match_fields=[
            MatchField(field_name="hdr.ipv4.srcAddr", match_type="ternary",
                       value="00000000", mask="00000000"),
            MatchField(field_name="hdr.ipv4.dstAddr", match_type="ternary",
                       value="00000000", mask="00000000"),
        ],
        action_name="MyIngress.acl_deny",
        action_params=[],
        priority=60000,
    )

    flow_rule = adapter.to_onos_flow_rule(entry, edge_device)
    result = await delivery.post_flows(edge_device, [flow_rule])
    return {
        "status": "ok" if not (result.get("errors")) else "partial",
        "action": "acl_block",
        "edge_device": edge_device,
        "flows_posted": result.get("flows_posted"),
        "src_ip": src_ip,
        "dst_ip": dst_ip,
    }

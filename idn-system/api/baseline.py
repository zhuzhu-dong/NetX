"""
Baseline connectivity for the research-datacenter BMv2 topology.

Policy intents install high-priority service rules (DSCP, drop, etc.). These
low-priority IPv4 forwarding rules provide the base path that lets real traffic
cross the topology and hit those policy rules.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

from api.topology import canonicalize_topology, fetch_mininet_topology
from mcp_servers.flow_adapter import FlowAdapter
from models.flow_table import ActionParam, AtomicityMode, MatchField, TableEntry, WriteRequest

logger = logging.getLogger(__name__)

BASELINE_PRIORITY = 10

@dataclass(frozen=True)
class ForwardingRule:
    device_id: str
    destination_ip: str
    egress_port: int
    src_mac: str
    dst_mac: str


# BASELINE_RULES removed — static fallback for the primary path.
# Forwarding rules are now always computed dynamically from the runtime
# topology via _dynamic_forwarding_rules() → Dijkstra shortest-path.


def _probe_url() -> str | None:
    base_url = os.getenv("MININET_PROBE_URL")
    return base_url.rstrip("/") if base_url else None


async def configure_p4_switch_ids(onos_client: Any | None = None) -> dict[str, Any]:
    """Install explicit per-device INT identity entries on every egress port."""
    from api._probe_session import get_probe_session

    session = await get_probe_session()
    inventory = await session.post("/p4/switch-ids", json_data={})
    if inventory is None:
        return {"status": "skipped", "reason": "MININET_PROBE_URL is not set"}
    if inventory.get("status") != "ok":
        return inventory
    if onos_client is None:
        from mcp_servers.onos_server import get_onos_client

        onos_client = get_onos_client()

    requests: list[WriteRequest] = []
    for item in inventory.get("results") or []:
        switch_id = int(item.get("switch_id") or 0)
        device_id = str(item.get("device_id") or "")
        ports = sorted({int(value) for value in item.get("ports") or [] if int(value) > 0})
        if not switch_id or not device_id or not ports:
            continue
        entries = [
            TableEntry(
                table_name="MyEgress.int_switch_config",
                match_fields=[MatchField(
                    field_name="standard_metadata.egress_port",
                    match_type="exact",
                    value=port,
                )],
                action_name="MyEgress.push_int_telemetry",
                action_params=[ActionParam(param_name="switch_id", value=switch_id)],
                priority=1,
            )
            for port in ports
        ]
        requests.append(WriteRequest(
            device_id=device_id,
            table_entries=entries,
            atomicity=AtomicityMode.ROLLBACK_ON_ERROR,
        ))

    rules_by_device = FlowAdapter().batch_convert(requests)
    results: dict[str, Any] = {}
    posted = total = 0
    errors: list[dict[str, Any]] = []
    for device_id, rules in rules_by_device.items():
        result = await onos_client.post_flows(device_id, rules)
        results[device_id] = result
        posted += int(result.get("flows_posted") or 0)
        total += int(result.get("total") or len(rules))
        if result.get("errors"):
            errors.append({"device_id": device_id, "errors": result["errors"]})
    return {
        "status": "SUCCESS" if total > 0 and posted == total and not errors else "PARTIAL",
        "table": "MyEgress.int_switch_config",
        "configured": posted,
        "total": total,
        "devices": results,
        "errors": errors or None,
        "inventory": inventory.get("results") or [],
    }


def _host_ip(topology: dict[str, Any], host_name: str) -> str:
    """Resolve host IP from topology data.  Raises ValueError if not found."""
    for host in topology.get("hosts", []) or []:
        if host.get("name") == host_name and host.get("ip"):
            return str(host["ip"]).split("/", 1)[0]
    raise ValueError(f"Host {host_name} not found in topology — is Mininet probe running?")


def _host_mac(topology: dict[str, Any], host_name: str) -> str:
    """Resolve host MAC from topology data.  Returns hex string without colons.

    Raises ValueError if the host or its MAC is not found — there is no
    fallback to placeholder values because an incorrect MAC causes the Linux
    host kernel to silently drop frames.
    """
    for host in topology.get("hosts", []) or []:
        if host.get("name") == host_name and host.get("mac"):
            return _mac_hex(host["mac"], label=f"host {host_name}")
    raise ValueError(
        f"MAC address not found for host '{host_name}' in topology. "
        "Ensure the Mininet probe is running and the host is connected to a switch."
    )


def _mac_hex(value: Any, *, label: str) -> str:
    compact = str(value or "").replace(":", "").replace("-", "").lower()
    if len(compact) != 12:
        raise ValueError(f"{label} MAC must contain 12 hex digits, got {value!r}")
    try:
        raw = bytes.fromhex(compact)
    except ValueError as exc:
        raise ValueError(f"{label} MAC is not hexadecimal: {value!r}") from exc
    if raw == bytes(6):
        raise ValueError(f"{label} MAC must not be 00:00:00:00:00:00")
    if raw[0] & 1:
        raise ValueError(f"{label} MAC must be unicast, got {value!r}")
    return compact


def _host_gateway_mac(topology: dict[str, Any], host_name: str) -> str:
    for host in topology.get("hosts", []) or []:
        if host.get("name") == host_name and host.get("gateway_mac"):
            return _mac_hex(host["gateway_mac"], label=f"host {host_name} gateway")
    raise ValueError(
        f"Gateway MAC not found for host '{host_name}' in topology. "
        "The Mininet probe must export YAML-derived gateway metadata."
    )


def _switch_router_mac(topology: dict[str, Any], switch_name: str) -> str:
    for switch in topology.get("switches", []) or []:
        if switch.get("name") == switch_name and switch.get("router_mac"):
            return _mac_hex(switch["router_mac"], label=f"switch {switch_name} router")
    raise ValueError(f"Router MAC not found for switch '{switch_name}' in topology")


def _hop_macs(
    topology: dict[str, Any],
    switch_name: str,
    next_node: str,
) -> tuple[str, str]:
    """Return valid source/destination MACs for one routed egress hop."""
    if next_node in {host.get("name") for host in topology.get("hosts", []) or []}:
        return (
            _host_gateway_mac(topology, next_node),
            _host_mac(topology, next_node),
        )
    return (
        _switch_router_mac(topology, switch_name),
        _switch_router_mac(topology, next_node),
    )


def _switch_devices(topology: dict[str, Any]) -> dict[str, str]:
    return {
        switch["name"]: switch["device_id"]
        for switch in topology.get("switches", []) or []
        if switch.get("name") and switch.get("device_id")
    }


def _endpoint_node(endpoint: dict[str, Any]) -> str:
    return str(endpoint.get("node") or "")


def _link_endpoints(link: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    endpoints = link.get("endpoints", []) or []
    if len(endpoints) != 2:
        raise ValueError(f"Invalid topology link endpoints: {link}")
    return endpoints[0], endpoints[1]


def _build_graph(topology: dict[str, Any]) -> dict[str, list[tuple[str, float, dict[str, Any]]]]:
    graph: dict[str, list[tuple[str, float, dict[str, Any]]]] = {}
    for link in topology.get("links", []) or []:
        # The Mininet snapshot retains down links so operators can inspect the
        # physical topology.  They must not remain eligible for forwarding
        # recomputation after a LINK_DOWN fault.
        if str(link.get("state") or "ACTIVE").upper() != "ACTIVE":
            continue
        try:
            endpoint_a, endpoint_b = _link_endpoints(link)
        except ValueError:
            continue
        node_a = _endpoint_node(endpoint_a)
        node_b = _endpoint_node(endpoint_b)
        if not node_a or not node_b:
            continue
        cost = float(link.get("cost", 1.0) or 1.0)
        graph.setdefault(node_a, []).append((node_b, cost, link))
        graph.setdefault(node_b, []).append((node_a, cost, link))
    return graph


def _shortest_path(topology: dict[str, Any], src: str, dst: str) -> list[str]:
    graph = _build_graph(topology)
    if src not in graph or dst not in graph:
        raise ValueError(f"NO_PATH: endpoint absent from canonical topology: {src} -> {dst}")

    queue: list[tuple[float, str, list[str]]] = [(0.0, src, [src])]
    best_cost: dict[str, float] = {}
    while queue:
        cost, node, path = heapq.heappop(queue)
        if node == dst:
            return path
        if node in best_cost and best_cost[node] <= cost:
            continue
        best_cost[node] = cost
        for neighbor, edge_cost, _link in graph.get(node, []):
            if neighbor in path:
                continue
            heapq.heappush(queue, (cost + edge_cost, neighbor, [*path, neighbor]))
    raise ValueError(f"NO_PATH: no active path in canonical topology: {src} -> {dst}")


def _link_between(topology: dict[str, Any], node_a: str, node_b: str) -> dict[str, Any]:
    for link in topology.get("links", []) or []:
        try:
            endpoint_a, endpoint_b = _link_endpoints(link)
        except ValueError:
            continue
        names = {_endpoint_node(endpoint_a), _endpoint_node(endpoint_b)}
        if names == {node_a, node_b}:
            return link
    raise ValueError(f"No link between {node_a} and {node_b}")


def _egress_port(link: dict[str, Any], switch_name: str) -> int:
    """Return the shared 1-based Mininet probe/BMv2/ONOS port number."""
    endpoint_a, endpoint_b = _link_endpoints(link)
    for endpoint in (endpoint_a, endpoint_b):
        if _endpoint_node(endpoint) == switch_name:
            port = endpoint.get("port")
            if port is None:
                raise ValueError(f"Missing port for switch {switch_name} in link {link.get('id')}")
            parsed = int(port)
            if parsed < 1:
                raise ValueError(f"Invalid non-positive BMv2 port {parsed} for {switch_name}")
            return parsed
    raise ValueError(f"Switch {switch_name} not found in link {link.get('id')}")


def _dynamic_forwarding_rules(
    topology: dict[str, Any],
    src_host: str,
    dst_host: str,
) -> list[ForwardingRule]:
    if topology.get("status") != "ok":
        raise ValueError("Topology is unavailable")

    switch_devices = _switch_devices(topology)
    if not switch_devices:
        raise ValueError("Topology has no switch device mapping")

    src_ip = _host_ip(topology, src_host)
    dst_ip = _host_ip(topology, dst_host)
    forward_path = _shortest_path(topology, src_host, dst_host)
    reverse_path = list(reversed(forward_path))

    rules: list[ForwardingRule] = []
    seen: set[tuple[str, str]] = set()
    for path, destination_ip in ((forward_path, dst_ip), (reverse_path, src_ip)):
        for index, node in enumerate(path[1:-1], start=1):
            device_id = switch_devices.get(node)
            if not device_id:
                continue
            next_node = path[index + 1]
            link = _link_between(topology, node, next_node)
            src_mac, dst_mac = _hop_macs(topology, node, next_node)
            rule = ForwardingRule(
                device_id,
                destination_ip,
                _egress_port(link, node),
                src_mac,
                dst_mac,
            )
            key = (rule.device_id, rule.destination_ip)
            if key not in seen:
                seen.add(key)
                rules.append(rule)

    if not rules:
        raise ValueError(f"No switch forwarding rules generated for {src_host} -> {dst_host}")
    return rules


def _forwarding_rules_for_path(
    topology: dict[str, Any],
    forward_path: list[str],
    src_host: str | None = None,
    dst_host: str | None = None,
) -> list[ForwardingRule]:
    """Build bidirectional forwarding rules for an explicit host-to-host path."""
    src_host = src_host or (forward_path[0] if forward_path else None)
    dst_host = dst_host or (forward_path[-1] if forward_path else None)
    if not src_host or not dst_host:
        raise ValueError("Explicit path must contain source and destination hosts")
    if len(forward_path) < 3 or forward_path[0] != src_host or forward_path[-1] != dst_host:
        raise ValueError(f"Invalid explicit path for {src_host} -> {dst_host}: {forward_path}")

    switch_devices = _switch_devices(topology)
    src_ip = _host_ip(topology, src_host)
    dst_ip = _host_ip(topology, dst_host)
    reverse_path = list(reversed(forward_path))

    rules: list[ForwardingRule] = []
    seen: set[tuple[str, str]] = set()
    for path, destination_ip in ((forward_path, dst_ip), (reverse_path, src_ip)):
        for index, node in enumerate(path[1:-1], start=1):
            device_id = switch_devices.get(node)
            if not device_id:
                continue
            next_node = path[index + 1]
            link = _link_between(topology, node, next_node)
            src_mac, dst_mac = _hop_macs(topology, node, next_node)
            rule = ForwardingRule(
                device_id,
                destination_ip,
                _egress_port(link, node),
                src_mac,
                dst_mac,
            )
            key = (rule.device_id, rule.destination_ip)
            if key not in seen:
                seen.add(key)
                rules.append(rule)

    if not rules:
        raise ValueError(f"No switch forwarding rules generated for explicit path: {forward_path}")
    return rules


def _rules_from_topology_or_fallback(topology: dict[str, Any] | None = None) -> list[ForwardingRule]:
    """Compute baseline forwarding rules from topology, or raise on failure.

    No static fallback — if topology data is unavailable, the caller must
    ensure ONOS/Mininet are running before invoking baseline configuration.
    """
    if topology:
        # Baseline forwarding follows the centralized ServiceProfile registry,
        # so every registered active-probe pair has bidirectional data and
        # iperf control connectivity.  Adding AI/backup services must not
        # require another hard-coded path list here.
        from api.service_profiles import list_service_profiles

        host_names = {str(item.get("name") or "") for item in topology.get("hosts", []) or []}
        path_pairs: list[tuple[str, str]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for profile in list_service_profiles():
            pair = (profile.src_host, profile.dst_host)
            if pair in seen_pairs or not set(pair).issubset(host_names):
                continue
            seen_pairs.add(pair)
            path_pairs.append(pair)
        if not path_pairs:
            raise ValueError("No ServiceProfile endpoints exist in runtime topology")

        merged: dict[tuple[str, str], ForwardingRule] = {}
        for src_host, dst_host in path_pairs:
            for rule in _dynamic_forwarding_rules(topology, src_host, dst_host):
                key = (rule.device_id, rule.destination_ip)
                previous = merged.get(key)
                if previous and previous != rule:
                    raise ValueError(
                        f"Conflicting baseline next hops for {key}: {previous} != {rule}"
                    )
                merged[key] = rule
        return list(merged.values())
    raise RuntimeError(
        "No topology data available for baseline forwarding rules. "
        "Ensure Mininet and ONOS are running."
    )


def _build_write_requests_from_rules(
    rules: list[ForwardingRule],
    *,
    priority: int = BASELINE_PRIORITY,
) -> list[WriteRequest]:
    grouped: dict[str, list[TableEntry]] = {}
    for rule in rules:
        # Reject malformed controller input before ONOS/P4Runtime can encode a
        # missing action parameter as an all-zero 48-bit value.
        src_mac = _mac_hex(rule.src_mac, label=f"{rule.device_id} srcMac")
        dst_mac = _mac_hex(rule.dst_mac, label=f"{rule.device_id} dstMac")
        entry = TableEntry(
            table_name="MyIngress.ipv4_routing",
            match_fields=[
                MatchField(
                    field_name="hdr.ipv4.dstAddr",
                    match_type="ternary",
                    value=rule.destination_ip,
                    mask=0xFFFFFFFF,
                    prefix_len=32,
                )
            ],
            action_name="MyIngress.set_nhop",
            action_params=[
                ActionParam(param_name="port", value=rule.egress_port),
                ActionParam(param_name="srcMac", value=src_mac),
                ActionParam(param_name="dstMac", value=dst_mac),
            ],
            priority=priority,
        )
        grouped.setdefault(rule.device_id, []).append(entry)

    return [
        WriteRequest(
            device_id=device_id,
            table_entries=entries,
            atomicity=AtomicityMode.ROLLBACK_ON_ERROR,
        )
        for device_id, entries in grouped.items()
    ]


def _host_mac_rules(topology: dict[str, Any]) -> dict[str, list[TableEntry]]:
    """Generate host_mac_resolution table entries for every host in the topology.

    The P4 host_mac_resolution table rewrites hdr.ethernet.dstAddr after routing,
    so the last-hop switch delivers the frame with the real host MAC.  Without
    this, a Linux host drops the frame because the placeholder dstMac does not
    match the host's own MAC.
    """
    entries: dict[str, list[TableEntry]] = {}
    host_locations: dict[str, str] = {}
    for link in topology.get("links", []) or []:
        endpoints = link.get("endpoints", []) or []
        if len(endpoints) != 2:
            continue
        host_endpoint = next((item for item in endpoints if item.get("type") == "host"), None)
        switch_endpoint = next((item for item in endpoints if item.get("type") == "switch"), None)
        if host_endpoint and switch_endpoint:
            host_locations[str(host_endpoint.get("node"))] = str(switch_endpoint.get("device_id") or "")

    for host in topology.get("hosts", []) or []:
        host_name = str(host.get("name") or "")
        ip = host.get("ip")
        mac = host.get("mac")
        device_id = host_locations.get(host_name)
        if not ip or not mac or not device_id:
            raise ValueError(f"Incomplete host MAC-resolution metadata for {host_name or host!r}")
        ip_addr = str(ip).split("/", 1)[0]
        mac_hex = _mac_hex(mac, label=f"host {host_name}")
        entries.setdefault(device_id, []).append(
            TableEntry(
                table_name="MyIngress.host_mac_resolution",
                match_fields=[
                    MatchField(
                        field_name="hdr.ipv4.dstAddr",
                        match_type="lpm",
                        value=ip_addr,
                        prefix_len=32,
                    )
                ],
                action_name="MyIngress.rewrite_dst_mac",
                action_params=[
                    ActionParam(param_name="mac", value=mac_hex),
                ],
            )
        )
    return entries


def _gateway_arp_proxy_rules(topology: dict[str, Any]) -> dict[str, list[TableEntry]]:
    """Generate one data-plane ARP reply rule per attached subnet gateway."""
    host_locations: dict[str, str] = {}
    for link in topology.get("links", []) or []:
        endpoints = link.get("endpoints", []) or []
        if len(endpoints) != 2:
            continue
        host_endpoint = next((item for item in endpoints if item.get("type") == "host"), None)
        switch_endpoint = next((item for item in endpoints if item.get("type") == "switch"), None)
        if host_endpoint and switch_endpoint:
            host_locations[str(host_endpoint.get("node"))] = str(
                switch_endpoint.get("device_id") or ""
            )

    gateways: dict[tuple[str, str], str] = {}
    for host in topology.get("hosts", []) or []:
        host_name = str(host.get("name") or "")
        device_id = host_locations.get(host_name)
        gateway_ip = str(host.get("gateway_ip") or "")
        gateway_mac = str(host.get("gateway_mac") or "")
        if not device_id or not gateway_ip or not gateway_mac:
            raise ValueError(f"Incomplete gateway attachment metadata for {host_name or host!r}")
        mac_hex = _mac_hex(gateway_mac, label=f"host {host_name} gateway")
        key = (device_id, gateway_ip)
        previous = gateways.get(key)
        if previous and previous != mac_hex:
            raise ValueError(f"Conflicting gateway MAC values for {device_id}/{gateway_ip}")
        gateways[key] = mac_hex

    entries: dict[str, list[TableEntry]] = {}
    for (device_id, gateway_ip), gateway_mac in sorted(gateways.items()):
        entries.setdefault(device_id, []).append(
            TableEntry(
                table_name="MyIngress.arp_proxy",
                match_fields=[MatchField(
                    field_name="hdr.arp.targetProtoAddr",
                    match_type="exact",
                    value=gateway_ip,
                )],
                action_name="MyIngress.arp_reply",
                action_params=[ActionParam(param_name="target_mac", value=gateway_mac)],
                priority=1000,
            )
        )
    return entries


def build_baseline_write_requests(topology: dict[str, Any] | None = None) -> list[WriteRequest]:
    """Build IPv4 routing, host-MAC resolution and gateway ARP rules."""
    # Baseline forwarding consumes only the canonical graph.  This is also
    # intentionally idempotent for callers that already obtained it from the
    # topology API or a persisted recovery snapshot.
    topology = canonicalize_topology(topology) if topology else topology
    requests = _build_write_requests_from_rules(
        _rules_from_topology_or_fallback(topology),
        priority=BASELINE_PRIORITY,
    )

    # Append host_mac_resolution rules — each switch needs a local copy
    # because the P4 table is consulted after routing on the egress switch.
    if topology:
        for generated in (_host_mac_rules(topology), _gateway_arp_proxy_rules(topology)):
            for device_id, entries in generated.items():
                requests.append(
                    WriteRequest(
                        device_id=device_id,
                        table_entries=entries,
                        atomicity=AtomicityMode.ROLLBACK_ON_ERROR,
                    )
                )
    return requests


def build_explicit_path_write_requests(
    topology: dict[str, Any],
    forward_path: list[str],
    *,
    priority: int = BASELINE_PRIORITY,
) -> list[WriteRequest]:
    return _build_write_requests_from_rules(
        _forwarding_rules_for_path(topology, forward_path),
        priority=priority,
    )


def build_baseline_onos_rules(topology: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    return FlowAdapter().batch_convert(build_baseline_write_requests(topology))


def _canonical_hex(value: Any) -> str:
    """Normalise ONOS/P4Runtime byte encodings without changing semantics."""
    text = str(value or "").lower().replace("0x", "")
    if not text:
        return ""
    try:
        return format(int(text, 16), "x")
    except ValueError:
        return text


def _canonical_onos_rule(rule: dict[str, Any]) -> tuple[Any, ...]:
    """Canonicalize the PI portion of an ONOS rule for exact drift checks."""
    matches: list[tuple[Any, ...]] = []
    for criterion in (rule.get("selector") or {}).get("criteria") or []:
        for item in criterion.get("matches") or []:
            matches.append((
                str(item.get("field") or ""), str(item.get("match") or ""),
                _canonical_hex(item.get("value")),
                _canonical_hex(item.get("mask")),
                int(item.get("prefixLength") or item.get("prefix_len") or 0),
            ))
    action: dict[str, Any] = {}
    for instruction in (rule.get("treatment") or {}).get("instructions") or []:
        if instruction.get("subtype") == "ACTION":
            action = instruction
            break
    params = tuple(sorted(
        (str(key), _canonical_hex(value))
        for key, value in (action.get("actionParams") or {}).items()
    ))
    return (
        int(rule.get("tableId") or rule.get("table_id") or 0),
        int(rule.get("priority") or 0),
        tuple(sorted(matches)), str(action.get("actionId") or ""), params,
    )


def _canonical_p4_entry(entry: dict[str, Any], table_name: str) -> tuple[Any, ...]:
    """Represent a P4Runtime table entry using the same PI fields as ONOS."""
    from mcp_servers.p4info_registry import p4info_registry

    matches = []
    for field, item in (entry.get("matches") or {}).items():
        matches.append((
            str(field), str(item.get("match") or ""),
            _canonical_hex(item.get("value")),
            _canonical_hex(item.get("mask")), int(item.get("prefix_len") or 0),
        ))
    action_name = str(entry.get("action_name") or "")
    names_by_id = {
        int(p4info_registry.param_id(action_name, item["name"])): item["name"]
        for item in p4info_registry.action_params(action_name)
        if p4info_registry.param_id(action_name, item["name"]) is not None
    }
    params = tuple(sorted(
        (str(names_by_id.get(int(item.get("param_id") or 0), item.get("param_id"))),
         _canonical_hex(item.get("value")))
        for item in entry.get("action_params") or []
    ))
    return (
        int(entry.get("table_id") or 0), int(entry.get("priority") or 0),
        tuple(sorted(matches)), action_name, params,
    )


def _canonical_p4_expected(rule: dict[str, Any]) -> tuple[Any, ...]:
    """Map ONOS PI rule to P4Runtime's physical table representation.

    ONOS uses a controller-level priority (10 for the foundational entries),
    while the target is an LPM table whose P4Runtime entries correctly carry
    priority 0.  Priority remains part of the ONOS comparison but is not
    falsely treated as a physical forwarding mismatch.
    """
    table_id, _onos_priority, matches, action, params = _canonical_onos_rule(rule)
    return table_id, 0, matches, action, params


async def observe_baseline_connectivity(
    onos_client: Any | None = None,
    topology: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare generated foundational forwarding Desired State with two readers.

    The controller's flow inventory and direct P4Runtime table reads are
    intentionally independent.  A non-empty ONOS flow list is therefore not
    evidence that foundational reachability survived a controller restart.
    """
    if onos_client is None:
        from mcp_servers.onos_server import get_onos_client
        onos_client = get_onos_client()
    if topology is None:
        topology = await fetch_mininet_topology()
    topology = canonicalize_topology(topology)
    desired_by_device = build_baseline_onos_rules(topology)
    raw_observed = await onos_client.get_flows()
    observed_by_device: dict[str, list[dict[str, Any]]] = {}
    for flow in raw_observed.get("flows") or []:
        if str(flow.get("state") or "").upper() != "ADDED":
            continue
        observed_by_device.setdefault(str(flow.get("deviceId") or ""), []).append(flow)

    from api.p4_evidence import read_p4_table_entries
    from mcp_servers.p4info_registry import p4info_registry

    table_names: dict[tuple[str, int], str] = {}
    for device_id, rules in desired_by_device.items():
        for rule in rules:
            table_id = int(rule.get("tableId") or 0)
            table_names[(device_id, table_id)] = p4info_registry.table_name_for_id(table_id)
    reads = await asyncio.gather(*(
        read_p4_table_entries(device_id, table_name)
        for (device_id, _), table_name in table_names.items()
    ))
    p4_by_key = {
        (item["device_id"], int(item.get("table_id") or 0)): item
        for item in reads
    }

    comparisons: list[dict[str, Any]] = []
    for device_id, rules in desired_by_device.items():
        for rule in rules:
            expected = _canonical_onos_rule(rule)
            p4_expected = _canonical_p4_expected(rule)
            onos_matches = [
                item for item in observed_by_device.get(device_id, [])
                if _canonical_onos_rule(item) == expected
            ]
            p4 = p4_by_key.get((device_id, expected[0])) or {}
            p4_entries = p4.get("entries") or []
            table_name = str(p4.get("table_name") or table_names.get((device_id, expected[0])) or "")
            p4_matches = [
                item for item in p4_entries
                if _canonical_p4_entry(item, table_name) == p4_expected
            ] if p4.get("status") == "VERIFIED" else []
            if p4.get("status") != "VERIFIED":
                status = "P4_READ_FAILED"
            elif not onos_matches and not p4_matches:
                status = "MISSING_IN_ONOS_AND_P4"
            elif not onos_matches:
                status = "MISSING_IN_ONOS"
            elif not p4_matches:
                status = "MISSING_IN_P4"
            else:
                status = "VERIFIED"
            comparisons.append({
                "status": status, "device_id": device_id, "table_id": expected[0],
                "table": table_name, "priority": expected[1], "desired_rule": rule,
                "onos_flow_ids": [str(item.get("id") or "") for item in onos_matches],
                "p4_matching_entries": len(p4_matches),
                "p4_read_status": p4.get("status"),
                "p4_read_error": p4.get("error"),
            })
    return {
        "status": "VERIFIED" if all(item["status"] == "VERIFIED" for item in comparisons) else "DRIFTED",
        "desired_entry_count": len(comparisons),
        "verified_entry_count": sum(item["status"] == "VERIFIED" for item in comparisons),
        "missing_entry_count": sum(item["status"] != "VERIFIED" for item in comparisons),
        "comparisons": comparisons,
        "topology_source": topology.get("source"),
    }


async def repair_baseline_connectivity(
    observation: dict[str, Any], onos_client: Any | None = None,
) -> dict[str, Any]:
    """Repair only drifted foundational entries, safely replacing P4 drift.

    Matching entries are never posted again.  When ONOS reports a rule but
    P4Runtime does not, delete the controller-owned matching rule first and
    then reinstall it; this avoids introducing a duplicate physical LPM key.
    """
    if onos_client is None:
        from mcp_servers.onos_server import get_onos_client
        onos_client = get_onos_client()
    to_post: dict[str, list[dict[str, Any]]] = {}
    removed: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    for item in observation.get("comparisons") or []:
        status = str(item.get("status") or "")
        if status == "VERIFIED":
            continue
        if status == "P4_READ_FAILED":
            errors.append({"device_id": item.get("device_id"), "error": "P4_READ_FAILED"})
            continue
        device_id = str(item["device_id"])
        if status == "MISSING_IN_P4":
            for flow_id in item.get("onos_flow_ids") or []:
                await onos_client.delete_flows(device_id, [flow_id])
                removed.append({"device_id": device_id, "flow_id": flow_id})
        to_post.setdefault(device_id, []).append(dict(item["desired_rule"]))
    posted: dict[str, Any] = {}
    for device_id, rules in to_post.items():
        result = await onos_client.post_flows(device_id, rules)
        posted[device_id] = result
        if result.get("errors"):
            errors.append({"device_id": device_id, "errors": result["errors"]})
    return {
        "status": "SUCCESS" if not errors else "PARTIAL",
        "trigger": "CONTROLLER_RESTART_DRIFT",
        "missing_entries": sum(len(value) for value in to_post.values()),
        "replaced_onos_only_entries": removed,
        "posted": posted,
        "errors": errors,
    }


async def install_baseline_connectivity(
    onos_client: Any | None = None,
    topology: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if onos_client is None:
        from mcp_servers.onos_server import get_onos_client

        onos_client = get_onos_client()

    if topology is None:
        topology = await fetch_mininet_topology()
    topology = canonicalize_topology(topology)

    dynamic_topology_used = bool(topology and topology.get("status") == "ok")
    rules_by_device = build_baseline_onos_rules(topology)
    device_results: dict[str, Any] = {}
    posted = 0
    total = 0

    for device_id, rules in rules_by_device.items():
        result = await onos_client.post_flows(device_id, rules)
        device_results[device_id] = result
        posted += int(result.get("flows_posted") or 0)
        total += int(result.get("total") or len(rules))

    switch_id_config = await configure_p4_switch_ids(onos_client)

    errors = [
        {"device_id": device_id, "errors": result.get("errors")}
        for device_id, result in device_results.items()
        if result.get("errors")
    ]
    return {
        "status": "SUCCESS" if not errors and posted == total and switch_id_config.get("status") == "SUCCESS" else "PARTIAL",
        "flows_posted": posted,
        "total": total,
        "topology_source": topology.get("source") if dynamic_topology_used else "static_fallback",
        "dynamic_topology": dynamic_topology_used,
        "switch_id_config": switch_id_config,
        "devices": device_results,
        "errors": errors or None,
    }


async def install_baseline_with_retries(
    retries: int = 20,
    delay_seconds: float = 3.0,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            result = await install_baseline_connectivity()
            if result["status"] == "SUCCESS":
                logger.info("Baseline connectivity installed: %s", result)
            else:
                logger.warning("Baseline connectivity partially installed: %s", result)
            return result
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Baseline install attempt %s/%s failed: %s",
                attempt,
                retries,
                exc,
            )
            await asyncio.sleep(delay_seconds)

    raise RuntimeError(f"Baseline connectivity install failed: {last_error}")

"""Dynamic topology collection for the dashboard and baseline forwarding."""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Any

import aiohttp

from api.topology_paths import topology_paths

logger = logging.getLogger(__name__)


def build_topology_summary_payload(
    mininet_topology: dict[str, Any] | None,
    onos_devices_raw: dict[str, Any] | None,
    onos_links_raw: dict[str, Any] | None,
    onos_hosts_raw: dict[str, Any] | None,
) -> dict[str, int]:
    """Return topology counts without comparing incompatible link semantics."""

    mininet = mininet_topology or {}
    switches = list(mininet.get("switches") or [])
    hosts = list(mininet.get("hosts") or [])
    mininet_links = list(mininet.get("links") or [])
    onos_devices = list((onos_devices_raw or {}).get("devices") or [])
    onos_links = list((onos_links_raw or {}).get("links") or [])
    onos_hosts = list((onos_hosts_raw or {}).get("hosts") or [])

    physical_device_links = 0
    host_attachment_links = 0
    for link in mininet_links:
        endpoints = list(link.get("endpoints") or [])
        if len(endpoints) != 2:
            continue
        endpoint_types = {str(item.get("type") or "") for item in endpoints}
        if endpoint_types == {"switch"}:
            physical_device_links += 1
        elif endpoint_types == {"switch", "host"}:
            host_attachment_links += 1

    # If the Mininet probe is temporarily unavailable, infer the physical
    # count by deduplicating ONOS's two directed edges.
    if not switches and onos_links:
        pairs = {
            tuple(sorted((str((item.get("src") or {}).get("device")), str((item.get("dst") or {}).get("device")))))
            for item in onos_links
            if (item.get("src") or {}).get("device") and (item.get("dst") or {}).get("device")
        }
        physical_device_links = len(pairs)
    if not hosts and onos_hosts:
        host_attachment_links = sum(1 for item in onos_hosts if item.get("locations"))

    device_ids = {
        str(item.get("id"))
        for item in onos_devices
        if item.get("id")
    }
    directed_edges = [
        (str((item.get("src") or {}).get("device")), str((item.get("dst") or {}).get("device")))
        for item in onos_links
        if (item.get("src") or {}).get("device") and (item.get("dst") or {}).get("device")
    ]
    topology_sccs = _raw_scc_count(device_ids, directed_edges)
    device_count = len(switches) if switches else len(onos_devices)
    host_count = len(hosts) if hosts else len(onos_hosts)
    return {
        "devices": device_count,
        "directed_device_links": len(directed_edges),
        "physical_device_links": physical_device_links,
        "host_attachment_links": host_attachment_links,
        "logical_total_links": physical_device_links + host_attachment_links,
        "hosts": host_count,
        "topology_sccs": topology_sccs,
    }


def _raw_scc_count(device_ids: set[str], edges: list[tuple[str, str]]) -> int:
    if not device_ids:
        return 0
    graph = {item: [] for item in device_ids}
    reverse = {item: [] for item in device_ids}
    for source, target in edges:
        if source in graph and target in graph:
            graph[source].append(target)
            reverse[target].append(source)
    order: list[str] = []
    seen: set[str] = set()

    def visit(node: str, adjacency: dict[str, list[str]], output: list[str] | None = None) -> None:
        if node in seen:
            return
        seen.add(node)
        for target in adjacency[node]:
            visit(target, adjacency, output)
        if output is not None:
            output.append(node)

    for node in sorted(device_ids):
        visit(node, graph, order)
    seen.clear()
    components = 0
    for node in reversed(order):
        if node in seen:
            continue
        components += 1
        visit(node, reverse)
    return components

# KNOWN_NODE_POSITIONS removed — procedural layout in _node_position()
# automatically positions any topology without hardcoded coordinates.


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_device_id(device_id: str | None) -> str:
    if not device_id:
        return ""
    if device_id.startswith("device:"):
        return device_id.removeprefix("device:")
    return device_id


def denormalize_device_id(node_id: str | None) -> str:
    if not node_id:
        return ""
    if node_id.startswith("device:"):
        return node_id
    if node_id.startswith("bmv2:"):
        return f"device:{node_id}"
    return node_id


async def _probe_get(path: str) -> dict[str, Any] | None:
    from api._probe_session import get_probe_session

    session = await get_probe_session()
    return await session.get(path)


async def fetch_mininet_topology() -> dict[str, Any] | None:
    return await _probe_get("/topology")


def canonicalize_topology(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Adapt Probe, ONOS, API and recovery snapshots to one graph contract.

    The runtime has historically exposed a Mininet graph for forwarding and a
    separately normalised graph for the UI.  The latter used ``bmv2:N`` as a
    switch node id while retaining Mininet names in link endpoints, which made
    a perfectly healthy graph look disconnected to consumers.  This adapter
    is deliberately the single boundary: consumers receive runtime names as
    graph node IDs, and device/host identities remain explicit attributes.
    """
    source = dict(payload or {})
    if isinstance(source.get("canonical"), dict):
        source = dict(source["canonical"])
    elif isinstance(source.get("mininet"), dict):
        source = dict(source["mininet"])

    raw_switches = list(source.get("switches") or [])
    raw_hosts = list(source.get("hosts") or [])
    raw_links = list(source.get("links") or [])
    onos = source.get("onos") if isinstance(source.get("onos"), dict) else {}
    onos_devices = list(onos.get("devices") or source.get("devices") or [])
    onos_links = list(onos.get("links") or [])
    onos_hosts = list(onos.get("hosts") or [])

    nodes: list[dict[str, Any]] = []
    runtime_by_device: dict[str, str] = {}
    for switch in raw_switches:
        runtime_name = str(switch.get("name") or switch.get("runtime_name") or "")
        device_id = str(switch.get("device_id") or switch.get("id") or "")
        if not runtime_name:
            runtime_name = device_id
        if not runtime_name:
            continue
        if device_id:
            runtime_by_device[device_id] = runtime_name
        nodes.append({
            "node_id": runtime_name, "node_type": "switch",
            "device_id": device_id or None, "host_id": None,
            "runtime_name": runtime_name,
        })
    if not nodes:
        for device in onos_devices:
            device_id = str(device.get("id") or device.get("device_id") or "")
            if not device_id:
                continue
            runtime_name = str(device.get("name") or device.get("runtime_name") or device_id)
            runtime_by_device[device_id] = runtime_name
            nodes.append({
                "node_id": runtime_name, "node_type": "switch",
                "device_id": device_id, "host_id": None,
                "runtime_name": runtime_name,
            })
    for host in raw_hosts:
        host_id = str(host.get("name") or host.get("host_id") or host.get("id") or "")
        if not host_id:
            continue
        nodes.append({
            "node_id": host_id, "node_type": "host", "host_id": host_id,
            "device_id": None, "runtime_name": str(host.get("runtime_name") or host_id),
        })
    if not raw_hosts:
        for host in onos_hosts:
            host_id = str(host.get("id") or host.get("host_id") or "")
            if host_id:
                nodes.append({"node_id": host_id, "node_type": "host", "host_id": host_id,
                              "device_id": None, "runtime_name": host_id})

    canonical_links: list[dict[str, Any]] = []
    for index, raw_link in enumerate(raw_links):
        endpoints = list(raw_link.get("endpoints") or [])
        if len(endpoints) != 2:
            continue
        normalized: list[dict[str, Any]] = []
        for endpoint in endpoints:
            node_type = str(endpoint.get("type") or "switch")
            device_id = str(endpoint.get("device_id") or "")
            runtime_name = str(endpoint.get("node") or endpoint.get("runtime_name") or "")
            if node_type == "switch" and not runtime_name:
                runtime_name = runtime_by_device.get(device_id, device_id)
            node_id = runtime_name
            if not node_id:
                normalized = []
                break
            normalized.append({
                **endpoint, "node": node_id, "node_id": node_id,
                "node_type": node_type, "runtime_name": runtime_name,
                "device_id": device_id or endpoint.get("device_id") or None,
                "host_id": node_id if node_type == "host" else None,
            })
        if len(normalized) == 2:
            canonical_links.append({
                **raw_link, "id": str(raw_link.get("id") or f"link-{index}"),
                "state": str(raw_link.get("state") or "ACTIVE").upper(),
                "capacity_mbps": raw_link.get("capacity_mbps", raw_link.get("bandwidth_mbps")),
                "bandwidth_mbps": raw_link.get("bandwidth_mbps", raw_link.get("capacity_mbps")),
                "endpoints": normalized,
            })
    if not canonical_links:
        for index, link in enumerate(onos_links):
            src, dst = dict(link.get("src") or {}), dict(link.get("dst") or {})
            src_device, dst_device = str(src.get("device") or ""), str(dst.get("device") or "")
            if not src_device or not dst_device:
                continue
            canonical_links.append({
                "id": str(link.get("id") or f"onos-{index}"), "state": str(link.get("state") or "ACTIVE").upper(),
                "cost": 1.0, "capacity_mbps": link.get("capacity_mbps"), "bandwidth_mbps": link.get("capacity_mbps"),
                "endpoints": [
                    {"node": runtime_by_device.get(src_device, src_device), "node_id": runtime_by_device.get(src_device, src_device), "node_type": "switch", "device_id": src_device, "runtime_name": runtime_by_device.get(src_device, src_device), "port": src.get("port")},
                    {"node": runtime_by_device.get(dst_device, dst_device), "node_id": runtime_by_device.get(dst_device, dst_device), "node_type": "switch", "device_id": dst_device, "runtime_name": runtime_by_device.get(dst_device, dst_device), "port": dst.get("port")},
                ],
            })

    return {
        "schema": "idn.canonical_topology.v1",
        "status": str(source.get("status") or "ok").lower(),
        "topology_name": source.get("topology_name") or source.get("name") or "runtime",
        "nodes": nodes, "links": canonical_links,
        # Compatibility views carry the same identities; they are not a
        # second graph representation.
        "hosts": raw_hosts, "switches": raw_switches,
    }


def _onos_device_map(devices_raw: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    devices = (devices_raw or {}).get("devices", []) or []
    return {device.get("id"): device for device in devices if device.get("id")}


def _node_position(node_id: str, node_type: str, index: int, total: int) -> tuple[float, float]:
    """Compute a visual layout position for a topology node.

    Hosts are placed in side columns; switches are arranged on an ellipse.
    Works for any topology without hardcoded coordinates.
    """
    if node_type == "host":
        x = 10 if index % 2 == 0 else 90
        y = 24 + (index * 14) % 54
        return x, y

    radius_x = 28
    radius_y = 22
    angle = (2 * math.pi * index) / max(total, 1) - math.pi / 2
    return 50 + radius_x * math.cos(angle), 50 + radius_y * math.sin(angle)


def _switch_role(name: str) -> str:
    if "core" in name:
        return "P4 core switch"
    if "agg" in name:
        return "Aggregation switch"
    if "access" in name:
        return "Access switch"
    return "P4 switch"


def _host_role(name: str) -> str:
    if "research" in name:
        return "Research host"
    if name.endswith("_dc") or "dc" in name:
        return "Datacenter host"
    return "Host"


def build_frontend_topology(
    mininet_topology: dict[str, Any] | None,
    onos_devices: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    switches = (mininet_topology or {}).get("switches", []) or []
    hosts = (mininet_topology or {}).get("hosts", []) or []
    raw_links = (mininet_topology or {}).get("links", []) or []

    nodes: list[dict[str, Any]] = []
    switch_total = len(switches)
    host_total = len(hosts)

    # ── Demo mode: read failed switches from env var AND runtime file ──
    _demo_failed = set()
    if os.getenv("IDN_DEMO_MODE", "").lower() == "true":
        _demo_failed = set(
            s.strip() for s in os.getenv("IDN_DEMO_FAILED_SWITCHES", "").split(",") if s.strip()
        )
        # Also check runtime file (written by demo scripts without restart)
        _demo_failed_file = "/app/data/demo_failed_switches.txt"
        if os.path.exists(_demo_failed_file):
            try:
                with open(_demo_failed_file) as fh:
                    for line in fh:
                        s = line.strip()
                        if s:
                            _demo_failed.add(s)
            except Exception:
                pass

    for index, switch in enumerate(switches):
        raw_device_id = switch.get("device_id")
        node_id = normalize_device_id(raw_device_id) or switch.get("name")
        switch_name = switch.get("name") or ""
        onos_device = onos_devices.get(raw_device_id or "")
        available = onos_device.get("available") if onos_device else None
        # Demo mode: override availability for specified switches
        if switch_name in _demo_failed:
            available = False
        x, y = _node_position(node_id, "switch", index, switch_total)
        nodes.append({
            "id": node_id,
            "rawId": raw_device_id,
            "name": switch_name or node_id,
            "role": _switch_role(switch_name or ""),
            "type": "switch",
            "state": "failed" if available is False else "normal",
            "x": round(x, 2),
            "y": round(y, 2),
            "kpi": "online" if available else ("offline" if available is False else "-"),
            "detail": f"{raw_device_id or node_id}, gRPC {switch.get('grpc_port')}",
            "ports": switch.get("ports", []),
        })

    for index, host in enumerate(hosts):
        node_id = host.get("name")
        x, y = _node_position(node_id, "host", index, host_total)
        nodes.append({
            "id": node_id,
            "name": node_id,
            "role": _host_role(node_id or ""),
            "type": "host",
            "state": "normal",
            "x": round(x, 2),
            "y": round(y, 2),
            "kpi": host.get("ip") or "-",
            "detail": f"{host.get('ip') or '-'} / {host.get('mac') or '-'}",
            "interface": host.get("interface"),
        })

    frontend_links: list[dict[str, Any]] = []
    detailed_links: list[dict[str, Any]] = []
    for index, link in enumerate(raw_links):
        endpoints = link.get("endpoints", [])
        if len(endpoints) != 2:
            continue
        front_endpoints = []
        for endpoint in endpoints:
            endpoint_id = normalize_device_id(endpoint.get("device_id")) if endpoint.get("type") == "switch" else endpoint.get("node")
            front_endpoints.append(endpoint_id)

        role = link.get("role", "primary")
        tone = "degraded" if role == "backup" else "primary"
        link_id = link.get("id") or f"link-{index}"

        # Extract port numbers from endpoints
        from_port = None
        to_port = None
        for ep in endpoints:
            if ep.get("type") == "switch" and ep.get("port") is not None:
                ep_id = normalize_device_id(ep.get("device_id")) if ep.get("type") == "switch" else ep.get("node")
                if ep_id == front_endpoints[0]:
                    from_port = ep.get("port")
                else:
                    to_port = ep.get("port")

        frontend_links.append({
            "id": link_id,
            "from": front_endpoints[0],
            "to": front_endpoints[1],
            "tone": tone,
            "state": "normal",
            "role": role,
            "cost": link.get("cost", 1.0),
            "fromPort": from_port,
            "toPort": to_port,
            "bandwidth_mbps": link.get("bandwidth_mbps"),
            "delay_ms": link.get("delay_ms"),
            "pathRole": _infer_path_role(front_endpoints, role),
        })
        detailed_links.append({
            **link,
            "from": front_endpoints[0],
            "to": front_endpoints[1],
        })

    # ── Inject INT per-hop latency into link delay_ms ──
    _inject_int_latency(frontend_links, detailed_links, switches)

    return {
        "nodes": nodes,
        "links": frontend_links,
        "detailed_links": detailed_links,
    }


def _inject_int_latency(
    frontend_links: list[dict[str, Any]],
    detailed_links: list[dict[str, Any]],
    switches: list[dict[str, Any]],
) -> None:
    """Inject real-time INT per-hop latency into topology link ``delay_ms``.

    Reads the latest INT probe from the telemetry cache and maps each
    hop's ``(device_id, egress_port)`` to a frontend link's
    ``(from, fromPort)``.  Falls back gracefully when INT data is stale
    or unavailable.
    """
    import time

    try:
        from api.telemetry import _last_probe_cache, _PROBE_CACHE_TTL_SECONDS
    except ImportError:
        return

    cached = _last_probe_cache.get("int")
    if not cached:
        return
    age = time.monotonic() - cached.get("monotonic", 0.0)
    if age > _PROBE_CACHE_TTL_SECONDS:
        return

    int_data = cached.get("payload") or {}
    from api.int_quality import TRUSTED, decision_safe_int_probe

    int_data = decision_safe_int_probe(int_data)
    if int_data.get("quality_status") != TRUSTED or not int_data.get("used_for_decision"):
        return
    hop_records = int_data.get("hop_records") or []
    if not hop_records:
        return

    # Build switch_name → device_id lookup
    name_to_device: dict[str, str] = {}
    for sw in switches:
        name = sw.get("name")
        did = sw.get("device_id")
        if name and did:
            name_to_device[name] = did

    # Build (from, fromPort) → link index for fast lookup
    link_index: dict[tuple[str, int], int] = {}
    for idx, link in enumerate(frontend_links):
        port = link.get("fromPort")
        if isinstance(port, int):
            link_index[(link["from"], port)] = idx

    updated = 0
    for hop in hop_records:
        hop_device = hop.get("device_id") or name_to_device.get(
            hop.get("switch_name") or ""
        )
        if not hop_device:
            continue
        # Normalize: strip "device:" prefix if present
        norm_device = hop_device.replace("device:", "") if hop_device.startswith("device:") else hop_device
        egress = hop.get("egress_port")
        if egress is None:
            continue
        try:
            egress = int(egress)
        except (TypeError, ValueError):
            continue

        latency_ns = hop.get("hop_latency_ns")
        if latency_ns is None:
            continue

        idx = link_index.get((norm_device, egress))
        if idx is None:
            continue

        delay_ms = round(float(latency_ns) / 1_000_000, 3)
        frontend_links[idx]["delay_ms"] = delay_ms
        if idx < len(detailed_links):
            detailed_links[idx]["delay_ms"] = delay_ms
        updated += 1

    if updated:
        import logging
        logging.getLogger(__name__).info(
            "Injected INT latency into %d topology links (probe age=%.1fs)",
            updated, age,
        )


def _infer_path_role(endpoints: list[str], link_role: str) -> str | None:
    """Heuristic: label the link with its likely path role for the UI."""
    if link_role == "backup":
        return "备份路径"
    has_host = any(ep.startswith("h_") for ep in endpoints)
    has_core = any("core" in ep for ep in endpoints)
    has_agg = any("agg" in ep for ep in endpoints)
    has_access = any("access" in ep for ep in endpoints)

    if has_host and (has_access or has_agg):
        return "终端接入"
    if has_access and has_agg:
        return "汇聚层"
    if has_agg and has_core:
        return "骨干层"
    if has_core:
        return "核心转发"
    return None


def _fallback_frontend_from_onos(onos_devices: dict[str, dict[str, Any]]) -> dict[str, Any]:
    switches = [
        {
            "name": normalize_device_id(device_id),
            "device_id": device_id,
            "grpc_port": None,
            "ports": [],
        }
        for device_id in sorted(onos_devices)
    ]
    return build_frontend_topology({"switches": switches, "hosts": [], "links": []}, onos_devices)


async def collect_current_topology(
    onos_client: Any | None = None,
    fields: str = "full",
) -> dict[str, Any]:
    """Collect the current network topology snapshot.

    Args:
        onos_client: Optional ONOSClient instance.
        fields: "full" (default, all data) or "frontend" (nodes/links only, lightweight).
    """
    if onos_client is None:
        from mcp_servers.onos_server import get_onos_client
        onos_client = get_onos_client()

    mininet_topology = await fetch_mininet_topology()
    mininet_ok = bool(mininet_topology and mininet_topology.get("status") == "ok")

    # ── Frontend-only fast path ──
    if fields == "frontend":
        onos_devices_raw = None
        try:
            onos_devices_raw = await onos_client.get_devices()
        except Exception as exc:
            logger.warning("ONOS devices collection failed: %s", exc)
        onos_devices = _onos_device_map(onos_devices_raw)
        frontend = (
            build_frontend_topology(mininet_topology, onos_devices)
            if mininet_ok
            else _fallback_frontend_from_onos(onos_devices)
        )
        return {
            "status": "ok" if mininet_ok or onos_devices_raw else "unavailable",
            "source": "mininet_probe+onos_rest" if mininet_ok and onos_devices_raw else "mininet_probe" if mininet_ok else "onos_rest",
            "timestamp": _utc_now(),
            "frontend": {
                "nodes": frontend["nodes"],
                "links": frontend["links"],
            },
            "path_config": topology_paths.validate_runtime(mininet_topology) if mininet_ok else topology_paths.report(),
        }

    # ── Full topology collection ──
    onos_devices_raw = None
    onos_links_raw = None
    onos_hosts_raw = None
    onos_error = None
    try:
        onos_devices_raw = await onos_client.get_devices()
        try:
            onos_links_raw = await onos_client.get_links()
        except Exception as exc:
            onos_links_raw = {"links": [], "error": str(exc)}
        try:
            onos_hosts_raw = await onos_client.get_hosts()
        except Exception as exc:
            onos_hosts_raw = {"hosts": [], "error": str(exc)}
    except Exception as exc:
        onos_error = str(exc)
        logger.warning("ONOS topology collection failed: %s", exc)

    onos_devices = _onos_device_map(onos_devices_raw)
    source_parts = []
    if mininet_ok:
        source_parts.append("mininet_probe")
    if onos_devices_raw:
        source_parts.append("onos_rest")

    frontend = (
        build_frontend_topology(mininet_topology, onos_devices)
        if mininet_ok
        else _fallback_frontend_from_onos(onos_devices)
    )

    status = "ok" if mininet_ok or onos_devices_raw else "unavailable"
    topology_summary = build_topology_summary_payload(
        mininet_topology if mininet_ok else None,
        onos_devices_raw,
        onos_links_raw,
        onos_hosts_raw,
    )
    path_config = (
        topology_paths.validate_runtime(mininet_topology)
        if mininet_ok
        else topology_paths.report()
    )
    canonical = canonicalize_topology(mininet_topology) if mininet_ok else canonicalize_topology({
        "status": status,
        "onos": {"devices": (onos_devices_raw or {}).get("devices") or [],
                 "links": (onos_links_raw or {}).get("links") or [],
                 "hosts": (onos_hosts_raw or {}).get("hosts") or []},
    })
    return {
        "status": status,
        "source": "+".join(source_parts) or "unavailable",
        "timestamp": _utc_now(),
        "mininet": mininet_topology,
        "onos": {
            "devices": onos_devices_raw.get("devices", []) if onos_devices_raw else [],
            "links": onos_links_raw.get("links", []) if onos_links_raw else [],
            "hosts": onos_hosts_raw.get("hosts", []) if onos_hosts_raw else [],
            "error": onos_error,
        },
        "switches": (mininet_topology or {}).get("switches", []) if mininet_ok else [],
        "hosts": (mininet_topology or {}).get("hosts", []) if mininet_ok else [],
        "links": frontend["detailed_links"],
        "canonical": canonical,
        "topology_summary": topology_summary,
        "path_config": path_config,
        "frontend": {
            "nodes": frontend["nodes"],
            "links": frontend["links"],
        },
    }

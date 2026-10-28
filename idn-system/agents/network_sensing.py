"""
agents/network_sensing.py
==========================

网络感知 Agent —— STAV 闭环的 SENSE 阶段执行者。

# 系统角色
通过 MCP 协议调用 ONOS REST API 采集当前网络状态：
- 拓扑结构（设备列表、链路信息）
- 主机信息（MAC、IP、接入位置）
- 运行时流表项（用于冲突检测和存量对比）

支持短时缓存（IDN_SENSE_CACHE_SECS），避免同一流水线内重复拉取。

# 主要依赖
- agents/base_agent.py：MCP 客户端基类
- mcp_servers/onos_server.py：ONOS REST API 封装

# 与其他模块交互
- orchestrator/nodes/sense_node.py：调用 collect_snapshot()
- agents/policy_conflict_checker.py：使用快照中的 flows 做存量对比
- agents/policy_compiler.py：使用快照中的拓扑信息做设备选择
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from agents.base_agent import BaseAgent
from models.network import (
    DeviceInfo,
    DeviceStatus,
    FlowEntry,
    HostInfo,
    LinkInfo,
    NetworkSnapshot,
    TopologySummary,
    build_topology_summary,
    parse_device_availability,
)

logger = logging.getLogger(__name__)

# ── 拓扑快照短时缓存 ──
# 同一意图流水线内多次 sense 时无需重复拉取 ONOS 拓扑。
# 设置 IDN_SENSE_CACHE_SECS=0 可完全关闭缓存。
_SNAPSHOT_CACHE_TTL_SECS = int(os.getenv("IDN_SENSE_CACHE_SECS", "30"))
_snapshot_cache: dict[str, tuple[float, NetworkSnapshot]] = {}


def invalidate_snapshot_cache() -> None:
    """Discard cached policy/topology snapshots after a policy commit/rollback."""
    _snapshot_cache.clear()


class NetworkSensingAgent(BaseAgent):
    """Build the topology snapshot consumed by planning and conflict checking."""

    agent_id = "network_sensing"
    description = "Collect current topology, hosts, and runtime flows from ONOS"

    async def collect_snapshot(self) -> NetworkSnapshot:
        # ── 短时缓存：TTL 内直接返回上一份快照 ──
        cache_key = "current"
        if _SNAPSHOT_CACHE_TTL_SECS > 0:
            entry = _snapshot_cache.get(cache_key)
            if entry is not None:
                cached_ts, cached_snapshot = entry
                age = time.monotonic() - cached_ts
                if age < _SNAPSHOT_CACHE_TTL_SECS:
                    logger.info(
                        "[%s] 使用缓存拓扑 (age=%.1fs, ttl=%ds)",
                        self.agent_id, age, _SNAPSHOT_CACHE_TTL_SECS,
                    )
                    return cached_snapshot

        # Sequential MCP reads — avoid asyncio.gather which creates sub-tasks
        # that cause anyio cancel-scope cross-task RuntimeError when the
        # shared ONOS MCP subprocess is later closed from the main task.
        topology = await self._read_mcp_resource("onos", "/topology/current")
        hosts = await self._read_mcp_resource("onos", "/hosts")
        flows = await self._read_mcp_resource("onos", "/flows")
        flow_items = flows.get("flows", [])
        devices = [DeviceInfo(**item) for item in topology.get("devices", [])]
        links = [LinkInfo(**item) for item in topology.get("links", [])]
        host_items = [HostInfo(**item) for item in hosts.get("hosts", [])]
        topology_summary = build_topology_summary(
            devices=devices,
            directed_links=links,
            hosts=host_items,
        )

        if not links or not host_items or any(not host.gateway_mac for host in host_items):
            merged = await self._collect_merged_topology()
            if merged:
                devices = devices or _devices_from_merged_topology(merged)
                links = links or _links_from_merged_topology(merged)
                derived_hosts = _hosts_from_merged_topology(merged)
                if not host_items:
                    host_items = derived_hosts
                else:
                    gateway_by_mac = {
                        host.mac.lower(): host.gateway_mac
                        for host in derived_hosts
                        if host.mac and host.gateway_mac
                    }
                    for host in host_items:
                        if not host.gateway_mac:
                            host.gateway_mac = gateway_by_mac.get(host.mac.lower())
                merged_summary = merged.get("topology_summary")
                if merged_summary:
                    topology_summary = TopologySummary(**merged_summary)
        # When no topology data is available from ONOS or Mininet probe,
        # return empty links/hosts — the caller must handle this gracefully.
        # Hardcoded fallback topology has been removed; it masked deployment
        # issues where ONOS or Mininet were not reachable.

        # ── Load existing policies from the policy store ──
        try:
            from persistence.policy_store import PolicyStore
            existing = PolicyStore().list_all()
        except Exception:
            existing = []

        snapshot = NetworkSnapshot(
            devices=devices,
            links=links,
            hosts=host_items,
            flows=[FlowEntry(**item) for item in flow_items],
            existing_policies=existing,
            topology_summary=topology_summary,
        )

        if _SNAPSHOT_CACHE_TTL_SECS > 0:
            _snapshot_cache[cache_key] = (time.monotonic(), snapshot)

        return snapshot

    async def _collect_merged_topology(self) -> dict[str, Any] | None:
        try:
            from api.topology import collect_current_topology

            topology = await collect_current_topology()
        except Exception as exc:
            logger.error("[%s] topology collection failed: %s", self.agent_id, exc)
            return None
        if topology.get("status") != "ok":
            logger.warning("[%s] topology status is not ok: %s", self.agent_id, topology.get("error") or topology.get("status"))
            return None
        return topology


def _devices_from_merged_topology(topology: dict[str, Any]) -> list[DeviceInfo]:
    onos_devices = {
        item.get("id") or item.get("device_id"): item
        for item in (topology.get("onos") or {}).get("devices", [])
        if item.get("id") or item.get("device_id")
    }
    devices: list[DeviceInfo] = []
    for switch in topology.get("switches") or []:
        device_id = switch.get("device_id") or _device_id_from_node(switch.get("name"))
        if not device_id:
            continue
        onos_device = onos_devices.get(device_id) or {}
        available = onos_device.get("available")
        status = parse_device_availability(available)
        devices.append(
            DeviceInfo(
                device_id=device_id,
                type="SWITCH",
                status=status,
                present_in_topology=True,
                controller_available=(
                    True if status == DeviceStatus.AVAILABLE
                    else False if status == DeviceStatus.UNAVAILABLE
                    else None
                ),
                grpc_port=switch.get("grpc_port"),
                ports=switch.get("ports") or [],
                annotations={"source": topology.get("source", "mininet_probe+onos_rest")},
                availability_source=(
                    "ONOS_DEVICE_AVAILABLE"
                    if available is not None
                    else "MININET_TOPOLOGY_WITHOUT_ONOS_AVAILABILITY"
                ),
                availability_reason_code=(
                    None if available is not None else "DEVICE_AVAILABILITY_NOT_OBSERVED"
                ),
                raw_availability=available,
            )
        )
    return devices


def _links_from_merged_topology(topology: dict[str, Any]) -> list[LinkInfo]:
    links: list[LinkInfo] = []
    for index, link in enumerate(topology.get("links") or []):
        endpoints = link.get("endpoints") or []
        if len(endpoints) != 2:
            continue
        src, dst = endpoints
        links.append(
            LinkInfo(
                link_id=link.get("id") or f"mininet-link-{index}",
                src_device=_endpoint_id(src),
                src_port=_endpoint_port(src),
                dst_device=_endpoint_id(dst),
                dst_port=_endpoint_port(dst),
                bandwidth_mbps=link.get("bandwidth_mbps"),
                delay_ms=link.get("delay_ms"),
                state=link.get("state", "ACTIVE"),
            )
        )
    return links


def _hosts_from_merged_topology(topology: dict[str, Any]) -> list[HostInfo]:
    locations = _host_locations(topology.get("links") or [])
    hosts: list[HostInfo] = []
    for host in topology.get("hosts") or []:
        host_id = host.get("name")
        if not host_id:
            continue
        location = locations.get(host_id, {})
        ip = host.get("ip")
        hosts.append(
            HostInfo(
                host_id=host_id,
                mac=host.get("mac") or "",
                ip_addresses=[_strip_prefix(ip)] if ip else [],
                location_device=location.get("device_id"),
                location_port=location.get("port"),
                gateway_mac=host.get("gateway_mac"),
            )
        )
    return hosts


def _host_locations(links: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    locations: dict[str, dict[str, str]] = {}
    for link in links:
        endpoints = link.get("endpoints") or []
        if len(endpoints) != 2:
            continue
        host_ep = next((item for item in endpoints if item.get("type") == "host"), None)
        switch_ep = next((item for item in endpoints if item.get("type") == "switch"), None)
        if not host_ep or not switch_ep:
            continue
        host_id = host_ep.get("node")
        if not host_id:
            continue
        locations[host_id] = {
            "device_id": switch_ep.get("device_id") or _device_id_from_node(switch_ep.get("node")),
            "port": _endpoint_port(switch_ep),
        }
    return locations


def _endpoint_id(endpoint: dict[str, Any]) -> str:
    if endpoint.get("type") == "switch":
        return endpoint.get("device_id") or _device_id_from_node(endpoint.get("node")) or endpoint.get("node") or "unknown"
    return endpoint.get("node") or endpoint.get("device_id") or "unknown"


def _endpoint_port(endpoint: dict[str, Any]) -> str:
    port = endpoint.get("port")
    if port is not None:
        return str(port)
    return str(endpoint.get("interface") or "")


# _known_research_dc_topology, _link, _host removed —
# hardcoded topology fallback masked deployment issues.
# When ONOS and Mininet probe are both unavailable, the caller
# must handle empty topology data gracefully.


def _strip_prefix(value: str) -> str:
    return str(value).split("/", 1)[0]


def _device_id_from_node(node: str | None) -> str | None:
    if not node:
        return None
    if node.startswith("device:"):
        return node
    if node.startswith("bmv2:"):
        return f"device:{node}"
    return None

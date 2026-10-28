"""
models/network.py
=================

网络拓扑与设备状态数据模型。

# 系统角色
定义 IDN-System 对物理/虚拟网络的统一抽象。NetworkSnapshot
是 SENSE 阶段的核心输出，为后续所有阶段提供网络状态上下文。

# 主要类型
- DeviceInfo：交换机/设备信息（ID、端口、P4 程序、状态）
- LinkInfo：链路信息（源/目的设备端口、带宽、时延）
- HostInfo：终端主机信息（MAC、IP、接入位置）
- FlowEntry：当前流表项快照（用于冲突检测和存量策略对比）
- NetworkSnapshot：网络快照聚合（设备 + 链路 + 主机 + 流表 + 存量策略）

# 主要依赖
- pydantic.BaseModel / Field
- datetime（快照时间戳）

# 与其他模块交互
- mcp_servers/onos_server.py：通过 ONOS REST API 采集拓扑数据
- agents/network_sensing.py：封装 MCP 调用，组装 NetworkSnapshot
- agents/policy_conflict_checker.py：基于 NetworkSnapshot 检测策略冲突
- agents/policy_compiler.py：基于拓扑信息选择目标设备和路径

# 设计意图
- 每次策略变更前重新采集快照，确保基于最新网络状态决策
- existing_policies 字段存储存量策略，供冲突检测比对
- 提供便捷查询方法（get_device, get_links_for_device）减少遍历代码
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DeviceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"


def parse_device_availability(value: Any) -> DeviceStatus:
    """Normalize controller availability without treating absent data as down.

    ONOS normally emits a JSON boolean, but cached/proxied integrations have
    historically returned strings.  Only an explicit false value proves an
    unavailable device; missing or malformed evidence remains UNKNOWN.
    """
    if value is True or value == 1:
        return DeviceStatus.AVAILABLE
    if value is False or value == 0:
        return DeviceStatus.UNAVAILABLE
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "available", "online"}:
            return DeviceStatus.AVAILABLE
        if normalized in {"false", "0", "unavailable", "offline"}:
            return DeviceStatus.UNAVAILABLE
    return DeviceStatus.UNKNOWN


class DeviceInfo(BaseModel):
    """交换机/设备信息"""
    device_id: str = Field(..., description="设备唯一标识 (e.g. device:bmv2:1)")
    type: str = Field(default="SWITCH", description="设备类型")
    status: DeviceStatus = Field(default=DeviceStatus.UNKNOWN)
    present_in_topology: bool = Field(default=True)
    controller_available: Optional[bool] = Field(default=None)
    p4runtime_reachable: Optional[bool] = Field(default=None)
    observed_at: Optional[datetime] = Field(default=None)
    availability_source: Optional[str] = Field(default=None)
    availability_reason_code: Optional[str] = Field(default=None)
    raw_availability: Any = Field(default=None)
    grpc_port: Optional[int] = Field(default=None, description="P4Runtime gRPC端口")
    p4_program: Optional[str] = Field(default=None, description="当前加载的P4程序名")
    ports: List[Dict[str, Any]] = Field(default_factory=list, description="端口列表")
    annotations: Optional[Dict[str, str]] = Field(default=None)


class LinkInfo(BaseModel):
    """链路信息"""
    link_id: str = Field(..., description="链路唯一标识")
    src_device: str = Field(..., description="源设备ID")
    src_port: str = Field(..., description="源端口")
    dst_device: str = Field(..., description="目的设备ID")
    dst_port: str = Field(..., description="目的端口")
    bandwidth_mbps: Optional[int] = Field(default=None, description="链路带宽 (Mbps)")
    delay_ms: Optional[float] = Field(default=None, description="链路时延 (ms)")
    state: str = Field(default="ACTIVE", description="链路状态")


class HostInfo(BaseModel):
    """终端主机信息"""
    host_id: str = Field(..., description="主机唯一标识")
    mac: str = Field(..., description="MAC地址")
    ip_addresses: List[str] = Field(default_factory=list, description="IP地址列表")
    location_device: Optional[str] = Field(default=None, description="接入设备ID")
    location_port: Optional[str] = Field(default=None, description="接入端口")
    gateway_mac: Optional[str] = Field(default=None, description="所属子网的虚拟网关 MAC")


class FlowEntry(BaseModel):
    """流表项信息"""
    flow_id: str = Field(..., description="流表项ID")
    device_id: str = Field(..., description="所在设备ID")
    table_name: str = Field(..., description="表名")
    priority: int = Field(default=0)
    timeout: int = Field(default=0, description="超时时间(秒), 0为永久")
    is_permanent: bool = Field(default=True)
    packets: int = Field(default=0, description="命中包数")
    bytes: int = Field(default=0, description="命中字节数")


class TopologySummary(BaseModel):
    """Topology counts with explicit, non-interchangeable semantics.

    ONOS reports directed switch-to-switch links while Mininet reports each
    physical link once.  Host attachment edges are a third category.  Keeping
    these values separate prevents a valid ``16 directed == 8 physical``
    topology from being flagged because it also has ``13 logical`` edges.
    """

    devices: int = Field(default=0, ge=0)
    directed_device_links: int = Field(default=0, ge=0)
    physical_device_links: int = Field(default=0, ge=0)
    host_attachment_links: int = Field(default=0, ge=0)
    logical_total_links: int = Field(default=0, ge=0)
    hosts: int = Field(default=0, ge=0)
    topology_sccs: int = Field(default=0, ge=0)


def build_topology_summary(
    *,
    devices: List[DeviceInfo] | None = None,
    directed_links: List[LinkInfo] | None = None,
    hosts: List[HostInfo] | None = None,
) -> TopologySummary:
    """Build a summary from the controller's directed device graph.

    Physical switch links are deduplicated by their unordered endpoint pair.
    A discovered host with a switch location contributes one logical host
    attachment edge even though ONOS does not expose it through ``/links``.
    """

    device_items = list(devices or [])
    link_items = list(directed_links or [])
    host_items = list(hosts or [])
    device_ids = {item.device_id for item in device_items}
    directed_device_links = [
        item
        for item in link_items
        if item.src_device in device_ids and item.dst_device in device_ids
    ]
    physical_pairs = {
        tuple(sorted((item.src_device, item.dst_device)))
        for item in directed_device_links
        if item.src_device != item.dst_device
    }
    attached_hosts = sum(1 for item in host_items if item.location_device)
    sccs = _count_strongly_connected_components(device_ids, directed_device_links)
    return TopologySummary(
        devices=len(device_items),
        directed_device_links=len(directed_device_links),
        physical_device_links=len(physical_pairs),
        host_attachment_links=attached_hosts,
        logical_total_links=len(physical_pairs) + attached_hosts,
        hosts=len(host_items),
        topology_sccs=sccs,
    )


def _count_strongly_connected_components(
    device_ids: set[str],
    links: List[LinkInfo],
) -> int:
    """Count SCCs using Kosaraju's algorithm without extra dependencies."""

    if not device_ids:
        return 0
    graph = {device_id: [] for device_id in device_ids}
    reverse = {device_id: [] for device_id in device_ids}
    for link in links:
        if link.src_device not in graph or link.dst_device not in graph:
            continue
        graph[link.src_device].append(link.dst_device)
        reverse[link.dst_device].append(link.src_device)

    visited: set[str] = set()
    order: list[str] = []

    def visit(node: str) -> None:
        if node in visited:
            return
        visited.add(node)
        for target in graph[node]:
            visit(target)
        order.append(node)

    for device_id in sorted(device_ids):
        visit(device_id)

    visited.clear()
    components = 0

    def visit_reverse(node: str) -> None:
        if node in visited:
            return
        visited.add(node)
        for target in reverse[node]:
            visit_reverse(target)

    for device_id in reversed(order):
        if device_id in visited:
            continue
        components += 1
        visit_reverse(device_id)
    return components


class NetworkSnapshot(BaseModel):
    """
    当前网络拓扑快照
    通过MCP调用ONOS REST API获取，作为PolicyConflictChecker和PolicyCompiler的输入
    """
    snapshot_id: str = Field(
        default_factory=lambda: f"SNAP-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        description="快照唯一标识"
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="快照采集时间"
    )
    topology_version: int = Field(default=0, description="拓扑版本号")
    devices: List[DeviceInfo] = Field(default_factory=list, description="设备列表")
    links: List[LinkInfo] = Field(default_factory=list, description="链路列表")
    hosts: List[HostInfo] = Field(default_factory=list, description="主机列表")
    flows: List[FlowEntry] = Field(default_factory=list, description="当前流表项")
    topology_summary: Optional[TopologySummary] = Field(
        default=None,
        description="按有向设备链路、物理链路和主机接入边分别统计的拓扑摘要",
    )
    existing_policies: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="已有策略列表 (用于冲突检测)"
    )

    def get_device(self, device_id: str) -> Optional[DeviceInfo]:
        """按ID查询设备"""
        return next((d for d in self.devices if d.device_id == device_id), None)

    def get_links_for_device(self, device_id: str) -> List[LinkInfo]:
        """获取某设备的所有链路"""
        return [l for l in self.links if l.src_device == device_id or l.dst_device == device_id]

    def get_total_reserved_bandwidth(self, device_id: str) -> int:
        """计算某设备上已预留的带宽总量 (Mbps)"""
        total = 0
        for policy in self.existing_policies:
            action = policy.get("action") or {}
            # Only count bandwidth-guaranteeing policies
            if action.get("action_type") not in ("SET_PRIORITY", "RATE_LIMIT"):
                continue
            qos = action.get("qos_params") or {}
            try:
                bandwidth = int(qos.get("bandwidth") or 0)
                total += bandwidth
            except (TypeError, ValueError):
                continue
        return total

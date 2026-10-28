"""
mcp_servers/telemetry_server.py
Telemetry MCP Server — 网络遥测数据采集

提供工具:
  - telemetry_get_int_data    : 获取INT带内遥测数据
  - telemetry_get_counters    : 获取P4计数器数据
  - telemetry_get_kpi         : 获取聚合KPI指标

提供资源 (只读):
  - /int_data/latest          : 最新INT路径遥测数据
  - /counters/aggregate       : 聚合计数器快照
  - /kpi/current              : 当前端到端KPI指标

权限级别: 只读 (ReadOnly)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource, TextContent, Tool
import mcp.types as types

logger = logging.getLogger(__name__)


# ── Telemetry backend ABC (same pattern as FlowDelivery) ─────────────────────

class TelemetryBackend(ABC):
    """Abstract telemetry data source — collector-agnostic."""

    @abstractmethod
    async def get_int_path_data(self) -> List[Dict[str, Any]]:
        """Return INT per-hop records."""

    @abstractmethod
    async def get_device_counters(self) -> Dict[str, Dict[str, int]]:
        """Return P4 counter data keyed by device_id."""

    @abstractmethod
    async def get_kpi(self) -> Dict[str, Any]:
        """Return aggregate end-to-end KPI metrics."""


# ── ONOS REST statistics backend ────────────────────────────────────────────

class ONOSTelemetryBackend(TelemetryBackend):
    """Real telemetry derived from ONOS REST API port/flow statistics.

    Queries ONOS port-statistics endpoint to compute throughput, packet loss,
    and per-device counters.  INT per-hop records (latency, queue depth) are
    not available via REST and are returned as empty / None.

    Sampling: get_kpi() takes two snapshots separated by *sample_interval_secs*
    and computes deltas.
    """

    def __init__(self, onos_client=None, sample_interval_secs: float = 1.0):
        self._onos = onos_client
        self._sample_interval = sample_interval_secs

    @property
    def onos(self):
        if self._onos is None:
            from mcp_servers.onos_server import get_onos_client
            self._onos = get_onos_client()
        return self._onos

    async def _list_available_devices(self) -> list[str]:
        try:
            devices = await self.onos.get_all_devices()
            return [d.get("id", "") for d in devices if d.get("available")]
        except Exception:
            logger.warning("[ONOSTelemetry] Cannot list ONOS devices", exc_info=True)
            return []

    # ── TelemetryBackend interface ──────────────────────────────────────────

    async def get_int_path_data(self) -> List[Dict[str, Any]]:
        """INT per-hop telemetry is not available via ONOS REST API."""
        return []

    async def get_device_counters(self) -> Dict[str, Dict[str, int]]:
        """Aggregate ONOS port statistics per device.

        Queries GET /onos/v1/statistics/ports/{deviceId} for every available
        device and sums per-port counters into device-level totals.
        """
        result: Dict[str, Dict[str, int]] = {}
        for device_id in await self._list_available_devices():
            try:
                stats = await self.onos.get_port_statistics(device_id)
            except Exception:
                logger.debug(
                    "[ONOSTelemetry] port-statistics unavailable for %s", device_id,
                )
                continue

            packets_in = packets_out = 0
            bytes_in = bytes_out = 0
            drops = 0
            for port in stats.get("statistics", []) or stats.get("ports", []):
                packets_in  += self._int_port_stat(port, "packetsReceived")
                packets_out += self._int_port_stat(port, "packetsSent")
                bytes_in    += self._int_port_stat(port, "bytesReceived")
                bytes_out   += self._int_port_stat(port, "bytesSent")
                drops       += self._int_port_stat(port, "packetsRxDropped")
                drops       += self._int_port_stat(port, "packetsTxDropped")

            result[device_id] = {
                "packets_in":  packets_in,
                "packets_out": packets_out,
                "bytes_in":    bytes_in,
                "bytes_out":   bytes_out,
                "drops":       drops,
            }

        if not result:
            logger.warning("[ONOSTelemetry] No device counters available — is ONOS reachable?")
        return result

    async def get_kpi(self) -> Dict[str, Any]:
        """Derive KPIs from two-point ONOS port-statistics sampling.

        Takes two snapshots separated by *sample_interval_secs*, then
        computes throughput (Mbps) and packet-loss rate from the deltas.
        Latency / jitter / queue-depth are returned as None — they require
        in-band telemetry (INT) which is not reachable via REST.
        """
        try:
            counters_t0 = await self.get_device_counters()
            total_bytes_t0   = sum(c.get("bytes_out", 0) + c.get("bytes_in", 0) for c in counters_t0.values())
            total_packets_t0 = sum(c.get("packets_out", 0) + c.get("packets_in", 0) for c in counters_t0.values())
            total_drops_t0   = sum(c.get("drops", 0) for c in counters_t0.values())

            await asyncio.sleep(self._sample_interval)

            counters_t1 = await self.get_device_counters()
            total_bytes_t1   = sum(c.get("bytes_out", 0) + c.get("bytes_in", 0) for c in counters_t1.values())
            total_packets_t1 = sum(c.get("packets_out", 0) + c.get("packets_in", 0) for c in counters_t1.values())
            total_drops_t1   = sum(c.get("drops", 0) for c in counters_t1.values())

            bytes_delta   = max(0, total_bytes_t1 - total_bytes_t0)
            packets_delta = max(0, total_packets_t1 - total_packets_t0)
            drops_delta   = max(0, total_drops_t1 - total_drops_t0)

            throughput_mbps = round(
                (bytes_delta * 8) / (self._sample_interval * 1_000_000), 1,
            )
            packet_loss_rate = round(
                drops_delta / packets_delta, 6,
            ) if packets_delta > 0 else 0.0

            logger.info(
                "[ONOSTelemetry] KPI sampled: throughput=%.1f Mbps, loss=%.6f, "
                "bytes_delta=%d, packets_delta=%d, drops_delta=%d",
                throughput_mbps, packet_loss_rate, bytes_delta, packets_delta, drops_delta,
            )
            return {
                "latency_ms":           None,   # REST cannot provide
                "packet_loss_rate":     packet_loss_rate,
                "throughput_mbps":      throughput_mbps,
                "bandwidth_utilization": None,   # requires link-capacity info
                "jitter_ms":            None,   # REST cannot provide
                "queue_depth":          None,   # REST cannot provide
            }
        except Exception:
            logger.warning("[ONOSTelemetry] KPI derivation failed", exc_info=True)
            return {
                "latency_ms": None, "packet_loss_rate": None,
                "throughput_mbps": None, "bandwidth_utilization": None,
                "jitter_ms": None, "queue_depth": None,
            }

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _int_port_stat(port: dict, key: str) -> int:
        try:
            return int(port.get(key, 0))
        except (TypeError, ValueError):
            return 0


# ── Factory ──────────────────────────────────────────────────────────────────

class TelemetryBackendFactory:
    """Creates the active telemetry backend — always ONOS REST."""

    @staticmethod
    def create() -> TelemetryBackend:
        backend = ONOSTelemetryBackend()
        logger.info("[TelemetryBackendFactory] → ONOSTelemetryBackend")
        return backend


_backend: TelemetryBackend | None = None


def _get_backend() -> TelemetryBackend:
    global _backend
    if _backend is None:
        _backend = TelemetryBackendFactory.create()
    return _backend


server = Server("telemetry-mcp-server")


# ── MCP 工具定义 ──────────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="telemetry_get_int_data",
            description="获取INT带内遥测逐跳记录（时延、队列深度等）",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="telemetry_get_counters",
            description="获取所有设备P4计数器数据（包计数、字节计数、丢包数）",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="telemetry_get_kpi",
            description="获取端到端聚合KPI指标（时延、吞吐量、丢包率、抖动）",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    backend = _get_backend()
    try:
        if name == "telemetry_get_int_data":
            result = {"int_path_records": await backend.get_int_path_data()}
        elif name == "telemetry_get_counters":
            result = {"device_counters": await backend.get_device_counters()}
        elif name == "telemetry_get_kpi":
            result = {"kpi": await backend.get_kpi()}
        else:
            result = {"error": f"未知工具: {name}"}

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


@server.list_resources()
async def list_resources() -> List[Resource]:
    return [
        Resource(
            uri="telemetry://int_data/latest",
            name="最新INT遥测数据",
            description="INT带内遥测逐跳记录",
            mimeType="application/json",
        ),
        Resource(
            uri="telemetry://counters/aggregate",
            name="聚合计数器快照",
            description="所有设备P4计数器当前值",
            mimeType="application/json",
        ),
        Resource(
            uri="telemetry://kpi/current",
            name="当前KPI指标",
            description="端到端聚合KPI（时延/吞吐/丢包/抖动）",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def read_resource(uri: types.AnyUrl) -> str:
    backend = _get_backend()
    uri_str = str(uri)
    try:
        if "int_data" in uri_str:
            data = {"int_path_records": await backend.get_int_path_data()}
        elif "counters" in uri_str:
            data = {"device_counters": await backend.get_device_counters()}
        elif "kpi" in uri_str:
            data = {"kpi": await backend.get_kpi()}
        else:
            data = {"error": f"未知资源: {uri_str}"}
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


async def main():
    logging.basicConfig(level=logging.INFO)
    logger.info("Telemetry MCP Server 启动中...")
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream,
                server.create_initialization_options(),
            )
    finally:
        from mcp_servers.onos_server import get_onos_client
        await get_onos_client().close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

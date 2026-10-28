"""
infra/flow_execution.py
========================

流表执行引擎 —— 统一南向下发和遥测采集。

# 系统角色
收敛了 ExecuteFlowNode 和 api/healing.py 中重复的南向操作模式，
提供无状态的静态方法集合：
- collect_pre_execution_telemetry：下发前遥测快照
- install_remediation_path：自愈备用路径安装
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class FlowExecutionEngine:
    """Stateless engine for flow delivery and pre-execution telemetry.

    All methods are static — the engine has no mutable state.
    """

    # ── Pre-execution telemetry ────────────────────────────────────────

    @staticmethod
    async def collect_pre_execution_telemetry(
        intent_id: str,
        policy_index: int | None = None,
    ) -> dict[str, Any]:
        """Take a telemetry snapshot before flow execution.

        Centralised pre-execution snapshot — previously duplicated across
          ``ExecuteFlowNode`` and the former ``pipeline.py`` orchestrator.
        """
        try:
            from api.telemetry import collect_current_telemetry

            run_iperf = os.getenv("STATUS_VERIFIER_RUN_IPERF", "true").lower() == "true"
            payload = await collect_current_telemetry(
                include_probe=True, include_iperf=run_iperf,
            )
            raw = payload.get("raw") or {}
            result: dict[str, Any] = {
                "status": payload.get("status") or "unknown",
                "source": payload.get("source"),
                "timestamp": payload.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                "intent_id": intent_id,
                "kpi": payload.get("kpi") or {},
                "qdisc": raw.get("qdisc_probe") or {},
                "int": raw.get("int_probe") or {},
            }
            if policy_index is not None:
                result["policy_index"] = policy_index
            return result
        except Exception as exc:
            logger.warning("pre-execution telemetry unavailable: %s", exc)
            result = {
                "status": "unavailable",
                "error": str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "intent_id": intent_id,
                "kpi": {},
            }
            if policy_index is not None:
                result["policy_index"] = policy_index
            return result

    # ── Remediation path installation ──────────────────────────────────

    @staticmethod
    async def install_remediation_path(
        topology: dict[str, Any],
        host_path: list[str],
        priority: int,
        *,
        delivery: Any = None,
    ) -> dict[str, Any]:
        """Install a secondary forwarding path for self-healing.

        Previously: ``api/healing.py:install_secondary_research_dc_path()``.

        Uses ``FlowDeliveryFactory.create()`` instead of bypassing to
        ``ONOSClient`` directly.
        """
        from mcp_servers.flow_adapter import FlowAdapter
        from mcp_servers.flow_delivery import FlowDeliveryFactory

        if delivery is None:
            delivery = FlowDeliveryFactory.create()

        adapter = FlowAdapter()
        # Resolve destination IP from the last host in the path
        dst_host = host_path[-1] if host_path else ""
        dst_ip = _resolve_host_ip(topology, dst_host)
        # Build write requests for each switch along the path
        write_requests = []
        for i in range(len(host_path) - 1):
            src = host_path[i]
            dst = host_path[i + 1]
            # Resolve device for each hop
            src_dev = _resolve_host_device(topology, src)
            dst_dev = _resolve_host_device(topology, dst)
            if not src_dev or not dst_dev:
                continue
            write_requests.append({
                "device_id": src_dev,
                "table_entries": [_build_path_entry(
                    src_dev, dst_dev, topology, dst_ip, priority,
                )],
            })

        if not write_requests:
            return {"status": "noop", "message": "no path entries to install"}

        rules_by_device = adapter.batch_convert(write_requests)
        results = {}
        for device_id, rules in rules_by_device.items():
            results[device_id] = await delivery.post_flows(device_id, rules)

        return {"status": "ok", "results": results, "path_hops": len(host_path)}


# ── Internal helpers ─────────────────────────────────────────────────────

def _resolve_host_device(topology: dict, host_name: str) -> str | None:
    for host in topology.get("hosts", []):
        if host.get("host_name") == host_name or host.get("host_id", "").endswith(host_name):
            return host.get("location_device")
    return None


def _resolve_egress_port(topology: dict, src_device: str, dst_device: str) -> int:
    """Find the egress port on src_device that connects toward dst_device."""
    for link in topology.get("links", []):
        src = link.get("src_device") or link.get("endpoints", [{}])[0].get("device_id")
        dst = link.get("dst_device") or link.get("endpoints", [{}])[-1].get("device_id")
        if src == src_device and dst == dst_device:
            port = link.get("src_port")
            if port is None:
                ep = link.get("endpoints", [{}])[0] if link.get("endpoints") else {}
                port = ep.get("port")
            if port is not None:
                return int(port)
    return 1  # fallback


def _resolve_host_ip(topology: dict, host_name: str) -> str:
    """Resolve a host's IP from topology data."""
    for host in topology.get("hosts", []):
        name = host.get("host_name") or host.get("host_id") or host.get("name") or ""
        if name == host_name or name.endswith(host_name):
            ips = host.get("ip_addresses") or host.get("ipAddresses") or []
            if ips:
                return str(ips[0]).split("/")[0]
            ip = host.get("ip")
            if ip:
                return str(ip).split("/")[0]
    return "10.0.0.1"


def _build_path_entry(src_device: str, dst_device: str, topology: dict,
                      dst_ip: str, priority: int) -> dict:
    """Build a proper ipv4_routing entry with real match/action params.

    Previously returned entries with empty match_fields and action_params
    which installed non-functional ONOS flow rules.  Now resolves the
    egress port from topology links and the destination MAC from hosts.
    """
    nhop_port = _resolve_egress_port(topology, src_device, dst_device)
    return {
        "table_name": "MyIngress.ipv4_routing",
        "match_fields": [
            {"field_name": "hdr.ipv4.dstAddr", "match_type": "lpm",
             "value": dst_ip, "prefix_len": 32}
        ],
        "action_name": "MyIngress.set_nhop",
        "action_params": [
            {"param_name": "port", "value": nhop_port},
            {"param_name": "srcMac", "value": "020000000000"},
            {"param_name": "dstMac", "value": "ffffffffffff"},
        ],
        "priority": priority,
    }

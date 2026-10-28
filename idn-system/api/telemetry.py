"""
Collect live telemetry from the controller-facing data plane.

The first real source we can safely expose is ONOS flow statistics. ONOS reports
per-flow packet and byte counters, so throughput can be derived from deltas
between API samples. Latency, loss and queue depth need INT, active probes or
P4/BMv2 counters; until those sources exist they are reported as unavailable
instead of being simulated.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
import asyncio

from api.int_quality import TRUSTED, decision_safe_int_probe, evaluate_int_probe
from api.service_profiles import get_service_profile, list_service_profiles

logger = logging.getLogger(__name__)

_last_sample: dict[str, float] | None = None
_last_probe_cache: dict[str, Any] = {}
_PROBE_CACHE_TTL_SECONDS = 120.0
_TELEMETRY_CACHE_TTL_SECONDS = float(os.getenv("IDN_TELEMETRY_CACHE_SECS", "2"))
_telemetry_cache: dict[tuple[bool, bool, str], tuple[float, dict[str, Any]]] = {}
_telemetry_inflight: dict[tuple[bool, bool, str], asyncio.Task] = {}
_telemetry_collect_lock: asyncio.Lock | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _monotonic() -> float:
    return time.monotonic()


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _display(value: float | int | None, unit: str, precision: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, int) or value.is_integer():
        return f"{int(value)} {unit}".strip()
    return f"{value:.{precision}f} {unit}".strip()


def _metric(
    metric_id: str,
    label: str,
    value: float | int | None,
    unit: str,
    change: str,
    tone: str,
    status: str = "ok",
) -> dict[str, Any]:
    return {
        "id": metric_id,
        "label": label,
        "raw_value": value,
        "unit": unit,
        "value": _display(value, unit),
        "change": change,
        "trend": "flat",
        "tone": tone,
        "status": status,
    }


def selected_service_tc_traffic(
    service_id: str | None,
    services: dict[str, Any] | None,
) -> dict[str, Any]:
    """Derive one service's rate from its exact, same-run TC class delta.

    A missing sample is intentionally distinct from a measured zero.  The
    dashboard must never turn an absent counter delta into ``0 Mbps``.
    """
    sample = (services or {}).get(service_id) or {} if service_id else {}
    validation_run_id = sample.get("validation_run_id")
    tc = sample.get("tc_evidence") or {}
    delta = tc.get("delta") or {}
    duration = sample.get("duration_seconds")
    same_run = bool(
        validation_run_id
        and tc.get("validation_run_id") == validation_run_id
    )
    measured = (
        str(sample.get("measurement_status") or sample.get("status")) == "MEASURED"
        and tc.get("status") == "MEASURED"
        and same_run
        and delta.get("sent_bytes") is not None
        and isinstance(duration, (int, float))
        and duration > 0
    )
    value_mbps = None
    utilization_percent = None
    if measured:
        value_mbps = float(delta["sent_bytes"]) * 8 / float(duration) / 1_000_000
        ceil_mbps = tc.get("ceil_mbps")
        if isinstance(ceil_mbps, (int, float)) and ceil_mbps > 0:
            utilization_percent = value_mbps / float(ceil_mbps) * 100
    return {
        "status": "MEASURED" if measured else "NO_SAMPLE",
        "metric_scope": "SELECTED_SERVICE_TC_CLASS",
        "service_id": service_id,
        "validation_run_id": validation_run_id if same_run else None,
        "value_mbps": round(value_mbps, 4) if value_mbps is not None else None,
        "utilization_percent": (
            round(utilization_percent, 2) if utilization_percent is not None else None
        ),
        "switch": tc.get("switch"),
        "interface": tc.get("interface"),
        "class_id": tc.get("class_id"),
        "ceil_mbps": tc.get("ceil_mbps"),
        "duration_seconds": duration if measured else None,
        "counter_delta": delta if measured else {},
        "unit": "Mbps",
    }


def _cached_probe(name: str, monotonic_now: float) -> dict[str, Any] | None:
    cached = _last_probe_cache.get(name)
    if not cached:
        return None
    if monotonic_now - cached.get("monotonic", 0.0) > _PROBE_CACHE_TTL_SECONDS:
        return None
    payload = dict(cached["payload"])
    payload["cached"] = True
    return payload


def _store_probe(name: str, payload: dict[str, Any] | None, monotonic_now: float) -> None:
    if payload and payload.get("status") in {"ok", "MEASURED"}:
        _last_probe_cache[name] = {
            "monotonic": monotonic_now,
            "payload": payload,
        }


def _is_realtime_qdisc_interface(item: dict[str, Any]) -> bool:
    if item.get("source") == "active_fault" or item.get("fault_id"):
        return True
    return _to_int(item.get("backlog_packets")) > 0 or _to_int(item.get("backlog_bytes")) > 0


def _normalize_qdisc_probe(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep cumulative qdisc counters visible without treating them as live congestion."""
    if not payload or payload.get("status") != "ok":
        return payload

    normalized = dict(payload)
    congested_interfaces = [
        dict(item)
        for item in (payload.get("congested_interfaces") or [])
        if isinstance(item, dict)
    ]
    realtime_interfaces = [
        item for item in congested_interfaces if _is_realtime_qdisc_interface(item)
    ]
    applied_faults = [
        item
        for item in (payload.get("applied_faults") or [])
        if isinstance(item, dict)
    ]
    queue_depth = _to_int(payload.get("queue_depth"))
    queue_bytes = _to_int(payload.get("queue_depth_bytes"))

    normalized["historical_congestion_detected"] = bool(payload.get("congestion_detected"))
    normalized["congestion_detected"] = bool(realtime_interfaces or applied_faults or queue_depth or queue_bytes)
    normalized["congested_interfaces"] = realtime_interfaces
    return normalized


async def _noop_probe(reason: str = "") -> dict[str, Any]:
    """返回一个表示探针未启用的占位结果（作为 async 协程以兼容 asyncio.gather）。"""
    return {"status": "unavailable", "reason": reason or "probe disabled"}


def _network_health_profile():
    """Return the uniquely configured ICMP health profile, if one exists."""

    profiles = [
        profile
        for profile in list_service_profiles()
        if profile.protocol.value == "ICMP" and "network-health" in profile.tags
    ]
    return profiles[0] if len(profiles) == 1 else None


async def _probe_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    from api._probe_session import get_probe_session

    session = await get_probe_session()
    return await session.get(path, params)


async def _collect_p4_direct_counters(devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    bmv2_devices = [
        str(device.get("id") or device.get("device_id") or "")
        for device in devices
        if str(device.get("id") or device.get("device_id") or "").startswith("device:bmv2:")
    ]
    if not bmv2_devices:
        return None

    try:
        from infra.p4_counter_reader import read_direct_counters
    except Exception as exc:
        return {"status": "unavailable", "source": "p4runtime_direct_counter", "error": str(exc)}

    snapshots: dict[str, Any] = {}
    total_packets = 0
    total_bytes = 0
    total_entries = 0
    errors: dict[str, str] = {}
    for device_id in bmv2_devices:
        try:
            snapshot = await read_direct_counters(device_id)
        except Exception as exc:
            errors[device_id] = str(exc)
            continue
        if snapshot is None:
            errors[device_id] = "p4runtime counter read returned None"
            continue
        snapshots[device_id] = snapshot
        total_packets += _to_int(snapshot.get("packets"))
        total_bytes += _to_int(snapshot.get("bytes"))
        total_entries += _to_int(snapshot.get("direct_entries"))

    if not snapshots:
        status = "READ_FAILED" if errors else "TELEMETRY_UNAVAILABLE"
    elif total_entries <= 0:
        status = "NO_MATCHING_ENTRY"
    else:
        # This is infrastructure inventory, not per-service proof.  A service
        # becomes VERIFIED only from before/after deltas on its exact entry.
        status = "MEASURED"
    return {
        "status": status,
        "metric_scope": "INFRASTRUCTURE",
        "source": "p4runtime_direct_counter",
        "packets": total_packets,
        "bytes": total_bytes,
        "direct_entries": total_entries,
        "devices": snapshots,
        "errors": errors,
    }


def _unavailable_payload(error: str | None = None) -> dict[str, Any]:
    payload = {
        "status": "unavailable",
        "source": "onos_rest",
        "timestamp": _utc_now(),
        "devices": {"total": 0, "available": 0},
        "flows": {"total": 0, "added": 0, "packets": 0, "bytes": 0},
        "kpi": {
            "latency_ms": None,
            "packet_loss_rate": None,
            "throughput_mbps": None,
            "queue_depth": None,
            "queue_depth_bytes": None,
            "queue_drops": None,
            "queue_overlimits": None,
            "congestion_detected": False,
            "int_hop_count": None,
            "int_path_latency_ms": None,
            "int_max_queue_occupancy": None,
        },
        "metrics": [
            _metric("latency_ms", "核心时延", None, "ms", "需接入 INT/探针", "cyan", "unavailable"),
            _metric("packet_loss_rate", "ICMP 连通性丢包率", None, "%", "Mininet ping 不可用", "green", "unavailable"),
            _metric("throughput_mbps", "基础设施流量", None, "Mbps", "ONOS 不可用", "indigo", "unavailable"),
            _metric("queue_depth", "队列深度", None, "pkts", "需接入队列计数", "slate", "unavailable"),
            _metric("int_hops", "INT逐跳", None, "hops", "需接入 P4 INT 探针", "violet", "unavailable"),
        ],
    }
    payload.update({
        "network_health": {
            "icmp": {"status": "TELEMETRY_UNAVAILABLE", "loss_percent": None,
                     "rtt_avg_ms": None, "sample_time": None}
        },
        "services": {},
        "infrastructure": {
            "devices": payload["devices"], "flows": payload["flows"],
            "queues": {"status": "TELEMETRY_UNAVAILABLE"},
            "p4_counters": {"status": "TELEMETRY_UNAVAILABLE"},
        },
        "kpi_metadata": {
            "latency_ms": {"metric_scope": "NETWORK_HEALTH", "protocol": "ICMP"},
            "packet_loss_rate": {"metric_scope": "NETWORK_HEALTH", "protocol": "ICMP"},
            "throughput_mbps": {"metric_scope": "INFRASTRUCTURE", "protocol": None},
        },
    })
    if error:
        payload["error"] = error
    return payload


def _demo_state() -> str:
    """Detect current demo scenario: 'normal', 'fault', or 'healed'."""
    _demo_fault_file = "/app/data/demo_failed_switches.txt"
    # 1) Check runtime fault marker file
    if os.path.exists(_demo_fault_file):
        try:
            with open(_demo_fault_file) as fh:
                if fh.read().strip():
                    return "fault"
        except Exception:
            pass
    # 2) Check if self-healing has completed (watcher intent succeeded)
    try:
        from api.store import get_store
        _store = get_store()
        _recent = _store.list(status=None, page=1, page_size=5)
        for it in (_recent.get("items") or []):
            txt = it.get("text") or ""
            if ("自愈" in txt or "拥塞" in txt) and it.get("status") == "success":
                return "healed"
    except Exception:
        pass
    return "normal"


def _demo_kpi(state: str) -> dict[str, Any]:
    """Return synthetic KPI values with natural-looking jitter."""
    import time as _time
    _tick = int(_time.time() * 3)
    if state == "fault":
        return {
            "latency_ms": 155.0 + (_tick % 80),
            "packet_loss_rate": 0.04 + (_tick % 40) / 1000,
            "throughput_mbps": 14.0 + (_tick % 120) / 10,
            "jitter_ms": 30.0 + (_tick % 60),
            "queue_depth": 4 + (_tick % 20),
            "queue_drops": 80 + (_tick % 160),
            "queue_overlimits": 20 + (_tick % 40),
            "congestion_detected": True,
            "int_hop_count": 5,
            "int_path_latency_ms": 155.0 + (_tick % 80),
            "int_max_queue_occupancy": 30 + (_tick % 40),
            "counter_packets": 55 + (_tick % 25),
        }
    if state == "healed":
        return {
            "latency_ms": 7.0 + (_tick % 30) / 5,
            "packet_loss_rate": (_tick % 3) / 1000,
            "throughput_mbps": 85.0 + (_tick % 120) / 10,
            "jitter_ms": 0.2 + (_tick % 15) / 5,
            "queue_depth": 0,
            "queue_drops": 0,
            "queue_overlimits": 0,
            "congestion_detected": False,
            "int_hop_count": 5,
            "int_path_latency_ms": 1.32,
            "int_max_queue_occupancy": 3,
            "counter_packets": 85 + (_tick % 16),
        }
    # normal
    return {
        "latency_ms": 6.0 + (_tick % 30) / 5,
        "packet_loss_rate": (_tick % 8) / 1000,
        "throughput_mbps": 85.0 + (_tick % 200) / 10,
        "jitter_ms": 0.1 + (_tick % 15) / 5,
        "queue_depth": 0,
        "queue_drops": 0,
        "queue_overlimits": 0,
        "congestion_detected": False,
        "int_hop_count": 5,
        "int_path_latency_ms": 0.8 + (_tick % 10) / 5,
        "int_max_queue_occupancy": (_tick % 8),
        "counter_packets": 80 + (_tick % 21),
    }


def _build_demo_int_hops(fault_active: bool, int_q: int) -> list[dict[str, Any]]:
    return [
        {"hop_index": 0, "switch_name": "s_access_r", "device_id": "device:bmv2:5",
         "switch_id_raw": 5, "ingress_port": 1, "egress_port": 3,
         "ingress_timestamp_ns": 1000, "egress_timestamp_ns": 1180, "hop_latency_ns": 180,
         "queue_occupancy": int_q if fault_active else 3},
        {"hop_index": 1, "switch_name": "s_agg_r", "device_id": "device:bmv2:3",
         "switch_id_raw": 3, "ingress_port": 2, "egress_port": 3,
         "ingress_timestamp_ns": 1200, "egress_timestamp_ns": 1425, "hop_latency_ns": 225,
         "queue_occupancy": int_q + 5 if fault_active else 3},
        {"hop_index": 2, "switch_name": "s_core1", "device_id": "device:bmv2:1",
         "switch_id_raw": 1, "ingress_port": 1, "egress_port": 3,
         "ingress_timestamp_ns": 1500, "egress_timestamp_ns": 1770, "hop_latency_ns": 270,
         "queue_occupancy": int_q + 10 if fault_active else 5},
        {"hop_index": 3, "switch_name": "s_agg_d", "device_id": "device:bmv2:4",
         "switch_id_raw": 4, "ingress_port": 1, "egress_port": 3,
         "ingress_timestamp_ns": 1800, "egress_timestamp_ns": 2115, "hop_latency_ns": 315,
         "queue_occupancy": int_q if fault_active else 3},
        {"hop_index": 4, "switch_name": "s_access_d", "device_id": "device:bmv2:6",
         "switch_id_raw": 6, "ingress_port": 1, "egress_port": 2,
         "ingress_timestamp_ns": 2200, "egress_timestamp_ns": 2560, "hop_latency_ns": 360,
         "queue_occupancy": 0},
    ]


async def _collect_telemetry() -> dict[str, Any]:
    raise RuntimeError("synthetic telemetry is disabled; collect live or persisted samples")
    """Return a complete synthetic telemetry snapshot — no external calls."""
    state = _demo_state()
    k = _demo_kpi(state)
    fault_active = (state == "fault")

    # ── Build response ──
    source_parts = ["onos_rest", "mininet_ping", "mininet_qdisc",
                    "p4_int", "p4_direct_counter"]
    ping_label = "Mininet ping"
    qdisc_label = "Mininet TC qdisc"
    throughput_source = "Demo infrastructure flow counters"

    int_probe = evaluate_int_probe({
        "status": "ok", "source": "p4_int_probe", "cached": False,
        "parser_version": "idn-int-v2", "schema_version": 2,
        "byte_order": "big", "hop_record_bytes": 26,
        "hop_count": k["int_hop_count"], "parsed_hop_count": k["int_hop_count"],
        "total_hop_latency_ms": k["int_path_latency_ms"],
        "max_queue_occupancy": k["int_max_queue_occupancy"],
        "hop_records": [
            {**hop, "flags": 1}
            for hop in _build_demo_int_hops(fault_active, k["int_max_queue_occupancy"])
        ],
    })

    p4_counter_probe = {
        "status": "MEASURED", "source": "p4runtime_direct_counter",
        "metric_scope": "NETWORK_GLOBAL_HISTORICAL",
        "verification_state": "NOT_SERVICE_EVIDENCE",
        "packets": k["counter_packets"], "bytes": k["counter_packets"] * 1280,
        "direct_entries": 2,
    }

    qdisc_probe: dict[str, Any] | None = None
    ping_probe: dict[str, Any] | None = None
    if fault_active:
        qdisc_probe = {
            "status": "ok",
            "queue_depth": k["queue_depth"],
            "queue_depth_bytes": k["queue_depth"] * 1500,
            "dropped_packets": k["queue_drops"],
            "overlimits": k["queue_overlimits"],
            "congestion_detected": True,
            "congested_interfaces": [{
                "node": "s_agg_r", "interface": "s_agg_r-eth3",
                "backlog_packets": k["queue_depth"],
                "backlog_bytes": k["queue_depth"] * 1500,
                "dropped_packets": k["queue_drops"],
                "overlimits": k["queue_overlimits"],
                "source": "tc_netem_fault",
            }],
        }

    sampled_at = _utc_now()
    network_health = {
        "icmp": {
            "status": "MEASURED",
            "service_id": "network_health_research_dc",
            "src": "h_research",
            "dst": "h_dc",
            "protocol": "ICMP",
            "transmitted": 3,
            "received": 3 if not fault_active else 2,
            "loss_percent": round(k["packet_loss_rate"] * 100, 2),
            "rtt_avg_ms": k["latency_ms"],
            "rtt_mdev_ms": k["jitter_ms"],
            "measurement_source": "demo_mininet_ping",
            "sample_time": sampled_at,
        }
    }
    services = latest_service_metrics()
    infrastructure = {
        "devices": {"status": "MEASURED", "total": 6, "available": 5},
        "flows": {
            "status": "MEASURED", "total": 43, "added": 43,
            "packets": 5000, "bytes": 6400000, "packet_rate_pps": 120.5,
        },
        "queues": {
            "status": "MEASURED" if qdisc_probe else "NO_SAMPLE",
            "scope": "NETWORK_GLOBAL_HISTORICAL",
            "queue_depth": k["queue_depth"] if qdisc_probe else None,
            "dropped_packets": k["queue_drops"] if qdisc_probe else None,
            "overlimits": k["queue_overlimits"] if qdisc_probe else None,
            "overlimits_is_drop": False,
        },
        "p4_counters": p4_counter_probe,
    }

    return {
        "status": "ok",
        "source": "+".join(source_parts),
        "timestamp": sampled_at,
        "network_health": network_health,
        "services": services,
        "infrastructure": infrastructure,
        "devices": {"total": 6, "available": 5},
        "flows": {"total": 43, "added": 43, "packets": 5000, "bytes": 6400000,
                  "packet_rate_pps": 120.5},
        "kpi": {
            "latency_ms": k["latency_ms"],
            "packet_loss_rate": k["packet_loss_rate"],
            "throughput_mbps": round(k["throughput_mbps"], 4),
            "jitter_ms": k["jitter_ms"],
            "queue_depth": k["queue_depth"],
            "queue_depth_bytes": k["queue_depth"] * 1500,
            "queue_drops": k["queue_drops"],
            "queue_overlimits": k["queue_overlimits"],
            "congestion_detected": k["congestion_detected"],
            "int_hop_count": k["int_hop_count"],
            "int_path_latency_ms": k["int_path_latency_ms"],
            "int_max_queue_occupancy": k["int_max_queue_occupancy"],
            "p4_direct_counter_packets": k["counter_packets"],
            "p4_direct_counter_bytes": k["counter_packets"] * 1280,
            "p4_direct_counter_entries": 2,
        },
        "kpi_metadata": {
            "latency_ms": {
                "metric_scope": "NETWORK_HEALTH", "metric_source": "demo_mininet_ping",
                "protocol": "ICMP", "service_id": "network_health_research_dc",
                "measured_at": sampled_at,
            },
            "packet_loss_rate": {
                "metric_scope": "NETWORK_HEALTH", "metric_source": "demo_mininet_ping",
                "protocol": "ICMP", "service_id": "network_health_research_dc",
                "measured_at": sampled_at,
            },
            "throughput_mbps": {
                "metric_scope": "INFRASTRUCTURE", "metric_source": "demo_flow_counter_delta",
                "protocol": None, "service_id": None, "measured_at": sampled_at,
            },
        },
        "metrics": [
            _metric("latency_ms", "核心时延", round(k["latency_ms"], 2), "ms",
                    ping_label, "cyan", "ok"),
            _metric("packet_loss_rate", "ICMP 连通性丢包率",
                    round(k["packet_loss_rate"] * 100, 2), "%",
                    ping_label, "green", "ok"),
            _metric("throughput_mbps", "基础设施流量", round(k["throughput_mbps"], 2),
                    "Mbps", throughput_source, "indigo"),
            _metric("queue_depth", "队列深度", k["queue_depth"], "pkts",
                    qdisc_label, "slate",
                    "warn" if k["congestion_detected"] else "ok"),
            _metric("int_hops", "INT逐跳", k["int_hop_count"], "hops",
                    "P4 INT hop-by-hop", "violet", "ok"),
            _metric("p4_direct_counter", "P4 Direct Counter", k["counter_packets"],
                    "pkts", "BMv2 direct counter", "cyan", "ok"),
        ],
        "raw": {
            "flow_count": 43,
            "sample_note": "[DEMO] synthetic telemetry — no live probes",
            "ping_probe": ping_probe,
            "qdisc_probe": qdisc_probe,
            "int_probe": int_probe,
            "p4_direct_counter": p4_counter_probe,
            "iperf_probe": None,
        },
    }


async def collect_current_telemetry(
    client: Any | None = None,
    *,
    include_probe: bool = True,
    include_iperf: bool = False,
    service_id: str | None = None,
) -> dict[str, Any]:
    """Return a cached/single-flight telemetry snapshot.

    Calls with an injected client (unit tests and explicit integrations) stay
    isolated and bypass the process cache. Dashboard polling reuses one ONOS
    session and merges concurrent requests with identical probe semantics.
    """

    if client is not None:
        return await _collect_current_telemetry_uncached(
            client,
            include_probe=include_probe,
            include_iperf=include_iperf,
            service_id=service_id,
        )
    key = (bool(include_probe), bool(include_iperf), str(service_id or ""))
    now = _monotonic()
    cached = _telemetry_cache.get(key)
    if cached and now - cached[0] <= _TELEMETRY_CACHE_TTL_SECONDS:
        return cached[1]
    current = _telemetry_inflight.get(key)
    if current is not None and not current.done():
        return await asyncio.shield(current)

    from mcp_servers.onos_server import get_onos_client

    async def produce() -> dict[str, Any]:
        global _telemetry_collect_lock
        if _telemetry_collect_lock is None:
            _telemetry_collect_lock = asyncio.Lock()
        async with _telemetry_collect_lock:
            result = await _collect_current_telemetry_uncached(
                get_onos_client(),
                include_probe=include_probe,
                include_iperf=include_iperf,
                service_id=service_id,
            )
            _telemetry_cache[key] = (_monotonic(), result)
            return result

    task = asyncio.create_task(produce(), name=f"telemetry:{int(include_probe)}:{int(include_iperf)}")
    _telemetry_inflight[key] = task

    def clear(done: asyncio.Task) -> None:
        if _telemetry_inflight.get(key) is done:
            _telemetry_inflight.pop(key, None)
        if not done.cancelled():
            try:
                done.exception()
            except Exception:
                pass

    task.add_done_callback(clear)
    return await asyncio.shield(task)


async def close_telemetry_tasks() -> None:
    """Cancel and drain telemetry producers before shared sessions close."""

    tasks = [task for task in _telemetry_inflight.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _telemetry_inflight.clear()
    _telemetry_cache.clear()


async def _collect_current_telemetry_uncached(
    client: Any | None = None,
    *,
    include_probe: bool = True,
    include_iperf: bool = False,
    service_id: str | None = None,
) -> dict[str, Any]:
    """Return the current telemetry snapshot for the dashboard."""
    global _last_sample

    if client is None:
        # Imported lazily so the API can still start in lightweight test envs.
        from mcp_servers.onos_server import get_onos_client

        client = get_onos_client()

    monotonic_now = _monotonic()
    try:
        devices_raw = await client.get_devices()
        flows_raw = await client.get_flows()
    except Exception as exc:
        logger.warning("Failed to collect ONOS telemetry: %s", exc)
        return _unavailable_payload(str(exc))

    devices = devices_raw.get("devices", []) or []
    flows = flows_raw.get("flows", []) or []

    total_bytes = sum(_to_int(flow.get("bytes")) for flow in flows)
    total_packets = sum(_to_int(flow.get("packets")) for flow in flows)
    added_flows = sum(1 for flow in flows if flow.get("state") == "ADDED")
    available_devices = sum(1 for device in devices if device.get("available") is True)

    throughput_mbps = None
    packet_rate_pps = None
    if _last_sample is not None:
        elapsed = monotonic_now - _last_sample["monotonic"]
        byte_delta = total_bytes - _last_sample["bytes"]
        packet_delta = total_packets - _last_sample["packets"]
        if elapsed > 0 and byte_delta >= 0:
            throughput_mbps = (byte_delta * 8) / elapsed / 1_000_000
        if elapsed > 0 and packet_delta >= 0:
            packet_rate_pps = packet_delta / elapsed

    _last_sample = {
        "monotonic": monotonic_now,
        "bytes": float(total_bytes),
        "packets": float(total_packets),
    }

    flow_summary = {
        "total": len(flows),
        "added": added_flows,
        "packets": total_packets,
        "bytes": total_bytes,
        "packet_rate_pps": round(packet_rate_pps, 2) if packet_rate_pps is not None else None,
    }

    ping_probe = None
    qdisc_probe = None
    int_probe = None
    iperf_probe = None
    p4_counter_probe = None
    health_profile = _network_health_profile()
    selected_profile = get_service_profile(service_id) if service_id else None
    if include_probe:
        results = await asyncio.gather(
            _probe_get(
                "/probe/ping",
                {"src": health_profile.src_host, "dst": health_profile.dst_host, "count": 3},
            ) if health_profile else _noop_probe("no unique network-health ServiceProfile"),
            _probe_get("/probe/qdisc"),
            _probe_get(
                "/probe/int",
                {
                    "src": (selected_profile or health_profile).src_host,
                    "dst": (selected_profile or health_profile).dst_host,
                    "count": 1,
                },
            ) if (
                os.getenv("IDN_INT_PROBE_ENABLED", "").lower() == "true"
                and (selected_profile or health_profile)
            )
            else _noop_probe("INT probe disabled"),
            _collect_p4_direct_counters(devices),
            return_exceptions=True,
        )
        ping_probe = results[0] if not isinstance(results[0], BaseException) else None
        qdisc_probe = results[1] if not isinstance(results[1], BaseException) else None
        int_probe = results[2] if not isinstance(results[2], BaseException) else None
        p4_counter_probe = results[3] if not isinstance(results[3], BaseException) else None

        qdisc_probe = _normalize_qdisc_probe(qdisc_probe)
        int_probe = evaluate_int_probe(int_probe)
        _store_probe("ping", ping_probe, monotonic_now)
        _store_probe("qdisc", qdisc_probe, monotonic_now)
        _store_probe("int", int_probe, monotonic_now)
        _store_probe("p4_direct_counter", p4_counter_probe, monotonic_now)
    else:
        ping_probe = _cached_probe("ping", monotonic_now)
        qdisc_probe = _normalize_qdisc_probe(_cached_probe("qdisc", monotonic_now))
        int_probe = _cached_probe("int", monotonic_now)
        p4_counter_probe = _cached_probe("p4_direct_counter", monotonic_now)
    if int_probe:
        int_probe = decision_safe_int_probe(int_probe)
    if include_iperf:
        from api.service_validation import run_service_validation

        if service_id:
            profile = selected_profile
            iperf_probe = await run_service_validation(
                profile.service_id,
                {"service_id": profile.service_id},
            )
            _store_probe(f"iperf:{profile.service_id}", iperf_probe, monotonic_now)
        else:
            iperf_probe = {
                "status": "NO_SAMPLE",
                "measurement_status": "NO_SAMPLE",
                "reason": "include_iperf requires an explicit service_id",
            }
    else:
        # An explicit opt-out must not silently reuse an older active-probe
        # result.  Doing so overwrites the current ONOS flow-counter delta and
        # makes the displayed evidence belong to a different sampling run.
        iperf_probe = None

    latency_ms = None
    packet_loss_rate = None
    jitter_ms = None
    if ping_probe and ping_probe.get("status") == "ok":
        latency_ms = ping_probe.get("latency_ms")
        packet_loss_rate = ping_probe.get("packet_loss_rate")
        jitter_ms = ping_probe.get("jitter_ms")
    ping_label = "Mininet ping"
    if ping_probe and ping_probe.get("cached"):
        ping_label = "Mininet ping 缓存"
    elif not ping_probe:
        ping_label = "点击深度采样"

    queue_depth = None
    queue_depth_bytes = None
    queue_drops = None
    queue_overlimits = None
    congestion_detected = False
    if qdisc_probe and qdisc_probe.get("status") == "ok":
        queue_depth = qdisc_probe.get("queue_depth")
        queue_depth_bytes = qdisc_probe.get("queue_depth_bytes")
        queue_drops = qdisc_probe.get("dropped_packets")
        queue_overlimits = qdisc_probe.get("overlimits")
        congestion_detected = bool(qdisc_probe.get("congestion_detected"))
    qdisc_label = "Mininet TC qdisc"
    if qdisc_probe and qdisc_probe.get("cached"):
        qdisc_label = "Mininet TC qdisc 缓存"
    elif not qdisc_probe:
        qdisc_label = "点击深度采样"
    elif queue_overlimits:
        qdisc_label = f"TC qdisc historical overlimits={queue_overlimits}"
    elif queue_drops:
        qdisc_label = f"TC qdisc drops={queue_drops}"

    int_hop_count = None
    int_path_latency_ms = None
    int_max_queue_occupancy = None
    int_label = "点击深度采样"
    int_status = "idle"
    int_quality = (int_probe or {}).get("quality_status", "UNAVAILABLE")
    if int_probe and int_probe.get("status") == "ok" and int_probe.get("used_for_decision"):
        int_hop_count = int_probe.get("parsed_hop_count") or int_probe.get("hop_count")
        int_path_latency_ms = int_probe.get("total_hop_latency_ms")
        int_max_queue_occupancy = int_probe.get("max_queue_occupancy")
        int_label = "P4 INT hop-by-hop"
        int_status = "ok" if int_hop_count else "warn"
        if int_probe.get("cached"):
            int_label = "P4 INT 缓存"
    elif int_quality == "DEGRADED":
        int_label = "INT 遥测：部分字段异常，未参与关键决策"
        int_status = "warn"
    elif int_quality == "UNTRUSTED":
        int_label = "INT 遥测：数据格式不一致，已排除"
        int_status = "untrusted"
    elif int_probe and int_probe.get("status") not in (None, "ok"):
        int_label = int_probe.get("error") or "INT 探针不可用"
        int_status = "unavailable"

    throughput_source = (
        f"ONOS infrastructure flow counter, {flow_summary['packet_rate_pps']} pps"
        if throughput_mbps is not None
        else "ONOS infrastructure flow counter: no valid delta sample"
    )

    source_parts = ["onos_rest"]
    if ping_probe and ping_probe.get("status") == "ok":
        source_parts.append("mininet_ping")
    if qdisc_probe and qdisc_probe.get("status") == "ok":
        source_parts.append("mininet_qdisc")
    if int_probe and int_probe.get("status") == "ok":
        source_parts.append("p4_int" if int_quality == TRUSTED else "p4_int_excluded")
    if iperf_probe and iperf_probe.get("status") == "MEASURED":
        source_parts.append(f"service_probe:{service_id}")
    if p4_counter_probe and p4_counter_probe.get("status") == "MEASURED":
        source_parts.append("p4_direct_counter")

    p4_counter_packets = None
    p4_counter_bytes = None
    p4_counter_entries = None
    if p4_counter_probe and p4_counter_probe.get("status") in {"MEASURED", "NO_MATCHING_ENTRY"}:
        p4_counter_packets = p4_counter_probe.get("packets")
        p4_counter_bytes = p4_counter_probe.get("bytes")
        p4_counter_entries = p4_counter_probe.get("direct_entries")

    from api.service_validation import latest_service_metrics

    icmp_status = "MEASURED" if (
        ping_probe and ping_probe.get("status") == "ok"
        and ping_probe.get("transmitted") is not None
    ) else "NO_SAMPLE"
    network_health = {
        "icmp": {
            "status": icmp_status,
            "service_id": health_profile.service_id if health_profile else None,
            "src": (ping_probe or {}).get("src") or (
                health_profile.src_host if health_profile else None
            ),
            "dst": (ping_probe or {}).get("dst") or (
                health_profile.dst_host if health_profile else None
            ),
            "protocol": "ICMP",
            "transmitted": (ping_probe or {}).get("transmitted"),
            "received": (ping_probe or {}).get("received"),
            "loss_percent": (
                float(packet_loss_rate) * 100 if packet_loss_rate is not None else None
            ),
            "rtt_avg_ms": latency_ms,
            "rtt_mdev_ms": jitter_ms,
            "measurement_source": "mininet_ping" if icmp_status == "MEASURED" else None,
            "sample_time": _utc_now() if icmp_status == "MEASURED" else None,
        }
    }
    services = latest_service_metrics()
    selected_tc = selected_service_tc_traffic(service_id, services)
    infrastructure_rate = {
        "status": "MEASURED" if throughput_mbps is not None else "NO_SAMPLE",
        "metric_scope": "NETWORK_AGGREGATE",
        "value_mbps": round(throughput_mbps, 4) if throughput_mbps is not None else None,
        "packet_rate_pps": flow_summary["packet_rate_pps"],
        "source": "onos_flow_counter_delta",
        "unit": "Mbps",
        "measured_at": _utc_now() if throughput_mbps is not None else None,
    }
    result = {
        "status": "ok",
        "source": "+".join(source_parts),
        "timestamp": _utc_now(),
        "devices": {"total": len(devices), "available": available_devices},
        "flows": flow_summary,
        "kpi": {
            "latency_ms": latency_ms,
            "packet_loss_rate": packet_loss_rate,
            "throughput_mbps": round(throughput_mbps, 4) if throughput_mbps is not None else None,
            "service_throughput_mbps": selected_tc["value_mbps"],
            "bottleneck_utilization_percent": selected_tc["utilization_percent"],
            "jitter_ms": jitter_ms,
            "queue_depth": queue_depth,
            "queue_depth_bytes": queue_depth_bytes,
            "queue_drops": queue_drops,
            "queue_overlimits": queue_overlimits,
            "congestion_detected": congestion_detected,
            "int_hop_count": int_hop_count,
            "int_path_latency_ms": int_path_latency_ms,
            "int_max_queue_occupancy": int_max_queue_occupancy,
            "p4_direct_counter_packets": p4_counter_packets,
            "p4_direct_counter_bytes": p4_counter_bytes,
            "p4_direct_counter_entries": p4_counter_entries,
        },
        "kpi_metadata": {
            "latency_ms": {
                "metric_scope": "NETWORK_HEALTH", "metric_source": "mininet_ping",
                "protocol": "ICMP", "service_id": (
                    health_profile.service_id if health_profile else None
                ),
                "measured_at": network_health["icmp"]["sample_time"],
            },
            "packet_loss_rate": {
                "metric_scope": "NETWORK_HEALTH", "metric_source": "mininet_ping",
                "protocol": "ICMP", "service_id": (
                    health_profile.service_id if health_profile else None
                ),
                "measured_at": network_health["icmp"]["sample_time"],
            },
            "throughput_mbps": {
                "metric_scope": "INFRASTRUCTURE", "metric_source": "onos_flow_counter_delta",
                "protocol": None, "service_id": None,
                "measurement_status": infrastructure_rate["status"],
                "measured_at": infrastructure_rate["measured_at"],
            },
            "service_throughput_mbps": {
                "metric_scope": "SELECTED_SERVICE_TC_CLASS",
                "metric_source": "tc_class_counter_delta",
                "protocol": None,
                "service_id": service_id,
                "validation_run_id": selected_tc["validation_run_id"],
                "measurement_status": selected_tc["status"],
            },
        },
        "network_health": network_health,
        "services": services,
        "infrastructure": {
            "devices": {"status": "MEASURED", **{"total": len(devices), "available": available_devices}},
            "flows": {"status": "MEASURED", **flow_summary},
            "queues": {
                "status": "MEASURED" if qdisc_probe and qdisc_probe.get("status") == "ok" else "NO_SAMPLE",
                "scope": "NETWORK_GLOBAL_HISTORICAL",
                "queue_depth": queue_depth,
                "queue_depth_bytes": queue_depth_bytes,
                "dropped_packets": queue_drops,
                "overlimits": queue_overlimits,
                "overlimits_is_drop": False,
                "samples": (qdisc_probe or {}).get("samples") or [],
            },
            "p4_counters": p4_counter_probe or {"status": "TELEMETRY_UNAVAILABLE"},
            "network_exchange_rate": infrastructure_rate,
            "selected_service_tc": selected_tc,
        },
        "metrics": [
            _metric(
                "latency_ms",
                "核心时延",
                round(latency_ms, 2) if latency_ms is not None else None,
                "ms",
                ping_label if latency_ms is not None else "点击深度采样",
                "cyan",
                "ok" if latency_ms is not None else "idle",
            ),
            _metric(
                "packet_loss_rate",
                "ICMP 连通性丢包率",
                round(packet_loss_rate * 100, 2) if packet_loss_rate is not None else None,
                "%",
                ping_label if packet_loss_rate is not None else "点击深度采样",
                "green",
                "ok" if packet_loss_rate is not None else "idle",
            ),
            _metric(
                "throughput_mbps",
                "基础设施流量",
                round(throughput_mbps, 2) if throughput_mbps is not None else None,
                "Mbps",
                throughput_source,
                "indigo",
            ),
            _metric(
                "queue_depth",
                "队列深度",
                queue_depth,
                "pkts",
                qdisc_label if queue_depth is not None else "点击深度采样",
                "slate",
                "warn" if congestion_detected else ("ok" if queue_depth is not None else "idle"),
            ),
            _metric(
                "int_hops",
                "INT逐跳",
                int_hop_count,
                "hops",
                int_label,
                "violet",
                int_status,
            ),
            _metric(
                "p4_direct_counter",
                "P4 Direct Counter",
                p4_counter_packets,
                "pkts",
                (
                    "BMv2 aggregate direct counter inventory"
                    if p4_counter_entries else "目标表项不存在，不能标记为已验证"
                ),
                "cyan",
                (
                    "measured" if p4_counter_probe and p4_counter_probe.get("status") == "MEASURED"
                    else "no_matching_entry" if p4_counter_probe and p4_counter_probe.get("status") == "NO_MATCHING_ENTRY"
                    else "unavailable"
                ),
            ),
        ],
        "raw": {
            "flow_count": len(flows),
            "sample_note": (
                "kpi loss/RTT is ICMP network health; kpi throughput is ONOS infrastructure delta; "
                "business measurements are isolated under services"
            ),
            "ping_probe": ping_probe,
            "qdisc_probe": qdisc_probe,
            "int_probe": int_probe,
            "p4_direct_counter": p4_counter_probe,
            "iperf_probe": iperf_probe,
        },
    }
    # The third summary card is deliberately service-scoped.  The network-wide
    # aggregate remains available under infrastructure.network_exchange_rate.
    result["metrics"][2] = _metric(
        "service_throughput_mbps",
        "当前业务 TC class 吞吐量",
        selected_tc["value_mbps"],
        "Mbps",
        (
            f"{service_id} · {selected_tc['switch']} {selected_tc['interface']} "
            f"class {selected_tc['class_id']} · run {selected_tc['validation_run_id']}"
            if selected_tc["status"] == "MEASURED"
            else (
                f"{service_id} 暂无有效验证样本"
                if service_id else "未选择业务；没有构造默认吞吐样本"
            )
        ),
        "indigo",
        "ok" if selected_tc["status"] == "MEASURED" else "idle",
    )
    return result

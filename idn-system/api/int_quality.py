"""Quality gate for raw P4 INT telemetry.

Raw records are always preserved.  Only a fully trusted probe may feed path
selection, risk scoring or KPI verification; degraded/untrusted observations
remain available for parser diagnostics and the UI audit view.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TRUSTED = "TRUSTED"
DEGRADED = "DEGRADED"
UNTRUSTED = "UNTRUSTED"
UNAVAILABLE = "UNAVAILABLE"

_DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "int_quality.json"


def load_int_quality_config(path: str | Path | None = None) -> dict[str, Any]:
    selected = Path(path or os.getenv("INT_QUALITY_CONFIG_FILE", str(_DEFAULT_CONFIG)))
    try:
        data = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"_config_error": str(exc), "parser_version": "unavailable"}
    data["_config_path"] = str(selected)
    return data


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _check(checks: list[dict[str, Any]], name: str, passed: bool, severity: str, detail: str) -> None:
    checks.append({
        "name": name,
        "passed": bool(passed),
        "severity": severity,
        "detail": f"{name} validation passed" if passed else detail,
    })


def _quality(checks: list[dict[str, Any]]) -> str:
    failed = [item for item in checks if not item["passed"]]
    if any(item["severity"] == UNTRUSTED for item in failed):
        return UNTRUSTED
    if failed:
        return DEGRADED
    return TRUSTED


def _reason(checks: list[dict[str, Any]], quality: str) -> str:
    failures = [item["detail"] for item in checks if not item["passed"]]
    if not failures:
        return "全部 INT 格式、设备、端口、时延、队列、时间戳和路径连续性校验通过"
    return "；".join(failures)


def evaluate_int_probe(
    probe: dict[str, Any] | None,
    *,
    config: dict[str, Any] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Return an audited copy of *probe* with deterministic quality metadata."""
    source = deepcopy(probe or {})
    cfg = config or load_int_quality_config()
    timestamp = observed_at or str(source.get("timestamp") or _now())
    parser_version = str(source.get("parser_version") or "unknown")
    byte_order = str(source.get("byte_order") or cfg.get("byte_order") or "big")
    raw_hops = [item for item in (source.get("hop_records") or []) if isinstance(item, dict)]

    if source.get("status") != "ok" or not raw_hops:
        result = source
        result.update({
            "quality_status": UNAVAILABLE,
            "quality_reason": str(source.get("error") or "没有可校验的 INT 逐跳记录"),
            "used_for_decision": False,
            "validation_checks": [],
            "parser_version": parser_version,
            "timestamp": timestamp,
            "raw_hop_records": deepcopy(raw_hops),
        })
        return result

    if cfg.get("_config_error"):
        result = source
        result.update({
            "quality_status": UNTRUSTED,
            "quality_reason": f"INT 质量配置不可用: {cfg['_config_error']}",
            "used_for_decision": False,
            "validation_checks": [],
            "parser_version": parser_version,
            "timestamp": timestamp,
            "raw_hop_records": deepcopy(raw_hops),
        })
        return result

    allowed = cfg.get("allowed_devices") or {}
    allowed_links = {
        tuple(sorted((str(pair[0]), str(pair[1]))))
        for pair in (cfg.get("allowed_device_links") or [])
        if isinstance(pair, list) and len(pair) == 2
    }
    supported_hop_bytes = {_int(value) for value in cfg.get("supported_hop_bytes") or []}
    allowed_parsers = cfg.get("allowed_parser_versions") or {}
    max_latency = _int(cfg.get("max_hop_latency_ns"), 100_000_000)
    degraded_queue = _int(cfg.get("degraded_queue_occupancy"), 10_000)
    max_queue = _int(cfg.get("max_queue_occupancy"), 1_000_000)
    probe_checks: list[dict[str, Any]] = []

    parser_layout = allowed_parsers.get(parser_version) or {}
    _check(
        probe_checks,
        "parser_version",
        parser_version in allowed_parsers,
        UNTRUSTED,
        f"INT parser_version {parser_version} 未在允许列表中",
    )
    _check(
        probe_checks,
        "schema_version",
        _int(source.get("schema_version"), -1) == _int(cfg.get("schema_version"), -2),
        UNTRUSTED,
        f"INT schema_version {source.get('schema_version')} 与定义 {cfg.get('schema_version')} 不一致",
    )

    declared_bytes = source.get("hop_record_bytes")
    payload_bytes = source.get("packet_payload_bytes", source.get("payload_bytes"))
    if declared_bytes is not None:
        expected_hop_bytes = _int(parser_layout.get("hop_record_bytes"), -1)
        _check(
            probe_checks,
            "format_length",
            _int(declared_bytes) in supported_hop_bytes
            and expected_hop_bytes > 0
            and _int(declared_bytes) == expected_hop_bytes,
            UNTRUSTED, f"INT hop 长度 {_int(declared_bytes)} 不在支持范围 {sorted(supported_hop_bytes)}",
        )
    else:
        _check(
            probe_checks,
            "format_length",
            False,
            UNTRUSTED,
            "INT hop_record_bytes 缺失",
        )
    if payload_bytes is not None:
        expected_payload_min = (
            len(raw_hops) * _int(parser_layout.get("hop_record_bytes"), 0)
            + _int(parser_layout.get("payload_prefix_bytes"), 0)
        )
        _check(
            probe_checks,
            "payload_length",
            expected_payload_min > 0 and _int(payload_bytes) >= expected_payload_min,
            UNTRUSTED,
            f"INT payload 长度 {_int(payload_bytes)} 与 {len(raw_hops)} 个 hop 不匹配",
        )
    _check(
        probe_checks, "byte_order", byte_order == str(cfg.get("byte_order", "big")),
        UNTRUSTED, f"INT 字节序 {byte_order} 与定义 {cfg.get('byte_order', 'big')} 不一致",
    )
    declared_count = source.get("parsed_hop_count")
    if declared_count is not None:
        _check(
            probe_checks, "hop_count", _int(declared_count) == len(raw_hops), UNTRUSTED,
            f"声明 hop 数 {_int(declared_count)} 与解析记录 {len(raw_hops)} 不一致",
        )

    enriched: list[dict[str, Any]] = []
    device_path: list[str] = []
    for index, raw in enumerate(raw_hops):
        checks: list[dict[str, Any]] = []
        raw_switch_id = _int(raw.get("switch_id_raw"))
        device_id = str(raw.get("device_id") or (f"device:bmv2:{raw_switch_id}" if raw_switch_id else ""))
        device_cfg = allowed.get(device_id) or {}
        _check(checks, "device_id", device_id in allowed, UNTRUSTED, f"未知 INT device_id: {device_id or 'empty'}")
        if raw_switch_id:
            _check(
                checks, "switch_id", device_id == f"device:bmv2:{raw_switch_id}", UNTRUSTED,
                f"原始 switch_id={raw_switch_id} 与映射 {device_id} 不一致",
            )
        switch_name = raw.get("switch_name")
        if switch_name:
            _check(
                checks, "switch_mapping", switch_name == device_cfg.get("switch_name"), UNTRUSTED,
                f"device/switch 映射不一致: {device_id}/{switch_name}",
            )

        ports = {_int(value) for value in device_cfg.get("ports") or []}
        ingress_port = _int(raw.get("ingress_port"), -1)
        egress_port = _int(raw.get("egress_port"), -1)
        _check(checks, "ingress_port", ingress_port in ports, UNTRUSTED, f"{device_id} 入端口 {ingress_port} 不存在")
        _check(checks, "egress_port", egress_port in ports, UNTRUSTED, f"{device_id} 出端口 {egress_port} 不存在")

        ingress_ts = _int(raw.get("ingress_timestamp_ns"))
        egress_ts = _int(raw.get("egress_timestamp_ns"))
        explicit_latency = raw.get("hop_latency_ns")
        latency = _int(explicit_latency) if explicit_latency is not None else egress_ts - ingress_ts
        _check(checks, "latency_non_negative", latency >= 0, UNTRUSTED, f"hop {index} 时延为负数: {latency}ns")
        _check(checks, "latency_upper_bound", latency <= max_latency, UNTRUSTED, f"hop {index} 时延 {latency}ns 超过上限 {max_latency}ns")
        timestamps_valid = ingress_ts > 0 and egress_ts >= ingress_ts
        _check(checks, "timestamp", timestamps_valid, UNTRUSTED, f"hop {index} 时间戳无效或顺序颠倒")

        queue = _int(raw.get("queue_occupancy"), -1)
        _check(checks, "queue_non_negative", queue >= 0, UNTRUSTED, f"hop {index} queue occupancy 为负数: {queue}")
        _check(checks, "queue_hard_limit", queue <= max_queue, UNTRUSTED, f"hop {index} queue occupancy {queue} 超过硬上限 {max_queue}")
        _check(checks, "queue_reasonable", queue <= degraded_queue, DEGRADED, f"hop {index} queue occupancy {queue} 超过可信阈值 {degraded_queue}")
        flags = _int(raw.get("flags"), 0)
        _check(checks, "hop_valid_flag", bool(flags & 1), UNTRUSTED, f"hop {index} 缺少有效标志")

        hop_quality = _quality(checks)
        parsed = {
            "hop_index": _int(raw.get("hop_index"), index),
            "device_id": device_id or None,
            "switch_name": switch_name or device_cfg.get("switch_name"),
            "switch_id_raw": raw_switch_id,
            "ingress_port": ingress_port,
            "egress_port": egress_port,
            "ingress_timestamp_ns": ingress_ts,
            "egress_timestamp_ns": egress_ts,
            "hop_latency_ns": latency,
            "queue_occupancy": queue,
            "flags": flags,
            "next_node": raw.get("next_node"),
            "link_id": raw.get("link_id"),
        }
        enriched.append({
            **parsed,
            "raw_value": deepcopy(raw),
            "parsed_value": deepcopy(parsed),
            "unit": {"hop_latency": "ns", "timestamp": "ns", "queue_occupancy": "packets"},
            "source": str(source.get("source") or "p4_int_probe"),
            "parser_version": parser_version,
            "timestamp": timestamp,
            "quality_status": hop_quality,
            "quality_reason": _reason(checks, hop_quality),
            "used_for_decision": hop_quality == TRUSTED,
            "validation_checks": checks,
        })
        device_path.append(device_id)
        probe_checks.extend({**item, "hop_index": index} for item in checks)

    for index, (left, right) in enumerate(zip(device_path, device_path[1:])):
        pair = tuple(sorted((left, right)))
        _check(
            probe_checks, "path_continuity", pair in allowed_links, UNTRUSTED,
            f"INT hop 顺序不连续: {left} -> {right} (index {index})",
        )

    overall = _quality(probe_checks)
    used = overall == TRUSTED
    result = source
    result.update({
        "hop_records": enriched,
        "raw_hop_records": deepcopy(raw_hops),
        "parser_version": parser_version,
        "byte_order": byte_order,
        "timestamp": timestamp,
        "quality_status": overall,
        "quality_reason": _reason(probe_checks, overall),
        "used_for_decision": used,
        "validation_checks": probe_checks,
    })
    return result


def decision_safe_int_probe(probe: dict[str, Any] | None) -> dict[str, Any]:
    """Return evaluated data; consumers must use only when flag is true."""
    if isinstance(probe, dict) and probe.get("quality_status") in {TRUSTED, DEGRADED, UNTRUSTED, UNAVAILABLE}:
        return probe
    return evaluate_int_probe(probe)

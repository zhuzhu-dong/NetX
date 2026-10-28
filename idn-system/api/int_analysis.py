"""INT path analysis helpers used by verification and healing reports.

Path definitions (switch sequences, bottleneck links, checkpoint
hop pairs) are loaded from ``config/topology_paths.json`` via the
module-level ``topology_paths`` singleton.  Remove or edit that
file to adapt the analyser to a different topology.
"""
from __future__ import annotations

import os
from typing import Any

from api.int_quality import TRUSTED, decision_safe_int_probe
from api.topology_paths import topology_paths


def _queue_threshold() -> int:
    try:
        return int(os.getenv("IDN_INT_QUEUE_CONGESTION_THRESHOLD", "1"))
    except ValueError:
        return 1


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _contains_sequence(path: list[str], expected: list[str]) -> bool:
    if not path or not expected or len(path) < len(expected):
        return False
    for index in range(0, len(path) - len(expected) + 1):
        if path[index:index + len(expected)] == expected:
            return True
    return False


def _hop_latency_ns(hop: dict[str, Any]) -> int:
    if hop.get("hop_latency_ns") is not None:
        return int(hop.get("hop_latency_ns") or 0)
    ingress = int(hop.get("ingress_timestamp_ns") or 0)
    egress = int(hop.get("egress_timestamp_ns") or 0)
    return max(0, egress - ingress)


def _compact_hop(hop: dict[str, Any]) -> dict[str, Any]:
    latency_ns = _hop_latency_ns(hop)
    return {
        "hop_index": hop.get("hop_index"),
        "switch_name": hop.get("switch_name"),
        "device_id": hop.get("device_id"),
        "switch_id_raw": int(hop.get("switch_id_raw") or 0),
        "ingress_port": hop.get("ingress_port"),
        "egress_port": hop.get("egress_port"),
        "next_node": hop.get("next_node"),
        "link_id": hop.get("link_id"),
        "hop_latency_ns": latency_ns,
        "hop_latency_ms": latency_ns / 1_000_000,
        "queue_occupancy": int(hop.get("queue_occupancy") or 0),
    }


def _int_probe_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    raw = payload.get("raw") or {}
    if isinstance(raw, dict) and isinstance(raw.get("int_probe"), dict):
        return raw["int_probe"]
    if isinstance(payload.get("int_probe"), dict):
        return payload["int_probe"]
    if isinstance(payload.get("raw"), dict) and payload["raw"].get("hop_records"):
        return payload["raw"]
    if payload.get("hop_records"):
        return payload
    return {}


def analyze_int_probe(int_probe: dict[str, Any] | None) -> dict[str, Any]:
    """Return deterministic bottleneck/path facts from one INT probe payload."""
    int_probe = decision_safe_int_probe(int_probe)
    raw_hops = [_as_dict(item) for item in (int_probe.get("hop_records") or [])]
    audit_hops = [_compact_hop(hop) for hop in raw_hops]
    trusted = int_probe.get("quality_status") == TRUSTED and bool(int_probe.get("used_for_decision"))
    # Derivations are deliberately empty for degraded/untrusted data.  The
    # audited records remain in ``hop_records`` for parser diagnosis.
    hops = audit_hops if trusted else []
    switch_path = [
        str(hop.get("switch_name") or hop.get("device_id") or hop.get("switch_id_raw"))
        for hop in hops
        if hop.get("switch_name") or hop.get("device_id") or hop.get("switch_id_raw")
    ]
    device_path = [
        str(hop.get("device_id"))
        for hop in hops
        if hop.get("device_id")
    ]
    links = [
        str(hop.get("link_id"))
        for hop in hops
        if hop.get("link_id")
    ]

    worst_latency = max(hops, key=lambda hop: hop["hop_latency_ns"], default=None)
    worst_queue = max(hops, key=lambda hop: hop["queue_occupancy"], default=None)
    max_queue = int(worst_queue["queue_occupancy"]) if worst_queue else 0
    threshold = _queue_threshold()

    primary = topology_paths.get("primary")
    secondary = topology_paths.get("secondary")

    primary_links = primary.bottleneck_links if primary else set()
    secondary_links = secondary.bottleneck_links if secondary else set()

    primary_path = (
        (primary and _contains_sequence(switch_path, primary.switch_path))
        or any(link in primary_links for link in links)
    )
    secondary_path = (
        (secondary and _contains_sequence(switch_path, secondary.switch_path))
        or any(link in secondary_links for link in links)
    )
    worst_queue_link = str(worst_queue.get("link_id") or "") if worst_queue else ""
    worst_latency_link = str(worst_latency.get("link_id") or "") if worst_latency else ""
    bottleneck_link = worst_queue_link or worst_latency_link or None

    # Generalised bottleneck-checkpoint matching (was hardcoded s_agg_r→s_core1).
    _checkpoint_hit = False
    if worst_queue and primary and primary.bottleneck_checkpoints:
        _checkpoint_hit = any(
            worst_queue.get("switch_name") == cp.switch_name
            and worst_queue.get("next_node") == cp.next_node
            for cp in primary.bottleneck_checkpoints
        )
    bottleneck_on_primary = bool(
        bottleneck_link in primary_links or _checkpoint_hit
    )

    return {
        "status": "ok" if trusted else ("unavailable" if not raw_hops else "excluded"),
        "quality_status": int_probe.get("quality_status", "UNAVAILABLE"),
        "quality_reason": int_probe.get("quality_reason", "没有可用的INT记录"),
        "used_for_decision": trusted,
        "validation_checks": int_probe.get("validation_checks") or [],
        "hop_count": len(audit_hops),
        "decision_hop_count": len(hops),
        "switch_path": switch_path,
        "device_path": device_path,
        "links": links,
        "primary_path": primary_path,
        "secondary_path": secondary_path,
        "bottleneck_link": bottleneck_link,
        "bottleneck_on_primary": bottleneck_on_primary,
        "congestion_on_primary": bool(primary_path and (max_queue >= threshold or bottleneck_on_primary)),
        "queue_threshold": threshold,
        "worst_latency_hop": worst_latency,
        "worst_queue_hop": worst_queue,
        "max_queue_occupancy": max_queue,
        "path_latency_ms": float(int_probe.get("total_hop_latency_ms") or 0.0) if trusted else None,
        "hop_records": audit_hops,
        "raw_hop_records": int_probe.get("raw_hop_records") or [],
    }


def analyze_telemetry(telemetry: Any) -> dict[str, Any]:
    """Analyze INT facts from a TelemetryData object or telemetry payload dict."""
    data = _as_dict(telemetry)
    raw = data.get("raw_int_data") if isinstance(data.get("raw_int_data"), dict) else data
    int_probe = _int_probe_from_payload(raw)
    if not int_probe and data.get("int_path_records"):
        int_probe = {
            "hop_records": [
                {
                    **_as_dict(item),
                    "switch_id_raw": 0,
                }
                for item in data.get("int_path_records") or []
            ]
        }
    return analyze_int_probe(int_probe)


def analysis_from_verification_report(report: dict[str, Any] | None) -> dict[str, Any]:
    report = report or {}
    if isinstance(report.get("int_analysis"), dict):
        return report["int_analysis"]
    if report.get("raw_int_data"):
        return analyze_int_probe(_int_probe_from_payload(report.get("raw_int_data")))
    if report.get("int_path_records"):
        return analyze_int_probe({"hop_records": report.get("int_path_records")})
    return analyze_int_probe(None)


def _metric_delta(before: dict[str, Any], after: dict[str, Any], name: str, higher_is_better: bool) -> dict[str, Any]:
    before_value = before.get(name)
    after_value = after.get(name)
    if before_value is None or after_value is None:
        return {"before": before_value, "after": after_value, "delta": None, "improved": None}
    delta = float(after_value) - float(before_value)
    improved = delta > 0 if higher_is_better else delta < 0
    return {
        "before": before_value,
        "after": after_value,
        "delta": round(delta, 6),
        "improved": improved,
    }


def build_healing_comparison(
    pre_report: dict[str, Any],
    post_report: dict[str, Any],
    healing_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare pre/post Verify reports for the demo self-healing story."""
    pre_int = analysis_from_verification_report(pre_report)
    post_int = analysis_from_verification_report(post_report)
    pre_kpi = pre_report.get("kpi") or {}
    post_kpi = post_report.get("kpi") or {}
    deltas = {
        "latency_ms": _metric_delta(pre_kpi, post_kpi, "latency_ms", higher_is_better=False),
        "packet_loss_rate": _metric_delta(pre_kpi, post_kpi, "packet_loss_rate", higher_is_better=False),
        "throughput_mbps": _metric_delta(pre_kpi, post_kpi, "throughput_mbps", higher_is_better=True),
        "queue_depth": _metric_delta(pre_kpi, post_kpi, "queue_depth", higher_is_better=False),
        "int_path_latency_ms": _metric_delta(pre_kpi, post_kpi, "int_path_latency_ms", higher_is_better=False),
        "int_max_queue_occupancy": _metric_delta(pre_kpi, post_kpi, "int_max_queue_occupancy", higher_is_better=False),
    }
    path_changed = pre_int.get("switch_path") != post_int.get("switch_path")
    switched_to_backup = bool(pre_int.get("primary_path") and post_int.get("secondary_path"))
    improved = any(item.get("improved") is True for item in deltas.values())
    recovered = bool(post_report.get("passed") or switched_to_backup or improved)
    return {
        "status": "recovered" if recovered else "not_recovered",
        "pre_int": pre_int,
        "post_int": post_int,
        "path_changed": path_changed,
        "switched_to_backup": switched_to_backup,
        "congestion_was_on_primary": bool(pre_int.get("congestion_on_primary")),
        "kpi_deltas": deltas,
        "kpi_improved": improved,
        "healing_actions": (healing_result or {}).get("actions") or [],
        "summary": (
            f"before_path={' -> '.join(pre_int.get('switch_path') or [])}; "
            f"after_path={' -> '.join(post_int.get('switch_path') or [])}; "
            f"switched_to_backup={switched_to_backup}; kpi_improved={improved}"
        ),
    }

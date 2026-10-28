"""Demo orchestration helpers used by the REST routes.

The public endpoints live in ``api.routes``.  This module keeps the demo
workflow small and compositional by reusing the existing baseline, fault,
healing, and telemetry helpers.
"""

from __future__ import annotations

from typing import Any


async def reset_demo_environment() -> dict[str, Any]:
    """Clear transient demo state and reinstall baseline connectivity."""
    steps: dict[str, Any] = {}

    try:
        from api.healing import clear_congestion_fault

        steps["fault_clear"] = await clear_congestion_fault(None)
    except Exception as exc:
        steps["fault_clear"] = {"status": "error", "error": str(exc)}

    try:
        from api.qos import clear_qos_policies

        steps["qos_clear"] = await clear_qos_policies(None)
    except Exception as exc:
        steps["qos_clear"] = {"status": "error", "error": str(exc)}

    try:
        from api.baseline import install_baseline_connectivity

        steps["bootstrap"] = await install_baseline_connectivity()
    except Exception as exc:
        steps["bootstrap"] = {"status": "error", "error": str(exc)}

    try:
        from api.telemetry import collect_current_telemetry

        steps["telemetry"] = await collect_current_telemetry(include_probe=True, include_iperf=False)
    except Exception as exc:
        steps["telemetry"] = {"status": "error", "error": str(exc)}

    bootstrap_ok = steps.get("bootstrap", {}).get("status") == "SUCCESS"
    telemetry_ok = steps.get("telemetry", {}).get("status") == "ok"
    return {
        "status": "ready" if bootstrap_ok and telemetry_ok else "partial",
        "steps": steps,
        "telemetry": steps.get("telemetry"),
    }


async def run_demo_preflight(reset_first: bool = True) -> dict[str, Any]:
    """Validate that demo dependencies are available."""
    steps: dict[str, Any] = {}
    if reset_first:
        steps["reset"] = await reset_demo_environment()

    try:
        from api.topology import collect_current_topology

        steps["topology"] = await collect_current_topology()
    except Exception as exc:
        steps["topology"] = {"status": "error", "error": str(exc)}

    try:
        from api.telemetry import collect_current_telemetry

        steps["telemetry"] = await collect_current_telemetry(include_probe=True, include_iperf=False)
    except Exception as exc:
        steps["telemetry"] = {"status": "error", "error": str(exc)}

    topology_ok = steps.get("topology", {}).get("status") == "ok"
    telemetry_ok = steps.get("telemetry", {}).get("status") == "ok"
    reset_ok = not reset_first or steps.get("reset", {}).get("status") in {"ready", "partial"}
    return {
        "status": "ready" if topology_ok and telemetry_ok and reset_ok else "partial",
        "steps": steps,
    }


async def run_self_healing_demo(
    *,
    reset_first: bool = True,
    include_iperf: bool = True,
    clear_after: bool = False,
    fault: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the one-click self-healing evidence workflow.

    This routine intentionally stays route-level: it captures before/fault/after
    telemetry evidence and delegates actual repair behavior to the existing
    baseline and fault helpers.
    """
    from api.baseline import install_baseline_connectivity
    from api.healing import clear_congestion_fault, inject_congestion_fault
    from api.telemetry import collect_current_telemetry

    steps: dict[str, Any] = {}
    if reset_first:
        steps["reset"] = await reset_demo_environment()

    before = await collect_current_telemetry(include_probe=True, include_iperf=include_iperf)
    steps["before"] = before

    fault_cfg = {
        "fault_id": "primary-path-congestion",
        "node": "s_agg_r",
        "interface": "s_agg_r-eth3",
        "rate_mbps": 10,
        "burst_kb": 64,
        "latency_ms": 400,
    }
    if fault:
        fault_cfg.update({k: v for k, v in fault.items() if v is not None})

    steps["fault"] = await inject_congestion_fault(**fault_cfg)
    congested = await collect_current_telemetry(include_probe=True, include_iperf=include_iperf)
    steps["congested"] = congested

    # The current repair path reinstalls baseline forwarding after the fault
    # evidence is captured.  More advanced reroute policies can still be layered
    # by the normal intent pipeline.
    steps["repair"] = await install_baseline_connectivity()
    after = await collect_current_telemetry(include_probe=True, include_iperf=include_iperf)
    steps["after"] = after

    if clear_after:
        steps["clear_after"] = await clear_congestion_fault(fault_cfg.get("fault_id"))

    comparison = _build_comparison(before, congested, after, fault_cfg)
    return {
        "status": "success" if after.get("status") == "ok" else "partial",
        "steps": steps,
        "comparison": comparison,
    }


def _build_comparison(
    before: dict[str, Any],
    congested: dict[str, Any],
    after: dict[str, Any],
    fault: dict[str, Any],
) -> dict[str, Any]:
    before_kpi = before.get("kpi") or {}
    after_kpi = after.get("kpi") or {}
    congested_raw = congested.get("raw") or {}
    qdisc = congested_raw.get("qdisc_probe") or {}
    int_probe = congested_raw.get("int_probe") or {}

    return {
        "self_heal_before_path_text": _path_text(before),
        "before_path_text": _path_text(before),
        "after_path_text": _path_text(after),
        "congested_link": f"{fault.get('node')}:{fault.get('interface')}",
        "switched_to_backup": False,
        "kpi_delta": {
            "latency_ms": _delta(before_kpi, after_kpi, "latency_ms"),
            "packet_loss_rate": _delta(before_kpi, after_kpi, "packet_loss_rate"),
            "throughput_mbps": _delta(before_kpi, after_kpi, "throughput_mbps"),
            "queue_depth": _delta(before_kpi, after_kpi, "queue_depth"),
        },
        "evidence": {
            "qdisc": qdisc,
            "int": int_probe,
        },
    }


def _delta(before: dict[str, Any], after: dict[str, Any], key: str) -> dict[str, Any]:
    b = before.get(key)
    a = after.get(key)
    return {
        "before": b,
        "after": a,
        "delta": (a - b) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None,
    }


def _path_text(snapshot: dict[str, Any]) -> str:
    raw = snapshot.get("raw") or {}
    ping = raw.get("ping_probe") or {}
    src = ping.get("src") or "h_research"
    dst = ping.get("dst") or ping.get("dst_ip") or "h_dc"
    return f"{src} -> {dst}"

"""Evidence-based packet accounting and software-dataplane diagnostics."""
from __future__ import annotations

import asyncio
import math
import statistics
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api._probe_session import get_probe_session
from api.service_profiles import get_service_profile
from api.store import get_store
from orchestrator.traffic_orchestrator import get_traffic_orchestrator
from persistence.telemetry_store import TelemetryStore

router = APIRouter(prefix="/api/v1/diagnostics", tags=["diagnostics"])


class PacketAccountingRequest(BaseModel):
    workload_id: str
    measurement_window_seconds: float = Field(default=2, ge=0.2, le=30)


class RepeatedAccountingRequest(PacketAccountingRequest):
    count: int = Field(default=3, ge=2, le=20)


def summarize_values(values: list[float]) -> dict[str, float | int | None]:
    clean = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not clean:
        return {key: None for key in ("mean", "median", "std", "min", "max", "p50", "p95")} | {"count": 0}

    def percentile(fraction: float) -> float:
        if len(clean) == 1:
            return clean[0]
        position = (len(clean) - 1) * fraction
        lower, upper = math.floor(position), math.ceil(position)
        if lower == upper:
            return clean[lower]
        return clean[lower] + (clean[upper] - clean[lower]) * (position - lower)

    return {
        "count": len(clean), "mean": statistics.fmean(clean),
        "median": statistics.median(clean),
        "std": statistics.pstdev(clean), "min": clean[0], "max": clean[-1],
        "p50": percentile(0.50), "p95": percentile(0.95),
    }


def _scope_for_workload(service_id: str) -> str | None:
    with get_store()._lock, get_store()._connect() as connection:
        rows = connection.execute(
            """SELECT traffic_scope_key FROM qos_resource_bindings
            WHERE service_id=? AND active=1 ORDER BY created_at DESC""",
            (service_id,),
        ).fetchall()
    return str(rows[0]["traffic_scope_key"]) if len(rows) == 1 else None


async def run_packet_accounting(request: PacketAccountingRequest) -> dict[str, Any]:
    workload = get_traffic_orchestrator().get(request.workload_id)
    if workload is None:
        raise KeyError(request.workload_id)
    profile = get_service_profile(workload.service_id)
    from api.p4_evidence import read_p4_snapshot

    p4_before = await read_p4_snapshot(profile)
    probe = await get_probe_session()
    result = await probe.post("/diagnostics/packet-accounting", {
        "workload_id": workload.workload_id, "service_id": workload.service_id,
        "src_endpoint": workload.src_endpoint, "dst_endpoint": workload.dst_endpoint,
        "selector": workload.selector.model_dump(mode="json"),
        "traffic_scope_key": _scope_for_workload(workload.service_id),
        "measurement_window_seconds": request.measurement_window_seconds,
    })
    if not result or result.get("status") != "ok":
        raise RuntimeError(str((result or {}).get("error") or "packet accounting unavailable"))
    p4_after = await read_p4_snapshot(profile)
    exact = (
        p4_before.get("status") == "VERIFIED" and p4_after.get("status") == "VERIFIED"
        and p4_before.get("counter_id") == p4_after.get("counter_id")
        and p4_before.get("table_entry_index") == p4_after.get("table_entry_index")
    )
    p4_delta = (
        int(p4_after.get("packets") or 0) - int(p4_before.get("packets") or 0)
        if exact and int(p4_after.get("packets") or 0) >= int(p4_before.get("packets") or 0)
        else None
    )
    for point in result.get("points") or []:
        if point.get("point_id") == "p4:classifier":
            point.update({
                "counter_before": p4_before.get("packets"),
                "counter_after": p4_after.get("packets"), "delta": p4_delta,
                "quality_status": "EXACT_SELECTOR" if p4_delta is not None else "UNKNOWN",
                "source": "p4runtime_direct_counter",
            })
    point_delta = {item["point_id"]: item.get("delta") for item in result.get("points") or []}
    for segment in result.get("segments") or []:
        if segment.get("segment_id") != "p4_classifier_to_tc_class":
            continue
        sent, received = point_delta.get("p4:classifier"), point_delta.get("tc:classified")
        if sent is not None and received is not None:
            difference = max(0, sent - received)
            segment.update({
                "packets_before": sent, "packets_after": received,
                "packet_difference": difference,
                "loss_pct": difference / sent * 100.0 if sent > 0 else 0.0,
                "status": "MEASURED_EXACT", "reason": None,
            })
    result["p4_evidence"] = {"before": p4_before, "after": p4_after, "delta_packets": p4_delta}
    TelemetryStore(get_store()).record_generic("PACKET_ACCOUNTING", result, "MEASURED")
    return result


@router.post("/packet-accounting")
async def packet_accounting(request: PacketAccountingRequest):
    try:
        return await run_packet_accounting(request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown workload_id: {request.workload_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/packet-accounting/repeated")
async def repeated_packet_accounting(request: RepeatedAccountingRequest):
    runs = []
    for _ in range(request.count):
        runs.append(await run_packet_accounting(PacketAccountingRequest(
            workload_id=request.workload_id,
            measurement_window_seconds=request.measurement_window_seconds,
        )))
    segment_ids = sorted({item["segment_id"] for run in runs for item in run.get("segments") or []})
    summary = {}
    for segment_id in segment_ids:
        values = [
            item.get("loss_pct") for run in runs for item in run.get("segments") or []
            if item["segment_id"] == segment_id and item.get("loss_pct") is not None
        ]
        summary[segment_id] = summarize_values(values)
    return {"status": "ok", "count": len(runs), "runs": runs, "segment_loss_statistics": summary}


@router.get("/environment")
async def environment_diagnostics():
    probe = await get_probe_session()
    result = await probe.get("/diagnostics/environment")
    if not result:
        raise HTTPException(status_code=502, detail="environment diagnostics unavailable")
    return result

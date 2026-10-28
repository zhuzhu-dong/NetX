"""Persisted telemetry history APIs; reads never trigger active probes."""
from __future__ import annotations

from fastapi import APIRouter, Query

from api.store import get_store
from persistence.telemetry_store import TelemetryStore

router = APIRouter(prefix="/api/v1", tags=["telemetry-history"])


@router.get("/telemetry/history")
async def telemetry_history(
    sample_type: str | None = None, limit: int = Query(300, ge=1, le=5000),
):
    return TelemetryStore(get_store()).generic_history(sample_type=sample_type, limit=limit)


@router.get("/interfaces/{resource_id}/metrics/history")
async def interface_metric_history(
    resource_id: str, limit: int = Query(300, ge=1, le=5000),
):
    return TelemetryStore(get_store()).interface_history(resource_id, limit=limit)


@router.get("/services/{service_id}/metrics/history")
async def persisted_service_metric_history(
    service_id: str, traffic_role: str | None = None,
    limit: int = Query(300, ge=1, le=5000),
):
    return TelemetryStore(get_store()).service_history(
        service_id, traffic_role=traffic_role, limit=limit,
    )


@router.get("/workloads/{workload_id}/metrics/history")
async def workload_metric_history(
    workload_id: str, limit: int = Query(300, ge=1, le=5000),
):
    return TelemetryStore(get_store()).workload_history(workload_id, limit=limit)

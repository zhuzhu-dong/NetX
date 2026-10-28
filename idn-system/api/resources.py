"""Canonical resource registry, allocator and capacity-ledger APIs."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.store import get_store
from infra.qos_resource_allocator import QoSResourceAllocator
from infra.resource_ledger import ResourceLedger
from infra.resource_registry import get_resource_registry
from infra.stable_capacity import StableCapacityRepository
from models.resources import StableDataplaneCapacityRecord

router = APIRouter(prefix="/api/v1", tags=["resources"])


class AdmissionRequest(BaseModel):
    resource_id: str
    requested_mbps: float = Field(gt=0)


class CapacityCalibrationRequest(BaseModel):
    samples: int = Field(default=5, ge=3, le=10)
    duration_seconds: int = Field(default=3, ge=1, le=15)
    required_schedulable_mbps: float | None = Field(default=None, gt=0)


@router.get("/resources/registry")
async def resource_registry():
    return get_resource_registry().snapshot()


@router.get("/resources/resolve")
async def resolve_resource(alias: str = Query(..., min_length=1)):
    try:
        return {"alias": alias, "resource_id": get_resource_registry().resolve(alias)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/resources/ledger")
async def resource_ledger():
    return ResourceLedger(get_store()).entries()


@router.get("/resources/stable-capacity")
async def stable_capacity_records(bottleneck_egress: str | None = None):
    return StableCapacityRepository(get_store()).list(bottleneck_egress)


@router.post("/resources/stable-capacity")
async def put_stable_capacity_record(record: StableDataplaneCapacityRecord):
    try:
        saved = StableCapacityRepository(get_store()).put(record)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return saved


@router.post("/resources/stable-capacity/calibrate/{traffic_scope_key}")
async def calibrate_stable_capacity(traffic_scope_key: str, request: CapacityCalibrationRequest):
    """Create current capacity evidence from real receiver-side Mininet probes."""
    from infra.capacity_calibration import ActiveIngressCapacityCalibrator, CapacityCalibrationError
    try:
        return await ActiveIngressCapacityCalibrator(get_store()).calibrate(
            traffic_scope_key,
            samples=request.samples,
            duration_seconds=request.duration_seconds,
            required_schedulable_mbps=request.required_schedulable_mbps,
        )
    except CapacityCalibrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/resources/admission")
async def admission(request: AdmissionRequest):
    try:
        return ResourceLedger(get_store()).admission(
            get_resource_registry().resolve(request.resource_id), request.requested_mbps,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/resources/qos-bindings")
async def qos_bindings():
    return QoSResourceAllocator(get_store()).list_active()


@router.get("/audit")
async def audit_events(
    resource_type: str | None = None, resource_id: str | None = None,
    event_type: str | None = None, limit: int = Query(200, ge=1, le=1000),
):
    return get_store().list_audit_events(
        resource_type=resource_type, resource_id=resource_id,
        event_type=event_type, limit=limit,
    )

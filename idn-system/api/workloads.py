"""REST API for persistent, long-running traffic workloads."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from api.store import get_store
from models.traffic_workload import (
    TrafficWorkload,
    TrafficWorkloadCreate,
    TrafficWorkloadPatch,
)
from orchestrator.traffic_orchestrator import (
    TrafficGeneratorError,
    get_traffic_orchestrator,
)

router = APIRouter(prefix="/api/v1/workloads", tags=["workloads"])


class ServiceWorkloadRequest(BaseModel):
    service_id: str
    overrides: dict[str, Any] = {}


def _not_found(workload_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"unknown workload_id: {workload_id}")


@router.post("", response_model=TrafficWorkload, status_code=201)
async def create_workload(request: TrafficWorkloadCreate):
    try:
        return await get_traffic_orchestrator().create(request)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TrafficGeneratorError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/from-service", response_model=TrafficWorkload, status_code=201)
async def create_workload_from_service(request: ServiceWorkloadRequest):
    try:
        create = get_traffic_orchestrator().from_service_profile(
            request.service_id, **request.overrides,
        )
        return await get_traffic_orchestrator().create(create)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TrafficGeneratorError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("", response_model=list[TrafficWorkload])
async def list_workloads(
    service_id: str | None = None,
    lifecycle_state: str | None = None,
    traffic_role: str | None = None,
):
    return get_traffic_orchestrator().list(
        service_id=service_id, lifecycle_state=lifecycle_state, traffic_role=traffic_role,
    )


@router.get("/{workload_id}", response_model=TrafficWorkload)
async def get_workload(workload_id: str):
    workload = get_traffic_orchestrator().get(workload_id)
    if workload is None:
        raise _not_found(workload_id)
    return workload


@router.get("/{workload_id}/events")
async def get_workload_events(workload_id: str):
    if get_traffic_orchestrator().get(workload_id) is None:
        raise _not_found(workload_id)
    return get_store().list_workload_events(workload_id)


@router.post("/{workload_id}/start", response_model=TrafficWorkload)
async def start_workload(workload_id: str):
    try:
        return await get_traffic_orchestrator().start(workload_id)
    except KeyError as exc:
        raise _not_found(workload_id) from exc
    except (ValueError, TrafficGeneratorError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{workload_id}/stop", response_model=TrafficWorkload)
async def stop_workload(workload_id: str):
    try:
        return await get_traffic_orchestrator().stop(workload_id)
    except KeyError as exc:
        raise _not_found(workload_id) from exc
    except (ValueError, TrafficGeneratorError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/{workload_id}", response_model=TrafficWorkload)
async def patch_workload(workload_id: str, request: TrafficWorkloadPatch):
    try:
        return await get_traffic_orchestrator().patch(workload_id, request)
    except KeyError as exc:
        raise _not_found(workload_id) from exc
    except (ValueError, TrafficGeneratorError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{workload_id}", status_code=204)
async def delete_workload(workload_id: str):
    try:
        await get_traffic_orchestrator().delete(workload_id)
    except KeyError as exc:
        raise _not_found(workload_id) from exc
    return Response(status_code=204)


@router.post("/_reconcile")
async def reconcile_workloads():
    try:
        return await get_traffic_orchestrator().reconcile()
    except TrafficGeneratorError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


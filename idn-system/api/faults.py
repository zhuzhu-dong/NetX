"""Fault lifecycle and constrained-path APIs."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from api.topology import fetch_mininet_topology
from infra.constrained_path_planner import ConstrainedPathPlanner
from infra.fault_controller import get_fault_controller
from models.faults import FaultDefinition, PathConstraints

router = APIRouter(prefix="/api/v1", tags=["faults"])


@router.post("/faults")
async def apply_fault(definition: FaultDefinition, auto_heal: bool = Query(default=True)):
    try:
        return (await get_fault_controller().apply(definition, heal=auto_heal)).model_dump(mode="json")
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/faults")
async def list_faults(include_removed: bool = Query(default=True)):
    values = get_fault_controller().list(include_removed=include_removed)
    return {"faults": [item.model_dump(mode="json") for item in values], "count": len(values)}


@router.get("/faults/{fault_id}")
async def inspect_fault(fault_id: str, refresh: bool = Query(default=True)):
    try:
        value = await get_fault_controller().inspect(fault_id) if refresh else get_fault_controller().get(fault_id)
        return value.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="fault not found") from exc


@router.delete("/faults/{fault_id}")
async def remove_fault(fault_id: str, auto_heal: bool = Query(default=True)):
    try:
        return (await get_fault_controller().remove(fault_id, heal=auto_heal)).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="fault not found") from exc


@router.post("/faults/reconcile")
async def reconcile_faults():
    return await get_fault_controller().reconcile()


@router.post("/paths/compute")
async def compute_path(source: str, destination: str, constraints: PathConstraints):
    try:
        topology = await fetch_mininet_topology()
        return ConstrainedPathPlanner().compute(topology, source, destination, constraints).model_dump(mode="json")
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

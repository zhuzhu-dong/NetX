"""Budgeted probe scheduler control API."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from infra.probe_scheduler import get_probe_scheduler

router = APIRouter(prefix="/api/v1/probes", tags=["probes"])


class ProbeTrigger(BaseModel):
    service_id: str
    reason: str
    heavy: bool = True


@router.get("/scheduler")
async def probe_scheduler_status():
    return get_probe_scheduler().status()


@router.post("/scheduler/trigger", status_code=202)
async def trigger_probe(request: ProbeTrigger):
    get_probe_scheduler().notify(request.service_id, request.reason, heavy=request.heavy)
    return {"status": "queued", **request.model_dump()}


@router.post("/scheduler/run")
async def run_probe_scheduler():
    return await get_probe_scheduler().run_once()

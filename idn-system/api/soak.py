from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from infra.soak_runner import get_soak_runner

router = APIRouter(prefix="/api/v1/soak", tags=["soak"])


class SoakRequest(BaseModel):
    duration_seconds: float = Field(default=3600, gt=0, le=86400)
    churn_interval_seconds: float = Field(default=30, gt=0, le=3600)
    service_ids: list[str] = Field(min_length=1)


@router.post("/runs")
async def start_soak(request: SoakRequest):
    return await get_soak_runner().start(
        duration_seconds=request.duration_seconds, service_ids=request.service_ids,
        churn_interval_seconds=request.churn_interval_seconds,
    )


@router.get("/runs/{run_id}")
async def soak_status(run_id: str):
    try:
        return get_soak_runner().status(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="soak run not found") from exc

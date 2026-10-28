from __future__ import annotations

from fastapi import APIRouter

from infra.recovery_manager import get_recovery_manager

router = APIRouter(prefix="/api/v1/recovery", tags=["recovery"])


@router.get("/status")
async def recovery_status():
    return get_recovery_manager().status()


@router.post("/run")
async def run_recovery():
    return await get_recovery_manager().recover_once(trigger="operator")

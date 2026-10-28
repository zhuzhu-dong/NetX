"""Desired/observed reconciliation endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from infra.network_reconciler import get_network_reconciler
from infra.ingress_admission_state import AdmissionMaterializationInputIncomplete, IngressAdmissionMaterializer
from fastapi import HTTPException
from infra.production_restart_recovery import get_production_restart_recovery_worker

router = APIRouter(prefix="/api/v1/reconciliation", tags=["reconciliation"])


@router.get("")
@router.get("/status")
async def reconciliation_status():
    return get_network_reconciler().status()


@router.post("/run")
async def run_reconciliation(auto_repair: bool = True):
    return await get_network_reconciler().run_once(auto_repair=auto_repair)


@router.post("/ingress-admission/materialize")
async def materialize_ingress_admission(intent_id: str, revision: int = 1):
    """One authorized materialization of long-lived ingress rules."""
    try:
        return IngressAdmissionMaterializer().materialize(intent_id=intent_id, revision=revision)
    except AdmissionMaterializationInputIncomplete as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "reason": str(exc)}) from exc


@router.get("/restart-recovery/status")
async def restart_recovery_status():
    """Read-only durable recovery state; this endpoint never triggers validation."""
    worker = get_production_restart_recovery_worker()
    return {"environment": worker.repo.latest_fingerprint(), "desired": worker.repo.active_desired()}

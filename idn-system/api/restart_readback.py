"""Operator-facing, read-only southbound restart-recovery readback endpoint."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from infra.production_restart_recovery import get_production_restart_recovery_worker

router = APIRouter(prefix="/api/v1/restart-readback", tags=["restart-readback"])


async def _materialize_current_egress() -> tuple[dict, object]:
    """Resolve and persist without reaching an actuator."""
    from infra.egress_qos_desired import EgressQosDesiredRepository, resolve_egress_qos_desired_for_scope
    from infra.qos_resource_allocator import QoSResourceAllocator
    worker = get_production_restart_recovery_worker()
    desireds = worker.repo.active_desired()
    if len(desireds) != 1:
        raise HTTPException(status_code=409, detail="RECONCILE_REQUIRES_ONE_ACTIVE_INGRESS_DESIRED_STATE")
    resolution = resolve_egress_qos_desired_for_scope(
        desireds[0]["traffic_scope_key"], store=worker.store,
        capacity_purpose="RECONCILE_EXISTING",
    )
    if not resolution.accepted:
        raise HTTPException(status_code=409, detail={"code": resolution.status, "resolution": resolution.model_dump(mode="json")})
    binding = QoSResourceAllocator(worker.store).get_active_for_scope(resolution.traffic_scope_key)
    materialized = EgressQosDesiredRepository(worker.store).materialize_authorized(resolution, binding=binding)
    return materialized, resolution


@router.post("/materialize-authorized-egress")
async def materialize_authorized_egress():
    """Persist a resolved desired contract; this endpoint performs no network write."""
    materialized, resolution = await _materialize_current_egress()
    return {"resolution": resolution.model_dump(mode="json"), "desired_state": materialized, "southbound_write": False}


@router.post("/reconcile-authorized-egress")
async def reconcile_authorized_egress():
    """Resolve current ingress desired state, persist it, then reconcile."""
    from infra.network_reconciler import get_network_reconciler
    materialized, resolution = await _materialize_current_egress()
    summary = await get_network_reconciler().run_once(auto_repair=True)
    return {"resolution": resolution.model_dump(mode="json"), "desired_state": materialized, "reconciliation": summary.model_dump(mode="json")}


@router.post("/run-recovery")
async def run_recovery():
    """Operator-triggered formal recovery: lease, fencing and reconciler only."""
    return await get_production_restart_recovery_worker().run_startup_recovery()


@router.post("/preflight")
async def preflight_readback():
    """Persist an isolated PREFLIGHT_READBACK bundle without reconciling.

    No actuator, ONOS client write, P4Runtime write, performance test,
    EvidenceBatch or Formal Assurance path is reachable from this endpoint.
    """
    worker = get_production_restart_recovery_worker()
    desireds = worker.repo.active_desired()
    if len(desireds) != 1:
        raise HTTPException(status_code=409, detail="PREFLIGHT_REQUIRES_ONE_ACTIVE_INGRESS_DESIRED_STATE")
    instances = await worker._instances()
    runtime_epoch = "PFL-RTE-" + uuid.uuid4().hex[:16]
    worker.repo.record_instances(runtime_epoch, instances)
    environment, _ = worker.repo.put_fingerprint(await worker._fingerprint())
    recovery_id = "PFL-" + uuid.uuid4().hex[:16]
    result = await worker.collect_full_readback_bundle(
        recovery_id=recovery_id, phase="PREFLIGHT_READBACK", desired=desireds[0],
        runtime_instance_epoch=runtime_epoch, environment_epoch=environment["environment_epoch"],
    )
    return {"recovery_id": recovery_id, "runtime_instance_epoch": runtime_epoch,
            "environment_epoch": environment["environment_epoch"], **result,
            "southbound_write": False, "performance_test": False,
            "evidence_batch_created": False, "assurance_decision_created": False}


@router.post("/fencing-preflight")
async def fencing_preflight():
    """Isolated lease/defer/no-change/atomic-completion acceptance proof."""
    return await get_production_restart_recovery_worker().run_fencing_preflight()

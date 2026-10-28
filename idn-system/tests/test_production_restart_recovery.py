from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from api.store import IntentStore
from infra.ingress_admission_state import IngressAdmissionDesiredRepository
from infra.production_restart_recovery import (
    FormalAssuranceTriggerPolicy,
    ProductionRestartRecoveryWorker,
    RestartRecoveryRepository,
)
from infra.network_write_fencing import NetworkWriteFencing


def _desired(store: IntentStore) -> dict:
    record = {
        "generation_id": "IADS-test", "admission_plan_id": "INGRESS-WC-20260805-03",
        "intent_id": "INT-test", "revision": 1, "traffic_scope_key": "TSK-test",
        "desired_policy_id": "POL-desired", "executed_resource_owner_policy_id": "POL-owner",
        "resource_binding_id": "RB-test", "capacity_record_id": "CAP-test",
        "desired": {"guaranteed_floor_mbps": 30}, "created_at": "2026-08-06T00:00:00+00:00",
    }
    IngressAdmissionDesiredRepository(store).append_desired(record)
    return record


def test_schema_migration_is_additive_and_idempotent(tmp_path):
    store = IntentStore(tmp_path / "persistent" / "app.db")
    first = RestartRecoveryRepository(store)
    second = RestartRecoveryRepository(store)
    with store._lock, store._connect() as c:
        count = c.execute("SELECT count(*) FROM persistent_schema_migrations_v1").fetchone()[0]
    assert first.schema_version == second.schema_version
    assert count == 1


def test_runtime_epoch_does_not_change_semantic_fingerprint(tmp_path):
    store = IntentStore(tmp_path / "app.db"); repo = RestartRecoveryRepository(store)
    first, equivalent_first = repo.put_fingerprint({"p4": "a", "cpu": "20"})
    second, equivalent_second = repo.put_fingerprint({"p4": "a", "cpu": "20"})
    changed, equivalent_changed = repo.put_fingerprint({"p4": "changed", "cpu": "20"})
    assert not equivalent_first and equivalent_second
    assert first["environment_epoch"] == second["environment_epoch"]
    assert not equivalent_changed and changed["environment_epoch"] != first["environment_epoch"]
    assert FormalAssuranceTriggerPolicy.decision(semantic_changed=False) == "NO_REVALIDATION_REQUIRED"
    assert FormalAssuranceTriggerPolicy.decision(semantic_changed=True) == "REVALIDATION_REQUIRED"


def test_same_runtime_identity_is_reused_atomically(tmp_path):
    store = IntentStore(tmp_path / "app.db"); repo = RestartRecoveryRepository(store)
    identity = {"instance_id": "RINST-same", "service_name": "mininet", "container_id": "cid", "container_started_at": "t", "details": {}}
    first = repo.record_instances("RTE-one", [identity])
    second = repo.record_instances("RTE-two", [identity])
    assert first[0]["created"] and second[0]["reused"]
    with store._lock, store._connect() as c:
        assert c.execute("SELECT count(1) FROM runtime_instance_records_v1 WHERE instance_id='RINST-same'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_satisfied_test_intent_preserves_assurance_without_assurance_work(tmp_path, monkeypatch):
    store = IntentStore(tmp_path / "app.db"); desired = _desired(store)

    class Reconciler:
        async def run_once(self, *, auto_repair):
            assert auto_repair is True
            return SimpleNamespace(model_dump=lambda **_: {"status": "COMPLETED", "repair_failed": [], "infrastructure": {"ingress_admission": {"status": "VERIFIED_NO_CHANGE"}}, "repaired": []})

    worker = ProductionRestartRecoveryWorker(store, Reconciler())
    worker._instances = lambda: None  # replaced below with an async implementation
    async def instances():
        return [{"instance_id": "backend-new", "service_name": "backend", "container_id": "new", "details": {}}]
    async def fingerprint():
        return {"p4_program_fingerprint": "same", "cpu_fingerprint": "20"}
    monkeypatch.setattr(worker, "_instances", instances)
    monkeypatch.setattr(worker, "_fingerprint", fingerprint)
    async def ready():
        return True, None
    monkeypatch.setattr(worker, "_dependencies_ready", ready)
    monkeypatch.setattr(worker.repo, "record_verification", lambda *args, **kwargs: (True, []))
    repo = RestartRecoveryRepository(store)
    repo.put_fingerprint({"p4_program_fingerprint": "same", "cpu_fingerprint": "20"})
    repo.upsert_intent_state(desired=desired, recovery_id="old", runtime_epoch="old", environment_epoch="ENV-old", equivalent=True, succeeded=True, capacity_stale=False)
    with store._lock, store._connect() as c:
        c.execute("UPDATE intent_recovery_states_v1 SET assurance_state='SATISFIED' WHERE intent_id='INT-test'")
    await worker.run_startup_recovery()
    state = repo.state_for("INT-test")
    assert state["assurance_state"] == "SATISFIED"
    assert state["enforcement_state"] == "ACTIVE"
    with store._lock, store._connect() as c:
        assert c.execute("SELECT count(*) FROM ingress_admission_desired_states_v1").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_overall_budget_stops_before_a_stage_can_overrun(tmp_path):
    store = IntentStore(tmp_path / "app.db")
    worker = ProductionRestartRecoveryWorker(store)
    recovery_id = worker.repo.create_recovery(runtime_epoch="RTE-budget", before="ENV", after="ENV", equivalent=True, desired=_desired(store))
    loop = __import__("asyncio").get_running_loop()

    async def short_stage():
        return "ok"

    assert await worker._run_stage(recovery_id=recovery_id, stage="unit_short", deadline=loop.time() + 1, operation=short_stage()) == "ok"
    with pytest.raises(TimeoutError, match="RECOVERY_OVERALL_DEADLINE_EXCEEDED"):
        await worker._run_stage(recovery_id=recovery_id, stage="unit_expired", deadline=loop.time() - 0.001, operation=short_stage())
    with store._lock, store._connect() as c:
        rows = c.execute("SELECT stage,status FROM restart_recovery_stage_timings_v1 WHERE recovery_id=? ORDER BY rowid", (recovery_id,)).fetchall()
    assert [tuple(row) for row in rows] == [("unit_short", "COMPLETED"), ("unit_expired", "BUDGET_EXHAUSTED")]


@pytest.mark.asyncio
async def test_successful_single_flight_handoff_starts_a_fresh_execution_budget(tmp_path):
    worker = ProductionRestartRecoveryWorker(IntentStore(tmp_path / "handoff-budget.db"))
    prior_handoff_deadline = asyncio.get_running_loop().time() + 0.005
    await asyncio.sleep(0.01)

    execution_deadline = worker._fresh_execution_deadline()

    assert execution_deadline > prior_handoff_deadline
    assert execution_deadline - asyncio.get_running_loop().time() > 100


def _second_no_change(repo: RestartRecoveryRepository, recovery_id: str) -> dict:
    return repo.persist_second_reconcile({
        "recovery_id": recovery_id, "reconcile_attempt": 2,
        "p4_result": "NO_CHANGE", "ingress_result": "NO_CHANGE", "egress_result": "NO_CHANGE",
        "changed_resource_count": 0, "duplicate_resource_count": 0, "write_operation_count": 0,
    })


def test_atomic_completion_rejects_absent_or_incomplete_evidence(tmp_path):
    store = IntentStore(tmp_path / "app.db"); repo = RestartRecoveryRepository(store)
    recovery_id = repo.create_recovery(runtime_epoch="RTE-test", before="ENV", after="ENV", equivalent=True, desired=_desired(store))
    lease = NetworkWriteFencing(store).acquire(owner_type="RESTART_RECOVERY", owner_id="test", recovery_id=recovery_id)
    second = _second_no_change(repo, recovery_id)
    single = repo.single_writer_verification(recovery_id=recovery_id, lease=lease,
        window_started_at="1970-01-01T00:00:00+00:00", window_completed_at="2999-01-01T00:00:00+00:00")
    result = repo.complete_recovery_atomically(recovery_id, second=second, single_writer=single)
    assert result["execution_state"] == "VERIFYING"
    assert result["acceptance_state"] == "REJECTED"
    assert "P4_FINAL_READBACK_MISSING_OR_INVALID" in result["errors"]


def test_atomic_completion_accepts_checksum_protected_no_change_second_reconcile(tmp_path):
    store = IntentStore(tmp_path / "app.db"); repo = RestartRecoveryRepository(store)
    recovery_id = repo.create_recovery(runtime_epoch="RTE-test", before="ENV", after="ENV", equivalent=True, desired=_desired(store))
    repo.persist_readback_bundle(recovery_id=recovery_id, phase="FINAL_READBACK",
        p4={"status": "P4_RECOVERY_READBACK_VERIFIED", "epoch": "RTE-test"},
        ingress={"status": "INGRESS_RECOVERY_READBACK_VERIFIED", "epoch": "RTE-test"},
        egress={"status": "EGRESS_RECOVERY_READBACK_VERIFIED", "epoch": "RTE-test"},
        bundle={"bundle_status": "VERIFIED", "epoch": "RTE-test"})
    lease = NetworkWriteFencing(store).acquire(owner_type="RESTART_RECOVERY", owner_id="test", recovery_id=recovery_id)
    second = _second_no_change(repo, recovery_id)
    single = repo.single_writer_verification(recovery_id=recovery_id, lease=lease,
        window_started_at="1970-01-01T00:00:00+00:00", window_completed_at="2999-01-01T00:00:00+00:00")
    result = repo.complete_recovery_atomically(recovery_id, second=second, single_writer=single)
    assert result["execution_state"] == "COMPLETED"
    assert result["acceptance_state"] == "ACCEPTED"


class _SingleFlightPreflightReconciler:
    """Minimal adapter with the same lock-first contract as NetworkReconciler."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self.calls = []

    async def run_once(self, *, auto_repair, scope_filter, dry_run):
        self.calls.append({
            "auto_repair": auto_repair, "scope_filter": scope_filter, "dry_run": dry_run,
            "locked": self._lock.locked(),
        })
        if self._lock.locked():
            return SimpleNamespace(model_dump=lambda **_: {"status": "SKIPPED_SINGLE_FLIGHT"})
        async with self._lock:
            return SimpleNamespace(model_dump=lambda **_: {
                "status": "COMPLETED", "run_id": "REC-PREFLIGHT", "checked": 1,
                "in_sync": 1, "missing": 0, "drifted": 0, "repaired": [], "repair_failed": [],
            })


@pytest.mark.asyncio
async def test_single_flight_preflight_waits_for_actual_reconciler_lock_then_runs_dry(tmp_path):
    reconciler = _SingleFlightPreflightReconciler()
    worker = ProductionRestartRecoveryWorker(IntentStore(tmp_path / "app.db"), reconciler)
    await reconciler._lock.acquire()
    task = asyncio.create_task(worker.run_single_flight_handoff_preflight(
        scope_filter={"TSK-test"}, overall_deadline_seconds=1.0, trace_id="PFL-test",
    ))
    await asyncio.sleep(0.05)
    reconciler._lock.release()
    result = await task
    assert result["status"] == "COMPLETED"
    assert [item["status"] for item in result["attempts"]] == ["SKIPPED_SINGLE_FLIGHT", "COMPLETED"]
    assert result["reconcile"] == {
        "status": "COMPLETED", "run_id": "REC-PREFLIGHT", "checked": 1,
        "in_sync": 1, "missing": 0, "drifted": 0, "repaired": [], "repair_failed": [],
    }
    assert all(not item["auto_repair"] and item["dry_run"] for item in reconciler.calls)


@pytest.mark.asyncio
async def test_single_flight_preflight_expires_without_running_when_resident_never_releases(tmp_path):
    reconciler = _SingleFlightPreflightReconciler()
    worker = ProductionRestartRecoveryWorker(IntentStore(tmp_path / "app.db"), reconciler)
    await reconciler._lock.acquire()
    try:
        result = await worker.run_single_flight_handoff_preflight(
            scope_filter={"TSK-test"}, overall_deadline_seconds=0.08,
        )
    finally:
        reconciler._lock.release()
    assert result["status"] == "FAILED_TIMEOUT"
    assert result["error"] == "RECOVERY_OVERALL_DEADLINE_EXCEEDED"
    assert all(item["status"] == "SKIPPED_SINGLE_FLIGHT" for item in result["attempts"])
    assert all(item["dry_run"] and not item["auto_repair"] for item in reconciler.calls)


@pytest.mark.asyncio
async def test_single_flight_preflight_allows_only_one_recovery_style_executor(tmp_path):
    reconciler = _SingleFlightPreflightReconciler()
    worker = ProductionRestartRecoveryWorker(IntentStore(tmp_path / "app.db"), reconciler)
    await reconciler._lock.acquire()
    first = asyncio.create_task(worker.run_single_flight_handoff_preflight(
        scope_filter={"TSK-test"}, overall_deadline_seconds=1.0, trace_id="PFL-first",
    ))
    await asyncio.sleep(0.02)
    second = await worker.run_single_flight_handoff_preflight(
        scope_filter={"TSK-test"}, overall_deadline_seconds=1.0, trace_id="PFL-second",
    )
    reconciler._lock.release()
    first_result = await first
    assert first_result["executor"] is True and first_result["status"] == "COMPLETED"
    assert second == {
        "status": "SKIPPED_SINGLE_FLIGHT", "trace_id": "PFL-second", "executor": False,
        "reason": "RECOVERY_WORKER_ALREADY_HAS_SINGLE_FLIGHT_OWNER", "attempts": [],
        "southbound_write": False,
    }

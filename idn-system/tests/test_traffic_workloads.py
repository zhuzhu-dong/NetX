from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from api.store import IntentStore
from models.traffic_workload import (
    RecoveryPolicy,
    TrafficRole,
    TrafficWorkloadCreate,
    TrafficWorkloadPatch,
    WorkloadLifecycle,
    WorkloadPattern,
    WorkloadSelector,
    WorkloadStartMode,
)
from orchestrator.traffic_orchestrator import TrafficGenerator, TrafficOrchestrator


class MemoryGenerator(TrafficGenerator):
    def __init__(self):
        self.runtime: dict[str, dict] = {}
        self.starts: list[str] = []
        self.stops: list[str] = []

    async def start(self, workload):
        await asyncio.sleep(0)
        self.starts.append(workload.workload_id)
        item = {
            **workload.model_dump(mode="json"),
            "lifecycle_state": "RUNNING",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "process_refs": [{"role": "client", "pid": 1000 + len(self.starts), "state": "RUNNING"}],
            "statistics": {"runtime_seconds": 0},
        }
        self.runtime[workload.workload_id] = item
        return {"status": "ok", "workload": item}

    async def stop(self, workload_id):
        self.stops.append(workload_id)
        item = self.runtime.pop(workload_id, {"workload_id": workload_id})
        item.update({"lifecycle_state": "STOPPED", "process_refs": [],
                     "stopped_at": datetime.now(timezone.utc).isoformat()})
        return {"status": "ok", "workload": item}

    async def update(self, workload):
        await self.stop(workload.workload_id)
        return await self.start(workload)

    async def inspect(self, workload_id):
        item = self.runtime.get(workload_id)
        return {"status": "ok", "workload": item} if item else {"status": "not_found"}

    async def list_runtime(self):
        return list(self.runtime.values())


def workload_request(
    workload_id: str, service_id: str = "ai_data", role: TrafficRole = TrafficRole.PRODUCTION,
    start_mode: WorkloadStartMode = WorkloadStartMode.MANUAL,
) -> TrafficWorkloadCreate:
    return TrafficWorkloadCreate(
        workload_id=workload_id,
        service_id=service_id,
        src_endpoint="h_ai",
        dst_endpoint="h_dc",
        selector=WorkloadSelector(
            source_ip="192.168.20.10/32", destination_ip="10.0.0.10/32",
            protocol="TCP", destination_port=5202,
        ),
        traffic_pattern=WorkloadPattern.BULK_TRANSFER,
        traffic_role=role,
        offered_rate_mbps=20,
        start_mode=start_mode,
        recovery_policy=RecoveryPolicy.AUTO_RESTART,
    )


@pytest.fixture
def workload_orchestrator(tmp_path):
    store = IntentStore(tmp_path / "workloads.db")
    generator = MemoryGenerator()
    return TrafficOrchestrator(store=store, generator=generator), store, generator


@pytest.mark.asyncio
async def test_multiple_workloads_run_concurrently_and_stop_independently(workload_orchestrator):
    orchestrator, store, generator = workload_orchestrator
    first = await orchestrator.create(workload_request("WL-A"))
    second = await orchestrator.create(workload_request("WL-B"))

    running_a, running_b = await asyncio.gather(
        orchestrator.start(first.workload_id), orchestrator.start(second.workload_id),
    )
    assert {running_a.lifecycle_state, running_b.lifecycle_state} == {WorkloadLifecycle.RUNNING}
    assert len({running_a.process_refs[0]["pid"], running_b.process_refs[0]["pid"]}) == 2

    stopped = await orchestrator.stop("WL-A")
    assert stopped.lifecycle_state == WorkloadLifecycle.STOPPED
    assert orchestrator.get("WL-B").lifecycle_state == WorkloadLifecycle.RUNNING
    assert "WL-A" not in generator.runtime and "WL-B" in generator.runtime


@pytest.mark.asyncio
async def test_validation_and_production_are_persistently_separated(workload_orchestrator):
    orchestrator, _, _ = workload_orchestrator
    await orchestrator.create(workload_request("WL-PROD", role=TrafficRole.PRODUCTION))
    await orchestrator.create(workload_request("WL-VALID", role=TrafficRole.VALIDATION))

    production = orchestrator.list(traffic_role="PRODUCTION")
    validation = orchestrator.list(traffic_role="VALIDATION")
    assert [item.workload_id for item in production] == ["WL-PROD"]
    assert [item.workload_id for item in validation] == ["WL-VALID"]
    assert validation[0].traffic_role == TrafficRole.VALIDATION


@pytest.mark.asyncio
async def test_runtime_update_restarts_only_target_workload(workload_orchestrator):
    orchestrator, _, generator = workload_orchestrator
    for workload_id in ("WL-A", "WL-B"):
        await orchestrator.create(workload_request(workload_id))
        await orchestrator.start(workload_id)
    b_pid = orchestrator.get("WL-B").process_refs[0]["pid"]

    updated = await orchestrator.patch("WL-A", TrafficWorkloadPatch(offered_rate_mbps=35))
    assert updated.offered_rate_mbps == 35
    assert generator.stops == ["WL-A"]
    assert orchestrator.get("WL-B").process_refs[0]["pid"] == b_pid


@pytest.mark.asyncio
async def test_reconcile_recovers_missing_auto_restart_and_cleans_orphan(workload_orchestrator):
    orchestrator, store, generator = workload_orchestrator
    await orchestrator.create(workload_request("WL-RECOVER"))
    await orchestrator.start("WL-RECOVER")
    generator.runtime.clear()
    generator.runtime["WL-ORPHAN"] = {
        "workload_id": "WL-ORPHAN", "lifecycle_state": "RUNNING", "process_refs": []
    }

    result = await orchestrator.reconcile()
    assert result["repaired"] == ["WL-RECOVER"]
    assert result["orphaned"] == ["WL-ORPHAN"]
    assert orchestrator.get("WL-RECOVER").lifecycle_state == WorkloadLifecycle.RUNNING
    audit_types = {item["event_type"] for item in store.list_audit_events()}
    assert "WORKLOAD_RECOVERED" in audit_types
    assert "ORPHAN_WORKLOAD_CLEANED" in audit_types


@pytest.mark.asyncio
async def test_delete_failed_workload_stops_partial_runtime_before_removal(workload_orchestrator):
    orchestrator, _, generator = workload_orchestrator
    created = await orchestrator.create(workload_request("WL-FAILED"))
    orchestrator._persist(
        created, lifecycle_state="FAILED", desired_state="RUNNING",
        process_refs=[{"role": "server", "pid": 4242, "state": "RUNNING"}],
        last_error="partial start",
    )
    generator.runtime["WL-FAILED"] = {
        "workload_id": "WL-FAILED", "lifecycle_state": "FAILED",
        "process_refs": [{"role": "server", "pid": 4242, "state": "RUNNING"}],
    }

    await orchestrator.delete("WL-FAILED")

    assert orchestrator.get("WL-FAILED") is None
    assert generator.stops == ["WL-FAILED"]
    assert "WL-FAILED" not in generator.runtime


@pytest.mark.asyncio
async def test_reconcile_cannot_advance_version_while_workload_is_stopping(tmp_path):
    class PausingStopGenerator(MemoryGenerator):
        def __init__(self):
            super().__init__()
            self.stop_entered = asyncio.Event()
            self.allow_stop = asyncio.Event()

        async def stop(self, workload_id):
            self.stop_entered.set()
            await self.allow_stop.wait()
            return await super().stop(workload_id)

    generator = PausingStopGenerator()
    orchestrator = TrafficOrchestrator(
        store=IntentStore(tmp_path / "workload-race.db"), generator=generator,
    )
    await orchestrator.create(workload_request("WL-RACE"))
    await orchestrator.start("WL-RACE")

    stopping = asyncio.create_task(orchestrator.stop("WL-RACE"))
    await generator.stop_entered.wait()
    reconciling = asyncio.create_task(orchestrator.reconcile())
    await asyncio.sleep(0)
    generator.allow_stop.set()

    stopped, result = await asyncio.gather(stopping, reconciling)
    assert stopped.lifecycle_state == WorkloadLifecycle.STOPPED
    assert stopped.desired_state == WorkloadLifecycle.STOPPED
    assert result["synchronized"] == []
    assert result["failed"] == []
    assert orchestrator.get("WL-RACE").lifecycle_state == WorkloadLifecycle.STOPPED


def test_scheduled_workload_requires_start_time_and_bursty_requires_shape():
    with pytest.raises(ValueError, match="start_time"):
        workload_request("WL-SCHEDULED", start_mode=WorkloadStartMode.SCHEDULED)
    with pytest.raises(ValueError, match="burst_size"):
        TrafficWorkloadCreate(
            **{
                **workload_request("WL-BURST").model_dump(),
                "traffic_pattern": "BURSTY",
            }
        )


def test_workload_api_persists_manual_instance(app_client):
    payload = workload_request("WL-API").model_dump(mode="json")
    response = app_client.post("/api/v1/workloads", json=payload, headers={"X-API-Key": "demo-key-xxx"})
    assert response.status_code == 201, response.text
    assert response.json()["lifecycle_state"] == "CREATED"
    listed = app_client.get("/api/v1/workloads?traffic_role=PRODUCTION")
    assert listed.status_code == 200
    assert [item["workload_id"] for item in listed.json()] == ["WL-API"]

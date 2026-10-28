from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from api.store import IntentStore
from api.baseline import _canonical_p4_entry, _canonical_p4_expected
from infra.recovery_manager import RecoveryManager
from infra.soak_runner import SoakRunner
from models.traffic_workload import WorkloadLifecycle


class IdentityProbe:
    runtime_id = "mn-1"

    async def get(self, path):
        return {"status": "ok", "runtime_id": self.runtime_id, "started_at": "now"}


class FakeOnos:
    def __init__(self):
        self.flows = [{"state": "ADDED"}]

    async def check_health(self):
        return {"status": "ok"}

    async def get_flows(self):
        return {"flows": self.flows}


class EpochOnos(FakeOnos):
    def __init__(self):
        super().__init__()
        self.epoch = "100"

    async def get(self, path):
        assert path == "/cluster"
        return {"nodes": [{"id": "node-1", "lastUpdate": self.epoch}]}


def test_p4_lpm_readback_normalizes_controller_priority_and_byte_padding():
    """P4Runtime LPM priority 0 must match ONOS's logical priority 10."""
    rule = {
        "tableId": 37066206, "priority": 10,
        "selector": {"criteria": [{"matches": [{
            "field": "hdr.ipv4.dstAddr", "match": "lpm", "value": "0a00000a", "prefixLength": 32,
        }]}]},
        "treatment": {"instructions": [{
            "subtype": "ACTION", "actionId": "MyIngress.set_nhop",
            "actionParams": {"port": "0003", "srcMac": "02aa00000005", "dstMac": "02aa00000003"},
        }]},
    }
    entry = {
        "table_id": 37066206, "priority": 0,
        "matches": {"hdr.ipv4.dstAddr": {"match": "lpm", "value": "0a00000a", "prefix_len": 32}},
        "action_name": "MyIngress.set_nhop",
        "action_params": [
            {"param_id": 1, "value": "03"},
            {"param_id": 2, "value": "02aa00000005"},
            {"param_id": 3, "value": "02aa00000003"},
        ],
    }
    assert _canonical_p4_entry(entry, "MyIngress.ipv4_routing") == _canonical_p4_expected(rule)


class ReconcileDependency:
    def __init__(self, name):
        self.name, self.calls = name, 0

    async def reconcile(self):
        self.calls += 1
        return {"status": "ok", "component": self.name}

    async def run_once(self, auto_repair=True):
        self.calls += 1
        return {"status": "COMPLETED", "auto_repair": auto_repair}


async def unavailable_baseline_observer(**_kwargs):
    return {"status": "UNAVAILABLE", "error": "test observer unavailable"}


@pytest.mark.asyncio
async def test_generation_aware_recovery_preserves_backend_restart_and_repairs_dataplane_restart(tmp_path):
    store, probe, onos = IntentStore(tmp_path / "recovery.db"), IdentityProbe(), FakeOnos()
    baseline_calls = []

    async def baseline():
        baseline_calls.append(probe.runtime_id)
        return {"status": "SUCCESS"}

    workloads, faults, policies = (ReconcileDependency(name) for name in ("workloads", "faults", "policies"))
    manager = RecoveryManager(
        store=store, probe=probe, onos=onos, baseline_installer=baseline,
        baseline_observer=unavailable_baseline_observer,
        workload_orchestrator=workloads, fault_controller=faults, policy_reconciler=policies,
    )
    first = await manager.recover_once(trigger="startup")
    assert first["detection"]["first_observation"] is True
    assert baseline_calls == ["mn-1"]

    backend_restart = await manager.recover_once(trigger="startup")
    assert backend_restart["steps"]["baseline"]["status"] == "PRESERVED"
    assert baseline_calls == ["mn-1"]

    probe.runtime_id = "mn-2"
    dataplane_restart = await manager.recover_once(trigger="periodic")
    assert dataplane_restart["detection"]["mininet_restarted"] is True
    assert baseline_calls == ["mn-1", "mn-2"]
    assert workloads.calls == faults.calls == policies.calls == 3


@pytest.mark.asyncio
async def test_onos_flow_state_loss_triggers_baseline_reinstall(tmp_path):
    store, probe, onos = IntentStore(tmp_path / "onos.db"), IdentityProbe(), FakeOnos()
    calls = []

    async def baseline():
        calls.append(1)
        return {"status": "SUCCESS"}

    dependency = ReconcileDependency("generic")
    manager = RecoveryManager(store=store, probe=probe, onos=onos, baseline_installer=baseline,
                              baseline_observer=unavailable_baseline_observer,
                              workload_orchestrator=dependency, fault_controller=dependency,
                              policy_reconciler=dependency)
    await manager.recover_once(trigger="startup")
    onos.flows = []
    result = await manager.recover_once(trigger="periodic")
    assert result["detection"]["onos_flow_state_lost"] is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_controller_epoch_drift_repairs_nonempty_flow_inventory(tmp_path):
    """A surviving classifier must not mask missing forwarding after ONOS restart."""
    store, probe, onos = IntentStore(tmp_path / "epoch-drift.db"), IdentityProbe(), EpochOnos()
    installs, repairs = [], []
    observer_values = [
        {"status": "VERIFIED", "desired_entry_count": 10, "verified_entry_count": 10,
         "missing_entry_count": 0, "comparisons": []},
        {"status": "DRIFTED", "desired_entry_count": 10, "verified_entry_count": 1,
         "missing_entry_count": 9, "comparisons": [{"status": "MISSING_IN_ONOS_AND_P4"}]},
        {"status": "VERIFIED", "desired_entry_count": 10, "verified_entry_count": 10,
         "missing_entry_count": 0, "comparisons": []},
    ]

    async def observer(**_kwargs):
        return observer_values.pop(0)

    async def installer():
        installs.append(True)
        return {"status": "SUCCESS"}

    async def repairer(observation, **_kwargs):
        repairs.append(observation)
        return {"status": "SUCCESS", "missing_entries": observation["missing_entry_count"]}

    dependency = ReconcileDependency("generic")
    manager = RecoveryManager(
        store=store, probe=probe, onos=onos, baseline_installer=installer,
        baseline_observer=observer, baseline_repairer=repairer,
        workload_orchestrator=dependency, fault_controller=dependency,
        policy_reconciler=dependency,
    )
    await manager.recover_once(trigger="startup")
    onos.epoch = "200"  # Controller restarted; its flow inventory is non-empty.
    result = await manager.recover_once(trigger="periodic")

    assert len(installs) == 0
    assert len(repairs) == 1
    assert result["detection"]["controller_epoch_changed"] is True
    assert result["detection"]["baseline_drift"] is True
    assert result["recovery_execution"]["trigger"] == "CONTROLLER_RESTART_DRIFT"
    assert result["recovery_execution"]["missing_entries"] == 9


@pytest.mark.asyncio
async def test_controller_epoch_with_verified_rules_does_not_repost_baseline(tmp_path):
    store, probe, onos = IntentStore(tmp_path / "epoch-verified.db"), IdentityProbe(), EpochOnos()
    installs = []

    async def observer(**_kwargs):
        return {"status": "VERIFIED", "desired_entry_count": 10, "verified_entry_count": 10,
                "missing_entry_count": 0, "comparisons": []}

    async def installer():
        installs.append(True)
        return {"status": "SUCCESS"}

    dependency = ReconcileDependency("generic")
    manager = RecoveryManager(
        store=store, probe=probe, onos=onos, baseline_installer=installer,
        baseline_observer=observer,
        workload_orchestrator=dependency, fault_controller=dependency,
        policy_reconciler=dependency,
    )
    await manager.recover_once(trigger="startup")
    onos.epoch = "201"
    result = await manager.recover_once(trigger="periodic")

    assert len(installs) == 0
    assert result["detection"]["controller_epoch_changed"] is True
    assert result["steps"]["baseline"] == {
        "status": "VERIFIED", "reason": "DESIRED_OBSERVED_IN_SYNC",
    }


@pytest.mark.asyncio
async def test_empty_onos_inventory_replays_configured_stratum_endpoints(tmp_path):
    class RecoveringOnos(FakeOnos):
        def __init__(self):
            super().__init__()
            self.available = 0
            self.posts = []

        async def check_health(self):
            return {"status": "ok", "device_count": self.available,
                    "available_devices": self.available}

        async def post(self, path, body):
            self.posts.append((path, body))
            self.available = 6
            return {"status": "ok"}

        async def get(self, path):
            return {}

        async def get_hosts(self):
            return {"hosts": []}

    onos = RecoveringOnos()
    dependency = ReconcileDependency("generic")
    manager = RecoveryManager(
        store=IntentStore(tmp_path / "onos-inventory.db"), probe=IdentityProbe(), onos=onos,
        baseline_installer=lambda: asyncio.sleep(0, result={"status": "SUCCESS"}),
        baseline_observer=unavailable_baseline_observer,
        workload_orchestrator=dependency, fault_controller=dependency,
        policy_reconciler=dependency,
    )
    result = await manager.recover_once(trigger="startup")
    assert result["status"] == "DEGRADED"
    assert result["detection"]["onos"]["inventory_recovery"]["status"] == "RESTORED"
    path, payload = next(item for item in onos.posts if item[0] == "/network/configuration/")
    assert path == "/network/configuration/"
    assert len(payload["devices"]) == 6
    assert payload["devices"]["device:bmv2:5"]["basic"]["managementAddress"].endswith(
        "mininet:50055?device_id=1"
    )
    assert payload["ports"]["device:bmv2:5/1"]["interfaces"][0] == {
        "name": "s_access_r-h_research",
        "ips": ["192.168.10.1/24"],
        "mac": "02:aa:00:00:10:01",
    }
    assert len(payload["hosts"]) == 5
    assert len([item for item in onos.posts if item[0] == "/hosts"]) == 5


class FakeWorkloads:
    def __init__(self):
        self.running = {}

    def from_service_profile(self, service_id, **kwargs):
        return SimpleNamespace(service_id=service_id, workload_id=kwargs["workload_id"])

    async def create(self, request):
        value = SimpleNamespace(workload_id=request.workload_id, lifecycle_state=WorkloadLifecycle.CREATED)
        self.running[value.workload_id] = value
        return value

    async def start(self, workload_id):
        value = SimpleNamespace(workload_id=workload_id, lifecycle_state=WorkloadLifecycle.RUNNING)
        self.running[workload_id] = value
        return value

    async def stop(self, workload_id):
        self.running[workload_id] = SimpleNamespace(workload_id=workload_id, lifecycle_state=WorkloadLifecycle.STOPPED)

    async def delete(self, workload_id):
        self.running.pop(workload_id, None)

    def list(self, lifecycle_state=None):
        return [item for item in self.running.values()
                if lifecycle_state is None or item.lifecycle_state.value == lifecycle_state]


@pytest.mark.asyncio
async def test_short_soak_cycles_workloads_and_cleans_every_created_process(tmp_path):
    workloads, reconciler = FakeWorkloads(), ReconcileDependency("policy")
    runner = SoakRunner(store=IntentStore(tmp_path / "soak.db"), workloads=workloads, reconciler=reconciler)
    started = await runner.start(duration_seconds=0.06, service_ids=["svc-a", "svc-b"],
                                 churn_interval_seconds=0.01)
    result = await runner._tasks[started["run_id"]]
    assert result["cycles"] >= 2
    assert result["status"] == "PASSED"
    assert workloads.running == {}
    assert result["resources"]["task_growth"] <= 3


@pytest.mark.asyncio
async def test_soak_retires_its_same_service_instance_before_reusing_selector(tmp_path):
    class ExclusiveServiceWorkloads(FakeWorkloads):
        def __init__(self):
            super().__init__()
            self.service_by_workload = {}
            self.stop_order = []

        async def create(self, request):
            assert request.service_id not in self.service_by_workload.values()
            value = await super().create(request)
            self.service_by_workload[value.workload_id] = request.service_id
            return value

        async def stop(self, workload_id):
            self.stop_order.append(workload_id)
            await super().stop(workload_id)

        async def delete(self, workload_id):
            self.service_by_workload.pop(workload_id, None)
            await super().delete(workload_id)

    workloads = ExclusiveServiceWorkloads()
    runner = SoakRunner(
        store=IntentStore(tmp_path / "soak-exclusive.db"), workloads=workloads,
        reconciler=ReconcileDependency("policy"),
    )
    started = await runner.start(
        duration_seconds=0.07, service_ids=["svc-a", "svc-b"],
        churn_interval_seconds=0.01,
    )
    result = await runner._tasks[started["run_id"]]
    assert result["cycles"] >= 4
    assert result["status"] == "PASSED"
    assert result["errors"] == []
    assert workloads.running == {}
    assert any(workload_id.endswith("-0") for workload_id in workloads.stop_order)


@pytest.mark.asyncio
async def test_soak_reports_external_server_selector_conflict_without_creating_residue(tmp_path):
    class ConflictAwareWorkloads(FakeWorkloads):
        def __init__(self):
            super().__init__()
            self.created = []
            self.running["WL-EXTERNAL"] = SimpleNamespace(
                workload_id="WL-EXTERNAL", lifecycle_state=WorkloadLifecycle.RUNNING,
                dst_endpoint="h_dc",
                selector=SimpleNamespace(destination_port=5004),
            )

        def from_service_profile(self, service_id, **kwargs):
            return SimpleNamespace(
                service_id=service_id, workload_id=kwargs["workload_id"],
                dst_endpoint="h_dc",
                selector=SimpleNamespace(destination_port=5004),
            )

        async def create(self, request):
            self.created.append(request.workload_id)
            return await super().create(request)

    workloads = ConflictAwareWorkloads()
    runner = SoakRunner(
        store=IntentStore(tmp_path / "soak-conflict.db"), workloads=workloads,
        reconciler=ReconcileDependency("policy"),
    )
    started = await runner.start(
        duration_seconds=0.03, service_ids=["shared-port"],
        churn_interval_seconds=0.01,
    )
    result = await runner._tasks[started["run_id"]]
    assert result["status"] == "FAILED"
    assert result["samples"] == []
    assert result["errors"] == []
    assert result["skipped"]
    assert {item["reason"] for item in result["skipped"]} == {
        "WORKLOAD_SERVER_SELECTOR_IN_USE"
    }
    assert workloads.created == []
    assert list(workloads.running) == ["WL-EXTERNAL"]

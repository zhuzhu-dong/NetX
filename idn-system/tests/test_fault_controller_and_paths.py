from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from api.store import IntentStore
from infra.constrained_path_planner import ConstrainedPathPlanner
from infra.fault_controller import FaultController
from infra.qos_resource_allocator import QoSResourceAllocator
from infra.resource_registry import ResourceRegistry
from models.faults import FaultDefinition, FaultLifecycle, FaultType, PathConstraints, PathReversionPolicy
from models.policy_update import traffic_scope_key
from models.yang_policy import ActionRules, ActionType, MatchCriteria, NetworkPolicy, QoSParams, YANGPolicy
from persistence.policy_store import PolicyStore


class FakeProbe:
    def __init__(self):
        self.active = {}
        self.fail_remove = False

    async def post(self, path, payload):
        fault_id = payload["fault_id"]
        if path == "/faults/apply":
            self.active[fault_id] = dict(payload)
            return {"status": "ok", "fault_id": fault_id, "active": True, "observed": {"active": True}}
        if self.fail_remove:
            return {"status": "error", "fault_id": fault_id, "active": True}
        self.active.pop(fault_id, None)
        return {"status": "ok", "fault_id": fault_id, "active": False}

    async def get(self, path, params=None):
        fault_id = (params or {}).get("fault_id")
        if fault_id:
            value = self.active.get(fault_id)
            return {"status": "ok", "fault_id": fault_id, "active": bool(value), **(value or {})}
        return {"status": "ok", "faults": [
            {"fault_id": fault_id, "active": True, **value}
            for fault_id, value in self.active.items()
        ]}


@pytest.mark.asyncio
async def test_fault_apply_inspect_remove_and_audit(tmp_path):
    store, probe, heals = IntentStore(tmp_path / "faults.db"), FakeProbe(), []

    async def healer(event, definition):
        heals.append((event, definition.fault_id))
        return {"status": "ok"}

    controller = FaultController(store=store, registry=ResourceRegistry(), probe=probe, topology_healer=healer)
    definition = FaultDefinition(
        fault_id="fault-link-1", fault_type=FaultType.LINK_DOWN,
        target_resource_id="s_agg_r-s_core1", reversion_policy=PathReversionPolicy.PREEMPTIVE,
    )
    active = await controller.apply(definition)
    assert active.lifecycle_state == FaultLifecycle.ACTIVE
    assert (await controller.inspect(definition.fault_id)).lifecycle_state == FaultLifecycle.ACTIVE
    removed = await controller.remove(definition.fault_id)
    assert removed.lifecycle_state == FaultLifecycle.REMOVED
    assert heals == [("FAULT_APPLIED", "fault-link-1"), ("FAULT_REMOVED", "fault-link-1")]
    events = store.list_audit_events(resource_type="fault", resource_id="fault-link-1")
    assert {item["event_type"] for item in events} >= {"FAULT_APPLIED", "FAULT_REMOVED"}


@pytest.mark.asyncio
async def test_failed_fault_remove_is_residual_not_false_success(tmp_path):
    store, probe = IntentStore(tmp_path / "residual.db"), FakeProbe()
    controller = FaultController(store=store, registry=ResourceRegistry(), probe=probe,
                                 topology_healer=lambda *args: None)
    definition = FaultDefinition(
        fault_id="fault-loss", fault_type=FaultType.LINK_LOSS,
        target_resource_id="s_core1-s_agg_d", parameters={"loss_pct": 5},
    )
    await controller.apply(definition, heal=False)
    probe.fail_remove = True
    with pytest.raises(RuntimeError, match="FAULT_REMOVE_NOT_OBSERVED"):
        await controller.remove(definition.fault_id, heal=False)
    assert controller.get(definition.fault_id).lifecycle_state == FaultLifecycle.RESIDUAL


@pytest.mark.asyncio
async def test_preemptive_heal_reapplies_each_active_qos_policy_and_persists_runtime(
    tmp_path, monkeypatch,
):
    store = IntentStore(tmp_path / "preemptive.db")
    store.create(intent_id="INT-PREEMPT", text="AI data qos", priority="medium", user_id="test")
    policy = YANGPolicy(
        network_policy=NetworkPolicy(
            policy_id="POL-PREEMPT", policy_name="ai-data-preemptive-qos",
            match=MatchCriteria(
                source_ip="192.168.20.10/32", destination_ip="10.0.0.10/32",
                protocol="TCP", destination_port=5202,
            ),
            action=ActionRules(
                action_type=ActionType.SET_PRIORITY,
                qos_params=QoSParams(bandwidth=25, dscp_value=18, burst_size=64),
            ),
        ),
    )
    policy_store = PolicyStore(store)
    scope = traffic_scope_key(policy).key
    policy_store.save(policy, "INT-PREEMPT")
    policy_store.commit_staged(
        scope, intent_id="INT-PREEMPT", runtime={},
    )
    QoSResourceAllocator(store).allocate(
        policy_id="POL-PREEMPT", service_id="ai_data",
        traffic_scope_key=scope, rate_mbps=25, ceil_mbps=100,
        dscp=18, reservation_kind="GUARANTEED", tenant_id="research",
    )
    reapplied: list[str] = []

    async def install_baseline_connectivity():
        return {"status": "ok"}

    async def apply_qos_policy(current, *, operation):
        assert operation == "APPLY"
        assert current.service_id == "ai_data"
        reapplied.append(current.network_policy.policy_id)
        return {
            "status": "ok", "config_applied": True, "migrated": True,
            "resource_binding": {"interface_name": "s_agg_r-eth3"},
        }

    class FakeReconciler:
        async def run_once(self, *, auto_repair):
            assert auto_repair is True
            return SimpleNamespace(model_dump=lambda **_: {"status": "COMPLETED"})

    monkeypatch.setitem(sys.modules, "api.baseline", SimpleNamespace(
        install_baseline_connectivity=install_baseline_connectivity,
    ))
    monkeypatch.setitem(sys.modules, "api.qos", SimpleNamespace(
        apply_qos_policy=apply_qos_policy,
    ))
    monkeypatch.setitem(sys.modules, "infra.network_reconciler", SimpleNamespace(
        get_network_reconciler=lambda: FakeReconciler(),
    ))

    controller = FaultController(store=store, registry=ResourceRegistry(), probe=FakeProbe())
    definition = FaultDefinition(
        fault_id="fault-preemptive", fault_type=FaultType.LINK_DOWN,
        target_resource_id="s_agg_r-s_core1",
        reversion_policy=PathReversionPolicy.PREEMPTIVE,
    )
    await controller._heal("FAULT_REMOVED", definition)

    assert reapplied == ["POL-PREEMPT"]
    restored = policy_store.get_by_scope(scope)
    assert restored is not None
    runtime = json.loads(restored["runtime_json"])
    assert runtime["service_id"] == "ai_data"
    assert runtime["qos_enforcement"]["resource_binding"]["interface_name"] == "s_agg_r-eth3"
    event = store.list_audit_events(
        event_type="FAULT_TOPOLOGY_RECONCILED", resource_id="fault-preemptive",
    )[0]
    assert event["result"]["preemptive_migrations"] == [{
        "policy_id": "POL-PREEMPT", "status": "MIGRATED",
        "resource_binding": {"interface_name": "s_agg_r-eth3"},
    }]


def _link(link_id, a, b, *, cost, capacity, delay=1, state="ACTIVE"):
    return {"id": link_id, "state": state, "cost": cost, "bandwidth_mbps": capacity,
            "delay_ms": delay, "endpoints": [{"node": a}, {"node": b}]}


def test_constrained_path_uses_capacity_and_avoids_failed_resources():
    topology = {"links": [
        _link("a-fast", "a", "fast", cost=1, capacity=100),
        _link("fast-z", "fast", "z", cost=1, capacity=100),
        _link("a-safe", "a", "safe", cost=2, capacity=80),
        _link("safe-z", "safe", "z", cost=2, capacity=80),
    ]}
    ledger = SimpleNamespace(entries=lambda: [
        SimpleNamespace(resource_id="link:a--fast", available_guaranteed_mbps=100),
        SimpleNamespace(resource_id="link:fast--z", available_guaranteed_mbps=100),
        SimpleNamespace(resource_id="link:a--safe", available_guaranteed_mbps=80),
        SimpleNamespace(resource_id="link:safe--z", available_guaranteed_mbps=80),
    ])
    planner = ConstrainedPathPlanner(ledger=ledger)
    assert planner.compute(topology, "a", "z").nodes == ["a", "fast", "z"]
    path = planner.compute(topology, "a", "z", PathConstraints(
        min_bandwidth_mbps=70, avoid_devices={"fast"}, max_latency_ms=5,
    ))
    assert path.nodes == ["a", "safe", "z"]
    with pytest.raises(ValueError, match="NO_FEASIBLE_PATH"):
        planner.compute(topology, "a", "z", PathConstraints(min_bandwidth_mbps=90, avoid_devices={"fast"}))

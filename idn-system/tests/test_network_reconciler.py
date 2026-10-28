from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from api.store import IntentStore
from infra.network_reconciler import NetworkReconciler
from infra.network_reconciler import ReconciliationRetryDeferred
from infra.network_write_fencing import NetworkWriteFencing, resource_write_scope, write_context
from infra.qos_resource_allocator import QoSResourceAllocator
from infra.stable_capacity import StableCapacityRepository
from models.resources import StableDataplaneCapacityRecord
from models.policy_update import traffic_scope_key
from persistence.policy_store import PolicyStore
from models.yang_policy import (
    ActionRules, ActionType, MatchCriteria, NetworkPolicy, QoSParams, YANGPolicy,
)


def qos_policy(bandwidth: int, policy_id: str) -> YANGPolicy:
    return YANGPolicy(
        raw_intent="generic qos",
        network_policy=NetworkPolicy(
            policy_id=policy_id,
            policy_name="generic-qos",
            match=MatchCriteria(
                source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32",
                protocol="UDP", destination_port=5004,
            ),
            action=ActionRules(
                action_type=ActionType.SET_PRIORITY,
                qos_params=QoSParams(bandwidth=bandwidth, dscp_value=46, burst_size=64),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_repair_defers_a_stale_snapshot_before_any_southbound_write(tmp_path, monkeypatch):
    store = IntentStore(tmp_path / "stale-snapshot.db")
    reconciler = NetworkReconciler(store=store)
    observed_row = {"policy_id": "POL-stable", "traffic_scope_key": "scope", "version": 4,
                    "yang_json": "{old}", "runtime_json": "{}"}
    observed_binding = SimpleNamespace(binding_id="RB-stable", rate_mbps=20.0, resource_key="link:a--b")
    current_binding = SimpleNamespace(binding_id="RB-stable", rate_mbps=10.0, resource_key="link:a--b")

    class CurrentPolicies:
        def __init__(self, _store):
            pass
        def resolve_managed_desired_for_scope(self, _scope):
            return {**observed_row, "version": 5, "yang_json": "{new}"}

    monkeypatch.setattr("persistence.policy_store.PolicyStore", CurrentPolicies)
    monkeypatch.setattr(reconciler.allocator, "get_active_for_scope", lambda _scope: current_binding)

    with pytest.raises(ReconciliationRetryDeferred, match="STALE_POLICY_SNAPSHOT"):
        await reconciler._repair_under_context(observed_row, observed_binding, {"tc": {}})


class FakeProbe:
    def __init__(self, drifted: bool = True, binding_active: bool = True):
        self.drifted = drifted
        self.binding_active = binding_active
        self.applies = 0
        self.runtime_scopes: list[str] = []
        self.class_id = "1:100"
        self.rate_mbps = 20
        self.ceil_mbps = 1000

    async def post(self, path, payload):
        if path == "/policy/clear":
            return {"status": "ok"}
        if payload.get("operation") == "APPLY":
            self.applies += 1
            self.drifted = False
            self.binding_active = True
            self.class_id = payload["class_id"]
            self.rate_mbps = payload["bandwidth_mbps"]
            self.ceil_mbps = (
                payload["bandwidth_mbps"] if payload["qos_mode"] == "RATE_LIMIT"
                # This fixture models the declared 100 Mbps test link.  A
                # process deployment maximum is not physical-link evidence.
                else payload.get("stable_capacity", {}).get("schedulable_capacity_mbps", 100)
            )
        if self.drifted:
            return {"status": "mismatch", "config_applied": False, "qos_binding": {}}
        return {
            "status": "ok", "config_applied": True,
            "binding_on_active_path": self.binding_active,
            "qos_mode": payload["qos_mode"],
            "configured_bandwidth_mbps": payload["bandwidth_mbps"],
            "selector": {
                "source_ip": payload["source_ip"], "destination_ip": payload["destination_ip"],
                "protocol": payload["protocol"], "source_port": payload.get("source_port"),
                "destination_port": payload.get("destination_port"),
            },
            "qos_binding": {
                "link_id": "s_access_r-s_agg_r", "device_id": "device:bmv2:5",
                "interface_name": "s_access_r-eth3",
            },
        }

    async def get(self, path, params=None):
        if path == "/policy/qos-readback":
            if self.drifted:
                return {"status": "mismatch", "config_applied": False}
            return {
                "status": "ok", "config_applied": True, "class_id": self.class_id,
                "rate_mbps": self.rate_mbps, "ceil_mbps": self.ceil_mbps,
                "qos_binding": {
                    "device_id": "device:bmv2:5", "interface_name": "s_access_r-eth3",
                    "link_id": "s_access_r-s_agg_r", "active": self.binding_active,
                },
            }
        assert path == "/policy/qos"
        return {"status": "ok", "scopes": self.runtime_scopes}


class RuntimeEpochProbe(FakeProbe):
    def __init__(self, runtime_id: str, drifted: bool = True):
        super().__init__(drifted=drifted)
        self.runtime_id = runtime_id

    async def get(self, path, params=None):
        if path == "/runtime/identity":
            return {
                "status": "ok", "runtime_id": self.runtime_id,
                "started_at": "2026-07-27T00:00:00+00:00",
            }
        return await super().get(path, params=params)


class FakeWorkloads:
    async def reconcile(self):
        return {"status": "ok", "repaired": []}


def create_policy(store: IntentStore):
    store.create(intent_id="INT-REC", text="reconcile", priority="medium", user_id="test")
    policy = qos_policy(20, policy_id="POL-REC")
    PolicyStore(store).save(policy, "INT-REC")
    scope = traffic_scope_key(policy).key
    allocator = QoSResourceAllocator(store)
    binding = allocator.allocate(
        policy_id="POL-REC", service_id="research_video", traffic_scope_key=scope,
        rate_mbps=20, ceil_mbps=100, dscp=46,
    )
    allocator.bind_runtime(binding.binding_id, {
        "qos_mode": "MIN_BANDWIDTH", "configured_bandwidth_mbps": 20,
        "selector": {
            "source_ip": "192.168.10.10/32", "destination_ip": "10.0.0.10/32",
            "protocol": "UDP", "source_port": None, "destination_port": 5004,
        },
        "qos_binding": {"link_id": "s_access_r-s_agg_r", "device_id": "device:bmv2:5",
                        "interface_name": "s_access_r-eth3"},
    })
    return scope


def test_unprotected_egress_payload_preserves_fractional_capacity_evidence(tmp_path):
    """Mininet must receive the raw capacity equation, not the HTB rounding.

    A scope without an ingress-admission generation still uses the formal
    reconciler path.  The actuator first validates the calibrated evidence
    and only then floors it to its whole-Mbit scheduling unit.
    """
    store = IntentStore(tmp_path / "fractional-capacity.db")
    scope = create_policy(store)
    binding = QoSResourceAllocator(store).get_active_for_scope(scope)
    assert binding is not None
    StableCapacityRepository(store).put(StableDataplaneCapacityRecord(
        capacity_record_id="CAP-fractional", topology_id="research_dc",
        environment_epoch="ENV-test", controller_epoch="ONOS-test",
        path_signature="test-path", bottleneck_egress=binding.resource_key,
        packet_profile="udp-live", protocol="UDP", packet_size_bytes=1200,
        observation_mode="LIVE_RECEIVER_IPERF_CALIBRATION",
        physical_capacity_mbps=100, stable_capacity_mbps=56.91853048228013,
        capacity_floor_mbps=56.91853048228013, p05_capacity_mbps=56.9,
        sample_count=5, confidence="CALIBRATED_LOW",
        measured_at="2026-08-08T14:44:16+00:00",
        valid_until="2099-08-08T15:44:16+00:00",
        source_experiment="regression", safety_margin_mbps=5,
    ))

    payload = NetworkReconciler(store=store)._tc_payload(binding)

    assert payload["stable_capacity"]["schedulable_capacity_mbps"] == pytest.approx(51.91853048228013)
    assert "authorized_schedulable_capacity_mbps" not in payload


def test_unprotected_minimum_scope_uses_resolved_physical_ceiling_not_process_limit(tmp_path, monkeypatch):
    store = IntentStore(tmp_path / "physical-ceiling.db")
    scope = create_policy(store)
    binding = QoSResourceAllocator(store).get_active_for_scope(scope)
    assert binding is not None

    class Registry:
        def snapshot(self):
            return type("Snapshot", (), {"topology_name": "test-topology", "links": [
                type("Link", (), {"resource_id": binding.resource_key, "capacity_mbps": 100})()
            ]})()

    monkeypatch.setenv("IDN_QOS_LINK_CEIL_MBPS", "100")
    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    payload = NetworkReconciler(store=store)._tc_payload(binding)
    assert payload["authorized_schedulable_capacity_mbps"] == 100


def test_shared_resource_uses_the_strictest_active_ingress_root(tmp_path, monkeypatch):
    store = IntentStore(tmp_path / "shared-root.db")
    scope = create_policy(store)
    binding = QoSResourceAllocator(store).get_active_for_scope(scope)
    assert binding is not None
    reconciler = NetworkReconciler(store=store)
    monkeypatch.setattr(reconciler.ingress_desired, "list_active", lambda: [
        {"traffic_scope_key": "TSK-sibling"}, {"traffic_scope_key": "TSK-other"},
    ])
    sibling = binding.model_copy(update={"traffic_scope_key": "TSK-sibling"})
    other = binding.model_copy(update={"traffic_scope_key": "TSK-other", "resource_key": "link:other"})
    monkeypatch.setattr(reconciler.allocator, "get_active_for_scope", lambda scope_key: {
        "TSK-sibling": sibling, "TSK-other": other,
    }.get(scope_key))

    class Resolution:
        def __init__(self, accepted, root): self.accepted, self.root_rate_mbps = accepted, root

    monkeypatch.setattr("infra.network_reconciler.resolve_egress_qos_desired_for_scope", lambda scope_key, store: Resolution(True, 49))
    assert reconciler._shared_resource_authorized_root(binding) == 49


def test_shared_scope_recovery_sends_sibling_durable_contract_to_tc(tmp_path, monkeypatch):
    store = IntentStore(tmp_path / "shared-root-payload.db")
    scope = create_policy(store)
    binding = QoSResourceAllocator(store).get_active_for_scope(scope)
    assert binding is not None
    reconciler = NetworkReconciler(store=store)
    sibling = binding.model_copy(update={"traffic_scope_key": "TSK-sibling"})
    monkeypatch.setattr(reconciler.ingress_desired, "list_active", lambda: [
        {"traffic_scope_key": "TSK-sibling"},
    ])
    monkeypatch.setattr(reconciler.allocator, "get_active_for_scope", lambda scope_key: {
        "TSK-sibling": sibling,
    }.get(scope_key))

    class Resolution:
        def __init__(self, accepted, root=None, status="RESOLVED"):
            self.accepted, self.root_rate_mbps, self.status = accepted, root, status

    def resolve(scope_key, store, **_kwargs):
        if scope_key == scope:
            return Resolution(False, status="INGRESS_DESIRED_MISSING")
        return Resolution(True, 49)

    monkeypatch.setattr("infra.network_reconciler.resolve_egress_qos_desired_for_scope", resolve)
    payload = reconciler._tc_payload(binding)
    assert payload["authorized_schedulable_capacity_mbps"] == 49
    assert payload["durable_scheduler_contract_mbps"] == 49


def test_recovery_payload_uses_committed_yang_qos_not_stale_runtime(tmp_path):
    store = IntentStore(tmp_path / "committed-qos-authority.db")
    scope = create_policy(store)
    binding = QoSResourceAllocator(store).get_active_for_scope(scope)
    assert binding is not None
    with store._lock, store._connect() as connection:
        row = dict(connection.execute(
            "SELECT * FROM policies WHERE traffic_scope_key=?", (scope,)
        ).fetchone())
    row["runtime_json"] = json.dumps({
        "configured_bandwidth_mbps": 30,
        "qos_mode": "RATE_LIMIT",
    })

    payload = NetworkReconciler(store=store)._tc_payload(binding, row)

    assert payload["qos_mode"] == "MIN_BANDWIDTH"


def test_preference_only_policy_without_bandwidth_keeps_active_binding_target(tmp_path):
    """A SET_PRIORITY preference has no numeric desired QoS override."""
    store = IntentStore(tmp_path / "preference-without-bandwidth.db")
    scope = create_policy(store)
    binding = QoSResourceAllocator(store).get_active_for_scope(scope)
    assert binding is not None
    with store._lock, store._connect() as connection:
        row = dict(connection.execute(
            "SELECT * FROM policies WHERE traffic_scope_key=?", (scope,)
        ).fetchone())
    document = json.loads(row["yang_json"])
    document["network_policy"]["action"]["qos_params"]["bandwidth"] = None
    row["yang_json"] = json.dumps(document)

    payload = NetworkReconciler(store=store)._tc_payload(binding, row)

    assert payload["qos_mode"] == "MIN_BANDWIDTH"
    assert payload["bandwidth_mbps"] == pytest.approx(binding.rate_mbps)


@pytest.mark.asyncio
async def test_pending_same_scope_update_is_never_reapplied_by_resident_reconciler(tmp_path):
    """A staged revision owns the writer lease until its own readback commits.

    Reapplying the committed 10 Mbps document while its 20 Mbps successor is
    validating makes an otherwise healthy rate-limit probe look degraded.
    """
    store = IntentStore(tmp_path / "pending-update.db")
    scope = create_policy(store)
    store.create(intent_id="INT-UPDATE", text="increase rate", priority="high", user_id="test")
    PolicyStore(store).stage(qos_policy(30, policy_id="POL-UPDATE"), "INT-UPDATE", {
        "execution_mode": "UPDATE_EXISTING_POLICY",
    })
    # The observed-state writer may have marked the old committed side
    # DEGRADED before this guard takes effect.  Pending ownership still makes
    # it ineligible for a resident repair.
    with store._lock, store._connect() as connection:
        connection.execute(
            "UPDATE policies SET status='DEGRADED' WHERE traffic_scope_key=?", (scope,)
        )
    probe = FakeProbe(drifted=True)
    reconciler = NetworkReconciler(store=store, probe=probe, workloads=FakeWorkloads())

    assert reconciler._active_policies() == {}
    result = await reconciler.run_once(auto_repair=True)
    assert result.checked == 0
    assert probe.applies == 0


@pytest.mark.asyncio
async def test_capacity_guard_blocks_tc_but_not_durable_p4_baseline_repair(tmp_path, monkeypatch):
    """A rejected capacity refresh must not strand the probe classifier.

    The P4 tuple is durable desired state and is needed before the next formal
    calibration.  Egress admission still remains blocked: this regression
    proves no TC apply is used as a back-door around that guard.
    """
    store = IntentStore(tmp_path / "p4-baseline-guard.db")
    scope = create_policy(store)
    binding = QoSResourceAllocator(store).get_active_for_scope(scope)
    assert binding is not None
    row = PolicyStore(store).get_by_scope(scope)
    assert row is not None
    row = {**row, "runtime_json": json.dumps({"installed_flows": [{"device_id": "device:bmv2:5", "flow_id": "old"}]})}
    probe = FakeProbe(drifted=True)
    reconciler = NetworkReconciler(store=store, probe=probe, workloads=FakeWorkloads())

    async def restored(_policy, runtime, *, store=None):
        return {"status": "VERIFIED"}, {**runtime, "classifier_schema_rebuilt": True}, {"status": "RESTORED"}

    monkeypatch.setattr("api.qos.ensure_p4_classifier", restored)
    observed = {
        "tc": {"status": "CAPACITY_GUARD_BLOCKED", "config_applied": False},
        "p4": {"status": "MISSING"},
        "capacity_guard_blocked": True,
        "semantic_diffs": {"egress": {"write_required": True}},
    }
    lease = NetworkWriteFencing(store).acquire(
        owner_type="RESIDENT_RECONCILER", owner_id="network-reconciler",
        recovery_id="REC-p4-baseline", resource_scope=resource_write_scope(binding.resource_key),
    )
    try:
        with write_context(owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                           fencing_token=lease["fencing_token"], resource_scope=lease["resource_scope"]):
            result = await reconciler._repair_under_context(row, binding, observed)
    finally:
        NetworkWriteFencing(store).release(lease["lease_id"])

    assert result["p4"]["status"] == "RESTORED"
    assert result["tc_write_executed"] is False
    assert probe.applies == 0


@pytest.mark.asyncio
async def test_capacity_guard_observes_p4_without_unbound_egress_diff(tmp_path, monkeypatch):
    store = IntentStore(tmp_path / "p4-guard-observe.db")
    scope = create_policy(store)
    binding = QoSResourceAllocator(store).get_active_for_scope(scope)
    row = PolicyStore(store).get_by_scope(scope)
    assert binding is not None and row is not None
    row = {**row, "runtime_json": json.dumps({"installed_flows": [{"device_id": "device:bmv2:5", "flow_id": "old"}]})}
    reconciler = NetworkReconciler(store=store, probe=FakeProbe(drifted=False), workloads=FakeWorkloads())
    monkeypatch.setattr(reconciler, "_tc_payload", lambda *_args: {
        "source_ip": "192.168.10.10/32", "destination_ip": "10.0.0.10/32",
        "protocol": "UDP", "destination_port": 5004, "dscp": 46,
        "egress_resolution": {"status": "CALIBRATION_DOES_NOT_SUPPORT_CURRENT_INGRESS_CONTRACT",
                              "traffic_scope_key": scope,
                              "resolution_errors": ["CALIBRATION_DOES_NOT_SUPPORT_CURRENT_INGRESS_CONTRACT"]},
    })

    async def verified(*_args, **_kwargs):
        return {"status": "VERIFIED", "config_applied": True}

    monkeypatch.setattr("api.qos.verify_p4_classifier", verified)
    drift, observed = await reconciler._observe_policy(row, binding)
    assert drift == "DRIFTED"
    assert observed["capacity_guard_blocked"] is True
    assert observed["p4"]["status"] == "VERIFIED"
    assert observed["semantic_diffs"]["egress"] is None


@pytest.mark.asyncio
async def test_verified_p4_under_capacity_guard_does_not_consume_resident_retry_budget(tmp_path, monkeypatch):
    store = IntentStore(tmp_path / "capacity-guard-deferred.db")
    create_policy(store)
    probe = FakeProbe(drifted=True)
    reconciler = NetworkReconciler(store=store, probe=probe, workloads=FakeWorkloads())

    async def guarded(*_args, **_kwargs):
        return "DRIFTED", {
            "capacity_guard_blocked": True,
            "egress_resolution": {"status": "CALIBRATION_DOES_NOT_SUPPORT_CURRENT_INGRESS_CONTRACT"},
            "checks": {"p4_ok": True, "tc_ok": False},
            "p4": {"status": "VERIFIED"}, "tc": {"status": "CAPACITY_GUARD_BLOCKED"},
            "semantic_diffs": {"p4": None, "egress": None},
        }

    monkeypatch.setattr(reconciler, "_observe_policy", guarded)
    result = await reconciler.run_once(auto_repair=True)
    assert result.status == "COMPLETED"
    assert result.repaired == [] and result.repair_failed == []
    assert result.infrastructure["capacity_guarded_scopes"][0]["write_executed"] is False
    assert probe.applies == 0


@pytest.mark.asyncio
async def test_same_resource_calibration_lease_skips_resident_repair_without_southbound_write(tmp_path, monkeypatch):
    store = IntentStore(tmp_path / "calibration-resource-busy.db")
    scope = create_policy(store)
    binding = QoSResourceAllocator(store).get_active_for_scope(scope)
    row = PolicyStore(store).get_by_scope(scope)
    assert binding is not None and row is not None
    probe = FakeProbe(drifted=True)
    lease = NetworkWriteFencing(store).acquire(
        owner_type="CAPACITY_CALIBRATION", owner_id=scope, recovery_id="CAL-test",
        resource_scope=resource_write_scope(binding.resource_key),
    )
    monkeypatch.setenv("IDN_NETWORK_WRITE_FENCING_REQUIRED", "true")
    reconciler = NetworkReconciler(store=store, probe=probe, workloads=FakeWorkloads())
    with pytest.raises(ReconciliationRetryDeferred, match="SKIPPED_RESOURCE_BUSY"):
        await reconciler._repair(row, binding, {"tc": {}, "p4": {}, "semantic_diffs": {}})
    assert probe.applies == 0
    assert NetworkWriteFencing(store).release(lease["lease_id"]) is True


@pytest.mark.asyncio
async def test_resident_promotes_active_binding_to_committed_stable_policy_owner(tmp_path):
    store = IntentStore(tmp_path / "binding-owner.db")
    scope = create_policy(store)
    allocator = QoSResourceAllocator(store)
    binding = allocator.get(scope)
    assert binding is not None
    # An update's draft ID is allowed during staging but must not remain the
    # owner once its scope has a committed stable policy row.
    with store._lock, store._connect() as connection:
        connection.execute("UPDATE qos_resource_bindings SET policy_id='POL-DRAFT' WHERE binding_id=?", (binding.binding_id,))
        connection.execute("UPDATE resource_reservations SET policy_id='POL-DRAFT' WHERE reservation_id=?", (f"RES-{binding.binding_id}",))
    result = await NetworkReconciler(store=store, probe=FakeProbe(drifted=False), workloads=FakeWorkloads()).run_once(auto_repair=False)
    assert result.infrastructure["resource_binding_ownership"]["write_executed"] is True
    promoted = allocator.get(scope)
    assert promoted is not None and promoted.policy_id == "POL-REC"
    with store._lock, store._connect() as connection:
        reservation = connection.execute("SELECT policy_id FROM resource_reservations WHERE reservation_id=?", (f"RES-{binding.binding_id}",)).fetchone()
    assert reservation["policy_id"] == "POL-REC"


def test_committed_update_clears_pending_and_resident_selects_new_current(tmp_path):
    store = IntentStore(tmp_path / "pending-commit.db")
    scope = create_policy(store)
    store.create(intent_id="INT-30", text="increase rate", priority="high", user_id="test")
    policy_store = PolicyStore(store)
    policy_store.stage(qos_policy(30, policy_id="POL-INCOMING"), "INT-30", {"execution_mode": "UPDATE_EXISTING_POLICY"})
    committed = policy_store.commit_staged(scope, intent_id="INT-30", runtime={})
    assert committed is not None
    assert committed["pending_yang_json"] is None
    active = NetworkReconciler(store=store)._active_policies()
    assert set(active) == {scope}
    assert json.loads(active[scope]["yang_json"])["network_policy"]["action"]["qos_params"]["bandwidth"] == 30


def test_rollback_clears_pending_and_resident_selects_old_committed_current(tmp_path):
    store = IntentStore(tmp_path / "pending-rollback.db")
    scope = create_policy(store)
    policy_store = PolicyStore(store)
    policy_store.stage(qos_policy(30, policy_id="POL-INCOMING"), "INT-30", {"execution_mode": "UPDATE_EXISTING_POLICY"})
    restored = policy_store.rollback_staged(scope, error="probe failed")
    assert restored is not None and restored["pending_yang_json"] is None
    active = NetworkReconciler(store=store)._active_policies()
    assert set(active) == {scope}
    assert json.loads(active[scope]["yang_json"])["network_policy"]["action"]["qos_params"]["bandwidth"] == 20


def test_resident_selects_policy_owned_by_unique_active_binding_over_active_history(tmp_path):
    """Historical ACTIVE rows must never override the bound current policy."""
    store = IntentStore(tmp_path / "bound-current-over-history.db")
    scope = create_policy(store)
    store.create(intent_id="INT-HISTORY", text="old policy", priority="low", user_id="test")
    # PolicyStore.save intentionally updates in place by traffic scope.  The
    # production defect requires a separate stale database row, so clone the
    # complete persisted shape and insert a historical policy without a live
    # ResourceBinding rather than mutating POL-REC.
    with store._lock, store._connect() as connection:
        current = dict(connection.execute(
            "SELECT * FROM policies WHERE policy_id='POL-REC'"
        ).fetchone())
        historical = dict(current)
        historical.update({
            "policy_id": "POL-HISTORICAL",
            "intent_id": "INT-HISTORY",
            "yang_json": qos_policy(8, policy_id="POL-HISTORICAL").model_dump_json(),
            "version": max(0, int(current.get("version") or 1) - 1),
            "status": "ACTIVE",
        })
        columns = list(historical)
        connection.execute(
            f"INSERT INTO policies ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(historical[column] for column in columns),
        )

    active = NetworkReconciler(store=store)._active_policies()
    assert active[scope]["policy_id"] == "POL-REC"
    assert json.loads(active[scope]["yang_json"])["network_policy"]["action"]["qos_params"]["bandwidth"] == 20


def test_managed_desired_resolver_is_shared_and_flags_binding_without_owner(tmp_path):
    """A binding cannot manufacture desired state when its policy is gone."""
    store = IntentStore(tmp_path / "managed-desired-owner.db")
    scope = create_policy(store)
    resolver = PolicyStore(store)
    assert resolver.resolve_managed_desired_for_scope(scope)["policy_id"] == "POL-REC"
    assert NetworkReconciler(store=store)._active_policies()[scope]["policy_id"] == "POL-REC"

    with store._lock, store._connect() as connection:
        connection.execute("DELETE FROM policies WHERE traffic_scope_key=?", (scope,))

    ownership = QoSResourceAllocator(store).reconcile_binding_ownership({})
    assert ownership["status"] == "OWNER_MISSING"
    assert ownership["write_executed"] is False
    assert ownership["owner_missing"] == [{
        "binding_id": QoSResourceAllocator(store).get_active_for_scope(scope).binding_id,
        "policy_id": "POL-REC",
        "traffic_scope_key": scope,
    }]


def test_scoped_owner_reconciliation_does_not_flag_other_scopes(tmp_path):
    store = IntentStore(tmp_path / "scoped-owner-reconciliation.db")
    scope = create_policy(store)
    other_scope = "TSK2-OTHER"
    QoSResourceAllocator(store).allocate(
        policy_id="POL-OTHER", service_id="other", traffic_scope_key=other_scope,
        rate_mbps=5, ceil_mbps=5, dscp=8,
    )
    ownership = QoSResourceAllocator(store).reconcile_binding_ownership(
        PolicyStore(store).resolve_managed_desired_policies(), scopes={scope},
    )
    assert ownership == {
        "status": "RECONCILED", "promoted": [], "owner_missing": [], "write_executed": False,
    }


@pytest.mark.asyncio
async def test_stale_pending_is_rolled_back_then_resident_manages_committed_current(tmp_path, monkeypatch):
    store = IntentStore(tmp_path / "pending-timeout.db")
    scope = create_policy(store)
    policy_store = PolicyStore(store)
    policy_store.stage(qos_policy(30, policy_id="POL-INCOMING"), "INT-30", {"execution_mode": "UPDATE_EXISTING_POLICY"})
    with store._lock, store._connect() as connection:
        # A prior readback may mark a crashed staged update DEGRADED before
        # the stale-pending sweep runs.  It must still be recoverable rather
        # than excluded from every future resident pass.
        connection.execute("UPDATE policies SET status='DEGRADED', updated_at='2000-01-01T00:00:00+00:00' WHERE traffic_scope_key=?", (scope,))
    monkeypatch.setenv("IDN_POLICY_PENDING_TIMEOUT_SECONDS", "1")
    result = await NetworkReconciler(store=store, probe=FakeProbe(drifted=False), workloads=FakeWorkloads()).run_once(auto_repair=False)
    assert result.infrastructure["stale_pending_recovery"]["status"] == "RECOVERED"
    active = NetworkReconciler(store=store)._active_policies()
    assert set(active) == {scope}
    assert json.loads(active[scope]["yang_json"])["network_policy"]["action"]["qos_params"]["bandwidth"] == 20


@pytest.mark.asyncio
async def test_tc_drift_is_detected_repaired_and_audited(tmp_path):
    store = IntentStore(tmp_path / "reconcile.db")
    scope = create_policy(store)
    probe = FakeProbe(drifted=True)
    reconciler = NetworkReconciler(store=store, probe=probe, workloads=FakeWorkloads())

    first = await reconciler.run_once(auto_repair=True)
    assert first.drifted == 1
    assert first.repaired == ["POL-REC"]
    assert probe.applies == 1
    assert store.list_audit_events(event_type="RECONCILIATION_REPAIR")[0]["resource_id"] == "POL-REC"

    second = await reconciler.run_once(auto_repair=True)
    assert second.in_sync == 1
    status = reconciler.status()
    assert status["observed"][0]["drift_type"] == "IN_SYNC"
    assert status["desired"][0]["traffic_scope_key"] == scope


@pytest.mark.asyncio
async def test_reconciler_retires_derived_reservation_without_live_binding(tmp_path):
    store = IntentStore(tmp_path / "reconcile-reservation-integrity.db")
    scope = create_policy(store)
    allocator = QoSResourceAllocator(store)
    binding = allocator.get(scope)
    assert binding is not None
    assert allocator.release(scope) is True
    with store._lock, store._connect() as connection:
        connection.execute(
            "UPDATE resource_reservations SET active=1 WHERE reservation_id=?",
            (f"RES-{binding.binding_id}",),
        )
    reconciler = NetworkReconciler(store=store, probe=FakeProbe(drifted=False), workloads=FakeWorkloads())

    result = await reconciler.run_once(auto_repair=False)

    integrity = result.infrastructure["resource_reservation_integrity"]
    assert integrity["released_reservation_ids"] == [f"RES-{binding.binding_id}"]
    assert store.list_audit_events(event_type="RESOURCE_RESERVATION_INTEGRITY_RECONCILED")


@pytest.mark.asyncio
async def test_inactive_recorded_path_is_drift_even_when_tc_resource_survives(tmp_path):
    store = IntentStore(tmp_path / "inactive-path.db")
    create_policy(store)
    probe = FakeProbe(drifted=False, binding_active=False)
    reconciler = NetworkReconciler(store=store, probe=probe, workloads=FakeWorkloads())

    first = await reconciler.run_once(auto_repair=True)

    assert first.drifted == 1
    assert first.repaired == ["POL-REC"]
    assert probe.applies == 1
    second = await reconciler.run_once(auto_repair=False)
    assert second.in_sync == 1


@pytest.mark.asyncio
async def test_runtime_extra_scope_is_cleaned_without_touching_desired(tmp_path):
    store = IntentStore(tmp_path / "extra.db")
    create_policy(store)
    probe = FakeProbe(drifted=False)
    probe.runtime_scopes = ["runtime-orphan"]
    reconciler = NetworkReconciler(store=store, probe=probe, workloads=FakeWorkloads())

    result = await reconciler.run_once(auto_repair=False)
    assert result.extra == 1
    events = store.list_audit_events(event_type="RECONCILIATION_CLEANUP")
    assert events[0]["resource_id"] == "runtime-orphan"


@pytest.mark.asyncio
async def test_real_p4_verification_contract_is_treated_as_in_sync(tmp_path, monkeypatch):
    store = IntentStore(tmp_path / "p4-contract.db")
    create_policy(store)
    with store._lock, store._connect() as connection:
        connection.execute(
            "UPDATE policies SET runtime_json=? WHERE policy_id='POL-REC'",
            (json.dumps({"installed_flows": [{"device_id": "device:bmv2:5", "flow_id": "1"}]}),),
        )

    async def verified_classifier(_policy, _runtime):
        return {
            "status": "ok",
            "verification_state": "P4_CLASSIFIER_VERIFIED",
            "config_applied": True,
            "flow_id": "1",
        }

    monkeypatch.setattr("api.qos.verify_p4_classifier", verified_classifier)
    monkeypatch.setenv("IDN_QOS_LINK_CEIL_MBPS", "100")
    probe = FakeProbe(drifted=False)
    probe.ceil_mbps = 100
    # This test isolates direct P4 verification; the fixture explicitly
    # reflects the resolved 100 Mbps physical binding rather than a process
    # upper limit that may be larger than the link.
    reconciler = NetworkReconciler(store=store, probe=probe, workloads=FakeWorkloads())
    result = await reconciler.run_once(auto_repair=True)
    assert result.in_sync == 1
    assert result.repaired == []


@pytest.mark.asyncio
async def test_missing_binding_is_reallocated_and_runtime_is_rebuilt(tmp_path):
    store = IntentStore(tmp_path / "missing-binding.db")
    scope = create_policy(store)
    allocator = QoSResourceAllocator(store)
    original = allocator.get(scope)
    assert original is not None
    assert allocator.release(scope)
    # Exhausted attempts from the previous runtime must not permanently block
    # recovery after a new binding is allocated.
    with store._lock, store._connect() as connection:
        connection.execute(
            """INSERT INTO observed_policy_states(
                policy_id, traffic_scope_key, observed_state, drift_type,
                observed_json, checked_at, last_repair_at, repair_attempts
            ) VALUES ('POL-REC', ?, 'DEGRADED', 'MISSING', '{}',
                      '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 3)""",
            (scope,),
        )

    probe = FakeProbe(drifted=True)
    reconciler = NetworkReconciler(store=store, probe=probe, workloads=FakeWorkloads())
    first = await reconciler.run_once(auto_repair=True)

    assert first.missing == 1
    assert first.repaired == ["POL-REC"]
    assert probe.applies == 1
    recovered = allocator.get(scope)
    assert recovered is not None
    assert recovered.binding_id != original.binding_id
    assert recovered.resource_key != "UNBOUND"
    with store._lock, store._connect() as connection:
        reservation = connection.execute(
            "SELECT * FROM resource_reservations WHERE active=1 AND policy_id='POL-REC'"
        ).fetchone()
    assert reservation is not None
    assert reservation["resource_id"] == recovered.resource_key

    second = await reconciler.run_once(auto_repair=True)
    assert second.in_sync == 1
    assert reconciler.status()["observed"][0]["observed_state"] == "ACTIVE"


@pytest.mark.asyncio
async def test_p4_schema_migration_resets_exhausted_retry_window(tmp_path, monkeypatch):
    store = IntentStore(tmp_path / "schema-retry.db")
    scope = create_policy(store)
    runtime = {
        "installed_flows": [{"device_id": "device:bmv2:5", "flow_id": "old"}],
        "delivered_rules": {"device:bmv2:5": [{"priority": 100}]},
    }
    with store._lock, store._connect() as connection:
        connection.execute(
            "UPDATE policies SET runtime_json=? WHERE policy_id='POL-REC'",
            (json.dumps(runtime),),
        )
        connection.execute(
            """INSERT INTO observed_policy_states(
                policy_id, traffic_scope_key, observed_state, drift_type,
                observed_json, checked_at, last_repair_at, repair_attempts
            ) VALUES ('POL-REC', ?, 'DEGRADED', 'MISSING', '{}',
                      '2026-01-01T00:00:00+00:00', ?, 3)""",
            (scope, datetime.now(timezone.utc).isoformat()),
        )

    async def restored(_policy, current_runtime, *, store=None):
        assert store is reconciler.store
        updated = {**current_runtime, "classifier_schema_rebuilt": True}
        return ({"status": "ok"}, updated, {"status": "RESTORED"})

    monkeypatch.setattr("api.qos.ensure_p4_classifier", restored)
    reconciler = NetworkReconciler(
        store=store, probe=FakeProbe(drifted=False), workloads=FakeWorkloads()
    )
    binding = QoSResourceAllocator(store).get(scope)
    result = await reconciler._repair(
        {**PolicyStore(store).get_by_scope(scope), "runtime_json": json.dumps(runtime)},
        binding, {"p4": {"status": "MISSING"}},
    )
    assert result["p4"]["status"] == "RESTORED"
    with store._lock, store._connect() as connection:
        attempts = connection.execute(
            "SELECT repair_attempts FROM observed_policy_states WHERE policy_id='POL-REC'"
        ).fetchone()[0]
    assert attempts == 0


@pytest.mark.asyncio
async def test_new_mininet_runtime_resets_exhausted_policy_repair_windows(tmp_path):
    store = IntentStore(tmp_path / "runtime-epoch.db")
    scope = create_policy(store)
    with store._lock, store._connect() as connection:
        connection.execute(
            """INSERT INTO observed_policy_states(
                policy_id, traffic_scope_key, observed_state, drift_type,
                observed_json, checked_at, last_repair_at, repair_attempts
            ) VALUES ('POL-REC', ?, 'DEGRADED', 'DRIFTED', '{}', ?, ?, 3)""",
            (scope, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        )
    probe = RuntimeEpochProbe("mininet-new-epoch", drifted=True)
    reconciler = NetworkReconciler(store=store, probe=probe, workloads=FakeWorkloads())

    first = await reconciler.run_once(auto_repair=True)

    assert first.repaired == ["POL-REC"]
    assert first.infrastructure["mininet_runtime"]["status"] == "changed"
    assert first.infrastructure["mininet_runtime"]["retry_windows_reset"] == 1
    assert probe.applies == 1
    second = await reconciler.run_once(auto_repair=True)
    assert second.in_sync == 1
    assert second.infrastructure["mininet_runtime"]["status"] == "unchanged"


@pytest.mark.asyncio
async def test_retry_deferred_cycle_does_not_refresh_cooldown_or_attempt_count(tmp_path):
    store = IntentStore(tmp_path / "retry-deferred.db")
    scope = create_policy(store)
    last_attempt = datetime.now(timezone.utc).isoformat()
    with store._lock, store._connect() as connection:
        connection.execute(
            """INSERT INTO observed_policy_states(
                policy_id, traffic_scope_key, observed_state, drift_type,
                observed_json, checked_at, last_repair_at, repair_attempts
            ) VALUES ('POL-REC', ?, 'DEGRADED', 'DRIFTED', '{}', ?, ?, 3)""",
            (scope, last_attempt, last_attempt),
        )
    reconciler = NetworkReconciler(
        store=store, probe=FakeProbe(drifted=True), workloads=FakeWorkloads(),
    )

    result = await reconciler.run_once(auto_repair=True)

    assert result.repair_failed == [{
        "policy_id": "POL-REC", "error": "RECONCILIATION_RETRY_LIMIT",
    }]
    with store._lock, store._connect() as connection:
        state = connection.execute(
            "SELECT repair_attempts, last_repair_at FROM observed_policy_states "
            "WHERE policy_id='POL-REC'"
        ).fetchone()
    assert state["repair_attempts"] == 3
    assert state["last_repair_at"] == last_attempt


@pytest.mark.asyncio
async def test_single_flight_skips_overlapping_cycle(tmp_path):
    store = IntentStore(tmp_path / "single.db")
    reconciler = NetworkReconciler(store=store, probe=FakeProbe(False), workloads=FakeWorkloads())
    await reconciler._lock.acquire()
    try:
        result = await reconciler.run_once()
    finally:
        reconciler._lock.release()
    assert result.status == "SKIPPED_SINGLE_FLIGHT"


@pytest.mark.asyncio
async def test_reconciler_marks_observation_timeout_as_partial_failure(tmp_path, monkeypatch):
    """A cancelled P4 observation cannot be reported as a clean completed pass."""
    store = IntentStore(tmp_path / "unknown-observation.db")
    create_policy(store)
    reconciler = NetworkReconciler(store=store, probe=FakeProbe(drifted=False), workloads=FakeWorkloads())

    async def timed_out_observation(*_args, **_kwargs):
        raise TimeoutError("P4_READBACK_DEADLINE")

    monkeypatch.setattr(reconciler, "_observe_policy", timed_out_observation)
    result = await reconciler.run_once(auto_repair=True)

    assert result.status == "PARTIAL"
    assert result.drifted == 1
    assert result.repair_failed == [{
        "policy_id": "POL-REC", "error": "OBSERVATION_FAILED:P4_READBACK_DEADLINE",
    }]


@pytest.mark.asyncio
async def test_validation_ingress_allocation_acquires_its_own_fenced_lease(tmp_path, monkeypatch):
    """Assurance writes occur after the workflow lease and must still be fenced."""
    import infra.network_reconciler as reconciler_module

    store = IntentStore(tmp_path / "ingress-allocation.db")
    calls = {"acquire": 0, "release": 0, "writes": 0}

    class FakeFencing:
        def __init__(self, _store):
            pass

        def acquire(self, **_kwargs):
            calls["acquire"] += 1
            return {
                "lease_id": "lease-1", "owner_type": "VALIDATION_INGRESS_ALLOCATION",
                "owner_id": "plan-1", "fencing_token": 7,
                "resource_scope": "ingress_admission:plan-1",
            }

        def release(self, _lease_id):
            calls["release"] += 1

        def record_execution(self, _authorization, *, write_executed):
            calls["writes"] += int(write_executed)

    class FakeDesired:
        def active_for_scope(self, scope):
            assert scope == "scope-1"
            return {
                "admission_plan_id": "plan-1", "generation_id": "gen-1",
                "traffic_scope_key": "scope-1", "capacity_record_id": "capacity-1",
                "desired": {
                    "protected_selector": {"scope_id": "scope-1"},
                    "guaranteed_floor_mbps": 30,
                    "schedulable_capacity_mbps": 49,
                    "eligible_best_effort_scopes": [
                        {"scope_id": "be-1", "weight": 1},
                    ],
                },
            }

        def active(self, _plan_id):
            return None

        def append_allocation(self, _generation, allocation, _status):
            return {"allocation_id": "allocation-1", "allocation": allocation}

    class FakeIngressProbe:
        async def post(self, path, payload):
            assert path == "/policy/ingress-admission"
            assert payload["mode"] == "OVERSUBSCRIBED_ADMISSION"
            return {"status": "VERIFIED"}

    monkeypatch.setenv("IDN_NETWORK_WRITE_FENCING_REQUIRED", "true")
    monkeypatch.setattr(reconciler_module, "NetworkWriteFencing", FakeFencing)
    monkeypatch.setattr(reconciler_module, "require_network_write", lambda **_kwargs: {"authorized": True})
    reconciler = NetworkReconciler(store=store, probe=FakeIngressProbe(), workloads=FakeWorkloads())
    reconciler.ingress_desired = FakeDesired()

    result = await reconciler.activate_ingress_runtime_allocation(
        target_demand_mbps=40, active_best_effort_scope_ids={"be-1"},
        traffic_scope_key="scope-1",
    )

    assert result["readback"]["status"] == "VERIFIED"
    assert calls == {"acquire": 1, "release": 1, "writes": 1}

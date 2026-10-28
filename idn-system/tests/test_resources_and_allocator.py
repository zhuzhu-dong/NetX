from __future__ import annotations

from api.store import IntentStore
from api.capacity_evidence import authoritative_admission_decision
from infra.qos_resource_allocator import QoSResourceAllocator
from infra.p4_counter_reader import _grpc_port, _p4runtime_node_id
from infra.resource_ledger import ResourceLedger
from infra.resource_registry import ResourceRegistry
from infra.ingress_admission_state import IngressAdmissionDesiredRepository
from infra.stable_capacity import StableCapacityRepository, conservative_capacity_estimate
from models.resources import StableDataplaneCapacityRecord
from models.resources import AdmissionDecision
from models.yang_policy import YANGPolicy
from models.policy_update import traffic_scope_key
from persistence.policy_store import PolicyStore


def test_resource_registry_maps_all_runtime_identifiers():
    registry = ResourceRegistry()
    snapshot = registry.snapshot()
    assert len(snapshot.devices) == 6
    assert len(snapshot.endpoints) == 5
    assert len(snapshot.links) == 13
    assert registry.resolve("s_access_r") == "device:bmv2:5"
    assert registry.resolve("mininet:50055") == "device:bmv2:5"
    assert registry.resolve("node_id:5") == "device:bmv2:5"
    assert next(device for device in snapshot.devices if device.node_id == 5).p4runtime_node_id == 1
    assert registry.resolve("192.168.20.10") == "endpoint:h_ai"
    ai = next(endpoint for endpoint in snapshot.endpoints if endpoint.host_name == "h_ai")
    assert (ai.subnet_name, ai.ip_prefix_length) == ("ai", 24)
    assert (ai.gateway_ip, ai.gateway_mac) == ("192.168.20.1", "02:aa:00:00:20:01")
    interface = registry.interface_for("device:bmv2:5", 3)
    assert interface.linux_interface == "s_access_r-eth3"
    assert interface.capacity_mbps == 100


def test_p4runtime_endpoint_and_local_node_ids_are_distinct():
    assert _grpc_port("device:bmv2:5") == 50055
    assert _p4runtime_node_id() == 1


def test_allocator_assigns_collision_free_resources_and_reuses_by_scope(tmp_path):
    store = IntentStore(tmp_path / "allocator.db")
    allocator = QoSResourceAllocator(store)
    first = allocator.allocate(
        policy_id="POL-A", service_id="svc-a", traffic_scope_key="scope-a",
        rate_mbps=20, ceil_mbps=100,
    )
    second = allocator.allocate(
        policy_id="POL-B", service_id="svc-b", traffic_scope_key="scope-b",
        rate_mbps=30, ceil_mbps=100,
    )
    assert first.class_id != second.class_id
    assert first.filter_id != second.filter_id
    updated = allocator.allocate(
        policy_id="POL-A2", service_id="svc-a", traffic_scope_key="scope-a",
        rate_mbps=25, ceil_mbps=100,
    )
    assert updated.binding_id == first.binding_id
    assert updated.class_id == first.class_id
    assert updated.rate_mbps == 25


def test_committed_minimum_revision_advances_existing_ingress_projection(tmp_path):
    """A policy revision must not leave recovery replaying the old ingress floor."""
    store = IntentStore(tmp_path / "ingress-policy-revision.db")
    policy = YANGPolicy.model_validate({"network_policy": {
        "policy_id": "POL-stable", "policy_name": "generic", "match": {
            "source_ip": "192.0.2.10/32", "destination_ip": "198.51.100.10/32",
            "protocol": "UDP", "destination_port": 5100,
        }, "action": {"action_type": "SET_PRIORITY", "qos_params": {
            "qos_mode": "MIN_BANDWIDTH", "bandwidth": 20, "dscp_value": 46,
        }},
    }, "service_id": "generic"})
    scope = traffic_scope_key(policy.network_policy).key
    binding = QoSResourceAllocator(store).allocate(
        policy_id="POL-stable", traffic_scope_key=scope, rate_mbps=20, ceil_mbps=49,
        service_id="generic", dscp=46,
    )
    store.create("INT-new", "update generic service", priority="high")
    policies = PolicyStore(store)
    policies.stage(policy, "INT-new")
    policies.commit_staged(scope, intent_id="INT-new")
    desireds = IngressAdmissionDesiredRepository(store)
    desireds.append_desired({
        "generation_id": "IADS-old", "admission_plan_id": "plan-revision",
        "intent_id": "INT-old", "revision": 1, "traffic_scope_key": scope,
        "desired_policy_id": "POL-stable", "executed_resource_owner_policy_id": "POL-stable",
        "resource_binding_id": binding.binding_id, "capacity_record_id": "CAP-old",
        "desired": {"qos_mode": "MIN_BANDWIDTH", "guaranteed_floor_mbps": 30,
                    "schedulable_capacity_mbps": 49,
                    "protected_selector": {"source_ip": "192.0.2.10", "destination_ip": "198.51.100.10", "protocol": "udp", "destination_port": 5100, "dscp": 46}},
        "created_at": "2026-08-09T00:00:00+00:00",
    })
    # Historical generations are intentionally unlimited; only ACTIVE is
    # unique.  This reproduces the second policy update that formerly failed
    # while trying to mark its predecessor SUPERSEDED.
    with store._lock, store._connect() as connection:
        connection.execute(
            """INSERT INTO ingress_admission_desired_states_v1(
                generation_id, admission_plan_id, intent_id, revision, traffic_scope_key,
                desired_policy_id, executed_resource_owner_policy_id, resource_binding_id,
                capacity_record_id, desired_json, status, created_at
            ) VALUES ('IADS-historical','plan-revision','INT-historical',0,?,
                      'POL-old','POL-old',?,'CAP-old','{}','SUPERSEDED','2026-08-08T00:00:00+00:00')""",
            (scope, binding.binding_id),
        )

    result = desireds.synchronize_committed_minimum_policy("plan-revision")

    assert result["status"] == "SYNCHRONIZED"
    assert result["record"]["desired"]["guaranteed_floor_mbps"] == 20
    assert result["record"]["intent_id"] == "INT-new"
    assert QoSResourceAllocator(store).get_active_for_scope(scope).rate_mbps == 20
    assert desireds.active("plan-revision")["generation_id"] == result["record"]["generation_id"]
    with store._lock, store._connect() as connection:
        assert connection.execute("SELECT status FROM ingress_admission_desired_states_v1 WHERE generation_id='IADS-old'").fetchone()[0] == "SUPERSEDED"
        assert connection.execute("SELECT count(*) FROM ingress_admission_desired_states_v1 WHERE admission_plan_id='plan-revision' AND status='SUPERSEDED'").fetchone()[0] == 2




def test_resource_ledger_separates_reservation_from_measured_load(tmp_path):
    store = IntentStore(tmp_path / "ledger.db")
    allocator = QoSResourceAllocator(store)
    a = allocator.allocate(
        policy_id="POL-A", traffic_scope_key="scope-a", rate_mbps=20,
        ceil_mbps=100, tenant_id="tenant-a", reservation_kind="GUARANTEED",
    )
    b = allocator.allocate(
        policy_id="POL-B", traffic_scope_key="scope-b", rate_mbps=30,
        ceil_mbps=100, tenant_id="tenant-b", reservation_kind="GUARANTEED",
    )
    c = allocator.allocate(
        policy_id="POL-C", traffic_scope_key="scope-c", rate_mbps=10,
        ceil_mbps=10, tenant_id="tenant-c", reservation_kind="RATE_LIMIT",
    )
    runtime = {"qos_binding": {"link_id": "s_access_r-s_agg_r", "device_id": "device:bmv2:5",
                                "interface_name": "s_access_r-eth3"}}
    for binding in (a, b, c):
        allocator.bind_runtime(binding.binding_id, runtime)

    link_id = ResourceRegistry().resolve("s_access_r-s_agg_r")
    entry = next(item for item in ResourceLedger(store).entries({link_id: 5}) if item.resource_id == link_id)
    assert entry.guaranteed_reservation_mbps == 50
    assert entry.rate_limit_commitment_mbps == 10
    assert entry.measured_load_mbps == 5
    assert entry.available_guaranteed_mbps == 45
    assert entry.per_tenant_reservation_mbps == {"tenant-a": 20, "tenant-b": 30}

    rejected = ResourceLedger(store).admission(link_id, 50)
    assert rejected.decision in {AdmissionDecision.ACCEPT_WITH_ADJUSTMENT, AdmissionDecision.REJECT}
    assert entry.available_guaranteed_mbps != entry.physical_capacity_mbps - entry.measured_load_mbps


def test_allocator_release_is_scoped_and_preserves_other_policy(tmp_path):
    store = IntentStore(tmp_path / "release.db")
    allocator = QoSResourceAllocator(store)
    allocator.allocate(policy_id="POL-A", traffic_scope_key="scope-a", rate_mbps=20, ceil_mbps=100)
    other = allocator.allocate(policy_id="POL-B", traffic_scope_key="scope-b", rate_mbps=30, ceil_mbps=100)
    assert allocator.release("scope-a") is True
    assert [item.binding_id for item in allocator.list_active()] == [other.binding_id]


def test_reservation_integrity_retires_orphaned_active_reservation(tmp_path):
    store = IntentStore(tmp_path / "reservation-integrity.db")
    allocator = QoSResourceAllocator(store)
    binding = allocator.allocate(
        policy_id="POL-OLD", traffic_scope_key="scope-old", rate_mbps=8, ceil_mbps=100,
    )
    allocator.bind_runtime(binding.binding_id, {
        "qos_binding": {
            "link_id": "s_access_r-s_agg_r", "device_id": "device:bmv2:5",
            "interface_name": "s_access_r-eth3",
        },
    })
    assert allocator.release("scope-old") is True
    # Simulate the historical interrupted-update defect: the owner is retired
    # while its derived accounting record incorrectly remains active.
    with store._lock, store._connect() as connection:
        connection.execute(
            "UPDATE resource_reservations SET active=1 WHERE reservation_id=?",
            (f"RES-{binding.binding_id}",),
        )

    result = allocator.reconcile_reservation_integrity()

    assert result["released_reservation_ids"] == [f"RES-{binding.binding_id}"]
    with store._lock, store._connect() as connection:
        reservation = connection.execute(
            "SELECT active FROM resource_reservations WHERE reservation_id=?",
            (f"RES-{binding.binding_id}",),
        ).fetchone()
    assert reservation["active"] == 0


def _stable_record(**changes):
    payload = {
        "capacity_record_id": "CAP-test", "topology_id": "research_dc",
        "environment_epoch": "ENV-test", "controller_epoch": "ONOS-test",
        "path_signature": "PATH-test", "bottleneck_egress": "link:s_access_r--s_agg_r",
        "packet_profile": "udp-1200", "protocol": "UDP", "packet_size_bytes": 1200,
        "observation_mode": "COMPETING_TRAFFIC", "physical_capacity_mbps": 100,
        "stable_capacity_mbps": 55, "capacity_floor_mbps": 55,
        "p05_capacity_mbps": 55, "sample_count": 5, "confidence": "CALIBRATED_LOW",
        "measured_at": "2026-08-05T00:00:00+00:00", "valid_until": "2099-08-05T00:00:00+00:00",
        "source_experiment": "test", "safety_margin_mbps": 0,
    }
    payload.update(changes)
    return StableDataplaneCapacityRecord(**payload)


def test_stable_capacity_model_uses_conservative_lower_bound_and_is_persistent(tmp_path):
    estimate = conservative_capacity_estimate([55, 58, 56, 57, 54.9])
    assert estimate["lower_bound_mbps"] == 54.9
    assert estimate["confidence"] == "CALIBRATED_LOW"
    store = IntentStore(tmp_path / "stable.db")
    repository = StableCapacityRepository(store)
    repository.put(_stable_record())
    record = repository.active_for(bottleneck_egress="link:s_access_r--s_agg_r", topology_id="research_dc")
    assert record and StableCapacityRepository.schedulable_capacity(record) == 55


def test_htb_capacity_is_a_conservative_whole_mbit_projection():
    record = _stable_record(stable_capacity_mbps=58.088, capacity_floor_mbps=58.088, safety_margin_mbps=5)
    assert StableCapacityRepository.schedulable_capacity(record) == 53.088
    assert StableCapacityRepository.htb_schedulable_capacity(record) == 53


def test_ledger_admission_uses_stable_not_physical_capacity(tmp_path):
    store = IntentStore(tmp_path / "stable-ledger.db")
    allocator = QoSResourceAllocator(store)
    binding = allocator.allocate(policy_id="POL-30", traffic_scope_key="scope-30", rate_mbps=30, ceil_mbps=55)
    allocator.bind_runtime(binding.binding_id, {"qos_binding": {"link_id": "s_access_r-s_agg_r", "device_id": "device:bmv2:5", "interface_name": "s_access_r-eth3"}})
    StableCapacityRepository(store).put(_stable_record())
    resource_id = ResourceRegistry().resolve("s_access_r-s_agg_r")
    entry = next(item for item in ResourceLedger(store).entries() if item.resource_id == resource_id)
    assert entry.physical_capacity_mbps == 100
    assert entry.schedulable_capacity_mbps == 55
    assert entry.remaining_best_effort_capacity_mbps == 25
    assert ResourceLedger(store).admission(resource_id, 30).reason == "INSUFFICIENT_STABLE_DATAPLANE_CAPACITY"


def test_ledger_honors_durable_scheduler_cap_over_fractional_calibration(tmp_path, monkeypatch):
    """A materialized ingress cap is a stricter resource authority than p05 evidence."""
    store = IntentStore(tmp_path / "durable-cap-ledger.db")
    allocator = QoSResourceAllocator(store)
    binding = allocator.allocate(
        policy_id="POL-40", traffic_scope_key="scope-40", rate_mbps=40, ceil_mbps=49,
    )
    allocator.bind_runtime(binding.binding_id, {
        "qos_binding": {"link_id": "s_access_r-s_agg_r", "device_id": "device:bmv2:5",
                        "interface_name": "s_access_r-eth3"}
    })
    record = _stable_record(stable_capacity_mbps=58.576017, capacity_floor_mbps=58.576017,
                            safety_margin_mbps=5)
    StableCapacityRepository(store).put(record)
    IngressAdmissionDesiredRepository(store).append_desired({
        "generation_id": "IADS-test", "admission_plan_id": "ingress-test",
        "intent_id": "INT-test", "revision": 1, "traffic_scope_key": "scope-40",
        "desired_policy_id": "POL-40", "executed_resource_owner_policy_id": "POL-40",
        "resource_binding_id": binding.binding_id, "capacity_record_id": record.capacity_record_id,
        "desired": {"schedulable_capacity_mbps": 49}, "created_at": "2026-08-08T00:00:00+00:00",
    })

    resource_id = ResourceRegistry().resolve("s_access_r-s_agg_r")
    entry = next(item for item in ResourceLedger(store).entries() if item.resource_id == resource_id)

    assert entry.authorized_schedulable_capacity_mbps == 49
    assert entry.schedulable_capacity_mbps == 49
    assert entry.remaining_guaranteed_capacity_mbps == 9
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", lambda: ResourceLedger(store))
    decision = authoritative_admission_decision(
        link_id=resource_id, src_device="device:bmv2:5", dst_device="device:bmv2:3",
        requested_mbps=10,
    )
    assert decision["decision"] == "REJECTED"
    assert decision["reason_code"] == "INSUFFICIENT_CAPACITY"

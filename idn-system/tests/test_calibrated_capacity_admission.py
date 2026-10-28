from types import SimpleNamespace

from api import capacity_evidence
from agents.policy_conflict_checker import PolicyConflictCheckerAgent
from models.yang_policy import YANGPolicy


def test_qos_revision_capacity_purpose_distinguishes_decrease_from_increase():
    """Only an increase is a new runtime-capacity admission decision."""
    decrease = PolicyConflictCheckerAgent._capacity_evaluation_purpose({
        "execution_mode": "MODIFY_QOS_RESOURCE", "change_direction": "DOWNGRADE",
    })
    increase = PolicyConflictCheckerAgent._capacity_evaluation_purpose({
        "execution_mode": "MODIFY_QOS_RESOURCE", "change_direction": "INCREASE",
    })
    assert decrease is capacity_evidence.CapacityEvaluationPurpose.DECREASE_UPDATE
    assert increase is capacity_evidence.CapacityEvaluationPurpose.INCREASE_UPDATE


def test_exact_active_binding_is_capacity_authority_when_profile_has_no_fixed_bottleneck(monkeypatch):
    """Generic profiles must not fall back to the unrelated primary path."""
    draft = YANGPolicy.model_validate({
        "service_id": "ai_data",
        "network_policy": {
            "policy_id": "POL-test", "policy_name": "ai-data-qos",
            "match": {"source_ip": "192.168.20.10/32", "destination_ip": "10.0.0.10/32", "protocol": "TCP", "destination_port": 5202},
            "action": {"action_type": "SET_PRIORITY", "qos_params": {"qos_mode": "MIN_BANDWIDTH", "bandwidth": 20}},
        },
    })

    class Registry:
        def snapshot(self):
            return SimpleNamespace(
                interfaces=[
                    SimpleNamespace(resource_id="if-a", device_id="device:bmv2:3"),
                    SimpleNamespace(resource_id="if-b", device_id="device:bmv2:1"),
                ],
                links=[SimpleNamespace(resource_id="link:s_agg_r--s_core1", src_interface_id="if-a", dst_interface_id="if-b")],
            )
    class Allocator:
        def get_active_for_scope(self, _scope):
            return SimpleNamespace(resource_key="link:s_agg_r--s_core1")

    monkeypatch.setattr("api.service_profiles.get_service_profile", lambda _service_id: SimpleNamespace(qos_binding={}))
    monkeypatch.setattr("agents.policy_conflict_checker.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("agents.policy_conflict_checker.QoSResourceAllocator", lambda: Allocator())

    assert PolicyConflictCheckerAgent._profile_qos_resource(draft) == (
        "link:s_agg_r--s_core1", "device:bmv2:3", "device:bmv2:1",
    )


def test_calibrated_capacity_is_the_pre_execution_admission_ceiling(monkeypatch):
    """A valid calibrated limit must win over a larger physical link rate."""
    class Registry:
        def snapshot(self):
            return SimpleNamespace(
                topology_name="test-topology",
                aliases={"edge-a-b": "link:a--b"},
                interfaces=[
                    SimpleNamespace(resource_id="if-a", device_id="a"),
                    SimpleNamespace(resource_id="if-b", device_id="b"),
                ],
                links=[SimpleNamespace(resource_id="link:a--b", src_interface_id="if-a", dst_interface_id="if-b")],
            )

    class Repository:
        def active_for(self, **kwargs):
            assert kwargs == {"bottleneck_egress": "link:a--b", "topology_id": "test-topology"}
            return SimpleNamespace(capacity_record_id="CAP-test")

        @staticmethod
        def schedulable_capacity(_record):
            return 49.95

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.stable_capacity.StableCapacityRepository", Repository)

    capacity, source, record_id = capacity_evidence.schedulable_capacity_mbps(
        "edge-a-b", physical_capacity_mbps=100,
        src_device="a", dst_device="b",
    )

    assert capacity == 49.95
    assert source == "stable_dataplane_capacity"
    assert record_id == "CAP-test"


def test_authoritative_admission_rejects_without_calibrated_record(monkeypatch):
    """Physical 100 Mbps must never silently admit a guaranteed request."""
    class Registry:
        def snapshot(self):
            return SimpleNamespace(
                aliases={"edge-a-b": "link:a--b"},
                interfaces=[
                    SimpleNamespace(resource_id="if-a", device_id="a"),
                    SimpleNamespace(resource_id="if-b", device_id="b"),
                ],
                links=[SimpleNamespace(resource_id="link:a--b", src_interface_id="if-a", dst_interface_id="if-b")],
            )

    class Ledger:
        def entries(self):
            return [SimpleNamespace(
                resource_id="link:a--b", physical_capacity_mbps=100.0,
                stable_capacity_mbps=None, safety_margin_mbps=5.0,
                schedulable_capacity_mbps=100.0,
                committed_guaranteed_capacity_mbps=0.0,
                remaining_guaranteed_capacity_mbps=100.0,
                capacity_record_id=None,
            )]

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", Ledger)
    decision = capacity_evidence.authoritative_admission_decision(
        link_id="edge-a-b", src_device="a", dst_device="b", requested_mbps=30,
    )

    assert decision["decision"] == "REJECTED"
    assert decision["reason_code"] == "RUNTIME_CAPACITY_UNVERIFIED"
    assert decision["physical_capacity_mbps"] == 100.0
    assert decision["safety_margin_mbps"] == 5.0


def test_durable_contract_rejects_before_runtime_health_gate(monkeypatch):
    class Registry:
        def snapshot(self):
            return SimpleNamespace(aliases={"edge-a-b": "link:a--b"}, interfaces=[], links=[])

    class Ledger:
        def entries(self):
            return [SimpleNamespace(
                resource_id="link:a--b", physical_capacity_mbps=100.0,
                stable_capacity_mbps=None, safety_margin_mbps=5.0,
                schedulable_capacity_mbps=49.0, authorized_schedulable_capacity_mbps=49.0,
                committed_guaranteed_capacity_mbps=40.0, remaining_guaranteed_capacity_mbps=9.0,
                capacity_record_id=None,
            )]

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", Ledger)
    decision = capacity_evidence.authoritative_admission_decision(
        link_id="edge-a-b", src_device="a", dst_device="b", requested_mbps=10,
    )

    assert decision["decision"] == "REJECTED"
    assert decision["reason_code"] == "INSUFFICIENT_CAPACITY"
    assert decision["contract_capacity_mbps"] == 49.0
    assert decision["contract_available_mbps"] == 9.0


def test_recovery_does_not_require_runtime_capacity_or_create_admission(monkeypatch):
    class Registry:
        def snapshot(self):
            return SimpleNamespace(aliases={"edge-a-b": "link:a--b"}, interfaces=[], links=[])

    class Ledger:
        def entries(self):
            return [SimpleNamespace(
                resource_id="link:a--b", physical_capacity_mbps=100.0,
                stable_capacity_mbps=None, safety_margin_mbps=5.0,
                schedulable_capacity_mbps=49.0, authorized_schedulable_capacity_mbps=49.0,
                committed_guaranteed_capacity_mbps=40.0, remaining_guaranteed_capacity_mbps=9.0,
                capacity_record_id=None,
            )]

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", Ledger)
    decision = capacity_evidence.authoritative_admission_decision(
        link_id="edge-a-b", src_device="a", dst_device="b", requested_mbps=0,
        purpose=capacity_evidence.CapacityEvaluationPurpose.RECOVERY_EXISTING,
    )

    assert decision["decision"] == "ADMITTED"
    assert decision["reason_code"] == "DURABLE_CONTRACT_OPERATION"
    assert decision["runtime_capacity_status"] == "DEGRADED"


def test_authoritative_admission_accounts_for_existing_reservations(monkeypatch):
    class Registry:
        def snapshot(self):
            return SimpleNamespace(
                aliases={"edge-a-b": "link:a--b"},
                interfaces=[SimpleNamespace(resource_id="if-a", device_id="a"), SimpleNamespace(resource_id="if-b", device_id="b")],
                links=[SimpleNamespace(resource_id="link:a--b", src_interface_id="if-a", dst_interface_id="if-b")],
            )

    class Ledger:
        def entries(self):
            return [SimpleNamespace(
                resource_id="link:a--b", physical_capacity_mbps=100.0,
                stable_capacity_mbps=54.9496, safety_margin_mbps=5.0,
                schedulable_capacity_mbps=49.9496,
                committed_guaranteed_capacity_mbps=30.0,
                remaining_guaranteed_capacity_mbps=19.9496,
                capacity_record_id="CAP-49",
            )]

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", Ledger)
    decision = capacity_evidence.authoritative_admission_decision(
        link_id="edge-a-b", src_device="a", dst_device="b", requested_mbps=30,
    )

    assert decision["decision"] == "REJECTED"
    assert decision["reason_code"] == "INSUFFICIENT_CAPACITY"
    assert decision["allocated_mbps"] == 30.0


def test_same_scope_revision_reclaims_its_existing_guarantee(monkeypatch):
    """An in-place 30 Mbps QoS revision must not reserve that 30 Mbps twice."""
    class Registry:
        def snapshot(self):
            return SimpleNamespace(
                aliases={"edge-a-b": "link:a--b"},
                interfaces=[SimpleNamespace(resource_id="if-a", device_id="a"), SimpleNamespace(resource_id="if-b", device_id="b")],
                links=[SimpleNamespace(resource_id="link:a--b", src_interface_id="if-a", dst_interface_id="if-b")],
            )

    class Ledger:
        def entries(self):
            return [SimpleNamespace(
                resource_id="link:a--b", physical_capacity_mbps=100.0,
                stable_capacity_mbps=58.5, safety_margin_mbps=5.0,
                schedulable_capacity_mbps=53.5,
                committed_guaranteed_capacity_mbps=38.0,
                remaining_guaranteed_capacity_mbps=15.5,
                capacity_record_id="CAP-live",
            )]

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", Ledger)
    decision = capacity_evidence.authoritative_admission_decision(
        link_id="edge-a-b", src_device="a", dst_device="b",
        requested_mbps=30, reclaim_mbps=30,
    )

    assert decision["decision"] == "ADMITTED"
    assert decision["reclaimed_mbps"] == 30.0
    assert decision["effective_allocated_mbps"] == 8.0
    assert decision["requested_delta_mbps"] == 0.0
    assert decision["available_mbps"] == 15.5


def test_zero_delta_update_short_circuits_before_unverified_runtime_capacity(monkeypatch):
    """A 10 -> 10 revision is readback-only, even without fresh calibration."""
    class Registry:
        def snapshot(self):
            return SimpleNamespace(aliases={"edge-a-b": "link:a--b"}, interfaces=[], links=[])

    class Ledger:
        def entries(self):
            return [SimpleNamespace(
                resource_id="link:a--b", physical_capacity_mbps=100.0,
                stable_capacity_mbps=None, safety_margin_mbps=5.0,
                schedulable_capacity_mbps=49.0, authorized_schedulable_capacity_mbps=49.0,
                committed_guaranteed_capacity_mbps=20.0, remaining_guaranteed_capacity_mbps=29.0,
                capacity_record_id=None,
            )]

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", Ledger)
    decision = capacity_evidence.authoritative_admission_decision(
        link_id="edge-a-b", src_device="a", dst_device="b", requested_mbps=10,
        reclaim_mbps=10, purpose=capacity_evidence.CapacityEvaluationPurpose.INCREASE_UPDATE,
    )

    assert decision["decision"] == "ADMITTED"
    assert decision["operation"] == "VERIFY_EXISTING"
    assert decision["requested_delta_mbps"] == 0.0
    assert decision["no_change"] is True
    assert decision["positive_capacity_admission_required"] is False
    assert decision["runtime_capacity_status"] == "NOT_REQUIRED"
    assert decision["reason_code"] == "NO_CHANGE_VERIFY_EXISTING"


def test_rate_limit_zero_delta_also_short_circuits_before_runtime_capacity(monkeypatch):
    """The capacity boundary is QoS-mode agnostic: 20 -> 20 is no new demand."""
    class Registry:
        def snapshot(self):
            return SimpleNamespace(aliases={"edge-a-b": "link:a--b"}, interfaces=[], links=[])

    class Ledger:
        def entries(self):
            return [SimpleNamespace(
                resource_id="link:a--b", physical_capacity_mbps=100.0,
                stable_capacity_mbps=None, safety_margin_mbps=5.0,
                schedulable_capacity_mbps=49.0, authorized_schedulable_capacity_mbps=49.0,
                committed_guaranteed_capacity_mbps=20.0, remaining_guaranteed_capacity_mbps=29.0,
                capacity_record_id=None,
            )]

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", Ledger)
    decision = capacity_evidence.authoritative_admission_decision(
        link_id="edge-a-b", src_device="a", dst_device="b", requested_mbps=20,
        reclaim_mbps=20, purpose=capacity_evidence.CapacityEvaluationPurpose.INCREASE_UPDATE,
    )

    assert decision["operation"] == "VERIFY_EXISTING"
    assert decision["requested_delta_mbps"] == 0.0
    assert decision["decision"] == "ADMITTED"


def test_increase_uses_target_delta_and_fresh_runtime_without_conflating_contract(monkeypatch):
    """20->30 consumes ten Mbps; fresh 41 Mbps is sufficient despite a 49 Mbps contract."""
    class Registry:
        def snapshot(self):
            return SimpleNamespace(aliases={"edge-a-b": "link:a--b"}, interfaces=[], links=[])

    class Ledger:
        def entries(self):
            return [SimpleNamespace(
                resource_id="link:a--b", physical_capacity_mbps=100.0,
                stable_capacity_mbps=46.0, safety_margin_mbps=5.0,
                schedulable_capacity_mbps=41.0, authorized_schedulable_capacity_mbps=49.0,
                committed_guaranteed_capacity_mbps=30.0, remaining_guaranteed_capacity_mbps=19.0,
                capacity_record_id="CAP-fresh",
            )]

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", Ledger)
    decision = capacity_evidence.authoritative_admission_decision(
        link_id="edge-a-b", src_device="a", dst_device="b", requested_mbps=30,
        reclaim_mbps=20, purpose=capacity_evidence.CapacityEvaluationPurpose.INCREASE_UPDATE,
    )

    assert decision["decision"] == "ADMITTED"
    assert decision["requested_delta_mbps"] == 10.0
    assert (decision["contract_available_mbps"], decision["runtime_available_mbps"], decision["effective_available_mbps"]) == (19.0, 11.0, 11.0)


def test_ten_to_thirty_update_passes_contract_delta_before_runtime_gate(monkeypatch):
    """10 -> 30 consumes 20; 49 - (10 + 10) leaves 29 for the contract check."""
    class Registry:
        def snapshot(self):
            return SimpleNamespace(aliases={"edge-a-b": "link:a--b"}, interfaces=[], links=[])

    class Ledger:
        def entries(self):
            return [SimpleNamespace(
                resource_id="link:a--b", physical_capacity_mbps=100.0,
                stable_capacity_mbps=None, safety_margin_mbps=5.0,
                schedulable_capacity_mbps=49.0, authorized_schedulable_capacity_mbps=49.0,
                committed_guaranteed_capacity_mbps=20.0, remaining_guaranteed_capacity_mbps=29.0,
                capacity_record_id=None,
            )]

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", Ledger)
    decision = capacity_evidence.authoritative_admission_decision(
        link_id="edge-a-b", src_device="a", dst_device="b", requested_mbps=30,
        reclaim_mbps=10, purpose=capacity_evidence.CapacityEvaluationPurpose.INCREASE_UPDATE,
    )

    assert decision["operation"] == "INCREASE_UPDATE"
    assert decision["existing_target_mbps"] == 10.0
    assert decision["requested_target_mbps"] == 30.0
    assert decision["requested_delta_mbps"] == 20.0
    assert decision["contract_available_mbps"] == 29.0
    assert decision["current_guaranteed_allocation_mbps"] == 20.0
    assert decision["runtime_current_allocation_mbps"] == 20.0
    assert decision["decision"] == "REJECTED"
    assert decision["reason_code"] == "RUNTIME_CAPACITY_UNVERIFIED"
    assert decision["runtime_available_mbps"] is None
    assert decision["runtime_schedulable_total_mbps"] is None
    assert decision["effective_available_mbps"] is None


def test_new_admission_keeps_full_target_semantics(monkeypatch):
    """Only revisions reclaim existing capacity; a new 30 Mbps admission does not."""
    class Registry:
        def snapshot(self):
            return SimpleNamespace(aliases={"edge-a-b": "link:a--b"}, interfaces=[], links=[])

    class Ledger:
        def entries(self):
            return [SimpleNamespace(
                resource_id="link:a--b", physical_capacity_mbps=100.0,
                stable_capacity_mbps=54.0, safety_margin_mbps=5.0,
                schedulable_capacity_mbps=49.0, authorized_schedulable_capacity_mbps=49.0,
                committed_guaranteed_capacity_mbps=20.0, remaining_guaranteed_capacity_mbps=29.0,
                capacity_record_id="CAP-49",
            )]

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", Ledger)
    decision = capacity_evidence.authoritative_admission_decision(
        link_id="edge-a-b", src_device="a", dst_device="b", requested_mbps=30,
        purpose=capacity_evidence.CapacityEvaluationPurpose.NEW_ADMISSION,
    )

    assert decision["requested_delta_mbps"] == 30.0
    assert decision["decision"] == "REJECTED"
    assert decision["reason_code"] == "INSUFFICIENT_CAPACITY"


def test_impossible_update_rejects_on_durable_contract_before_runtime_gate(monkeypatch):
    class Registry:
        def snapshot(self):
            return SimpleNamespace(aliases={"edge-a-b": "link:a--b"}, interfaces=[], links=[])

    class Ledger:
        def entries(self):
            return [SimpleNamespace(
                resource_id="link:a--b", physical_capacity_mbps=100.0,
                stable_capacity_mbps=None, safety_margin_mbps=5.0,
                schedulable_capacity_mbps=0.0, authorized_schedulable_capacity_mbps=49.0,
                committed_guaranteed_capacity_mbps=30.0, remaining_guaranteed_capacity_mbps=19.0,
                capacity_record_id=None,
            )]

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", Ledger)
    decision = capacity_evidence.authoritative_admission_decision(
        link_id="edge-a-b", src_device="a", dst_device="b", requested_mbps=45,
        reclaim_mbps=20, purpose=capacity_evidence.CapacityEvaluationPurpose.INCREASE_UPDATE,
    )

    assert decision["decision"] == "REJECTED"
    assert decision["reason_code"] == "INSUFFICIENT_CAPACITY"
    assert decision["requested_delta_mbps"] == 25.0


def test_decrease_needs_no_fresh_runtime_evidence(monkeypatch):
    class Registry:
        def snapshot(self):
            return SimpleNamespace(aliases={"edge-a-b": "link:a--b"}, interfaces=[], links=[])

    class Ledger:
        def entries(self):
            return [SimpleNamespace(
                resource_id="link:a--b", physical_capacity_mbps=100.0,
                stable_capacity_mbps=None, safety_margin_mbps=5.0,
                schedulable_capacity_mbps=0.0, authorized_schedulable_capacity_mbps=49.0,
                committed_guaranteed_capacity_mbps=30.0, remaining_guaranteed_capacity_mbps=19.0,
                capacity_record_id=None,
            )]

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr("infra.resource_ledger.ResourceLedger", Ledger)
    decision = capacity_evidence.authoritative_admission_decision(
        link_id="edge-a-b", src_device="a", dst_device="b", requested_mbps=10,
        reclaim_mbps=20, purpose=capacity_evidence.CapacityEvaluationPurpose.DECREASE_UPDATE,
    )

    assert decision["decision"] == "ADMITTED"
    assert decision["reason_code"] == "DURABLE_CONTRACT_OPERATION"


def test_profile_qos_binding_resolves_to_the_canonical_shared_resource():
    """Named services use registry-owned bottlenecks before generic path fallback."""
    from models.yang_policy import ActionRules, ActionType, MatchCriteria, NetworkPolicy, QoSParams, YANGPolicy

    policy = YANGPolicy(
        service_id="background_video",
        network_policy=NetworkPolicy(
            policy_name="background-video",
            match=MatchCriteria(source_ip="192.168.10.20/32", destination_ip="10.0.0.10/32",
                                protocol="UDP", destination_port=5005),
            action=ActionRules(action_type=ActionType.SET_PRIORITY, qos_params=QoSParams(bandwidth=10, dscp_value=0)),
        ),
    )

    resource_id, devices = PolicyConflictCheckerAgent._profile_qos_resource(policy)[0], PolicyConflictCheckerAgent._profile_qos_resource(policy)[1:]
    assert resource_id == "link:s_access_r--s_agg_r"
    assert set(devices) == {"device:bmv2:5", "device:bmv2:3"}


def test_same_scope_reclaim_uses_active_binding_only_on_same_bottleneck(monkeypatch):
    """A stale policy document must not double-count its live exact-scope binding."""
    binding = SimpleNamespace(
        reservation_kind="GUARANTEED", resource_key="link:a--b", rate_mbps=30,
    )

    class Allocator:
        def get_active_for_scope(self, scope):
            assert scope == "scope-video"
            return binding

    class Registry:
        def resolve(self, value):
            return {"edge-a-b": "link:a--b", "edge-other": "link:other"}[value]

    monkeypatch.setattr("agents.policy_conflict_checker.QoSResourceAllocator", Allocator)
    monkeypatch.setattr("agents.policy_conflict_checker.get_resource_registry", lambda: Registry())

    assert PolicyConflictCheckerAgent._active_binding_reclaim_for_bottleneck(
        "scope-video", "edge-a-b",
    ) == 30
    assert PolicyConflictCheckerAgent._active_binding_reclaim_for_bottleneck(
        "scope-video", "edge-other",
    ) == 0


def test_same_scope_reclaim_resolves_runtime_onos_link_by_device_pair(monkeypatch):
    binding = SimpleNamespace(
        reservation_kind="GUARANTEED", resource_key="link:a--b", rate_mbps=30,
    )

    class Allocator:
        def get_active_for_scope(self, _scope):
            return binding

    class Registry:
        def resolve(self, _value):
            raise KeyError("runtime link IDs are not durable aliases")

        def snapshot(self):
            return SimpleNamespace(
                interfaces=[
                    SimpleNamespace(resource_id="if-a", device_id="device:a"),
                    SimpleNamespace(resource_id="if-b", device_id="device:b"),
                ],
                links=[SimpleNamespace(
                    resource_id="link:a--b", src_interface_id="if-a", dst_interface_id="if-b",
                )],
            )

    monkeypatch.setattr("agents.policy_conflict_checker.QoSResourceAllocator", Allocator)
    monkeypatch.setattr("agents.policy_conflict_checker.get_resource_registry", lambda: Registry())

    assert PolicyConflictCheckerAgent._active_binding_reclaim_for_bottleneck(
        "scope-video", "link-device:a-[eth3](3)-to-device:b-2", "device:a", "device:b",
    ) == 30

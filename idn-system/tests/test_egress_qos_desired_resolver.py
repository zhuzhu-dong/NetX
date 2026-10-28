"""Regression coverage for the authoritative egress desired resolver."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from infra.egress_qos_desired import resolve_egress_qos_desired
from infra.egress_qos_desired import _aggregate_current_minimum_reservations
from api.store import IntentStore
from infra.qos_resource_allocator import QoSResourceAllocator
from persistence.policy_store import PolicyStore
from models.resources import ResourceBinding, StableDataplaneCapacityRecord
from models.yang_policy import YANGPolicy


SCOPE = "TSK-generic"


def _binding(binding_id="RB-current", *, active=True, rate=30, runtime=None):
    return ResourceBinding(
        binding_id=binding_id, policy_id="POL-binding", service_id="generic", traffic_scope_key=SCOPE,
        resource_key="link:a--b", device_id="device:bmv2:1", interface_id="s_a-eth3",
        class_id="1:100", filter_id=101, handle=101, dscp=46, rate_mbps=rate, ceil_mbps=1000,
        reservation_kind="GUARANTEED", tenant_id="default", created_at="2026-08-07T00:00:00+00:00",
        active=active, runtime=runtime or {},
    )


def _capacity(*, schedulable=49.9):
    return StableDataplaneCapacityRecord(
        capacity_record_id="CAP-current", topology_id="topo", environment_epoch="ENV-current",
        controller_epoch="ONOS-current", path_signature="path", bottleneck_egress="link:a--b",
        packet_profile="udp", protocol="UDP", packet_size_bytes=1200, observation_mode="COMPETING_TRAFFIC",
        physical_capacity_mbps=100, stable_capacity_mbps=schedulable + 5,
        capacity_floor_mbps=schedulable + 5, p05_capacity_mbps=schedulable + 5,
        sample_count=5, confidence="CALIBRATED_LOW", measured_at="2026-08-07T00:00:00+00:00",
        valid_until="2027-08-07T00:00:00+00:00", source_experiment="formal-calibration", safety_margin_mbps=5,
    )


def _ingress(*, floor=30, binding_id="RB-current", capacity_id="CAP-current"):
    return {
        "generation_id": "IADS-current", "admission_plan_id": "INGRESS-current", "intent_id": "INT-current",
        "traffic_scope_key": SCOPE, "desired_policy_id": "POL-request", "executed_resource_owner_policy_id": "POL-current",
        "resource_binding_id": binding_id, "capacity_record_id": capacity_id,
        "desired": {"qos_mode": "MIN_BANDWIDTH", "guaranteed_floor_mbps": floor,
                    "schedulable_capacity_mbps": 49,
                    "protected_selector": {"source_ip": "192.0.2.10", "destination_ip": "198.51.100.10", "protocol": "udp", "destination_port": 5100, "dscp": 34}},
    }


def _policy(*, mode="MIN_BANDWIDTH", bandwidth=30):
    model = YANGPolicy.model_validate({"network_policy": {
        "policy_id": "POL-current", "policy_name": "generic-service", "match": {
            "source_ip": "192.0.2.10/32", "destination_ip": "198.51.100.10/32", "protocol": "UDP", "destination_port": 5100,
        }, "action": {"action_type": "SET_PRIORITY", "qos_params": {"qos_mode": mode, "bandwidth": bandwidth, "dscp_value": 34}},
    }, "service_id": "generic"})
    return {"policy_id": "POL-current", "status": "ACTIVE", "yang_json": model.model_dump_json()}


PROFILE = SimpleNamespace(service_id="generic", src_ip="192.0.2.10/32", dst_ip="198.51.100.10/32", protocol="UDP", dst_port=5100, dscp=34)


def _resolve(*, bindings=None, ingress=None, capacity=None, policy=None, aggregate_guaranteed_mbps=None, capacity_purpose="NEW_ADMISSION"):
    return resolve_egress_qos_desired(
        traffic_scope_key=SCOPE, ingress_desired=ingress if ingress is not None else _ingress(),
        bindings=bindings if bindings is not None else [_binding()], current_policy=policy or _policy(),
        capacity_records=[] if capacity is None else [capacity], topology_id="topo", service_profile=PROFILE,
        aggregate_guaranteed_mbps=aggregate_guaranteed_mbps, capacity_purpose=capacity_purpose,
    )


def test_inactive_eight_mbps_history_is_ignored_and_current_ingress_wins():
    result = _resolve(bindings=[_binding("RB-historical", active=False, rate=8), _binding()], capacity=_capacity())
    assert result.accepted and result.target_rate_mbps == 30 and result.root_rate_mbps == 49
    assert result.ignored_bindings == [{"binding_id": "RB-historical", "reason": "SUPERSEDED_OR_NON_AUTHORITATIVE_BINDING"}]


def test_two_inactive_eight_mbps_bindings_cannot_change_current_thirty():
    result = _resolve(bindings=[_binding("RB-old-1", active=False, rate=8), _binding("RB-old-2", active=False, rate=8), _binding()], capacity=_capacity())
    assert result.accepted and result.target_rate_mbps == 30 and len(result.ignored_bindings) == 2


def test_calibrated_schedulable_capacity_wins_over_physical_capacity():
    result = _resolve(capacity=_capacity(schedulable=49.9))
    assert result.accepted and result.root_rate_mbps == 49 and result.root_rate_mbps != 100


def test_ingress_floor_is_the_minimum_bandwidth_contract_not_a_service_constant():
    result = _resolve(ingress=_ingress(floor=10), capacity=_capacity())
    assert result.accepted and result.target_rate_mbps == 10 and result.default_ceil_mbps == 39


def test_shared_resource_default_ceiling_reserves_all_current_minimums():
    result = _resolve(capacity=_capacity(), aggregate_guaranteed_mbps=40)
    assert result.accepted
    assert result.target_rate_mbps == 30
    assert result.root_rate_mbps == 49
    assert result.default_ceil_mbps == 9


def test_aggregate_reservations_interprets_qos_mode_enum_value(monkeypatch):
    policy = _policy(mode="MIN_BANDWIDTH", bandwidth=30)
    monkeypatch.setattr(
        "infra.egress_qos_desired.PolicyStore.resolve_managed_desired_policies",
        lambda _self: {SCOPE: policy},
    )
    monkeypatch.setattr(
        "infra.egress_qos_desired.QoSResourceAllocator.get_active_for_scope",
        lambda _self, _scope: _binding(),
    )
    assert _aggregate_current_minimum_reservations(
        store=IntentStore(), resource_key="link:a--b",
    ) == 30


def test_aggregate_reservations_uses_binding_for_preference_without_bandwidth(monkeypatch):
    """Preference-only desired policies retain their active reservation."""
    policy = _policy(mode="MIN_BANDWIDTH", bandwidth=None)
    monkeypatch.setattr(
        "infra.egress_qos_desired.PolicyStore.resolve_managed_desired_policies",
        lambda _self: {SCOPE: policy},
    )
    monkeypatch.setattr(
        "infra.egress_qos_desired.QoSResourceAllocator.get_active_for_scope",
        lambda _self, _scope: _binding(rate=10),
    )
    assert _aggregate_current_minimum_reservations(
        store=IntentStore(), resource_key="link:a--b",
    ) == 10


def test_generic_profile_and_selector_are_not_research_specific():
    result = _resolve(capacity=_capacity())
    assert result.accepted
    assert result.selector == {"source_prefix": "192.0.2.10/32", "destination_prefix": "198.51.100.10/32", "ip_protocol": 17, "destination_port": 5100, "dscp": 34}


def test_no_stable_capacity_is_a_no_write_failure_not_a_100mbps_fallback():
    result = _resolve(capacity=None)
    assert result.status == "STABLE_CAPACITY_EVIDENCE_MISSING" and result.root_rate_mbps is None


def test_expired_capacity_record_is_not_current_evidence():
    stale = _capacity()
    stale.valid_until = "2020-01-01T00:00:00+00:00"
    result = _resolve(capacity=stale)
    assert result.status == "STABLE_CAPACITY_EVIDENCE_MISSING"


def test_recovery_uses_durable_ingress_contract_without_fresh_capacity():
    result = _resolve(capacity=None, capacity_purpose="RECOVERY_EXISTING")
    assert result.accepted
    assert (result.root_rate_mbps, result.target_rate_mbps) == (49, 30)
    assert result.source_provenance["capacity"]["runtime_capacity_status"] == "DEGRADED"


def test_reconcile_uses_durable_contract_when_runtime_is_below_it():
    stale = _capacity(schedulable=41.1)
    stale.valid_until = "2020-01-01T00:00:00+00:00"
    result = _resolve(capacity=stale, capacity_purpose="RECONCILE_EXISTING")
    assert result.accepted
    assert result.root_rate_mbps == 49
    assert result.source_provenance["capacity"]["runtime_capacity_status"] == "DEGRADED"


def test_historical_only_binding_is_not_an_active_desired_source():
    result = _resolve(bindings=[_binding("RB-historical", active=False, rate=8)], capacity=_capacity())
    assert result.status == "ACTIVE_BINDING_MISSING" and result.ignored_bindings


def test_multiple_active_bindings_fail_closed():
    result = _resolve(bindings=[_binding(), _binding("RB-other")], capacity=_capacity())
    assert result.status == "AMBIGUOUS_ACTIVE_BINDING"


def test_binding_runtime_and_tc_observation_never_participate_in_desired_resolution():
    result = _resolve(bindings=[_binding(runtime={"tc_readback": {"rate_mbps": 8, "root_rate_mbps": 100}})], capacity=_capacity())
    assert result.accepted and (result.root_rate_mbps, result.target_rate_mbps) == (49, 30)


def test_recovery_and_resident_consumers_share_the_same_canonical_contract_hash():
    first, second = _resolve(capacity=_capacity()), _resolve(capacity=_capacity())
    first_hash = hashlib.sha256(json.dumps(first.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    second_hash = hashlib.sha256(json.dumps(second.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    assert first.accepted and first_hash == second_hash

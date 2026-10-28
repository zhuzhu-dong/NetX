"""Regression for fractional stable-capacity evidence sent to Mininet."""
from models.resources import StableDataplaneCapacityRecord
from api.qos import _shared_ingress_scheduler_contract, _stable_capacity_payload


def test_qos_payload_preserves_fractional_evidence_equation():
    record = StableDataplaneCapacityRecord(
        capacity_record_id="CAP-TEST", topology_id="research_dc", environment_epoch="ENV-TEST",
        controller_epoch="ONOS", path_signature="PATH", bottleneck_egress="link:test",
        packet_profile="udp", protocol="UDP", packet_size_bytes=1200,
        observation_mode="LIVE", physical_capacity_mbps=100.0,
        stable_capacity_mbps=58.57601700875149, capacity_floor_mbps=58.57601700875149,
        p05_capacity_mbps=58.6, sample_count=5, confidence="CALIBRATED_LOW",
        measured_at="2026-08-08T00:00:00+00:00", valid_until="2027-08-08T00:00:00+00:00",
        source_experiment="test", safety_margin_mbps=5.0,
    )

    payload = _stable_capacity_payload(record)

    assert payload["schedulable_capacity_mbps"] == 53.57601700875149


def test_shared_ingress_contract_is_used_for_new_rate_limit_resource(monkeypatch):
    """A profile-known, unbound scope must inherit its shared HTB root."""
    import api.qos as qos

    class Binding:
        resource_key = "link:s_access_r--s_agg_r"

    class Desired:
        def list_active(self):
            return [{"traffic_scope_key": "TSK-existing"}]

    class Allocator:
        def __init__(self, _store):
            pass

        def get_active_for_scope(self, scope):
            return Binding() if scope == "TSK-existing" else None

    class Resolution:
        accepted = True
        root_rate_mbps = 49

    monkeypatch.setattr(qos, "QoSResourceAllocator", Allocator)
    monkeypatch.setattr(
        "infra.ingress_admission_state.IngressAdmissionDesiredRepository",
        lambda _store: Desired(),
    )
    monkeypatch.setattr(
        "infra.egress_qos_desired.resolve_egress_qos_desired_for_scope",
        lambda scope, **_kwargs: Resolution(),
    )

    assert _shared_ingress_scheduler_contract("link:s_access_r--s_agg_r") == 49

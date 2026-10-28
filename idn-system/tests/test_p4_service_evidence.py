from __future__ import annotations

import pytest
from types import SimpleNamespace

from api import p4_evidence
from api.p4_evidence import (
    _entry_matches,
    canonical_ip_protocol,
    canonical_pi_value_matches,
    canonical_service_selector,
    read_p4_snapshot,
    resolve_p4runtime_endpoint,
)
from api.qos import p4_counter_evidence
from api.service_profiles import get_service_profile
from api.service_validation import _evidence_rollup, _kpi_evaluation, no_sample_result
pytest.importorskip("p4.config.v1")
from mcp_servers.p4info_registry import p4info_registry
from models.yang_policy import ActionRules, ActionType, MatchCriteria, NetworkPolicy, QoSParams, YANGPolicy


TABLE = "MyIngress.sla_enforcement"
COUNTER = "MyIngress.qos_classifier_counter"


def _entry(*, dscp: int = 46, port: int = 5004):
    values = {
        "hdr.ipv4.srcAddr": bytes.fromhex("c0a80a0a"),
        "hdr.ipv4.dstAddr": bytes.fromhex("0a00000a"),
        "hdr.ipv4.protocol": bytes.fromhex("11"),
        "hdr.udp.dstPort": int(port).to_bytes(2, "big"),
    }
    class Match:
        def __init__(self, field_id, value):
            self.field_id = field_id
            self.ternary = SimpleNamespace(value=value, mask=b"\xff" * len(value))

        @staticmethod
        def WhichOneof(_name):
            return "ternary"

    matches = [Match(p4info_registry.field_id(TABLE, name), value) for name, value in values.items()]
    param = SimpleNamespace(
        param_id=p4info_registry.param_id("MyIngress.set_dscp", "dscp_value"),
        value=int(dscp).to_bytes(1, "big"),
    )
    action = SimpleNamespace(
        action_id=p4info_registry.action_id("MyIngress.set_dscp"), params=[param],
    )
    return SimpleNamespace(
        table_id=p4info_registry.table_id(TABLE), priority=100, match=matches,
        action=SimpleNamespace(action=action),
    )


def _counter(entry, packets=15, byte_count=1500):
    return SimpleNamespace(
        table_entry=entry,
        data=SimpleNamespace(packet_count=packets, byte_count=byte_count),
    )


def _counter_without_action(entry, packets=15, byte_count=1500):
    key = SimpleNamespace(
        table_id=entry.table_id,
        priority=entry.priority,
        match=entry.match,
        action=SimpleNamespace(action=SimpleNamespace(action_id=0, params=[])),
    )
    return _counter(key, packets, byte_count)


def _policy(bandwidth=10):
    return YANGPolicy(
        service_id="research_video",
        network_policy=NetworkPolicy(
            policy_name="research-video-test",
            match=MatchCriteria(
                source_ip="192.168.10.10/32",
                destination_ip="10.0.0.10/32",
                protocol="UDP",
                destination_port=5004,
            ),
            action=ActionRules(
                action_type=ActionType.SET_PRIORITY,
                qos_params=QoSParams(bandwidth=bandwidth, dscp_value=46, burst_size=1024),
            ),
        ),
    )


@pytest.mark.parametrize("value", ["UDP", "17", 17, "0x11"])
def test_udp_protocol_canonical_equivalence(value):
    assert canonical_ip_protocol(value) == 17


def test_onos_protocol_decimal_and_hex_byte_are_equivalent():
    assert canonical_pi_value_matches("hdr.ipv4.protocol", "17", "11")
    assert canonical_pi_value_matches("hdr.ipv4.protocol", "0x11", "11")
    assert canonical_pi_value_matches("hdr.ipv4.protocol", "11", "11")


def test_current_p4info_ids_and_direct_binding_are_dynamic():
    assert p4info_registry.table_id(TABLE) == 38513872
    assert p4info_registry.direct_counter_id(COUNTER) == 320825904
    assert p4info_registry.direct_counter_table(COUNTER) == TABLE


def test_research_selector_is_exact_udp_5004_dscp_46():
    selector = canonical_service_selector(get_service_profile("research_video"))
    assert selector == {
        "src": "192.168.10.10/32", "dst": "10.0.0.10/32",
        "protocol": 17, "dst_port": 5004, "dscp": 46,
    }


def test_p4_table_entry_matches_selector_and_action_dscp():
    profile = get_service_profile("research_video")
    assert _entry_matches(_entry(), TABLE, canonical_service_selector(profile))
    assert not _entry_matches(_entry(dscp=10), TABLE, canonical_service_selector(profile))
    assert not _entry_matches(_entry(port=5005), TABLE, canonical_service_selector(profile))


@pytest.mark.asyncio
async def test_device_bmv2_5_maps_to_s_access_r_port_50055(monkeypatch):
    class Probe:
        async def get(self, _path):
            return {"switches": [{"name": "s_access_r", "device_id": "device:bmv2:5", "grpc_port": 50055}]}

    async def session():
        return Probe()

    import api._probe_session
    monkeypatch.setattr(api._probe_session, "get_probe_session", session)
    endpoint = await resolve_p4runtime_endpoint(get_service_profile("research_video"))
    assert endpoint["switch"] == "s_access_r"
    assert endpoint["grpc_port"] == 50055
    assert endpoint["p4runtime_node_id"] == 1


async def _fixed_endpoint(_profile):
    return {
        "device_id": "device:bmv2:5", "logical_device_number": 5,
        "switch": "s_access_r", "grpc_host": "mininet", "grpc_port": 50055,
        "grpc_endpoint": "mininet:50055", "p4runtime_node_id": 1,
    }


@pytest.mark.asyncio
async def test_exact_table_and_direct_counter_read_is_verified(monkeypatch):
    entry = _entry()
    monkeypatch.setattr(p4_evidence, "resolve_p4runtime_endpoint", _fixed_endpoint)
    monkeypatch.setattr(p4_evidence, "_read_entities_sync", lambda *_: ([entry], [_counter_without_action(entry)]))
    result = await read_p4_snapshot(get_service_profile("research_video"))
    assert result["status"] == "VERIFIED"
    assert result["matching_entry_count"] == 1
    assert result["packets"] == 15
    assert result["bytes"] == 1500
    assert result["read_request"]["device_id"] == 1


@pytest.mark.asyncio
async def test_no_table_entry_returns_no_matching_entry(monkeypatch):
    monkeypatch.setattr(p4_evidence, "resolve_p4runtime_endpoint", _fixed_endpoint)
    monkeypatch.setattr(p4_evidence, "_read_entities_sync", lambda *_: ([], []))
    result = await read_p4_snapshot(get_service_profile("research_video"))
    assert result["status"] == "NO_MATCHING_ENTRY"
    assert result["reason_code"] == "NO_MATCHING_ENTRY"


@pytest.mark.asyncio
async def test_selector_mismatch_is_not_reported_as_read_failed(monkeypatch):
    entry = _entry(port=5005)
    monkeypatch.setattr(p4_evidence, "resolve_p4runtime_endpoint", _fixed_endpoint)
    monkeypatch.setattr(p4_evidence, "_read_entities_sync", lambda *_: ([entry], [_counter(entry)]))
    result = await read_p4_snapshot(get_service_profile("research_video"))
    assert result["status"] == "SELECTOR_MISMATCH"


@pytest.mark.asyncio
async def test_counter_not_bound_is_explicit(monkeypatch):
    monkeypatch.setattr(p4_evidence, "resolve_p4runtime_endpoint", _fixed_endpoint)
    monkeypatch.setattr(p4info_registry, "direct_counter_id", lambda _name: None)
    result = await read_p4_snapshot(get_service_profile("research_video"))
    assert result["status"] == "COUNTER_NOT_BOUND"
    assert result["counter_name"] == COUNTER


@pytest.mark.asyncio
async def test_read_exception_is_structured(monkeypatch):
    monkeypatch.setattr(p4_evidence, "resolve_p4runtime_endpoint", _fixed_endpoint)
    def fail(*_args):
        raise RuntimeError("boom")
    monkeypatch.setattr(p4_evidence, "_read_entities_sync", fail)
    result = await read_p4_snapshot(get_service_profile("research_video"))
    assert result["status"] == "READ_FAILED"
    assert result["reason_code"] == "READ_EXCEPTION"
    assert result["grpc_endpoint"] == "mininet:50055"
    assert result["exception"]["type"] == "RuntimeError"
    assert result["read_request"]["selector"]["protocol"] == 17


def test_direct_counter_before_after_delta_is_exact():
    result = p4_counter_evidence(
        {"status": "VERIFIED", "packets": 10, "bytes": 1000},
        {"status": "VERIFIED", "matching_entry_count": 1, "packets": 25, "bytes": 2800},
    )
    assert result["status"] == "VERIFIED"
    assert result["delta_packets"] == 15
    assert result["delta_bytes"] == 1800


def test_measurement_evidence_and_kpi_states_are_separate():
    sample = no_sample_result("research_video")
    assert sample["measurement_status"] == "NO_SAMPLE"
    assert sample["evidence_status"] == "UNAVAILABLE"
    assert sample["kpi_status"] == "NOT_EVALUATED"
    assert _evidence_rollup("READ_FAILED", "MEASURED", p4_required=True, tc_required=True) == "PARTIAL"


def test_bandwidth_pass_with_high_udp_loss_is_warning_not_sla_failure():
    result = _kpi_evaluation(
        get_service_profile("research_video"),
        _policy(10),
        {"protocol_metrics": {"received_mbps": 22.508, "loss_percent": 23.646, "jitter_ms": 0.942}},
    )
    assert result["primary_sla"]["status"] == "PASS"
    assert result["quality"]["loss"]["status"] == "WARN"
    assert result["quality"]["jitter"]["status"] == "PASS"
    assert result["overall"] == "VERIFIED_WITH_WARNING"
    assert result["kpi_status"] == "DEGRADED"

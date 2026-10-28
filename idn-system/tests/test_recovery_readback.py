from __future__ import annotations

from types import SimpleNamespace

import pytest

from infra.recovery_readback import (
    EgressQosRecoveryReadbackService,
    P4RecoveryReadbackService,
    RestartRecoveryReadbackBundleService,
    checksum,
    normalized_classid,
    normalized_mbps,
)


PROFILE = SimpleNamespace(
    src_ip="192.168.10.10/32", dst_ip="10.0.0.10/32", protocol="UDP", dst_port=5004, dscp=46,
    p4_counter_binding={"device_id": "device:bmv2:5", "table": "MyIngress.sla_enforcement", "counter": "MyIngress.qos_classifier_counter"},
)
DESIRED = {"generation_id": "IADS-test", "admission_plan_id": "INGRESS-WC-20260805-03", "intent_id": "INT-test", "desired_policy_id": "POL-desired", "executed_resource_owner_policy_id": "POL-owner", "resource_binding_id": "RB-test", "traffic_scope_key": "TSK-test", "desired": {"guaranteed_floor_mbps": 30, "schedulable_capacity_mbps": 49, "protected_selector": {}}}
BINDING = SimpleNamespace(traffic_scope_key="TSK-test", policy_id="POL-owner", rate_mbps=30, ceil_mbps=49, dscp=46, class_id="1:678b", filter_id=123, device_id="device:bmv2:5", interface_id="s_access_r-eth3", runtime={"qos_binding": {"device_id": "device:bmv2:5", "interface_name": "s_access_r-eth3"}})
RESOLUTION = SimpleNamespace(
    selector={"source_prefix": "192.168.10.10/32", "destination_prefix": "10.0.0.10/32", "ip_protocol": 17, "destination_port": 5004, "dscp": 46},
    root_rate_mbps=49, root_ceil_mbps=49, target_rate_mbps=30, target_ceil_mbps=49,
    default_rate_mbps=1, default_ceil_mbps=19, egress_device="device:bmv2:5", egress_interface="s_access_r-eth3",
)


class Registry:
    def table_names(self): return ["MyIngress.sla_enforcement"]
    def action_names(self): return ["MyIngress.set_dscp"]
    def table_id(self, name): return 38513872
    def action_id(self, name): return 16777233
    def direct_counter_id(self, name): return 33554444
    def param_id(self, action, param): return 1


async def _p4_raw(status="VERIFIED", *, dscp="2e", action="MyIngress.set_dscp"):
    return {"status": status, "device_id": "device:bmv2:5", "grpc_endpoint": "mininet:50055", "packets": 0, "bytes": 0, "read_at": "2026-08-06T00:00:00+00:00", "read_response": {"matching_table_entries": [{"action_name": action, "action_id": 16777233, "action_params": [{"param_id": 1, "value": dscp}]}]}}


@pytest.mark.asyncio
async def test_p4_full_entry_with_zero_counter_is_verified():
    service = P4RecoveryReadbackService(reader=lambda profile: _p4_raw(), registry=Registry())
    value = await service.collect(recovery_id="RRC-test", verification_phase="FINAL_READBACK", execution_generation="IADS-test", runtime_instance_epoch="RTE-test", profile=PROFILE)
    assert value["status"] == "P4_RECOVERY_READBACK_VERIFIED"
    assert value["counter_packets"] == 0 and value["counter_bytes"] == 0
    assert value["checksum"] == checksum(value)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), [("NO_MATCHING_ENTRY", "P4_ENTRY_NOT_FOUND"), ("SELECTOR_MISMATCH", "P4_SELECTOR_MISMATCH"), ("COUNTER_NOT_BOUND", "P4_DIRECT_COUNTER_UNREADABLE"), ("READ_FAILED", "P4RUNTIME_UNAVAILABLE")])
async def test_p4_readback_classifies_real_reader_failures(status, expected):
    service = P4RecoveryReadbackService(reader=lambda profile: _p4_raw(status), registry=Registry())
    value = await service.collect(recovery_id="RRC-test", verification_phase="FINAL_READBACK", execution_generation="IADS-test", runtime_instance_epoch="RTE-test", profile=PROFILE)
    assert value["status"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("dscp", "action", "expected"), [("2d", "MyIngress.set_dscp", "P4_DSCP_MISMATCH"), ("2e", "MyIngress.noop", "P4_ACTION_MISMATCH")])
async def test_p4_action_and_dscp_mismatch_are_not_verified(dscp, action, expected):
    service = P4RecoveryReadbackService(reader=lambda profile: _p4_raw(dscp=dscp, action=action), registry=Registry())
    value = await service.collect(recovery_id="RRC-test", verification_phase="FINAL_READBACK", execution_generation="IADS-test", runtime_instance_epoch="RTE-test", profile=PROFILE)
    assert value["status"] == expected


@pytest.mark.asyncio
async def test_egress_normalizes_and_verifies_complete_tc_readback(monkeypatch):
    class Probe:
        async def get(self, path, params):
            assert path == "/policy/qos-readback" and params["scope_key"] == "TSK-test"
            return {"status": "ok", "config_applied": True, "root_qdisc_present": True, "root_rate_mbps": "49000Kbit", "root_ceil_mbps": "49Mbit", "class_id": "1:678b", "default_class_id": "1:fffe", "rate_mbps": "30Mbit", "ceil_mbps": "49000Kbit", "default_rate_mbps": "1000Kbit", "default_ceil_mbps": "19Mbit", "filter_kind": "flower_dscp_plus_five_tuple", "filter_priority": 123, "filter_id": 123, "classification_key": {"source_ip": "192.168.10.10/32", "destination_ip": "10.0.0.10/32", "protocol": "udp", "destination_port": 5004, "dscp": 46}, "raw": {"qdisc": "qdisc htb 1: root", "class": "class htb 1:678b", "filter": "filter flower"}}
    async def probe(): return Probe()
    monkeypatch.setattr("api._probe_session.get_probe_session", probe)
    value = await EgressQosRecoveryReadbackService().collect(recovery_id="RRC-test", verification_phase="FINAL_READBACK", desired=DESIRED, runtime_instance_epoch="RTE-test", binding=BINDING, profile=PROFILE, resolution=RESOLUTION)
    assert value["status"] == "EGRESS_RECOVERY_READBACK_VERIFIED"
    assert value["target_rate_mbps"] == "30Mbit"
    assert value["checksum"] == checksum(value)


@pytest.mark.asyncio
async def test_egress_filter_selector_mismatch_is_rejected(monkeypatch):
    class Probe:
        async def get(self, path, params):
            return {"status": "ok", "config_applied": True, "root_qdisc_present": True, "root_rate_mbps": 49, "root_ceil_mbps": 49, "class_id": "1:678b", "default_class_id": "1:fffe", "rate_mbps": 30, "ceil_mbps": 49, "default_rate_mbps": 1, "default_ceil_mbps": 19, "classification_key": {"source_ip": "192.168.10.20/32", "destination_ip": "10.0.0.10/32", "protocol": "udp", "destination_port": 5004, "dscp": 46}}
    async def probe(): return Probe()
    monkeypatch.setattr("api._probe_session.get_probe_session", probe)
    value = await EgressQosRecoveryReadbackService().collect(recovery_id="RRC-test", verification_phase="FINAL_READBACK", desired=DESIRED, runtime_instance_epoch="RTE-test", binding=BINDING, profile=PROFILE, resolution=RESOLUTION)
    assert value["status"] == "EGRESS_FILTER_MISMATCH"


def _readback(kind, status, *, recovery_id="RRC-test", epoch="RTE-test", generation="IADS-test", scope="TSK-test"):
    value = {"recovery_id": recovery_id, "runtime_instance_epoch": epoch, "execution_generation": generation, "desired_generation": generation, "traffic_scope_key": scope, "status": status}
    value["checksum"] = checksum(value)
    return value


def test_bundle_accepts_all_three_and_rejects_lineage_or_checksum_mismatch():
    p4 = _readback("p4", "P4_RECOVERY_READBACK_VERIFIED")
    ingress = _readback("ingress", "INGRESS_RECOVERY_READBACK_VERIFIED")
    egress = _readback("egress", "EGRESS_RECOVERY_READBACK_VERIFIED")
    service = RestartRecoveryReadbackBundleService()
    bundle = service.build(recovery_id="RRC-test", verification_phase="FINAL_READBACK", runtime_instance_epoch="RTE-test", environment_epoch="ENV-test", desired=DESIRED, p4=p4, ingress=ingress, egress=egress)
    assert bundle["bundle_status"] == "VERIFIED" and bundle["checksum"] == checksum(bundle)
    egress["traffic_scope_key"] = "TSK-other"
    broken = service.build(recovery_id="RRC-test", verification_phase="FINAL_READBACK", runtime_instance_epoch="RTE-test", environment_epoch="ENV-test", desired=DESIRED, p4=p4, ingress=ingress, egress=egress)
    assert broken["bundle_status"] == "INCOMPLETE" and "EGRESS_SCOPE_MISMATCH" in broken["validation_errors"]


def test_rate_and_class_normalization_are_not_string_contains_checks():
    assert normalized_mbps("49000Kbit") == 49
    assert normalized_mbps("1Mbit") == 1
    assert normalized_classid("1:0xfffe") == "1:fffe"

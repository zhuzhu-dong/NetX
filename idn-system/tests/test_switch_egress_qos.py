"""Focused regression tests for the P4-classifier/switch-egress QoS plane."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from agents.status_verifier import StatusVerifierAgent
from api.qos import (
    _expected_classifier,
    build_current_classifier_rule,
    ensure_p4_classifier,
    p4_counter_evidence,
    qos_probe_payload,
    run_qos_demo_validation,
    verify_p4_classifier,
)
from api.decision_report import _execution_plane
from models.policy_update import effective_qos_mode, traffic_scope_key
from models.yang_policy import ActionType, QoSMode


_DEFAULT_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = next(
    (candidate for candidate in (_DEFAULT_ROOT, Path("/workspace")) if (candidate / "mininet").is_dir()),
    _DEFAULT_ROOT,
)
_PROBE_PATH = _PROJECT_ROOT / "mininet" / "probe_server.py"
_SPEC = importlib.util.spec_from_file_location("idn_probe_server_for_tests", _PROBE_PATH)
assert _SPEC and _SPEC.loader
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


def _payload(**overrides):
    value = {
        "scope_key": "TSK-test-video",
        "source_ip": "192.168.10.10/32",
        "destination_ip": "10.0.0.10/32",
        "protocol": "UDP",
        "source_port": None,
        "destination_port": 5004,
        "bandwidth_mbps": 10,
        "burst_kb": 64,
        "qos_mode": "MIN_BANDWIDTH",
        "dscp": 46,
        "priority": 1,
    }
    value.update(overrides)
    return value


def _binding(**overrides):
    value = {
        "device_id": "device:bmv2:5",
        "switch_name": "s_access_r",
        "egress_port": 3,
        "interface_name": "s_access_r-eth3",
        "peer_device": "s_agg_r",
        "peer_device_id": "device:bmv2:3",
        "link_capacity_mbps": 100,
        "classifier_device_id": "device:bmv2:5",
        "source_host": "h_research",
        "destination_host": "h_dc",
        "path_id": "PATH-primary",
        "current_path": ["s_access_r", "s_agg_r", "s_core1", "s_agg_d", "s_access_d"],
    }
    value.update(overrides)
    return value


def _stable_capacity(**overrides):
    value = {"capacity_record_id": "CAP-test", "physical_capacity_mbps": 100,
             "stable_capacity_mbps": 55, "schedulable_capacity_mbps": 55,
             "safety_margin_mbps": 0, "status": "ACTIVE", "confidence": "CALIBRATED_LOW"}
    value.update(overrides)
    return value


@pytest.fixture(autouse=True)
def _reset_runtime_state():
    probe._qos_policies.clear()
    probe._ingress_admission_plans.clear()
    probe._ingress_calibration_windows.clear()
    yield
    probe._qos_policies.clear()
    probe._ingress_admission_plans.clear()
    probe._ingress_calibration_windows.clear()


def test_01_min_bandwidth_payload_is_not_a_source_host_resource(yang_policy_factory):
    policy = yang_policy_factory(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32")
    policy.network_policy.action.qos_params.qos_mode = QoSMode.MIN_BANDWIDTH
    payload = qos_probe_payload(policy)
    assert payload["qos_mode"] == "MIN_BANDWIDTH"
    assert "host" not in payload and "interface" not in payload


def test_tcp_classifier_evidence_uses_tcp_destination_port(yang_policy_factory):
    policy = yang_policy_factory(
        source_ip="192.168.20.10/32", destination_ip="10.0.0.10/32",
        protocol="TCP", destination_port=5202, dscp_value=18,
    )
    expected = _expected_classifier(policy)
    assert expected["hdr.tcp.dstPort"] == {"value": "1452", "mask": "ffff"}
    assert "hdr.udp.dstPort" not in expected


def test_02_qos_binding_names_a_switch_egress():
    binding = _binding()
    assert binding["switch_name"].startswith("s_")
    assert not binding["interface_name"].startswith("h_")
    assert binding["peer_device"] == "s_agg_r"


def test_03_min_bandwidth_rate_and_ceil_commands(monkeypatch):
    commands = []
    monkeypatch.setattr(probe, "_tc_cmd", lambda _node, command: commands.append(command) or "")
    monkeypatch.setattr(probe, "_read_tc_resource", lambda *_: {"config_applied": True})
    net = type("Net", (), {"get": lambda self, name: object()})()
    probe._install_tc_resource(net, _payload(), _binding(), create_filter=True)
    policy_class = next(item for item in commands if "classid 1:" in item and "burst 64kb" in item)
    assert "rate 10mbit" in policy_class and "ceil 100mbit" in policy_class


def test_04_rate_limit_rate_equals_ceil(monkeypatch):
    commands = []
    monkeypatch.setattr(probe, "_tc_cmd", lambda _node, command: commands.append(command) or "")
    monkeypatch.setattr(probe, "_read_tc_resource", lambda *_: {"config_applied": True})
    net = type("Net", (), {"get": lambda self, name: object()})()
    probe._install_tc_resource(net, _payload(qos_mode="RATE_LIMIT"), _binding(), create_filter=True)
    policy_class = next(item for item in commands if "classid 1:" in item and "burst 64kb" in item)
    assert "rate 10mbit" in policy_class and "ceil 10mbit" in policy_class


def test_minimum_bandwidth_uses_stable_root_and_residual_default(monkeypatch):
    commands = []
    monkeypatch.setattr(probe, "_tc_cmd", lambda _node, command: commands.append(command) or "")
    monkeypatch.setattr(probe, "_read_tc_resource", lambda *_: {"config_applied": True})
    net = type("Net", (), {"get": lambda self, name: object()})()
    probe._install_tc_resource(net, _payload(bandwidth_mbps=30, stable_capacity=_stable_capacity()), _binding(), create_filter=False)
    assert any("classid 1:1 htb rate 55mbit ceil 55mbit" in command for command in commands)
    assert any("classid 1:100 htb rate 30mbit ceil 55mbit" in command for command in commands)
    assert any("classid 1:fffe htb rate 1mbit ceil 55mbit" in command for command in commands)


def test_recovery_uses_durable_scheduler_contract_without_runtime_calibration(monkeypatch):
    """Expired runtime evidence may not widen a committed recovery to link capacity."""
    commands = []
    monkeypatch.setattr(probe, "_tc_cmd", lambda _node, command: commands.append(command) or "")
    monkeypatch.setattr(probe, "_read_tc_resource", lambda *_: {"config_applied": True})
    net = type("Net", (), {"get": lambda self, name: object()})()

    probe._install_tc_resource(
        net,
        _payload(
            bandwidth_mbps=30,
            authorized_schedulable_capacity_mbps=49,
            durable_scheduler_contract_mbps=49,
        ),
        _binding(),
        create_filter=False,
    )

    assert any("classid 1:1 htb rate 49mbit ceil 49mbit" in command for command in commands)
    assert any("classid 1:100 htb rate 30mbit ceil 49mbit" in command for command in commands)


def test_durable_contract_survives_qos_http_payload_validation():
    validated = probe._validate_qos_payload(
        _payload(
            authorized_schedulable_capacity_mbps=49,
            durable_scheduler_contract_mbps=49,
        )
    )

    assert validated["authorized_schedulable_capacity_mbps"] == 49
    assert validated["durable_scheduler_contract_mbps"] == 49


def test_05_classifier_filter_contains_dscp_and_flow_disambiguation():
    command = probe._filter_command("s_access_r-eth3", _payload(), "1234", 900)
    for fragment in ("ip_tos 0xb8/0xfc", "src_ip 192.168.10.10/32", "dst_ip 10.0.0.10/32", "ip_proto udp", "dst_port 5004"):
        assert fragment in command


def test_ingress_fallback_uses_verified_residual_for_reverse_tcp_acks(monkeypatch):
    """A protected host's unmatched return traffic must borrow residual capacity.

    The fallback selector is source-wide by design, so TCP ACKs for a reverse
    flow are unmatched.  They must not be limited to the 1-kbit guaranteed
    best-effort rate when the admission plan has a verified 19-Mbps residual.
    """
    commands = []

    class Net:
        def get(self, _name):
            return object()

    def fake_tc(_node, command):
        commands.append(command)
        if "qdisc show" in command:
            return "qdisc htb 5: root"
        if "class show" in command:
            return "class htb 5:1 root\nclass htb 5:10 parent 5:1\nclass htb 5:fffe parent 5:1"
        if "filter show" in command:
            return ""
        return ""

    monkeypatch.setattr(probe, "_attachment_for_source", lambda _net, _source: {
        "host": "h_source", "interface": "h_source-eth0",
    })
    monkeypatch.setattr(probe, "_tc_cmd", fake_tc)
    plan = {
        "admission_plan_id": "AP-ACK-RESIDUAL",
        "mode": "OVERSUBSCRIBED_ADMISSION",
        "schedulable_capacity_mbps": 49,
        "implementation_reserved_mbps": 30,
        "best_effort_residual_mbps": 19,
        "expected_aggregate_ceiling_mbps": 49,
        "protected_scopes": [{
            "source_ip": "192.0.2.10/32", "destination_ip": "198.51.100.10/32",
            "protocol": "UDP", "destination_port": 5004,
            "implementation_guaranteed_floor_mbps": 30,
            "ingress_admission_ceiling_mbps": 30,
        }],
        "best_effort_scopes": [],
        "inactive_best_effort_scopes": [],
    }

    result = probe._apply_ingress_admission(Net(), plan)

    assert result["attachments"][0]["best_effort_ceil_mbps"] == 19
    assert any(
        "classid 5:fffe htb rate 1mbit ceil 19mbit" in command
        for command in commands
    )


def test_06_dscp_mark_preserves_ecn_in_p4_source():
    source = (_PROJECT_ROOT / "p4" / "idn_basic.p4").read_text(encoding="utf-8")
    assert "hdr.ipv4.diffserv & 0x03" in source
    assert "direct_counter(CounterType.packets_and_bytes) qos_classifier_counter" in source


def test_07_legacy_verify_is_disabled_without_install(monkeypatch):
    probe._qos_policies["TSK-test-video"] = {**_payload(), "qos_binding": _binding()}
    monkeypatch.setattr(probe, "_read_tc_resource", lambda *_: {"config_applied": True, "rate_mbps": 10, "ceil_mbps": 100})
    # VERIFY is retained only as a compatibility symbol and cannot inspect or
    # reconcile a TC tree.  GET /policy/qos-readback is the read path.
    monkeypatch.setattr(probe, "_select_qos_binding", lambda *_: _binding())
    result = probe._verify_tc_qos(object(), _payload())
    assert result["reason_code"] == "LEGACY_QOS_VERIFY_DISABLED"
    assert result["write_executed"] is False


def test_minimum_bandwidth_readback_accepts_a_stronger_existing_guarantee(monkeypatch):
    def fake_class_values(_output, class_id):
        values = {
            "100": {"present": True, "rate_mbps": 50, "ceil_mbps": 100},
            "1": {"present": True, "rate_mbps": 100, "ceil_mbps": 100},
            "fffe": {"present": True, "rate_mbps": 1, "ceil_mbps": 100},
        }
        return values[class_id]

    monkeypatch.setattr(probe, "_tc_class_values", fake_class_values)
    monkeypatch.setattr(
        probe,
        "_tc_cmd",
        lambda _node, command: (
            "qdisc htb 1: root default 0xfffe" if "qdisc" in command
            else "filter flower classid 1:100" if "filter" in command
            else ""
        ),
    )
    net = type("Net", (), {"get": lambda self, name: object()})()

    readback = probe._read_tc_resource(net, _payload(bandwidth_mbps=30), _binding())

    assert readback["rate_mbps"] == 50
    assert readback["rate_requirement_satisfied"] is True
    assert readback["config_applied"] is True


def test_rate_limit_readback_still_requires_the_exact_cap(monkeypatch):
    def fake_class_values(_output, class_id):
        values = {
            "100": {"present": True, "rate_mbps": 50, "ceil_mbps": 50},
            "1": {"present": True, "rate_mbps": 100, "ceil_mbps": 100},
            "fffe": {"present": True, "rate_mbps": 1, "ceil_mbps": 100},
        }
        return values[class_id]

    monkeypatch.setattr(probe, "_tc_class_values", fake_class_values)
    monkeypatch.setattr(
        probe,
        "_tc_cmd",
        lambda _node, command: (
            "qdisc htb 1: root default 0xfffe" if "qdisc" in command
            else "filter flower classid 1:100" if "filter" in command
            else ""
        ),
    )
    net = type("Net", (), {"get": lambda self, name: object()})()

    readback = probe._read_tc_resource(
        net, _payload(bandwidth_mbps=30, qos_mode="RATE_LIMIT"), _binding()
    )

    assert readback["rate_requirement_satisfied"] is False
    assert readback["config_applied"] is False


def test_08_upgrade_reconciles_class_and_missing_filter_idempotently(monkeypatch):
    probe._qos_policies["TSK-test-video"] = {**_payload(), "qos_binding": _binding()}
    seen = []
    monkeypatch.setattr(probe, "_select_qos_binding", lambda *_: _binding())
    monkeypatch.setattr(probe, "_capacity_check", lambda *_: {"passed": True})
    monkeypatch.setattr(probe, "_install_tc_resource", lambda _n, p, _b, create_filter: seen.append((p["bandwidth_mbps"], create_filter)) or {"config_applied": True, "rate_mbps": 30, "ceil_mbps": 100})
    result = probe._apply_tc_qos(object(), _payload(bandwidth_mbps=30))
    # create_filter=True is safe because the probe uses an idempotent
    # "replace" operation; it also repairs an externally deleted filter.
    assert seen == [(30, True)] and result["rate_mbps"] == 30


def test_09_downgrade_releases_reserved_capacity(monkeypatch):
    probe._qos_policies["TSK-test-video"] = {**_payload(bandwidth_mbps=30), "qos_binding": _binding()}
    monkeypatch.setattr(probe, "_select_qos_binding", lambda *_: _binding())
    check = probe._capacity_check(_payload(bandwidth_mbps=10), _binding())
    assert check["available_mbps"] == 99 and check["requested_mbps"] == 10


def test_10_capacity_overcommit_is_rejected(monkeypatch):
    probe._qos_policies["other"] = {**_payload(scope_key="other", bandwidth_mbps=90), "qos_binding": _binding()}
    monkeypatch.setattr(probe, "_select_qos_binding", lambda *_: _binding())
    result = probe._apply_tc_qos(object(), _payload(bandwidth_mbps=10))
    assert result["code"] == "INSUFFICIENT_CAPACITY" and result["config_applied"] is False


def test_11_path_migration_installs_new_before_removing_old(monkeypatch):
    old = _binding()
    new = _binding(device_id="device:bmv2:3", switch_name="s_agg_r", egress_port=4, interface_name="s_agg_r-eth4")
    probe._qos_policies["TSK-test-video"] = {**_payload(), "qos_binding": old}
    order = []
    monkeypatch.setattr(probe, "_select_qos_binding", lambda *_: new)
    monkeypatch.setattr(probe, "_capacity_check", lambda *_: {"passed": True})
    monkeypatch.setattr(probe, "_install_tc_resource", lambda *_args, **_kwargs: order.append("install-new") or {"config_applied": True, "rate_mbps": 10, "ceil_mbps": 100})
    monkeypatch.setattr(probe, "_remove_tc_resource", lambda *_: order.append("remove-old") or {"class_removed": True, "filter_removed": True})
    result = probe._apply_tc_qos(object(), _payload())
    assert order == ["install-new", "remove-old"] and result["migrated"] is True


def test_12_failed_new_egress_preserves_old_runtime(monkeypatch):
    old_record = {**_payload(), "qos_binding": _binding()}
    probe._qos_policies["TSK-test-video"] = old_record
    new = _binding(device_id="device:bmv2:3", switch_name="s_agg_r", interface_name="s_agg_r-eth4")
    monkeypatch.setattr(probe, "_select_qos_binding", lambda *_: new)
    monkeypatch.setattr(probe, "_capacity_check", lambda *_: {"passed": True})
    monkeypatch.setattr(probe, "_install_tc_resource", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("new readback failed")))
    monkeypatch.setattr(probe, "_remove_tc_resource", lambda *_: {"class_removed": True, "filter_removed": True})
    with pytest.raises(RuntimeError, match="new readback failed"):
        probe._apply_tc_qos(object(), _payload())
    assert probe._qos_policies["TSK-test-video"] is old_record


def test_13_same_dscp_uses_distinct_stable_class_and_filter_ids():
    first = probe._tc_identity("TSK-flow-one")
    second = probe._tc_identity("TSK-flow-two")
    assert first != second and first == probe._tc_identity("TSK-flow-one")
    assert probe._tc_identity({"class_id": "1:100", "filter_id": 100}) == ("100", 100)
    assert probe._tc_identity({"class_id": "0x101", "filter_id": 101}) == ("101", 101)


def test_14_default_class_is_created(monkeypatch):
    commands = []
    monkeypatch.setattr(probe, "_tc_cmd", lambda _node, command: commands.append(command) or "")
    monkeypatch.setattr(probe, "_read_tc_resource", lambda *_: {"config_applied": True})
    net = type("Net", (), {"get": lambda self, name: object()})()
    probe._install_tc_resource(net, _payload(), _binding(), create_filter=True)
    assert any("default fffe" in item for item in commands)
    assert any("classid 1:fffe" in item and "prio 7" in item for item in commands)


def test_15_delete_one_policy_preserves_other_class(monkeypatch):
    probe._qos_policies.update({
        "TSK-test-video": {**_payload(), "qos_binding": _binding()},
        "other": {**_payload(scope_key="other"), "qos_binding": _binding()},
    })
    monkeypatch.setattr(probe, "_remove_tc_resource", lambda *_: {"class_removed": True, "filter_removed": True})
    result = probe._clear_tc_qos(object(), "TSK-test-video", "192.168.10.10/32")
    assert result["resources_released"] is True and "other" in probe._qos_policies


def test_15b_legacy_verify_rejects_failed_path_without_tc_mutation(monkeypatch):
    old = _binding(interface_name="s_agg_r-eth3", egress_port=3, link_id="s_agg_r-s_core1")
    alternate = _binding(
        interface_name="s_agg_r-eth4", egress_port=4,
        peer_device="s_core2", link_id="s_agg_r-s_core2",
    )
    payload = _payload(scope_key="path-drift", protocol="TCP", destination_port=5202, dscp=18)
    probe._qos_policies["path-drift"] = {**payload, "qos_binding": old}
    monkeypatch.setattr(probe, "_select_qos_binding", lambda *_: alternate)
    monkeypatch.setattr(probe, "_read_tc_resource", lambda *_: {
        "config_applied": True, "rate_mbps": 20, "ceil_mbps": 1000,
    })
    result = probe._verify_tc_qos(object(), payload)
    assert result["reason_code"] == "LEGACY_QOS_VERIFY_DISABLED"
    assert result["write_executed"] is False


def test_15c_legacy_verify_rejects_viable_path_without_tc_mutation(monkeypatch):
    alternate = _binding(
        interface_name="s_agg_r-eth4", egress_port=4,
        peer_device="s_core2", link_id="s_agg_r-s_core2",
        current_path=["s_agg_r", "s_core2", "s_agg_d", "s_access_d"],
    )
    primary = _binding(
        interface_name="s_agg_r-eth3", egress_port=3,
        peer_device="s_core1", link_id="s_agg_r-s_core1",
    )
    payload = _payload(scope_key="sticky-path", protocol="TCP", destination_port=5202, dscp=18)
    probe._qos_policies["sticky-path"] = {**payload, "qos_binding": alternate}
    monkeypatch.setattr(probe, "_select_qos_binding", lambda *_: primary)
    monkeypatch.setattr(probe, "_binding_path_is_active", lambda *_: True)
    monkeypatch.setattr(probe, "_read_tc_resource", lambda *_: {
        "config_applied": True, "rate_mbps": 20, "ceil_mbps": 1000,
    })
    result = probe._verify_tc_qos(object(), payload)
    assert result["reason_code"] == "LEGACY_QOS_VERIFY_DISABLED"
    assert result["write_executed"] is False


def _onos_flow(policy, *, packets=1, state="ADDED", protocol_value="11"):
    expected = qos_probe_payload(policy)
    return {
        "id": "101",
        "deviceId": "device:bmv2:5",
        "state": state,
        "packets": packets,
        "bytes": packets * 100,
        "selector": {"criteria": [{"matches": [
            {"field": "hdr.ipv4.srcAddr", "match": "ternary", "value": "c0a80a0a", "mask": "ffffffff"},
            {"field": "hdr.ipv4.dstAddr", "match": "ternary", "value": "0a00000a", "mask": "ffffffff"},
            {"field": "hdr.ipv4.protocol", "match": "ternary", "value": protocol_value, "mask": "ff"},
            {"field": "hdr.udp.dstPort", "match": "ternary", "value": "138c", "mask": "ffff"},
        ]}]},
        "treatment": {"instructions": [{
            "subtype": "ACTION", "actionId": "MyIngress.set_dscp",
            "actionParams": {"dscp_value": format(expected["dscp"], "02x")},
        }]},
    }


async def _verify_with_fake_onos(monkeypatch, policy, flow):
    class FakeClient:
        closed = False
        async def get_flows(self, device_id):
            # Most checks need a single inventory entry, while ownership
            # checks deliberately exercise an unsafe duplicate inventory.
            return {"flows": flow if isinstance(flow, list) else [flow]}
        async def close(self):
            self.closed = True

    import mcp_servers.onos_server
    monkeypatch.setattr(mcp_servers.onos_server, "ONOSClient", FakeClient)
    return await verify_p4_classifier(policy, {
        "installed_flows": [{"device_id": "device:bmv2:5", "flow_id": "101"}],
        "qos_enforcement": {"qos_binding": _binding()},
    })


@pytest.mark.asyncio
async def test_16_onos_exact_flow_readback_succeeds(monkeypatch, yang_policy_factory):
    policy = yang_policy_factory(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32")
    result = await _verify_with_fake_onos(monkeypatch, policy, _onos_flow(policy))
    assert result["verification_state"] == "P4_CLASSIFIER_VERIFIED"


@pytest.mark.asyncio
async def test_16a_duplicate_exact_controller_classifier_is_reported(monkeypatch, yang_policy_factory):
    """A stale equal-selector FlowRule is ownership debt, never safe reuse."""
    policy = yang_policy_factory(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32")
    current = _onos_flow(policy)
    stale = {**_onos_flow(policy), "id": "stale-101", "priority": 91}
    result = await _verify_with_fake_onos(monkeypatch, policy, [current, stale])
    assert result["verification_state"] == "P4_CLASSIFIER_VERIFIED"
    assert result["matching_entry_count"] == 2
    assert result["duplicate_exact_selector"] is True
    assert set(result["duplicate_flow_ids"]) == {"101", "stale-101"}


@pytest.mark.asyncio
async def test_16a_profile_binding_recovers_missing_runtime_device(monkeypatch, yang_policy_factory):
    policy = yang_policy_factory(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32")
    policy.service_profile_snapshot = {"p4_counter_binding": {"device_id": "device:bmv2:5"}}

    class FakeClient:
        async def get_flows(self, device_id):
            assert device_id == "device:bmv2:5"
            return {"flows": [_onos_flow(policy)]}

        async def close(self):
            pass

    import mcp_servers.onos_server
    monkeypatch.setattr(mcp_servers.onos_server, "ONOSClient", FakeClient)
    result = await verify_p4_classifier(policy, {})
    assert result["device_id"] == "device:bmv2:5"
    assert result["verification_state"] == "P4_CLASSIFIER_VERIFIED"


@pytest.mark.asyncio
async def test_16b_onos_decimal_protocol_readback_is_equivalent(monkeypatch, yang_policy_factory):
    policy = yang_policy_factory(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32")
    result = await _verify_with_fake_onos(
        monkeypatch, policy, _onos_flow(policy, protocol_value="17")
    )
    assert result["verification_state"] == "P4_CLASSIFIER_VERIFIED"


@pytest.mark.asyncio
async def test_17_zero_counter_is_not_reported_as_hit(monkeypatch, yang_policy_factory):
    policy = yang_policy_factory(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32")
    result = await _verify_with_fake_onos(monkeypatch, policy, _onos_flow(policy, packets=0))
    assert result["config_applied"] is True and result["counter_hit"] is False


@pytest.mark.asyncio
async def test_18_non_added_onos_flow_fails_readback(monkeypatch, yang_policy_factory):
    policy = yang_policy_factory(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32")
    with pytest.raises(RuntimeError, match="readback mismatch"):
        await _verify_with_fake_onos(monkeypatch, policy, _onos_flow(policy, state="FAILED"))


@pytest.mark.asyncio
async def test_18b_pending_onos_flow_is_polled_until_added(monkeypatch, yang_policy_factory):
    policy = yang_policy_factory(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32")
    replies = iter([
        {"flows": [_onos_flow(policy, state="PENDING_ADD")]},
        {"flows": [_onos_flow(policy, state="ADDED")]},
    ])

    class FakeClient:
        async def get_flows(self, device_id):
            return next(replies)
        async def close(self):
            pass

    import mcp_servers.onos_server
    monkeypatch.setattr(mcp_servers.onos_server, "ONOSClient", FakeClient)
    monkeypatch.setenv("ONOS_FLOW_VERIFY_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("ONOS_FLOW_VERIFY_INTERVAL_SECONDS", "0.001")
    result = await verify_p4_classifier(policy, {
        "installed_flows": [{"device_id": "device:bmv2:5", "flow_id": "101"}],
        "qos_enforcement": {"qos_binding": _binding()},
    })
    assert result["flow_state"] == "ADDED"


@pytest.mark.asyncio
async def test_18c_post_traffic_readback_polls_until_counter_hit(monkeypatch, yang_policy_factory):
    policy = yang_policy_factory(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32")
    replies = iter([
        {"flows": [_onos_flow(policy, packets=0)]},
        {"flows": [_onos_flow(policy, packets=7)]},
    ])

    class FakeClient:
        async def get_flows(self, device_id):
            return next(replies)
        async def close(self):
            pass

    import mcp_servers.onos_server
    monkeypatch.setattr(mcp_servers.onos_server, "ONOSClient", FakeClient)
    monkeypatch.setenv("ONOS_COUNTER_VERIFY_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("ONOS_FLOW_VERIFY_INTERVAL_SECONDS", "0.001")
    result = await verify_p4_classifier(
        policy,
        {
            "installed_flows": [{"device_id": "device:bmv2:5", "flow_id": "101"}],
            "qos_enforcement": {"qos_binding": _binding()},
        },
        require_counter_hit=True,
    )
    assert result["counter_hit"] is True
    assert result["packets"] == 7


def test_18d_current_classifier_rule_uses_tcp_field_not_persisted_udp(yang_policy_factory):
    policy = yang_policy_factory(
        source_ip="192.168.20.10/32", destination_ip="10.0.0.10/32",
        protocol="TCP", destination_port=5202,
    )
    rule = build_current_classifier_rule(
        policy, "device:bmv2:6", template={"priority": 777},
    )
    matches = {
        item["field"]: item
        for criterion in rule["selector"]["criteria"]
        for item in criterion["matches"]
    }
    assert rule["priority"] == 777
    assert matches["hdr.ipv4.protocol"]["value"] == "06"
    assert matches["hdr.tcp.dstPort"]["value"] == "1452"
    assert "hdr.udp.dstPort" not in matches


@pytest.mark.asyncio
async def test_18e_classifier_repair_rebuilds_obsolete_persisted_rule(
    monkeypatch, yang_policy_factory,
):
    # This is an isolated compiler/delivery-adapter test.  Production restart
    # recovery has a real fenced lease and is covered by fencing regressions.
    monkeypatch.setenv("IDN_NETWORK_WRITE_FENCING_REQUIRED", "false")
    policy = yang_policy_factory(
        source_ip="192.168.20.10/32", destination_ip="10.0.0.10/32",
        protocol="TCP", destination_port=5202,
    )
    verification_calls = 0

    async def verify(_policy, runtime):
        nonlocal verification_calls
        verification_calls += 1
        if verification_calls == 1:
            raise RuntimeError("old schema")
        return {"status": "ok", "verification_state": "P4_CLASSIFIER_VERIFIED",
                "config_applied": True, "flow_id": "202"}

    posted = []

    class Delivery:
        async def post_flows(self, device_id, rules):
            posted.extend(rules)
            return {"status": "SUCCESS", "flow_ids": ["202"], "errors": None}

    monkeypatch.setattr("api.qos.verify_p4_classifier", verify)
    monkeypatch.setattr("mcp_servers.flow_delivery.FlowDeliveryFactory.create", lambda: Delivery())
    old_rule = {
        "priority": 777,
        "selector": {"criteria": [{"matches": [
            {"field": "hdr.udp.dstPort", "match": "ternary", "value": "1452", "mask": "ffff"}
        ]}]},
    }
    verified, runtime, reconciliation = await ensure_p4_classifier(policy, {
        "installed_flows": [{"device_id": "device:bmv2:6", "flow_id": "101"}],
        "delivered_rules": {"device:bmv2:6": [old_rule]},
        "qos_enforcement": {"qos_binding": {"classifier_device_id": "device:bmv2:6"}},
    })
    fields = {
        item["field"] for criterion in posted[0]["selector"]["criteria"]
        for item in criterion["matches"]
    }
    assert "hdr.tcp.dstPort" in fields and "hdr.udp.dstPort" not in fields
    assert runtime["installed_flows"] == [{"device_id": "device:bmv2:6", "flow_id": "202"}]
    assert runtime["classifier_schema_rebuilt"] is True
    assert reconciliation["status"] == "RESTORED"
    assert verified["flow_id"] == "202"


def test_19_uncongested_min_bandwidth_may_exceed_guarantee(yang_policy_factory):
    policy = yang_policy_factory(bandwidth=10)
    constraints = StatusVerifierAgent()._extract_constraints(policy)
    from models.telemetry import KPIMetrics
    assert constraints.is_satisfied_by(KPIMetrics(throughput_mbps=80))[0] is True


def test_20_congested_10_mbps_threshold_is_centralized(monkeypatch, yang_policy_factory):
    monkeypatch.setenv("IDN_QOS_KPI_TOLERANCE_RATIO", "0.95")
    constraints = StatusVerifierAgent()._extract_constraints(yang_policy_factory(bandwidth=10))
    assert constraints.min_bandwidth_mbps == 9.5


def test_21_congested_30_mbps_threshold_is_centralized(monkeypatch, yang_policy_factory):
    monkeypatch.setenv("IDN_QOS_KPI_TOLERANCE_RATIO", "0.95")
    constraints = StatusVerifierAgent()._extract_constraints(yang_policy_factory(bandwidth=30))
    assert constraints.min_bandwidth_mbps == 28.5


def test_22_rate_limit_has_only_a_maximum_threshold(monkeypatch, yang_policy_factory):
    monkeypatch.setenv("IDN_QOS_RATE_LIMIT_TOLERANCE_RATIO", "1.10")
    policy = yang_policy_factory(action_type=ActionType.RATE_LIMIT, bandwidth=10)
    policy.network_policy.action.qos_params.qos_mode = QoSMode.RATE_LIMIT
    constraints = StatusVerifierAgent()._extract_constraints(policy)
    assert effective_qos_mode(policy) == "RATE_LIMIT"
    assert constraints.min_bandwidth_mbps is None and constraints.max_bandwidth_mbps == 11


def test_23_udp_iperf_client_result_uses_delivered_not_offered_rate():
    parsed = probe.parse_iperf_output(json.dumps({
        "start": {"test_start": {"protocol": "UDP"}},
        "end": {"sum": {
            "bits_per_second": 60_000_000,
            "packets": 30_000,
            "lost_packets": 20_000,
            "lost_percent": 66.6667,
        }},
    }))

    assert parsed["sent_mbps"] == 60
    assert parsed["throughput_mbps"] == pytest.approx(20)
    assert parsed["received_mbps"] == pytest.approx(20)


def test_udp_iperf_prefers_embedded_receiver_server_measurement():
    parsed = probe.parse_iperf_output(json.dumps({
        "start": {"test_start": {"protocol": "UDP"}},
        "end": {"sum": {"bits_per_second": 40_000_000}},
        "server_output_json": {
            "start": {"test_start": {"protocol": "UDP"}},
            "end": {"sum": {
                "bits_per_second": 19_800_000,
                "packets": 20_000,
                "lost_packets": 20_000,
                "lost_percent": 50,
            }},
        },
    }))

    assert parsed["throughput_mbps"] == pytest.approx(19.8)
    assert parsed["measurement_source"] == "receiver_server"


def test_udp_iperf_reconstructs_broken_zero_server_summary_from_intervals():
    parsed = probe.parse_iperf_output(json.dumps({
        "start": {"test_start": {"protocol": "UDP"}},
        "end": {"sum": {"bits_per_second": 40_000_000}},
        "server_output_json": {
            "start": {"test_start": {"protocol": "UDP"}},
            "intervals": [
                {"sum": {"seconds": 1, "bytes": 100_000, "omitted": True}},
                {"sum": {"seconds": 1, "bytes": 2_475_000}},
                {"sum": {"seconds": 1, "bytes": 2_500_000}},
            ],
            "end": {"sum": {"bits_per_second": 0, "bytes": 0}},
        },
    }))

    assert parsed["throughput_mbps"] == pytest.approx(19.9)


def test_qos_mode_is_persisted_but_excluded_from_scope_key(yang_policy_factory):
    minimum = yang_policy_factory(action_type=ActionType.SET_PRIORITY)
    limited = yang_policy_factory(action_type=ActionType.RATE_LIMIT)
    minimum.network_policy.action.qos_params.qos_mode = QoSMode.MIN_BANDWIDTH
    limited.network_policy.action.qos_params.qos_mode = QoSMode.RATE_LIMIT
    assert traffic_scope_key(minimum).key == traffic_scope_key(limited).key
    assert minimum.model_dump(mode="json")["network_policy"]["action"]["qos_params"]["qos_mode"] == "MIN_BANDWIDTH"


def test_rate_limit_no_background_probe_does_not_start_h_video(monkeypatch):
    binding = _binding()
    probe._qos_policies["TSK-test-video"] = {
        **_payload(qos_mode="RATE_LIMIT", bandwidth_mbps=20),
        "qos_binding": binding,
    }
    nodes = {name: type("Node", (), {"name": name})() for name in ("h_research", "h_video", "h_dc")}
    net = type("Net", (), {
        "nameToNode": nodes,
        "get": lambda self, name: nodes[name],
    })()
    started = []

    class FakeProcess:
        def __init__(self, node, args):
            self.node = node
            self.args = args
            self.returncode = 0
            self.done = "-c" in args
        def communicate(self, timeout=None):
            output = json.dumps({
                "start": {"test_start": {"protocol": "UDP"}},
                "end": {"sum": {
                    "bits_per_second": 60_000_000,
                    "packets": 30_000,
                    "lost_packets": 20_000,
                    "lost_percent": 66.6667,
                    "jitter_ms": 0.2,
                }},
            })
            return output, ""
        def poll(self):
            return 0 if self.done else None

    def fake_popen(node, args, **_kwargs):
        started.append((node.name, args))
        return FakeProcess(node, args)

    readbacks = iter([
        {"stats": {"sent_bytes": 0, "sent_packets": 0, "overlimits": 0}, "root_stats": {}, "default_stats": {}},
        {"stats": {"sent_bytes": 1_000_000, "sent_packets": 1000, "overlimits": 50}, "root_stats": {}, "default_stats": {}},
    ])
    monkeypatch.setattr(probe, "_node_popen", fake_popen)
    monkeypatch.setattr(probe, "_read_tc_resource", lambda *_: next(readbacks))
    monkeypatch.setattr(probe, "_kill_process", lambda process: setattr(process, "done", True))
    monkeypatch.setattr(probe.time, "sleep", lambda *_: None)

    result = probe._run_qos_competition(net, {
        **_payload(qos_mode="RATE_LIMIT", bandwidth_mbps=20),
        "test_profile": "RATE_LIMIT_NO_BACKGROUND",
        "include_background": False,
        "duration": 15,
        "target_offered_mbps": 60,
    })
    assert result["test_type"] == "NO_BACKGROUND"
    assert result["background_started"] is False and result["background"] is None
    assert result["duration"] == 15 and result["throughput_mbps"] == pytest.approx(20)
    assert result["warmup_duration"] == 3 and result["traffic_wall_duration"] == 18
    assert all(node_name != "h_video" for node_name, _args in started)
    target_args = next(args for node_name, args in started if node_name == "h_research")
    assert target_args[target_args.index("--fq-rate") + 1] == "60M"
    assert result["tc_class_counter_increased"] is True
    assert result["process_cleanup_verified"] is True


def test_p4_counter_evidence_uses_per_validation_delta():
    evidence = p4_counter_evidence(
        {"packets": 100, "bytes": 10_000},
        {"packets": 135, "bytes": 15_250, "read_at": "2026-07-22T00:00:00Z"},
    )
    assert evidence["packets_delta"] == 35
    assert evidence["bytes_delta"] == 5_250
    assert evidence["verification_state"] == "P4_COUNTER_VERIFIED"


def test_p4_counter_zero_delta_is_not_verified():
    evidence = p4_counter_evidence(
        {"packets": 100, "bytes": 10_000},
        {"packets": 100, "bytes": 10_000},
    )
    assert evidence["counter_hit"] is False
    assert evidence["verification_state"] == "P4_COUNTER_ZERO"


@pytest.mark.asyncio
async def test_demo_refuses_mode_change_outside_formal_intent_chain(yang_policy_factory):
    policy = yang_policy_factory(action_type=ActionType.SET_PRIORITY, bandwidth=10)
    policy.network_policy.action.qos_params.qos_mode = QoSMode.MIN_BANDWIDTH
    with pytest.raises(ValueError, match="formal intent API"):
        await run_qos_demo_validation(
            policy,
            {},
            scenario="RATE_LIMIT_NO_BACKGROUND",
        )


def test_execution_plane_contains_four_consistent_evidence_layers():
    execution = {
        "traffic_scope_key": "TSK-test-video",
        "classifier_reused": True,
        "qos_enforcement": {
            "qos_mode": "MIN_BANDWIDTH",
            "link_capacity_mbps": 100,
            "qos_binding": _binding(),
        },
    }
    verification = {
        "passed": True,
        "verification_state": "MIN_BANDWIDTH_KPI_VERIFIED",
        "config_verification": {
            "p4_classifier_after_traffic": {
                "device_id": "device:bmv2:5", "flow_id": "101", "flow_state": "ADDED",
                "counter_hit": True, "packets": 135, "bytes": 15_250,
            },
            "p4_counter": {
                "verification_state": "P4_COUNTER_VERIFIED", "counter_hit": True,
                "packets_before": 100, "packets_after": 135, "packets_delta": 35,
                "bytes_before": 10_000, "bytes_after": 15_250, "bytes_delta": 5_250,
            },
            "switch_egress_qos": {
                "qos_mode": "MIN_BANDWIDTH", "config_applied": True,
                "rate_mbps": 10, "ceil_mbps": 100, "qos_binding": _binding(),
                "tc_readback": {"class_id": "1:1234", "stats": {"sent_bytes": 1}},
            },
        },
        "policy_traffic_probe": {
            "traffic_generated": True, "test_type": "COMPETING_TRAFFIC",
            "background_started": True, "congestion_created": True,
            "throughput_mbps": 20, "background": {"throughput_mbps": 75},
            "tc_class_counter_increased": True,
        },
    }
    evidence = _execution_plane(execution, verification)
    assert evidence["classifier"]["verification_state"] == "P4_CLASSIFIER_VERIFIED"
    assert evidence["p4_counter"]["packets_delta"] == 35
    assert evidence["scheduler"]["verification_state"] == "SWITCH_EGRESS_QOS_VERIFIED"
    assert evidence["competition_kpi"]["background_throughput_mbps"] == 75
    assert evidence["path"]["display_state"] == "无路径变化"
    assert evidence["all_evidence_consistent"] is True


def test_ingress_calibration_window_restores_the_original_authoritative_plan(monkeypatch):
    """Capacity sampling must not turn a temporary larger ceiling into desired state."""
    plan = {
        "admission_plan_id": "INGRESS-CAL-1", "traffic_scope_key": "scope-cal-1",
        "mode": "IDLE_BORROWING", "schedulable_capacity_mbps": 49,
        "implementation_reserved_mbps": 30, "best_effort_residual_mbps": 19,
        "expected_aggregate_ceiling_mbps": 49,
        "protected_scopes": [{
            "source_ip": "192.0.2.10", "destination_ip": "198.51.100.10",
            "protocol": "UDP", "destination_port": 5004,
            "implementation_guaranteed_floor_mbps": 30,
            "ingress_admission_ceiling_mbps": 49,
        }],
        "best_effort_scopes": [], "inactive_best_effort_scopes": [],
    }
    calls = []
    probe._ingress_admission_plans[plan["admission_plan_id"]] = plan

    def fake_apply(_net, raw):
        calls.append(raw)
        result = dict(raw)
        result["status"] = "VERIFIED"
        probe._ingress_admission_plans[raw["admission_plan_id"]] = result
        return result

    monkeypatch.setattr(probe, "_apply_ingress_admission", fake_apply)
    net = type("Net", (), {})()
    entered = probe._set_ingress_calibration_window(net, "INGRESS-CAL-1", "scope-cal-1", "ENTER", 59)
    restored = probe._set_ingress_calibration_window(net, "INGRESS-CAL-1", "scope-cal-1", "EXIT")

    assert entered["status"] == "ok" and entered["ceiling_mbps"] == 59
    assert calls[0]["schedulable_capacity_mbps"] == 59
    assert calls[0]["protected_scopes"][0]["ingress_admission_ceiling_mbps"] == 59
    assert restored["status"] == "ok"
    assert calls[1]["schedulable_capacity_mbps"] == 49
    assert calls[1]["protected_scopes"][0]["ingress_admission_ceiling_mbps"] == 49


def test_rate_limit_qos_can_open_a_fenced_calibration_window(monkeypatch):
    """A stale cap must not cap the probe needed to recover its capacity evidence."""
    policy = _payload(qos_mode="RATE_LIMIT", bandwidth_mbps=20, priority=3)
    policy["qos_binding"] = _binding()
    probe._qos_policies[policy["scope_key"]] = policy
    commands = []
    restored = []

    monkeypatch.setattr(probe, "_tc_cmd", lambda _node, command: commands.append(command) or "classes")
    monkeypatch.setattr(probe, "_tc_class_values", lambda _output, class_id: {
        "rate_mbps": 100.0 if class_id in {"1", "100"} else None,
        "ceil_mbps": 100.0 if class_id == "100" else None,
    })
    monkeypatch.setattr(probe, "_install_tc_resource", lambda _net, payload, _binding, create_filter: restored.append((payload, create_filter)) or {"config_applied": True})

    net = type("Net", (), {"get": lambda _self, _name: object()})()
    entered = probe._set_qos_calibration_window(net, policy["scope_key"], "ENTER")
    exited = probe._set_qos_calibration_window(net, policy["scope_key"], "EXIT")

    assert entered["status"] == "ok"
    assert any("classid 1:100 htb rate 100mbit ceil 100mbit" in command for command in commands)
    assert exited["status"] == "ok"
    assert restored == [(policy, True)]


def test_frontend_evidence_area_contains_all_four_layers():
    source = (
        _PROJECT_ROOT
        / "frontend" / "src" / "components" / "dashboard" / "EvidenceSummary.vue"
    ).read_text(encoding="utf-8")
    presentation = (
        _PROJECT_ROOT
        / "frontend" / "src" / "utils" / "evidencePresentation.js"
    ).read_text(encoding="utf-8")
    # Four independent layers: control plane, data plane, egress QoS and KPI.
    assert "策略生效证据" in source
    for label in ("ONOS Flow", "P4 Direct Counter", "TC 配置回读", "Mininet ping/iperf"):
        assert label in presentation

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import threading

import pytest

from api.qos import p4_counter_evidence
from api.p4_evidence import selector_match_fields
from api.service_profiles import (
    ValidationProfileMismatch,
    get_service_profile,
    get_traffic_scenario,
    list_service_profiles,
    attach_service_profile,
    profile_matches_policy,
    resolve_service_profile_for_policy,
)
from api.service_validation import no_sample_result
from agents.intent_planner.action_keywords import extract_explicit_dscp, has_explicit_dscp
from agents.policy_modeling import PolicyModelingAgent
from models.intent_plan import IntentPlan, SubIntent
from models.policy_update import traffic_scope_key
from models.semantic_resolution import FieldProvenance
from models.yang_policy import (
    ActionRules,
    ActionType,
    MatchCriteria,
    NetworkPolicy,
    Protocol,
    QoSParams,
    YANGPolicy,
)


ROOT = Path(__file__).resolve().parents[2]
PROBE_SERVER_PATH = next(
    path for path in (
        ROOT / "mininet" / "probe_server.py",
        Path("/workspace/mininet/probe_server.py"),
    )
    if path.exists()
)
SPEC = importlib.util.spec_from_file_location(
    "idn_probe_server_service_tests", PROBE_SERVER_PATH
)
probe_server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(probe_server)


def test_iperf_server_lock_pool_allows_independent_business_flows_to_overlap():
    """Only identical destination sockets are mutually exclusive.

    This protects a real concurrent multi-intent probe from being silently
    serialized by an HTTP-server-wide iperf lock.
    """
    pool = probe_server._IperfServerLockPool()
    first = pool.for_server("h_research", 5203)
    distinct = pool.for_server("h_dc", 5004)
    same = pool.for_server("h_research", 5203)
    assert first is same
    assert first is not distinct

    entered = threading.Event()
    release = threading.Event()
    acquired = threading.Event()

    def hold_first():
        with first:
            entered.set()
            release.wait(timeout=1)

    def take_distinct():
        entered.wait(timeout=1)
        with distinct:
            acquired.set()

    one = threading.Thread(target=hold_first)
    two = threading.Thread(target=take_distinct)
    one.start()
    two.start()
    assert acquired.wait(timeout=0.5)
    release.set()
    one.join(timeout=1)
    two.join(timeout=1)


def test_service_validation_reserves_only_its_exact_listener_sockets():
    research = probe_server._service_validation_server_keys({
        "service_id": "research_video", "background_profiles": ["background_video"],
    })
    backup = probe_server._service_validation_server_keys({"service_id": "backup_data"})

    assert ("h_dc", 5004) in research
    assert ("h_dc", 5005) in research
    assert backup == [("h_research", 5203)]
    assert set(research).isdisjoint(backup)


def test_rate_limit_validation_remains_an_upper_bound_with_real_traffic():
    result = probe_server._service_validation_result(
        probe_server._service_profile("backup_data"),
        {"sender_mbps": 19.0, "receiver_mbps": 17.7, "retransmits": 4},
        {"qos_mode": "RATE_LIMIT", "bandwidth_mbps": 20},
    )

    assert result["passed"] is True
    assert result["verification_state"] == "RATE_LIMIT_KPI_VERIFIED"
    assert result["traffic_observed"] is True
    assert result["maximum_acceptable_mbps"] == 22.0
    assert "minimum_healthy_mbps" not in result


def research_policy(**updates) -> YANGPolicy:
    match = {
        "source_ip": "192.168.10.10/32",
        "destination_ip": "10.0.0.10/32",
        "protocol": "UDP",
        "destination_port": 5004,
    }
    match.update(updates.pop("match", {}))
    qos = {"dscp_value": 46, "bandwidth": 10, "burst_size": 1024}
    qos.update(updates.pop("qos", {}))
    return YANGPolicy(
        service_id=updates.pop("service_id", "research_video"),
        validation_profile_id="research_video",
        network_policy=NetworkPolicy(
            policy_name="research-video",
            match=MatchCriteria(**match),
            action=ActionRules(
                action_type=ActionType.SET_PRIORITY,
                qos_params=QoSParams(**qos),
            ),
        ),
    )


def test_service_profile_registry_loads_and_video_is_udp_5004():
    profiles = list_service_profiles()
    video = get_service_profile("research_video")
    assert len(profiles) >= 5
    assert video.protocol.value == "UDP"
    assert video.dst_port == 5004
    assert video.dscp == 46
    assert video.src_applicability["mode"] == "ANY_REGISTERED_HOST"
    assert video.dst_applicability["mode"] == "ALLOW_LIST"
    assert "loss_percent" in video.validation_metrics
    assert "jitter_ms" in video.validation_metrics


def test_explicit_background_profile_defaults_omitted_dscp_to_zero():
    """DSCP 0 must survive named-service attachment as an explicit value."""
    policy = YANGPolicy(
        network_policy=NetworkPolicy(
            policy_name="background-video",
            match=MatchCriteria(
                source_ip="192.168.10.20/32", destination_ip="10.0.0.10/32",
                protocol=Protocol.UDP, destination_port=5005,
            ),
            action=ActionRules(
                action_type=ActionType.SET_PRIORITY,
                qos_params=QoSParams(bandwidth=10, dscp_value=None),
            ),
        ),
    )

    attached = attach_service_profile(policy, "background_video")

    assert attached.service_id == "background_video"
    assert attached.network_policy.action.qos_params.dscp_value == 0


def _profile_plan(service_id: str, dscp_value, origin: str, raw_intent: str) -> IntentPlan:
    profile = get_service_profile(service_id)
    return IntentPlan(
        raw_intent=raw_intent,
        sub_intents=[
            SubIntent(
                sequence=1,
                objective=raw_intent,
                intent_type="SET_PRIORITY",
                match={
                    "source_ip": profile.src_ip,
                    "destination_ip": profile.dst_ip,
                    "protocol": profile.protocol.value,
                    "destination_port": profile.dst_port,
                },
                action={
                    "action_type": "SET_PRIORITY",
                    "qos_params": {"bandwidth": 10, "dscp_value": dscp_value},
                },
                constraints={
                    "policy_name": f"{service_id}-provenance",
                    "field_provenance": {"dscp_value": origin},
                },
                service_id=service_id,
                validation_profile_id=service_id,
            )
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_id", "llm_dscp", "origin", "expected"),
    [
        ("background_video", 46, FieldProvenance.LLM_DEFAULT.value, 0),
        ("background_video", None, FieldProvenance.LLM_INFERRED.value, 0),
        ("research_video", None, FieldProvenance.SERVICE_PROFILE.value, 46),
        ("shared_port_video", None, FieldProvenance.SERVICE_PROFILE.value, 0),
    ],
)
async def test_profile_canonicalizes_nonexplicit_dscp_provenance(
    service_id, llm_dscp, origin, expected
):
    """A provider default is not a user selector and cannot defeat a profile."""
    policy = await PolicyModelingAgent().model(
        _profile_plan(service_id, llm_dscp, origin, f"ensure {service_id} bandwidth")
    )
    assert policy.network_policy.action.qos_params.dscp_value == expected
    assert policy.service_id == service_id


@pytest.mark.asyncio
async def test_background_explicit_dscp_zero_is_preserved_without_falsey_default():
    assert has_explicit_dscp("background_video DSCP0")
    assert extract_explicit_dscp("background_video DSCP0") == 0
    policy = await PolicyModelingAgent().model(
        _profile_plan(
            "background_video", 0, FieldProvenance.USER_EXPLICIT.value,
            "background_video use DSCP0",
        )
    )
    assert policy.network_policy.action.qos_params.dscp_value == 0


@pytest.mark.asyncio
async def test_background_explicit_dscp_override_is_not_silently_rewritten():
    """A genuine user override must produce the formal profile conflict."""
    with pytest.raises(ValidationProfileMismatch, match="selector mismatch: dscp"):
        await PolicyModelingAgent().model(
            _profile_plan(
                "background_video", 46, FieldProvenance.USER_EXPLICIT.value,
                "background_video use DSCP46",
            )
        )


def test_backup_tcp_profile_has_a_direct_p4_evidence_binding():
    """TCP business evidence must use the same concrete classifier device."""
    backup = get_service_profile("backup_data")
    assert backup.protocol.value == "TCP"
    assert backup.dst_port == 5203
    assert backup.p4_counter_binding == {
        "device_id": "device:bmv2:6",
        "table": "MyIngress.sla_enforcement",
        "counter": "MyIngress.qos_classifier_counter",
    }
    assert backup.qos_binding == {
        "switch": "s_agg_r", "interface": "s_agg_r-eth2",
        "bottleneck_link": "s_access_r-s_agg_r",
    }


def test_network_health_profile_protocol_is_representable_by_yang_policy():
    """A formal connectivity intent must not fail when its ICMP profile is resolved."""
    profile = get_service_profile("network_health_research_dc")
    match = MatchCriteria(
        source_ip=profile.src_ip,
        destination_ip=profile.dst_ip,
        protocol=profile.protocol.value,
    )
    assert match.protocol is Protocol.ICMP


def test_services_can_share_port_when_full_selectors_differ():
    research = get_service_profile("research_video")
    shared = get_service_profile("shared_port_video")
    assert research.dst_port == shared.dst_port == 5004
    assert research.selector() != shared.selector()
    assert research.src_ip != shared.src_ip
    assert research.dscp != shared.dscp
    # Sharing UDP/5004 does not make the second classifier unobservable:
    # both centralized profiles must name the same direct-counter table while
    # their /32+DSCP selectors keep the counter evidence unambiguous.
    assert shared.p4_counter_binding == {
        "device_id": "device:bmv2:5",
        "table": "MyIngress.sla_enforcement",
        "counter": "MyIngress.qos_classifier_counter",
    }


def test_policy_profile_consistency_and_mismatch():
    policy = research_policy()
    profile = resolve_service_profile_for_policy(policy)
    assert profile.service_id == "research_video"
    matched, mismatches = profile_matches_policy(profile, policy)
    assert matched and not mismatches

    wrong = research_policy(match={"protocol": "TCP"})
    with pytest.raises(ValidationProfileMismatch):
        resolve_service_profile_for_policy(wrong)


def test_traffic_scope_carries_service_id_without_changing_selector_hash():
    with_service = research_policy(service_id="research_video")
    without_service = with_service.model_copy(update={"service_id": None})
    key = traffic_scope_key(with_service)
    assert key.service_id == "research_video"
    assert key.key == traffic_scope_key(without_service).key


def test_same_service_on_two_registered_sources_has_distinct_traffic_scopes():
    research_scope = traffic_scope_key(research_policy()).key
    ai_policy = research_policy(
        match={"source_ip": "192.168.20.10/32"},
        service_id="research_video",
    )
    profile = resolve_service_profile_for_policy(ai_policy)
    matched, mismatches = profile_matches_policy(profile, ai_policy)
    assert profile.service_id == "research_video"
    assert matched and not mismatches
    assert traffic_scope_key(ai_policy).key != research_scope


def test_direct_p4_evidence_uses_transport_field_from_protocol():
    common = {"src": "10.0.0.20/32", "dst": "192.168.10.10/32", "dst_port": 5203}
    tcp = selector_match_fields({**common, "protocol": 6})
    udp = selector_match_fields({**common, "protocol": 17})

    assert tcp["hdr.tcp.dstPort"] == "1453"
    assert "hdr.udp.dstPort" not in tcp
    assert udp["hdr.udp.dstPort"] == "1453"
    assert "hdr.tcp.dstPort" not in udp


def test_no_sample_uses_null_protocol_metrics_not_zero():
    value = no_sample_result("research_video")
    assert value["status"] == "NO_SAMPLE"
    assert value["protocol_metrics"]["loss_percent"] is None
    assert value["protocol_metrics"]["jitter_ms"] is None
    assert value["protocol_metrics"]["received_mbps"] is None


def test_udp_parser_has_real_udp_fields_and_no_tcp_retransmits():
    payload = {
        "start": {"test_start": {"protocol": "UDP"}},
        "end": {"sum": {"bits_per_second": 12_000_000, "packets": 1000}},
        "server_output_json": {
            "start": {"test_start": {"protocol": "UDP"}},
            "end": {"sum": {"bits_per_second": 11_500_000, "packets": 1000,
                              "lost_packets": 5, "lost_percent": 0.5,
                              "jitter_ms": 0.23, "out_of_order": 1}},
        },
    }
    metrics = probe_server.parse_udp_iperf_output(json.dumps(payload))
    assert metrics["sent_mbps"] == 12
    assert metrics["received_mbps"] == 11.5
    assert metrics["lost_packets"] == 5
    assert metrics["loss_percent"] == 0.5
    assert metrics["jitter_ms"] == 0.23
    assert "retransmits" not in metrics


def test_service_probe_completion_budget_includes_warmup_and_grace(monkeypatch):
    """Process deadlines must cover iperf warmup without changing the SLA."""
    monkeypatch.setenv("MININET_SERVICE_PROBE_COMPLETION_GRACE_SECONDS", "17")
    budget = probe_server._service_probe_timeout_seconds(
        [
            {"duration_seconds": 15, "warmup_seconds": 5},
            {"duration_seconds": 10, "warmup_seconds": 0},
        ]
    )
    assert budget == 37


def test_min_bandwidth_measurement_waits_for_target_not_background_completion():
    """A lossy background's final UDP handshake cannot invalidate target KPI."""
    class Process:
        pass

    target = ({"role": "target", "service_id": "foreground"}, Process())
    background = ({"role": "background", "service_id": "best-effort"}, Process())

    assert probe_server._measurement_client_records([target, background]) == [target]


def test_tcp_parser_has_retransmits_and_no_udp_loss_fields():
    payload = {
        "end": {
            "sum_sent": {"bits_per_second": 20_000_000, "bytes": 2_000_000,
                         "seconds": 1.0, "retransmits": 3},
            "sum_received": {"bits_per_second": 19_000_000, "bytes": 1_900_000},
            "streams": [{"sender": {"mean_rtt": 2500, "max_snd_cwnd": 65536,
                                      "retransmits": 3}}],
        }
    }
    metrics = probe_server.parse_tcp_iperf_output(json.dumps(payload))
    assert metrics["sender_mbps"] == 20
    assert metrics["receiver_mbps"] == 19
    assert metrics["retransmits"] == 3
    assert metrics["mean_rtt_ms"] == 2.5
    assert "lost_packets" not in metrics
    assert "jitter_ms" not in metrics


def test_icmp_parser_is_protocol_scoped():
    output = (
        "3 packets transmitted, 3 received, 0% packet loss, time 2002ms\n"
        "rtt min/avg/max/mdev = 1.000/2.000/3.000/0.500 ms\n"
    )
    metrics = probe_server.parse_icmp_service_output(output)
    assert metrics == {
        "transmitted": 3, "received": 3, "loss_percent": 0.0,
        "rtt_min_ms": 1.0, "rtt_avg_ms": 2.0, "rtt_max_ms": 3.0,
        "rtt_mdev_ms": 0.5, "raw": output,
    }


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ({"status": "MATCHED", "matching_entry_count": 1, "packets": 10, "bytes": 100}, "VERIFIED"),
        ({"status": "MATCHED", "matching_entry_count": 1, "packets": 0, "bytes": 0}, "MATCHING_ENTRY_NO_TRAFFIC"),
        ({"status": "NO_MATCHING_ENTRY", "matching_entry_count": 0}, "NO_MATCHING_ENTRY"),
        ({"status": "READ_FAILED", "matching_entry_count": 0}, "READ_FAILED"),
        ({"status": "UNSUPPORTED", "matching_entry_count": 0}, "UNSUPPORTED"),
        ({"status": "NOT_APPLICABLE", "matching_entry_count": 0}, "NOT_APPLICABLE"),
    ],
)
def test_p4_counter_status_is_not_ok_for_zero_or_absent(after, expected):
    value = p4_counter_evidence({"packets": 0, "bytes": 0}, after)
    assert value["status"] == expected
    assert value["counter_hit"] is (expected == "VERIFIED")


@pytest.mark.asyncio
async def test_dynamic_qos_service_uses_runtime_classifier_counter(monkeypatch):
    from api.service_validation import _runtime_classifier_snapshot

    async def verified(_policy, runtime, **kwargs):
        assert runtime["installed_flows"][0]["flow_id"] == "tcp-flow"
        assert kwargs["minimum_packets"] == 7
        return {
            "status": "ok", "verification_state": "P4_CLASSIFIER_VERIFIED",
            "config_applied": True, "device_id": "device:bmv2:3", "flow_id": "tcp-flow",
            "packets": 11, "bytes": 2200, "matching_entry_count": 1,
            "table_name": "MyIngress.sla_enforcement",
        }

    monkeypatch.setattr("api.qos.verify_p4_classifier", verified)
    snapshot = await _runtime_classifier_snapshot(
        research_policy(match={"protocol": "TCP", "destination_port": 5202}),
        {"installed_flows": [{"device_id": "device:bmv2:3", "flow_id": "tcp-flow"}]},
        minimum_packets=7, minimum_bytes=1400,
    )
    assert snapshot["status"] == "VERIFIED"
    assert snapshot["packets"] == 11
    assert snapshot["counter"] == "MyIngress.qos_classifier_counter"


def test_tc_stats_target_class_does_not_double_count_root_or_default():
    output = """class htb 1:1 root rate 100Mbit ceil 100Mbit
 Sent 10000 bytes 100 pkt (dropped 1, overlimits 2 requeues 0)
class htb 1:fffe parent 1:1 rate 1Mbit ceil 100Mbit
 Sent 5000 bytes 50 pkt (dropped 3, overlimits 4 requeues 0)
class htb 1:678b parent 1:1 rate 10Mbit ceil 100Mbit
 Sent 4000 bytes 40 pkt (dropped 0, overlimits 7 requeues 0)
 lended: 2 borrowed: 3 giants: 0
 backlog 1200b 1p requeues 0
"""
    target = probe_server._tc_class_stats(output, "678b")
    assert target["sent_packets"] == 40
    assert target["dropped_packets"] == 0
    assert target["overlimits"] == 7
    assert target["borrowed"] == 3


def test_root_qdisc_parser_uses_one_root_only_and_overlimits_is_not_drop():
    output = """qdisc htb 1: root refcnt 2
 Sent 1000 bytes 10 pkt (dropped 1, overlimits 9 requeues 0)
qdisc fq_codel 0: parent 1:678b
 Sent 2000 bytes 20 pkt (dropped 3, overlimits 7 requeues 0)
"""
    value = probe_server.parse_root_qdisc_output(output)
    assert value["dropped_packets"] == 1
    assert value["overlimits"] == 9
    assert value["aggregation_scope"] == "root_qdisc_only"
    assert value["overlimits_is_drop"] is False


def test_selected_runtime_path_uses_validated_intent_plan_not_shortest_path():
    topology = {
        "links": [
            {"id": "h1-s1", "state": "ACTIVE", "endpoints": [{"node": "h1"}, {"node": "s1"}]},
            {"id": "s1-h2", "state": "ACTIVE", "endpoints": [{"node": "s1"}, {"node": "h2"}]},
            {"id": "h1-s2", "state": "ACTIVE", "endpoints": [{"node": "h1"}, {"node": "s2"}]},
            {"id": "s2-h2", "state": "ACTIVE", "endpoints": [{"node": "s2"}, {"node": "h2"}]},
        ]
    }
    nodes, links = probe_server._selected_runtime_path(
        topology, "h1", "h2", ["h1", "s2", "h2"]
    )
    assert nodes == ["h1", "s2", "h2"]
    assert [item["id"] for item in links] == ["h1-s2", "s2-h2"]


def test_tracked_cleanup_ignores_unrelated_production_processes():
    class Finished:
        pid = 101

        @staticmethod
        def poll():
            return 0

    result = probe_server._tracked_process_cleanup_evidence([Finished()])
    assert result["process_cleanup_verified"] is True
    assert result["scope"] == "CURRENT_PROBE_ONLY"


def test_scenarios_encode_qos_mode_specific_background_rules():
    no_background = get_traffic_scenario("no_background")
    competition = get_traffic_scenario("shared_bottleneck_congestion")
    mixed = get_traffic_scenario("mixed_services")
    assert no_background.background_flows == []
    assert competition.bottleneck_link == "s_access_r-s_agg_r"
    assert competition.background_flows[0].service_profile_id == "background_video"
    assert {item.service_profile_id for item in mixed.background_flows} == {"ai_data", "backup_data"}


def test_services_api_and_no_sample_contract(app_client, monkeypatch):
    import api.service_validation as validation_module

    monkeypatch.setattr(validation_module, "_latest_metrics", {})
    monkeypatch.setattr(validation_module, "_metrics_history", {})
    response = app_client.get("/api/v1/services")
    assert response.status_code == 200
    payload = response.json()
    assert any(item["service_id"] == "research_video" for item in payload["profiles"])
    unmeasured = payload["latest_metrics"]["shared_port_video"]
    assert unmeasured["status"] == "NO_SAMPLE"
    assert unmeasured["protocol_metrics"]["loss_percent"] is None


@pytest.mark.asyncio
async def test_verify_probe_uses_bound_udp_profile_not_tcp_5201(monkeypatch):
    from agents.status_verifier import StatusVerifierAgent
    import api.service_validation as validation_module

    captured = {}

    async def fake_run(service_id, request, **kwargs):
        captured.update({"service_id": service_id, "request": request, **kwargs})
        return {
            "status": "MEASURED", "protocol": "UDP",
            "protocol_metrics": {"received_mbps": 10.2, "loss_percent": 0.1,
                                 "lost_packets": 2, "jitter_ms": 0.2},
        }

    monkeypatch.setattr(validation_module, "run_service_validation", fake_run)
    result = await StatusVerifierAgent()._collect_policy_throughput(
        research_policy(), intent_id="INT-1", execution_result={"status": "SUCCESS"}
    )
    assert captured["service_id"] == "research_video"
    assert captured["request"]["offered_rate_mbps"] == 30
    assert result["protocol"] == "UDP"
    assert result["throughput_mbps"] == 10.2
    assert captured["request"].get("dst_port") is None
    assert get_service_profile(captured["service_id"]).dst_port == 5004


@pytest.mark.asyncio
async def test_min_bandwidth_probe_uses_bounded_overoffer_with_competition(monkeypatch):
    """Validation demand may exceed a floor, but must preserve BE residual."""
    from agents.status_verifier import StatusVerifierAgent
    import api.service_validation as validation_module

    captured = {}

    async def fake_run(service_id, request, **kwargs):
        captured.update({"service_id": service_id, "request": request, **kwargs})
        return {
            "status": "MEASURED", "protocol": "UDP",
            "protocol_metrics": {"received_mbps": 30.0, "loss_percent": 0.0,
                                 "lost_packets": 0, "jitter_ms": 0.1},
        }

    monkeypatch.setattr(validation_module, "run_service_validation", fake_run)
    await StatusVerifierAgent()._collect_policy_throughput(
        research_policy(qos={"bandwidth": 30}),
        intent_id="INT-MIN-30", execution_result={"status": "SUCCESS"},
    )

    assert captured["request"]["offered_rate_mbps"] == 40
    assert captured["request"]["background_profiles"] == ["background_video"]


def test_active_competitor_resolution_uses_scope_desired_state(monkeypatch):
    """A validation contender is selected by declared scope, never by name."""
    import api.service_validation as validation_module
    import infra.ingress_admission_state as admission_module

    class FakeDesiredRepository:
        def __init__(self, _store):
            pass

        def active_for_scope(self, scope):
            assert scope == traffic_scope_key(research_policy(qos={"bandwidth": 30})).key
            return {"desired": {"eligible_best_effort_scopes": [
                {"scope_id": "be-alpha", "service_id": "background_video"},
                {"scope_id": "be-beta", "service_id": "unrequested-service"},
            ]}}

    monkeypatch.setattr(admission_module, "IngressAdmissionDesiredRepository", FakeDesiredRepository)
    active = validation_module._active_best_effort_scope_ids(
        policy=research_policy(qos={"bandwidth": 30}),
        request={"background_profiles": [{"service_id": "background_video"}]},
    )
    assert active == {"be-alpha"}


@pytest.mark.asyncio
async def test_low_latency_uses_bound_service_probe_without_competition(monkeypatch):
    from agents.status_verifier import StatusVerifierAgent
    import api.service_validation as validation_module

    captured = {}

    async def fake_run(service_id, request, **kwargs):
        captured.update({"service_id": service_id, "request": request, **kwargs})
        return {
            "status": "MEASURED",
            "protocol": "UDP",
            "protocol_metrics": {
                "received_mbps": 1.0,
                "loss_percent": 0.0,
                "lost_packets": 0,
                "jitter_ms": 0.1,
            },
        }

    monkeypatch.setattr(validation_module, "run_service_validation", fake_run)
    policy = research_policy(qos={"bandwidth": None})
    policy.raw_intent = "保障科研视频低时延与低抖动"
    result = await StatusVerifierAgent()._collect_policy_throughput(
        policy, intent_id="INT-LATENCY", execution_result={"status": "SUCCESS"}
    )
    assert result["protocol"] == "UDP"
    assert captured["scenario_id"] is None
    assert not captured["request"].get("background_profiles")

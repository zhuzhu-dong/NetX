"""Agent-layer unit tests — LLM config, SafetyCouncil, RootCauseAnalyzer,
ExperienceMemory, and StatusVerifier.

Covers: agents.llm_client, agents.safety_council, agents.root_cause_analyzer,
        agents.experience_memory, agents.status_verifier
"""

from __future__ import annotations

import pytest

from agents.experience_memory import ExperienceMemoryAgent
from agents.llm_client import load_llm_config
from agents.root_cause_analyzer import RootCauseAnalyzerAgent
from agents.safety_council import SafetyCouncilAgent
from agents.status_verifier import StatusVerifierAgent


@pytest.mark.asyncio
async def test_status_verifier_bounds_a_stalled_external_step():
    import asyncio
    import time

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="VERIFICATION_STEP_TIMEOUT:telemetry"):
        await StatusVerifierAgent()._bounded_verification_step(
            "telemetry", asyncio.sleep(1), default_timeout=0.01,
        )
    assert time.monotonic() - started < 0.2


def test_execution_readback_budget_covers_controller_convergence(monkeypatch):
    """The enclosing deadline cannot preempt the ONOS classifier poll."""
    monkeypatch.setenv("ONOS_FLOW_VERIFY_TIMEOUT_SECONDS", "73")
    monkeypatch.setenv("IDN_VERIFY_EXECUTION_READBACK_TIMEOUT_SECONDS", "35")
    assert StatusVerifierAgent._execution_readback_timeout_seconds() == 83.0

    monkeypatch.setenv("IDN_VERIFY_EXECUTION_READBACK_TIMEOUT_SECONDS", "120")
    assert StatusVerifierAgent._execution_readback_timeout_seconds() == 120.0
from api.int_analysis import build_healing_comparison
from api.store import reset_store
from models.agent_state import RuleVerificationReport
from models.flow_table import FlowTablePlan, MatchField, TableEntry, WriteRequest
from models.network import DeviceInfo, HostInfo, LinkInfo, NetworkSnapshot
from models.telemetry import KPIMetrics, TelemetryData
from models.yang_policy import (
    ActionRules,
    ActionType,
    MatchCriteria,
    NetworkPolicy,
    QoSParams,
    YANGPolicy,
)


# ═══════════════════════════════════════════════════════════════════════════════
# LLM Config
# ═══════════════════════════════════════════════════════════════════════════════

def test_llm_config_defaults_to_dashscope_qwen(monkeypatch):
    for name in ("LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_TEMPERATURE",
                 "LLM_MAX_TOKENS", "OPENAI_API_BASE", "OPENAI_BASE_URL",
                 "LOCAL_QWEN_MODEL", "LOCAL_QWEN_BASE_URL",
                 "WANGTIAO2026_API_KEY", "DASHSCOPE_API_KEY",
                 "OPENAI_API_KEY", "LOCAL_QWEN_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "")

    config = load_llm_config()
    assert config.provider == "openai"
    assert config.model == "qwen-plus"
    assert config.temperature == 0.0


def test_llm_config_prefers_wangtiao_api_key(monkeypatch):
    monkeypatch.setenv("WANGTIAO2026_API_KEY", "dashscope-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secondary-key")
    config = load_llm_config()
    assert config.api_key == "dashscope-key"


# ═══════════════════════════════════════════════════════════════════════════════
# SafetyCouncil
# ═══════════════════════════════════════════════════════════════════════════════

def _safety_policy() -> YANGPolicy:
    return YANGPolicy(
        raw_intent="Guarantee video traffic.",
        confidence_score=0.93,
        network_policy=NetworkPolicy(
            policy_id="POL-SAFETY-001", policy_name="research-video-qos",
            match=MatchCriteria(source_ip="192.168.10.0/24", destination_ip="10.0.0.0/8",
                                protocol="UDP", destination_port=5004),
            action=ActionRules(action_type=ActionType.SET_PRIORITY,
                               qos_params=QoSParams(dscp_value=46, bandwidth=100, burst_size=1024)),
        ),
    )


def _safety_snapshot() -> NetworkSnapshot:
    return NetworkSnapshot(
        snapshot_id="SNAP-SAFETY-001",
        devices=[
            DeviceInfo(device_id="device:bmv2:1", status="AVAILABLE"),
            DeviceInfo(device_id="device:bmv2:2", status="AVAILABLE"),
        ],
        links=[LinkInfo(link_id="s1-s2", src_device="device:bmv2:1", src_port="2",
                        dst_device="device:bmv2:2", dst_port="1",
                        bandwidth_mbps=1000, state="ACTIVE")],
        hosts=[
            HostInfo(host_id="h_src", mac="00:00:00:00:00:01",
                     ip_addresses=["192.168.10.10"],
                     location_device="device:bmv2:1", location_port="1"),
            HostInfo(host_id="h_dst", mac="00:00:00:00:00:02",
                     ip_addresses=["10.0.0.10"],
                     location_device="device:bmv2:2", location_port="2"),
        ],
    )


def _safety_plan(egress_port: int) -> FlowTablePlan:
    return FlowTablePlan(
        plan_id="PLAN-SAFETY-001", intent_id="intent-safety-001",
        write_requests=[WriteRequest(
            device_id="device:bmv2:1",
            table_entries=[TableEntry(
                table_name="MyIngress.dscp_marker",
                match_fields=[MatchField(field_name="hdr.ipv4.srcAddr", match_type="lpm",
                                         value="192.168.10.0", prefix_len=24)],
                action_name="MyIngress.mark_dscp", priority=100,
                egress_port=egress_port,
            )],
        )],
    )


@pytest.mark.asyncio
async def test_safety_council_approves_valid_plan():
    report = await SafetyCouncilAgent().review(
        intent_id="intent-safety-001", yang_policy=_safety_policy(),
        flow_table_plan=_safety_plan(egress_port=2),
        network_snapshot=_safety_snapshot(),
        rule_report=RuleVerificationReport(is_valid=True),
    )
    assert report["overall_decision"] == "approve"
    assert report["risk_level"] == "LOW"


@pytest.mark.asyncio
async def test_safety_council_rejects_blackhole_egress():
    report = await SafetyCouncilAgent().review(
        intent_id="intent-safety-001", yang_policy=_safety_policy(),
        flow_table_plan=_safety_plan(egress_port=99),
        network_snapshot=_safety_snapshot(),
        rule_report=RuleVerificationReport(is_valid=True),
    )
    assert report["overall_decision"] == "reject"
    assert any("NO_BLACKHOLE" in blocker for blocker in report["hard_blockers"])


# ═══════════════════════════════════════════════════════════════════════════════
# RootCauseAnalyzer
# ═══════════════════════════════════════════════════════════════════════════════

def _rca_policy():
    return YANGPolicy(
        network_policy=NetworkPolicy(
            policy_id="POL-RCA-1", policy_name="root-cause-test",
            match=MatchCriteria(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32"),
            action=ActionRules(action_type=ActionType.SET_PRIORITY,
                               qos_params=QoSParams(bandwidth=30, dscp_value=46)),
        ),
    )


def _congested_telemetry():
    return TelemetryData(
        intent_id="INT-RCA", sample_count=3,
        kpi=KPIMetrics(latency_ms=35.2, packet_loss_rate=0.08,
                       throughput_mbps=8.7, queue_depth=42,
                       int_hop_count=5, int_max_queue_occupancy=12),
        raw_int_data={
            "status": "ok", "source": "onos_rest+mininet_ping+mininet_qdisc+p4_int",
            "raw": {
                "qdisc_probe": {"status": "ok", "congestion_detected": True,
                                "queue_depth": 42, "overlimits": 120,
                                "congested_interfaces": [
                                    {"node": "s_agg_r", "interface": "s_agg_r-eth3"}]},
                "int_probe": {"status": "ok", "hop_records": [
                    {"switch_name": "s_access_r", "queue_occupancy": 0},
                    {"switch_name": "s_agg_r", "next_node": "s_core1",
                     "link_id": "s_agg_r-s_core1", "queue_occupancy": 12,
                     "hop_latency_ns": 9_000_000},
                ]},
            },
        },
    )


def test_root_cause_analyzer_scores_primary_path_congestion():
    report = RootCauseAnalyzerAgent().analyze(
        telemetry=_congested_telemetry(),
        violations=["时延超限: 实际35.20ms > 约束10.0ms",
                      "带宽不足: 实际8.7Mbps < 约束28.5Mbps"],
        yang_draft=_rca_policy(),
        telemetry_source="onos_rest+mininet_ping+mininet_qdisc+p4_int",
    )
    assert report["primary_cause"] == "path_segment_congestion"
    assert report["bottleneck_link"] == "s_agg_r-s_core1"
    assert report["scope"] == "PATH_SPECIFIC"
    assert report["recommended_action"] == "candidate_action_evaluation_required"


@pytest.mark.asyncio
async def test_root_cause_analyzer_guarded_llm_mode(monkeypatch):
    monkeypatch.setenv("IDN_MOCK_MODE", "true")
    monkeypatch.setenv("IDN_ROOT_CAUSE_AI_MODE", "auto")

    report = await RootCauseAnalyzerAgent().analyze_with_ai(
        telemetry=_congested_telemetry(),
        violations=["时延超限: 实际35.20ms > 约束10.0ms"],
        yang_draft=_rca_policy(),
        telemetry_source="onos_rest+mininet_qdisc+p4_int",
    )
    assert report["ai_reasoning_mode"] == "guarded_llm"
    assert report["ai_assessment"]["selected_cause"] == "path_segment_congestion"


# ═══════════════════════════════════════════════════════════════════════════════
# ExperienceMemory
# ═══════════════════════════════════════════════════════════════════════════════

def _record_case(memory: ExperienceMemoryAgent) -> dict:
    return memory.record_case({
        "case_id": "CASE-VECTOR-001",
        "intent_type": "video_service_priority",
        "symptom": "primary path congestion",
        "evidence": ["qdisc overlimits increasing", "INT path still uses s_core1"],
        "root_cause": "primary_path_congestion",
        "repair_action": ["reroute_secondary_path", "throttle_low_priority_source"],
        "before_kpi": {"throughput_mbps": 8, "latency_ms": 40},
        "after_kpi": {"throughput_mbps": 30, "latency_ms": 3},
        "success": True,
        "lesson": "Prefer backup path when primary path congestion recurs.",
    })


def test_experience_memory_hybrid_retrieval(tmp_path, monkeypatch):
    monkeypatch.setenv("IDN_EXPERIENCE_RETRIEVAL_MODE", "hybrid")
    store = reset_store(tmp_path / "app.db")
    memory = ExperienceMemoryAgent(store=store)

    recorded = _record_case(memory)
    assert recorded["embedding_model"].startswith("local_hash_embedding_v1:")

    results = memory.retrieve_similar(
        intent_type="video_service_priority",
        symptoms=["throughput below target", "primary path congestion"],
        evidence=["qdisc overlimits increasing"],
        root_cause="primary_path_congestion",
    )
    assert results
    assert results[0]["case_id"] == "CASE-VECTOR-001"


def test_experience_memory_records_closed_loop_success(tmp_path):
    store = reset_store(tmp_path / "app.db")
    memory = ExperienceMemoryAgent(store=store)
    policy = YANGPolicy(
        raw_intent="Guarantee video traffic.",
        confidence_score=0.93,
        network_policy=NetworkPolicy(
            policy_id="POL-MEM-SUCCESS", policy_name="research-video-qos",
            match=MatchCriteria(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32",
                                protocol="UDP", destination_port=5004),
            action=ActionRules(action_type=ActionType.SET_PRIORITY,
                               qos_params=QoSParams(dscp_value=46, bandwidth=30, burst_size=1024)),
        ),
    )
    recorded = memory.record_from_verification(
        intent_id="INT-MEM-SUCCESS", yang_policy=policy,
        verification_report={
            "passed": True, "intent_achieved": True,
            "before_kpi": {"throughput_mbps": 12},
            "after_kpi": {"throughput_mbps": 30},
            "kpi": {"throughput_mbps": 30}, "violations": [],
        },
    )
    assert recorded is not None
    assert recorded["success"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# Healing Comparison
# ═══════════════════════════════════════════════════════════════════════════════

def test_healing_comparison_detects_backup_path_switch():
    before = {
        "passed": False,
        "kpi": {"latency_ms": 20.0, "throughput_mbps": 10.0, "int_max_queue_occupancy": 8},
        "int_analysis": {
            "switch_path": ["s_access_r", "s_agg_r", "s_core1", "s_agg_d", "s_access_d"],
            "primary_path": True, "secondary_path": False,
            "congestion_on_primary": True, "bottleneck_link": "s_agg_r-s_core1",
        },
    }
    after = {
        "passed": True,
        "kpi": {"latency_ms": 4.0, "throughput_mbps": 40.0, "int_max_queue_occupancy": 0},
        "int_analysis": {
            "switch_path": ["s_access_r", "s_agg_r", "s_core2", "s_agg_d", "s_access_d"],
            "primary_path": False, "secondary_path": True,
            "congestion_on_primary": False, "bottleneck_link": "s_agg_r-s_core2",
        },
    }
    comparison = build_healing_comparison(before, after, {"actions": []})
    assert comparison["path_changed"] is True
    assert comparison["switched_to_backup"] is True
    assert comparison["kpi_deltas"]["latency_ms"]["improved"] is True

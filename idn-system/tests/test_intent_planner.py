"""IntentPlanner unit tests — rule-based intent parsing, slot filling,
host alias resolution, Chinese text normalisation, and watcher context.

All tests use rule-fallback LLM to exercise the deterministic rule path.
"""

from __future__ import annotations

import json
import os

import pytest
from agents.intent_planner import INTENT_PLANNER_SYSTEM_PROMPT, IntentPlannerAgent
from agents.policy_conflict_checker import PolicyConflictCheckerAgent
from agents.policy_modeling import PolicyModelingAgent
from models.agent_state import GlobalAgentState
from models.intent import IntentStatus, UserIntent
from models.intent_plan import IntentPlan, SubIntent
from models.network import NetworkSnapshot
from orchestrator.nodes import ThinkNode

DEFAULT_INTENT = (
    "Guarantee 100Mbps video traffic from 192.168.10.0/24 " "to 10.0.0.0/8."
)


# ── Test fixtures (autouse handled by conftest.mock_env) ─────────────────────


def _watcher_healing_context() -> dict:
    return {
        "triggered_by": "kpi_watcher",
        "incident_id": "INC-watch-test",
        "target_flow": {
            "source": "h_research",
            "destination": "h_dc",
            "source_ip": "192.168.10.10",
            "destination_ip": "10.0.0.10",
        },
        "self_healing": {
            "enabled": True,
            "autonomous": True,
            "reason": "queue_overlimits_delta=7716.0; throughput below target",
            "repair_actions": [
                "reroute_secondary_path",
                "throttle_low_priority_source",
            ],
            "target_path": [
                "h_research",
                "s_access_r",
                "s_agg_r",
                "s_core2",
                "s_agg_d",
                "s_access_d",
                "h_dc",
            ],
            "low_priority_source": {
                "host": "h_video",
                "source_ip": "192.168.10.20",
                "destination_ip": "10.0.0.0/8",
                "limit_mbps": 10,
            },
            "min_throughput_mbps": 20,
        },
    }


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_basic_intent(rule_fallback_llm):
    """Basic intent → one SET_PRIORITY sub-intent with video defaults."""
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(DEFAULT_INTENT)
    assert plan.raw_intent == DEFAULT_INTENT
    assert plan.sub_intents[0].intent_type == "SET_PRIORITY"
    assert plan.sub_intents[0].action["qos_params"]["bandwidth"] == 100


@pytest.mark.asyncio
async def test_resolve_host_aliases(rule_fallback_llm):
    """h_research → 192.168.10.10/32, h_dc → 10.0.0.10/32."""
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "Guarantee research video bandwidth from h_research to h_dc at least 20Mbps"
    )
    item = plan.sub_intents[0]
    assert item.match["source_ip"] == "192.168.10.10/32"
    assert item.match["destination_ip"] == "10.0.0.10/32"
    assert item.action["qos_params"]["bandwidth"] == 20
    assert plan.clarification_questions == []


@pytest.mark.asyncio
async def test_compound_endpoint_evidence_survives_llm_child_truncation():
    """Rule scope must survive a two-child LLM plan with endpoint-free text."""
    planner = IntentPlannerAgent()
    raw = "对h_backup到h_research的备份数据业务，既保证最低带宽30Mbps，又限制最大带宽不超过10Mbps"
    rule_plan = planner._rule_intent_plan(raw, None, None)
    assert len(rule_plan.sub_intents) == 2
    assert [item.intent_type for item in rule_plan.sub_intents] == [
        "SET_PRIORITY",
        "RATE_LIMIT",
    ]

    # This mirrors the real provider shape: the model recognizes two actions
    # but emits terse objectives without repeating the common endpoint scope.
    llm_plan = IntentPlan(
        raw_intent=raw,
        confidence_score=0.9,
        sub_intents=[
            SubIntent(
                sequence=1,
                objective="保证最低带宽30Mbps",
                intent_type="SET_PRIORITY",
                action={"action_type": "SET_PRIORITY", "qos_params": {"bandwidth": 30}},
                constraints={},
            ),
            SubIntent(
                sequence=2,
                objective="限制最大带宽不超过10Mbps",
                intent_type="RATE_LIMIT",
                action={"action_type": "RATE_LIMIT", "qos_params": {"bandwidth": 10}},
                constraints={},
            ),
        ],
    )
    final = planner._merge_plans(llm_plan, rule_plan, raw)

    assert len(final.sub_intents) == 2
    for item in final.sub_intents:
        assert item.match["source_ip"] == "10.0.0.20/32"
        assert item.match["destination_ip"] == "192.168.10.10/32"
        assert item.constraints["endpoint_resolution"]["source"]["status"] == "RESOLVED"
        assert item.constraints["endpoint_resolution"]["destination"]["status"] == "RESOLVED"
    assert final.clarification_questions == []

    # Continue through the same semantic artifacts consumed by the real API:
    # IntentPlan -> modeled policies -> intrinsic conflict gate.  The test is
    # deliberately independent of capacity and southbound adapters: an empty
    # interval is a semantic rejection and must short-circuit before either.
    policies = await PolicyModelingAgent().model_all(final)
    conflict = PolicyConflictCheckerAgent().intrinsic_qos_interval_conflict(policies)
    assert conflict is not None
    assert conflict["conflict_type"] == "INFEASIBLE_INTERVAL"
    assert conflict["minimum_mbps"] == 30
    assert conflict["maximum_mbps"] == 10


@pytest.mark.asyncio
async def test_resolve_ai_service_endpoints_without_role_cross_assignment(
    rule_fallback_llm,
):
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "Guarantee AI data from h_ai to h_dc TCP/5202 at least 20Mbps DSCP 18"
    )
    item = plan.sub_intents[0]
    assert item.match["source_ip"] == "192.168.20.10/32"
    assert item.match["destination_ip"] == "10.0.0.10/32"
    assert item.match["protocol"] == "TCP"
    assert item.match["destination_port"] == 5202
    assert item.service_id == "ai_data"


@pytest.mark.asyncio
async def test_chinese_video_service_fills_udp_defaults(rule_fallback_llm):
    """Chinese input → protocol=UDP, dport=5004, service=video injected."""
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "保障科研楼 192.168.10.10 到数据中心 10.0.0.10 的视频业务带宽不低于 30Mbps"
    )
    item = plan.sub_intents[0]
    assert item.match["source_ip"] == "192.168.10.10/32"
    assert item.match["destination_ip"] == "10.0.0.10/32"
    assert item.match["protocol"] == "UDP"
    assert item.match["destination_port"] == 5004
    assert item.action["qos_params"]["bandwidth"] == 30


@pytest.mark.asyncio
async def test_slot_filling_vague_input_triggers_clarification(rule_fallback_llm):
    """Vague input → low confidence + clarification questions."""
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan("限制流量")
    assert plan.confidence_score <= 0.65
    assert any("源" in q for q in plan.clarification_questions)
    assert any("目的" in q for q in plan.clarification_questions)
    slot = plan.sub_intents[0].constraints["slot_filling"]
    assert set(slot["missing_slots"]) >= {"source_ip", "destination_ip", "bandwidth"}


@pytest.mark.asyncio
async def test_destination_only_drop_is_a_complete_directional_scope(rule_fallback_llm):
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan("阻断 10.0.0.50 流量")

    item = plan.sub_intents[0]
    assert item.action["action_type"] == "DROP"
    assert item.match["destination_ip"] == "10.0.0.50/32"
    assert not plan.clarification_questions
    assert (item.constraints.get("slot_filling") or {}).get("missing_slots", []) == []


@pytest.mark.asyncio
async def test_slot_filling_uses_clarification_context(rule_fallback_llm):
    """Clarification answer → filled slots without re-asking."""
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "限制流量",
        context={
            "clarifications": [
                {"answer": "限制 192.168.10.0/24 到 10.0.0.0/8 的所有流量不超过20Mbps"}
            ]
        },
    )
    item = plan.sub_intents[0]
    assert len(plan.sub_intents) == 1
    assert item.intent_type == "RATE_LIMIT"
    assert item.match["source_ip"] == "192.168.10.0/24"
    assert item.match["destination_ip"] == "10.0.0.0/8"
    assert item.action["qos_params"]["bandwidth"] == 20


@pytest.mark.asyncio
async def test_normalize_cidr_gbps(rule_fallback_llm):
    """CIDR /32 normalisation, Gbps→Mbps, video defaults."""
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "保障 192.168.10.100 到 10.0.0.50 的视频会议带宽不低于 10Gbps"
    )
    item = plan.sub_intents[0]
    assert item.match["source_ip"] == "192.168.10.100/32"
    assert item.match["destination_ip"] == "10.0.0.50/32"
    assert item.match["protocol"] == "UDP"
    assert item.action["qos_params"]["bandwidth"] == 10000


@pytest.mark.asyncio
async def test_watcher_context_builds_autonomous_plan(rule_fallback_llm):
    """Watcher self-healing context → autonomous plan with target_path."""
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "Watcher detected abnormal KPI for h_research to h_dc. Diagnose congestion.",
        context=_watcher_healing_context(),
    )
    item = plan.sub_intents[0]
    assert plan.confidence_score >= 0.9
    assert item.intent_type == "SET_PRIORITY"
    assert item.constraints["autonomous_self_healing"] is True
    assert "s_core2" in item.constraints["target_path"]


@pytest.mark.asyncio
async def test_watcher_context_never_uses_service_profile_endpoints_as_fallback(
    rule_fallback_llm,
):
    """An execution-capable watcher event must carry its observed selector."""
    context = _watcher_healing_context()
    context["target_flow"].pop("source_ip")
    context["target_flow"].pop("destination_ip")
    context["target_flow"]["service_id"] = "research_video"

    with pytest.raises(ValueError, match="target_flow.source_ip"):
        await IntentPlannerAgent(llm=rule_fallback_llm).plan(
            "Watcher detected abnormal KPI.", context=context
        )


@pytest.mark.asyncio
async def test_think_node_watcher_no_pause():
    """Watcher context → ThinkNode does NOT pause for clarification."""
    state = GlobalAgentState(
        intent_id="INT-WATCHER",
        user_intent=UserIntent(
            text="Watcher detected abnormal KPI.",
            user_id="watcher",
            context=_watcher_healing_context(),
        ),
        network_snapshot=NetworkSnapshot(),
    )
    state = await ThinkNode().run(state)
    assert state.needs_clarification is False
    assert state.clarification_question is None


@pytest.mark.asyncio
async def test_think_node_no_pause_on_resolved_conflict(monkeypatch):
    """Parse warning + critic_report → ThinkNode proceeds without pause."""

    async def fake_plan(self, user_input, network_snapshot=None, context=None):
        return IntentPlan(
            raw_intent=user_input,
            confidence_score=0.65,
            parse_warnings=["LLM 与规则解析器在 bandwidth 上存在冲突，已采用规则值。"],
            critic_report={
                "conflict": True,
                "missing_slots": [],
                "final_authority": "schema_validator_and_rule_extractor",
            },
            sub_intents=[
                SubIntent(
                    sequence=1,
                    objective="保障视频业务",
                    intent_type="SET_PRIORITY",
                    match={
                        "source_ip": "192.168.10.0/24",
                        "destination_ip": "10.0.0.0/8",
                        "protocol": "UDP",
                        "destination_port": 5004,
                    },
                    action={
                        "action_type": "SET_PRIORITY",
                        "qos_params": {
                            "dscp_value": 46,
                            "bandwidth": 10000,
                            "burst_size": 1024,
                        },
                    },
                    constraints={"policy_name": "video-qos"},
                    priority=100,
                )
            ],
        )

    monkeypatch.setattr(IntentPlannerAgent, "plan", fake_plan)
    state = GlobalAgentState(
        intent_id="INT-RESOLVED",
        user_intent=UserIntent(text="保障科研大楼到数据中心的视频业务带宽不低于10Gbps"),
        network_snapshot=NetworkSnapshot(),
    )
    state = await ThinkNode().run(state)
    assert state.needs_clarification is False
    assert state.yang_draft.network_policy.action.qos_params.bandwidth == 10000


@pytest.mark.asyncio
async def test_service_selector_mismatch_pauses_for_clarification(monkeypatch):
    """An inferred incompatible profile requires user input, not FAILED."""
    from agents.policy_modeling import PolicyModelingAgent
    from api.service_profiles import ValidationProfileMismatch

    async def fake_plan(self, user_input, network_snapshot=None, context=None):
        return IntentPlan(
            raw_intent=user_input,
            confidence_score=0.8,
            sub_intents=[
                SubIntent(
                    sequence=1,
                    objective="bandwidth guarantee",
                    intent_type="SET_PRIORITY",
                    match={
                        "source_ip": "192.168.10.10/32",
                        "destination_ip": "10.0.0.10/32",
                        "protocol": "UDP",
                        "destination_port": 5004,
                    },
                    action={"action_type": "SET_PRIORITY", "qos_params": {"bandwidth": 10}},
                    service_id="network_health_research_dc",
                )
            ],
        )

    async def incompatible_profile(self, intent_plan, network_snapshot=None):
        raise ValidationProfileMismatch("cannot attach network health; selector mismatch: protocol")

    monkeypatch.setattr(IntentPlannerAgent, "plan", fake_plan)
    monkeypatch.setattr(PolicyModelingAgent, "model_all", incompatible_profile)
    state = GlobalAgentState(
        intent_id="INT-SERVICE-MISMATCH",
        user_intent=UserIntent(text="Guarantee traffic from A to B above 10Mbps"),
        network_snapshot=NetworkSnapshot(),
    )

    state = await ThinkNode().run(state)

    assert state.current_status == IntentStatus.PENDING_CLARIFICATION
    assert state.needs_clarification is True
    assert state.yang_draft is None
    assert state.yang_policies == []
    assert state.intent_plan.artifact_type == "CANDIDATE_INTERPRETATION"
    assert state.intent_plan.plan_id is None
    assert state.clarification_question
    assert state.clarification_questions[0]["reason"] == "SERVICE_SELECTOR_MISMATCH"

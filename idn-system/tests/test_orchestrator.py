"""Orchestrator layer tests — LangGraph state machine node-level logic.

Full-workflow tests (run_intent_workflow) require MCP/ONOS infrastructure
and are covered by test_integration_onos_flow_delivery.py.
"""

from __future__ import annotations

import pytest

from agents.intent_planner import IntentPlannerAgent
from agents.policy_conflict_checker import PolicyConflictCheckerAgent
from agents.policy_modeling import PolicyModelingAgent
from models.agent_state import GlobalAgentState
from models.intent import IntentStatus, UserIntent
from models.intent_plan import IntentPlan, SubIntent
from models.network import NetworkSnapshot
from orchestrator.graph import route_after_verification
from orchestrator.nodes import ThinkNode, VerifyStatusNode


# ── Graph Routing ────────────────────────────────────────────────────────────

def test_route_after_verification_success_ends():
    """When verification passed and status is SUCCESS, route to __end__."""
    from models.yang_policy import (
        YANGPolicy, NetworkPolicy, ActionRules, ActionType, MatchCriteria,
    )
    state = GlobalAgentState(
        intent_id="INT-ROUTE", user_intent=UserIntent(text="test"),
        network_snapshot=NetworkSnapshot(),
        verification_passed=True,
        yang_draft=YANGPolicy(
            network_policy=NetworkPolicy(
                policy_id="P1", policy_name="test",
                match=MatchCriteria(source_ip="10.0.0.0/8"),
                action=ActionRules(action_type=ActionType.SET_PRIORITY),
            ),
        ),
    )
    state.current_status = IntentStatus.SUCCESS
    assert route_after_verification(state) == "__end__"


def test_multi_policy_transition_routes_to_conflict_check_without_old_execution():
    """The next policy must never enter status verification with prior state cleared."""
    from models.yang_policy import (
        YANGPolicy, NetworkPolicy, ActionRules, ActionType, MatchCriteria,
    )

    first = YANGPolicy(
        network_policy=NetworkPolicy(
            policy_id="P1", policy_name="first",
            match=MatchCriteria(source_ip="192.168.10.10/32"),
            action=ActionRules(action_type=ActionType.SET_PRIORITY),
        ),
    )
    second = first.model_copy(deep=True)
    second.network_policy.policy_id = "P2"
    state = GlobalAgentState(
        intent_id="INT-MULTI-ROUTE", user_intent=UserIntent(text="two services"),
        network_snapshot=NetworkSnapshot(), yang_policies=[first, second], yang_draft=first,
        current_policy_index=0, execution_result={"status": "SUCCESS"},
        verification_passed=True,
    )

    assert VerifyStatusNode()._prepare_next_policy(state) is True
    state.current_status = IntentStatus.ACTING
    state.next_action = "PROCESS_NEXT_POLICY"

    assert state.execution_result is None
    assert route_after_verification(state) == "check_conflict"


def test_route_after_inconclusive_measurement_never_recompiles_same_policy():
    """Missing business telemetry must not create a ReAct loop."""
    state = GlobalAgentState(
        intent_id="INT-MEASUREMENT-INCONCLUSIVE",
        user_intent=UserIntent(text="protect a video flow"),
        current_status=IntentStatus.VERIFYING,
        verification_passed=False,
        verification_report={"verification_state": "MEASUREMENT_INCONCLUSIVE"},
    )

    assert route_after_verification(state) == "__end__"
    assert state.current_status == IntentStatus.DEGRADED
    assert state.measurement_status == "INCONCLUSIVE"
    assert state.verification_report["route_decision"] == "END_AWAITING_VALID_MEASUREMENT"
    assert state.react_state.retry_count == 0


# ── ThinkNode → Multi-Policy ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_think_node_expands_multi_sub_intents(monkeypatch):
    """Two sub-intents → two YANG policies, with pointer advancing logic."""
    async def fake_plan(self, user_input, network_snapshot=None, context=None):
        return IntentPlan(
            raw_intent=user_input, confidence_score=0.95,
            sub_intents=[
                SubIntent(sequence=1, objective="Video QoS", intent_type="SET_PRIORITY",
                          match={"source_ip": "192.168.10.0/24", "destination_ip": "10.0.0.0/8",
                                 "destination_port": 5004, "protocol": "UDP"},
                          action={"action_type": "SET_PRIORITY",
                                  "qos_params": {"dscp_value": 46, "bandwidth": 100, "burst_size": 1024}},
                          constraints={"policy_name": "video-qos"}, priority=100),
                SubIntent(sequence=2, objective="Block host", intent_type="DROP",
                          match={"source_ip": "192.168.10.100/32", "destination_ip": "10.0.0.50/32",
                                 "protocol": "ANY"},
                          action={"action_type": "DROP"},
                          constraints={"policy_name": "block-host"}, priority=50),
            ],
        )
    monkeypatch.setattr(IntentPlannerAgent, "plan", fake_plan)

    state = GlobalAgentState(
        intent_id="INT-MULTI",
        user_intent=UserIntent(text="Video QoS and block host."),
        network_snapshot=NetworkSnapshot(),
    )
    state = await ThinkNode().run(state)
    assert len(state.yang_policies) == 2
    assert state.current_policy_index == 0
    assert state.yang_policies[0].network_policy.action.action_type.value == "SET_PRIORITY"
    assert state.yang_policies[1].network_policy.action.action_type.value == "DROP"

    # Advance to next policy
    state.verification_passed = True
    state.error_message = "previous error"
    advanced = VerifyStatusNode()._prepare_next_policy(state)
    assert advanced is True
    assert state.current_policy_index == 1
    assert state.verification_passed is None
    assert state.error_message is None


# ── Low-confidence Conflict Check ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conflict_checker_low_confidence_warns():
    """Low confidence + resolved conflict → warning but no hard block."""
    plan = IntentPlan(
        raw_intent="保障科研大楼到数据中心的视频业务带宽不低于10Gbps",
        confidence_score=0.65,
        parse_warnings=["LLM 与规则解析器在 bandwidth 上存在冲突，已采用规则值。"],
        sub_intents=[SubIntent(
            sequence=1, objective="保障视频业务", intent_type="SET_PRIORITY",
            match={"source_ip": "192.168.10.0/24", "destination_ip": "10.0.0.0/8",
                   "protocol": "UDP", "destination_port": 5004},
            action={"action_type": "SET_PRIORITY",
                    "qos_params": {"dscp_value": 46, "bandwidth": 10000, "burst_size": 1024}},
            constraints={"policy_name": "video-qos"}, priority=100,
        )],
    )
    yang_policy = await PolicyModelingAgent().model(plan)
    report = await PolicyConflictCheckerAgent().check(yang_policy, NetworkSnapshot())
    assert report.has_conflict is False
    assert report.approval_token
    assert any(item["conflict_type"] == "LOW_CONFIDENCE_INTENT" for item in report.warning_items)

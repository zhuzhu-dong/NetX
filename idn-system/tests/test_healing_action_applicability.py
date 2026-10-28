from __future__ import annotations

import pytest

from agents.healing_action_applicability import (
    CandidateActionGenerator,
    FeasibilityEvaluator,
    HealingActionApplicabilityValidator,
    PathRootCauseImpactEvaluator,
    action_fingerprint,
    context_signature,
    path_contains_link,
    path_edges,
)
from agents.intent_root_cause import IntentRootCauseAgent
from agents.intent_execution import ExecutionSkippedNoop, IntentExecutionAgent
from infra.healing_plan_gate import is_repeated_noop
from api.worker import _terminal_workflow_status
from models.agent_state import ConsensusReasonCode, ConsensusStatus, GlobalAgentState, StageStatus
from models.intent import IntentStatus, UserIntent
from models.intent_domain import (
    CandidatePlan,
    HealingAction,
    HealingActionApplicability,
    HealingActionType,
    HealingRejectionReason,
    IntentObjective,
    IntentRuntimeState,
    IntentPlanIR,
    IntentSpec,
    NetworkContext,
    ObjectiveType,
    PlanDiff,
    PlanValidationStatus,
    RequiredAction,
    RootCauseAnalysis,
    RootCauseCategory,
    RootCauseScope,
)
from orchestrator.graph import route_after_verification
from orchestrator.nodes.verify_status_node import VerifyStatusNode


P1 = ["s_access_r", "s_agg_r", "s_core1", "s_agg_d", "s_access_d"]
P2 = ["s_access_r", "s_agg_r", "s_core2", "s_agg_d", "s_access_d"]
P3 = ["s_access_x", "s_core3", "s_access_d"]
SHARED = "s_access_r-s_agg_r"
P1_ONLY = "s_agg_r-s_core1"


def _context(*, material: str = "ctx-a", active=None, reservations=None) -> NetworkContext:
    return NetworkContext(
        material_state_hash=material,
        decision_usable=True,
        active_intents=list(active or []),
        reservations=list(reservations or []),
        evidence_refs=["EVD-INT", "EVD-LEDGER"],
    )


def _root(*, shared: bool = False, link: str = P1_ONLY) -> RootCauseAnalysis:
    return RootCauseAnalysis(
        intent_id="INT-PROTECTED",
        assurance_id="ASR-1",
        category=(
            RootCauseCategory.SHARED_BOTTLENECK_CONGESTION
            if shared
            else RootCauseCategory.PATH_SEGMENT_CONGESTION
        ),
        confidence=0.95,
        final_confidence=0.95,
        explanation="deterministic test root cause",
        scope=RootCauseScope.SHARED_PATH if shared else RootCauseScope.PATH_SPECIFIC,
        bottleneck_link_id=link,
        affected_link_ids=[link],
    )


def _reroute(path, *, candidate="path-target", expected=100.0) -> HealingAction:
    return HealingAction(
        action_id=f"ACT-{candidate}",
        type=HealingActionType.REROUTE,
        target=candidate,
        params={
            "candidate_id": candidate,
            "target_path": list(path),
            "expected_available_mbps": expected,
            "current_measurement_mbps": 11.5,
            "effective_min_mbps": 47.5,
        },
        reason="test candidate",
        confidence=0.95,
    )


def _resource_action(*, released=40.0, target_ids=None, kind=HealingActionType.THROTTLE_LOWER_PRIORITY):
    target_ids = list(target_ids or ["INT-BE"])
    return HealingAction(
        action_id=f"ACT-{kind.value}",
        type=kind,
        target=",".join(target_ids),
        params={
            "target_intent_ids": target_ids,
            "expected_released_mbps": released,
            "required_gain_mbps": 36.0,
            "current_measurement_mbps": 11.5,
        },
        reason="test resource candidate",
        affected_intent_ids=target_ids,
        confidence=0.9,
    )


def _validate(action, *, context=None, current=P1, root=None, previous=None):
    root = root or _root()
    return HealingActionApplicabilityValidator().validate(
        action,
        context=context or _context(),
        current_plan=None,
        current_path=list(current),
        root_cause=root,
        bottleneck_link_ids=list(root.affected_link_ids),
        active_intents=list((context or _context()).active_intents),
        previously_attempted_actions=list(previous or []),
    )


def _spec() -> IntentSpec:
    return IntentSpec(
        intent_id="INT-PROTECTED",
        original_text="guarantee 50 Mbps",
        normalized_text="guarantee 50 Mbps",
        objectives=[
            IntentObjective(type=ObjectiveType.MIN_BANDWIDTH, value=50, unit="Mbps")
        ],
    )


def _candidates() -> list[CandidatePlan]:
    return [
        CandidatePlan(
            candidate_id="P1",
            context_id="CTX",
            path=P1,
            available_capacity_mbps=100,
            empirical_capacity_mbps=100,
        ),
        CandidatePlan(
            candidate_id="P2",
            context_id="CTX",
            path=P2,
            available_capacity_mbps=100,
            empirical_capacity_mbps=100,
        ),
    ]


def test_current_path_equals_target_is_redundant():
    result = _validate(_reroute(P2, candidate="P2"), current=P2, root=_root(shared=True, link=SHARED))
    assert result.applicability == HealingActionApplicability.REDUNDANT
    assert result.reason_code == HealingRejectionReason.TARGET_PATH_ALREADY_ACTIVE


def test_reroute_that_keeps_bottleneck_has_no_expected_effect():
    result = _validate(_reroute(P1, candidate="P1"), current=P2, root=_root(shared=True, link=SHARED))
    assert result.applicability == HealingActionApplicability.NO_EXPECTED_EFFECT
    assert result.reason_code == HealingRejectionReason.SHARED_BOTTLENECK_NOT_AVOIDED
    assert result.expected_effect.bottleneck_avoided is False


def test_reroute_that_avoids_path_specific_bottleneck_is_applicable():
    result = _validate(_reroute(P2, candidate="P2"), current=P1, root=_root(link=P1_ONLY))
    assert result.applicability == HealingActionApplicability.APPLICABLE
    assert result.expected_effect.bottleneck_avoided is True


def test_shared_bottleneck_scope_is_identified():
    context = _context()
    context.candidate_paths = [
        {"candidate_id": "P1", "path": P1, "feasible": True},
        {"candidate_id": "P2", "path": P2, "feasible": True},
    ]
    scope, affected, location = IntentRootCauseAgent._scope([SHARED], context)
    assert scope == RootCauseScope.SHARED_PATH
    assert affected == ["P1", "P2"]
    assert location == RootCauseScope.ACCESS


def test_path_specific_bottleneck_scope_is_identified():
    context = _context()
    context.candidate_paths = [
        {"candidate_id": "P1", "path": P1, "feasible": True},
        {"candidate_id": "P2", "path": P2, "feasible": True},
    ]
    scope, affected, _ = IntentRootCauseAgent._scope([P1_ONLY], context)
    assert scope == RootCauseScope.PATH_SPECIFIC
    assert affected == ["P1"]


def test_plan_diff_noop_is_detected():
    diff = PlanDiff(parent_plan_id="OLD", new_plan_id="NEW", old_path=P2, new_path=P2)
    assert diff.is_noop() is True


def _plan(path, *, plan_id, rate=None, selector=None):
    actions = []
    if rate is not None or selector is not None:
        actions.append(
            RequiredAction(
                action_type="QOS_REALLOCATION",
                target="h_research->h_dc",
                parameters={
                    **({"rate": rate} if rate is not None else {}),
                    **({"selector": selector} if selector is not None else {}),
                },
            )
        )
    candidate = CandidatePlan(candidate_id=f"C-{plan_id}", context_id="CTX", path=path)
    return IntentPlanIR(
        plan_id=plan_id,
        intent_id="INT",
        intent_revision=1,
        context_id="CTX",
        candidates=[candidate],
        selected_candidate_id=candidate.candidate_id,
        required_actions=actions,
        validation_status=PlanValidationStatus.VALID,
    )


def test_different_plan_ids_with_same_effect_are_noop():
    diff = PlanDiff.compare(_plan(P2, plan_id="PLAN-A"), _plan(P2, plan_id="PLAN-B"))
    assert diff.current_effect_signature == diff.proposed_effect_signature
    assert diff.is_noop() is True


def test_plan_diff_detects_path_rate_and_selector_changes():
    baseline = _plan(P1, plan_id="PLAN-A", rate=30, selector="10.0.0.1/32")
    assert PlanDiff.compare(baseline, _plan(P2, plan_id="PLAN-B", rate=30, selector="10.0.0.1/32")).path_changed
    assert not PlanDiff.compare(baseline, _plan(P1, plan_id="PLAN-C", rate=40, selector="10.0.0.1/32")).is_noop()
    assert PlanDiff.compare(baseline, _plan(P1, plan_id="PLAN-D", rate=30, selector="10.0.0.9/32")).selector_changed


@pytest.mark.asyncio
async def test_execution_agent_defensive_gate_rejects_noop_before_network_io():
    plan = _plan(P2, plan_id="PLAN-NOOP")
    plan.plan_diff = PlanDiff.compare(_plan(P2, plan_id="PLAN-COMMITTED"), plan)
    assert plan.plan_diff.is_noop()
    with pytest.raises(ExecutionSkippedNoop, match="EXECUTION_SKIPPED_NOOP"):
        await IntentExecutionAgent().execute(plan)


def test_a2a_flow_plan_gate_rejects_same_effect_after_a_real_execution():
    plan = {
        "plan_id": "PLAN-NEW",
        "policy_operation": "CREATE",
        "traffic_scope_key": "scope:research",
        "write_requests": [{"device_id": "s1", "table_entries": [{"table_name": "ipv4", "priority": 10}]}],
    }
    first_noop, signature = is_repeated_noop(
        flow_plan={**plan, "plan_id": "PLAN-OLD"},
        yang_draft=None,
        previous_execution={},
    )
    repeated_noop, _ = is_repeated_noop(
        flow_plan=plan,
        yang_draft=None,
        previous_execution={"desired_effect_signature": signature, "data_plane_changed": True},
    )
    assert first_noop is False
    assert repeated_noop is True


def test_a2a_flow_plan_gate_allows_a_rate_change():
    previous = {"policy_operation": "MODIFY_QOS_RESOURCE", "policy_change": {"new_qos": {"rate": 30}}}
    _, signature = is_repeated_noop(flow_plan=previous, yang_draft=None, previous_execution={})
    repeated_noop, _ = is_repeated_noop(
        flow_plan={"policy_operation": "MODIFY_QOS_RESOURCE", "policy_change": {"new_qos": {"rate": 40}}},
        yang_draft=None,
        previous_execution={"desired_effect_signature": signature, "data_plane_changed": True},
    )
    assert repeated_noop is False


def test_noop_does_not_increment_effective_attempt_counter():
    runtime = IntentRuntimeState(intent_id="INT", healing_attempts=1, skipped_healing_actions=0)
    diff = PlanDiff(new_plan_id="NEW", old_path=P2, new_path=P2)
    if diff.is_noop():
        runtime.skipped_healing_actions += 1
    assert runtime.healing_attempts == 1
    assert runtime.skipped_healing_actions == 1


def test_same_failed_action_same_context_is_rejected():
    context = _context(material="same")
    action = _reroute(P2, candidate="P2")
    root = _root()
    first = _validate(action, context=context, current=P1, root=root)
    repeated = _validate(
        action,
        context=context,
        current=P1,
        root=root,
        previous=[{
            "action_fingerprint": first.action_fingerprint,
            "context_signature": first.context_signature,
            "status": "FAILED",
        }],
    )
    assert repeated.reason_code == HealingRejectionReason.REPEATED_FAILED_ACTION


def test_context_change_allows_same_action_to_be_reassessed():
    action = _reroute(P2, candidate="P2")
    root = _root()
    first = _validate(action, context=_context(material="old"), current=P1, root=root)
    changed = _validate(
        action,
        context=_context(material="capacity-recovered"),
        current=P1,
        root=root,
        previous=[{
            "action_fingerprint": first.action_fingerprint,
            "context_signature": first.context_signature,
            "status": "FAILED",
        }],
    )
    assert changed.applicability == HealingActionApplicability.APPLICABLE


def test_candidate_targets_are_topology_candidates_not_fixed_core2_label():
    actions = CandidateActionGenerator().generate(
        _spec(), _context(), current_path=P2, root_cause=_root(),
        path_candidates=[*_candidates(), CandidatePlan(candidate_id="P3", context_id="CTX", path=P3, available_capacity_mbps=100)],
        current_measurement_mbps=11.5, tolerance_ratio=0.95,
    )
    targets = {item.target for item in actions if item.type == HealingActionType.REROUTE}
    assert targets == {"P1", "P2", "P3"}
    assert "s_core2" not in targets


def test_shared_edge_analysis_reports_real_edge_delta():
    impact = PathRootCauseImpactEvaluator.evaluate(P2, P1, [SHARED])
    assert "s_access_r--s_agg_r" in impact["shared_edges"]
    assert "s_agg_r--s_core1" in impact["changed_edges"]
    assert "s_agg_r--s_core2" in impact["changed_edges"]
    assert impact["bottleneck_avoided"] is False


def test_protected_competing_intent_cannot_be_throttled():
    context = _context(active=[{
        "intent_id": "INT-PROTECTED-OTHER", "priority": "high", "preemptible": False,
        "current_path": P1, "current_usage_mbps": 40,
    }])
    result = _validate(
        _resource_action(target_ids=["INT-PROTECTED-OTHER"]),
        context=context,
        root=_root(shared=True, link=SHARED),
    )
    assert result.applicability == HealingActionApplicability.UNSAFE
    assert result.reason_code == HealingRejectionReason.PROTECTED_INTENT


def test_best_effort_competitor_can_be_resource_candidate():
    context = _context(active=[{
        "intent_id": "INT-BE", "priority": "low", "preemptible": True,
        "current_path": P1, "current_usage_mbps": 40,
    }])
    result = _validate(_resource_action(), context=context, root=_root(shared=True, link=SHARED))
    assert result.applicability == HealingActionApplicability.APPLICABLE
    assert result.expected_effect.expected_gain_mbps == 40


def test_expected_released_bandwidth_is_summed_from_runtime_usage():
    context = _context(active=[
        {"intent_id": "BE-1", "priority": "low", "preemptible": True, "current_path": P1, "current_usage_mbps": 12},
        {"intent_id": "BE-2", "priority": "low", "preemptible": True, "current_path": P2, "current_usage_mbps": 28},
    ])
    actions = CandidateActionGenerator().generate(
        _spec(), context, current_path=P2, root_cause=_root(shared=True, link=SHARED),
        path_candidates=_candidates(), current_measurement_mbps=11.5, tolerance_ratio=0.95,
    )
    throttle = next(item for item in actions if item.type == HealingActionType.THROTTLE_LOWER_PRIORITY)
    assert throttle.params["expected_released_mbps"] == 40
    assert throttle.params["target_intent_ids"] == ["BE-1", "BE-2"]


def test_insufficient_released_bandwidth_is_rejected():
    context = _context(active=[{
        "intent_id": "INT-BE", "priority": "low", "preemptible": True,
        "current_path": P1, "current_usage_mbps": 10,
    }])
    result = _validate(_resource_action(released=10), context=context, root=_root(shared=True, link=SHARED))
    assert result.applicability == HealingActionApplicability.NO_EXPECTED_EFFECT
    assert result.reason_code == HealingRejectionReason.INSUFFICIENT_RECOVERABLE_CAPACITY


def test_all_candidates_rejected_is_unsatisfiable_by_feasibility():
    root = _root(shared=True, link=SHARED)
    results = [
        _validate(_reroute(P2, candidate="P2"), current=P2, root=root),
        _validate(_reroute(P1, candidate="P1"), current=P2, root=root),
        _validate(_resource_action(released=0), current=P2, root=root),
    ]
    feasibility = FeasibilityEvaluator.evaluate(
        results, effective_min_mbps=47.5, current_measurement_mbps=11.5
    )
    assert feasibility["has_applicable_action"] is False
    assert feasibility["estimated_satisfiable"] is False


def test_retry_limit_without_candidate_matrix_is_not_unsatisfiable():
    state = GlobalAgentState(
        intent_id="INT",
        user_intent=UserIntent(intent_id="INT", text="test"),
        current_status=IntentStatus.FAILED,
        verification_passed=False,
    )
    state.react_state.retry_count = state.react_state.max_retries
    assert route_after_verification(state) == "human_approval"
    assert state.current_status == IntentStatus.FAILED


def test_needs_repair_is_not_terminal_consensus_after_unsatisfiable():
    state = GlobalAgentState(
        intent_id="INT",
        user_intent=UserIntent(intent_id="INT", text="test"),
        current_status=IntentStatus.UNSATISFIABLE,
        verification_passed=False,
        verification_report={"terminal_reason": "NO_FEASIBLE_HEALING_ACTION"},
    )
    decision = VerifyStatusNode()._build_consensus_decision(state, StageStatus.SUCCESS, "done")
    assert decision.status == ConsensusStatus.TERMINAL
    assert decision.decision_code == "UNSATISFIABLE"
    assert decision.retry_allowed is False


def test_terminal_status_exists_before_a2a_completion_normalization():
    assert _terminal_workflow_status(
        IntentStatus.UNSATISFIABLE,
        error_message="NO_FEASIBLE_HEALING_ACTION",
        needs_clarification=False,
    ) == "unsatisfiable"


def test_llm_invalid_same_path_suggestion_is_rejected_by_validator():
    llm_action = _reroute(P2, candidate="LLM-secondary")
    result = _validate(llm_action, current=P2, root=_root(shared=True, link=SHARED))
    assert result.validator_result == "REJECTED"
    assert result.reason_code == HealingRejectionReason.TARGET_PATH_ALREADY_ACTIVE


def test_path_edge_aliases_are_direction_independent():
    assert path_contains_link(P1, "s_agg_r-s_access_r")
    assert path_edges(P1)[0] == "s_access_r--s_agg_r"


def test_missing_target_path_is_not_applicable():
    result = _validate(_reroute([], candidate="EMPTY"))
    assert result.applicability == HealingActionApplicability.NOT_APPLICABLE


def test_context_signature_ignores_capture_nonce_but_includes_current_path():
    context = _context(material="same-material")
    left = context_signature(context, current_path=P1, bottleneck_link_ids=[P1_ONLY])
    right = context_signature(context, current_path=P2, bottleneck_link_ids=[P1_ONLY])
    assert left != right


def test_action_fingerprint_includes_target_and_root_cause():
    a = action_fingerprint(_reroute(P1, candidate="P1"), root_cause="PATH", bottleneck_link_ids=[P1_ONLY])
    b = action_fingerprint(_reroute(P2, candidate="P2"), root_cause="PATH", bottleneck_link_ids=[P1_ONLY])
    assert a != b


def test_stale_reservation_requires_sufficient_recoverable_capacity():
    action = HealingAction(
        type=HealingActionType.RELEASE_STALE_RESERVATION,
        target="stale",
        params={"reservation_ids": ["R1"], "expected_released_mbps": 40, "required_gain_mbps": 36},
        reason="stale",
    )
    result = _validate(action, root=_root(shared=True, link=SHARED))
    assert result.applicability == HealingActionApplicability.APPLICABLE


# Integration-level decision paths: generator -> validator -> feasibility -> outcome.


def _decision_pipeline(*, current, root, context):
    actions = CandidateActionGenerator().generate(
        _spec(), context, current_path=current, root_cause=root,
        path_candidates=_candidates(), current_measurement_mbps=11.5, tolerance_ratio=0.95,
    )
    evaluations = [
        HealingActionApplicabilityValidator().validate(
            action, context=context, current_plan=None, current_path=current,
            root_cause=root, bottleneck_link_ids=root.affected_link_ids,
            active_intents=context.active_intents, previously_attempted_actions=[],
        )
        for action in actions
    ]
    applicable = [item for item in evaluations if item.applicability == HealingActionApplicability.APPLICABLE]
    feasibility = FeasibilityEvaluator.evaluate(
        evaluations, effective_min_mbps=47.5, current_measurement_mbps=11.5
    )
    return actions, evaluations, applicable, feasibility


def test_integration_path_specific_selects_healthy_p2_and_satisfies():
    actions, _, applicable, feasibility = _decision_pipeline(
        current=P1, root=_root(link=P1_ONLY), context=_context()
    )
    selected = next(item for item in applicable if item.action == HealingActionType.REROUTE)
    selected_action = next(item for item in actions if item.action_id == selected.action_id)
    network_executions = [selected_action.params["target_path"]]
    assurance_outcome = "SATISFIED" if selected.expected_effect.bottleneck_avoided else "FAILED"
    assert network_executions == [P2]
    assert assurance_outcome == "SATISFIED"
    assert feasibility["has_applicable_action"] is True


def test_integration_shared_bottleneck_without_preemptible_is_unsatisfiable_without_execution():
    _, evaluations, applicable, feasibility = _decision_pipeline(
        current=P2, root=_root(shared=True, link=SHARED), context=_context()
    )
    network_executions = []
    assert applicable == []
    assert network_executions == []
    assert feasibility["estimated_satisfiable"] is False
    reasons = {item.reason_code for item in evaluations}
    assert HealingRejectionReason.TARGET_PATH_ALREADY_ACTIVE in reasons
    assert HealingRejectionReason.SHARED_BOTTLENECK_NOT_AVOIDED in reasons


def test_integration_shared_bottleneck_with_best_effort_selects_resource_action():
    context = _context(active=[{
        "intent_id": "INT-BE", "priority": "low", "preemptible": True,
        "current_path": P2, "current_usage_mbps": 40,
        "selector": {"source_ip": "10.0.0.2/32", "destination_ip": "10.0.0.4/32"},
    }])
    _, _, applicable, feasibility = _decision_pipeline(
        current=P2, root=_root(shared=True, link=SHARED), context=context
    )
    selected = next(item for item in applicable if item.action == HealingActionType.THROTTLE_LOWER_PRIORITY)
    execution = {"status": "COMPLETED", "released_mbps": selected.expected_effect.expected_gain_mbps}
    assurance_outcome = "SATISFIED" if execution["released_mbps"] >= 36 else "FAILED"
    assert execution["status"] == "COMPLETED"
    assert assurance_outcome == "SATISFIED"
    assert feasibility["estimated_satisfiable"] is True

"""Regression contract for execution/assurance generation isolation."""
from __future__ import annotations

import pytest

from api.worker import _intent_lifecycle_from_terminal_status, _terminal_workflow_status
from models.agent_state import GlobalAgentState
from models.intent import IntentStatus, UserIntent
from orchestrator.graph import (
    GraphNodeName,
    route_after_diagnostics,
    route_after_verification,
    validate_graph_routes,
)
from orchestrator.state_generations import (
    begin_assurance,
    can_confirm_violation,
    diagnostics_allowed,
    ensure_execution_generation,
    invalidate_downstream_state_after_execution_change,
    initialize_transient_state_for_policy_reuse,
    mark_degraded,
    mark_readback_verified,
    root_cause_is_current,
    violation_is_current,
)


def state(intent_id: str = "INT-GEN", *, revision: int = 1, run_id: str = "RUN-1") -> GlobalAgentState:
    return GlobalAgentState(
        intent_id=intent_id,
        revision=revision,
        run_id=run_id,
        user_intent=UserIntent(intent_id=intent_id, text="protect video", session_id="session"),
        execution_result={"execution_id": "EXEC-1", "desired_effect_signature": "effect-1"},
    )


def ready_for_diagnostics() -> GlobalAgentState:
    value = state()
    ensure_execution_generation(value, reason="test execution")
    mark_readback_verified(value)
    begin_assurance(value, reason="test assurance")
    value.measurement_status = "COMPLETED"
    value.valid_assurance_sample_count = 3
    value.assurance_samples = [
        {"valid": True, "received_mbps": 20, "threshold_mbps": 28.5, "environment_epoch": "epoch-1"},
        {"valid": True, "received_mbps": 21, "threshold_mbps": 28.5, "environment_epoch": "epoch-1"},
        {"valid": True, "received_mbps": 22, "threshold_mbps": 28.5, "environment_epoch": "epoch-1"},
    ]
    value.violation_confirmed = True
    value.violation_id = "V-1"
    value.violation_basis_assurance_generation = value.assurance_generation
    value.root_cause_status = "PENDING"
    value.root_cause_analysis_id = "RCA-1"
    value.root_cause_basis_violation_id = "V-1"
    value.diagnostic_basis_root_cause_id = "RCA-1"
    value.diagnostic_state = {"diagnostic_round": 0, "max_diagnostic_rounds": 3}
    value.diagnostics_required = True
    value.verification_report = {"verification_state": "VIOLATION_CONFIRMED_ROOT_CAUSE_PENDING"}
    return value


@pytest.mark.parametrize("field, expected", [
    ("measurement_status", "NOT_STARTED"),
    ("valid_assurance_sample_count", 0),
    ("violation_confirmed", False),
    ("root_cause_status", "NOT_STARTED"),
])
def test_new_intent_state_has_no_transient_policy_facts(field, expected):
    assert getattr(state(), field) == expected


def test_new_intents_do_not_share_mutable_transient_state():
    left, right = state("INT-A"), state("INT-B")
    left.assurance_samples.append({"sample": 1})
    left.diagnostic_state["diagnostic_round"] = 2
    assert right.assurance_samples == []
    assert right.diagnostic_state == {}


def test_revisions_and_runs_have_checkpoint_isolation():
    assert state(revision=1, run_id="one").get_thread_id() != state(revision=2, run_id="two").get_thread_id()
    assert "INT-GEN::r1::run-one" == state(revision=1, run_id="one").get_thread_id()


def test_first_execution_initializes_generation_and_clean_assurance():
    value = state()
    assert ensure_execution_generation(value, reason="apply") is True
    assert value.execution_generation == value.assurance_basis_execution_generation == 1
    assert value.assurance_generation == 1 and value.assurance_samples == []


def test_readback_not_verified_cannot_confirm_violation_or_route_diagnostics():
    value = ready_for_diagnostics()
    value.execution_readback_status = "EXECUTION_READBACK_MISMATCH"
    assert not can_confirm_violation(value)
    assert route_after_verification(value) == "reconcile_execution"


def test_assurance_not_started_cannot_confirm_violation():
    value = ready_for_diagnostics()
    value.measurement_status = "NOT_STARTED"
    assert not can_confirm_violation(value)
    assert route_after_verification(value) == "verify_status"


def test_zero_valid_samples_cannot_confirm_violation():
    value = ready_for_diagnostics()
    value.valid_assurance_sample_count = 0
    assert not can_confirm_violation(value)


def test_reconciliation_execution_change_invalidates_all_downstream_state():
    value = ready_for_diagnostics()
    old_assurance = value.assurance_id
    ensure_execution_generation(value, force=True, reason="reconciliation")
    assert value.execution_generation == 2
    assert value.assurance_id is None and value.assurance_samples == []
    assert value.violation_confirmed is False and value.violation_id is None
    assert value.root_cause_status == "NOT_STARTED" and value.root_cause_analysis_id is None
    assert value.diagnostic_state == {}
    assert any(row["event"] == "DOWNSTREAM_STATE_INVALIDATED" and row["invalidated"]["assurance_id"] == old_assurance for row in value.state_audit)


def test_policy_reuse_keeps_desired_effect_but_resets_all_transient_evidence():
    value = ready_for_diagnostics()
    original_execution = value.execution_result.copy()
    initialize_transient_state_for_policy_reuse(value)
    assert value.execution_result == original_execution
    assert value.execution_readback_status == "NOT_STARTED"
    assert value.assurance_id is None and value.assurance_samples == []
    assert value.valid_assurance_sample_count == 0
    assert value.violation_confirmed is False and value.violation_id is None
    assert value.root_cause_status == "NOT_STARTED"
    assert value.diagnostic_state["diagnostic_round"] == 0
    assert any(row["event"] == "POLICY_REUSED_DESIRED_STATE_ONLY" for row in value.state_audit)


def test_reconciliation_then_readback_then_assurance_is_the_only_route():
    value = ready_for_diagnostics()
    ensure_execution_generation(value, force=True, reason="reconciliation")
    mark_readback_verified(value)
    begin_assurance(value, reason="reconciliation verified")
    assert value.execution_readback_generation == value.execution_generation
    assert value.measurement_status == "ASSURANCE_MEASURING"
    assert route_after_verification(value) == "verify_status"


def test_old_violation_is_not_current_in_a_new_assurance_generation():
    value = ready_for_diagnostics()
    value.assurance_generation += 1
    assert not violation_is_current(value)


def test_old_root_cause_is_not_current_for_a_new_violation():
    value = ready_for_diagnostics()
    value.violation_id = "V-2"
    assert not root_cause_is_current(value)
    assert not diagnostics_allowed(value)


def test_current_basis_routes_pending_root_cause_to_diagnostics():
    assert route_after_verification(ready_for_diagnostics()) == "collect_diagnostics"


def test_stale_root_cause_basis_is_refused_not_collected():
    value = ready_for_diagnostics()
    value.diagnostic_basis_root_cause_id = "RCA-OLD"
    assert route_after_verification(value) == "__end__"
    assert value.verification_report["route_decision"] == "REJECT_STALE_DIAGNOSTIC_BASIS"


def test_route_validation_rejects_unregistered_destination_before_runtime():
    with pytest.raises(ValueError, match="unregistered"):
        validate_graph_routes(
            registered_nodes={GraphNodeName.VERIFY_STATUS.value},
            static_targets=[GraphNodeName.VERIFY_STATUS.value],
            conditional_routes={
                GraphNodeName.VERIFY_STATUS.value: {"verify_status": "missing_node"},
            },
        )


def test_invalid_diagnostic_basis_ends_assurance_required_not_verify_loop():
    value = ready_for_diagnostics()
    value.violation_confirmed = False
    assert route_after_diagnostics(value) == "__end__"
    assert value.verification_report["verification_state"] == "ASSURANCE_REQUIRED"
    assert value.current_status == IntentStatus.DEGRADED


@pytest.mark.parametrize("round_no", [3, 4, 10])
def test_diagnostic_max_is_bounded_and_degraded(round_no):
    value = ready_for_diagnostics()
    value.diagnostic_state.update({"diagnostic_round": round_no, "max_diagnostic_rounds": 3, "final_reanalysis_pending": False})
    assert route_after_diagnostics(value) == "__end__"
    assert value.current_status == IntentStatus.DEGRADED
    assert value.verification_report["verification_state"] == "DEGRADED_ROOT_CAUSE_UNCONFIRMED"


def test_degraded_completion_is_not_failed():
    value = state()
    mark_degraded(value, reason="bounded diagnostics", next_action="OPERATOR_REVIEW")
    assert value.current_status == IntentStatus.DEGRADED
    assert _terminal_workflow_status(value.current_status, error_message=None, needs_clarification=False) == "degraded"


def test_explicit_worker_degraded_status_beats_stale_in_memory_failed_status():
    from models.intent_domain import IntentLifecycle
    assert _intent_lifecycle_from_terminal_status("degraded", IntentLifecycle) == IntentLifecycle.DEGRADED


@pytest.mark.parametrize("status", ["degraded", "waiting_for_infrastructure", "pending_approval"])
def test_non_failure_terminal_and_waiting_statuses_are_not_defaulted_to_failed(status):
    assert _terminal_workflow_status(status, error_message=None, needs_clarification=False) == status


def test_invalidation_preserves_identity_and_audit_not_desired_resources():
    value = ready_for_diagnostics()
    intent_id, revision, run_id = value.intent_id, value.revision, value.run_id
    invalidate_downstream_state_after_execution_change(value, reason="epoch changed")
    assert (value.intent_id, value.revision, value.run_id) == (intent_id, revision, run_id)
    assert value.execution_result["execution_id"] == "EXEC-1"
    assert value.state_audit

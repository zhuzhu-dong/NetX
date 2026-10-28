from types import SimpleNamespace

from api.worker import (
    _apply_awaiting_input_contract,
    _skip_legacy_lifecycle_projection,
    _terminal_workflow_status,
)
from models.intent_domain import IntentLifecycle
from orchestrator.intent_control_plane import IntentControlPlane


def test_completed_graph_error_is_terminal_failed():
    assert _terminal_workflow_status(
        "thinking", error_message="policy modeling failed", needs_clarification=False
    ) == "failed"


def test_clarification_and_approval_states_are_preserved():
    assert _terminal_workflow_status(
        "pending_clarification", error_message="missing source", needs_clarification=True
    ) == "pending_clarification"
    assert _terminal_workflow_status(
        "pending_approval", error_message="approval required", needs_clarification=False
    ) == "pending_approval"


def test_clarification_contract_overrides_stale_legacy_failed_status():
    """A graph END marker must not turn a persisted question into FAILED."""
    assert _terminal_workflow_status(
        "failed", error_message=None, needs_clarification=True
    ) == "pending_clarification"
    assert _skip_legacy_lifecycle_projection("pending_clarification") is True


def test_clarification_persists_an_explicit_awaiting_input_contract():
    snapshot = _apply_awaiting_input_contract(
        {
            "needs_clarification": True,
            "clarification_question": "Please provide a destination.",
            "unresolved_fields": ["source"],
            "clarification_details": [
                {"field": "destination", "reason": "MISSING_DESTINATION"},
                {"field": "source", "reason": "MISSING_SOURCE"},
            ],
        }
    )

    assert snapshot["phase"] == "AWAITING_USER_INPUT"
    assert snapshot["requires_user_input"] is True
    assert snapshot["clarification_prompt"] == "Please provide a destination."
    assert snapshot["missing_fields"] == ["source", "destination"]
    assert snapshot["reason_code"] == "MISSING_REQUIRED_FIELDS"


def test_pre_compiler_unsatisfiable_result_skips_legacy_deployment_projection():
    """An admission rejection must never be replayed through DEPLOYING/VERIFYING."""
    assert _skip_legacy_lifecycle_projection("unsatisfiable") is True
    assert _skip_legacy_lifecycle_projection("success") is False


def test_semantic_only_completion_skips_legacy_deployment_projection():
    """A deliberate parse-only run must never fabricate deployment ownership."""
    assert _skip_legacy_lifecycle_projection("semantic_complete") is True


def test_clarification_answer_formally_resumes_the_same_runtime():
    runtime = SimpleNamespace(lifecycle=IntentLifecycle.NEEDS_CLARIFICATION)

    class Domain:
        def get_runtime(self, intent_id):
            assert intent_id == "INT-CLARIFY"
            return runtime

        def transition(self, intent_id, lifecycle, **kwargs):
            assert intent_id == "INT-CLARIFY"
            assert lifecycle == IntentLifecycle.UNDERSTANDING
            assert kwargs["actor"] == "conversation_clarification"
            runtime.lifecycle = lifecycle
            return runtime

    control = object.__new__(IntentControlPlane)
    control.domain = Domain()
    resumed = control.resume_after_clarification("INT-CLARIFY")
    assert resumed is runtime
    assert resumed.lifecycle == IntentLifecycle.UNDERSTANDING

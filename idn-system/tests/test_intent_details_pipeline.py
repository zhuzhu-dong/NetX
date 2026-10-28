from api.intent_control import _pipeline_projection
from models.intent_domain import IntentLifecycle


def test_pre_execution_admission_rejection_has_explicit_not_run_pipeline():
    projection = _pipeline_projection(
        lifecycle=IntentLifecycle.UNSATISFIABLE,
        executions=[],
        workflow_result={"policy_conflict_report": {"conflict_type": "INSUFFICIENT_CAPACITY"}},
    )

    assert projection == {
        "conflict_check": "REJECTED",
        "policy_compiler": "NOT_RUN",
        "execution": "NOT_RUN",
        "status_verifier": "NOT_RUN",
        "southbound_write_operation_count": 0,
    }

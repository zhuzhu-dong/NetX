from __future__ import annotations

import pytest
from api.store import IntentStore
from models.intent_domain import IntentLifecycle, IntentRuntimeState
from models.intent_lifecycle_state_machine import IntentStateTransitionError
from persistence.intent_domain_store import IntentDomainStore


@pytest.fixture
def domain(tmp_path):
    return IntentDomainStore(IntentStore(tmp_path / "lifecycle.db"))


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (IntentLifecycle.RECEIVED, IntentLifecycle.SATISFIED),
        (IntentLifecycle.SATISFIED, IntentLifecycle.UNDERSTANDING),
        (IntentLifecycle.WITHDRAWN, IntentLifecycle.DEPLOYING),
    ],
)
def test_illegal_lifecycle_transitions_are_rejected(domain, start, target):
    runtime = IntentRuntimeState(intent_id=f"INT-{start.value}", lifecycle=start)
    domain.save_runtime(runtime)

    with pytest.raises(IntentStateTransitionError) as caught:
        domain.transition(runtime.intent_id, target, actor="test", reason="illegal")

    assert caught.value.as_dict() == {
        "code": "ILLEGAL_INTENT_STATE_TRANSITION",
        "intent_id": runtime.intent_id,
        "from": start.value,
        "to": target.value,
        "message": str(caught.value),
    }
    assert domain.get_runtime(runtime.intent_id).lifecycle == start
    assert domain.history(runtime.intent_id) == []


def test_transition_atomically_records_runtime_history_and_audit(domain):
    intent_id = "INT-AUDIT"
    domain.save_runtime(IntentRuntimeState(intent_id=intent_id))

    runtime = domain.transition(
        intent_id,
        IntentLifecycle.UNDERSTANDING,
        actor="intent_understanding",
        reason="parse accepted input",
        correlation_id="CORR-1",
        trace_id="TRACE-1",
    )

    assert runtime.lifecycle == IntentLifecycle.UNDERSTANDING
    event = domain.history(intent_id)[0]
    assert event["data"] == {"from": "RECEIVED", "to": "UNDERSTANDING"}
    assert event["intent_revision"] == 1
    assert event["correlation_id"] == "CORR-1"
    audit = domain.base_store.list_audit_events(
        resource_type="intent", resource_id=intent_id
    )[0]
    assert audit["event_type"] == "INTENT_LIFECYCLE_TRANSITION"
    assert audit["correlation"]["trace_id"] == "TRACE-1"


def test_redeploy_event_plan_does_not_activate_uncommitted_plan(domain):
    intent_id = "INT-MAKE-BEFORE-BREAK"
    domain.save_runtime(
        IntentRuntimeState(
            intent_id=intent_id,
            lifecycle=IntentLifecycle.REPLANNING,
            active_plan_id="PLAN-OLD",
        )
    )

    runtime = domain.transition(
        intent_id,
        IntentLifecycle.REDEPLOYING,
        actor="intent_execution",
        reason="candidate is being prepared",
        event_plan_id="PLAN-NEW",
    )

    assert runtime.active_plan_id == "PLAN-OLD"
    assert domain.history(intent_id)[0]["plan_id"] == "PLAN-NEW"


@pytest.mark.parametrize(
    "target",
    [
        IntentLifecycle.DEGRADED,
        IntentLifecycle.VIOLATED,
        IntentLifecycle.SATISFIED,
    ],
)
def test_unsatisfiable_intent_can_reenter_assurance_after_environment_changes(
    domain, target
):
    intent_id = f"INT-UNSAT-RECOVERY-{target.value}"
    domain.save_runtime(
        IntentRuntimeState(intent_id=intent_id, lifecycle=IntentLifecycle.UNSATISFIABLE)
    )

    recovered = domain.transition(
        intent_id,
        target,
        actor="intent_assurance",
        reason="fresh telemetry observed after an environment change",
    )

    assert recovered.lifecycle == target
    assert domain.history(intent_id)[0]["data"] == {
        "from": "UNSATISFIABLE",
        "to": target.value,
    }


def test_public_runtime_save_cannot_bypass_lifecycle_machine(domain):
    intent_id = "INT-BYPASS"
    domain.save_runtime(IntentRuntimeState(intent_id=intent_id))
    changed = domain.get_runtime(intent_id)
    changed.lifecycle = IntentLifecycle.UNDERSTANDING

    with pytest.raises(RuntimeError, match="must use IntentDomainStore.transition"):
        domain.save_runtime(changed)

    assert domain.get_runtime(intent_id).lifecycle == IntentLifecycle.RECEIVED


def test_clarification_must_reenter_understanding_before_planning(domain):
    intent_id = "INT-CLARIFICATION-GATE"
    domain.save_runtime(
        IntentRuntimeState(
            intent_id=intent_id,
            lifecycle=IntentLifecycle.NEEDS_CLARIFICATION,
        )
    )

    with pytest.raises(IntentStateTransitionError):
        domain.transition(
            intent_id,
            IntentLifecycle.PLANNING,
            actor="test",
            reason="must not skip semantic validation",
        )

    domain.transition(
        intent_id,
        IntentLifecycle.UNDERSTANDING,
        actor="intent_understanding",
        reason="clarification answer received for the same intent",
    )
    domain.transition(
        intent_id,
        IntentLifecycle.PARSED,
        actor="intent_understanding",
        reason="semantic validation complete",
    )
    domain.transition(
        intent_id,
        IntentLifecycle.SENSING,
        actor="network_sensing",
        reason="collect current context",
    )
    planned = domain.transition(
        intent_id,
        IntentLifecycle.PLANNING,
        actor="planning",
        reason="formal planning gate passed",
    )

    assert planned.intent_id == intent_id
    assert planned.lifecycle == IntentLifecycle.PLANNING


def _advance_to_deploying(domain, intent_id):
    domain.save_runtime(IntentRuntimeState(intent_id=intent_id))
    for target in (
        IntentLifecycle.UNDERSTANDING,
        IntentLifecycle.PARSED,
        IntentLifecycle.SENSING,
        IntentLifecycle.PLANNING,
        IntentLifecycle.PLAN_READY,
        IntentLifecycle.CHECKING,
        IntentLifecycle.DEPLOYING,
    ):
        domain.transition(intent_id, target, actor="test", reason=target.value)


def test_planning_capacity_failure_is_unsatisfiable(domain):
    intent_id = "INT-PLANNING-UNSAT"
    domain.save_runtime(IntentRuntimeState(intent_id=intent_id))
    for target in (
        IntentLifecycle.UNDERSTANDING,
        IntentLifecycle.PARSED,
        IntentLifecycle.SENSING,
        IntentLifecycle.PLANNING,
    ):
        domain.transition(intent_id, target, actor="test", reason=target.value)

    runtime = domain.transition(
        intent_id,
        IntentLifecycle.UNSATISFIABLE,
        actor="plan_validator",
        reason="insufficient path capacity",
    )

    assert runtime.lifecycle == IntentLifecycle.UNSATISFIABLE
    assert runtime.failure_stage == "PLANNING"


def test_execution_failure_is_not_assurance_failure(domain):
    intent_id = "INT-EXECUTION-FAILED"
    _advance_to_deploying(domain, intent_id)

    runtime = domain.transition(
        intent_id,
        IntentLifecycle.EXECUTION_FAILED,
        actor="execution_agent",
        reason="southbound write failed",
    )

    assert runtime.lifecycle == IntentLifecycle.EXECUTION_FAILED
    assert runtime.failure_stage == "EXECUTION"
    assert runtime.failure_reason == "southbound write failed"


def test_post_execution_sla_failure_is_assurance_failed(domain):
    intent_id = "INT-ASSURANCE-FAILED"
    _advance_to_deploying(domain, intent_id)
    domain.transition(
        intent_id,
        IntentLifecycle.VERIFYING,
        actor="intent_assurance",
        reason="configuration readback passed",
    )

    runtime = domain.transition(
        intent_id,
        IntentLifecycle.ASSURANCE_FAILED,
        actor="intent_assurance",
        reason="receiver throughput below threshold",
        data={"failure_reason": "SLA_VIOLATED", "recoverable": True},
    )

    assert runtime.lifecycle == IntentLifecycle.ASSURANCE_FAILED
    assert runtime.failure_stage == "ASSURANCE"
    assert runtime.failure_reason == "SLA_VIOLATED"
    assert runtime.recoverable is True


def test_post_execution_sla_pass_is_satisfied(domain):
    intent_id = "INT-SATISFIED"
    _advance_to_deploying(domain, intent_id)
    domain.transition(
        intent_id,
        IntentLifecycle.VERIFYING,
        actor="intent_assurance",
        reason="configuration readback passed",
    )

    runtime = domain.transition(
        intent_id,
        IntentLifecycle.SATISFIED,
        actor="intent_assurance",
        reason="receiver throughput met threshold",
    )

    assert runtime.lifecycle == IntentLifecycle.SATISFIED
    assert runtime.failure_stage is None
    assert runtime.failure_reason is None

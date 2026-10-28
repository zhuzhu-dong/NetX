"""The single authoritative intent lifecycle transition contract."""

from __future__ import annotations

from dataclasses import dataclass

from models.intent_domain import IntentLifecycle


@dataclass(slots=True)
class IntentStateTransitionError(Exception):
    intent_id: str
    from_state: IntentLifecycle
    to_state: IntentLifecycle
    code: str = "ILLEGAL_INTENT_STATE_TRANSITION"

    def __str__(self) -> str:
        return (
            f"illegal intent lifecycle transition for {self.intent_id}: "
            f"{self.from_state.value} -> {self.to_state.value}"
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "intent_id": self.intent_id,
            "from": self.from_state.value,
            "to": self.to_state.value,
            "message": str(self),
        }


class IntentLifecycleStateMachine:
    """Validate every lifecycle change before any durable state is written."""

    _WITHDRAWABLE = frozenset(
        state
        for state in IntentLifecycle
        if state
        not in {
            IntentLifecycle.WITHDRAWN,
            IntentLifecycle.WITHDRAW_REQUESTED,
            IntentLifecycle.WITHDRAW_PENDING_CLEANUP,
            IntentLifecycle.WITHDRAW_FAILED,
            IntentLifecycle.CANCELLING,
            IntentLifecycle.CANCELLED,
        }
    )

    _ALLOWED: dict[IntentLifecycle, frozenset[IntentLifecycle]] = {
        IntentLifecycle.RECEIVED: frozenset(
            {
                IntentLifecycle.UNDERSTANDING,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.UNDERSTANDING: frozenset(
            {
                IntentLifecycle.PARSED,
                IntentLifecycle.NEEDS_CLARIFICATION,
                IntentLifecycle.NEEDS_APPROVAL,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.NEEDS_CLARIFICATION: frozenset(
            {
                IntentLifecycle.UNDERSTANDING,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.PARSED: frozenset(
            {
                IntentLifecycle.SENSING,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.SENSING: frozenset(
            {
                IntentLifecycle.PLANNING,
                IntentLifecycle.UNSATISFIABLE,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.PLANNING: frozenset(
            {
                IntentLifecycle.PLAN_READY,
                IntentLifecycle.UNSATISFIABLE,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.PLAN_READY: frozenset(
            {
                IntentLifecycle.CHECKING,
                IntentLifecycle.NEEDS_APPROVAL,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.CHECKING: frozenset(
            {
                IntentLifecycle.DEPLOYING,
                IntentLifecycle.REDEPLOYING,
                IntentLifecycle.NEEDS_APPROVAL,
                IntentLifecycle.UNSATISFIABLE,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.NEEDS_APPROVAL: frozenset(
            {
                IntentLifecycle.CHECKING,
                IntentLifecycle.UNDERSTANDING,
                IntentLifecycle.REPLANNING,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.DEPLOYING: frozenset(
            {
                IntentLifecycle.VERIFYING,
                IntentLifecycle.EXECUTION_FAILED,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.REDEPLOYING: frozenset(
            {
                IntentLifecycle.VERIFYING,
                IntentLifecycle.REPLANNING,
                IntentLifecycle.UNSATISFIABLE,
                IntentLifecycle.EXECUTION_FAILED,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.VERIFYING: frozenset(
            {
                IntentLifecycle.SATISFIED,
                IntentLifecycle.DEGRADED,
                IntentLifecycle.VIOLATED,
                IntentLifecycle.ASSURANCE_FAILED,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.SATISFIED: frozenset(
            {
                IntentLifecycle.DEGRADED,
                IntentLifecycle.VIOLATED,
                IntentLifecycle.ASSURANCE_FAILED,
                IntentLifecycle.REPLANNING,
                IntentLifecycle.VERIFYING,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.DEGRADED: frozenset(
            {
                IntentLifecycle.SATISFIED,
                IntentLifecycle.VIOLATED,
                IntentLifecycle.ASSURANCE_FAILED,
                IntentLifecycle.DIAGNOSING,
                IntentLifecycle.REPLANNING,
                IntentLifecycle.VERIFYING,
                IntentLifecycle.UNSATISFIABLE,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.VIOLATED: frozenset(
            {
                IntentLifecycle.ASSURANCE_FAILED,
                IntentLifecycle.DIAGNOSING,
                IntentLifecycle.REPLANNING,
                IntentLifecycle.VERIFYING,
                IntentLifecycle.DEGRADED,
                IntentLifecycle.SATISFIED,
                IntentLifecycle.UNSATISFIABLE,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.ASSURANCE_FAILED: frozenset(
            {
                IntentLifecycle.DIAGNOSING,
                IntentLifecycle.REPLANNING,
                IntentLifecycle.REDEPLOYING,
                IntentLifecycle.VERIFYING,
                IntentLifecycle.DEGRADED,
                IntentLifecycle.SATISFIED,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.DIAGNOSING: frozenset(
            {
                IntentLifecycle.REPLANNING,
                IntentLifecycle.REDEPLOYING,
                IntentLifecycle.VIOLATED,
                IntentLifecycle.ASSURANCE_FAILED,
                IntentLifecycle.NEEDS_APPROVAL,
                IntentLifecycle.UNSATISFIABLE,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.REPLANNING: frozenset(
            {
                IntentLifecycle.PLAN_READY,
                IntentLifecycle.REDEPLOYING,
                IntentLifecycle.UNSATISFIABLE,
                IntentLifecycle.NEEDS_APPROVAL,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.UNSATISFIABLE: frozenset(
            {
                # UNSATISFIABLE is a bounded conclusion for one observed
                # environment, not a terminal state.  A later assurance run
                # may legitimately recover, remain uncertain, or confirm a
                # fresh violation after capacity/telemetry changes.
                IntentLifecycle.SATISFIED,
                IntentLifecycle.DEGRADED,
                IntentLifecycle.VIOLATED,
                IntentLifecycle.ASSURANCE_FAILED,
                IntentLifecycle.REPLANNING,
                IntentLifecycle.UNDERSTANDING,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.FAILED: frozenset(
            {
                IntentLifecycle.UNDERSTANDING,
                IntentLifecycle.REPLANNING,
            }
        ),
        IntentLifecycle.EXECUTION_FAILED: frozenset(
            {
                IntentLifecycle.REPLANNING,
                IntentLifecycle.REDEPLOYING,
                IntentLifecycle.UNDERSTANDING,
                IntentLifecycle.FAILED,
            }
        ),
        IntentLifecycle.WITHDRAW_REQUESTED: frozenset(
            {
                IntentLifecycle.WITHDRAWN,
                IntentLifecycle.WITHDRAW_PENDING_CLEANUP,
                IntentLifecycle.WITHDRAW_FAILED,
            }
        ),
        IntentLifecycle.WITHDRAW_PENDING_CLEANUP: frozenset(
            {
                IntentLifecycle.WITHDRAWN,
                IntentLifecycle.WITHDRAW_FAILED,
            }
        ),
        IntentLifecycle.WITHDRAW_FAILED: frozenset(
            {
                IntentLifecycle.WITHDRAW_REQUESTED,
                IntentLifecycle.WITHDRAW_PENDING_CLEANUP,
                IntentLifecycle.WITHDRAWN,
            }
        ),
        IntentLifecycle.CANCELLING: frozenset({
            IntentLifecycle.CANCELLED,
            IntentLifecycle.CANCELLED_AFTER_COMMIT,
        }),
        IntentLifecycle.CANCELLED: frozenset(),
        # The execution is terminal but an already committed policy remains
        # live.  It may subsequently be retired only through withdrawal.
        IntentLifecycle.CANCELLED_AFTER_COMMIT: frozenset(),
        # A stale/incorrect terminal projection can be disproven by durable
        # policy ownership.  In that exceptional case only allow a retry into
        # explicit cleanup-pending state; business execution never resumes.
        IntentLifecycle.WITHDRAWN: frozenset({IntentLifecycle.WITHDRAW_PENDING_CLEANUP}),
    }

    @classmethod
    def allowed_targets(cls, from_state: IntentLifecycle) -> frozenset[IntentLifecycle]:
        targets = cls._ALLOWED.get(from_state, frozenset())
        if from_state in cls._WITHDRAWABLE:
            return targets | frozenset({
                IntentLifecycle.WITHDRAW_REQUESTED,
                IntentLifecycle.CANCELLING,
            })
        return targets

    @classmethod
    def validate(
        cls,
        intent_id: str,
        from_state: IntentLifecycle,
        to_state: IntentLifecycle,
    ) -> None:
        # Repeated delivery is an idempotent observation, not a state change.
        if from_state == to_state:
            return
        if to_state not in cls.allowed_targets(from_state):
            raise IntentStateTransitionError(intent_id, from_state, to_state)

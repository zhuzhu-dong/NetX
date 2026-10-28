"""Bounded Desired–Observed reconciliation for an execution readback mismatch."""
from __future__ import annotations

from models.agent_state import ExecutionStage, GlobalAgentState, StageStatus
from models.intent import IntentStatus
from .base_node import BaseNode
from ..state_generations import (
    audit,
    begin_assurance,
    ensure_execution_generation,
    mark_degraded,
    mark_readback_verified,
)


class ReconcileExecutionNode(BaseNode):
    """Repair only proven configuration drift, then repeat readback.

    This is not a KPI/healing node.  It never runs assurance, root-cause
    analysis, or Understanding, and it uses the existing reconciler's
    idempotent Desired–Observed implementation rather than inventing policy
    objects or duplicate resources.
    """
    agent_id = "execution_reconciler"
    stage = ExecutionStage.EXECUTE

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        if state.execution_readback_status != "EXECUTION_READBACK_MISMATCH":
            return self._end_log(state, start, StageStatus.SUCCESS, "RECONCILIATION_SKIPPED: no readback mismatch")
        if state.reconciliation_attempt >= state.max_reconciliation_attempts:
            return self._degraded(state, start, "reconciliation attempt budget exhausted before repair")
        state.reconciliation_attempt += 1
        state.execution_status = "RECONCILIATION_IN_PROGRESS"
        try:
            from infra.network_reconciler import NetworkReconciler
            from api.qos import verify_execution_plane

            summary = await NetworkReconciler().run_once(auto_repair=True)
            readback = await verify_execution_plane(state.yang_draft, state.execution_result or {})
            verified = bool(
                str(readback.get("status") or "").lower() in {"ok", "verified"}
                and readback.get("config_applied") is True
            )
            report = state.verification_report or {}
            report["reconciliation"] = {
                "attempt": state.reconciliation_attempt,
                "max_attempts": state.max_reconciliation_attempts,
                "summary": summary.model_dump(mode="json") if hasattr(summary, "model_dump") else dict(summary),
                "readback": readback,
            }
            if verified:
                state.reconciliation_generation += 1
                ensure_execution_generation(
                    state, force=True,
                    reason="reconciliation repaired desired-observed mismatch",
                )
                mark_readback_verified(state)
                begin_assurance(state, reason="reconciliation verified")
                report.update({
                    "verification_state": "RECONCILIATION_VERIFIED",
                    "next_action": "ASSURANCE_MEASURING",
                    "reconciliation_required": False,
                    "execution_generation": state.execution_generation,
                    "execution_readback_generation": state.execution_readback_generation,
                    "assurance_generation": state.assurance_generation,
                    "state_invalidation": "DOWNSTREAM_STATE_INVALIDATED",
                })
                state.verification_report = report
                audit(state, "RECONCILIATION_VERIFIED", "desired-observed readback now matches")
                return self._end_log(state, start, StageStatus.SUCCESS, "RECONCILIATION_VERIFIED: Desired–Observed readback now matches")
            state.execution_mismatch = readback
            state.verification_report = report
        except Exception as exc:
            state.execution_mismatch = {**(state.execution_mismatch or {}), "reconciliation_error": str(exc)}
        if state.reconciliation_attempt >= state.max_reconciliation_attempts:
            return self._degraded(state, start, "Desired–Observed mismatch persists after bounded reconciliation")
        return self._end_log(state, start, StageStatus.SUCCESS, f"RECONCILIATION_REQUIRED: retry {state.reconciliation_attempt}/{state.max_reconciliation_attempts}")

    def _degraded(self, state: GlobalAgentState, start, reason: str) -> GlobalAgentState:
        state.execution_status = "DEGRADED_EXECUTION_UNVERIFIED"
        state.execution_readback_status = "DEGRADED_EXECUTION_UNVERIFIED"
        state.reconciliation_required = False
        state.measurement_status = "WAITING_FOR_EXECUTION_VERIFICATION"
        state.violation_confirmed = False
        report = state.verification_report or {}
        report.update({"verification_state": "DEGRADED_EXECUTION_UNVERIFIED", "phase": "execution_readback", "terminal_reason": reason, "next_action": "WAIT_FOR_EXECUTION_VERIFICATION", "violation_confirmed": False, "healing_ready": False, "diagnostics_required": False})
        state.verification_report = report
        mark_degraded(state, reason=reason, next_action="WAIT_FOR_EXECUTION_VERIFICATION")
        return self._end_log(state, start, StageStatus.SUCCESS, "DEGRADED_EXECUTION_UNVERIFIED: " + reason)

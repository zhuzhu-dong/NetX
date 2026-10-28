"""Structured phase-generation gates for the legacy workflow graph.

Transient workflow facts are only routable when their upstream basis is the
currently verified execution.  This module deliberately preserves audit
records while resetting the mutable *current* view after execution changes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from models.agent_state import GlobalAgentState
from models.intent import IntentStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def audit(state: GlobalAgentState, event: str, reason: str, **data: Any) -> None:
    state.state_audit.append({
        "event": event, "intent_id": state.intent_id, "revision": state.revision,
        "run_id": state.run_id, "execution_generation": state.execution_generation,
        "timestamp": _now(), "reason": reason, **data,
    })


def invalidate_downstream_state_after_execution_change(
    state: GlobalAgentState, *, reason: str,
) -> None:
    """Retire current derived facts without deleting persisted audit evidence."""
    old = {
        "assurance_id": state.assurance_id,
        "violation_id": state.violation_id,
        "root_cause_id": state.root_cause_analysis_id,
        "diagnostic_round": (state.diagnostic_state or {}).get("diagnostic_round", 0),
    }
    if any(value not in (None, 0, "") for value in old.values()):
        audit(state, "DOWNSTREAM_STATE_INVALIDATED", reason, invalidated=old)
    state.measurement_status = "NOT_STARTED"
    state.assurance_generation += 1
    state.assurance_basis_execution_generation = state.execution_generation
    state.assurance_id = None
    state.assurance_samples = []
    state.valid_assurance_sample_count = 0
    state.violation_confirmed = False
    state.violation_id = None
    state.violation_basis_assurance_generation = None
    state.root_cause_status = "NOT_STARTED"
    state.root_cause_analysis_id = None
    state.root_cause_basis_violation_id = None
    state.diagnostic_basis_root_cause_id = None
    state.diagnostic_state = {}
    state.diagnostics_required = False
    state.healing_ready = False
    state.terminal_reason = None
    state.next_action = "START_ASSURANCE"


def initialize_transient_state_for_policy_reuse(state: GlobalAgentState) -> None:
    """Reuse desired state only; every observed/assurance fact is new-run data."""
    ensure_execution_generation(
        state,
        force=True,
        reason="POLICY_REUSED_DESIRED_STATE_ONLY",
    )
    state.execution_readback_status = "NOT_STARTED"
    state.execution_readback_generation = None
    state.assurance_basis_execution_generation = None
    state.diagnostic_state = {
        "diagnostic_round": 0, "diagnostic_status": "NOT_STARTED",
        "diagnostic_basis_root_cause_id": None, "raw_evidence_hash": None,
        "material_evidence_hash": None, "previous_material_evidence_hash": None,
        "material_changes": [], "missing_evidence": [],
    }
    audit(
        state,
        "POLICY_REUSED_DESIRED_STATE_ONLY",
        "committed policy, scope and resource lineage retained; transient state reset",
    )


def ensure_execution_generation(state: GlobalAgentState, *, force: bool = False, reason: str) -> bool:
    execution = state.execution_result or {}
    signature = str(execution.get("desired_effect_signature") or execution.get("execution_id") or execution.get("timestamp") or "")
    changed = force or state.execution_generation == 0 or (signature and signature != state.execution_effect_signature)
    if not changed:
        return False
    previous = state.execution_generation
    state.execution_generation += 1
    state.execution_effect_signature = signature or f"generation-{state.execution_generation}"
    state.execution_readback_generation = None
    audit(state, "EXECUTION_GENERATION_CHANGED", reason, old_execution_generation=previous)
    invalidate_downstream_state_after_execution_change(state, reason=reason)
    return True


def mark_readback_verified(state: GlobalAgentState, *, status: str = "EXECUTION_READBACK_VERIFIED") -> None:
    state.execution_readback_status = status
    state.execution_status = status
    state.execution_readback_generation = state.execution_generation
    state.execution_mismatch = {}
    state.reconciliation_required = False
    audit(state, "POLICY_REUSE_READBACK_VERIFIED", "desired-observed readback matched")


def begin_assurance(state: GlobalAgentState, *, reason: str) -> None:
    if state.assurance_basis_execution_generation != state.execution_generation or state.assurance_generation == 0:
        invalidate_downstream_state_after_execution_change(state, reason=reason)
    if state.assurance_id is None:
        state.assurance_id = uuid4().hex
        audit(state, "ASSURANCE_STARTED", reason,
              assurance_id=state.assurance_id,
              assurance_generation=state.assurance_generation,
              basis_execution_generation=state.execution_generation,
              max_samples=3)
    state.assurance_basis_execution_generation = state.execution_generation
    state.measurement_status = "ASSURANCE_MEASURING"
    state.next_action = "ASSURANCE_MEASURING"


def readback_is_current(state: GlobalAgentState) -> bool:
    return (
        state.execution_readback_status in {"EXECUTION_READBACK_VERIFIED", "VERIFIED"}
        and state.execution_readback_generation == state.execution_generation
    )


def can_enter_assurance(state: GlobalAgentState) -> bool:
    """Assurance can only measure the currently read-back execution effect."""
    return bool(
        state.execution_generation > 0
        and readback_is_current(state)
        and state.execution_readback_generation == state.execution_generation
    )


def can_confirm_violation(state: GlobalAgentState) -> bool:
    samples = list(state.assurance_samples or [])
    valid = [sample for sample in samples if sample.get("valid") is True]
    low = [sample for sample in valid if sample.get("received_mbps") is not None and sample.get("threshold_mbps") is not None and float(sample["received_mbps"]) < float(sample["threshold_mbps"])]
    epochs = {sample.get("environment_epoch") for sample in valid if sample.get("environment_epoch") is not None}
    return (
        can_enter_assurance(state)
        and bool(state.assurance_id)
        and state.measurement_status in {"COMPLETED", "CONFIRMED"}
        and state.assurance_basis_execution_generation == state.execution_generation
        and state.assurance_generation > 0
        and state.valid_assurance_sample_count >= 2
        and len(valid) >= 2
        and len(low) >= 2
        and len(epochs) <= 1
    )


def violation_is_current(state: GlobalAgentState) -> bool:
    return bool(
        state.violation_confirmed and state.violation_id
        and state.violation_basis_assurance_generation == state.assurance_generation
        and can_confirm_violation(state)
    )


def root_cause_is_current(state: GlobalAgentState) -> bool:
    return bool(
        violation_is_current(state)
        and state.root_cause_analysis_id
        and state.root_cause_basis_violation_id == state.violation_id
    )


def can_enter_root_cause(state: GlobalAgentState) -> bool:
    """Root-cause analysis is gated by a persistent, current violation."""
    return violation_is_current(state) and state.measurement_status == "COMPLETED"


def diagnostic_basis_is_current(state: GlobalAgentState) -> bool:
    """Check the evidence basis independently of the diagnostic round cap."""
    diagnostic = state.diagnostic_state or {}
    return bool(
        can_enter_root_cause(state)
        and root_cause_is_current(state)
        and state.root_cause_status == "PENDING"
        and state.diagnostic_basis_root_cause_id == state.root_cause_analysis_id
        and state.diagnostics_required
    )


def diagnostics_allowed(state: GlobalAgentState) -> bool:
    diagnostic = state.diagnostic_state or {}
    return bool(
        diagnostic_basis_is_current(state)
        and int(diagnostic.get("diagnostic_round") or 0) < int(diagnostic.get("max_diagnostic_rounds") or 3)
    )


def mark_degraded(state: GlobalAgentState, *, reason: str, next_action: str) -> None:
    state.current_status = IntentStatus.DEGRADED
    state.terminal_reason = reason
    state.next_action = next_action
    state.error_message = None
    audit(state, "WORKFLOW_COMPLETED_WITH_DEGRADED_STATE", reason, next_action=next_action)

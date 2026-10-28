"""One read-side lifecycle contract for every Intent API projection.

The API store predates the durable Intent domain, so a record can contain a
legacy worker ``status`` while the domain runtime contains the canonical
lifecycle.  Consumers must never infer an active workflow from an omitted
field.  This module makes the precedence and terminal semantics explicit for
list, current-card, and detail responses.
"""
from __future__ import annotations

from typing import Any


_ALIASES = {
    "success": "SATISFIED", "succeeded": "SATISFIED", "completed": "SATISFIED", "done": "SATISFIED",
    "rejected": "UNSATISFIABLE", "unsatisfiable": "UNSATISFIABLE",
    "failed": "FAILED", "execution_failed": "FAILED",
    "cancelled": "CANCELLED", "canceled": "CANCELLED", "cancelled_after_commit": "CANCELLED_AFTER_COMMIT",
    "pending_clarification": "AWAITING_USER_INPUT", "needs_clarification": "AWAITING_USER_INPUT",
    "clarification_required": "AWAITING_USER_INPUT", "waiting_for_user": "AWAITING_USER_INPUT",
    "received": "PENDING", "pending": "PENDING", "queued": "PENDING",
    "understanding": "PLANNING", "parsed": "PLANNING", "sensing": "PLANNING", "planning": "PLANNING",
    "plan_ready": "EVALUATED", "checking": "EVALUATED", "needs_approval": "EVALUATED",
    "deploying": "RUNNING", "executing": "RUNNING", "acting": "RUNNING", "verifying": "RUNNING",
    "cancelling": "CANCELLING",
}

_CANONICAL = {
    "PENDING", "PLANNING", "EVALUATED", "AWAITING_USER_INPUT", "RUNNING", "CANCELLING",
    "SATISFIED", "UNSATISFIABLE", "FAILED", "DEGRADED", "VIOLATED", "ASSURANCE_FAILED",
    "WITHDRAW_REQUESTED", "WITHDRAW_PENDING_CLEANUP", "WITHDRAW_FAILED", "WITHDRAWN",
    "CANCELLED", "CANCELLED_AFTER_COMMIT", "EXPIRED", "SUPERSEDED", "REJECTED",
}

_TERMINAL = {
    "SATISFIED", "UNSATISFIABLE", "FAILED", "WITHDRAWN", "CANCELLED",
    "CANCELLED_AFTER_COMMIT", "EXPIRED", "SUPERSEDED", "REJECTED",
}


def canonical_lifecycle(value: Any) -> str | None:
    """Normalize a persisted value without treating absent/unknown as active."""
    raw = str(value or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    if upper in _CANONICAL:
        return upper
    return _ALIASES.get(raw.lower())


def project_intent_lifecycle(
    record: dict[str, Any] | None,
    *,
    runtime: Any = None,
    jobs: list[dict[str, Any]] | None = None,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return authoritative lifecycle fields without mutating persisted data.

    An explicit terminal value wins over an old job or an omitted phase.  A
    job is useful diagnostics, but never an authority to reopen a terminal
    Intent unless a later lifecycle transition has already made the Intent
    non-terminal.
    """
    record = record or {}
    snapshot = record.get("snapshot") or {}
    raw_status = record.get("status") or record.get("lifecycle")
    runtime_value = getattr(runtime, "lifecycle", None)
    if hasattr(runtime_value, "value"):
        runtime_value = runtime_value.value
    phase_value = snapshot.get("phase") or record.get("phase")
    candidates = [canonical_lifecycle(raw_status), canonical_lifecycle(runtime_value), canonical_lifecycle(phase_value)]
    lifecycle = next((value for value in candidates if value in _TERMINAL), None)
    lifecycle = lifecycle or next((value for value in candidates if value), "UNKNOWN")
    terminal = lifecycle in _TERMINAL
    awaiting_input = lifecycle == "AWAITING_USER_INPUT" and not terminal
    job_rows = list(jobs or [])
    latest_job = job_rows[-1] if job_rows else {}
    execution = execution or (record.get("result") or {}).get("execution_result") or {}
    execution_status = execution.get("status") if isinstance(execution, dict) else None
    current_stage = snapshot.get("current_stage") or snapshot.get("stage") or phase_value or lifecycle
    return {
        "lifecycle_status": lifecycle,
        "phase": "COMPLETED" if terminal else ("AWAITING_USER_INPUT" if awaiting_input else current_stage),
        "current_stage": current_stage,
        "terminal": terminal,
        "requires_user_input": awaiting_input,
        "needs_clarification": awaiting_input,
        "job_status": latest_job.get("status") if latest_job else None,
        "execution_status": execution_status,
    }

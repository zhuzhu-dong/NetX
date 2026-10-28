"""Explicit, idempotent repair for stale dashboard lifecycle projections.

This service is intentionally separate from the read path.  It reconciles only
the persisted runtime projection to an already authoritative completed
execution/assurance fact; it never invokes an agent or writes network state.
"""
from __future__ import annotations

from typing import Any

from models.intent_domain import IntentAssuranceResult, IntentLifecycle
from persistence.intent_domain_store import IntentDomainStore, get_intent_domain_store


class IntentProjectionRepairService:
    def __init__(self, domain: IntentDomainStore | None = None):
        self.domain = domain or get_intent_domain_store()

    def repair(self, *, limit: int = 200) -> dict[str, Any]:
        repaired: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        protected_terminal = {
            IntentLifecycle.WITHDRAW_REQUESTED,
            IntentLifecycle.WITHDRAW_PENDING_CLEANUP,
            IntentLifecycle.WITHDRAW_FAILED,
            IntentLifecycle.WITHDRAWN,
            IntentLifecycle.UNSATISFIABLE,
            IntentLifecycle.EXECUTION_FAILED,
            IntentLifecycle.FAILED,
        }
        for record in self.domain.current_intent_records(limit=limit):
            runtime = record["runtime"]
            # A prior version of this reconciler could incorrectly treat old
            # terminal history as a current success.  Its own durable audit
            # event carries the exact previous lifecycle, so restore only
            # those self-identified mistakes before considering any new work.
            reconciliation = next(
                (
                    event for event in self.domain.history(runtime.intent_id)
                    if event.get("event_type") == "PROJECTION_RECONCILED"
                    and event.get("actor") == "intent_projection_repair"
                    and (event.get("data") or {}).get("after") == "SATISFIED"
                    and (event.get("data") or {}).get("before") in {item.value for item in protected_terminal}
                ),
                None,
            )
            if reconciliation and runtime.lifecycle == IntentLifecycle.SATISFIED:
                before = IntentLifecycle((reconciliation.get("data") or {})["before"])
                runtime.lifecycle = before
                runtime.last_transition_reason = "projection reconciliation reverted: protected historical terminal state"
                self.domain._write_runtime(runtime)
                self.domain.record_event(runtime.intent_id, "PROJECTION_RECONCILIATION_REVERTED", actor="intent_projection_repair", reason="restored protected historical terminal lifecycle from reconciliation audit", data={"before": "SATISFIED", "after": before.value, "revision": runtime.current_revision})
                repaired.append({"intent_id": runtime.intent_id, "before": "SATISFIED", "after": before.value, "revision": runtime.current_revision, "reverted": True})
                continue
            if runtime.lifecycle in protected_terminal:
                skipped.append({"intent_id": runtime.intent_id, "reason": "protected historical terminal lifecycle"})
                continue
            aggregate = record.get("aggregate") or {}
            result = aggregate.get("result") or {}
            execution = result.get("execution_result") or {}
            verification = result.get("status_verification_report") or {}
            authoritative_satisfied = bool(execution.get("config_applied")) and (
                verification.get("passed") is True
                or (aggregate.get("snapshot") or {}).get("verification_passed") is True
                or str(aggregate.get("status", "")).lower() in {"success", "satisfied"}
            )
            aggregate_status = str(aggregate.get("status") or "").lower()
            assurance = record.get("assurance") or {}
            verification_state = str(verification.get("verification_state") or "").upper()
            target = IntentLifecycle.SATISFIED if authoritative_satisfied else None
            if (
                target is None
                and aggregate_status == "degraded"
                and assurance.get("assurance_id")
                and (
                    verification_state.startswith("VIOLATION_")
                    or verification_state.startswith("DEGRADED_")
                    or assurance.get("satisfied") is False
                )
            ):
                # A measured post-execution violation is operationally
                # degraded, never a stale approval or graph failure.
                target = IntentLifecycle.DEGRADED
            if target is None:
                skipped.append({"intent_id": runtime.intent_id, "reason": "no authoritative completed lifecycle fact"})
                continue
            if runtime.lifecycle == target:
                skipped.append({"intent_id": runtime.intent_id, "reason": "projection already current"})
                continue
            before = runtime.lifecycle.value
            # Projection-only repair: preserve revision, policy IDs, desired
            # policy, metrics, and every authority/event; write a new audit
            # event before returning.  Re-running is a no-op after this write.
            runtime.lifecycle = target
            runtime.last_transition_reason = "projection reconciliation: persisted authoritative workflow outcome"
            self.domain._write_runtime(runtime)
            if assurance.get("assurance_id"):
                persisted_assurance = IntentAssuranceResult.model_validate(assurance)
                self.domain.save_assurance(persisted_assurance.model_copy(update={
                    "lifecycle": target,
                    "satisfied": target == IntentLifecycle.SATISFIED,
                    "summary": verification_state or persisted_assurance.summary,
                    "failure_reason": verification.get("terminal_reason") or persisted_assurance.failure_reason,
                }))
            self.domain.record_event(runtime.intent_id, "PROJECTION_RECONCILED", actor="intent_projection_repair", reason="aligned stale lifecycle projection with authoritative completed workflow outcome", data={"before": before, "after": target.value, "revision": runtime.current_revision, "aggregate_status": aggregate_status})
            repaired.append({"intent_id": runtime.intent_id, "before": before, "after": target.value, "revision": runtime.current_revision})
        return {"repaired": repaired, "skipped": skipped, "idempotent": True, "writes_data_plane": False, "calls_llm": False}

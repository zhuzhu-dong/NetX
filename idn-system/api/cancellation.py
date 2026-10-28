"""Durable, cooperative user cancellation for asynchronous intent jobs.

Cancellation is deliberately distinct from withdrawal.  A user may stop the
*current execution* without retiring a policy that has already been formally
committed.  The worker observes the durable request at graph-node boundaries;
there is no browser-only cancellation state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from api.store import IntentStore
from models.intent_domain import IntentLifecycle


class IntentExecutionCancelled(RuntimeError):
    """Raised by the orchestrator guard after a durable user cancellation."""

    def __init__(self, intent_id: str, *, state: Any = None):
        super().__init__(f"USER_CANCELLED:{intent_id}")
        self.intent_id = intent_id
        self.state = state


TERMINAL_CANCEL_STATUSES = frozenset({"cancelled", "cancelled_after_commit"})
TERMINAL_STATUSES = frozenset({
    "success", "completed", "failed", "rejected", "unsatisfiable",
    "cancelled", "cancelled_after_commit", "withdrawn",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cancellation_requested(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    if str(record.get("status") or "").lower() == "cancelling":
        return True
    return bool((record.get("snapshot") or {}).get("cancellation", {}).get("requested_at"))


def committed_policy_in_state(state: Any) -> bool:
    """Only a completed policy-commit record authorizes resource preservation."""
    if state is None:
        return False
    report = getattr(state, "verification_report", None)
    if report is None and isinstance(state, dict):
        report = state.get("verification_report")
    commit = (report or {}).get("policy_commit") or {}
    return str(commit.get("status") or "").upper() in {"COMMITTED", "ACTIVE"}


def _transition(store: IntentStore, intent_id: str, lifecycle: IntentLifecycle, *, reason: str, data: dict[str, Any]) -> None:
    from persistence.intent_domain_store import IntentDomainStore

    domain = IntentDomainStore(store)
    runtime = domain.get_runtime(intent_id)
    if runtime and runtime.lifecycle == lifecycle:
        return
    domain.transition(intent_id, lifecycle, actor="operator", reason=reason, data=data)


def request_cancellation(store: IntentStore, intent_id: str) -> dict[str, Any]:
    """Persist an idempotent cancellation request without withdrawing policy."""
    record = store.get(intent_id)
    if record is None:
        raise KeyError(intent_id)
    current = str(record.get("status") or "").lower()
    latest_job = (store.list_jobs(intent_id) or [{}])[-1]
    if current in TERMINAL_CANCEL_STATUSES:
        return {"intent_id": intent_id, "job_id": latest_job.get("job_id"), "status": current.upper(), "terminal": True, "idempotent": True, "cancellation_requested": True}
    if current in TERMINAL_STATUSES:
        return {"intent_id": intent_id, "job_id": latest_job.get("job_id"), "status": current.upper(), "terminal": True, "idempotent": True, "cancellation_requested": False}

    snapshot = dict(record.get("snapshot") or {})
    cancellation = dict(snapshot.get("cancellation") or {})
    cancellation.setdefault("requested_at", _now())
    cancellation.update({"reason": "USER_CANCELLED", "state": "REQUESTED"})
    snapshot["cancellation"] = cancellation
    jobs = store.list_jobs(intent_id)
    active_jobs = [job for job in jobs if job.get("status") in {"queued", "running"}]

    # A queued/no-worker intent has not crossed a southbound boundary and can
    # be concluded synchronously. A running worker will acknowledge at its
    # next guarded graph boundary.
    if not active_jobs or all(job.get("status") == "queued" for job in active_jobs):
        # Preserve the same audited lifecycle path as an asynchronous worker
        # acknowledgement even though the safe boundary is available now.
        store.update(intent_id, status="cancelling", snapshot=snapshot)
        _transition(store, intent_id, IntentLifecycle.CANCELLING, reason="USER_CANCELLED", data=cancellation)
        store.cancel_queued_jobs(intent_id, reason="USER_CANCELLED")
        return finalize_cancellation(store, intent_id, after_commit=False, snapshot=snapshot)

    store.update(intent_id, status="cancelling", snapshot=snapshot)
    _transition(store, intent_id, IntentLifecycle.CANCELLING, reason="USER_CANCELLED", data=cancellation)
    store.add_event(intent_id, "cancellation_requested", cancellation)
    store.add_message(
        record["session_id"], "system", "cancellation_requested",
        "正在终止当前意图执行；系统会在安全边界完成取消。",
        intent_id=intent_id, payload=cancellation,
    )
    return {"intent_id": intent_id, "job_id": active_jobs[0].get("job_id"), "status": "CANCELLING", "terminal": False, "idempotent": current == "cancelling", "cancellation_requested": True}


def finalize_cancellation(
    store: IntentStore,
    intent_id: str,
    *,
    after_commit: bool,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the authoritative terminal cancellation projection exactly once."""
    record = store.get(intent_id)
    if record is None:
        raise KeyError(intent_id)
    current = str(record.get("status") or "").lower()
    if current in TERMINAL_CANCEL_STATUSES:
        latest_job = (store.list_jobs(intent_id) or [{}])[-1]
        return {"intent_id": intent_id, "job_id": latest_job.get("job_id"), "status": current.upper(), "terminal": True, "idempotent": True, "cancellation_requested": True}
    payload = dict(snapshot or record.get("snapshot") or {})
    cancellation = dict(payload.get("cancellation") or {})
    cancellation.setdefault("requested_at", _now())
    cancellation.update({
        "reason": "USER_CANCELLED",
        "cancelled_at": _now(),
        "state": "CANCELLED_AFTER_COMMIT" if after_commit else "CANCELLED",
    })
    payload["cancellation"] = cancellation
    status = "cancelled_after_commit" if after_commit else "cancelled"
    lifecycle = IntentLifecycle.CANCELLED_AFTER_COMMIT if after_commit else IntentLifecycle.CANCELLED
    # A clarification prompt is a non-terminal contract.  Preserve its audit
    # history, but clear its *active* projection once the user has cancelled
    # so a refresh cannot describe a terminal execution as awaiting input.
    payload.update(
        {
            "phase": lifecycle.value,
            "needs_clarification": False,
            "requires_user_input": False,
            "clarification_prompt": None,
            "missing_fields": [],
            "reason_code": "USER_CANCELLED",
        }
    )
    store.update(intent_id, status=status, snapshot=payload)
    _transition(store, intent_id, lifecycle, reason="USER_CANCELLED", data=cancellation)
    store.add_event(intent_id, "cancellation_completed", cancellation)
    store.add_message(
        record["session_id"], "assistant", "cancellation_result",
        "已停止本次执行；已提交策略保持生效。" if after_commit else "已停止本次执行；未继续下发网络策略。",
        intent_id=intent_id, payload=cancellation,
    )
    latest_job = (store.list_jobs(intent_id) or [{}])[-1]
    return {"intent_id": intent_id, "job_id": latest_job.get("job_id"), "status": lifecycle.value, "terminal": True, "idempotent": False, "cancellation_requested": True}

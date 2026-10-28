from __future__ import annotations

from api.cancellation import finalize_cancellation, request_cancellation
from api.store import IntentStore
from models.intent_domain import IntentLifecycle
from persistence.intent_domain_store import IntentDomainStore


def _store_with_intent(tmp_path, intent_id: str = "INT-CANCEL") -> IntentStore:
    store = IntentStore(tmp_path / "cancellation.db")
    store.create(intent_id=intent_id, text="temporary network request", user_id="operator")
    return store


def test_queued_cancel_is_terminal_idempotent_and_never_withdraws(tmp_path):
    store = _store_with_intent(tmp_path)
    job = store.enqueue_job("INT-CANCEL", {"text": "temporary", "session_id": store.get("INT-CANCEL")["session_id"]})

    first = request_cancellation(store, "INT-CANCEL")
    second = request_cancellation(store, "INT-CANCEL")

    assert first["status"] == "CANCELLED"
    assert first["terminal"] is True
    assert second["idempotent"] is True
    assert store.get_job(job["job_id"])["status"] == "cancelled"
    assert store.get("INT-CANCEL")["status"] == "cancelled"
    assert IntentDomainStore(store).get_runtime("INT-CANCEL").lifecycle == IntentLifecycle.CANCELLED


def test_running_cancel_waits_for_worker_ack_and_preserves_reason(tmp_path):
    store = _store_with_intent(tmp_path)
    job = store.enqueue_job("INT-CANCEL", {"text": "temporary", "session_id": store.get("INT-CANCEL")["session_id"]})
    store.claim_next_job("test-worker")

    response = request_cancellation(store, "INT-CANCEL")

    assert response["status"] == "CANCELLING"
    assert response["terminal"] is False
    record = store.get("INT-CANCEL")
    assert record["status"] == "cancelling"
    assert record["snapshot"]["cancellation"]["reason"] == "USER_CANCELLED"
    assert store.get_job(job["job_id"])["status"] == "running"
    assert IntentDomainStore(store).get_runtime("INT-CANCEL").lifecycle == IntentLifecycle.CANCELLING


def test_after_commit_cancellation_preserves_policy_lifecycle(tmp_path):
    store = _store_with_intent(tmp_path)
    store.enqueue_job("INT-CANCEL", {"text": "temporary", "session_id": store.get("INT-CANCEL")["session_id"]})
    store.claim_next_job("test-worker")
    request_cancellation(store, "INT-CANCEL")

    response = finalize_cancellation(store, "INT-CANCEL", after_commit=True)

    assert response["status"] == "CANCELLED_AFTER_COMMIT"
    assert store.get("INT-CANCEL")["status"] == "cancelled_after_commit"
    assert IntentDomainStore(store).get_runtime("INT-CANCEL").lifecycle == IntentLifecycle.CANCELLED_AFTER_COMMIT


def test_completed_cancel_is_a_safe_terminal_readback(tmp_path):
    store = _store_with_intent(tmp_path)
    store.update("INT-CANCEL", status="success")

    response = request_cancellation(store, "INT-CANCEL")

    assert response == {
        "intent_id": "INT-CANCEL",
        "job_id": None,
        "status": "SUCCESS",
        "terminal": True,
        "idempotent": True,
        "cancellation_requested": False,
    }


def test_awaiting_input_cancel_clears_the_active_clarification_projection(tmp_path):
    store = _store_with_intent(tmp_path)
    store.update(
        "INT-CANCEL",
        status="pending_clarification",
        snapshot={
            "phase": "AWAITING_USER_INPUT",
            "needs_clarification": True,
            "requires_user_input": True,
            "clarification_prompt": "Provide a destination.",
            "missing_fields": ["destination"],
        },
    )

    response = request_cancellation(store, "INT-CANCEL")
    record = store.get("INT-CANCEL")

    assert response["status"] == "CANCELLED"
    assert record["status"] == "cancelled"
    assert record["snapshot"]["phase"] == "CANCELLED"
    assert record["snapshot"]["requires_user_input"] is False
    assert record["snapshot"]["missing_fields"] == []

from __future__ import annotations

from fastapi.testclient import TestClient

from api.intent_lifecycle_projection import project_intent_lifecycle
from api.main import create_app
from api.store import reset_store


def test_success_has_one_terminal_lifecycle_contract_in_list_and_detail(tmp_path):
    """A legacy worker success cannot become a frontend PENDING execution."""
    store = reset_store(tmp_path / "terminal-projection.db")
    intent_id = "INT-TERMINAL-SUCCESS"
    store.create(intent_id=intent_id, text="completed intent", user_id="operator")
    job = store.enqueue_job(intent_id, {"text": "completed intent"})
    store.update(intent_id, status="success", snapshot={})
    store.complete_job(job["job_id"])

    client = TestClient(create_app())
    listing = client.get("/api/v1/intents", headers={"X-API-Key": "demo-key-xxx"})
    detail = client.get(f"/api/v1/intents/{intent_id}", headers={"X-API-Key": "demo-key-xxx"})

    assert listing.status_code == detail.status_code == 200
    listed = next(item for item in listing.json()["items"] if item["intent_id"] == intent_id)
    detailed = detail.json()
    for payload in (listed, detailed):
        assert payload["status"] == "success"
        assert payload["lifecycle_status"] == "SATISFIED"
        assert payload["phase"] == "COMPLETED"
        assert payload["terminal"] is True
        assert payload["requires_user_input"] is False
        assert payload["needs_clarification"] is False
        assert payload["job_status"] == "completed"


def test_terminal_intent_wins_over_a_stale_active_job_projection():
    projection = project_intent_lifecycle(
        {"status": "success", "snapshot": {"phase": "RUNNING"}},
        jobs=[{"status": "running"}],
    )
    assert projection["lifecycle_status"] == "SATISFIED"
    assert projection["phase"] == "COMPLETED"
    assert projection["terminal"] is True
    assert projection["job_status"] == "running"

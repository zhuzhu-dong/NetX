from types import SimpleNamespace

from fastapi.testclient import TestClient

from api.main import create_app
from api.store import reset_store


def test_clarification_continuation_reuses_same_intent_and_clears_awaiting_contract(
    tmp_path, monkeypatch
):
    """The user response resumes one persisted intent; it is never a new submit."""
    store = reset_store(tmp_path / "awaiting-input.db")
    intent_id = "INT-AWAITING-INPUT"
    store.create(intent_id=intent_id, text="minimum bandwidth", user_id="operator")
    store.update(
        intent_id,
        status="pending_clarification",
        snapshot={
            "phase": "AWAITING_USER_INPUT",
            "needs_clarification": True,
            "requires_user_input": True,
            "clarification_prompt": "Please provide a destination.",
            "missing_fields": ["destination"],
            "reason_code": "MISSING_REQUIRED_FIELDS",
        },
    )

    calls: list[str] = []

    class ControlPlane:
        def resume_after_clarification(self, supplied_intent_id):
            calls.append(supplied_intent_id)
            return SimpleNamespace(lifecycle="UNDERSTANDING")

    import orchestrator.intent_control_plane as control_plane_module

    monkeypatch.setattr(
        control_plane_module, "get_intent_control_plane", lambda: ControlPlane()
    )
    client = TestClient(create_app())

    response = client.post(
        f"/api/v1/intents/{intent_id}/clarifications",
        headers={"X-API-Key": "demo-key-xxx"},
        json={"text": "destination = 10.0.0.10/32"},
    )

    assert response.status_code == 202
    assert response.json()["intent_id"] == intent_id
    assert calls == [intent_id]
    resumed = store.get(intent_id)
    assert resumed["status"] == "pending"
    assert resumed["snapshot"]["requires_user_input"] is False
    assert resumed["snapshot"]["missing_fields"] == []
    assert resumed["snapshot"]["phase"] == "PLANNING"
    assert len(store.list_jobs(intent_id)) == 1

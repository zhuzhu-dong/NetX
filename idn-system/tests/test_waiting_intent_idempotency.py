from api.store import IntentStore
from services.waiting_intent_ownership import WaitingIntentOwnershipService


def _waiting(store: IntentStore, intent_id: str, *, session_id: str, user_id: str, text: str, context=None):
    store.create(intent_id, text, user_id=user_id, session_id=session_id, context=context)
    store.update(intent_id, status="pending_clarification", snapshot={
        "needs_clarification": True,
        "clarification_details": [
            {"field": "source", "question": "请指定源对象"},
            {"field": "destination", "question": "请指定目的对象"},
            {"field": "service", "question": "请指定业务类型"},
        ],
    })


def test_waiting_intent_reuses_same_user_session_and_semantic_request(tmp_path):
    store = IntentStore(tmp_path / "waiting.db")
    text = "保障A点到B点的带宽大于10Mbps"
    _waiting(store, "INT-WAIT-1", session_id="SES-1", user_id="operator", text=text)

    reused = store.find_equivalent_waiting_intent(user_id="operator", session_id="SES-1", text=text)

    assert reused is not None
    assert reused["intent_id"] == "INT-WAIT-1"
    assert reused["waiting_intent_fingerprint"]


def test_waiting_intent_reuses_across_sessions_but_not_after_withdrawal(tmp_path):
    store = IntentStore(tmp_path / "waiting-isolation.db")
    text = "保障A点到B点的带宽大于10Mbps"
    _waiting(store, "INT-WAIT-1", session_id="SES-1", user_id="operator", text=text)

    reused = store.find_equivalent_waiting_intent(user_id="operator", session_id="SES-2", text=text)
    assert reused is not None
    assert reused["intent_id"] == "INT-WAIT-1"
    store.update("INT-WAIT-1", status="withdrawn")
    assert store.find_equivalent_waiting_intent(user_id="operator", session_id="SES-1", text=text) is None


def test_waiting_owner_key_respects_user_tenant_and_semantic_scope(tmp_path):
    store = IntentStore(tmp_path / "waiting-global-scope.db")
    text = "Protect h_research to h_dc bandwidth greater than 10Mbps"
    _waiting(store, "INT-WAIT-1", session_id="SES-1", user_id="operator", text=text)

    assert store.find_equivalent_waiting_intent(
        user_id="other-user", session_id="SES-2", text=text,
    ) is None
    assert store.find_equivalent_waiting_intent(
        user_id="operator", session_id="SES-2",
        text="Protect h_other to h_dc bandwidth greater than 10Mbps",
    ) is None
    assert store.find_equivalent_waiting_intent(
        user_id="operator", session_id="SES-2",
        text="Cap h_research to h_dc bandwidth at 10Mbps",
    ) is None


def test_waiting_logical_key_is_session_independent_and_tenant_scoped(tmp_path):
    store = IntentStore(tmp_path / "waiting-key.db")
    text = "Protect h_research to h_dc bandwidth greater than 10Mbps"
    _waiting(store, "INT-WAIT-1", session_id="SES-1", user_id="operator", text=text, context={"tenant_id": "tenant-a"})
    first = store.get("INT-WAIT-1")
    assert first is not None
    first_key = store.waiting_logical_key(first)

    _waiting(store, "INT-WAIT-2", session_id="SES-2", user_id="operator", text=text, context={"tenant_id": "tenant-a"})
    second = store.get("INT-WAIT-2")
    assert second is not None
    assert store.waiting_logical_key(second) == first_key

    _waiting(store, "INT-WAIT-3", session_id="SES-3", user_id="operator", text=text, context={"tenant_id": "tenant-b"})
    third = store.get("INT-WAIT-3")
    assert third is not None
    assert store.waiting_logical_key(third) != first_key


def test_legacy_duplicate_waiting_rows_have_one_deterministic_owner(tmp_path):
    store = IntentStore(tmp_path / "waiting-owner.db")
    text = "Protect h_research to h_dc bandwidth greater than 10Mbps"
    _waiting(store, "INT-WAIT-OLD", session_id="SES-OLD", user_id="operator", text=text)
    _waiting(store, "INT-WAIT-NEW", session_id="SES-NEW", user_id="operator", text=text)
    store.add_message("SES-NEW", "user", "clarification", "h_research and h_dc", intent_id="INT-WAIT-NEW")

    service = WaitingIntentOwnershipService(store)
    groups = service.groups()
    assert len(groups) == 1
    owner = service.authoritative_owner(next(iter(groups.values())))
    assert owner["intent_id"] == "INT-WAIT-NEW"


def test_waiting_key_normalizes_objective_and_service_diagnostic_aliases(tmp_path):
    store = IntentStore(tmp_path / "waiting-slot-aliases.db")
    text = "Protect h_research to h_dc bandwidth greater than 10Mbps"
    _waiting(store, "INT-WAIT-SERVICE", session_id="SES-1", user_id="operator", text=text)
    _waiting(store, "INT-WAIT-OBJECTIVE", session_id="SES-2", user_id="operator", text=text)
    store.update("INT-WAIT-OBJECTIVE", snapshot={
        "needs_clarification": True,
        "clarification_details": [
            {"field": "source_ip"}, {"field": "destination_ip"}, {"field": "objective"},
        ],
    })
    first = store.get("INT-WAIT-SERVICE")
    second = store.get("INT-WAIT-OBJECTIVE")
    assert first is not None and second is not None
    assert store.waiting_logical_key(first) == store.waiting_logical_key(second)

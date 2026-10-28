from __future__ import annotations


def test_preview_event_manager_is_ephemeral_and_never_hits_intent_fk(monkeypatch):
    """PREVIEW-* tracing must not require a fabricated production Intent row."""
    import api.events as events

    calls = []

    class Store:
        def add_event(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(events, "get_store", lambda: Store())
    manager = events.EventManager()

    manager.push("PREVIEW-20260810-abc", "stage_change", {"stage": "parse"})
    assert calls == []

    manager.push("INT-20260810-real", "stage_change", {"stage": "parse"})
    assert len(calls) == 1
    assert calls[0][0][0] == "INT-20260810-real"

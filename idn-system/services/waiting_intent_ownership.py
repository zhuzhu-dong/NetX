"""Formal ownership reconciliation for unresolved conversational intents.

Waiting intents have no network-policy scope yet, but they still own a logical
business request.  This service ensures that one user/tenant/logical request
has exactly one current waiting owner while retaining every prior conversation
as an auditable, terminal lifecycle record.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from api.store import IntentStore, get_store


class WaitingIntentOwnershipService:
    def __init__(self, store: IntentStore | None = None) -> None:
        self.store = store or get_store()

    def groups(self) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in self.store.list_waiting_intents():
            record["logical_waiting_key"] = self.store.waiting_logical_key(record)
            groups[record["logical_waiting_key"]].append(record)
        return groups

    def authoritative_owner(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Prefer the most complete, recently-updated live conversation."""
        def score(record: dict[str, Any]) -> tuple[int, str, str]:
            messages = [
                item for item in self.store.list_messages(record["session_id"])
                if str(item.get("intent_id") or "") == str(record["intent_id"])
            ]
            # A user clarification plus a current assistant answer is more
            # complete than an untouched duplicate; recency breaks ties.
            return (
                len(messages),
                str(record.get("updated_at") or ""),
                str(record.get("intent_id") or ""),
            )
        return max(records, key=score)

    async def reconcile(self) -> dict[str, Any]:
        """Withdraw duplicate waiting owners through the normal lifecycle path."""
        from orchestrator.intent_control_plane import get_intent_control_plane

        reconciled: list[dict[str, Any]] = []
        control_plane = get_intent_control_plane()
        for logical_key, records in self.groups().items():
            if len(records) < 2:
                continue
            owner = self.authoritative_owner(records)
            for duplicate in records:
                if duplicate["intent_id"] == owner["intent_id"]:
                    continue
                runtime = await control_plane.withdraw(duplicate["intent_id"])
                detail = {
                    "logical_waiting_key": logical_key,
                    "authoritative_owner": owner["intent_id"],
                    "terminal_lifecycle": runtime.lifecycle.value,
                }
                self.store.add_message(
                    duplicate["session_id"], "system", "waiting_owner_superseded",
                    f"Waiting intent superseded by logical owner {owner['intent_id']}",
                    intent_id=duplicate["intent_id"], payload=detail,
                )
                self.store.add_audit_event(
                    event_type="WAITING_INTENT_SUPERSEDED",
                    resource_type="intent",
                    resource_id=duplicate["intent_id"],
                    actor="waiting_owner_reconciler",
                    correlation=detail,
                    expected={"current_owner": owner["intent_id"]},
                    observed={"lifecycle": runtime.lifecycle.value},
                    action="FORMAL_WITHDRAWAL",
                )
                reconciled.append({
                    "intent_id": duplicate["intent_id"],
                    "authoritative_owner": owner["intent_id"],
                    "lifecycle": runtime.lifecycle.value,
                    "logical_waiting_key": logical_key,
                })
        return {"reconciled": reconciled, "groups": self.groups()}


async def reconcile_waiting_intent_owners(store: IntentStore | None = None) -> dict[str, Any]:
    return await WaitingIntentOwnershipService(store).reconcile()

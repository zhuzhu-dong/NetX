"""
api/conversation/timeline.py
Timeline assembly — interleave messages and intent milestones into a
single chronologically-ordered conversation view.

Replaces the old split where the frontend had to call separate
``/sessions/{id}/messages`` and ``/sessions/{id}/intents`` endpoints
and synthesize a timeline client-side.
"""

from __future__ import annotations

from typing import Any

from api.store import IntentStore


def build_timeline(
    store: IntentStore,
    session_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    include_intents: bool = True,
) -> dict[str, Any]:
    """Return a unified timeline for a conversation session.

    The timeline interleaves:
    - Chat messages (from the ``messages`` table)
    - Intent lifecycle milestones (derived from ``intents`` table)

    Each entry has::
      {id, session_id, role, type, content, metadata, intent_id, created_at}

    This is the single source for "what happened in this session."
    """
    # ── Get raw messages ──
    raw_messages = store.list_messages(session_id)

    # ── Build timeline entries from messages ──
    entries: list[dict] = []
    for msg in raw_messages:
        entry = {
            "id": msg.get("id", ""),
            "session_id": session_id,
            "role": msg.get("role", "user"),
            "type": msg.get("message_type", "user_text"),
            "content": msg.get("content", ""),
            "metadata": msg.get("payload"),
            "intent_id": msg.get("intent_id"),
            "created_at": msg.get("created_at", ""),
        }
        entries.append(entry)

    # ── Add intent milestone entries (if requested) ──
    if include_intents:
        intent_ids_seen: set[str] = set()
        for entry in entries:
            if entry.get("intent_id"):
                intent_ids_seen.add(entry["intent_id"])

        intents_resp = store.list(session_id=session_id, page=1, page_size=100)
        for intent in intents_resp.get("items", []):
            intent_id = intent.get("intent_id", "")
            if intent_id in intent_ids_seen:
                continue

            milestones = intent.get("milestones") or []
            for milestone in milestones:
                milestone_ts = milestone.get("timestamp") or intent.get("created_at", "")
                entries.append({
                    "id": f"{intent_id}-milestone-{milestone.get('stage', '')}",
                    "session_id": session_id,
                    "role": "system",
                    "type": "stage_progress",
                    "content": milestone.get("summary", ""),
                    "metadata": {"stage": milestone.get("stage"), "status": milestone.get("status")},
                    "intent_id": intent_id,
                    "created_at": milestone_ts,
                })

            # Add final result if intent is complete
            status = intent.get("status", "")
            if status in ("success", "failed", "cancelled"):
                result = intent.get("result") or {}
                entries.append({
                    "id": f"{intent_id}-result",
                    "session_id": session_id,
                    "role": "system",
                    "type": "execution_result",
                    "content": _result_summary(status, result),
                    "metadata": result,
                    "intent_id": intent_id,
                    "created_at": intent.get("updated_at", intent.get("created_at", "")),
                })

    # ── Sort by created_at ──
    entries.sort(key=lambda e: e.get("created_at", ""))

    # ── Paginate ──
    total = len(entries)
    start = (page - 1) * page_size
    end = start + page_size
    page_entries = entries[start:end]

    return {
        "session_id": session_id,
        "items": page_entries,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _result_summary(status: str, result: dict) -> str:
    """Generate a human-readable summary for an intent result."""
    if status == "success":
        report = result.get("decision_report") or {}
        return report.get("final_conclusion") or report.get("summary") or "意图执行成功。"
    elif status == "failed":
        snapshot = result.get("snapshot") or {}
        return snapshot.get("error_message") or "意图执行失败。"
    elif status == "cancelled":
        return "意图已被取消。"
    return f"意图状态: {status}"

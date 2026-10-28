"""
api/conversation/stream.py
SSE event stream for a conversation.

Provides the ``conversation_event_stream()`` async generator that the
route handler wraps in a ``StreamingResponse``.  This is the *unified*
stream — it pushes all conversation events (tokens, messages, stage
progress, heartbeats) through a single SSE connection.

The stream watches for:
- New messages persisted to the store (polled every 500ms, or via event
  manager notifications)
- Pipeline stage changes (from the ExecutionTracker via EventManager)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator

from api.conversation.models import SSEEvent
from api.store import IntentStore

logger = logging.getLogger(__name__)


async def conversation_event_stream(
    session_id: str,
    store: IntentStore,
    *,
    heartbeat_interval: float = 15.0,
    poll_interval: float = 0.5,
    idle_timeout: float = 10.0,
) -> AsyncIterator[str]:
    """Yield SSE frames for all events in a conversation.

    Polls for new messages and intent events associated with the session.
    Emits heartbeats to keep the connection alive.  The caller wraps this
    in a ``StreamingResponse``.

    Each yielded string is a complete SSE frame (``event: ...\\ndata: ...\\n\\n``).
    """
    # ── Track what we've already sent ──
    last_message_id: str | None = None
    last_event_id: int = 0
    idle_count = 0
    max_idle_ticks = int(idle_timeout / poll_interval)

    # Send initial snapshot of all existing messages
    existing = store.list_messages(session_id)
    for msg_dict in existing:
        from api.conversation.models import Message, MessageType
        msg = Message(
            id=msg_dict.get("id", ""),
            session_id=session_id,
            role=msg_dict.get("role", "user"),
            type=MessageType(msg_dict.get("message_type", "user_text")),
            content=msg_dict.get("content", ""),
            metadata=msg_dict.get("payload"),
            intent_id=msg_dict.get("intent_id"),
            created_at=msg_dict.get("created_at", ""),
        )
        yield msg.to_sse(event_type="message_created")
        last_message_id = msg.id

    # ── Poll loop for new messages ──
    while True:
        try:
            # Check for new messages
            all_messages = store.list_messages(session_id)
            new_messages = []
            found_last = last_message_id is None
            for msg_dict in all_messages:
                if not found_last:
                    if msg_dict.get("id") == last_message_id:
                        found_last = True
                    continue
                new_messages.append(msg_dict)

            if new_messages:
                idle_count = 0
                from api.conversation.models import Message, MessageType
                for msg_dict in new_messages:
                    msg = Message(
                        id=msg_dict.get("id", ""),
                        session_id=session_id,
                        role=msg_dict.get("role", "user"),
                        type=MessageType(msg_dict.get("message_type", "user_text")),
                        content=msg_dict.get("content", ""),
                        metadata=msg_dict.get("payload"),
                        intent_id=msg_dict.get("intent_id"),
                        created_at=msg_dict.get("created_at", ""),
                    )
                    yield msg.to_sse(event_type="message_created")
                    last_message_id = msg.id

            else:
                idle_count += 1
                if idle_count >= max_idle_ticks:
                    # Send keepalive comment (not a real event)
                    yield ": keepalive\n\n"
                    idle_count = 0

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception("Error in conversation SSE stream for %s", session_id)
            yield SSEEvent.error(f"Stream error: {exc}").to_sse()
            break

        await asyncio.sleep(poll_interval)

"""
api/execution/projector.py
StateProjector — read from LangGraph Checkpointer and project into API Store.

Eliminates the dual-persistence consistency gap.  Instead of the API Store
independently storing ``snapshot_json``/``result_json`` extracted by
``_workflow_record()``, the projector reads the authoritative state from
the checkpointer on demand and materializes a read-optimized projection.

The API Store becomes a **projection** — a materialized view derived from
checkpoint state, rebuilt incrementally after each checkpoint write.

Caching: hot intents are cached in-memory (TTL 60 s) to avoid repeated
checkpointer reads for polling clients.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# In-memory cache for hot intent projections
_projection_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 60


class StateProjector:
    """Project checkpointer state into API-store-compatible dicts.

    Usage::

        projector = StateProjector()
        projection = await projector.project_intent_state(intent_id)
        # projection has: status, snapshot, milestones, result
    """

    def __init__(self):
        self._checkpointer = None
        self._initialized = False

    async def _ensure_checkpointer(self):
        if self._initialized:
            return
        try:
            from orchestrator.checkpointer import get_async_checkpointer
            self._checkpointer = await get_async_checkpointer("sqlite")
        except Exception as exc:
            logger.warning("StateProjector: checkpointer unavailable: %s", exc)
        self._initialized = True

    async def project_intent_state(self, intent_id: str) -> dict[str, Any]:
        """Return an API-store-compatible projection of the intent's state.

        Returns a dict with keys matching the old IntentStore.get() shape:
        ``{status, snapshot, milestones, result}`` — plus a ``source``
        field indicating where the data came from.
        """
        # Check cache first
        cached = _projection_cache.get(intent_id)
        if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
            return {**cached[1], "source": "cache"}

        await self._ensure_checkpointer()

        if self._checkpointer is None:
            return self._empty_projection(intent_id, "checkpointer unavailable")

        try:
            # We need the session_id to build the thread_id.
            # Fall back to searching the API store for the intent record.
            from api.store import get_current_store
            store = get_current_store()
            record = store.get(intent_id) if store else None
            session_id = record.get("session_id", "") if record else ""
            thread_id = f"{intent_id}::{session_id}"

            config = {"configurable": {"thread_id": thread_id}}
            checkpoint = await self._checkpointer.aget_tuple(config)

            if checkpoint is None or checkpoint.checkpoint is None:
                return self._empty_projection(intent_id, "no checkpoint found")

            channel_values = checkpoint.checkpoint.get("channel_values", {})
            if not channel_values:
                return self._empty_projection(intent_id, "empty checkpoint")

            # Extract from GlobalAgentState
            from api.worker import _workflow_record, _jsonable
            from models.agent_state import GlobalAgentState

            state = GlobalAgentState(**channel_values)
            status, snapshot, milestones, result = _workflow_record(state)

            projection = {
                "status": status,
                "snapshot": snapshot,
                "milestones": milestones,
                "result": result,
                "source": "checkpointer",
                "checkpoint_ts": checkpoint.checkpoint.get("ts"),
            }

            # Cache the projection
            _projection_cache[intent_id] = (time.monotonic(), projection)

            return projection

        except Exception as exc:
            logger.exception("StateProjector: failed to project intent %s: %s", intent_id, exc)
            return self._empty_projection(intent_id, f"projection error: {exc}")

    def _empty_projection(self, intent_id: str, reason: str) -> dict[str, Any]:
        return {
            "status": "unknown",
            "snapshot": None,
            "milestones": [],
            "result": None,
            "source": f"empty — {reason}",
        }

    @staticmethod
    def invalidate_cache(intent_id: str | None = None) -> None:
        """Clear cached projections.

        Call after a checkpoint write to ensure the next read returns
        fresh data.
        """
        if intent_id:
            _projection_cache.pop(intent_id, None)
        else:
            _projection_cache.clear()

    @staticmethod
    def cache_size() -> int:
        return len(_projection_cache)

"""
api/execution/tracker.py
ExecutionTracker — emits pipeline progress events into the conversation stream.

Called by worker.py during STAV pipeline execution.  Each event is:
1. Persisted as a Message in the ``messages`` table (so the timeline
   survives page refresh)
2. Emitted as an SSE event to the conversation stream (for live UI updates)
3. Optionally emits partial decision report fragments for incremental
   rendering in the frontend
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from api.conversation.models import Message, MessageType, SSEEvent

logger = logging.getLogger(__name__)

# ── Incremental report fragments: which fields become available at which stage ──
STAGE_REPORT_FIELDS: dict[str, list[str]] = {
    "sense": ["network_observation", "network_evidence"],
    "parse": ["intent_understanding"],
    "check": ["policy_conflict"],
    "generate": [],   # policy compilation — internal
    "execute": [],    # flow delivery — internal
    "verify": ["verification_result", "problem_diagnosis", "candidate_solutions",
               "final_action", "fallback_plan"],
}


class ExecutionTracker:
    """Tracks STAV pipeline execution and emits events to the conversation.

    Usage in worker.py::

        tracker = ExecutionTracker(intent_id, session_id, store, on_sse_event)
        await tracker.on_stage_start("sense", "network_sensing")
        # ... run sense node ...
        await tracker.on_stage_complete("sense", {"status": "success"},
                                         partial_report={"network_observation": {...}})
    """

    def __init__(
        self,
        intent_id: str,
        session_id: str,
        store,
        on_sse_event: Optional[Callable[[SSEEvent], None]] = None,
    ):
        self.intent_id = intent_id
        self.session_id = session_id
        self._store = store
        self._on_sse = on_sse_event or (lambda _: None)
        self._accumulated_report: dict[str, Any] = {}
        self._stage_start_times: dict[str, float] = {}

    async def on_stage_start(self, stage: str, agent_id: str) -> None:
        """Called when a pipeline stage begins."""
        import time
        self._stage_start_times[stage] = time.monotonic()

        msg = Message.stage_progress(
            self.session_id, self.intent_id, stage, "running",
            f"⏳ {_stage_label(stage)} 进行中...",
        )
        self._persist(msg)

        event = SSEEvent.stage_progress(
            self.intent_id, stage, "running",
            f"⏳ {_stage_label(stage)} 进行中...",
        )
        self._on_sse(event)

        # Also push to event manager for the existing intent SSE endpoint
        try:
            from api.events import get_event_manager
            get_event_manager().push(self.intent_id, "stage_change", {
                "stage": stage, "agent_id": agent_id, "status": "running",
                "summary": f"{_stage_label(stage)} started",
            })
        except Exception:
            pass

    async def on_stage_complete(
        self, stage: str, result: dict, partial_report: dict | None = None,
    ) -> None:
        """Called when a pipeline stage completes successfully.

        ``partial_report`` carries the subset of the decision report that
        became available at this stage (see STAGE_REPORT_FIELDS).
        """
        import time
        duration_ms = None
        if stage in self._stage_start_times:
            duration_ms = (time.monotonic() - self._stage_start_times[stage]) * 1000

        summary = result.get("summary") or f"✅ {_stage_label(stage)} 完成"
        status = result.get("status", "success")

        # Accumulate report fragments
        if partial_report:
            self._accumulated_report.update(partial_report)

        msg = Message.stage_progress(
            self.session_id, self.intent_id, stage, status, summary,
            partial_report=partial_report,
        )
        self._persist(msg)

        event = SSEEvent.stage_progress(
            self.intent_id, stage, status, summary,
            partial_report=partial_report,
        )
        self._on_sse(event)

        # Push to event manager
        try:
            from api.events import get_event_manager
            get_event_manager().push(self.intent_id, "stage_change", {
                "stage": stage, "status": status, "summary": summary,
                "duration_ms": duration_ms,
            })
            if partial_report:
                get_event_manager().push(self.intent_id, "phase_progress", {
                    "stage": stage, "message": summary,
                    "partial_report": partial_report,
                })
        except Exception:
            pass

    async def on_clarification_needed(self, question: str) -> None:
        """Called when the pipeline pauses for user clarification."""
        msg = Message.clarification_question(self.session_id, self.intent_id, question)
        self._persist(msg)
        self._on_sse(SSEEvent.message_created(msg))

    async def on_approval_required(self, context: dict) -> None:
        """Called when the pipeline requires human approval."""
        msg = Message.approval_required(self.session_id, self.intent_id, context)
        self._persist(msg)
        self._on_sse(SSEEvent.message_created(msg))

    async def on_execution_complete(self, final_result: dict) -> None:
        """Called when the STAV pipeline finishes (success or failure)."""
        status = final_result.get("status", "unknown")
        if status == "success":
            summary = final_result.get("summary", "✅ 意图执行完成，KPI 验证通过。")
        elif status == "failed":
            summary = final_result.get("summary", "❌ 意图执行失败。")
        else:
            summary = f"意图执行结束，状态: {status}"

        # Merge accumulated report with final result
        report = {**self._accumulated_report, **(final_result.get("decision_report") or {})}

        msg = Message.execution_result(
            self.session_id, self.intent_id, summary,
            result={"decision_report": report, **final_result},
        )
        self._persist(msg)
        self._on_sse(SSEEvent.message_created(msg))

        # Update intent record with accumulated report
        try:
            self._store.update(self.intent_id, result={
                "decision_report": report,
                "a2a_timeline": final_result.get("a2a_timeline", []),
                "a2a_runtime_evidence": final_result.get("a2a_runtime_evidence", {}),
            })
        except Exception as exc:
            logger.exception("Failed to update intent %s with final result: %s",
                           self.intent_id, exc)

    async def on_error(self, error_message: str, stage: str = "", details: dict | None = None) -> None:
        """Called when an error occurs during execution."""
        msg = Message.error(
            self.session_id, f"❌ {error_message}",
            intent_id=self.intent_id, details={"stage": stage, **(details or {})},
        )
        self._persist(msg)
        self._on_sse(SSEEvent.message_created(msg))

    def _persist(self, message: Message) -> None:
        """Persist a message to the store (best-effort)."""
        try:
            self._store.add_message(
                session_id=message.session_id,
                role=message.role,
                message_type=message.type.value,
                content=message.content,
                intent_id=message.intent_id,
                payload=message.metadata,
            )
        except Exception as exc:
            logger.exception("ExecutionTracker: failed to persist message %s: %s",
                           message.id, exc)


def _stage_label(stage: str) -> str:
    """Human-readable label for a pipeline stage."""
    labels = {
        "sense": "网络拓扑感知",
        "parse": "意图约束解析",
        "check": "冲突与风险检测",
        "generate": "策略编译生成",
        "execute": "流表下发执行",
        "verify": "闭环验证",
    }
    return labels.get(stage, stage)

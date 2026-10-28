"""
api/conversation/models.py
Unified message and conversation data models.

Every interaction — user text, assistant reply, pipeline progress,
clarification, approval, execution result, error — is a **Message**.
Messages are ordered by ``created_at`` to form a **Conversation Timeline**.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "MSG") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class MessageType(str, Enum):
    """All interaction types in a conversation timeline."""

    # ── Chat messages ──
    USER_TEXT = "user_text"
    ASSISTANT_TEXT = "assistant_text"
    ASSISTANT_THINKING = "assistant_thinking"  # streaming in-progress
    SYSTEM = "system"

    # ── Intent lifecycle ──
    INTENT_SUBMITTED = "intent_submitted"
    STAGE_PROGRESS = "stage_progress"          # pipeline stage update
    CLARIFICATION_QUESTION = "clarification_question"
    CLARIFICATION_ANSWER = "clarification_answer"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_DECISION = "approval_decision"
    EXECUTION_RESULT = "execution_result"
    VERIFICATION_RESULT = "verification_result"

    # ── Errors ──
    ERROR = "error"


class Message(BaseModel):
    """A single entry in the conversation timeline.

    ``role`` describes who produced the message: user, assistant, or system.
    ``type`` describes what kind of interaction it represents.
    ``content`` is the human-readable text (markdown for assistant messages).
    ``metadata`` carries type-specific structured payload (e.g. partial
    decision report, approval context, stage details).
    """

    id: str = Field(default_factory=lambda: _new_id("MSG"))
    session_id: str
    role: Literal["user", "assistant", "system"]
    type: MessageType
    content: str = ""
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Type-specific payload")
    intent_id: Optional[str] = Field(default=None, description="Null for pure chat messages")
    created_at: str = Field(default_factory=_utc_now)

    @classmethod
    def user_text(cls, session_id: str, content: str) -> "Message":
        return cls(session_id=session_id, role="user", type=MessageType.USER_TEXT, content=content)

    @classmethod
    def assistant_text(cls, session_id: str, content: str, metadata: dict | None = None) -> "Message":
        return cls(session_id=session_id, role="assistant", type=MessageType.ASSISTANT_TEXT,
                   content=content, metadata=metadata)

    @classmethod
    def assistant_thinking(cls, session_id: str) -> "Message":
        return cls(session_id=session_id, role="assistant", type=MessageType.ASSISTANT_THINKING, content="")

    @classmethod
    def intent_submitted(cls, session_id: str, intent_id: str, content: str) -> "Message":
        return cls(session_id=session_id, role="system", type=MessageType.INTENT_SUBMITTED,
                   content=content, intent_id=intent_id)

    @classmethod
    def stage_progress(cls, session_id: str, intent_id: str, stage: str, status: str,
                       summary: str, partial_report: dict | None = None) -> "Message":
        return cls(session_id=session_id, role="system", type=MessageType.STAGE_PROGRESS,
                   content=summary, intent_id=intent_id,
                   metadata={"stage": stage, "status": status, "partial_report": partial_report})

    @classmethod
    def clarification_question(cls, session_id: str, intent_id: str, question: str) -> "Message":
        return cls(session_id=session_id, role="assistant", type=MessageType.CLARIFICATION_QUESTION,
                   content=question, intent_id=intent_id)

    @classmethod
    def clarification_answer(cls, session_id: str, intent_id: str, answer: str) -> "Message":
        return cls(session_id=session_id, role="user", type=MessageType.CLARIFICATION_ANSWER,
                   content=answer, intent_id=intent_id)

    @classmethod
    def approval_required(cls, session_id: str, intent_id: str, context: dict) -> "Message":
        return cls(session_id=session_id, role="system", type=MessageType.APPROVAL_REQUIRED,
                   content=context.get("summary", "需要人工审批"), intent_id=intent_id, metadata=context)

    @classmethod
    def approval_decision(cls, session_id: str, intent_id: str, decision: str, reason: str = "") -> "Message":
        return cls(session_id=session_id, role="user", type=MessageType.APPROVAL_DECISION,
                   content=f"审批决策: {decision}", intent_id=intent_id,
                   metadata={"decision": decision, "reason": reason})

    @classmethod
    def execution_result(cls, session_id: str, intent_id: str, summary: str,
                         result: dict | None = None) -> "Message":
        return cls(session_id=session_id, role="system", type=MessageType.EXECUTION_RESULT,
                   content=summary, intent_id=intent_id, metadata=result)

    @classmethod
    def error(cls, session_id: str, error_message: str, intent_id: str | None = None,
              details: dict | None = None) -> "Message":
        return cls(session_id=session_id, role="system", type=MessageType.ERROR,
                   content=error_message, intent_id=intent_id, metadata=details)

    @classmethod
    def system(cls, session_id: str, content: str, metadata: dict | None = None) -> "Message":
        return cls(session_id=session_id, role="system", type=MessageType.SYSTEM,
                   content=content, metadata=metadata)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "type": self.type.value,
            "content": self.content,
            "metadata": self.metadata,
            "intent_id": self.intent_id,
            "created_at": self.created_at,
        }

    def to_sse(self, event_type: str = "message_created") -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False)
        return f"event: {event_type}\ndata: {payload}\n\n"


class Conversation(BaseModel):
    """Thin wrapper for conversation (session) metadata."""

    session_id: str
    user_id: str = "anonymous"
    title: Optional[str] = None
    message_count: int = 0
    last_message_at: Optional[str] = None
    created_at: str = Field(default_factory=_utc_now)
    updated_at: str = Field(default_factory=_utc_now)


class SSEEvent:
    """A single SSE event emitted by the conversation stream.

    Types:
      - ``token`` — streaming LLM token (data: {token, message_id, seq})
      - ``message_created`` — a new Message was persisted
      - ``message_updated`` — an existing Message was modified (e.g. thinking → text)
      - ``stage_progress`` — pipeline stage change (dedicated event for UI)
      - ``heartbeat`` — keepalive
      - ``stream_end`` — stream finished normally
      - ``error`` — error event
    """

    def __init__(self, event_type: str, data: dict | None = None):
        self.event_type = event_type
        self.data = data or {}

    def to_sse(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False)
        return f"event: {self.event_type}\ndata: {payload}\n\n"

    @classmethod
    def token(cls, token: str, message_id: str, seq: int = 0) -> "SSEEvent":
        return cls("token", {"token": token, "message_id": message_id, "seq": seq})

    @classmethod
    def message_created(cls, message: Message) -> "SSEEvent":
        return cls("message_created", message.to_dict())

    @classmethod
    def message_updated(cls, message: Message) -> "SSEEvent":
        return cls("message_updated", message.to_dict())

    @classmethod
    def stage_progress(cls, intent_id: str, stage: str, status: str, summary: str,
                       partial_report: dict | None = None) -> "SSEEvent":
        return cls("stage_progress", {
            "intent_id": intent_id, "stage": stage, "status": status,
            "summary": summary, "partial_report": partial_report,
        })

    @classmethod
    def heartbeat(cls) -> "SSEEvent":
        return cls("heartbeat", {"ts": _utc_now()})

    @classmethod
    def stream_end(cls, extra: dict | None = None) -> "SSEEvent":
        data = {"done": True}
        if extra:
            data.update(extra)
        return cls("stream_end", data)

    @classmethod
    def error(cls, message: str, details: dict | None = None) -> "SSEEvent":
        return cls("error", {"message": message, "details": details or {}})


class ApprovalContext(BaseModel):
    """Structured context for a human approval request.

    Populated by HumanApprovalNode and rendered by the frontend as an
    approval card with clear Accept / Reject / Abort buttons.
    """

    summary: str = Field(default="", description="One-line summary of what needs approval")
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    risk_score: int = Field(default=50, ge=0, le=100)
    proposed_changes: List[Dict[str, Any]] = Field(default_factory=list,
        description="Each change: {device, table, action, params}")
    alternatives: List[Dict[str, Any]] = Field(default_factory=list,
        description="Alternative approaches")
    rollback_plan: str = Field(default="", description="Steps to roll back if needed")
    hard_blockers: List[str] = Field(default_factory=list,
        description="Safety council blockers (if any)")
    agent_votes: List[Dict[str, Any]] = Field(default_factory=list,
        description="Per-agent votes: {agent_id, vote, risk_score, reason}")
    created_at: str = Field(default_factory=_utc_now)

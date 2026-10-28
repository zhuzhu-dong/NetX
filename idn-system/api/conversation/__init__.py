"""
api/conversation — IDN unified conversation subsystem.

Replaces the old split design where chat messages and intent executions
were tracked in separate tables with no unified timeline.  This module
treats every interaction (user text, assistant reply, pipeline progress,
clarification, approval, execution result, error) as a **Message** on a
single chronologically-ordered **Timeline**.

Core entry point: ``ChatSession`` — one interface for the route handler.
"""

from .models import Message, MessageType, Conversation, SSEEvent
from .session import ChatSession
from .timeline import build_timeline

__all__ = [
    "Message",
    "MessageType",
    "Conversation",
    "SSEEvent",
    "ChatSession",
    "build_timeline",
]

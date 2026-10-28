"""
api/execution — Pipeline execution tracking and state projection.

- ``ExecutionTracker`` emits pipeline progress events into the conversation
  stream (via SSE) and persists them as Messages.
- ``StateProjector`` reads from the LangGraph Checkpointer and projects
  into the API Store, eliminating the dual-persistence consistency gap.
"""

from .tracker import ExecutionTracker
from .projector import StateProjector

__all__ = ["ExecutionTracker", "StateProjector"]

"""
api/events.py
=============

SSE 事件管理器 —— 工作流进度事件的持久化发布。

# 系统角色
将编排节点产生的阶段变更和进度事件写入 IntentStore，
供 API 层通过 SSE 连接流式推送给前端。

# 设计意图
EventManager 作为发布者/订阅者之间的桥梁：
- 编排节点（生产者）通过 push() 发布事件
- API 端点（消费者）从 IntentStore 读取事件并通过 SSE 流式输出
- 基于 SQLite 的持久化确保事件不会因进程重启丢失
"""
from __future__ import annotations

from typing import Any

from api.store import get_store


class EventManager:
    """Write SSE events to shared storage so API and worker may be separate processes."""

    def push(self, intent_id: str, event_type: str, data: dict[str, Any]) -> None:
        # Preview graphs intentionally use a synthetic PREVIEW-* id and an
        # in-memory checkpoint.  ``events.intent_id`` has a foreign key to a
        # persisted production intent, so writing a child event here creates
        # avoidable FK noise.  Do not manufacture an Intent row merely for
        # tracing: previews are read-only and their trace is ephemeral.
        if str(intent_id).startswith("PREVIEW-"):
            return
        get_store().add_event(intent_id, event_type, data)


_event_manager: EventManager | None = None


def get_event_manager() -> EventManager:
    global _event_manager
    if _event_manager is None:
        _event_manager = EventManager()
    return _event_manager

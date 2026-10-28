"""
persistence/approval_store.py
==============================

人工审批状态持久化 —— 工作流暂停/恢复的数据层。

# 系统角色
将需要人工审批的工作流状态持久化到 SQLite（通过 IntentStore），
提供 CRUD 接口供 API 层（审批/拒绝/查询）使用。

# 主要依赖
- api/store.py：IntentStore（共享 SQLite 数据库）
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ApprovalStore:
    """CRUD for pending human-approval workflow states.

    Persisted to the shared SQLite app database via ``IntentStore``.
    """

    def __init__(self, store: Any = None):
        from api.store import get_store
        self._store = store or get_store()

    def save(
        self,
        intent_id: str,
        state: Any,
        reason: str,
        *,
        approval_id: Optional[str] = None,
    ) -> str:
        """Persist a workflow state awaiting human approval.

        Returns the ``approval_id`` for later resolution.
        """
        approval_id = approval_id or f"APPROVAL-{uuid.uuid4().hex[:12].upper()}"
        state_json = json.dumps(
            state.model_dump(mode="json") if hasattr(state, "model_dump") else state,
            ensure_ascii=False,
            default=str,
        )
        now = datetime.now(timezone.utc).isoformat()

        with self._store._lock, self._store._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_approvals(approval_id, intent_id, state_json, reason, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (approval_id, intent_id, state_json, reason, now),
            )
        logger.info("Approval stored: %s for intent %s", approval_id, intent_id)
        return approval_id

    def load(self, approval_id: str) -> dict[str, Any] | None:
        with self._store._lock, self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_pending(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._store._lock, self._store._connect() as conn:
            rows = conn.execute(
                """
                SELECT approval_id, intent_id, reason, status, created_at
                FROM pending_approvals WHERE status = 'pending'
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve(self, approval_id: str, *, approved: bool) -> dict[str, Any] | None:
        status = "approved" if approved else "rejected"
        now = datetime.now(timezone.utc).isoformat()
        with self._store._lock, self._store._connect() as conn:
            conn.execute(
                """
                UPDATE pending_approvals SET status = ?, resolved_at = ?
                WHERE approval_id = ? AND status = 'pending'
                """,
                (status, now, approval_id),
            )
        logger.info("Approval %s resolved: %s", approval_id, status)
        return self.load(approval_id)

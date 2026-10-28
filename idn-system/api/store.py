"""
api/store.py
============

SQLite 持久化存储 —— 对话、消息和意图执行记录。

# 系统角色
提供 IDN-System 的所有持久化需求：
- 意图记录：提交、状态更新、结果回写
- 对话消息：用户/系统消息的持久化和检索
- 会话管理：多轮对话的 session 隔离
- Stale Job Recovery：检测被中断的任务

# 设计决策
- 与 LangGraph checkpointer 共享同一 SQLite 文件（IDN_APP_DB_PATH），
  确保意图状态和 checkpoint 在同一事务域内
- 线程安全：使用 threading.Lock 保护 SQLite 写操作
- JSON 序列化：复杂字段（result, snapshot）以 JSON 字符串存储

# 主要依赖
- sqlite3（标准库）
- threading.Lock（线程安全写保护）
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_load(value: Optional[str], default: Any = None) -> Any:
    return json.loads(value) if value else default


_WAITING_STATUSES = {"pending_clarification", "waiting_for_user", "pending_user_input"}


def _waiting_intent_identity(text: str, unresolved_slots: list[Any] | None = None) -> dict[str, Any]:
    """Stable semantic identity for an unfinished conversational Intent.

    This is intentionally richer than a text Set: normalized wording anchors
    the original request, while intent family/objective and unresolved slots
    explain which waiting owner may be resumed.
    """
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").casefold())
    compact = normalized.replace("mbps", "m")
    family = "BANDWIDTH" if any(token in compact for token in ("带宽", "bandwidth", "Mbps", "mbps")) else "GENERAL"
    objective = "MIN_BANDWIDTH" if any(token in compact for token in ("保障", "不低于", "大于", "minimum", "greater")) else "UNSPECIFIED"
    slot_aliases = {
        "source_ip": "source", "src": "source",
        "destination_ip": "destination", "dst": "destination",
        "service_or_protocol": "service", "protocol": "service",
        "port": "service", "destination_port": "service",
        # A missing business objective in this conversational parser means the
        # same user-facing service/business clarification, not a new owner.
        "objective": "service",
    }
    slots = sorted({
        slot_aliases.get(str(slot or "").strip().lower(), str(slot or "").strip().lower())
        for slot in (unresolved_slots or []) if str(slot or "").strip()
    })
    return {"normalized_original_intent": normalized, "intent_family": family, "objective": objective, "unresolved_slots": slots}


def _waiting_tenant(context: dict[str, Any] | None = None) -> str:
    """Resolve the tenant scope without making a missing legacy field global."""
    context = context or {}
    return str(
        context.get("tenant_id")
        or context.get("tenant")
        or context.get("workspace_id")
        or "default"
    ).strip().casefold()


def logical_waiting_identity(
    text: str,
    unresolved_slots: list[Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a session-independent semantic identity for a waiting owner.

    A session is conversation context only.  It must never allow a second
    current owner for a user's unresolved logical business request.
    """
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").casefold())
    compact = normalized.replace("mbps", "m")
    family = "BANDWIDTH" if any(token in compact for token in ("\u5e26\u5bbd", "bandwidth", "mbps")) else "GENERAL"
    objective = "MIN_BANDWIDTH" if any(token in compact for token in ("\u4fdd\u969c", "\u4e0d\u4f4e\u4e8e", "\u5927\u4e8e", "minimum", "greater")) else "UNSPECIFIED"
    slot_aliases = {
        "source_ip": "source", "src": "source",
        "destination_ip": "destination", "dst": "destination",
        "service_or_protocol": "service", "protocol": "service",
        "port": "service", "destination_port": "service",
        "objective": "service",
    }
    slots = sorted({
        slot_aliases.get(str(slot or "").strip().lower(), str(slot or "").strip().lower())
        for slot in (unresolved_slots or []) if str(slot or "").strip()
    })
    known_selector_facts = sorted(set(re.findall(r"(?:\d{1,3}\.){3}\d{1,3}|h_[a-z0-9_-]+", (text or "").casefold())))
    return {
        "tenant_id": _waiting_tenant(context),
        "normalized_original_intent": normalized,
        "intent_family": family,
        "objective": objective,
        "known_selector_facts": known_selector_facts,
        "unresolved_slots": slots,
    }


def logical_waiting_key(
    *, user_id: str, text: str, unresolved_slots: list[Any] | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """Deterministic owner key for one user's unresolved logical intent."""
    identity = logical_waiting_identity(text, unresolved_slots, context)
    payload = {"user_id": str(user_id or "anonymous").casefold(), **identity}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def logical_waiting_request_key(
    *, user_id: str, text: str, context: dict[str, Any] | None = None,
) -> str:
    """Match a newly submitted raw request before its slots are parsed.

    The durable owner key includes unresolved slots.  A fresh POST does not
    have those diagnostics yet, so creation lookup compares the stable request
    portion and then returns the existing authoritative owner with its slots.
    """
    identity = logical_waiting_identity(text, context=context)
    identity.pop("unresolved_slots", None)
    payload = {"user_id": str(user_id or "anonymous").casefold(), **identity}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class IntentStore:
    """Persist API-facing dialogue and intent results in a local SQLite file."""

    def __init__(self, db_path: str | Path | None = None):
        default_path = Path(__file__).resolve().parents[1] / "data" / "app.db"
        resolved_path = (
            db_path
            if db_path is not None
            else (os.getenv("IDN_APP_DB_PATH") or str(default_path))
        )
        self.db_path = Path(resolved_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open one transactional connection and always close its descriptors.

        ``sqlite3.Connection.__exit__`` commits or rolls back but deliberately
        does not close the connection.  The store has many short-lived calls,
        so relying on ``with sqlite3.connect(...)`` leaked DB/WAL descriptors
        on every telemetry and reconciliation cycle.
        """
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        _busy_timeout_ms = int(os.getenv("IDN_SQLITE_BUSY_TIMEOUT_MS", "5000"))
        connection.execute(f"PRAGMA busy_timeout = {_busy_timeout_ms}")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            journal_mode = os.getenv("IDN_SQLITE_JOURNAL_MODE", "WAL").upper()
            if journal_mode not in {
                "DELETE",
                "TRUNCATE",
                "PERSIST",
                "MEMORY",
                "WAL",
                "OFF",
            }:
                journal_mode = "WAL"
            for _ in range(3):
                try:
                    connection.execute(f"PRAGMA journal_mode = {journal_mode}")
                    break
                except sqlite3.OperationalError:
                    # WAL transition requires a write lock which may be held
                    # by another container during concurrent startup.
                    import time

                    time.sleep(1.0)
            else:
                # After retries, accept the current mode if already correct.
                current = connection.execute("PRAGMA journal_mode").fetchone()[0]
                if current.upper() != journal_mode:
                    raise sqlite3.OperationalError(
                        f"Could not set journal_mode={journal_mode} "
                        f"(current={current})"
                    )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    user_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    execution_text TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    status TEXT NOT NULL,
                    context_json TEXT,
                    snapshot_json TEXT,
                    milestones_json TEXT NOT NULL,
                    result_json TEXT,
                    checkpoint_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    intent_id TEXT REFERENCES intents(intent_id),
                    role TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intent_jobs (
                    job_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    heartbeat_at TEXT,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
                    event_type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watcher_incidents (
                    incident_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    reason TEXT,
                    kpi_snapshot TEXT,
                    triggered_intent_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS watcher_samples (
                    sample_id TEXT PRIMARY KEY,
                    incident_id TEXT,
                    kpi_snapshot TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS policies (
                    policy_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
                    action_type TEXT NOT NULL,
                    yang_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS policy_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    policy_id TEXT NOT NULL,
                    traffic_scope_key TEXT,
                    intent_id TEXT,
                    version INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    old_yang_json TEXT,
                    new_yang_json TEXT,
                    runtime_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watcher_state (
                    state_id TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS experience_cases (
                    case_id TEXT PRIMARY KEY,
                    intent_id TEXT,
                    intent_type TEXT,
                    symptom TEXT,
                    evidence_json TEXT NOT NULL,
                    root_cause TEXT,
                    repair_action_json TEXT NOT NULL,
                    before_kpi_json TEXT NOT NULL,
                    after_kpi_json TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    lesson TEXT,
                    search_text TEXT,
                    embedding_json TEXT,
                    embedding_model TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS traffic_workloads (
                    workload_id TEXT PRIMARY KEY,
                    service_id TEXT NOT NULL,
                    traffic_role TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    desired_state TEXT NOT NULL,
                    recovery_policy TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    application_id TEXT,
                    specification_json TEXT NOT NULL,
                    process_refs_json TEXT NOT NULL,
                    statistics_json TEXT NOT NULL,
                    last_error TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    stopped_at TEXT
                );

                CREATE TABLE IF NOT EXISTS workload_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workload_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    previous_state TEXT,
                    current_state TEXT,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    correlation_json TEXT NOT NULL,
                    expected_json TEXT,
                    observed_json TEXT,
                    action_json TEXT,
                    result_json TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_idempotency_v1 (
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_json TEXT,
                    resource_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(operation, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS qos_resource_bindings (
                    binding_id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL,
                    service_id TEXT,
                    traffic_scope_key TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    device_id TEXT,
                    interface_id TEXT,
                    class_id TEXT NOT NULL,
                    filter_id INTEGER NOT NULL,
                    handle INTEGER NOT NULL,
                    dscp INTEGER,
                    rate_mbps REAL NOT NULL,
                    ceil_mbps REAL NOT NULL,
                    reservation_kind TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    runtime_json TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS resource_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL,
                    traffic_scope_key TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    reservation_kind TEXT NOT NULL,
                    bandwidth_mbps REAL NOT NULL,
                    active INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stable_dataplane_capacity_records (
                    capacity_record_id TEXT PRIMARY KEY,
                    bottleneck_egress TEXT NOT NULL,
                    topology_id TEXT NOT NULL,
                    environment_epoch TEXT NOT NULL,
                    controller_epoch TEXT NOT NULL,
                    path_signature TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    valid_until TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS desired_policy_states (
                    policy_id TEXT PRIMARY KEY,
                    traffic_scope_key TEXT NOT NULL,
                    desired_state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    expected_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS observed_policy_states (
                    policy_id TEXT PRIMARY KEY,
                    traffic_scope_key TEXT NOT NULL,
                    observed_state TEXT NOT NULL,
                    drift_type TEXT NOT NULL,
                    observed_json TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    last_repair_at TEXT,
                    repair_attempts INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS reconciliation_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telemetry_samples (
                    sample_id TEXT PRIMARY KEY,
                    sample_type TEXT NOT NULL,
                    resource_id TEXT,
                    service_id TEXT,
                    workload_id TEXT,
                    traffic_role TEXT,
                    validation_run_id TEXT,
                    payload_json TEXT NOT NULL,
                    quality_status TEXT NOT NULL,
                    sampled_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS interface_metrics (
                    sample_id TEXT PRIMARY KEY,
                    resource_id TEXT NOT NULL,
                    sampled_at TEXT NOT NULL,
                    interval_seconds REAL,
                    counter_before_json TEXT,
                    counter_after_json TEXT NOT NULL,
                    delta_json TEXT,
                    calculated_rate_mbps REAL,
                    utilization_pct REAL,
                    queue_json TEXT NOT NULL,
                    quality_status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS service_metrics (
                    sample_id TEXT PRIMARY KEY,
                    service_id TEXT NOT NULL,
                    traffic_role TEXT NOT NULL,
                    workload_id TEXT,
                    validation_run_id TEXT,
                    payload_json TEXT NOT NULL,
                    quality_status TEXT NOT NULL,
                    sampled_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workload_metrics (
                    sample_id TEXT PRIMARY KEY,
                    workload_id TEXT NOT NULL,
                    service_id TEXT NOT NULL,
                    traffic_role TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    quality_status TEXT NOT NULL,
                    sampled_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS faults (
                    fault_id TEXT PRIMARY KEY,
                    fault_type TEXT NOT NULL,
                    target_resource_id TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    desired_state TEXT NOT NULL,
                    reversion_policy TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    observed_json TEXT NOT NULL,
                    last_error TEXT,
                    applied_at TEXT,
                    removed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS infrastructure_state (
                    component TEXT PRIMARY KEY,
                    identity_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recovery_runs (
                    run_id TEXT PRIMARY KEY,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS soak_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    result_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_intents_session_created
                    ON intents(session_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_session_created
                    ON messages(session_id, created_at ASC);
                CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                    ON intent_jobs(status, created_at ASC);
                CREATE INDEX IF NOT EXISTS idx_events_intent_id
                    ON events(intent_id, event_id ASC);
                CREATE INDEX IF NOT EXISTS idx_watcher_incidents_created
                    ON watcher_incidents(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_watcher_samples_created
                    ON watcher_samples(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_experience_cases_created
                    ON experience_cases(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_experience_cases_root_cause
                    ON experience_cases(root_cause, success);
                CREATE INDEX IF NOT EXISTS idx_policies_intent
                    ON policies(intent_id);
                CREATE INDEX IF NOT EXISTS idx_policies_created
                    ON policies(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_policy_history_scope
                    ON policy_history(traffic_scope_key, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_workloads_state_updated
                    ON traffic_workloads(lifecycle_state, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_workloads_service_updated
                    ON traffic_workloads(service_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_workload_events_resource
                    ON workload_events(workload_id, event_id ASC);
                CREATE INDEX IF NOT EXISTS idx_audit_resource_created
                    ON audit_events(resource_type, resource_id, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_qos_binding_active_scope
                    ON qos_resource_bindings(traffic_scope_key) WHERE active = 1;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_qos_binding_active_class
                    ON qos_resource_bindings(class_id) WHERE active = 1;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_qos_binding_active_filter
                    ON qos_resource_bindings(filter_id) WHERE active = 1;
                CREATE INDEX IF NOT EXISTS idx_reservations_resource_active
                    ON resource_reservations(resource_id, active);
                CREATE INDEX IF NOT EXISTS idx_stable_capacity_lookup
                    ON stable_dataplane_capacity_records(bottleneck_egress, topology_id, status, valid_until);
                CREATE INDEX IF NOT EXISTS idx_observed_drift_checked
                    ON observed_policy_states(drift_type, checked_at DESC);
                CREATE INDEX IF NOT EXISTS idx_telemetry_type_sampled
                    ON telemetry_samples(sample_type, sampled_at DESC);
                CREATE INDEX IF NOT EXISTS idx_faults_state_updated
                    ON faults(lifecycle_state, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_interface_metrics_resource_sampled
                    ON interface_metrics(resource_id, sampled_at DESC);
                CREATE INDEX IF NOT EXISTS idx_service_metrics_service_sampled
                    ON service_metrics(service_id, sampled_at DESC);
                CREATE INDEX IF NOT EXISTS idx_workload_metrics_resource_sampled
                    ON workload_metrics(workload_id, sampled_at DESC);
                """
            )
            self._ensure_columns(
                connection,
                "experience_cases",
                {
                    "search_text": "TEXT",
                    "embedding_json": "TEXT",
                    "embedding_model": "TEXT",
                },
            )
            self._ensure_columns(
                connection,
                "policies",
                {
                    "traffic_scope_key": "TEXT",
                    "status": "TEXT NOT NULL DEFAULT 'ACTIVE'",
                    "version": "INTEGER NOT NULL DEFAULT 1",
                    "pending_yang_json": "TEXT",
                    "pending_intent_id": "TEXT",
                    "pending_operation": "TEXT",
                    "runtime_json": "TEXT",
                    "audit_json": "TEXT",
                    "error_message": "TEXT",
                },
            )
            self._ensure_columns(
                connection,
                "intent_jobs",
                {
                    "heartbeat_at": "TEXT",
                },
            )
            self._ensure_columns(
                connection,
                "intents",
                {
                    "checkpoint_at": "TEXT",
                },
            )

    def _ensure_columns(
        self,
        connection: sqlite3.Connection,
        table: str,
        columns: dict[str, str],
    ) -> None:
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )

    def create_session(
        self,
        user_id: str = "anonymous",
        title: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        session_id = session_id or f"SES-{uuid.uuid4().hex[:12]}"
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(session_id, user_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, user_id, title, now, now),
            )
        session = self.get_session(session_id)
        if session is None:  # pragma: no cover - committed insert invariant
            raise RuntimeError(f"session {session_id} was not persisted")
        return session

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(
        self, user_id: Optional[str] = None, page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        where = "WHERE user_id = ?" if user_id else ""
        params: list[Any] = [user_id] if user_id else []
        with self._lock, self._connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM sessions {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT * FROM sessions {where}
                ORDER BY updated_at DESC LIMIT ? OFFSET ?
                """,
                params + [page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def create(
        self,
        intent_id: str,
        text: str,
        priority: str = "medium",
        user_id: str = "anonymous",
        session_id: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if session_id is None:
            session_id = self.create_session(user_id, text[:40])["session_id"]
        elif self.get_session(session_id) is None:
            self.create_session(user_id, text[:40], session_id=session_id)

        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO intents(
                    intent_id, session_id, user_id, text, execution_text,
                    priority, status, context_json, snapshot_json,
                    milestones_json, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    session_id,
                    user_id,
                    text,
                    text,
                    priority,
                    "pending",
                    _json_dump(context) if context else None,
                    None,
                    "[]",
                    None,
                    now,
                    now,
                ),
            )
        self.add_message(session_id, "user", "intent", text, intent_id=intent_id)
        record = self.get(intent_id)
        if record is None:  # pragma: no cover - committed insert invariant
            raise RuntimeError(f"intent {intent_id} was not persisted")
        return record

    def _intent_record(self, row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["context"] = _json_load(record.pop("context_json"), {})
        record["snapshot"] = _json_load(record.pop("snapshot_json"))
        record["milestones"] = _json_load(record.pop("milestones_json"), [])
        record["result"] = _json_load(record.pop("result_json"))
        return record

    def get(self, intent_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return self._intent_record(row) if row else None

    def update(self, intent_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        allowed = {
            "status": "status",
            "execution_text": "execution_text",
            "context": "context_json",
            "snapshot": "snapshot_json",
            "milestones": "milestones_json",
            "result": "result_json",
            "checkpoint_at": "checkpoint_at",
        }
        updates: list[str] = []
        values: list[Any] = []
        for field, value in fields.items():
            column = allowed.get(field)
            if not column:
                continue
            updates.append(f"{column} = ?")
            values.append(_json_dump(value) if column.endswith("_json") else value)

        if not updates:
            return self.get(intent_id)

        now = _utc_now()
        updates.append("updated_at = ?")
        values.extend([now, intent_id])

        # Retry on SQLITE_BUSY (up to 3 attempts with back-off)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self._lock, self._connect() as connection:
                    connection.execute(
                        f"UPDATE intents SET {', '.join(updates)} WHERE intent_id = ?",
                        values,
                    )
                    connection.execute(
                        "UPDATE sessions SET updated_at = ? WHERE session_id = "
                        "(SELECT session_id FROM intents WHERE intent_id = ?)",
                        (now, intent_id),
                    )
                break
            except sqlite3.OperationalError as exc:
                if (
                    "database is locked" in str(exc).lower()
                    and attempt < max_retries - 1
                ):
                    import time

                    time.sleep(0.5 * (2**attempt))  # 0.5s, 1s, 2s
                    continue
                raise
        return self.get(intent_id)

    def update_with_checkpoint_verification(
        self,
        intent_id: str,
        checkpoint_timestamp: str,
        **fields: Any,
    ) -> Optional[dict[str, Any]]:
        """Update intent with a checkpoint timestamp for consistency tracking.

        The checkpoint_timestamp records when the LangGraph checkpoint was
        last written.  This allows the read path to detect and repair
        inconsistencies between the API store and the checkpoint.
        """
        return self.update(
            intent_id,
            checkpoint_at=checkpoint_timestamp,
            **fields,
        )

    def delete(self, intent_id: str) -> bool:
        """Delete an intent and all related records (jobs, events, messages)."""
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM intent_jobs WHERE intent_id = ?", (intent_id,)
            )
            connection.execute("DELETE FROM events WHERE intent_id = ?", (intent_id,))
            connection.execute("DELETE FROM messages WHERE intent_id = ?", (intent_id,))
            connection.execute(
                "UPDATE experience_cases SET intent_id = NULL WHERE intent_id = ?",
                (intent_id,),
            )
            cursor = connection.execute(
                "DELETE FROM intents WHERE intent_id = ?", (intent_id,)
            )
            return cursor.rowcount > 0

    def list(
        self,
        status: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("user_id", user_id),
            ("session_id", session_id),
        ):
            if value:
                conditions.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        with self._lock, self._connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM intents {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT * FROM intents {where}
                ORDER BY created_at DESC LIMIT ? OFFSET ?
                """,
                params + [page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "items": [self._intent_record(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def list_waiting_intents(self, *, user_id: str | None = None) -> list[dict[str, Any]]:
        """Return every non-terminal conversational waiting row for ownership repair."""
        where = "status IN (?, ?, ?)"
        params: list[Any] = [*_WAITING_STATUSES]
        if user_id:
            where = f"user_id = ? AND {where}"
            params.insert(0, user_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM intents WHERE {where} ORDER BY updated_at DESC, rowid DESC",
                params,
            ).fetchall()
        return [self._intent_record(row) for row in rows]

    @staticmethod
    def waiting_slots(record: dict[str, Any]) -> list[Any]:
        snapshot = record.get("snapshot") or {}
        details = snapshot.get("clarification_details") or []
        return [item.get("field") for item in details if isinstance(item, dict)] or snapshot.get("unresolved_fields") or []

    def waiting_logical_key(self, record: dict[str, Any]) -> str:
        """Derive the global logical owner key from a persisted waiting row."""
        return logical_waiting_key(
            user_id=str(record.get("user_id") or "anonymous"),
            text=str(record.get("text") or ""),
            unresolved_slots=self.waiting_slots(record),
            context=record.get("context") or {},
        )

    def find_equivalent_waiting_intent(
        self, *, user_id: str, text: str, context: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Optional[dict[str, Any]]:
        """Return the logical current waiting owner across all user sessions.

        ``session_id`` is accepted for backwards-compatible callers but is not
        part of equivalence.  It remains conversation provenance on the owner.
        """
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM intents WHERE user_id = ?
                   AND status IN (?, ?, ?) ORDER BY updated_at DESC, rowid DESC""",
                (user_id, *_WAITING_STATUSES),
            ).fetchall()
        incoming_key = logical_waiting_request_key(user_id=user_id, text=text, context=context)
        for row in rows:
            candidate = self._intent_record(row)
            fingerprint = self.waiting_logical_key(candidate)
            candidate["logical_waiting_key"] = fingerprint
            # Backwards-compatible field name for external diagnostics.  Its
            # value is now deliberately session-independent.
            candidate["waiting_intent_fingerprint"] = fingerprint
            candidate_request_key = logical_waiting_request_key(
                user_id=str(candidate.get("user_id") or "anonymous"),
                text=str(candidate.get("text") or ""), context=candidate.get("context") or {},
            )
            if candidate_request_key == incoming_key:
                return candidate
        return None

    def add_message(
        self,
        session_id: str,
        role: str,
        message_type: str,
        content: str,
        *,
        intent_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        message_id = f"MSG-{uuid.uuid4().hex[:12]}"
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages(
                    message_id, session_id, intent_id, role, message_type,
                    content, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    intent_id,
                    role,
                    message_type,
                    content,
                    _json_dump(payload) if payload else None,
                    now,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
        return {
            "message_id": message_id,
            "session_id": session_id,
            "intent_id": intent_id,
            "role": role,
            "message_type": message_type,
            "content": content,
            "payload": payload,
            "created_at": now,
        }

    def list_messages(self, session_id: str) -> List[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM messages WHERE session_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()
        messages: List[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _json_load(item.pop("payload_json"))
            messages.append(item)
        return messages

    def enqueue_job(self, intent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist an orchestration request for execution by the worker."""
        job_id = f"JOB-{uuid.uuid4().hex[:12]}"
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO intent_jobs(
                    job_id, intent_id, payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, intent_id, _json_dump(payload), "queued", now),
            )
        job = self.get_job(job_id)
        if job is None:  # pragma: no cover - committed insert invariant
            raise RuntimeError(f"job {job_id} was not persisted")
        return job

    def _job_record(self, row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["payload"] = _json_load(record.pop("payload_json"), {})
        return record

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM intent_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._job_record(row) if row else None

    def list_jobs(self, intent_id: str) -> List[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM intent_jobs WHERE intent_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (intent_id,),
            ).fetchall()
        return [self._job_record(row) for row in rows]

    def claim_next_job(self, worker_id: str) -> Optional[dict[str, Any]]:
        """Atomically reserve the oldest queued task, skipping stale jobs."""
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Auto-cancel queued jobs older than 24 hours
            connection.execute(
                """
                UPDATE intent_jobs SET status = 'cancelled'
                WHERE status = 'queued'
                  AND created_at < datetime('now', '-24 hours')
                """
            )
            row = connection.execute(
                """
                SELECT job_id FROM intent_jobs
                WHERE status = 'queued'
                ORDER BY created_at ASC, rowid ASC LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            job_id = row["job_id"]
            connection.execute(
                """
                UPDATE intent_jobs
                SET status = 'running', worker_id = ?, attempts = attempts + 1,
                    started_at = ?, heartbeat_at = ?, error_message = NULL
                WHERE job_id = ? AND status = 'queued'
                """,
                (worker_id, now, now, job_id),
            )
        return self.get_job(job_id)

    def update_job_heartbeat(self, job_id: str) -> None:
        """Refresh the heartbeat timestamp for a running job.

        Called periodically by the worker to signal it is still alive.
        If the heartbeat stops, recover_stale_jobs() will requeue the job.
        """
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE intent_jobs SET heartbeat_at = ? WHERE job_id = ? AND status = 'running'",
                (_utc_now(), job_id),
            )

    def complete_job(self, job_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE intent_jobs SET status = 'completed', heartbeat_at = NULL, finished_at = ? WHERE job_id = ?",
                (_utc_now(), job_id),
            )

    def cancel_queued_jobs(self, intent_id: str, *, reason: str) -> int:
        """Atomically prevent an unclaimed intent from reaching a worker."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE intent_jobs
                   SET status = 'cancelled', error_message = ?, heartbeat_at = NULL, finished_at = ?
                   WHERE intent_id = ? AND status = 'queued'""",
                (reason, _utc_now(), intent_id),
            )
        return cursor.rowcount

    def fail_job(self, job_id: str, error_message: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE intent_jobs
                SET status = 'failed', error_message = ?, heartbeat_at = NULL, finished_at = ?
                WHERE job_id = ?
                """,
                (error_message, _utc_now(), job_id),
            )

    def recover_running_jobs(self) -> int:
        """Requeue tasks left running when the single worker process stopped."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE intent_jobs
                SET status = 'queued', worker_id = NULL, started_at = NULL,
                    error_message = 'Recovered after worker restart'
                WHERE status = 'running'
                """
            )
        return cursor.rowcount

    def recover_stale_jobs(self, lease_timeout_seconds: int = 120) -> int:
        """Requeue running jobs whose heartbeat lease has expired.

        Uses ``heartbeat_at`` (updated every ~30 s by the worker) as the
        primary liveness signal.  Falls back to ``started_at`` when
        ``heartbeat_at`` has never been set (backward compat).

        Safe to call from the API process or a background timer; uses a
        separate transaction so it cannot conflict with the worker's
        claim_next_job.
        """
        import os

        timeout = int(
            os.getenv("IDN_JOB_LEASE_TIMEOUT_SECONDS", str(lease_timeout_seconds))
        )
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE intent_jobs
                SET status = 'queued',
                    worker_id = NULL,
                    started_at = NULL,
                    heartbeat_at = NULL,
                    error_message = 'Lease expired — worker may have crashed'
                WHERE status = 'running'
                  AND (
                      (heartbeat_at IS NOT NULL
                       AND heartbeat_at < datetime('now', ?))
                      OR
                      (heartbeat_at IS NULL
                       AND started_at IS NOT NULL
                       AND started_at < datetime('now', ?))
                  )
                """,
                (
                    f"-{timeout} seconds",
                    f"-{timeout} seconds",
                ),
            )
        count = cursor.rowcount
        if count:
            import logging

            logging.getLogger(__name__).warning(
                "Recovered %s stale job(s) (lease timeout=%ss)", count, timeout
            )
        return count

    def add_event(self, intent_id: str, event_type: str, data: dict[str, Any]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events(intent_id, event_type, data_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (intent_id, event_type, _json_dump(data), _utc_now()),
            )

    def list_events(self, intent_id: str, after_id: int = 0) -> List[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE intent_id = ? AND event_id > ?
                ORDER BY event_id ASC
                """,
                (intent_id, after_id),
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["data"] = _json_load(event.pop("data_json"), {})
            events.append(event)
        return events

    # -- Persistent production workloads ---------------------------------

    @staticmethod
    def _workload_record(row: sqlite3.Row) -> dict[str, Any]:
        record = _json_load(row["specification_json"], {}) or {}
        record.update(
            {
                "workload_id": row["workload_id"],
                "service_id": row["service_id"],
                "traffic_role": row["traffic_role"],
                "lifecycle_state": row["lifecycle_state"],
                "desired_state": row["desired_state"],
                "recovery_policy": row["recovery_policy"],
                "tenant_id": row["tenant_id"],
                "application_id": row["application_id"],
                "process_refs": _json_load(row["process_refs_json"], []),
                "statistics": _json_load(row["statistics_json"], {}),
                "last_error": row["last_error"],
                "version": row["version"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "started_at": row["started_at"],
                "stopped_at": row["stopped_at"],
            }
        )
        return record

    def create_workload(self, record: dict[str, Any]) -> dict[str, Any]:
        workload_id = str(record["workload_id"])
        now = str(record.get("updated_at") or _utc_now())
        specification = {
            key: value
            for key, value in record.items()
            if key
            not in {
                "lifecycle_state",
                "desired_state",
                "process_refs",
                "statistics",
                "last_error",
                "version",
                "created_at",
                "updated_at",
                "started_at",
                "stopped_at",
            }
        }
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO traffic_workloads(
                    workload_id, service_id, traffic_role, lifecycle_state,
                    desired_state, recovery_policy, tenant_id, application_id,
                    specification_json, process_refs_json, statistics_json,
                    last_error, version, created_at, updated_at, started_at, stopped_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workload_id,
                    record["service_id"],
                    record["traffic_role"],
                    record["lifecycle_state"],
                    record["desired_state"],
                    record["recovery_policy"],
                    record.get("tenant_id") or "default",
                    record.get("application_id"),
                    _json_dump(specification),
                    _json_dump(record.get("process_refs") or []),
                    _json_dump(record.get("statistics") or {}),
                    record.get("last_error"),
                    int(record.get("version") or 1),
                    str(record.get("created_at") or now),
                    now,
                    record.get("started_at"),
                    record.get("stopped_at"),
                ),
            )
        self.add_workload_event(
            workload_id,
            "WORKLOAD_CREATED",
            None,
            str(record["lifecycle_state"]),
            {
                "service_id": record["service_id"],
                "traffic_role": record["traffic_role"],
            },
        )
        created = self.get_workload(workload_id)
        assert created is not None
        return created

    def get_workload(self, workload_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM traffic_workloads WHERE workload_id = ?", (workload_id,)
            ).fetchone()
        return self._workload_record(row) if row else None

    def list_workloads(
        self,
        *,
        service_id: str | None = None,
        lifecycle_state: str | None = None,
        traffic_role: str | None = None,
        limit: int = 500,
    ) -> List[dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        for column, value in (
            ("service_id", service_id),
            ("lifecycle_state", lifecycle_state),
            ("traffic_role", traffic_role),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 5000)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM traffic_workloads{where} ORDER BY created_at ASC LIMIT ?",
                params,
            ).fetchall()
        return [self._workload_record(row) for row in rows]

    def update_workload(
        self,
        workload_id: str,
        *,
        expected_version: int | None = None,
        **fields: Any,
    ) -> Optional[dict[str, Any]]:
        current = self.get_workload(workload_id)
        if current is None:
            return None
        if expected_version is not None and int(current["version"]) != int(
            expected_version
        ):
            raise RuntimeError("WORKLOAD_VERSION_CONFLICT")
        previous_state = current["lifecycle_state"]
        event_data = fields.pop("event_data", {})
        merged = {**current, **fields}
        merged["updated_at"] = _utc_now()
        merged["version"] = int(current["version"]) + 1
        specification = {
            key: value
            for key, value in merged.items()
            if key
            not in {
                "lifecycle_state",
                "desired_state",
                "process_refs",
                "statistics",
                "last_error",
                "version",
                "created_at",
                "updated_at",
                "started_at",
                "stopped_at",
            }
        }
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE traffic_workloads SET
                    service_id = ?, traffic_role = ?, lifecycle_state = ?, desired_state = ?,
                    recovery_policy = ?, tenant_id = ?, application_id = ?,
                    specification_json = ?, process_refs_json = ?, statistics_json = ?,
                    last_error = ?, version = ?, updated_at = ?, started_at = ?, stopped_at = ?
                WHERE workload_id = ? AND version = ?
                """,
                (
                    merged["service_id"],
                    merged["traffic_role"],
                    merged["lifecycle_state"],
                    merged["desired_state"],
                    merged["recovery_policy"],
                    merged.get("tenant_id") or "default",
                    merged.get("application_id"),
                    _json_dump(specification),
                    _json_dump(merged.get("process_refs") or []),
                    _json_dump(merged.get("statistics") or {}),
                    merged.get("last_error"),
                    merged["version"],
                    merged["updated_at"],
                    merged.get("started_at"),
                    merged.get("stopped_at"),
                    workload_id,
                    current["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("WORKLOAD_VERSION_CONFLICT")
        if previous_state != merged["lifecycle_state"]:
            self.add_workload_event(
                workload_id,
                "WORKLOAD_STATE_CHANGED",
                previous_state,
                merged["lifecycle_state"],
                event_data,
            )
        return self.get_workload(workload_id)

    def delete_workload(self, workload_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM traffic_workloads WHERE workload_id = ?", (workload_id,)
            )
        return cursor.rowcount > 0

    def add_workload_event(
        self,
        workload_id: str,
        event_type: str,
        previous_state: str | None,
        current_state: str | None,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO workload_events(
                    workload_id, event_type, previous_state, current_state, data_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    workload_id,
                    event_type,
                    previous_state,
                    current_state,
                    _json_dump(data or {}),
                    _utc_now(),
                ),
            )

    def list_workload_events(self, workload_id: str) -> List[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workload_events WHERE workload_id = ? ORDER BY event_id ASC",
                (workload_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["data"] = _json_load(item.pop("data_json"), {})
            result.append(item)
        return result

    @staticmethod
    def _idempotency_hash(payload: Any) -> str:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def begin_idempotent(
        self, *, operation: str, key: str | None, payload: Any
    ) -> dict[str, Any] | None:
        """Reserve a key, or return the completed response for an exact replay."""
        if not key:
            return None
        from api.idempotency import IdempotencyConflictError

        key = key.strip()
        if not key:
            return None
        payload_hash = self._idempotency_hash(payload)
        now = _utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT payload_hash, status, response_json
                   FROM api_idempotency_v1 WHERE operation=? AND idempotency_key=?""",
                (operation, key),
            ).fetchone()
            if row is not None:
                if row["payload_hash"] != payload_hash:
                    raise IdempotencyConflictError(operation, key)
                if row["status"] == "COMPLETED" and row["response_json"]:
                    return json.loads(row["response_json"])
                raise IdempotencyConflictError(
                    operation, key, code="IDEMPOTENCY_REQUEST_IN_PROGRESS"
                )
            connection.execute(
                """INSERT INTO api_idempotency_v1(
                    operation, idempotency_key, payload_hash, status,
                    response_json, resource_id, created_at, updated_at
                ) VALUES (?, ?, ?, 'PENDING', NULL, NULL, ?, ?)""",
                (operation, key, payload_hash, now, now),
            )
        return None

    def complete_idempotent(
        self,
        *,
        operation: str,
        key: str | None,
        response: dict[str, Any],
        resource_id: str | None = None,
    ) -> None:
        if not key or not key.strip():
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE api_idempotency_v1
                   SET status='COMPLETED', response_json=?, resource_id=?, updated_at=?
                   WHERE operation=? AND idempotency_key=?""",
                (_json_dump(response), resource_id, _utc_now(), operation, key.strip()),
            )

    def abort_idempotent(self, *, operation: str, key: str | None) -> None:
        if not key or not key.strip():
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                """DELETE FROM api_idempotency_v1
                   WHERE operation=? AND idempotency_key=? AND status='PENDING'""",
                (operation, key.strip()),
            )

    def add_audit_event(
        self,
        *,
        event_type: str,
        resource_type: str,
        resource_id: str,
        actor: str = "system",
        correlation: dict[str, Any] | None = None,
        expected: Any = None,
        observed: Any = None,
        action: Any = None,
        result: Any = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO audit_events(
                    event_type, actor, resource_type, resource_id, correlation_json,
                    expected_json, observed_json, action_json, result_json, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_type,
                    actor,
                    resource_type,
                    resource_id,
                    _json_dump(correlation or {}),
                    _json_dump(expected) if expected is not None else None,
                    _json_dump(observed) if observed is not None else None,
                    _json_dump(action) if action is not None else None,
                    _json_dump(result) if result is not None else None,
                    reason,
                    now,
                ),
            )
            event_id = int(cursor.lastrowid or 0)
        return {
            "event_id": event_id,
            "event_type": event_type,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "created_at": now,
        }

    def list_audit_events(
        self,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        event_type: str | None = None,
        limit: int = 200,
    ) -> List[dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        for column, value in (
            ("resource_type", resource_type),
            ("resource_id", resource_id),
            ("event_type", event_type),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM audit_events{where} ORDER BY event_id DESC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for column in (
                "correlation_json",
                "expected_json",
                "observed_json",
                "action_json",
                "result_json",
            ):
                item[column.removesuffix("_json")] = _json_load(item.pop(column), None)
            result.append(item)
        return result

    def upsert_watcher_state(self, value: dict[str, Any]) -> dict[str, Any]:
        now = _utc_now()
        payload = {**value, "updated_at": now}
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO watcher_state(state_id, value_json, updated_at)
                VALUES ('singleton', ?, ?)
                ON CONFLICT(state_id) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (_json_dump(payload), now),
            )
        return payload

    def get_watcher_state(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value_json, updated_at FROM watcher_state WHERE state_id = 'singleton'"
            ).fetchone()
        if not row:
            return {
                "enabled": os.getenv("IDN_WATCHER_ENABLED", "true").lower() == "true",
                "state": "UNKNOWN",
                "last_sample_at": None,
                "consecutive_bad": 0,
                "active_incident_id": None,
                "last_triggered_intent_id": None,
                "cooldown_remaining_sec": 0,
                "last_reason": "watcher has not reported yet",
                "updated_at": None,
            }
        value = _json_load(row["value_json"], {}) or {}
        value.setdefault("updated_at", row["updated_at"])
        return value

    def record_watcher_sample(
        self,
        *,
        kpi_snapshot: dict[str, Any],
        verdict: dict[str, Any],
        incident_id: Optional[str] = None,
        sample_id: Optional[str] = None,
    ) -> dict[str, Any]:
        sample_id = sample_id or f"WS-{uuid.uuid4().hex[:12]}"
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO watcher_samples(
                    sample_id, incident_id, kpi_snapshot, verdict, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    sample_id,
                    incident_id,
                    _json_dump(kpi_snapshot),
                    _json_dump(verdict),
                    now,
                ),
            )
        return {
            "sample_id": sample_id,
            "incident_id": incident_id,
            "kpi_snapshot": kpi_snapshot,
            "verdict": verdict,
            "created_at": now,
        }

    def upsert_watcher_incident(
        self,
        *,
        incident_id: str,
        status: str,
        reason: Optional[str] = None,
        kpi_snapshot: Optional[dict[str, Any]] = None,
        triggered_intent_id: Optional[str] = None,
        resolved: bool = False,
    ) -> dict[str, Any]:
        now = _utc_now()
        existing = self.get_watcher_incident(incident_id)
        created_at = existing["created_at"] if existing else now
        resolved_at = now if resolved else (existing or {}).get("resolved_at")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO watcher_incidents(
                    incident_id, status, reason, kpi_snapshot,
                    triggered_intent_id, created_at, updated_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    status = excluded.status,
                    reason = excluded.reason,
                    kpi_snapshot = excluded.kpi_snapshot,
                    triggered_intent_id = COALESCE(
                        excluded.triggered_intent_id,
                        watcher_incidents.triggered_intent_id
                    ),
                    updated_at = excluded.updated_at,
                    resolved_at = excluded.resolved_at
                """,
                (
                    incident_id,
                    status,
                    reason,
                    _json_dump(kpi_snapshot or {}),
                    triggered_intent_id,
                    created_at,
                    now,
                    resolved_at,
                ),
            )
        return self.get_watcher_incident(incident_id) or {}

    def get_watcher_incident(self, incident_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM watcher_incidents WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
        return self._watcher_incident_record(row) if row else None

    def list_watcher_incidents(self, limit: int = 20) -> List[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM watcher_incidents
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._watcher_incident_record(row) for row in rows]

    def _watcher_incident_record(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["kpi_snapshot"] = _json_load(item.pop("kpi_snapshot"), {})
        return item

    def upsert_experience_case(self, case: dict[str, Any]) -> dict[str, Any]:
        case_id = case.get("case_id") or f"CASE-{uuid.uuid4().hex[:12].upper()}"
        now = _utc_now()
        existing = self.get_experience_case(case_id)
        created_at = (
            existing["created_at"] if existing else case.get("created_at") or now
        )
        payload = {
            "case_id": case_id,
            "intent_id": case.get("intent_id"),
            "intent_type": case.get("intent_type"),
            "symptom": case.get("symptom"),
            "evidence": case.get("evidence") or [],
            "root_cause": case.get("root_cause"),
            "repair_action": case.get("repair_action") or [],
            "before_kpi": case.get("before_kpi") or {},
            "after_kpi": case.get("after_kpi") or {},
            "success": bool(case.get("success")),
            "lesson": case.get("lesson"),
            "search_text": case.get("search_text"),
            "embedding": case.get("embedding") or [],
            "embedding_model": case.get("embedding_model"),
            "created_at": created_at,
            "updated_at": now,
        }
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO experience_cases(
                    case_id, intent_id, intent_type, symptom, evidence_json,
                    root_cause, repair_action_json, before_kpi_json, after_kpi_json,
                    success, lesson, search_text, embedding_json, embedding_model,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    intent_id = excluded.intent_id,
                    intent_type = excluded.intent_type,
                    symptom = excluded.symptom,
                    evidence_json = excluded.evidence_json,
                    root_cause = excluded.root_cause,
                    repair_action_json = excluded.repair_action_json,
                    before_kpi_json = excluded.before_kpi_json,
                    after_kpi_json = excluded.after_kpi_json,
                    success = excluded.success,
                    lesson = excluded.lesson,
                    search_text = excluded.search_text,
                    embedding_json = excluded.embedding_json,
                    embedding_model = excluded.embedding_model,
                    updated_at = excluded.updated_at
                """,
                (
                    payload["case_id"],
                    payload["intent_id"],
                    payload["intent_type"],
                    payload["symptom"],
                    _json_dump(payload["evidence"]),
                    payload["root_cause"],
                    _json_dump(payload["repair_action"]),
                    _json_dump(payload["before_kpi"]),
                    _json_dump(payload["after_kpi"]),
                    1 if payload["success"] else 0,
                    payload["lesson"],
                    payload["search_text"],
                    _json_dump(payload["embedding"]),
                    payload["embedding_model"],
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )
        return self.get_experience_case(case_id) or payload

    def get_experience_case(self, case_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM experience_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        return self._experience_case_record(row) if row else None

    def list_experience_cases(self, limit: int = 50) -> List[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM experience_cases
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._experience_case_record(row) for row in rows]

    def _experience_case_record(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["evidence"] = _json_load(item.pop("evidence_json"), [])
        item["repair_action"] = _json_load(item.pop("repair_action_json"), [])
        item["before_kpi"] = _json_load(item.pop("before_kpi_json"), {})
        item["after_kpi"] = _json_load(item.pop("after_kpi_json"), {})
        item["embedding"] = _json_load(item.pop("embedding_json", None), [])
        item["success"] = bool(item.get("success"))
        return item


_store: Optional[IntentStore] = None


def get_store() -> IntentStore:
    global _store
    if _store is None:
        _store = IntentStore()
    return _store


def get_current_store() -> Optional[IntentStore]:
    """Return the already-created API store without creating a new database file."""
    return _store


def reset_store(db_path: str | Path | None = None) -> IntentStore:
    """Create a fresh store singleton, mainly for application tests."""
    global _store
    _store = IntentStore(db_path)
    return _store

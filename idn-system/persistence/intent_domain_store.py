"""Durable storage for first-class intent control-plane state."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, TypeVar

from api.store import IntentStore, get_store
from models.intent_domain import (
    AgentDecision,
    HealingAttempt,
    HealingProposal,
    IntentAssuranceResult,
    IntentConflict,
    IntentExecutionResult,
    IntentLifecycle,
    LLMCallRecord,
    IntentPlanIR,
    IntentRevision,
    IntentRuntimeState,
    IntentSpec,
    IntentTimelineEvent,
    NetworkContext,
    RevisionStatus,
)
from models.intent_lifecycle_state_machine import IntentLifecycleStateMachine
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def event_id_seed(
    intent_id: str,
    previous: IntentLifecycle,
    lifecycle: IntentLifecycle,
) -> str:
    raw = f"{intent_id}:{previous.value}:{lifecycle.value}:{_now()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12].upper()


def _dump(value: BaseModel | dict[str, Any]) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(payload, ensure_ascii=False, default=str)


def _load(value: str | None) -> dict[str, Any]:
    return json.loads(value) if value else {}


class IntentDomainStore:
    """Append-oriented intent history with one mutable runtime row per intent."""

    def __init__(self, base_store: IntentStore | None = None):
        self.base_store = base_store or get_store()
        self._initialize()

    def _initialize(self) -> None:
        with self.base_store._lock, self.base_store._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS intent_specs_v1 (
                    intent_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    spec_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(intent_id, revision)
                );
                CREATE TABLE IF NOT EXISTS intent_runtime_v1 (
                    intent_id TEXT PRIMARY KEY,
                    lifecycle TEXT NOT NULL,
                    runtime_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_revisions_v1 (
                    intent_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    revision_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(intent_id, revision)
                );
                CREATE TABLE IF NOT EXISTS intent_contexts_v1 (
                    context_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_plans_v1 (
                    plan_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_executions_v1 (
                    execution_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    execution_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_assurance_v1 (
                    assurance_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    assurance_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_decisions_v1 (
                    decision_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_history_v1 (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    intent_id TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_evidence_v1 (
                    evidence_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_healing_v1 (
                    healing_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    healing_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_conflicts_v1 (
                    conflict_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    conflict_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_calls_v1 (
                    call_id TEXT PRIMARY KEY,
                    intent_id TEXT,
                    agent TEXT NOT NULL,
                    status TEXT NOT NULL,
                    call_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_intent_specs_latest
                    ON intent_specs_v1(intent_id, revision DESC);
                CREATE INDEX IF NOT EXISTS idx_intent_revisions_latest
                    ON intent_revisions_v1(intent_id, revision DESC);
                CREATE INDEX IF NOT EXISTS idx_intent_contexts_intent
                    ON intent_contexts_v1(intent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_intent_plans_intent
                    ON intent_plans_v1(intent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_intent_executions_intent
                    ON intent_executions_v1(intent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_intent_assurance_intent
                    ON intent_assurance_v1(intent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_intent_decisions_intent
                    ON intent_decisions_v1(intent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_intent_history_intent
                    ON intent_history_v1(intent_id, sequence ASC);
                CREATE INDEX IF NOT EXISTS idx_intent_evidence_intent
                    ON intent_evidence_v1(intent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_intent_healing_intent
                    ON intent_healing_v1(intent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_intent_conflicts_intent
                    ON intent_conflicts_v1(intent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_llm_calls_intent
                    ON llm_calls_v1(intent_id, created_at DESC);
                """
            )

    def save_spec(self, spec: IntentSpec) -> IntentSpec:
        with self.base_store._lock, self.base_store._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO intent_specs_v1
                   (intent_id, revision, spec_json, created_at) VALUES (?, ?, ?, ?)""",
                (
                    spec.intent_id,
                    spec.revision,
                    _dump(spec),
                    spec.updated_at.isoformat(),
                ),
            )
        return spec

    def get_spec(
        self, intent_id: str, revision: int | None = None
    ) -> IntentSpec | None:
        query = "SELECT spec_json FROM intent_specs_v1 WHERE intent_id=?"
        params: list[Any] = [intent_id]
        if revision is not None:
            query += " AND revision=?"
            params.append(revision)
        query += " ORDER BY revision DESC LIMIT 1"
        with self.base_store._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return IntentSpec.model_validate(_load(row["spec_json"])) if row else None

    def save_revision(self, revision: IntentRevision) -> IntentRevision:
        revision.updated_at = datetime.now(timezone.utc)
        with self.base_store._lock, self.base_store._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO intent_revisions_v1
                   (intent_id, revision, revision_json, status, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    revision.intent_id,
                    revision.revision,
                    _dump(revision),
                    revision.status.value,
                    revision.updated_at.isoformat(),
                ),
            )
        return revision

    def get_revision(self, intent_id: str, revision: int) -> IntentRevision | None:
        with self.base_store._connect() as connection:
            row = connection.execute(
                "SELECT revision_json FROM intent_revisions_v1 WHERE intent_id=? AND revision=?",
                (intent_id, revision),
            ).fetchone()
        return (
            IntentRevision.model_validate(_load(row["revision_json"])) if row else None
        )

    def revisions(self, intent_id: str) -> list[dict[str, Any]]:
        with self.base_store._connect() as connection:
            rows = connection.execute(
                "SELECT revision_json FROM intent_revisions_v1 WHERE intent_id=? ORDER BY revision DESC",
                (intent_id,),
            ).fetchall()
        return [_load(row["revision_json"]) for row in rows]

    def next_revision(self, intent_id: str) -> int:
        """Allocate monotonically after active, failed, and rolled-back attempts."""
        runtime = self.get_runtime(intent_id) or IntentRuntimeState(intent_id=intent_id)
        with self.base_store._connect() as connection:
            row = connection.execute(
                """SELECT MAX(revision) AS latest_revision FROM (
                     SELECT revision FROM intent_specs_v1 WHERE intent_id=?
                     UNION ALL
                     SELECT revision FROM intent_revisions_v1 WHERE intent_id=?
                   )""",
                (intent_id, intent_id),
            ).fetchone()
        latest = int(row["latest_revision"] or 0) if row else 0
        return max(runtime.current_revision, latest) + 1

    def stage_revision(
        self,
        spec: IntentSpec,
        *,
        actor: str,
        reason: str,
    ) -> IntentRevision:
        runtime = self.get_runtime(spec.intent_id) or IntentRuntimeState(
            intent_id=spec.intent_id
        )
        if runtime.pending_revision is not None:
            raise ValueError(f"revision {runtime.pending_revision} is already pending")
        expected = self.next_revision(spec.intent_id)
        if spec.revision != expected:
            raise ValueError(f"next revision must be {expected}, got {spec.revision}")
        revision = IntentRevision(
            intent_id=spec.intent_id,
            revision=spec.revision,
            previous_revision=runtime.current_revision,
            status=RevisionStatus.PENDING,
            spec_snapshot=spec.model_dump(mode="json"),
            actor=actor,
            reason=reason,
        )
        runtime.pending_revision = spec.revision
        runtime.updated_at = datetime.now(timezone.utc)
        with self.base_store._lock, self.base_store._connect() as connection:
            connection.execute(
                """INSERT INTO intent_specs_v1(intent_id, revision, spec_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    spec.intent_id,
                    spec.revision,
                    _dump(spec),
                    spec.updated_at.isoformat(),
                ),
            )
            connection.execute(
                """INSERT INTO intent_revisions_v1(intent_id, revision, revision_json, status, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    revision.intent_id,
                    revision.revision,
                    _dump(revision),
                    revision.status.value,
                    revision.updated_at.isoformat(),
                ),
            )
            connection.execute(
                """INSERT INTO intent_runtime_v1(intent_id, lifecycle, runtime_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(intent_id) DO UPDATE SET
                     lifecycle=excluded.lifecycle,
                     runtime_json=excluded.runtime_json,
                     updated_at=excluded.updated_at""",
                (
                    runtime.intent_id,
                    runtime.lifecycle.value,
                    _dump(runtime),
                    runtime.updated_at.isoformat(),
                ),
            )
        return revision

    def finish_revision(
        self,
        revision: IntentRevision,
        runtime: IntentRuntimeState,
        *,
        status: RevisionStatus,
        plan_id: str | None = None,
        execution_id: str | None = None,
        error: str | None = None,
    ) -> IntentRevision:
        revision.status = status
        revision.plan_id = plan_id or revision.plan_id
        revision.execution_id = execution_id or revision.execution_id
        revision.error = error
        revision.updated_at = datetime.now(timezone.utc)
        if status == RevisionStatus.ACTIVE:
            runtime.current_revision = revision.revision
            runtime.revision = revision.revision
        runtime.pending_revision = None
        runtime.updated_at = datetime.now(timezone.utc)
        with self.base_store._lock, self.base_store._connect() as connection:
            if (
                status == RevisionStatus.ACTIVE
                and revision.previous_revision is not None
            ):
                prior_row = connection.execute(
                    "SELECT revision_json FROM intent_revisions_v1 WHERE intent_id=? AND revision=?",
                    (revision.intent_id, revision.previous_revision),
                ).fetchone()
                if prior_row:
                    prior = IntentRevision.model_validate(
                        _load(prior_row["revision_json"])
                    )
                    prior.status = RevisionStatus.SUPERSEDED
                    prior.updated_at = revision.updated_at
                    connection.execute(
                        """UPDATE intent_revisions_v1 SET revision_json=?, status=?, updated_at=?
                           WHERE intent_id=? AND revision=?""",
                        (
                            _dump(prior),
                            prior.status.value,
                            prior.updated_at.isoformat(),
                            prior.intent_id,
                            prior.revision,
                        ),
                    )
            connection.execute(
                """UPDATE intent_revisions_v1 SET revision_json=?, status=?, updated_at=?
                   WHERE intent_id=? AND revision=?""",
                (
                    _dump(revision),
                    revision.status.value,
                    revision.updated_at.isoformat(),
                    revision.intent_id,
                    revision.revision,
                ),
            )
            connection.execute(
                """UPDATE intent_runtime_v1 SET lifecycle=?, runtime_json=?, updated_at=?
                   WHERE intent_id=?""",
                (
                    runtime.lifecycle.value,
                    _dump(runtime),
                    runtime.updated_at.isoformat(),
                    runtime.intent_id,
                ),
            )
        return revision

    def save_runtime(self, runtime: IntentRuntimeState) -> IntentRuntimeState:
        existing = self.get_runtime(runtime.intent_id)
        if existing is not None:
            IntentLifecycleStateMachine.validate(
                runtime.intent_id, existing.lifecycle, runtime.lifecycle
            )
            if existing.lifecycle != runtime.lifecycle:
                raise RuntimeError(
                    "lifecycle changes must use IntentDomainStore.transition()"
                )
        return self._write_runtime(runtime)

    def _write_runtime(self, runtime: IntentRuntimeState) -> IntentRuntimeState:
        runtime.updated_at = datetime.now(timezone.utc)
        with self.base_store._lock, self.base_store._connect() as connection:
            connection.execute(
                """INSERT INTO intent_runtime_v1(intent_id, lifecycle, runtime_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(intent_id) DO UPDATE SET
                     lifecycle=excluded.lifecycle,
                     runtime_json=excluded.runtime_json,
                     updated_at=excluded.updated_at""",
                (
                    runtime.intent_id,
                    runtime.lifecycle.value,
                    _dump(runtime),
                    runtime.updated_at.isoformat(),
                ),
            )
        return runtime

    def get_runtime(self, intent_id: str) -> IntentRuntimeState | None:
        with self.base_store._connect() as connection:
            row = connection.execute(
                "SELECT runtime_json FROM intent_runtime_v1 WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
        return (
            IntentRuntimeState.model_validate(_load(row["runtime_json"]))
            if row
            else None
        )

    def current_runtimes(self, *, limit: int = 200) -> list[IntentRuntimeState]:
        """Return the latest durable runtime projection for current-intent reads.

        This is deliberately a single bounded query.  Dashboard list reads must
        not fan out into controller, P4Runtime, TC, or assurance activity.
        """
        with self.base_store._connect() as connection:
            rows = connection.execute(
                "SELECT runtime_json FROM intent_runtime_v1 "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [IntentRuntimeState.model_validate(_load(row["runtime_json"])) for row in rows]

    def current_intent_records(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Read all persisted inputs for the dashboard projection in batches.

        This is intentionally one database connection and bounded table reads.
        It does not call a controller, an assurance worker, or an LLM.  Keeping
        the joins here prevents an N+1 query pattern when the dashboard grows.
        """
        with self.base_store._connect() as connection:
            runtime_rows = connection.execute(
                "SELECT runtime_json FROM intent_runtime_v1 ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            runtimes = [IntentRuntimeState.model_validate(_load(row["runtime_json"])) for row in runtime_rows]
            ids = [runtime.intent_id for runtime in runtimes]
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            def rows(table: str, column: str, order: str = "created_at DESC") -> dict[str, list[dict[str, Any]]]:
                result: dict[str, list[dict[str, Any]]] = {}
                for row in connection.execute(f"SELECT intent_id, {column} FROM {table} WHERE intent_id IN ({marks}) ORDER BY {order}", ids).fetchall():
                    result.setdefault(row["intent_id"], []).append(_load(row[column]))
                return result
            specs = rows("intent_specs_v1", "spec_json", "revision DESC")
            revisions = rows("intent_revisions_v1", "revision_json", "revision DESC")
            executions = rows("intent_executions_v1", "execution_json")
            assurances = rows("intent_assurance_v1", "assurance_json")
            evidences = rows("intent_evidence_v1", "evidence_json")
            aggregates: dict[str, dict[str, Any]] = {}
            for row in connection.execute(f"SELECT * FROM intents WHERE intent_id IN ({marks})", ids).fetchall():
                aggregates[row["intent_id"]] = self.base_store._intent_record(row)
            jobs: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in connection.execute(f"SELECT * FROM intent_jobs WHERE intent_id IN ({marks}) ORDER BY created_at DESC", ids).fetchall():
                jobs[row["intent_id"]].append(self.base_store._job_record(row))
        return [{
            "runtime": runtime,
            "spec": next((item for item in specs.get(runtime.intent_id, []) if item.get("revision") == runtime.current_revision), None) or (specs.get(runtime.intent_id) or [None])[0],
            "aggregate": aggregates.get(runtime.intent_id, {}),
            "execution": (executions.get(runtime.intent_id) or [{}])[0],
            "assurance": (assurances.get(runtime.intent_id) or [{}])[0],
            "evidence": (evidences.get(runtime.intent_id) or [{}])[0],
            "jobs": jobs.get(runtime.intent_id, []),
            "revisions": revisions.get(runtime.intent_id, []),
            "policies": [],
        } for runtime in runtimes]

    def transition(
        self,
        intent_id: str,
        lifecycle: IntentLifecycle,
        *,
        actor: str,
        reason: str,
        data: dict[str, Any] | None = None,
        plan_id: str | None = None,
        event_plan_id: str | None = None,
        context_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
    ) -> IntentRuntimeState:
        runtime = self.get_runtime(intent_id) or IntentRuntimeState(intent_id=intent_id)
        previous = runtime.lifecycle
        IntentLifecycleStateMachine.validate(intent_id, previous, lifecycle)
        runtime.lifecycle = lifecycle
        runtime.last_transition_reason = reason
        runtime.last_transition_at = datetime.now(timezone.utc)
        if lifecycle in {IntentLifecycle.FAILED, IntentLifecycle.EXECUTION_FAILED}:
            runtime.last_error = reason
        if lifecycle == IntentLifecycle.UNSATISFIABLE:
            runtime.failure_stage = "PLANNING"
            runtime.failure_reason = reason
            runtime.recoverable = True
        elif lifecycle == IntentLifecycle.EXECUTION_FAILED:
            runtime.failure_stage = "EXECUTION"
            runtime.failure_reason = reason
            runtime.recoverable = True
        elif lifecycle == IntentLifecycle.ASSURANCE_FAILED:
            runtime.failure_stage = "ASSURANCE"
            runtime.failure_reason = str((data or {}).get("failure_reason") or "SLA_VIOLATED")
            runtime.recoverable = bool((data or {}).get("recoverable", True))
            runtime.assurance_status = IntentLifecycle.ASSURANCE_FAILED.value
        elif lifecycle == IntentLifecycle.SATISFIED:
            runtime.failure_stage = None
            runtime.failure_reason = None
            runtime.recoverable = None
        if plan_id is not None:
            runtime.active_plan_id = plan_id
        if context_id is not None:
            runtime.context_id = context_id
        event = IntentTimelineEvent(
            intent_id=intent_id,
            intent_revision=runtime.pending_revision
            or runtime.current_revision
            or runtime.revision,
            lifecycle=lifecycle,
            event_type="LIFECYCLE_TRANSITION",
            actor=actor,
            reason=reason,
            correlation_id=correlation_id
            or f"CORR-{event_id_seed(intent_id, previous, lifecycle)}",
            trace_id=trace_id,
            # A proposed/redeploying plan belongs on the audit event, but it
            # must not become the active committed plan until execution has
            # passed readback and committed.  ``event_plan_id`` keeps those
            # two identities deliberately separate.
            plan_id=event_plan_id or plan_id or runtime.active_plan_id,
            context_id=context_id or runtime.context_id,
            data={
                "from": previous.value,
                "to": lifecycle.value,
                **(data or {}),
            },
        )
        runtime.updated_at = datetime.now(timezone.utc)
        # Runtime, append-only history and audit are one SQLite transaction.
        with self.base_store._lock, self.base_store._connect() as connection:
            connection.execute(
                """INSERT INTO intent_runtime_v1(intent_id, lifecycle, runtime_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(intent_id) DO UPDATE SET
                     lifecycle=excluded.lifecycle,
                     runtime_json=excluded.runtime_json,
                     updated_at=excluded.updated_at""",
                (
                    runtime.intent_id,
                    runtime.lifecycle.value,
                    _dump(runtime),
                    runtime.updated_at.isoformat(),
                ),
            )
            connection.execute(
                """INSERT INTO intent_history_v1
                   (event_id, intent_id, lifecycle, event_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.intent_id,
                    event.lifecycle.value,
                    _dump(event),
                    event.created_at.isoformat(),
                ),
            )
            correlation = {
                "intent_id": intent_id,
                "revision": event.intent_revision,
                "plan_id": event.plan_id,
                "context_id": event.context_id,
                "correlation_id": event.correlation_id,
                "trace_id": event.trace_id,
            }
            connection.execute(
                """INSERT INTO audit_events(
                    event_type, actor, resource_type, resource_id, correlation_json,
                    expected_json, observed_json, action_json, result_json, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "INTENT_LIFECYCLE_TRANSITION",
                    actor,
                    "intent",
                    intent_id,
                    _dump(correlation),
                    _dump({"lifecycle": previous.value}),
                    _dump({"lifecycle": lifecycle.value}),
                    _dump({"transition": f"{previous.value}->{lifecycle.value}"}),
                    _dump({"status": "COMMITTED", "event_id": event.event_id}),
                    reason,
                    event.created_at.isoformat(),
                ),
            )
        return runtime

    def record_event(
        self,
        intent_id: str,
        event_type: str,
        *,
        actor: str,
        reason: str,
        data: dict[str, Any] | None = None,
        plan_id: str | None = None,
        context_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
    ) -> IntentTimelineEvent:
        """Append a named audit event without changing lifecycle state."""
        runtime = self.get_runtime(intent_id) or IntentRuntimeState(intent_id=intent_id)
        event = IntentTimelineEvent(
            intent_id=intent_id,
            intent_revision=runtime.pending_revision
            or runtime.current_revision
            or runtime.revision,
            lifecycle=runtime.lifecycle,
            event_type=event_type,
            actor=actor,
            reason=reason,
            correlation_id=correlation_id
            or "CORR-"
            + hashlib.sha256(
                f"{intent_id}:{event_type}:{runtime.lifecycle.value}:{_now()}".encode("utf-8")
            ).hexdigest()[:12].upper(),
            trace_id=trace_id,
            plan_id=plan_id or runtime.active_plan_id,
            context_id=context_id or runtime.context_id,
            data=dict(data or {}),
        )
        correlation = {
            "intent_id": intent_id,
            "revision": event.intent_revision,
            "plan_id": event.plan_id,
            "context_id": event.context_id,
            "correlation_id": event.correlation_id,
            "trace_id": event.trace_id,
            **{
                key: value
                for key, value in event.data.items()
                if key.endswith("_id") or key.endswith("_ids")
            },
        }
        with self.base_store._lock, self.base_store._connect() as connection:
            connection.execute(
                """INSERT INTO intent_history_v1
                   (event_id, intent_id, lifecycle, event_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.intent_id,
                    event.lifecycle.value,
                    _dump(event),
                    event.created_at.isoformat(),
                ),
            )
            connection.execute(
                """INSERT INTO audit_events(
                    event_type, actor, resource_type, resource_id, correlation_json,
                    expected_json, observed_json, action_json, result_json, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_type,
                    actor,
                    "intent",
                    intent_id,
                    _dump(correlation),
                    _dump({}),
                    _dump({"lifecycle": runtime.lifecycle.value}),
                    _dump({"event": event_type, "data": event.data}),
                    _dump({"status": "RECORDED", "event_id": event.event_id}),
                    reason,
                    event.created_at.isoformat(),
                ),
            )
        return event

    def save_context(self, intent_id: str, context: NetworkContext) -> NetworkContext:
        self._save(
            "intent_contexts_v1",
            "context_id",
            context.context_id,
            intent_id,
            "context_json",
            context,
        )
        return context

    def save_plan(self, plan: IntentPlanIR) -> IntentPlanIR:
        self._save(
            "intent_plans_v1",
            "plan_id",
            plan.plan_id,
            plan.intent_id,
            "plan_json",
            plan,
        )
        return plan

    def save_execution(self, result: IntentExecutionResult) -> IntentExecutionResult:
        self._save(
            "intent_executions_v1",
            "execution_id",
            result.execution_id,
            result.intent_id,
            "execution_json",
            result,
        )
        return result

    def save_assurance(self, result: IntentAssuranceResult) -> IntentAssuranceResult:
        self._save(
            "intent_assurance_v1",
            "assurance_id",
            result.assurance_id,
            result.intent_id,
            "assurance_json",
            result,
        )
        return result

    def save_decision(self, decision: AgentDecision) -> AgentDecision:
        self._save(
            "intent_decisions_v1",
            "decision_id",
            decision.decision_id,
            decision.intent_id,
            "decision_json",
            decision,
        )
        return decision

    def save_evidence(self, intent_id: str, evidence: BaseModel) -> None:
        evidence_id = str(getattr(evidence, "evidence_id"))
        self._save(
            "intent_evidence_v1",
            "evidence_id",
            evidence_id,
            intent_id,
            "evidence_json",
            evidence,
        )

    def save_healing(
        self, healing: HealingProposal | HealingAttempt
    ) -> HealingProposal | HealingAttempt:
        self._save(
            "intent_healing_v1",
            "healing_id",
            healing.healing_id,
            healing.intent_id,
            "healing_json",
            healing,
        )
        return healing

    def save_conflict(self, conflict: IntentConflict) -> IntentConflict:
        self._save(
            "intent_conflicts_v1",
            "conflict_id",
            conflict.conflict_id,
            conflict.intent_id,
            "conflict_json",
            conflict,
        )
        return conflict

    def save_llm_call(self, record: LLMCallRecord) -> LLMCallRecord:
        """Persist only the secret-free LLM call record contract."""

        with self.base_store._lock, self.base_store._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO llm_calls_v1
                   (call_id, intent_id, agent, status, call_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    record.call_id,
                    record.intent_id,
                    record.agent,
                    record.status,
                    _dump(record),
                    record.started_at.isoformat(),
                ),
            )
        return record

    def llm_calls(
        self, intent_id: str | None = None, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        query = "SELECT call_json FROM llm_calls_v1"
        params: list[Any] = []
        if intent_id is not None:
            query += " WHERE intent_id=?"
            params.append(intent_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.base_store._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_load(row["call_json"]) for row in rows]

    def add_history(self, event: IntentTimelineEvent) -> IntentTimelineEvent:
        with self.base_store._lock, self.base_store._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO intent_history_v1
                   (event_id, intent_id, lifecycle, event_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.intent_id,
                    event.lifecycle.value,
                    _dump(event),
                    event.created_at.isoformat(),
                ),
            )
        return event

    def _save(
        self,
        table: str,
        id_column: str,
        item_id: str,
        intent_id: str,
        json_column: str,
        value: BaseModel,
    ) -> None:
        allowed = {
            "intent_contexts_v1",
            "intent_plans_v1",
            "intent_assurance_v1",
            "intent_executions_v1",
            "intent_decisions_v1",
            "intent_evidence_v1",
            "intent_healing_v1",
            "intent_conflicts_v1",
        }
        if table not in allowed:
            raise ValueError(f"unsupported intent-domain table: {table}")
        with self.base_store._lock, self.base_store._connect() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO {table}({id_column}, intent_id, {json_column}, created_at) VALUES (?, ?, ?, ?)",
                (item_id, intent_id, _dump(value), _now()),
            )

    def _list(
        self, table: str, json_column: str, intent_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        allowed = {
            "intent_contexts_v1",
            "intent_plans_v1",
            "intent_assurance_v1",
            "intent_executions_v1",
            "intent_decisions_v1",
            "intent_history_v1",
            "intent_evidence_v1",
            "intent_healing_v1",
            "intent_conflicts_v1",
        }
        if table not in allowed:
            raise ValueError(f"unsupported intent-domain table: {table}")
        order = "sequence ASC" if table == "intent_history_v1" else "created_at DESC"
        with self.base_store._connect() as connection:
            rows = connection.execute(
                f"SELECT {json_column} FROM {table} WHERE intent_id=? ORDER BY {order} LIMIT ?",
                (intent_id, limit),
            ).fetchall()
        return [_load(row[json_column]) for row in rows]

    def contexts(self, intent_id: str) -> list[dict[str, Any]]:
        return self._list("intent_contexts_v1", "context_json", intent_id)

    def plans(self, intent_id: str) -> list[dict[str, Any]]:
        return self._list("intent_plans_v1", "plan_json", intent_id)

    def executions(self, intent_id: str) -> list[dict[str, Any]]:
        return self._list("intent_executions_v1", "execution_json", intent_id)

    def assurance(self, intent_id: str) -> list[dict[str, Any]]:
        return self._list("intent_assurance_v1", "assurance_json", intent_id)

    def decisions(self, intent_id: str) -> list[dict[str, Any]]:
        return self._list("intent_decisions_v1", "decision_json", intent_id)

    def history(self, intent_id: str) -> list[dict[str, Any]]:
        return self._list("intent_history_v1", "event_json", intent_id)

    def evidence(self, intent_id: str) -> list[dict[str, Any]]:
        return self._list("intent_evidence_v1", "evidence_json", intent_id)

    def healing(self, intent_id: str) -> list[dict[str, Any]]:
        return self._list("intent_healing_v1", "healing_json", intent_id)

    def conflicts(self, intent_id: str) -> list[dict[str, Any]]:
        return self._list("intent_conflicts_v1", "conflict_json", intent_id)

    def delete_intent(self, intent_id: str) -> None:
        tables = [
            "intent_specs_v1",
            "intent_revisions_v1",
            "intent_runtime_v1",
            "intent_contexts_v1",
            "intent_plans_v1",
            "intent_executions_v1",
            "intent_assurance_v1",
            "intent_decisions_v1",
            "intent_history_v1",
            "intent_evidence_v1",
            "intent_healing_v1",
            "intent_conflicts_v1",
            "llm_calls_v1",
        ]
        with self.base_store._lock, self.base_store._connect() as connection:
            for table in tables:
                connection.execute(
                    f"DELETE FROM {table} WHERE intent_id=?", (intent_id,)
                )


_domain_store: IntentDomainStore | None = None
_domain_store_lock = threading.Lock()


def get_intent_domain_store() -> IntentDomainStore:
    global _domain_store
    current = get_store()
    if _domain_store is None or _domain_store.base_store is not current:
        with _domain_store_lock:
            if _domain_store is None or _domain_store.base_store is not current:
                _domain_store = IntentDomainStore(current)
    return _domain_store

"""
infra/trace.py
TraceBus — agent-to-agent runtime tracing.

Migrated from ``api/a2a_trace.py``.  All ``record_*`` functions,
``build_runtime_evidence``, and ``normalize_message`` are now
classmethods on ``TraceBus``.  The old module-level functions
remain available via thin re-exports in ``api/a2a_trace.py``.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

PROTOCOL_VERSION = "a2a.runtime.v1"

_MESSAGE_METHODS = {
    "Task_Delegation": "a2a.task.delegate",
    "Task_Response": "a2a.task.response",
    "Consensus_Sync": "a2a.consensus.sync",
    "ReAct_Trigger": "a2a.react.trigger",
    "Heartbeat": "a2a.heartbeat",
}

_MESSAGE_SCHEMAS = {
    "Task_Delegation": "A2A.TaskDelegation.v1",
    "Task_Response": "A2A.TaskResponse.v1",
    "Consensus_Sync": "A2A.ConsensusSync.v1",
    "ReAct_Trigger": "A2A.ReActTrigger.v1",
    "Heartbeat": "A2A.Heartbeat.v1",
}


# ── Internal helpers ─────────────────────────────────────────────────────


def _enabled() -> bool:
    return os.getenv("IDN_A2A_TRACE_ENABLED", "true").lower() == "true"


def _trim(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else f"{text[: limit - 1]}..."


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _list_strings(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)]


def _round_confidence(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return round(float(value), 3)


def _number_or_none(value: Any) -> float | int | None:
    if not isinstance(value, (int, float)):
        return None
    return value


def _target_agents(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _agent_values(message: dict[str, Any]) -> list[str]:
    return [
        str(item)
        for item in [
            message.get("source_agent"),
            *(_target_agents(message.get("target_agent"))),
            *(message.get("target_agents") or []),
        ]
        if item
    ]


def _handoff_edges(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for message in messages:
        source = message.get("source_agent")
        if not source:
            continue
        targets = message.get("target_agents") or _target_agents(
            message.get("target_agent")
        )
        for target in targets:
            counts[(source, target)] = counts.get((source, target), 0) + 1
    return [
        {"source_agent": source, "target_agent": target, "message_count": count}
        for (source, target), count in sorted(counts.items())
    ]


def _state_value(state: Any, key: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _stage_value(stage: Any) -> str:
    if isinstance(stage, Enum):
        return str(stage.value)
    return str(stage)


def _status_value(status: Any) -> str:
    if isinstance(status, Enum):
        return str(status.value)
    return str(status)


def _task_id(state: Any, agent_id: str, stage: Any) -> str:
    return f"TASK-{_state_value(state, 'intent_id', 'unknown')}-{_stage_value(stage)}-{agent_id}"


def _push(state: Any, payload: dict[str, Any]) -> None:
    intent_id = _state_value(state, "intent_id")
    if not intent_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    message_type = str(payload.get("message_type") or "")
    message = {
        "id": f"A2A-{uuid.uuid4().hex[:12]}",
        "protocol_version": PROTOCOL_VERSION,
        "trace_id": f"TRACE-{intent_id}",
        "intent_id": intent_id,
        "intent_revision": _state_value(state, "revision"),
        "plan_id": _state_value(state, "plan_id"),
        "context_id": _state_value(state, "context_id"),
        "timestamp": now,
        **payload,
    }
    message["method"] = (
        message.get("method") or _MESSAGE_METHODS.get(message_type) or "a2a.message"
    )
    message["payload_schema"] = message.get("payload_schema") or _MESSAGE_SCHEMAS.get(
        message_type
    )
    message["correlation_id"] = message.get("correlation_id") or message.get("task_id")
    message["evidence_refs"] = _list_strings(message.get("evidence_refs"))
    message["payload"] = _jsonable(message.get("payload") or {})
    message["metadata"] = _jsonable(message.get("metadata") or {})
    message["params"] = {
        "task_id": message.get("task_id"),
        "intent_id": intent_id,
        "intent_revision": message.get("intent_revision"),
        "plan_id": message.get("plan_id"),
        "context_id": message.get("context_id"),
        "correlation_id": message.get("correlation_id"),
        "trace_id": message.get("trace_id"),
        "source_agent": message.get("source_agent"),
        "target_agent": message.get("target_agent"),
        "target_agents": message.get("target_agents"),
        "stage": message.get("stage"),
        "status": message.get("status"),
        "payload_schema": message.get("payload_schema"),
    }
    from api.a2a_transport import get_a2a_transport

    transport = get_a2a_transport()
    message["transport"] = {
        "selected": transport.name,
        "event_type": "a2a_message",
    }
    result = transport.publish(intent_id, "a2a_message", message)
    message["transport"].update(result.to_dict())


# ── TraceBus ──────────────────────────────────────────────────────────────


class TraceBus:
    """Agent-to-agent runtime tracing bus.

    All methods are static — TraceBus has no mutable state.
    """

    # ── Record methods ────────────────────────────────────────────────

    @staticmethod
    def record_delegation(
        state: Any,
        *,
        source_agent: str,
        target_agent: str,
        stage: Any,
        input_summary: str,
        task_id: str | None = None,
        payload_schema: str | None = None,
        payload: dict[str, Any] | None = None,
        evidence_refs: list[str] | None = None,
        confidence: Any = None,
        risk_score: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        if not _enabled():
            return None
        task_id = task_id or _task_id(state, target_agent, stage)
        _push(
            state,
            {
                "jsonrpc": "2.0",
                "method": "a2a.task.delegate",
                "message_type": "Task_Delegation",
                "task_id": task_id,
                "source_agent": source_agent,
                "target_agent": target_agent,
                "stage": _stage_value(stage),
                "status": "running",
                "input_summary": _trim(input_summary),
                "output_summary": None,
                "confidence": _round_confidence(confidence),
                "risk_score": _number_or_none(risk_score),
                "evidence_refs": _list_strings(evidence_refs),
                "payload_schema": payload_schema,
                "payload": payload or {},
                "metadata": metadata or {},
            },
        )
        return task_id

    @staticmethod
    def record_response(
        state: Any,
        *,
        source_agent: str,
        target_agent: str,
        stage: Any,
        status: Any,
        output_summary: str,
        task_id: str | None = None,
        duration_ms: float | None = None,
        failure_reason: str | None = None,
        retry_suggestion: str | None = None,
        payload_schema: str | None = None,
        payload: dict[str, Any] | None = None,
        evidence_refs: list[str] | None = None,
        confidence: Any = None,
        risk_score: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        if not _enabled():
            return None
        task_id = task_id or _task_id(state, source_agent, stage)
        resolved_status = _status_value(status)
        _push(
            state,
            {
                "jsonrpc": "2.0",
                "method": "a2a.task.response",
                "message_type": "Task_Response",
                "task_id": task_id,
                "source_agent": source_agent,
                "target_agent": target_agent,
                "stage": _stage_value(stage),
                "status": resolved_status,
                "input_summary": None,
                "output_summary": _trim(output_summary),
                "duration_ms": duration_ms,
                "failure_reason": _trim(failure_reason) if failure_reason else None,
                "retry_suggestion": (
                    _trim(retry_suggestion) if retry_suggestion else None
                ),
                "confidence": _round_confidence(confidence),
                "risk_score": _number_or_none(risk_score),
                "evidence_refs": _list_strings(evidence_refs),
                "payload_schema": payload_schema,
                "payload": payload or {},
                "metadata": metadata or {},
            },
        )
        return task_id

    @staticmethod
    def record_react_trigger(
        state: Any,
        *,
        source_agent: str,
        target_agent: str,
        stage: Any,
        failure_reason: str,
        observation_data: dict[str, Any] | None = None,
        retry_suggestion: str | None = None,
        evidence_refs: list[str] | None = None,
        task_id: str | None = None,
    ) -> str | None:
        if not _enabled():
            return None
        task_id = task_id or _task_id(state, f"{source_agent}-react", stage)
        _push(
            state,
            {
                "jsonrpc": "2.0",
                "method": "a2a.react.trigger",
                "message_type": "ReAct_Trigger",
                "task_id": task_id,
                "source_agent": source_agent,
                "target_agent": target_agent,
                "stage": _stage_value(stage),
                "status": "running",
                "input_summary": _trim(failure_reason),
                "output_summary": _trim(
                    retry_suggestion or "触发上游智能体重新规划或执行自愈。"
                ),
                "failure_reason": _trim(failure_reason),
                "retry_suggestion": (
                    _trim(retry_suggestion) if retry_suggestion else None
                ),
                "evidence_refs": _list_strings(evidence_refs),
                "payload_schema": "A2A.ReActObservation.v1",
                "payload": observation_data or {},
                "observation_data": observation_data or {},
            },
        )
        return task_id

    @staticmethod
    def record_consensus_sync(
        state: Any,
        *,
        source_agent: str,
        target_agents: list[str] | None = None,
        stage: Any,
        decision: str,
        consensus: str = "accepted",
        confidence: Any = None,
        risk_score: Any = None,
        evidence_refs: list[str] | None = None,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> str | None:
        if not _enabled():
            return None
        targets = target_agents or ["orchestrator"]
        task_id = task_id or _task_id(state, f"{source_agent}-consensus", stage)
        decision_payload = payload or {}
        _push(
            state,
            {
                "jsonrpc": "2.0",
                "method": "a2a.consensus.sync",
                "message_type": "Consensus_Sync",
                "task_id": task_id,
                "source_agent": source_agent,
                "target_agent": ",".join(targets),
                "target_agents": targets,
                "stage": _stage_value(stage),
                "status": "success",
                "input_summary": _trim(decision),
                "output_summary": _trim(f"consensus={consensus}; decision={decision}"),
                "decision": _trim(decision),
                "consensus": consensus,
                "decision_code": decision_payload.get("decision_code"),
                "reason_code": decision_payload.get("reason_code"),
                "reason_detail": decision_payload.get("reason_detail") or {},
                "blocking": decision_payload.get("blocking", False),
                "blocking_stage": decision_payload.get("blocking_stage"),
                "blocking_agent": decision_payload.get("blocking_agent"),
                "repair_action": decision_payload.get("repair_actions") or [],
                "retry_allowed": decision_payload.get("retry_allowed", False),
                "original_policy_preserved": decision_payload.get(
                    "original_policy_preserved", True
                ),
                "confidence": _round_confidence(confidence),
                "risk_score": _number_or_none(risk_score),
                "evidence_refs": _list_strings(evidence_refs),
                "payload_schema": "A2A.ConsensusDecision.v1",
                "payload": decision_payload,
            },
        )
        return task_id

    @staticmethod
    def record_heartbeat(
        state: Any,
        *,
        agent_id: str,
        status: str,
        summary: str,
        stage: Any = "runtime",
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        if not _enabled():
            return None
        task_id = _task_id(state, f"{agent_id}-heartbeat", stage)
        _push(
            state,
            {
                "jsonrpc": "2.0",
                "method": "a2a.heartbeat",
                "message_type": "Heartbeat",
                "task_id": task_id,
                "source_agent": agent_id,
                "target_agent": "orchestrator",
                "stage": _stage_value(stage),
                "status": status,
                "input_summary": _trim(summary),
                "output_summary": _trim(summary),
                "payload_schema": "A2A.Heartbeat.v1",
                "payload": metadata or {},
                "metadata": metadata or {},
            },
        )
        return task_id

    # ── Evidence builder ──────────────────────────────────────────────

    @staticmethod
    def build_runtime_evidence(
        intent_id: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for index, event in enumerate(events, start=1):
            if event.get("event_type") and event.get("event_type") != "a2a_message":
                continue
            data = event.get("data") if "data" in event else event
            if not isinstance(data, dict):
                continue
            message = TraceBus.normalize_message(data, sequence=len(messages) + 1)
            if event.get("event_id") is not None:
                message["event_id"] = event["event_id"]
                message["sequence"] = event["event_id"]
            else:
                message["sequence"] = index
            messages.append(message)
        agents = sorted(
            {value for message in messages for value in _agent_values(message) if value}
        )
        evidence_sources = sorted(
            {
                ref
                for message in messages
                for ref in message.get("evidence_refs") or []
                if ref
            }
        )
        return {
            "intent_id": intent_id,
            "trace_id": f"TRACE-{intent_id}",
            "protocol_version": PROTOCOL_VERSION,
            "runtime_trace_ready": bool(messages),
            "message_count": len(messages),
            "agents": agents,
            "message_types": sorted(
                {
                    message.get("message_type")
                    for message in messages
                    if message.get("message_type")
                }
            ),
            "handoff_edges": _handoff_edges(messages),
            "evidence_sources": evidence_sources,
            "messages": messages,
        }

    @staticmethod
    def normalize_message(data: dict[str, Any], sequence: int = 0) -> dict[str, Any]:
        message_type = data.get("message_type") or "Task_Response"
        method = (
            data.get("method") or _MESSAGE_METHODS.get(message_type) or "a2a.message"
        )
        payload = data.get("payload") or data.get("observation_data") or {}
        return {
            "id": data.get("id") or f"A2A-{sequence:02d}",
            "sequence": sequence,
            "protocol_version": data.get("protocol_version") or PROTOCOL_VERSION,
            "trace_id": data.get("trace_id")
            or f"TRACE-{data.get('intent_id', 'unknown')}",
            "intent_id": data.get("intent_id"),
            "intent_revision": data.get("intent_revision"),
            "plan_id": data.get("plan_id"),
            "context_id": data.get("context_id"),
            "jsonrpc": data.get("jsonrpc") or "2.0",
            "method": method,
            "message_type": message_type,
            "task_id": data.get("task_id"),
            "correlation_id": data.get("correlation_id") or data.get("task_id"),
            "source_agent": data.get("source_agent"),
            "target_agent": data.get("target_agent"),
            "target_agents": data.get("target_agents")
            or _target_agents(data.get("target_agent")),
            "stage": data.get("stage"),
            "status": data.get("status"),
            "timestamp": data.get("timestamp"),
            "duration_ms": data.get("duration_ms"),
            "input_summary": _trim(data.get("input_summary")),
            "output_summary": _trim(data.get("output_summary")),
            "decision": data.get("decision"),
            "consensus": data.get("consensus"),
            "decision_code": data.get("decision_code") or payload.get("decision_code"),
            "reason_code": data.get("reason_code") or payload.get("reason_code"),
            "reason_detail": _jsonable(
                data.get("reason_detail") or payload.get("reason_detail") or {}
            ),
            "blocking": bool(data.get("blocking", payload.get("blocking", False))),
            "blocking_stage": data.get("blocking_stage")
            or payload.get("blocking_stage"),
            "blocking_agent": data.get("blocking_agent")
            or payload.get("blocking_agent"),
            "repair_action": _jsonable(
                data.get("repair_action") or payload.get("repair_actions") or []
            ),
            "retry_allowed": bool(
                data.get("retry_allowed", payload.get("retry_allowed", False))
            ),
            "original_policy_preserved": bool(
                data.get(
                    "original_policy_preserved",
                    payload.get("original_policy_preserved", True),
                )
            ),
            "confidence": _round_confidence(data.get("confidence")),
            "risk_score": _number_or_none(data.get("risk_score")),
            "evidence_refs": _list_strings(data.get("evidence_refs")),
            "failure_reason": (
                _trim(data.get("failure_reason"))
                if data.get("failure_reason")
                else None
            ),
            "retry_suggestion": (
                _trim(data.get("retry_suggestion"))
                if data.get("retry_suggestion")
                else None
            ),
            "payload_schema": data.get("payload_schema")
            or _MESSAGE_SCHEMAS.get(message_type),
            "payload": _jsonable(payload),
            "metadata": _jsonable(data.get("metadata") or {}),
            "params": _jsonable(data.get("params") or {}),
            "transport": _jsonable(data.get("transport") or {}),
        }


# ── Module-level re-exports (backward compatibility) ────────────────────
#  Old code does ``from api.a2a_trace import record_delegation``.
#  These thin wrappers keep that working while encouraging TraceBus.x().

record_delegation = TraceBus.record_delegation
record_response = TraceBus.record_response
record_react_trigger = TraceBus.record_react_trigger
record_consensus_sync = TraceBus.record_consensus_sync
record_heartbeat = TraceBus.record_heartbeat
build_runtime_evidence = TraceBus.build_runtime_evidence
normalize_message = TraceBus.normalize_message

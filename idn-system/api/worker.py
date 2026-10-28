"""
api/worker.py
=============

单 Worker 持久化任务消费者 —— 意图编排的后台执行引擎。

# 系统角色
作为 FastAPI 的后台任务（asyncio.create_task），轮询 IntentStore 中的
待处理意图（PENDING 状态），提交给 IDNOrchestrator 执行，
并将最终结果写回 IntentStore。

# 关键功能
- 轮询消费：从 SQLite 持久化队列中取出待处理意图
- 编排执行：调用 IDNOrchestrator.run() 驱动 LangGraph 状态机
- 结果回写：将 workflow 结果序列化写入 IntentStore
- Stale Job Recovery：检测被中断/超时的任务，重新入队
- 审批超时处理：IDN_APPROVAL_TIMEOUT_SECONDS 超时后自动 abort

# 主要依赖
- api.store.IntentStore：持久化任务队列
- orchestrator.graph.IDNOrchestrator：LangGraph 编排引擎
- models.agent_state.GlobalAgentState：状态容器

# 启动方式
开发模式：作为 FastAPI lifespan 的 background task 启动
生产模式：python -m api.worker（独立进程）
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
import logging
import os
import sqlite3
import socket
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from api.store import IntentStore, get_store
from models.agent_state import GlobalAgentState
from models.intent import IntentPriority, IntentStatus, UserIntent

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _state_value(state: Any, key: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _terminal_workflow_status(status: Any, *, error_message: Any, needs_clarification: bool) -> str:
    """Normalize a completed graph result to an API lifecycle state."""
    # The graph uses a generic terminal branch for a paused parse in a few
    # legacy paths, leaving ``current_status=failed`` despite a durable,
    # affirmative clarification contract.  Required user input is the
    # business conclusion here; it must never be converted into a failure by
    # the compatibility projection below.
    if needs_clarification:
        return IntentStatus.PENDING_CLARIFICATION.value
    value = str(_jsonable(status) or "unknown")
    waiting = {
        IntentStatus.PENDING_APPROVAL.value,
        IntentStatus.PENDING_CLARIFICATION.value,
        IntentStatus.WAITING_FOR_INFRASTRUCTURE.value,
    }
    terminal = {
        IntentStatus.SUCCESS.value,
        IntentStatus.FAILED.value,
        IntentStatus.DEGRADED.value,
        IntentStatus.UNSATISFIABLE.value,
    }
    if error_message and not needs_clarification and value not in terminal | waiting:
        return IntentStatus.FAILED.value
    return value


def _apply_awaiting_input_contract(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project a persisted clarification into one UI-safe state contract.

    ``pending_clarification`` is the established storage status and
    ``NEEDS_CLARIFICATION`` is the corresponding domain lifecycle.  Clients
    must not infer either one from an agent's natural-language message.  This
    projection gives them the explicit fields needed to render an enabled
    continuation composer after a refresh.
    """
    if not snapshot.get("needs_clarification"):
        return snapshot

    missing_fields: list[str] = []
    for value in snapshot.get("unresolved_fields") or []:
        field = str(value or "").strip()
        if field and field not in missing_fields:
            missing_fields.append(field)
    for detail in snapshot.get("clarification_details") or []:
        if not isinstance(detail, dict):
            continue
        field = str(detail.get("field") or "").strip()
        if field and field not in missing_fields:
            missing_fields.append(field)

    snapshot.update(
        {
            "phase": "AWAITING_USER_INPUT",
            "requires_user_input": True,
            "clarification_prompt": snapshot.get("clarification_question"),
            "missing_fields": missing_fields,
            "reason_code": "MISSING_REQUIRED_FIELDS",
        }
    )
    return snapshot


def _workflow_record(final_state: Any) -> tuple[str, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Extract results intended for the REST API while checkpoints keep full state."""
    from api.decision_report import build_decision_artifacts

    logs = _jsonable(_state_value(final_state, "execution_log", []))
    a2a_timeline, decision_report = build_decision_artifacts(final_state)
    milestones = [
        {
            "stage": entry.get("stage", ""),
            "status": entry.get("status", ""),
            "summary": entry.get("output_summary", ""),
            "duration_ms": entry.get("duration_ms"),
        }
        for entry in logs
    ]
    yang = _jsonable(_state_value(final_state, "yang_draft"))
    intent_plan = _jsonable(_state_value(final_state, "intent_plan"))
    error_message = _state_value(final_state, "error_message")
    needs_clarification = bool(_state_value(final_state, "needs_clarification", False))
    status = _terminal_workflow_status(
        _state_value(final_state, "current_status", "unknown"),
        error_message=error_message,
        needs_clarification=needs_clarification,
    )
    semantic_only_completed = bool(
        _state_value(final_state, "semantic_only_completed", False)
    )
    if semantic_only_completed:
        status = "semantic_complete"
    if isinstance(decision_report, dict):
        decision_report["final_status"] = status
    safety_report = _jsonable(_state_value(final_state, "safety_council_report"))
    snapshot = {
        "current_stage": status,
        "intent_plan_id": (intent_plan or {}).get("plan_id"),
        "confidence_score": (yang or {}).get("confidence_score"),
        "yang_policy_name": (yang or {}).get("network_policy", {}).get("policy_name"),
        "verification_passed": _state_value(final_state, "verification_passed"),
        "error_message": error_message,
        "needs_clarification": needs_clarification,
        "clarification_question": _state_value(final_state, "clarification_question"),
        "clarification_questions": _jsonable(
            _state_value(final_state, "clarification_questions", [])
        ),
        "decision_report_ready": bool(decision_report),
        "a2a_message_count": len(a2a_timeline),
        "safety_council_ready": bool(safety_report),
        "execution_mode": "SEMANTIC_ONLY" if semantic_only_completed else "NORMAL",
        "semantic_only_completed": semantic_only_completed,
        "terminal_reason": _state_value(final_state, "terminal_reason"),
    }
    result = {
        "intent_plan": intent_plan,
        "yang_policies": _jsonable(_state_value(final_state, "yang_policies", [])),
        "policy_results": _jsonable(_state_value(final_state, "policy_results", [])),
        "policy_conflict_report": _jsonable(_state_value(final_state, "conflict_report")),
        "consensus_decision": _jsonable(_state_value(final_state, "consensus_decision")),
        "flow_table_plan": _jsonable(_state_value(final_state, "flow_table_plan")),
        "rule_verification_report": _jsonable(
            _state_value(final_state, "rule_verification_report")
        ),
        "safety_council_report": _jsonable(
            safety_report
        ),
        "execution_result": _jsonable(_state_value(final_state, "execution_result")),
        "status_verification_report": _jsonable(
            _state_value(final_state, "verification_report")
        ),
        "state_audit": _jsonable(_state_value(final_state, "state_audit", [])),
        "a2a_timeline": a2a_timeline,
        "decision_report": decision_report,
        "execution_mode": "SEMANTIC_ONLY" if semantic_only_completed else "NORMAL",
    }
    return str(status), snapshot, milestones, result


def _intent_lifecycle_from_terminal_status(status: str, lifecycle_type: Any) -> Any:
    """Translate a terminal worker contract without inferring success from END."""
    return {
        "SUCCESS": lifecycle_type.SATISFIED,
        "DEGRADED": lifecycle_type.DEGRADED,
        "UNSATISFIABLE": lifecycle_type.UNSATISFIABLE,
        "WAITING_FOR_INFRASTRUCTURE": lifecycle_type.DEGRADED,
    }.get(str(status or "").upper(), lifecycle_type.FAILED)


def _skip_legacy_lifecycle_projection(final_status: str) -> bool:
    """Return whether a graph conclusion must not be replayed as deployment.

    ``UNSATISFIABLE`` emitted by the conflict/admission gate is already
    authoritatively persisted by ``ingest_pipeline_result``.  The historical
    compatibility bridge has no execution evidence to add in that case, and
    replaying its generic RECEIVED-to-VERIFYING bridge would fabricate an
    active plan/policy ownership projection after a pre-compiler rejection.
    """
    return str(final_status or "").upper() in {
        IntentStatus.UNSATISFIABLE.value.upper(),
        IntentStatus.PENDING_CLARIFICATION.value.upper(),
        "SEMANTIC_COMPLETE",
    }


def _project_legacy_workflow_lifecycle(final_state: Any, final_status: str) -> None:
    """Project a completed legacy Graph run into the authoritative Intent domain.

    Graph END is only a technical completion signal.  This bridge writes the
    corresponding lifecycle, assurance record and policy ownership without
    invoking agents, LLMs or the data plane.
    """
    if _skip_legacy_lifecycle_projection(final_status):
        logger.info(
            "Intent %s was rejected before compilation; skip legacy deployment projection.",
            _state_value(final_state, "intent_id"),
        )
        return
    try:
        from models.intent_domain import (
            EvidenceItem, EvidenceQuality, ExecutionStatus, IntentAssuranceResult,
            IntentExecutionResult, IntentLifecycle, IntentPlanIR, PlanValidationStatus,
        )
        from persistence.intent_domain_store import get_intent_domain_store

        intent_id = str(_state_value(final_state, "intent_id"))
        # The persisted worker result is the graph-completion contract.  A
        # late routing helper must never turn a business DEGRADED result into
        # FAILED merely because it left a stale in-memory `current_status`.
        # Prefer the explicit terminal result and use the state only for
        # callers that did not supply one (for example checkpoint recovery).
        state_status = str(final_status or _state_value(final_state, "current_status") or "").upper()
        target = _intent_lifecycle_from_terminal_status(state_status, IntentLifecycle)
        domain = get_intent_domain_store()
        runtime = domain.get_runtime(intent_id)
        if runtime is None:
            return
        if runtime.lifecycle == IntentLifecycle.NEEDS_APPROVAL:
            domain.record_event(intent_id, "APPROVAL_GRANTED", actor="legacy_workflow_projector", reason="execution evidence exists after approval", data={"run_id": _state_value(final_state, "run_id")})
        # Advance stale approval projections monotonically through the real
        # post-approval phases.  Each event is durable; no repair endpoint is
        # required and the old NEEDS_APPROVAL cannot overwrite later state.
        bridge = [
            IntentLifecycle.UNDERSTANDING, IntentLifecycle.PARSED,
            IntentLifecycle.SENSING, IntentLifecycle.PLANNING,
            IntentLifecycle.PLAN_READY, IntentLifecycle.CHECKING,
            IntentLifecycle.DEPLOYING, IntentLifecycle.VERIFYING, target,
        ]
        for lifecycle in bridge:
            if runtime.lifecycle == lifecycle:
                continue
            try:
                runtime = domain.transition(
                    intent_id, lifecycle, actor="legacy_workflow_projector",
                    reason="authoritative legacy workflow lifecycle projection",
                    data={"graph_execution_status": "COMPLETED", "run_id": _state_value(final_state, "run_id")},
                )
            except Exception:
                # A prior valid lifecycle may already be beyond this bridge;
                # keep progressing only through legal state-machine edges.
                continue
        execution = _state_value(final_state, "execution_result", {}) or {}
        verification = _state_value(final_state, "verification_report", {}) or {}
        legacy_plan = _jsonable(_state_value(final_state, "intent_plan")) or {}
        flow_plan = _jsonable(_state_value(final_state, "flow_table_plan")) or {}
        plan_id = str(legacy_plan.get("plan_id") or flow_plan.get("plan_id") or f"PLAN-LEGACY-{intent_id}")
        context_id = str(flow_plan.get("context_id") or f"CTX-LEGACY-{intent_id}")
        existing_plans = {item.get("plan_id") for item in domain.plans(intent_id)}
        if plan_id not in existing_plans:
            domain.save_plan(IntentPlanIR(
                plan_id=plan_id, intent_id=intent_id, intent_revision=runtime.current_revision,
                context_id=context_id, selected_candidate_id=f"CAND-LEGACY-{intent_id}",
                selected_reasoning=["projected from completed legacy workflow"],
                validation_status=PlanValidationStatus.VALID,
                expected_outcomes={"legacy_intent_plan": legacy_plan, "flow_table_plan": flow_plan},
                legacy_plan_id=str(legacy_plan.get("plan_id") or plan_id),
            ))
        runtime.active_plan_id = plan_id
        policy_id = (
            execution.get("existing_policy_id")
            or (verification.get("policy_commit") or {}).get("policy_id")
            or getattr(getattr(_state_value(final_state, "yang_draft"), "network_policy", None), "policy_id", None)
        )
        if policy_id:
            runtime.active_policy_ids = [str(policy_id)]
            runtime.desired_policy = _jsonable(_state_value(final_state, "yang_draft")) or runtime.desired_policy
            domain._write_runtime(runtime)
            domain.record_event(intent_id, "POLICY_OWNERSHIP_PROJECTED", actor="legacy_workflow_projector", reason="active desired policy associated with current Intent", data={"policy_id": policy_id, "traffic_scope_key": execution.get("traffic_scope_key")})
        execution_id = str(execution.get("execution_id") or f"EXEC-LEGACY-{intent_id}")
        existing_executions = {item.get("execution_id") for item in domain.executions(intent_id)}
        if execution and execution_id not in existing_executions:
            domain.save_execution(IntentExecutionResult(
                execution_id=execution_id, intent_id=intent_id, plan_id=plan_id,
                context_id=context_id, selected_candidate_id=f"CAND-LEGACY-{intent_id}",
                selected_path=list((execution.get("qos_enforcement") or {}).get("qos_binding", {}).get("current_path") or []),
                status=ExecutionStatus.COMPLETED if execution.get("config_applied") else ExecutionStatus.FAILED,
                phase_history=[{"phase": "legacy_workflow_projection", "raw_execution": execution}],
                flows_installed=len(execution.get("installed_flows") or []),
                config_verified=bool(execution.get("config_applied")),
                commit_verified=bool(execution.get("config_applied")),
                error=None if execution.get("config_applied") else str(execution.get("errors") or "configuration readback failed"),
            ))
        kpi = verification.get("kpi") or {}
        throughput = kpi.get("throughput_mbps")
        if throughput is not None:
            domain.save_evidence(intent_id, EvidenceItem(
                evidence_id=f"EVD-LEGACY-THROUGHPUT-{intent_id}", intent_id=intent_id,
                source="mininet_service_probe", metric="throughput_mbps", value=throughput,
                unit="Mbps", quality=EvidenceQuality.TRUSTED if verification.get("passed") else EvidenceQuality.DEGRADED,
                correlation={"plan_id": plan_id, "execution_id": execution_id}, raw=verification,
            ))
        assurance_id = _state_value(final_state, "assurance_id")
        samples = list(_state_value(final_state, "assurance_samples", []) or [])
        if assurance_id:
            domain.save_assurance(IntentAssuranceResult(
                assurance_id=str(assurance_id), intent_id=intent_id,
                plan_id=getattr(runtime, "active_plan_id", None),
                execution_id=execution_id, lifecycle=target,
                evidence_quality=EvidenceQuality.TRUSTED if samples else EvidenceQuality.MISSING,
                satisfied=True if target == IntentLifecycle.SATISFIED else False if samples else None,
                summary=str(verification.get("verification_state") or "ASSURANCE_COMPLETED"),
                failure_reason=verification.get("terminal_reason"),
            ))
            domain.record_event(intent_id, "ASSURANCE_COMPLETED" if samples else "ASSURANCE_INCONCLUSIVE", actor="legacy_workflow_projector", reason="persisted current-run assurance projection", data={"assurance_id": assurance_id, "assurance_generation": _state_value(final_state, "assurance_generation"), "samples": samples})
    except Exception:
        logger.exception("Legacy workflow lifecycle projection failed")


def _attach_runtime_a2a_trace(
    *,
    store: IntentStore,
    intent_id: str,
    snapshot: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Prefer the persisted runtime A2A trace over the synthesized fallback timeline."""
    from api.a2a_trace import build_runtime_evidence

    runtime_events = [
        event
        for event in store.list_events(intent_id)
        if event.get("event_type") == "a2a_message"
    ]
    runtime_evidence = build_runtime_evidence(intent_id, runtime_events)
    result["a2a_runtime_evidence"] = runtime_evidence
    if not runtime_evidence["runtime_trace_ready"]:
        snapshot["runtime_a2a_trace_ready"] = False
        return

    result["compiled_a2a_timeline"] = result.get("a2a_timeline") or []
    result["a2a_timeline"] = runtime_evidence["messages"]
    snapshot["runtime_a2a_trace_ready"] = True
    snapshot["a2a_message_count"] = runtime_evidence["message_count"]
    decision_report = result.get("decision_report") or {}
    collaboration = decision_report.setdefault("collaboration", {})
    collaboration["runtime_message_count"] = runtime_evidence["message_count"]
    collaboration["runtime_trace_ready"] = True
    collaboration["runtime_agents"] = runtime_evidence["agents"]
    collaboration["runtime_message_types"] = runtime_evidence["message_types"]
    decision_report["a2a_runtime_evidence"] = {
        key: value
        for key, value in runtime_evidence.items()
        if key != "messages"
    }


def _collect_compensatable_flows(record: dict[str, Any]) -> list[dict[str, str]]:
    """Collect and de-duplicate real ONOS IDs from all nested execution results."""
    collected: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(device_id: Any, flow_id: Any) -> None:
        device = str(device_id or "")
        flow = str(flow_id or "")
        key = (device, flow)
        if device and flow and key not in seen:
            seen.add(key)
            collected.append({"device_id": device, "flow_id": flow})

    def walk(value: Any, device_hint: str = "") -> None:
        if isinstance(value, dict):
            device = str(
                value.get("device_id")
                or value.get("deviceId")
                or value.get("edge_device")
                or value.get("device")
                or device_hint
                or ""
            )
            add(device, value.get("flow_id") or value.get("flowId"))
            for flow_id in value.get("flow_ids") or []:
                add(device, flow_id)
            for key, child in value.items():
                child_hint = key if isinstance(key, str) and key.startswith("device:") else device
                walk(child, child_hint)
        elif isinstance(value, list):
            for item in value:
                walk(item, device_hint)

    walk(record.get("result") or {})
    walk(record.get("snapshot") or {})
    return collected


def _newly_created_qos_policies(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only policies whose *current run* created a new TC resource.

    A terminal KPI result must never tear down an older, reused policy.  The
    execution result is therefore the ownership authority here: only an
    ``operation=CREATE`` / ``executor_status=CREATED`` result may release the
    matching QoS binding.  This also makes pre-compiler ``UNSATISFIABLE``
    outcomes a strict no-op.
    """
    result = record.get("result") or {}
    yang_policies = result.get("yang_policies") or []
    by_policy_id = {
        str((item.get("network_policy") or {}).get("policy_id") or ""): item
        for item in yang_policies
        if isinstance(item, dict)
    }
    created: list[dict[str, Any]] = []
    for policy_result in result.get("policy_results") or []:
        if not isinstance(policy_result, dict):
            continue
        execution = policy_result.get("execution_result") or {}
        if (
            execution.get("operation") != "CREATE"
            or execution.get("executor_status") != "CREATED"
            or not execution.get("data_plane_changed")
        ):
            continue
        policy = by_policy_id.get(str(policy_result.get("policy_id") or ""))
        if policy is not None:
            created.append(policy)
    return created


def _has_newly_created_dataplane_resources(record: dict[str, Any] | None) -> bool:
    """Whether a terminal result owns newly-created resources to compensate."""
    if not record:
        return False
    return bool(_newly_created_qos_policies(record) or _collect_compensatable_flows(record))


async def _compensate_flows(
    intent_id: str,
    store: IntentStore,
    *,
    delete_policy_metadata: bool = True,
    acquire_lease: bool = True,
    flow_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compensate newly-created ONOS, TC and binding state for *intent_id*.

    The control plane first persists an execution result, then this function
    acquires a formal global recovery lease and invokes the same southbound
    actuator used by normal QoS cleanup.  Policy metadata is removed only
    after every owned data-plane resource has been confirmed removed.
    """
    record = store.get(intent_id)
    result: dict[str, Any] = {
        "status": "noop",
        "deleted": 0,
        "failed": 0,
        "qos_deleted": 0,
        "qos_failed": 0,
        "policies_deleted": 0,
        "errors": [],
    }
    if not record:
        return result

    installed = _collect_compensatable_flows(record)
    # The durable policy runtime is the fallback authority for workflows that
    # persist their flow receipts in ``policies.runtime_json`` rather than the
    # legacy intent result projection.  This keeps withdrawal exact and does
    # not infer a selector from text.
    known_flow_refs = {
        (str(item.get("device_id") or item.get("deviceId") or ""),
         str(item.get("flow_id") or item.get("flowId") or ""))
        for item in installed
    }
    for item in flow_refs or []:
        device_id = str(item.get("device_id") or item.get("deviceId") or "")
        flow_id = str(item.get("flow_id") or item.get("flowId") or "")
        if device_id and flow_id and (device_id, flow_id) not in known_flow_refs:
            known_flow_refs.add((device_id, flow_id))
            installed.append({"device_id": device_id, "flow_id": flow_id})
    created_qos_policies = _newly_created_qos_policies(record)
    if installed or created_qos_policies:
        logger.warning(
            "Compensating intent %s: flows=%d qos_resources=%d",
            intent_id, len(installed), len(created_qos_policies),
        )

    # Stand-alone terminal compensation owns a durable global writer lease.
    # Withdrawal already owns an INTENT_WITHDRAWAL lease, however, and must
    # reuse that fenced context.  Attempting to acquire a second lease while
    # holding the first one is rejected by fencing and used to leave the P4
    # classifier behind while the caller incorrectly reported success.
    lease = None
    try:
        from infra.network_write_fencing import NetworkWriteFencing, require_network_write, write_context

        fencing = NetworkWriteFencing(store)
        if acquire_lease:
            lease = fencing.acquire(
                owner_type="TERMINAL_COMPENSATION",
                owner_id=intent_id,
                recovery_id=f"COMPENSATE-{intent_id}",
                ttl_seconds=120,
            )
            writer_context = write_context(
                owner_type=lease["owner_type"],
                owner_id=lease["owner_id"],
                fencing_token=int(lease["fencing_token"]),
            )
        else:
            writer_context = nullcontext()

        with writer_context:
            if installed:
                from mcp_servers.onos_server import get_onos_client, onos_client as shared_onos_client

                onos = get_onos_client()
                owns_onos = onos is not shared_onos_client
                by_device: dict[str, list[str]] = {}
                for item in installed:
                    did = item.get("device_id") or item.get("deviceId") or ""
                    fid = item.get("flow_id") or item.get("flowId") or ""
                    if did and fid:
                        by_device.setdefault(did, []).append(fid)

                for device_id, flow_ids in by_device.items():
                    authorization = require_network_write(operation="ONOS_DELETE_COMPENSATION", store=store)
                    if not authorization["authorized"]:
                        result["failed"] += len(flow_ids)
                        result["errors"].append({"device_id": device_id, "authorization": authorization})
                        continue
                    try:
                        delete_result = await asyncio.shield(
                            asyncio.wait_for(onos.delete_flows(device_id, flow_ids), timeout=10.0)
                        )
                        deleted = int(delete_result.get("flows_deleted") or 0)
                        failed = max(len(flow_ids) - deleted, 0)
                        result["deleted"] += deleted
                        result["failed"] += failed
                        fencing.record_execution(authorization, write_executed=bool(deleted))
                        if delete_result.get("errors"):
                            result["errors"].append({
                                "device_id": device_id,
                                "errors": delete_result["errors"],
                            })
                    except Exception as exc:
                        result["failed"] += len(flow_ids)
                        result["errors"].append({"device_id": device_id, "error": str(exc)})

            if created_qos_policies:
                from api.qos import clear_qos_policies

                for policy in created_qos_policies:
                    try:
                        clear_result = await clear_qos_policies(yang_policy=policy)
                        if clear_result.get("status") in {"ok", "not_found"}:
                            result["qos_deleted"] += 1
                        else:
                            result["qos_failed"] += 1
                            result["errors"].append({"qos": clear_result})
                    except Exception as exc:
                        result["qos_failed"] += 1
                        result["errors"].append({"qos": str(exc)})

    except Exception as exc:
        logger.exception("Compensation failed for intent %s: %s", intent_id, exc)
        # A lease/authorization failure means no delete was verified.  It
        # must drive the caller to WITHDRAW_PENDING_CLEANUP instead of being
        # hidden as an otherwise-successful no-op compensation result.
        result["failed"] += max(len(installed), len(created_qos_policies), 1)
        result["errors"].append({"error": str(exc)})
    finally:
        if locals().get("owns_onos") and locals().get("onos") is not None:
            try:
                await onos.close()
            except Exception:
                logger.exception("Failed to close scoped ONOS compensation client")
        if lease is not None:
            try:
                NetworkWriteFencing(store).release(lease["lease_id"])
            except Exception as exc:
                result["errors"].append({"lease_release": str(exc)})
    result["status"] = "ok" if result["failed"] == 0 and result["qos_failed"] == 0 else "partial"
    if result["status"] == "ok":
        try:
            from persistence.policy_store import PolicyStore

            if delete_policy_metadata:
                result["policies_deleted"] = PolicyStore(store).delete_by_intent(intent_id)
        except Exception as exc:
            result["errors"].append({"policy_store": str(exc)})
    # Persist compensation result
    try:
        existing = store.get(intent_id)
        if existing:
            existing_snapshot = existing.get("snapshot") or {}
            existing_snapshot["compensation_state"] = result
            store.update(intent_id, snapshot=existing_snapshot)
    except Exception:
        pass
    return result


async def execute_intent_job(job: dict[str, Any], store: IntentStore | None = None) -> None:
    """Execute one queued intent and persist all user-visible workflow output."""
    store = store or get_store()
    payload = job["payload"]
    intent_id = job["intent_id"]
    session_id = payload["session_id"]
    orchestrator = None

    # ── Demo mode: simulate pipeline deterministically (no real LLM calls) ──
    if os.getenv("IDN_DEMO_MODE", "").lower() == "true":
        from verify.demo_simulator import simulate_pipeline

        await simulate_pipeline(intent_id, session_id, payload["text"], store)
        # Mark the job completed so it won't be re-claimed
        store.complete_job(job["job_id"])
        return

    # ── ExecutionTracker: emits pipeline events into the conversation stream ──
    from api.execution.tracker import ExecutionTracker

    tracker = ExecutionTracker(intent_id, session_id, store)

    # Skip jobs whose intent is already terminal or cancelled
    record = store.get(intent_id)
    if record and record.get("status") in ("success", "failed", "cancelled"):
        logger.warning("Skipping job %s: intent %s already terminal (%s)", job["job_id"], intent_id, record["status"])
        return

    async def _check_cancelled() -> bool:
        """Return True if the intent has been cancelled mid-execution."""
        r = store.get(intent_id)
        return r is not None and r.get("status") == "cancelled"
    try:
        user_intent = UserIntent(
            intent_id=intent_id,
            text=payload["text"],
            priority=IntentPriority(payload["priority"]),
            user_id=payload["user_id"],
            session_id=session_id,
            context=payload.get("context"),
        )
        # A checkpoint is only reusable within the same intent revision and
        # worker run.  Policy scope/session identity is intentionally not a
        # checkpoint identity because it can carry stale assurance facts.
        revision = int(payload.get("revision") or 1)
        run_id = str(job["job_id"])
        checkpoint_thread_id = f"{intent_id}::r{revision}::run-{run_id}"
        # ── Restore network_snapshot from this pipeline run checkpoint ──
        existing_snapshot = None
        try:
            from orchestrator.checkpointer import _checkpointer_manager
            if _checkpointer_manager is not None and _checkpointer_manager.checkpointer is not None:
                cp_config = {"configurable": {"thread_id": checkpoint_thread_id}}
                cp = await _checkpointer_manager.checkpointer.aget_tuple(cp_config)
                if cp and cp.checkpoint:
                    ch = cp.checkpoint.get("channel_values", {})
                    snap_data = ch.get("network_snapshot")
                    if snap_data is not None:
                        from models.network import NetworkSnapshot
                        existing_snapshot = (
                            NetworkSnapshot(**snap_data)
                            if isinstance(snap_data, dict) else snap_data
                        )
                        logger.info(
                            "Restored network_snapshot from checkpoint for %s "
                            "(devices=%d)", intent_id,
                            len(existing_snapshot.devices) if existing_snapshot else 0,
                        )
        except Exception:
            pass  # best-effort; SenseNode will collect fresh if needed

        state = GlobalAgentState(
            intent_id=intent_id,
            user_intent=user_intent,
            network_snapshot=existing_snapshot,
            revision=revision,
            run_id=run_id,
        )
        try:
            from api.a2a_trace import record_heartbeat

            record_heartbeat(
                state,
                agent_id="orchestrator",
                status="running",
                stage="understanding",
                summary="A2A runtime trace started for this intent.",
                metadata={"job_id": job["job_id"], "session_id": session_id},
            )
        except Exception:
            pass

        from api.events import get_event_manager

        get_event_manager().push(
            intent_id,
            "stage_change",
            {
                "stage": "understanding",
                "agent_id": "orchestrator",
                "status": "running",
                "summary": "Intent semantic understanding started",
            },
        )
        # Emit an initial heartbeat so SSE clients know the pipeline is alive
        get_event_manager().push(
            intent_id,
            "heartbeat",
            {"ts": _utc_now()},
        )

        # ── Periodic heartbeat while the orchestrator runs ──
        async def _periodic_heartbeat():
            while True:
                await asyncio.sleep(15)
                try:
                    get_event_manager().push(
                        intent_id,
                        "heartbeat",
                        {"ts": _utc_now()},
                    )
                except Exception:
                    pass

        heartbeat_task = asyncio.create_task(_periodic_heartbeat())

        from orchestrator.graph import IDNOrchestrator

        orchestrator = IDNOrchestrator()
        await orchestrator.initialize()

        # ── Track pipeline start ──
        await tracker.on_stage_start("understanding", "intent_understanding")

        _diag_timing = {}  # [DIAG] collect all timing data for API retrieval
        _t0 = time.monotonic()
        logger.info("[DIAG-0001] Starting orchestrator.run() at t=%.1fs", _t0)
        try:
            final_state = await orchestrator.run(state)
        except Exception as exc:
            from api.cancellation import IntentExecutionCancelled
            if not isinstance(exc, IntentExecutionCancelled):
                raise
            cancelled_state = exc.state or state
            final_status, snapshot, milestones, result = _workflow_record(cancelled_state)
            # Make any uncommitted delivery receipts visible to the formal
            # compensator before deciding the terminal cancellation meaning.
            store.update(
                intent_id, status="cancelling", snapshot=snapshot,
                milestones=milestones, result=result,
            )
            from api.cancellation import committed_policy_in_state, finalize_cancellation
            after_commit = committed_policy_in_state(cancelled_state)
            if not after_commit and _has_newly_created_dataplane_resources(store.get(intent_id)):
                await _compensate_flows(intent_id, store)
            terminal = finalize_cancellation(
                store, intent_id, after_commit=after_commit,
                snapshot=store.get(intent_id).get("snapshot") if store.get(intent_id) else snapshot,
            )
            try:
                await tracker.on_execution_complete({
                    "status": terminal["status"].lower(),
                    "summary": "Intent execution was cancelled by the operator.",
                })
            except Exception:
                pass
            logger.info("Intent %s cancellation finalized as %s", intent_id, terminal["status"])
            return
        _t1 = time.monotonic()
        _diag_timing["orchestrator_run_s"] = round(_t1 - _t0, 2)
        logger.info("[DIAG-0002] orchestrator.run() completed in %.1fs", _t1 - _t0)

        # A request can arrive while the final graph node is already running.
        # The node then returns normally, but its result must not overwrite
        # CANCELLING.  Resolve through the exact same durable terminal path as
        # a node-boundary cancellation.
        from api.cancellation import cancellation_requested, committed_policy_in_state, finalize_cancellation
        if cancellation_requested(store.get(intent_id)):
            final_status, snapshot, milestones, result = _workflow_record(final_state)
            store.update(
                intent_id, status="cancelling", snapshot=snapshot,
                milestones=milestones, result=result,
            )
            after_commit = committed_policy_in_state(final_state)
            if not after_commit and _has_newly_created_dataplane_resources(store.get(intent_id)):
                await _compensate_flows(intent_id, store)
            terminal = finalize_cancellation(
                store, intent_id, after_commit=after_commit,
                snapshot=store.get(intent_id).get("snapshot") if store.get(intent_id) else snapshot,
            )
            logger.info("Intent %s cancellation finalized after graph return as %s", intent_id, terminal["status"])
            return

        normalized_trace_status = _terminal_workflow_status(
            _state_value(final_state, "current_status", "unknown"),
            error_message=_state_value(final_state, "error_message"),
            needs_clarification=bool(
                _state_value(final_state, "needs_clarification", False)
            ),
        )
        try:
            from api.a2a_trace import record_heartbeat

            record_heartbeat(
                final_state,
                agent_id="orchestrator",
                status=normalized_trace_status,
                stage="verify",
                summary="A2A runtime trace completed for this intent.",
                metadata={"job_id": job["job_id"]},
            )
        except Exception:
            pass
        _t2 = time.monotonic()
        final_status, snapshot, milestones, result = _workflow_record(final_state)
        graph_final_status = str(final_status)
        _t3 = time.monotonic()
        _diag_timing["workflow_record_ms"] = round((_t3 - _t2) * 1000, 1)
        logger.info("[DIAG-0003] _workflow_record() took %.2fs", _t3 - _t2)
        # The formal control plane owns the authoritative IntentSpec.  Ingest
        # before the compatibility bridge advances the runtime to a terminal
        # lifecycle; otherwise the initial NOT_ATTEMPTED placeholder cannot
        # legally be replaced by the completed workflow's real LLM result.
        try:
            from orchestrator.intent_control_plane import get_intent_control_plane

            control_plane = get_intent_control_plane()
            runtime = await control_plane.ingest_pipeline_result(
                intent_id=intent_id,
                legacy_status=str(final_status),
                result=result,
                snapshot=snapshot,
            )
            if snapshot.get("needs_clarification"):
                # Use the persisted IntentSpec as the authoritative source for
                # the conversational contract.  The legacy graph may carry a
                # short question string, but it does not own structured detail
                # or the unresolved-field set.
                spec = control_plane.domain.get_spec(intent_id)
                if spec is not None:
                    snapshot["clarification_questions"] = list(
                        spec.clarification_questions
                        or snapshot.get("clarification_questions")
                        or []
                    )
                    snapshot["clarification_details"] = (
                        [
                        item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                        for item in (spec.clarification_details or [])
                        ]
                        or snapshot.get("clarification_details")
                        or []
                    )
                    snapshot["unresolved_fields"] = list(
                        spec.unresolved_fields
                        or snapshot.get("unresolved_fields")
                        or []
                    )
            # A scheduler-only preference deliberately ends the legacy graph
            # before policy compilation.  Its authoritative control-plane
            # readback and assurance are completed by ingestion, so expose a
            # truthful success state to API/UI rather than legacy
            # ``semantic_complete``.
            if getattr(runtime, "lifecycle", None) and runtime.lifecycle.value == "SATISFIED":
                final_status = IntentStatus.SUCCESS.value
                snapshot["current_stage"] = final_status
                snapshot["execution_mode"] = "CONTROL_PLANE_SCHEDULING_PREFERENCE"
                snapshot["semantic_only_completed"] = False
        except Exception:
            logger.exception("Intent control-plane ingestion failed for %s", intent_id)
        # Preserve the graph conclusion for this compatibility bridge.  A
        # preference may legitimately upgrade its *API* terminal status from
        # semantic_complete to success after durable scheduler readback, but
        # the legacy graph still must not fabricate policy/execution ownership.
        _apply_awaiting_input_contract(snapshot)
        _project_legacy_workflow_lifecycle(final_state, graph_final_status)
        _attach_runtime_a2a_trace(
            store=store,
            intent_id=intent_id,
            snapshot=snapshot,
            result=result,
        )
        _t4 = time.monotonic()
        _diag_timing["a2a_trace_ms"] = round((_t4 - _t3) * 1000, 1)
        logger.info("[DIAG-0004] _attach_runtime_a2a_trace() took %.2fs", _t4 - _t3)

        # ── Track pipeline completion ──
        try:
            status_str = str(final_status)
            if status_str == "success":
                await tracker.on_execution_complete({
                    "status": "success",
                    "summary": "✅ 意图执行完成，闭环验证通过。",
                    "decision_report": result.get("decision_report"),
                    "a2a_timeline": result.get("a2a_timeline"),
                    "a2a_runtime_evidence": result.get("a2a_runtime_evidence"),
                })
            elif status_str == "failed":
                await tracker.on_execution_complete({
                    "status": "failed",
                    "summary": f"❌ 意图执行失败: {snapshot.get('error_message', '未知错误')}",
                })
            else:
                await tracker.on_execution_complete({
                    "status": status_str,
                    "summary": f"意图执行结束，状态: {status_str}",
                })
        except Exception:
            pass  # tracker is best-effort, don't fail the job
        # ── Checkpoint-verified write ──
        checkpoint_ts = _utc_now()
        _t5 = time.monotonic()
        try:
            store.update_with_checkpoint_verification(
                intent_id,
                checkpoint_timestamp=checkpoint_ts,
                status=final_status,
                snapshot=snapshot,
                milestones=milestones,
                result=result,
            )
        except Exception as write_exc:
            # Retry up to 3 times on write failure
            logger.warning("Store update failed (attempt 1): %s", write_exc)
            for retry in range(2, 5):
                time.sleep(1.0 * (2 ** (retry - 2)))
                try:
                    store.update_with_checkpoint_verification(
                        intent_id,
                        checkpoint_timestamp=checkpoint_ts,
                        status=final_status,
                        snapshot=snapshot,
                        milestones=milestones,
                        result=result,
                    )
                    logger.info("Store update succeeded on attempt %d", retry)
                    break
                except Exception as retry_exc:
                    logger.error("Store update attempt %d failed: %s", retry, retry_exc)
                    if retry == 4:
                        logger.critical(
                            "Store update FAILED after 4 attempts for intent %s — "
                            "data is in checkpoint but not in API store",
                            intent_id,
                        )
        # A failed KPI after a newly-created policy is not a valid active
        # deployment.  ``UNSATISFIABLE`` may also be emitted by the admission
        # gate, so only compensate it when this run has authoritative CREATE
        # ownership; a pre-compiler rejection remains a no-op.
        terminal_compensation_required = (
            final_status == IntentStatus.FAILED.value
            or (
                final_status == IntentStatus.UNSATISFIABLE.value
                and _has_newly_created_dataplane_resources(store.get(intent_id))
            )
        )
        if terminal_compensation_required:
            try:
                compensation = await asyncio.shield(
                    asyncio.wait_for(_compensate_flows(intent_id, store), timeout=60.0)
                )
                snapshot["compensation_state"] = compensation
                logger.info(
                    "Terminal compensation for %s: flows_deleted=%d qos_deleted=%d failures=%d",
                    intent_id,
                    compensation.get("deleted", 0),
                    compensation.get("qos_deleted", 0),
                    compensation.get("failed", 0) + compensation.get("qos_failed", 0),
                )
            except Exception as comp_exc:
                logger.exception(
                    "Terminal-failure compensation failed for intent %s: %s",
                    intent_id, comp_exc,
                )
        _t6 = time.monotonic()
        _diag_timing["store_update_ms"] = round((_t6 - _t5) * 1000, 1)
        _diag_timing["total_post_pipeline_ms"] = round((_t6 - _t1) * 1000, 1)
        _diag_timing["total_job_wall_ms"] = round((_t6 - _t0) * 1000, 1)
        # ── Collect node-level timings from execution_log ──
        stage_durations = {}
        total_stage_ms = 0
        for entry in (getattr(final_state, 'execution_log', []) or []):
            key = getattr(entry, 'stage', 'unknown')
            key = key.value if hasattr(key, 'value') else str(key)
            dur = getattr(entry, 'duration_ms', 0) or 0
            stage_durations[key] = round(dur, 1)
            total_stage_ms += dur
        _diag_timing["stage_durations_ms"] = stage_durations
        _diag_timing["total_stage_sum_ms"] = round(total_stage_ms, 1)
        _diag_timing["unaccounted_gap_ms"] = round(max(0, (_t1 - _t0) * 1000 - total_stage_ms), 1)
        _diag_timing["unaccounted_pct"] = round(max(0, ((_t1 - _t0) * 1000 - total_stage_ms) / max(1, (_t1 - _t0) * 1000) * 100), 1)
        # Inject into snapshot for API retrieval
        snapshot["_diag_timing"] = _diag_timing
        logger.info("[DIAG-0005] store.update_with_checkpoint_verification() took %.2fs", _t6 - _t5)
        logger.info("[DIAG-0000] FULL pipeline breakdown: run=%.1fs stages_sum=%.1fs gap=%.1fs(%.0f%%) record=%.2fs a2a=%.2fs store=%.2fs TOTAL=%.1fs",
                    _t1 - _t0, total_stage_ms/1000, _diag_timing["unaccounted_gap_ms"]/1000, _diag_timing["unaccounted_pct"],
                    _t3 - _t2, _t4 - _t3, _t6 - _t5, _t6 - _t0)

        if snapshot["needs_clarification"] and snapshot["clarification_question"]:
            store.add_message(
                session_id,
                "assistant",
                "clarification_question",
                snapshot["clarification_question"],
                intent_id=intent_id,
                payload={"snapshot": snapshot},
            )
        elif final_status == IntentStatus.SUCCESS.value:
            store.add_message(
                session_id,
                "assistant",
                "final_result",
                "意图已执行并完成闭环验证。",
                intent_id=intent_id,
                payload={"snapshot": snapshot},
            )
        else:
            store.add_message(
                session_id,
                "assistant",
                "status",
                f"意图处理结束，当前状态：{final_status}。",
                intent_id=intent_id,
                payload={"snapshot": snapshot},
            )
    except Exception as exc:
        logger.exception("Intent %s execution failed", intent_id)
        # ── Track error in conversation ──
        try:
            await tracker.on_error(str(exc), stage="runtime")
        except Exception:
            pass
        from models.agent_state import ErrorInfo
        err = ErrorInfo.from_exception(exc, stage="runtime", agent_id="orchestrator")
        # asyncio.TimeoutError has an empty ``str(exc)``.  Preserve the node's
        # explicit timeout detail so users see e.g. "sense_agent timed out
        # after 10s (3 attempts)" instead of an empty message.
        failure_message = (
            getattr(state, "error_message", None)
            or err.message
            or f"{type(exc).__name__} during intent execution"
        )
        try:
            from api.a2a_trace import record_heartbeat

            record_heartbeat(
                state if "state" in locals() else {"intent_id": intent_id},
                agent_id="orchestrator",
                status="failed",
                stage="runtime",
                summary=f"A2A runtime trace failed: {failure_message}",
                metadata={"job_id": job["job_id"]},
            )
        except Exception:
            pass
        store.update(intent_id, status="failed", snapshot={
            "error_message": failure_message,
            "error_type": err.error_type.value,
            "error_code": err.error_code,
            "retryable": err.retryable,
            "retry_after_seconds": err.retry_after_seconds,
        })
        store.add_message(
            session_id,
            "assistant",
            "status",
            f"意图处理失败 [{err.error_type.value}]: {failure_message}",
            intent_id=intent_id,
        )
        # A nested node timeout is not the same as the outer job deadline.
        # Convert it before re-raising so process_next_job does not report the
        # configured 600-second job timeout after a 10-second node timeout.
        if isinstance(exc, asyncio.TimeoutError):
            raise RuntimeError(failure_message) from exc
        raise
    finally:
        if 'heartbeat_task' in locals():
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        if orchestrator is not None:
            await orchestrator.close()


# ── Worker health state (in-process, shared with health endpoint) ──────────

_worker_health: dict = {
    "ready": False,
    "running_since": None,
    "processed_count": 0,
    "error_count": 0,
    "last_error": None,
    "restart_count": 0,
    "last_restart_at": None,
    "active_jobs": 0,
}


def get_worker_health() -> dict:
    """Return a snapshot of the in-process worker health state."""
    return dict(_worker_health)


async def process_next_job(
    *,
    store: IntentStore | None = None,
    worker_id: str | None = None,
    executor=execute_intent_job,
) -> bool:
    """Claim and execute one queued task, returning False when the queue is empty."""
    store = store or get_store()
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    job = store.claim_next_job(worker_id)
    if job is None:
        return False

    job_id = job["job_id"]
    intent_id = job["intent_id"]
    job_timeout = float(os.getenv("IDN_JOB_TIMEOUT_SECONDS", "300"))
    heartbeat_interval = float(os.getenv("IDN_JOB_HEARTBEAT_SECONDS", "30"))

    # ── Heartbeat loop — signals the API process that this worker is alive ──
    async def _heartbeat_loop():
        while True:
            await asyncio.sleep(heartbeat_interval)
            try:
                store.update_job_heartbeat(job_id)
            except Exception:
                pass  # heartbeat is best-effort

    heartbeat_task = asyncio.create_task(_heartbeat_loop())

    try:
        _worker_health["active_jobs"] += 1
        # ── Overall job execution timeout ──
        await asyncio.wait_for(executor(job, store), timeout=job_timeout)
        # Shield the final state write so it cannot be cancelled mid-write
        await asyncio.shield(_shielded_complete(store, job_id))
        _worker_health["processed_count"] += 1
    except asyncio.TimeoutError:
        timeout_message = f"Execution timed out after {job_timeout}s"
        logger.error("Job %s timed out after %.0fs for intent %s", job_id, job_timeout, intent_id)
        # ── Saga compensation: clean up any partially-installed ONOS flows ──
        try:
            compensate_result = await asyncio.shield(
                asyncio.wait_for(_compensate_flows(intent_id, store), timeout=60.0)
            )
            logger.info(
                "Compensation result for intent %s: deleted=%d failed=%d",
                intent_id, compensate_result.get("deleted", 0), compensate_result.get("failed", 0),
            )
        except Exception as comp_exc:
            logger.exception("Compensation attempt failed for intent %s: %s", intent_id, comp_exc)
        # ── Mark job and intent as failed ──
        await asyncio.shield(_shielded_fail(store, job_id, timeout_message))
        record = store.get(intent_id)
        if record and record.get("status") not in ("success", "failed", "cancelled"):
            store.update(
                intent_id,
                status="failed",
                snapshot={"error_message": f"执行超时（{job_timeout}秒）"},
            )
        _worker_health["error_count"] += 1
        _worker_health["last_error"] = timeout_message
    except Exception as exc:
        logger.exception("Job %s execution failed", job_id)
        failure_message = str(exc) or type(exc).__name__
        await asyncio.shield(_shielded_fail(store, job_id, failure_message))
        _worker_health["error_count"] += 1
        _worker_health["last_error"] = failure_message
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        _worker_health["active_jobs"] -= 1
    return True


async def _shielded_complete(store: IntentStore, job_id: str) -> None:
    """Shielded job completion — never cancelled mid-write."""
    store.complete_job(job_id)


async def _shielded_fail(store: IntentStore, job_id: str, error: str) -> None:
    """Shielded job failure — never cancelled mid-write."""
    store.fail_job(job_id, error)


async def run_worker(poll_seconds: float = 1.0, once: bool = False) -> None:
    """Continuously consume durable jobs with configurable concurrency.

    Set ``IDN_WORKER_CONCURRENCY`` (default 2) to control how many intents
    may be processed in parallel.  When > 1 the worker spawns a background
    task per job up to the limit; each task independently claims and executes.

    The worker auto-restarts on unhandled exceptions (max 3 restarts per 60 s).
    Stale-job recovery runs in a separate loop decoupled from the worker tasks.
    """
    store = get_store()
    recovered = store.recover_running_jobs()
    if recovered:
        logger.warning("Recovered %s interrupted job(s) after worker restart.", recovered)

    concurrency = int(os.getenv("IDN_WORKER_CONCURRENCY", "2"))
    semaphore = asyncio.Semaphore(max(1, concurrency))
    stale_check_interval = float(os.getenv("IDN_STALE_CHECK_INTERVAL_SECONDS", "60"))
    heartbeat_interval = float(os.getenv("IDN_JOB_HEARTBEAT_SECONDS", "30"))

    _worker_health["ready"] = True
    _worker_health["running_since"] = _utc_now()

    if once:
        await process_next_job(store=store)
        _worker_health["ready"] = False
        return

    # ── Concurrent worker loop with per-worker restarts ──────────────────
    _last_restart_window: list[float] = []  # timestamps of recent restarts
    _max_restarts = 3
    _restart_window_sec = 60.0

    async def _concurrent_worker(worker_index: int):
        while True:
            await semaphore.acquire()
            try:
                processed = await process_next_job(store=store)
            except sqlite3.OperationalError as exc:
                logger.warning("SQLite queue claim failed, retrying: %s", exc)
                processed = False
            except Exception:
                logger.exception("Worker %d crashed on process_next_job", worker_index)
                processed = False  # don't crash the loop
            finally:
                semaphore.release()

            if not processed:
                await asyncio.sleep(poll_seconds)

    async def _supervised_worker(worker_index: int) -> None:
        """Run _concurrent_worker with automatic restart on crash."""
        inner_task: asyncio.Task | None = None
        while True:
            inner_task = asyncio.create_task(_concurrent_worker(worker_index))
            try:
                await inner_task
            except asyncio.CancelledError:
                if inner_task is not None:
                    inner_task.cancel()
                raise
            except Exception:
                logger.exception("Worker %d task crashed, restarting...", worker_index)
                # Rate-limit restarts
                now = asyncio.get_event_loop().time()
                _last_restart_window[:] = [t for t in _last_restart_window if now - t < _restart_window_sec]
                _last_restart_window.append(now)
                if len(_last_restart_window) > _max_restarts:
                    logger.critical(
                        "Worker %d restarted %d times in %ds — pausing for 60s",
                        worker_index, _max_restarts, _restart_window_sec,
                    )
                    _worker_health["last_error"] = "too many worker restarts"
                    _worker_health["restart_count"] += _max_restarts
                    await asyncio.sleep(60)
                    _last_restart_window.clear()
                else:
                    _worker_health["restart_count"] += 1
                    _worker_health["last_restart_at"] = _utc_now()
                # Short back-off before restart
                await asyncio.sleep(2)

    # Spawn supervised concurrent workers
    tasks = [
        asyncio.create_task(_supervised_worker(i))
        for i in range(max(1, concurrency))
    ]

    # ── Independent stale-recovery loop ──────────────────────────────────
    async def _stale_recovery_loop():
        while True:
            await asyncio.sleep(stale_check_interval)
            try:
                n = store.recover_stale_jobs()
                if n:
                    logger.info("Stale recovery requeued %d job(s)", n)
            except Exception:
                logger.exception("Stale recovery check failed")

    # ── Approval timeout watcher ────────────────────────────────────────
    async def _approval_timeout_loop():
        approval_timeout = float(os.getenv("IDN_APPROVAL_TIMEOUT_SECONDS", "600"))
        check_interval = min(60.0, approval_timeout / 4)  # check every 1/4 of timeout
        while True:
            await asyncio.sleep(check_interval)
            try:
                resp = store.list(status="pending_approval", page_size=100)
                for item in resp.get("items", []):
                    intent_id = item["intent_id"]
                    approval_ctx = (item.get("snapshot") or {}).get("approval_context") or {}
                    created_at = approval_ctx.get("created_at") or item.get("updated_at") or ""
                    if not created_at:
                        continue
                    try:
                        from datetime import datetime as dt
                        created = dt.fromisoformat(created_at.replace("Z", "+00:00"))
                        age_sec = (dt.now(created.tzinfo) - created).total_seconds()
                        if age_sec > approval_timeout:
                            logger.warning(
                                "Approval timeout for intent %s (age=%.0fs > timeout=%.0fs) — auto-aborting",
                                intent_id, age_sec, approval_timeout,
                            )
                            # Auto-rollback: abort + compensate
                            try:
                                await _compensate_flows(intent_id, store)
                            except Exception:
                                pass
                            store.update(
                                intent_id,
                                status="failed",
                                snapshot={
                                    "error_message": f"审批超时（{approval_timeout}秒），自动中止",
                                    "approval_context": approval_ctx,
                                },
                            )
                            store.add_message(
                                item.get("session_id", ""),
                                "system",
                                "approval_timeout",
                                f"意图 {intent_id} 审批超时（{approval_timeout}秒），已自动中止并清理。",
                                intent_id=intent_id,
                            )
                    except Exception:
                        pass
            except Exception:
                pass

    tasks.append(asyncio.create_task(_stale_recovery_loop()))
    tasks.append(asyncio.create_task(_approval_timeout_loop()))

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        _worker_health["ready"] = False
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the IDN intent worker.")
    parser.add_argument("--once", action="store_true", help="Process at most one queued task.")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.getenv("IDN_WORKER_POLL_SECONDS", "1.0")),
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(poll_seconds=args.poll_seconds, once=args.once))


if __name__ == "__main__":
    main()

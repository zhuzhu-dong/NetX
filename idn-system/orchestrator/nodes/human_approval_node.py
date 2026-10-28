"""
orchestrator/nodes/human_approval_node.py
==========================================

人工审批挂起/恢复节点 (Human-in-the-Loop Approval Node)。

# 系统角色
当自动恢复机制（ReAct 重试）耗尽后，通过 LangGraph 的 interrupt()
机制挂起工作流，等待人工操作员做出决策。

# LangGraph interrupt() 机制
1. 节点调用 interrupt(payload) → LangGraph 暂停图执行
2. Checkpoint 自动保存当前状态（包括 pending interrupt）
3. 前端通过 POST /api/v1/intents/{id}/approve 发送决策
4. API 层调用 graph.ainvoke(Command(resume=decision), config)
5. interrupt() 返回 decision 值，节点继续执行

# 三种审批决策
- approve：继续执行（清除错误，重置重试计数）
- reject：回退到策略编译（重新生成流表计划）
- abort：中止并触发回滚（清理已安装的流表规则）

# See also
- ``POST /api/v1/intents/{id}/approve``
- ``POST /api/v1/intents/{id}/rollback``
- Worker auto-rollback on timeout (``IDN_APPROVAL_TIMEOUT_SECONDS``)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from models.agent_state import (
    ExecutionStage,
    GlobalAgentState,
    StageStatus,
)
from models.intent import IntentStatus

from .base_node import BaseNode

logger = logging.getLogger(__name__)


class HumanApprovalNode(BaseNode):
    """Human-in-the-loop approval gate using LangGraph interrupt().

    When automatic recovery (ReAct) fails after max retries, the graph
    pauses here and waits for a human decision.  The graph resumes when
    the API calls ``graph.ainvoke(Command(resume=...), config)``.

    See also:
      - ``POST /api/v1/intents/{id}/approve``
      - ``POST /api/v1/intents/{id}/rollback``
      - Worker auto-rollback on timeout (``IDN_APPROVAL_TIMEOUT_SECONDS``)
    """

    agent_id = "human_approval_gate"
    stage = ExecutionStage.VERIFY

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        state.current_status = IntentStatus.PENDING_APPROVAL

        # Build structured approval context
        failure_history = [
            {
                "stage": entry.stage.value if hasattr(entry, "stage") else str(entry.get("stage", "")),
                "agent_id": entry.agent_id if hasattr(entry, "agent_id") else str(entry.get("agent_id", "")),
                "status": entry.status.value if hasattr(entry, "status") else str(entry.get("status", "")),
                "summary": entry.output_summary if hasattr(entry, "output_summary") else str(entry.get("output_summary", "")),
                "error": entry.error_message if hasattr(entry, "error_message") else str(entry.get("error_message") or ""),
            }
            for entry in state.execution_log
            if (hasattr(entry, "status") and entry.status == StageStatus.FAILED)
            or (isinstance(entry, dict) and entry.get("status") == "failed")
        ]

        # Collect rollback plan from safety council
        safety_report = state.safety_council_report or {}
        rollback_plan = safety_report.get("rollback_plan", {})
        delete_requests = (
            rollback_plan.get("delete_requests")
            or (state.verified_flow_table_plan or state.flow_table_plan or {}).get("delete_requests")
            or []
        )

        # Build installed flows snapshot for compensation
        installed_flows = (state.execution_result or {}).get("installed_flows", [])

        approval_payload = {
            "type": "human_approval",
            "intent_id": state.intent_id,
            "session_id": state.user_intent.session_id,
            "summary": _build_approval_summary(state),
            "stage": self.stage.value,
            "current_policy_index": state.current_policy_index,
            "total_policies": len(state.yang_policies),
            "retry_count": state.react_state.retry_count,
            "max_retries": state.react_state.max_retries,
            "last_failure_reason": state.react_state.last_failure_reason,
            "last_failure_stage": state.react_state.last_failure_stage,
            "failure_history": failure_history,
            "error_message": state.error_message,
            "risk_score": safety_report.get("overall_risk_score", 50),
            "risk_level": _derive_risk_level(safety_report.get("overall_risk_score")),
            "safety_decision": safety_report.get("overall_decision"),
            "proposed_changes": _build_proposed_changes(state),
            "alternatives": safety_report.get("alternatives") or [],
            "agent_votes": _build_agent_votes(safety_report),
            "rollback_plan": {
                "delete_requests": delete_requests,
                "installed_flows": installed_flows,
                "strategy": safety_report.get("rollback_plan", {}).get("strategy", "删除已安装流规则并恢复基线"),
            },
            "hard_blockers": safety_report.get("hard_blockers") or [],
            "suggested_actions": [
                {"id": "approve", "label": "继续执行", "description": "跳过失败阶段，继续执行后续步骤"},
                {"id": "reject", "label": "重新编译", "description": "回退到策略编译阶段，使用不同参数重试"},
                {"id": "abort", "label": "中止并清理", "description": "停止执行并清理已安装的流表规则"},
            ],
            "timeout_seconds": int(
                __import__("os").getenv("IDN_APPROVAL_TIMEOUT_SECONDS", "600")
            ),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        logger.warning(
            "[Intent %s] 工作流已挂起，等待人工审批。"
            "失败原因: %s | retry_count=%s",
            state.intent_id,
            state.react_state.last_failure_reason,
            state.react_state.retry_count,
        )

        # ── Persist approval context to API store for frontend ──
        try:
            from api.store import get_current_store
            store = get_current_store()
            if store:
                existing = store.get(state.intent_id)
                if existing:
                    existing_snapshot = existing.get("snapshot") or {}
                    existing_snapshot["approval_context"] = approval_payload
                    store.update(state.intent_id, snapshot=existing_snapshot)

                # ── Emit approval message to conversation stream ──
                session_id = state.user_intent.session_id
                store.add_message(
                    session_id,
                    "system",
                    "approval_required",
                    approval_payload.get("summary", "需要人工审批"),
                    intent_id=state.intent_id,
                    payload=approval_payload,
                )
        except Exception:
            pass

        # ── Pause the graph — LangGraph checkpoint is written automatically ──
        from langgraph.types import interrupt

        decision = interrupt(approval_payload)

        # ── Resume: decision is the value passed via Command(resume=...) ──
        decision_type = decision.get("decision", "abort") if isinstance(decision, dict) else "abort"
        reason = decision.get("reason", "") if isinstance(decision, dict) else ""

        logger.info(
            "[Intent %s] 人工审批结果: decision=%s reason=%s",
            state.intent_id, decision_type, reason,
        )

        # Store the decision on state for downstream routing
        state.approval_decision = decision_type

        if decision_type == "approve":
            # Continue — clear error and progress to execute_flow or verify_status
            state.error_message = None
            state.react_state.retry_count = 0  # reset for fresh attempt
            return self._end_log(
                state, start, StageStatus.SUCCESS,
                f"人工审批通过 (decision=approve){': ' + reason if reason else ''}，继续执行",
            )
        elif decision_type == "reject":
            # Go back to policy compilation for a fresh attempt
            state.error_message = f"人工审批拒绝，重新编译策略: {reason}" if reason else "人工审批拒绝，重新编译策略"
            state.react_state.retry_count = 0
            state.flow_table_plan = None
            state.rule_verification_report = None
            state.verified_flow_table_plan = None
            state.safety_council_report = None
            return self._end_log(
                state, start, StageStatus.FAILED,
                f"人工审批拒绝 (decision=reject){': ' + reason if reason else ''}，回退至策略编译",
                error=state.error_message,
            )
        else:  # abort
            state.error_message = f"人工审批中止: {reason}" if reason else "人工审批中止"
            # Trigger rollback via the existing compensation path
            rollback_plan = {
                "status": "ROLLBACK",
                "reason": state.error_message,
                "installed_flows": installed_flows,
            }
            state.execution_result = rollback_plan
            return self._end_log(
                state, start, StageStatus.FAILED,
                f"人工审批中止 (decision=abort){': ' + reason if reason else ''}，执行清理",
                error=state.error_message,
            )


# ── Approval context helpers ──────────────────────────────────────────────

def _build_approval_summary(state: GlobalAgentState) -> str:
    """Build a one-line summary describing what needs approval."""
    safety = state.safety_council_report or {}
    if safety.get("overall_decision") != "approve":
        blockers = safety.get("hard_blockers") or []
        if blockers:
            return f"安全评审未通过：{'; '.join(blockers[:2])}"
        return f"安全评审: {safety.get('overall_decision', 'unknown')} (risk={safety.get('overall_risk_score', '?')})"

    if state.react_state.last_failure_reason:
        return f"ReAct 重试 {state.react_state.retry_count}/{state.react_state.max_retries} 次后仍失败: {state.react_state.last_failure_reason[:120]}"

    if state.error_message:
        return f"执行异常: {state.error_message[:120]}"

    return f"意图 {state.intent_id[-8:]} 需要人工审批（策略 {state.current_policy_index + 1}/{len(state.yang_policies)}）"


def _derive_risk_level(risk_score: Any) -> str:
    """Derive a risk level label from a numeric score."""
    score = risk_score if isinstance(risk_score, (int, float)) else 50
    if score >= 70:
        return "critical"
    elif score >= 50:
        return "high"
    elif score >= 30:
        return "medium"
    return "low"


def _build_proposed_changes(state: GlobalAgentState) -> list[dict]:
    """Build a list of proposed changes from the flow table plan."""
    flow_plan = state.verified_flow_table_plan or state.flow_table_plan or {}
    changes = []
    for request in flow_plan.get("write_requests") or []:
        changes.append({
            "device": request.get("device_id", "unknown"),
            "table": request.get("table_name", "unknown"),
            "entries": len(request.get("table_entries") or []),
            "action": ", ".join(
                set(e.get("action_name", "") for e in (request.get("table_entries") or []))
            ) or "unknown",
        })
    if not changes:
        # Fallback: describe from yang_draft
        yang = state.yang_draft
        if yang:
            policy = yang.network_policy if hasattr(yang, "network_policy") else {}
            if hasattr(policy, "model_dump"):
                policy = policy.model_dump(mode="json")
            changes.append({
                "device": "all",
                "table": "ipv4_routing / sla_enforcement",
                "entries": 1,
                "action": (policy.get("action") or {}).get("action_type", "unknown"),
            })
    return changes


def _build_agent_votes(safety_report: dict) -> list[dict]:
    """Extract per-agent votes from the safety council report."""
    votes = []
    for review in safety_report.get("agent_reviews") or safety_report.get("votes") or []:
        agent_id = review.get("agent_id") or review.get("agent") or "unknown"
        votes.append({
            "agent_id": agent_id,
            "vote": review.get("decision") or review.get("vote") or "unknown",
            "risk_score": review.get("risk_score", 50),
            "reason": review.get("reason") or review.get("rationale") or "",
        })
    if not votes and safety_report.get("overall_decision"):
        votes.append({
            "agent_id": "safety_council",
            "vote": safety_report.get("overall_decision"),
            "risk_score": safety_report.get("overall_risk_score", 50),
            "reason": safety_report.get("final_rationale", ""),
        })
    return votes

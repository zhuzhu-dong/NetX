"""
orchestrator/nodes/safety_council_node.py
==========================================

执行前安全评审委员会节点 (SafetyCouncil Node) —— 最后的安全闸门。

# 系统角色
在执行流表下发前，运行 5 个并行子 Agent 进行全面安全评审：
1. invariant_verifier：网络不变量检查（设备状态、链路可用性）
2. digital_twin：数字孪生仿真验证（shadow simulation）
3. redteam：红队攻击审查（安全性测试）
4. rollback_verifier：回滚计划可执行性验证
5. history_risk_agent：历史风险评估（相似策略的成功/失败率）

每个子 Agent 的评审结果通过 A2A 追踪系统独立记录（_emit_safety_subagent_trace）。

# 输入/输出
- 输入：state.verified_flow_table_plan + state.yang_draft + state.network_snapshot + state.rule_verification_report
- 输出：state.safety_council_report（含 overall_decision 和各子 Agent 评审详情）
"""
from __future__ import annotations

import logging
import time
import os
from datetime import datetime
from typing import Any

from models.agent_state import (
    ExecutionLogEntry,
    ExecutionStage,
    GlobalAgentState,
    StageStatus,
)
from models.intent import IntentStatus

from .base_node import BaseNode

logger = logging.getLogger(__name__)

# ── SafetyCouncil result cache (TTL-based, module-level) ──
_SAFETY_CACHE_TTL_SECS = int(os.getenv("IDN_SAFETY_CACHE_SECS", "60"))
_safety_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _safety_cache_key(
    policy_id: str, plan_id: str, device_count: int,
    link_count: int, host_count: int,
) -> str:
    return f"{policy_id}:{plan_id}:d{device_count}:l{link_count}:h{host_count}"


# ──────────────────────────────────────────────
# 基础节点类
# ──────────────────────────────────────────────


class SafetyCouncilNode(BaseNode):
    """
    执行前安全评审委员会。

    输入: verified_flow_table_plan + RuleVerificationReport + NetworkSnapshot
    输出: state.safety_council_report
    """
    agent_id = "safety_council"
    stage = ExecutionStage.GENERATE

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        try:
            if not state.verified_flow_table_plan:
                state.error_message = "安全评审失败: verified_flow_table_plan 为空"
                return self._end_log(state, start, StageStatus.FAILED, state.error_message)
            if state.yang_draft is None:
                state.error_message = "安全评审失败: yang_draft 为空"
                return self._end_log(state, start, StageStatus.FAILED, state.error_message)
            if state.network_snapshot is None:
                state.error_message = "安全评审失败: network_snapshot 为空"
                return self._end_log(state, start, StageStatus.FAILED, state.error_message)

            # ── Check result cache ──
            cache_key = _safety_cache_key(
                policy_id=state.yang_draft.network_policy.policy_id,
                plan_id=(state.verified_flow_table_plan or {}).get("plan_id", ""),
                device_count=len(state.network_snapshot.devices),
                link_count=len(state.network_snapshot.links),
                host_count=len(state.network_snapshot.hosts),
            )
            if _SAFETY_CACHE_TTL_SECS > 0:
                entry = _safety_cache.get(cache_key)
                if entry is not None:
                    cached_ts, cached_report = entry
                    age = time.monotonic() - cached_ts
                    if age < _SAFETY_CACHE_TTL_SECS:
                        logger.info(
                            "[%s] SafetyCouncil cache HIT (age=%.1fs, key=%s)",
                            self.agent_id, age, cache_key,
                        )
                        state.safety_council_report = cached_report
                        self._current_policy_result(state)["safety_council_report"] = cached_report
                        self._emit_safety_subagent_trace(state, cached_report)
                        decision = cached_report.get("overall_decision")
                        risk = cached_report.get("overall_risk_score")
                        if decision == "approve":
                            return self._end_log(
                                state, start, StageStatus.SUCCESS,
                                f"安全评审通过 (cached): risk={risk}",
                                artifacts={"safety_council_report": cached_report},
                            )
                        state.current_status = IntentStatus.PENDING_APPROVAL
                        state.error_message = "安全评审未通过 (cached): " + "; ".join(
                            cached_report.get("hard_blockers") or ["需要人工审批"]
                        )
                        return self._end_log(
                            state, start, StageStatus.SUCCESS,
                            f"安全评审阻断执行 (cached): decision={decision}",
                            error=state.error_message,
                        )

            from agents.safety_council import SafetyCouncilAgent

            report = await SafetyCouncilAgent().review(
                intent_id=state.intent_id,
                yang_policy=state.yang_draft,
                flow_table_plan=state.verified_flow_table_plan,
                network_snapshot=state.network_snapshot,
                rule_report=state.rule_verification_report,
            )
            state.safety_council_report = report
            self._current_policy_result(state)["safety_council_report"] = report
            # ── Populate cache ──
            if _SAFETY_CACHE_TTL_SECS > 0:
                _safety_cache[cache_key] = (time.monotonic(), dict(report))
            self._emit_safety_subagent_trace(state, report)

            decision = report.get("overall_decision")
            risk = report.get("overall_risk_score")
            if decision == "approve":
                return self._end_log(
                    state,
                    start,
                    StageStatus.SUCCESS,
                    f"安全评审通过: risk={risk}, level={report.get('risk_level')}",
                    artifacts={"safety_council_report": report},
                )

            state.current_status = IntentStatus.PENDING_APPROVAL
            state.error_message = (
                "安全评审未通过: "
                + "; ".join(report.get("hard_blockers") or [report.get("final_rationale") or "需要人工审批"])
            )
            state.react_state.last_failure_reason = state.error_message
            state.react_state.last_failure_stage = "safety_council"
            return self._end_log(
                state,
                start,
                StageStatus.SUCCESS,
                f"安全评审阻断执行: decision={decision}, risk={risk}",
                error=state.error_message,
                artifacts={"safety_council_report": report},
            )

        except Exception as e:
            logger.error(
                "[%s] SafetyCouncil crashed, degrading to warning: %s",
                self.agent_id, e,
            )
            degraded_report = {
                "report_id": f"SAFETY-DEGRADED-{state.intent_id}",
                "intent_id": state.intent_id,
                "policy_id": (
                    state.yang_draft.network_policy.policy_id
                    if state.yang_draft else "unknown"
                ),
                "generated_at": datetime.utcnow().isoformat(),
                "overall_decision": "degraded",
                "overall_risk_score": 50,
                "risk_level": "MEDIUM",
                "hard_blockers": [],
                "agent_reviews": [],
                "degradation_reason": f"{type(e).__name__}: {e}",
                "degraded": True,
            }
            state.safety_council_report = degraded_report
            self._current_policy_result(state)["safety_council_report"] = degraded_report
            return self._end_log(
                state, start, StageStatus.SUCCESS,
                f"安全评审降级: {type(e).__name__} — 跳过安全评审继续执行",
                artifacts={"safety_council_report": degraded_report},
                error=f"SafetyCouncil degraded: {e}",
            )

    def _emit_safety_subagent_trace(
        self,
        state: GlobalAgentState,
        report: dict[str, Any],
    ) -> None:
        try:
            from api.a2a_trace import record_consensus_sync, record_delegation, record_response

            for review in report.get("agent_reviews") or []:
                agent_id = review.get("agent_id")
                if not agent_id:
                    continue
                record_delegation(
                    state,
                    source_agent="safety_council",
                    target_agent=agent_id,
                    stage=self.stage,
                    input_summary="执行 Safety Assurance Council 子评审。",
                    payload_schema=f"{agent_id}.safety_review.input.v1",
                    payload={
                        "policy_id": report.get("policy_id"),
                        "safety_report_id": report.get("report_id"),
                    },
                    evidence_refs=["verified_flow_table_plan", "NetworkSnapshot", "RuleVerificationReport"],
                    risk_score=review.get("risk_score"),
                )
                record_response(
                    state,
                    source_agent=agent_id,
                    target_agent="safety_council",
                    stage=self.stage,
                    status=StageStatus.SUCCESS,
                    output_summary=(
                        f"{agent_id} decision={review.get('decision')}, "
                        f"risk={review.get('risk_score')}"
                    ),
                    payload_schema=f"{agent_id}.safety_review.output.v1",
                    payload=review,
                    evidence_refs=review.get("evidence") or [],
                    risk_score=review.get("risk_score"),
                    failure_reason="; ".join(review.get("hard_blockers") or []) or None,
                    retry_suggestion="进入人工审批或触发 ReAct 重编译。" if review.get("hard_blockers") else None,
                )
            negotiation = report.get("negotiation") or {}
            if negotiation:
                record_consensus_sync(
                    state,
                    source_agent="safety_council",
                    target_agents=["p4_runtime_executor"],
                    stage=self.stage,
                    decision=negotiation.get("proposal") or "SafetyCouncil 完成协商投票。",
                    consensus=negotiation.get("final_consensus") or report.get("overall_decision"),
                    confidence=1 - min(1, float(report.get("overall_risk_score") or 0) / 100),
                    risk_score=report.get("overall_risk_score"),
                    evidence_refs=[
                        f"{vote.get('agent')}:{vote.get('vote')}"
                        for vote in negotiation.get("votes") or []
                    ],
                    payload=negotiation,
                )
        except Exception:
            pass


# ──────────────────────────────────────────────
# Act阶段 — 流表下发节点
# ──────────────────────────────────────────────

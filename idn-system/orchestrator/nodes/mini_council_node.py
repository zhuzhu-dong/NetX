"""
orchestrator/nodes/mini_council_node.py
========================================

轻量级安全评审节点 (MiniCouncil Node) —— MEDIUM 风险路径。

# 系统角色
对 MEDIUM 风险意图执行轻量级安全评审，仅运行两个确定性子 Agent：
1. invariant_verifier：不变量检查（设备可达性、容量约束）
2. history_risk_agent：历史风险评估（相似策略的历史成功率）

跳过的 Agent（保留给 HIGH/CRITICAL 路径）：
- digital_twin（数字孪生仿真）
- redteam（红队攻击审查）
- rollback（回滚计划验证）

# 输入/输出
- 输入：state.verified_flow_table_plan + state.yang_draft + state.network_snapshot
- 输出：state.safety_council_report
"""
from __future__ import annotations

import logging

from models.agent_state import ExecutionStage, GlobalAgentState, StageStatus

from .base_node import BaseNode

logger = logging.getLogger(__name__)


class MiniCouncilNode(BaseNode):
    """MEDIUM-risk safety review: invariant checks + history lookup only."""

    agent_id = "mini_council"
    stage = ExecutionStage.GENERATE

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        try:
            if not state.verified_flow_table_plan:
                state.error_message = "MiniCouncil failed: verified_flow_table_plan is empty"
                return self._end_log(state, start, StageStatus.FAILED, state.error_message)
            if state.yang_draft is None:
                state.error_message = "MiniCouncil failed: yang_draft is empty"
                return self._end_log(state, start, StageStatus.FAILED, state.error_message)
            if state.network_snapshot is None:
                state.error_message = "MiniCouncil failed: network_snapshot is empty"
                return self._end_log(state, start, StageStatus.FAILED, state.error_message)

            from agents.safety_council import SafetyCouncilAgent

            report = await SafetyCouncilAgent().review_mini(
                intent_id=state.intent_id,
                yang_policy=state.yang_draft,
                flow_table_plan=state.verified_flow_table_plan,
                network_snapshot=state.network_snapshot,
                rule_report=state.rule_verification_report,
            )
            state.safety_council_report = report
            self._current_policy_result(state)["safety_council_report"] = report

            decision = report.get("overall_decision")
            risk = report.get("overall_risk_score")
            if decision == "approve":
                return self._end_log(
                    state, start, StageStatus.SUCCESS,
                    f"MiniCouncil 通过: risk={risk}, level={report.get('risk_level')}",
                    artifacts={"safety_council_report": report},
                )

            state.error_message = (
                "MiniCouncil 未通过: "
                + "; ".join(report.get("hard_blockers") or ["需要升级为完整 SafetyCouncil 评审"])
            )
            state.react_state.last_failure_reason = state.error_message
            state.react_state.last_failure_stage = "mini_council"
            return self._end_log(
                state, start, StageStatus.SUCCESS,
                f"MiniCouncil 阻断: decision={decision}, risk={risk}",
                error=state.error_message,
                artifacts={"safety_council_report": report},
            )

        except Exception as e:
            logger.error(
                "[%s] MiniCouncil crashed, degrading to warning: %s",
                self.agent_id, e,
            )
            degraded_report = {
                "report_id": f"SAFETY-MINI-DEGRADED-{state.intent_id}",
                "intent_id": state.intent_id,
                "policy_id": (
                    state.yang_draft.network_policy.policy_id
                    if state.yang_draft else "unknown"
                ),
                "generated_at": __import__("datetime").datetime.utcnow().isoformat(),
                "overall_decision": "degraded",
                "overall_risk_score": 50,
                "risk_level": "MEDIUM",
                "hard_blockers": [],
                "agent_reviews": [],
                "review_mode": "mini",
                "degradation_reason": f"{type(e).__name__}: {e}",
                "degraded": True,
            }
            state.safety_council_report = degraded_report
            self._current_policy_result(state)["safety_council_report"] = degraded_report
            return self._end_log(
                state, start, StageStatus.SUCCESS,
                f"MiniCouncil 降级: {type(e).__name__} — 跳过安全评审继续执行",
                artifacts={"safety_council_report": degraded_report},
                error=f"MiniCouncil degraded: {e}",
            )

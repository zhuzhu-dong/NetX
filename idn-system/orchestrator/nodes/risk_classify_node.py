"""
orchestrator/nodes/risk_classify_node.py
========================================

风险分级节点 (Risk Classify Node) —— v2.0 分级安全评审入口。

# 系统角色
位于 P4 规则验证之后、安全评审之前，评估意图执行的风险等级：
- LOW：跳过安全评审，直接下发（如测试网络 FORWARD 操作）
- MEDIUM：轻量级 MiniCouncil（仅不变量 + 历史检查）
- HIGH：完整 5-Agent SafetyCouncil（默认等级）
- CRITICAL：完整 SafetyCouncil + 强制人工审批

# 输入/输出
- 输入：state.yang_draft + state.verified_flow_table_plan + state.network_snapshot
- 输出：state.risk_level（"LOW" | "MEDIUM" | "HIGH" | "CRITICAL"）

# 风险判断维度
RiskClassifier 综合考虑：动作类型（DROP > RATE_LIMIT > SET_PRIORITY > FORWARD）、
受影响设备数、策略优先级、网络状态等因素。
"""
from __future__ import annotations

import logging
import os
from typing import Literal

from models.agent_state import ExecutionStage, GlobalAgentState, StageStatus

from .base_node import BaseNode

logger = logging.getLogger(__name__)


class RiskClassifyNode(BaseNode):
    """Evaluate intent risk level and decide safety review depth."""

    agent_id = "risk_classifier"
    stage = ExecutionStage.GENERATE  # logically part of the Act preparation phase

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)

        # ── Allow operators to force full SafetyCouncil regardless of risk ──
        force_mode = os.getenv("IDN_SAFETY_COUNCIL_MODE", "").lower()
        if force_mode == "full":
            state.risk_level = "HIGH"
            return self._end_log(
                state, start, StageStatus.SUCCESS,
                "SafetyCouncil mode forced to FULL via IDN_SAFETY_COUNCIL_MODE=full",
            )

        try:
            from agents.risk_classifier import classify_risk

            if state.yang_draft is None:
                state.risk_level = "HIGH"
                return self._end_log(
                    state, start, StageStatus.SUCCESS,
                    "yang_draft 为空，默认 HIGH 风险 → 完整 SafetyCouncil",
                )

            plan = state.verified_flow_table_plan or state.flow_table_plan or {}
            risk = classify_risk(
                yang_policy=state.yang_draft,
                flow_table_plan=plan,
                network_snapshot=state.network_snapshot,
            )
            state.risk_level = risk

            risk_descriptions = {
                "LOW": "跳过 SafetyCouncil，直接进入执行",
                "MEDIUM": "MiniCouncil (invariant + history)",
                "HIGH": "完整 SafetyCouncil (5 专家评审)",
                "CRITICAL": "完整 SafetyCouncil + 强制人工审批",
            }
            return self._end_log(
                state, start, StageStatus.SUCCESS,
                f"风险等级={risk}: {risk_descriptions.get(risk, risk)}",
            )
        except Exception as exc:
            logger.warning("[%s] 风险分类失败，默认 HIGH: %s", self.agent_id, exc)
            state.risk_level = "HIGH"
            return self._end_log(
                state, start, StageStatus.SUCCESS,
                f"风险分类异常，默认 HIGH → 完整 SafetyCouncil",
                error=str(exc),
            )

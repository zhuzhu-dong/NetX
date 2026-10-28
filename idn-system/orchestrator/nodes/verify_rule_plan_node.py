"""
orchestrator/nodes/verify_rule_plan_node.py
===========================================

P4 表项级验证节点 (Verify Rule Plan Node) —— 策略编译后的质量闸门。

# 系统角色
调用 P4RuleVerifierAgent 验证 PolicyCompiler 生成的流表项：
- P4Info schema 一致性（字段名、匹配类型、动作参数是否正确）
- 资源约束（TCAM/SRAM 容量是否充足）
- 运行时规则无冲突（不与已安装规则重叠，包括同意图前序策略已下发的规则）
- 审批令牌校验（require_approval_token=True 时强制验证）

验证失败时：
- 递增 react_state.retry_count（触发 ReAct 回溯到 generate_policy）
- 设置 react_state.last_failure_reason（供 PolicyCompiler 调整策略）

# 输入/输出
- 输入：state.flow_table_plan + P4Info + runtime flows + approval_token
- 输出：state.rule_verification_report, state.verified_flow_table_plan
- 必填字段校验：flow_table_plan
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


# ──────────────────────────────────────────────
# 基础节点类
# ──────────────────────────────────────────────


class VerifyRulePlanNode(BaseNode):
    """
    策略生成后表项级验证节点。

    输入: state.flow_table_plan + P4Info + runtime flows + approval_token
    输出: state.rule_verification_report
    """
    agent_id = "p4_rule_verifier"
    stage = ExecutionStage.GENERATE
    _required_fields = ("flow_table_plan",)

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        try:
            if not state.flow_table_plan:
                state.error_message = "P4 表项级验证失败: flow_table_plan 为空"
                return self._end_log(state, start, StageStatus.FAILED, state.error_message)

            from agents.p4_rule_verifier import P4RuleVerifierAgent
            from agents.policy_compiler import PolicyCompilerAgent
            from models.flow_table import FlowTablePlan

            plan = FlowTablePlan(**state.flow_table_plan)
            p4info = await PolicyCompilerAgent().load_p4info(state.network_snapshot)
            existing_runtime_rules = [
                flow.model_dump() if hasattr(flow, "model_dump") else dict(flow)
                for flow in (state.network_snapshot.flows if state.network_snapshot else [])
            ]
            # A later policy must also see flow entries successfully written by
            # earlier policies in this same multi-policy graph run.
            for prior_result in state.policy_results[:state.current_policy_index]:
                if (prior_result.get("execution_result") or {}).get("status") != "SUCCESS":
                    continue
                previous_plan = (
                    prior_result.get("verified_flow_table_plan")
                    or prior_result.get("flow_table_plan")
                    or {}
                )
                existing_runtime_rules.extend(previous_plan.get("write_requests") or [])

            report = await P4RuleVerifierAgent().verify(
                flow_table_plan=plan,
                p4info=p4info,
                existing_runtime_rules=existing_runtime_rules,
                approval_token=state.conflict_report.approval_token if state.conflict_report else None,
                require_approval_token=True,
            )
            state.rule_verification_report = report
            policy_result = self._current_policy_result(state)
            policy_result["rule_verification_report"] = report.model_dump(mode="json")

            if not report.is_valid:
                state.verified_flow_table_plan = None
                state.react_state.retry_count += 1
                state.react_state.last_failure_stage = self.agent_id
                state.react_state.last_failure_reason = report.explanation
                logger.warning(f"[{self.agent_id}] P4 表项级验证未通过: {report.explanation}")
                return self._end_log(
                    state,
                    start,
                    StageStatus.SUCCESS,
                    f"P4 表项级验证未通过: {report.violation_type}",
                    artifacts={"report": report.model_dump()},
                )

            # Only a verified plan is allowed to enter P4RuntimeExecutor.
            state.verified_flow_table_plan = state.flow_table_plan
            policy_result["verified_flow_table_plan"] = state.verified_flow_table_plan
            return self._end_log(
                state,
                start,
                StageStatus.SUCCESS,
                f"P4 表项级验证通过: checked_items={report.checked_items}",
                artifacts={"report": report.model_dump()},
            )

        except Exception as e:
            state.error_message = f"P4 表项级验证异常: {str(e)}"
            logger.error(f"[{self.agent_id}] {state.error_message}")
            return self._end_log(state, start, StageStatus.FAILED, "P4 表项级验证异常", error=str(e))


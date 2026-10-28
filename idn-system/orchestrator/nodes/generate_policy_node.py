"""
orchestrator/nodes/generate_policy_node.py
==========================================

策略编译节点 (Generate Policy Node) —— STAV 闭环的第四阶段。

# 系统角色
调用 PolicyCompilerAgent，基于 YANG 策略 + 网络拓扑 + P4Info
生成可下发的 P4Runtime FlowTablePlan。

支持 ReAct 重编译：当 react_context 非 None 时，
PolicyCompiler 会根据上次失败原因调整策略
（如降低优先级、使用备用匹配字段、启用确定性 fallback）。

# 输入/输出
- 输入：state.yang_draft + state.network_snapshot + state.react_state
- 输出：state.flow_table_plan（FlowTablePlan JSON dict）
- 必填字段校验：yang_draft, network_snapshot
"""

from __future__ import annotations

import logging
import os
import re
import time
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

_MAC_HEX_RE = re.compile(r"^[0-9a-fA-F]{12}$")


def _check_mac_formats(plan_dict: dict) -> list[dict]:
    """检查流表计划中 action_params 的 MAC 地址格式。

    P4Runtime 要求 MAC 地址为 12 字符纯十六进制（无冒号/短横线）。
    本函数对所有 write_requests 中的 action_params 做预检，
    发现非法格式时返回错误列表。
    """
    errors = []
    for req in plan_dict.get("write_requests") or []:
        for entry in req.get("table_entries") or []:
            for param in entry.get("action_params") or []:
                name = str(param.get("param_name") or param.get("name") or "").lower()
                value = str(param.get("value") or "")
                if ("mac" in name) and value:
                    cleaned = value.replace(":", "").replace("-", "").lower()
                    if not _MAC_HEX_RE.match(cleaned):
                        errors.append(
                            {
                                "entry_id": entry.get("entry_id", "?"),
                                "param_name": name,
                                "value": value,
                                "hint": "MAC must be 12 hex chars, no colons/dashes",
                            }
                        )
    return errors


# ──────────────────────────────────────────────
# 基础节点类
# ──────────────────────────────────────────────


class GeneratePolicyNode(BaseNode):
    """
    策略编译节点 (Policy Compiler)
    职责: 调用 PolicyCompilerAgent，基于YANG策略 + 网络拓扑 + P4Info
          生成可下发的P4Runtime流表计划。

    输入: state.yang_draft + state.network_snapshot
    输出: state.flow_table_plan (FlowTablePlan.model_dump())
    """

    agent_id = "policy_compiler"
    stage = ExecutionStage.GENERATE
    _required_fields = ("yang_draft", "network_snapshot")

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        admission = (state.conflict_report.admission_decision if state.conflict_report else {}) or {}
        if admission and admission.get("decision") != "ADMITTED":
            state.current_status = IntentStatus.UNSATISFIABLE
            state.flow_table_plan = {
                "status": "NOT_RUN", "reason_code": admission.get("reason_code"),
                "admission_decision": admission,
            }
            self._current_policy_result(state)["flow_table_plan"] = state.flow_table_plan
            return self._end_log(
                state, start, StageStatus.SKIPPED,
                f"POLICY_COMPILER_NOT_RUN: {admission.get('reason_code', 'ADMISSION_REJECTED')}",
            )
        state.current_status = IntentStatus.ACTING
        try:
            # YANGPolicy -> FlowTablePlan，名字用 Compiler 避免和建模阶段混淆。
            from agents.policy_compiler import PolicyCompilerAgent

            agent = PolicyCompilerAgent(llm=self.llm)
            react_context = None
            if state.react_state.retry_count:
                react_context = {
                    "retry_count": state.react_state.retry_count,
                    "last_failure_stage": state.react_state.last_failure_stage,
                    "last_failure_reason": state.react_state.last_failure_reason,
                    "rule_verification_report": (
                        state.rule_verification_report.model_dump(mode="json")
                        if state.rule_verification_report
                        else None
                    ),
                    "verification_report": state.verification_report,
                }
            plan = await agent.compile(
                yang_draft=state.yang_draft,
                network_snapshot=state.network_snapshot,
                react_context=react_context,
                # P4Info 不传时，由 PolicyCompilerAgent 通过 P4Info 工具获取。
            )

            # Conflict checking has already decided whether this exact scope
            # is a create, an idempotent reuse, or an in-place QoS update.
            # Carry that decision into execution; bandwidth-only changes do
            # not need a duplicate P4 table entry because TC owns that
            # concrete resource.
            report = state.conflict_report
            if report is not None:
                plan.policy_operation = (
                    "VERIFY_EXISTING"
                    if report.execution_mode == "VERIFY_EXISTING"
                    else (
                        "MODIFY_QOS_RESOURCE"
                        if report.execution_mode == "MODIFY_QOS_RESOURCE"
                        else "CREATE"
                    )
                )
                plan.traffic_scope_key = report.traffic_scope_key
                plan.existing_policy_id = report.existing_policy_id
                plan.policy_change = {
                    "resolution": report.resolution,
                    "old_qos": report.old_qos,
                    "new_qos": report.new_qos,
                    "changed_fields": list(report.changed_fields),
                    "old_bandwidth_mbps": report.old_bandwidth_mbps,
                    "new_bandwidth_mbps": report.new_bandwidth_mbps,
                    "change_direction": report.change_direction,
                    "released_bandwidth_mbps": report.released_bandwidth_mbps,
                }
                # Keep the compiled classifier as a repair candidate.  The
                # executor reads the controller-owned flow first and drops
                # these writes only when the old classifier is still ADDED
                # with the exact selector/treatment.  This is essential after
                # an ONOS restart or when migrating a legacy host-TC record.

            # ── 写入状态 ──
            state.flow_table_plan = plan.model_dump()
            plan_dict = plan.model_dump()
            plan_dict["total_entries"] = plan.total_entries
            plan_dict["affected_devices"] = plan.affected_devices
            state.flow_table_plan = plan_dict

            # ── 预检 MAC 格式：避免因 LLM 生成非法 MAC 触发 ReAct 重试风暴 ──
            _mac_errs = _check_mac_formats(plan_dict)
            if (
                _mac_errs
                and state.react_state.retry_count < state.react_state.max_retries
            ):
                logger.warning(
                    "[%s] MAC 格式预检: %d 个错误, 触发内联重编译",
                    self.agent_id,
                    len(_mac_errs),
                )
                _mac_ctx = {
                    "retry_count": state.react_state.retry_count,
                    "last_failure_stage": "pre_flight_mac_check",
                    "last_failure_reason": (
                        f"MAC 地址格式错误 ({len(_mac_errs)} 处): "
                        f"MAC 必须为 12 位十六进制字符，不含冒号/短横线。 "
                        + "; ".join(
                            f"{e['param_name']}={e['value']}" for e in _mac_errs[:3]
                        )
                    ),
                }
                try:
                    plan = await agent.compile(
                        yang_draft=state.yang_draft,
                        network_snapshot=state.network_snapshot,
                        react_context=_mac_ctx,
                    )
                    plan_dict = plan.model_dump()
                    plan_dict["total_entries"] = plan.total_entries
                    plan_dict["affected_devices"] = plan.affected_devices
                    state.flow_table_plan = plan_dict
                    _mac_errs2 = _check_mac_formats(plan_dict)
                    if _mac_errs2:
                        logger.warning(
                            "[%s] MAC 格式错误仍存在 (%d 处), 交由 P4 验证器处理",
                            self.agent_id,
                            len(_mac_errs2),
                        )
                    else:
                        logger.info("[%s] MAC 格式已修正。", self.agent_id)
                except Exception as _exc:
                    logger.warning(
                        "[%s] MAC 重编译失败: %s, 使用原始计划", self.agent_id, _exc
                    )

            self._current_policy_result(state)["flow_table_plan"] = plan.model_dump(
                mode="json"
            )
            if plan.react_adjustments:
                state.react_state.adjustment_history.append(
                    {
                        "retry_count": state.react_state.retry_count,
                        "policy_index": state.current_policy_index,
                        "adjustments": list(plan.react_adjustments),
                    }
                )

            # ── 日志 ──
            total_entries = state.flow_table_plan.get("total_entries", 0)
            affected = state.flow_table_plan.get("affected_devices", [])
            logger.info(
                f"[{self.agent_id}] 流表计划生成完成: "
                f"{total_entries}条表项, 涉及设备: {affected}"
            )

            return self._end_log(
                state,
                start,
                StageStatus.SUCCESS,
                f"流表计划生成完成: {total_entries}条表项, " f"涉及设备: {affected}",
            )

        except Exception as e:
            state.error_message = f"策略生成失败: {str(e)}"
            logger.error(f"[{self.agent_id}] {state.error_message}")
            return self._end_log(
                state, start, StageStatus.FAILED, "策略生成失败", error=str(e)
            )

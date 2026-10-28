"""
orchestrator/nodes/check_conflict_node.py
=========================================

策略冲突检测节点 (Check Conflict Node) —— STAV 闭环的第三阶段。

# 系统角色
调用 PolicyConflictCheckerAgent 进行三层校验：
1. 逻辑冲突：新策略规则与 ONOS 现有流表是否重叠/矛盾
2. 资源冲突：带宽等资源是否超出物理链路限制
3. 拓扑兼容：IP 段/端口是否与当前拓扑匹配

冲突检测发生在策略编译之前，避免浪费 LLM 编译成本。

# 输入/输出
- 输入：state.yang_draft + state.network_snapshot
- 输出：state.conflict_report（含 approval_token 或 rejection_reason）
- 必填字段校验：yang_draft, network_snapshot
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any

from models.agent_state import (
    ConsensusReasonCode,
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


class CheckConflictNode(BaseNode):
    """
    策略级冲突检测节点 (Policy Conflict Checker)
    职责: 从ONOS获取现有流表 + 调用PolicyConflictCheckerAgent进行三层校验
          1. 逻辑冲突: 与现有策略规则是否重叠/矛盾
          2. 资源冲突: 带宽等资源是否超出物理限制
          3. 拓扑兼容: IP段/端口是否与当前拓扑匹配

    输入: state.yang_draft + state.network_snapshot
    输出: state.conflict_report (含审批令牌或拒绝原因)
    """

    agent_id = "policy_conflict_checker"
    stage = ExecutionStage.CHECK
    _required_fields = ("yang_draft", "network_snapshot")

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        try:
            existing_flows = (
                state.network_snapshot.flows if state.network_snapshot else []
            )
            logger.info(
                f"[{self.agent_id}] 从ONOS获取到 {len(existing_flows)} 条现有流表"
            )

            # YANGPolicy 层冲突检测，发生在流表编译之前。
            from agents.policy_conflict_checker import PolicyConflictCheckerAgent

            checker = PolicyConflictCheckerAgent(llm=self.llm)
            intrinsic_conflict = checker.intrinsic_qos_interval_conflict(
                state.yang_policies,
            )
            if intrinsic_conflict is not None:
                # A contradiction among objectives belonging to this one
                # submitted plan is deterministic and independent of current
                # capacity.  Do not query admission first, because an expired
                # calibration record must not hide an empty QoS interval.
                from models.agent_state import ConflictReport

                report = ConflictReport(
                    # Preserve the legacy blocking signal while keeping the
                    # explicit policy-conflict fact false for this semantic
                    # validation stop.
                    has_conflict=True,
                    conflict_detected=False,
                    blocking=True,
                    stop_category=ConsensusReasonCode.SEMANTIC_VALIDATION_FAILED,
                    requires_manual_review=False,
                    conflict_type=intrinsic_conflict["conflict_type"],
                    severity=intrinsic_conflict["severity"],
                    explanation=intrinsic_conflict["explanation"],
                    suggestions=[intrinsic_conflict["suggestion"]],
                    conflict_items=[intrinsic_conflict],
                    rejection_reason=intrinsic_conflict["explanation"],
                    traffic_scope_key=intrinsic_conflict["traffic_scope_key"],
                    resolution="BLOCK_CONFLICT",
                    execution_mode="BLOCK_POLICY",
                )
            else:
                report = await checker.check(
                    yang_draft=state.yang_draft,
                    network_snapshot=state.network_snapshot,
                )

            # A fresh installation deliberately has no StableCapacity record.
            # Keep the fail-closed admission verdict, but give a *formal,
            # modeled candidate* one chance to collect the missing real
            # receiver-side evidence.  This runs before Policy/Binding commit
            # and is disabled for PREVIEW-* ids so dry-run never persists
            # capacity, A2A, scheduler, or network state.
            report = await self._calibrate_fresh_candidate_if_required(
                state=state,
                checker=checker,
                report=report,
            )

            # UNKNOWN/STALE is not an offline verdict.  Refresh the sole
            # sensing input once, preserving the parsed policy and avoiding a
            # second Understanding/LLM invocation.
            if report.context_refresh_required and state.context_refresh_attempts < 1:
                from agents.network_sensing import (
                    NetworkSensingAgent,
                    invalidate_snapshot_cache,
                )

                state.context_refresh_attempts += 1
                invalidate_snapshot_cache()
                sensing = NetworkSensingAgent()
                try:
                    state.network_snapshot = await sensing.collect_snapshot()
                finally:
                    await sensing.close()
                report = await checker.check(
                    yang_draft=state.yang_draft,
                    network_snapshot=state.network_snapshot,
                )

            if report.context_refresh_required:
                context_item = next(
                    (
                        item for item in report.warning_items
                        if item.get("context_refresh_required")
                    ),
                    {},
                )
                report = report.model_copy(update={
                    "has_conflict": False,
                    "conflict_detected": False,
                    "blocking": True,
                    "stop_category": ConsensusReasonCode.RESOURCE_UNRESOLVED,
                    "conflict_type": context_item.get("conflict_type", "NETWORK_CONTEXT_UNKNOWN"),
                    "severity": "HIGH",
                    "explanation": (
                        "无法确认计划依赖设备的实时可用状态；已完成一次上下文刷新，"
                        "Intent 暂停等待基础设施证据恢复。"
                    ),
                    "suggestions": [
                        "控制器或 P4Runtime 恢复后将使用同一 Intent 重新检查，不重新进行语义理解。"
                    ],
                    "conflict_items": [context_item],
                    "approval_token": None,
                    "southbound_execution": "NOT_EXECUTED",
                    "final_reason_code": context_item.get("conflict_type", "NETWORK_CONTEXT_UNKNOWN"),
                    "final_decision": "WAITING_FOR_INFRASTRUCTURE",
                })

            # ── 写入状态 ──
            state.conflict_report = report
            policy_result = self._current_policy_result(state)
            if state.yang_draft is not None:
                policy_result["yang_policy"] = state.yang_draft.model_dump(mode="json")
            policy_result["policy_conflict_report"] = report.model_dump(mode="json")

            if report.blocking:
                if report.stop_category in {
                    ConsensusReasonCode.CAPACITY_ADMISSION_REJECTED,
                    ConsensusReasonCode.SEMANTIC_VALIDATION_FAILED,
                } or report.conflict_type in {
                    "INSUFFICIENT_CAPACITY",
                    "INFEASIBLE_INTERVAL",
                    "INCOMPATIBLE_QOS_OBJECTIVES",
                    "OBJECTIVE_CONFLICT",
                    "SEMANTIC_CONFLICT",
                }:
                    state.current_status = IntentStatus.UNSATISFIABLE
                elif report.stop_category == ConsensusReasonCode.RESOURCE_UNRESOLVED or report.conflict_type in {
                    "INFRASTRUCTURE_UNAVAILABLE",
                    "NETWORK_CONTEXT_UNKNOWN",
                    "NETWORK_CONTEXT_STALE",
                }:
                    state.current_status = IntentStatus.WAITING_FOR_INFRASTRUCTURE
                else:
                    state.current_status = IntentStatus.FAILED
                logger.warning(
                    f"[{self.agent_id}] 发现 {len(report.conflict_items)} 个冲突: "
                    f"{report.rejection_reason}"
                )
                return self._end_log(
                    state,
                    start,
                    StageStatus.SUCCESS,
                    f"冲突检测完成: 发现{len(report.conflict_items)}个冲突, "
                    f"{report.rejection_reason}",
                )
            else:
                logger.info(
                    f"[{self.agent_id}] 冲突检测通过, "
                    f"approval_token={report.approval_token[:8]}..."
                )
                return self._end_log(
                    state,
                    start,
                    StageStatus.SUCCESS,
                    f"冲突检测通过: approval_token={report.approval_token[:8]}...",
                )
        except Exception as e:
            state.error_message = f"冲突检测异常: {str(e)}"
            logger.exception("[%s] %s", self.agent_id, state.error_message)
            return self._end_log(
                state, start, StageStatus.FAILED, "冲突检测异常", error=str(e)
            )

    async def _calibrate_fresh_candidate_if_required(
        self,
        *,
        state: GlobalAgentState,
        checker: Any,
        report: Any,
    ) -> Any:
        """Replace one fresh-capacity rejection with fenced candidate evidence.

        The candidate is created from the current in-memory draft only.  It
        never reads a previous policy/binding and is intentionally unavailable
        to previews.  A failed probe returns a normal blocking report; it does
        not leave a policy, binding, ingress desired object, or lease behind.
        """
        if str(state.intent_id).startswith("PREVIEW-") or not report.blocking:
            return report
        capacity_item = next(
            (
                item
                for item in (report.conflict_items or [])
                if str((item.get("admission_decision") or {}).get("reason_code"))
                == "RUNTIME_CAPACITY_UNVERIFIED"
            ),
            None,
        )
        if capacity_item is None or state.yang_draft is None:
            return report
        admission = dict(capacity_item.get("admission_decision") or {})
        purpose = str(admission.get("purpose") or "").upper()
        if purpose not in {"NEW_ADMISSION", "INCREASE_UPDATE"}:
            return report
        resource_ids = list(admission.get("affected_resources") or [])
        resource_id = str(resource_ids[0] if resource_ids else capacity_item.get("link_id") or "")
        requested = float(admission.get("requested_mbps") or 0)
        if not resource_id or requested <= 0:
            return report

        from datetime import timezone

        from infra.capacity_calibration import (
            ActiveIngressCapacityCalibrator,
            CalibrationCandidate,
            CapacityCalibrationError,
        )
        from infra.resource_registry import get_resource_registry
        from models.policy_update import traffic_scope_key

        registry = get_resource_registry().snapshot()
        scope = traffic_scope_key(state.yang_draft.network_policy).key
        candidate = CalibrationCandidate(
            intent_id=state.intent_id,
            traffic_scope_key=scope,
            staged_policy=state.yang_draft,
            service_id=str(
                (state.intent_plan.sub_intents[0].service_id if state.intent_plan and state.intent_plan.sub_intents else "")
                or ""
            ) or None,
            bottleneck_egress=resource_id,
            requested_schedulable_mbps=requested,
            topology_id=registry.topology_name,
            path_information={"link_id": resource_id, "purpose": purpose},
            trace_id=f"CAL-CANDIDATE-{state.intent_id}",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            evidence = await ActiveIngressCapacityCalibrator().calibrate_candidate(candidate)
        except CapacityCalibrationError as exc:
            failure = str(exc)
            item = {
                **capacity_item,
                "calibration_attempted": True,
                "calibration_authority": "PROVISIONAL_ADMISSION_CANDIDATE",
                "calibration_failure": failure,
                "admission_decision": {
                    **admission,
                    "reason_code": failure,
                    "candidate_calibration_attempted": True,
                },
            }
            return report.model_copy(
                update={
                    "has_conflict": False,
                    "conflict_detected": False,
                    "stop_category": ConsensusReasonCode.CAPACITY_ADMISSION_REJECTED,
                    "conflict_items": [item],
                    "explanation": f"Guaranteed bandwidth admission rejected: {failure}.",
                    "rejection_reason": failure,
                    "initial_admission_reason": admission.get("reason_code"),
                    "recovery_action": "LIVE_RECEIVER_IPERF_CANDIDATE_CALIBRATION",
                    "calibration_result": "REJECTED",
                    "final_reason_code": failure,
                    "final_decision": "UNSATISFIABLE",
                    "southbound_execution": "NOT_EXECUTED",
                    "resolution": "BLOCK_ADMISSION",
                    "execution_mode": "NOT_EXECUTED",
                }
            )
        logger.info(
            "[%s] candidate calibration succeeded for %s: record=%s schedulable=%s",
            self.agent_id,
            state.intent_id,
            (evidence.get("capacity_record") or {}).get("capacity_record_id"),
            evidence.get("schedulable_capacity_mbps"),
        )
        # ResourceLedger reads StableCapacityRepository on every decision, so
        # rerunning the same checker immediately observes CURRENT evidence in
        # this Intent lifecycle without a backend restart or a provisional
        # reservation.
        return await checker.check(
            yang_draft=state.yang_draft,
            network_snapshot=state.network_snapshot,
        )


# ──────────────────────────────────────────────
# Act阶段 — 策略生成节点
# ──────────────────────────────────────────────

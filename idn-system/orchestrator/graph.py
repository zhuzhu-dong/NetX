"""
orchestrator/graph.py
=====================

LangGraph 多智能体编排工作流 —— IDN-System 的核心状态机定义文件。

# 系统角色
本文件定义了 IDN-System 的完整 STAV（Sense → Think → Act → Verify）闭环工作流，
基于 LangGraph 的 StateGraph 实现 12 个节点的有向条件路由。
graph.py 是正式工作流路径（含 Checkpoint 持久化、条件路由、ReAct 重试和人工审批），
而 orchestrator/__init__.py 中的 run_intent_workflow() 是对外暴露的便捷调用入口。

# 工作流节点链
START → sense → parse_intent → check_conflict → generate_policy → verify_rules
     → risk_classify → {execute_flow | mini_council | safety_council}
     → execute_flow → verify_status → {END | check_conflict | generate_policy | human_approval}

# 条件路由说明
每个条件路由函数由 LangGraph 的 add_conditional_edges 调用，
在节点执行后根据 GlobalAgentState 的状态决定下一个节点。
路由决策基于以下维度：
- 是否出错（error_message）
- 是否需要用户澄清（needs_clarification）
- 是否存在策略冲突（conflict_report.has_conflict）
- P4 规则验证是否通过（rule_verification_report.is_valid）
- 风险等级（risk_level: LOW/MEDIUM/HIGH/CRITICAL）
- 安全评审是否批准（safety_council_report.overall_decision）
- ReAct 重试是否耗尽（react_state.can_retry）
- 人工审批决策（approval_decision: approve/reject/abort）
- KPI 验证是否通过（verification_passed）

# 主要依赖
- langgraph.graph.StateGraph / END / START：状态机引擎核心
- models.agent_state.GlobalAgentState：全局状态容器（泛型参数）
- .checkpointer：Checkpoint 持久化（SQLite/Redis/Memory）
- .nodes.*：12 个阶段节点的具体实现

# 与其他模块交互
- orchestrator/__init__.py：封装 IDNOrchestrator，提供便捷 API
- api/worker.py：通过 IDNOrchestrator.run() 启动工作流
- api/conversation/session.py：通过 thread_id 进行会话隔离

# 设计意图
- 路由函数与节点实现分离：路由决策集中在 graph.py，节点逻辑在 nodes/ 目录
- 风险分级路由（v2.0）：根据 RiskClassifyNode 的输出选择不同的安全评审深度
  - LOW → 跳过安全评审，直接下发
  - MEDIUM → MiniCouncil（轻量级不变量 + 历史检查）
  - HIGH → 完整 SafetyCouncil（5 个 Agent 并行评审）
  - CRITICAL → SafetyCouncil + 强制人工审批
- ReAct 重试机制：验证失败时回退到 generate_policy 重新编译，
  最多 3 次重试后升级到人工审批
"""

from __future__ import annotations

import logging
from functools import wraps
from enum import Enum
from typing import Any, Dict, Iterable, Literal, Mapping

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from models.agent_state import GlobalAgentState
from models.intent import IntentStatus
from .state_generations import (
    diagnostic_basis_is_current,
    diagnostics_allowed,
    mark_degraded,
    readback_is_current,
    violation_is_current,
)

from .checkpointer import close_checkpointer, get_async_checkpointer
from .nodes import (
    CheckConflictNode,
    CollectDiagnosticEvidenceNode,
    ExecuteFlowNode,
    GeneratePolicyNode,
    HumanApprovalNode,
    MiniCouncilNode,
    RiskClassifyNode,
    ReconcileExecutionNode,
    SafetyCouncilNode,
    SenseNode,
    ThinkNode,
    UnderstandingNode,
    VerifyRulePlanNode,
    VerifyStatusNode,
)

logger = logging.getLogger(__name__)


def _cancellation_guard(node_name: str, runner):
    """Check the durable cancel request immediately before every graph node.

    This guard is intentionally in the formal graph rather than the UI or a
    worker timer, so a late cancellation can never advance into another agent,
    compiler, controller write, verifier, or reconciliation node.
    """
    @wraps(runner)
    async def guarded(state: GlobalAgentState):
        from api.cancellation import IntentExecutionCancelled, cancellation_requested
        from api.store import get_store

        if cancellation_requested(get_store().get(state.intent_id)):
            logger.info("Intent %s cancellation acknowledged before %s", state.intent_id, node_name)
            raise IntentExecutionCancelled(state.intent_id, state=state)
        return await runner(state)
    return guarded


class GraphNodeName(str, Enum):
    """The sole registry of executable workflow node names.

    Conditional route values are part of the public orchestration contract:
    LangGraph resolves them at runtime.  Keep them here rather than duplicating
    string literals across node registration and edge maps.
    """

    UNDERSTAND_INTENT = "understand_intent"
    SENSE = "sense"
    PARSE_INTENT = "parse_intent"
    CHECK_CONFLICT = "check_conflict"
    GENERATE_POLICY = "generate_policy"
    VERIFY_RULES = "verify_rules"
    RISK_CLASSIFY = "risk_classify"
    MINI_COUNCIL = "mini_council"
    SAFETY_COUNCIL = "safety_council"
    EXECUTE_FLOW = "execute_flow"
    VERIFY_STATUS = "verify_status"
    RECONCILE_EXECUTION = "reconcile_execution"
    COLLECT_DIAGNOSTICS = "collect_diagnostics"
    HUMAN_APPROVAL = "human_approval"


END_ROUTE = "__end__"
FAILED_ROUTE = "end_failed"


def validate_graph_routes(
    *,
    registered_nodes: Iterable[str],
    static_targets: Iterable[str],
    conditional_routes: Mapping[str, Mapping[str, str]],
) -> None:
    """Fail graph construction when a route cannot be resolved by LangGraph.

    A conditional edge map has two contracts: its route keys must be known
    route outcomes and its destination values must be registered graph nodes
    (or LangGraph's END).  This makes an omitted mapping fail at startup/tests,
    not mid-workflow after a real policy has been executed.
    """
    registered = set(registered_nodes)
    allowed_destinations = registered | {END_ROUTE}
    unknown_static = set(static_targets) - allowed_destinations
    if unknown_static:
        raise ValueError(f"Graph has unregistered static targets: {sorted(unknown_static)}")
    for source, routes in conditional_routes.items():
        if source not in registered:
            raise ValueError(f"Conditional routes declared for unregistered node: {source}")
        invalid = set(routes.values()) - allowed_destinations
        if invalid:
            raise ValueError(
                f"Conditional routes from {source} target unregistered nodes: {sorted(invalid)}"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 条件路由函数 — 每个节点执行后调用，根据 GlobalAgentState 决策下一步
# ═══════════════════════════════════════════════════════════════════════════════


def route_after_understanding(
    state: GlobalAgentState,
) -> Literal["sense", "__end__", "end_failed"]:
    """Pause incomplete semantics before any sensing, planning or policy work."""
    if state.error_message:
        return "end_failed"
    if state.needs_clarification:
        return "__end__"
    if state.semantic_only_completed:
        return "__end__"
    return "sense"


def route_after_parse_intent(
    state: GlobalAgentState,
) -> Literal["check_conflict", "__end__", "end_failed"]:
    """意图解析后的路由决策。

    三种可能路径：
    1. 解析失败（error_message 非空）→ end_failed，终止工作流
    2. 意图模糊（needs_clarification=True）→ __end__，挂起等待用户澄清
    3. 解析成功 → check_conflict，进入冲突检测阶段

    Args:
        state: 当前全局状态（含 parse 阶段的输出）

    Returns:
        下一节点名称
    """
    if state.error_message:
        logger.error("Intent planning/modeling failed; stop graph.")
        return "end_failed"
    if state.needs_clarification:
        logger.info(
            "[Intent %s] Waiting for clarification: %s",
            state.intent_id,
            state.clarification_question,
        )
        return "__end__"
    if state.semantic_only_completed:
        logger.info(
            "[Intent %s] Semantic-only request completed; stop before conflict, compiler and execution.",
            state.intent_id,
        )
        return "__end__"
    return "check_conflict"


def route_after_conflict_check(
    state: GlobalAgentState,
) -> Literal["generate_policy", "__end__", "end_failed"]:
    """冲突检测后的路由决策。

    三种可能路径：
    1. 检测报告缺失 → end_failed，内部错误
    2. 存在冲突（has_conflict=True）→ __end__，拒绝策略，停止下发
    3. 无冲突 → generate_policy，进入策略编译阶段

    注意：与 P4 规则验证不同，冲突检测的失败不支持 ReAct 重试——
    冲突意味着策略本身与新策略矛盾，重新编译无法解决。

    Args:
        state: 当前全局状态（含 conflict_report）

    Returns:
        下一节点名称
    """
    if state.conflict_report is None:
        logger.error("conflict_report is empty; stop graph.")
        return "end_failed"
    if state.conflict_report.blocking:
        logger.warning(
            "Pre-execution gate blocked at %s; stop before flow generation.",
            state.conflict_report.stop_category.value,
        )
        return "__end__"
    return "generate_policy"


def route_after_rule_verification(
    state: GlobalAgentState,
) -> Literal["risk_classify", "generate_policy", "human_approval", "end_failed"]:
    """P4 规则验证后的路由决策。

    四种可能路径：
    1. 验证报告缺失 → end_failed
    2. 验证通过 → risk_classify，进入风险分级阶段
    3. 验证失败 + 可重试 → generate_policy，触发 ReAct 重编译
    4. 验证失败 + 重试耗尽 → human_approval，升级到人工审批

    ReAct 机制说明：当 P4 规则验证失败时（如 TCAM 容量不足、字段不匹配），
    系统不会立即放弃，而是回退到 PolicyCompiler 重新编译策略。
    每次重试时 PolicyCompiler 会根据上次失败原因调整策略
    （如降低优先级、使用备用匹配字段、简化动作）。

    Args:
        state: 当前全局状态（含 rule_verification_report 和 react_state）

    Returns:
        下一节点名称
    """
    if state.rule_verification_report is None:
        logger.error("rule_verification_report is empty; stop graph.")
        return "end_failed"
    if not state.rule_verification_report.is_valid:
        # 验证失败：检查是否还有重试配额
        if not state.react_state.can_retry:
            logger.error("P4 rule verification failed repeatedly; escalate.")
            return "human_approval"
        logger.warning("P4 rule verification failed; retry policy compilation.")
        return "generate_policy"
    # 验证通过，进入风险分级
    return "risk_classify"


def route_after_risk_classify(
    state: GlobalAgentState,
) -> Literal[
    "execute_flow", "mini_council", "safety_council", "human_approval", "end_failed"
]:
    """风险分级后的路由决策 —— v2.0 分级安全评审入口。

    根据 RiskClassifyNode 分析的风险等级选择不同的安全评审路径：

    - LOW（低风险）→ 跳过安全评审，直接 execute_flow 下发
      典型场景：对测试网络执行简单的 FORWARD 操作
    - MEDIUM（中等风险）→ MiniCouncil（轻量级：仅不变量 + 历史检查）
      典型场景：非生产时间修改已有策略参数
    - HIGH（高风险，默认）→ 完整 SafetyCouncil（5 个 Agent 并行评审）
      典型场景：生产网络新增 QoS 策略
    - CRITICAL（极高风险）→ SafetyCouncil + 强制人工审批
      典型场景：DROP 核心链路流量、修改 BGP 路由策略

    Args:
        state: 当前全局状态（含 risk_level 字段）

    Returns:
        下一节点名称
    """
    risk = getattr(state, "risk_level", "HIGH")
    if risk == "LOW":
        logger.info("[Intent %s] LOW risk — skipping SafetyCouncil", state.intent_id)
        return "execute_flow"
    if risk == "MEDIUM":
        logger.info("[Intent %s] MEDIUM risk — MiniCouncil", state.intent_id)
        return "mini_council"
    if risk == "CRITICAL":
        logger.info(
            "[Intent %s] CRITICAL risk — SafetyCouncil + human approval",
            state.intent_id,
        )
        return "safety_council"
    # HIGH（默认）→ full SafetyCouncil
    return "safety_council"


def route_after_mini_council(
    state: GlobalAgentState,
) -> Literal["execute_flow", "human_approval", "end_failed"]:
    """MiniCouncil 后的路由决策。

    MiniCouncil 是轻量级安全评审（仅不变量检查 + 历史风险评估），
    比完整 SafetyCouncil 更快但覆盖范围较小。
    驳回时升级到人工审批而非直接拒绝。
    - degraded：评审异常降级，允许继续执行（带警告）

    Args:
        state: 当前全局状态（含 safety_council_report）

    Returns:
        下一节点名称
    """
    report = state.safety_council_report
    if report is None:
        logger.error("safety_council_report is empty after mini_council; stop graph.")
        return "end_failed"
    decision = report.get("overall_decision")
    if decision == "degraded":
        logger.warning("MiniCouncil degraded — proceeding to execute_flow with warning")
        return "execute_flow"
    if decision != "approve":
        logger.warning("MiniCouncil blocked execution; escalate to human approval.")
        return "human_approval"
    return "execute_flow"


def route_after_safety_council(
    state: GlobalAgentState,
) -> Literal["execute_flow", "human_approval", "end_failed"]:
    """SafetyCouncil 后的路由决策。

    只有所有 5 个安全评审 Agent（不变量检查、影子仿真、红队审查、
    回滚验证、历史风险评估）全部通过（overall_decision == "approve"）时，
    才允许进入流表下发阶段。

    驳回时升级到人工审批——管理员可以覆盖 AI 安全评审的决策。
    - degraded：评审异常降级，允许继续执行（带警告）

    Args:
        state: 当前全局状态（含 safety_council_report）

    Returns:
        下一节点名称
    """
    report = state.safety_council_report
    if report is None:
        logger.error("safety_council_report is empty; stop graph.")
        return "end_failed"
    decision = report.get("overall_decision")
    if decision == "degraded":
        logger.warning(
            "SafetyCouncil degraded — proceeding to execute_flow with warning"
        )
        return "execute_flow"
    if decision != "approve":
        logger.warning("Safety Council blocked execution; escalate to human approval.")
        return "human_approval"
    return "execute_flow"


def route_after_human_approval(
    state: GlobalAgentState,
) -> Literal["execute_flow", "generate_policy", "__end__", "end_failed"]:
    """人工审批后的路由决策。

    HumanApprovalNode 通过 LangGraph 的 interrupt() 机制暂停工作流，
    等待人工操作员通过 Command(resume={decision}) 恢复执行。
    恢复时 approval_decision 被设置为 "approve" / "reject" / "abort"。

    三种路径：
    - approve → execute_flow：批准执行，继续流表下发
    - reject  → generate_policy：驳回策略，触发重新编译
    - abort   → __end__：中止，停止工作流（可能触发回滚）

    Args:
        state: 当前全局状态（含 approval_decision）

    Returns:
        下一节点名称
    """
    decision = getattr(state, "approval_decision", "abort")
    if decision == "approve":
        logger.info(
            "[Intent %s] Human approval: APPROVE → continuing to execute_flow",
            state.intent_id,
        )
        return "execute_flow"
    elif decision == "reject":
        logger.info(
            "[Intent %s] Human approval: REJECT → recompiling policy", state.intent_id
        )
        return "generate_policy"
    else:
        logger.warning("[Intent %s] Human approval: ABORT → ending", state.intent_id)
        if state.error_message:
            return "end_failed"
        return "__end__"


def route_after_verification(
    state: GlobalAgentState,
) -> Literal["__end__", "check_conflict", "generate_policy", "human_approval", "collect_diagnostics", "reconcile_execution", "verify_status"]:
    """KPI 验证后的路由决策 —— STAV 闭环的终点或回溯点。

    这是整个工作流中最复杂的路由函数，处理以下场景：

    1. 验证通过 + 没有更多策略 → __end__（成功完成）
    2. 验证通过 + 还有策略待处理 → check_conflict（处理下一个 YANG Policy）
    3. 验证失败 + 可重试 → generate_policy（ReAct 回溯，重新编译）
    4. 验证失败 + 重试耗尽 → human_approval（升级到人工审批）

    多策略处理说明：
    当 IntentPlan 包含多个 SubIntent 时，每个 SubIntent 生成一个 YANGPolicy。
    当前策略通过验证后，current_policy_index 递增，回到 check_conflict
    处理下一个策略。循环直到所有策略处理完毕或某个策略失败。

    Args:
        state: 当前全局状态（含 verification_passed, react_state,
               current_policy_index, yang_policies）

    Returns:
        下一节点名称
    """
    # VerifyStatusNode resets all per-policy execution and assurance fields
    # before moving to the next YANG policy.  Route that transition before any
    # readback-generation checks: otherwise the intentionally cleared
    # ``execution_result`` is mistaken for an incomplete prior execution and
    # the verifier is invoked without an executor result.
    if (
        state.next_action == "PROCESS_NEXT_POLICY"
        and state.yang_draft is not None
    ):
        return "check_conflict"

    if state.current_status == IntentStatus.UNSATISFIABLE:
        logger.error(
            "[Intent %s] Healing reached terminal UNSATISFIABLE state.",
            state.intent_id,
        )
        return "__end__"
    # A completed success has already passed the execution/readback and
    # assurance gates.  Preserve this terminal branch for checkpoint replay.
    if state.current_status == IntentStatus.SUCCESS and state.verification_passed:
        return "__end__"

    verification = state.verification_report or {}
    # Phase priority is deliberate.  A root-cause string is never an override
    # for infrastructure/readback/assurance gates.
    if state.current_status == IntentStatus.WAITING_FOR_INFRASTRUCTURE:
        return "__end__"
    readback_status = str(state.execution_readback_status or verification.get("verification_state") or "").upper()
    if readback_status == "EXECUTION_READBACK_MISMATCH":
        return "reconcile_execution"
    if readback_status in {"EXECUTION_READBACK_UNAVAILABLE", "DEGRADED_EXECUTION_UNVERIFIED"}:
        return "__end__"
    has_execution_phase = bool(state.execution_result or state.execution_generation > 0)
    if not readback_is_current(state) and has_execution_phase:
        # No current execution proof: re-enter only the verifier/readback
        # phase, never diagnostics or policy compilation.
        state.next_action = "VERIFY_EXECUTION_READBACK"
        return "verify_status"
    if has_execution_phase and state.measurement_status in {"NOT_STARTED", "ASSURANCE_MEASURING"}:
        # The mapping explicitly registers this self-transition.  It is used
        # only to resume a partially interrupted readback/assurance phase.
        return "verify_status"
    if has_execution_phase and state.measurement_status == "INCONCLUSIVE":
        mark_degraded(state, reason="MEASUREMENT_INCONCLUSIVE", next_action="MEASUREMENT_CONFIRMATION")
        return "__end__"
    diagnostic = state.diagnostic_state or {}
    # A final reanalysis is allowed after round three when fresh material
    # evidence was collected.  Once that reanalysis returns without a
    # confirmed root cause, it is a terminal *business* outcome, not a ReAct
    # failure and not another approval request.  This has to precede the
    # generic failed-verification branch because the verifier refreshes its
    # report while performing that final reanalysis.
    if (
        violation_is_current(state)
        and state.root_cause_status == "PENDING"
        and int(diagnostic.get("diagnostic_round") or 0)
        >= int(diagnostic.get("max_diagnostic_rounds") or 3)
    ):
        verification.update({
            "verification_state": "DEGRADED_ROOT_CAUSE_UNCONFIRMED",
            "orchestration_phase": "DEGRADED_AWAITING_NEW_MATERIAL_EVIDENCE",
            "terminal_reason": "ROOT_CAUSE_UNCONFIRMED_AFTER_MAX_DIAGNOSTICS",
            "route_decision": "END_DEGRADED",
            "diagnostics_required": False,
            "healing_ready": False,
            "terminal": True,
        })
        state.verification_report = verification
        state.diagnostics_required = False
        state.healing_ready = False
        mark_degraded(
            state,
            reason="ROOT_CAUSE_UNCONFIRMED_AFTER_MAX_DIAGNOSTICS",
            next_action="WAIT_FOR_NEW_MATERIAL_EVIDENCE",
        )
        return "__end__"
    if str(verification.get("verification_state") or "").upper() == "VIOLATION_CONFIRMED_ROOT_CAUSE_PENDING":
        if not violation_is_current(state) or not diagnostics_allowed(state):
            verification.update({
                "verification_state": "ASSURANCE_REQUIRED",
                "route_decision": "REJECT_STALE_DIAGNOSTIC_BASIS",
                "diagnostics_required": False,
            })
            state.verification_report = verification
            state.violation_confirmed = False
            state.root_cause_status = "NOT_STARTED"
            state.diagnostics_required = False
            state.measurement_status = "INCONCLUSIVE"
            mark_degraded(state, reason="ASSURANCE_REQUIRED", next_action="MEASUREMENT_CONFIRMATION")
            return "__end__"
        if int(diagnostic.get("diagnostic_round") or 0) >= int(diagnostic.get("max_diagnostic_rounds") or 3):
            verification.update({"verification_state": "DEGRADED_ROOT_CAUSE_UNCONFIRMED", "terminal_reason": "DIAGNOSTIC_ROUND_LIMIT_REACHED", "route_decision": "END_DEGRADED"})
            state.verification_report = verification
            state.diagnostics_required = False
            state.healing_ready = False
            mark_degraded(state, reason="ROOT_CAUSE_UNCONFIRMED_AFTER_MAX_DIAGNOSTICS", next_action="OPERATOR_REVIEW")
            return "__end__"
        # Evidence collection is a read-only path.  It must never reuse the
        # generic ReAct branch, which recompiles and may reach execute_flow.
        return "collect_diagnostics"

    if state.verification_passed:
        # 当前策略验证通过：检查是否还有更多策略需要处理
        if (
            state.current_status != IntentStatus.SUCCESS
            and state.yang_draft is not None
        ):
            logger.info(
                "[Intent %s] Current policy passed; continue with policy index %s.",
                state.intent_id,
                state.current_policy_index,
            )
            return "check_conflict"
        # 所有策略处理完毕
        logger.info("[Intent %s] All policies verified successfully.", state.intent_id)
        return "__end__"

    # 验证失败：检查 ReAct 重试配额
    # An inconclusive measurement is intentionally neither a policy conflict
    # nor a failed candidate. Recompiling the same desired effect cannot
    # create missing evidence, and it does not advance the ReAct counter.
    # Routing it through the generic retry branch would therefore spin
    # forever while repeatedly executing/reusing the same policy.
    report = state.verification_report or {}
    measurement_state = str(
        report.get("verification_state") or state.measurement_status or ""
    ).upper()
    if measurement_state in {
        "MEASUREMENT_INCONCLUSIVE",
        "MEASUREMENT_UNAVAILABLE",
        "NO_SAMPLE",
        "TELEMETRY_UNAVAILABLE",
    }:
        state.current_status = IntentStatus.DEGRADED
        state.measurement_status = "INCONCLUSIVE"
        report.update(
            {
                "route_decision": "END_AWAITING_VALID_MEASUREMENT",
                "next_action": "RUN_FRESH_SERVICE_MEASUREMENT",
                "retryable": True,
            }
        )
        state.verification_report = report
        mark_degraded(
            state,
            reason=measurement_state,
            next_action="RUN_FRESH_SERVICE_MEASUREMENT",
        )
        logger.warning(
            "[Intent %s] Measurement is inconclusive; ending without policy recompilation.",
            state.intent_id,
        )
        return "__end__"

    if state.react_state.can_retry:
        logger.warning(
            "[Intent %s] Verification failed, retry %s/%s via PolicyCompiler.",
            state.intent_id,
            state.react_state.retry_count,
            state.react_state.max_retries,
        )
        return "generate_policy"

    # A retry budget is only a loop-safety mechanism.  It is not proof that
    # every supported healing candidate has been enumerated and rejected.
    # UNSATISFIABLE may be emitted only by VerifyStatusNode with its complete
    # candidate-evaluation matrix; otherwise preserve the failed evidence and
    # ask for review instead of claiming no feasible action exists.
    report = state.verification_report or {}
    matrix = report.get("candidate_evaluations") or []
    if report.get("healing_search_status") == "EXHAUSTED" and matrix:
        state.current_status = IntentStatus.UNSATISFIABLE
        return "__end__"
    state.current_status = IntentStatus.FAILED
    state.error_message = state.error_message or "HEALING_RETRY_LIMIT_REACHED"
    logger.error(
        "[Intent %s] Verification retry limit reached without an exhausted candidate matrix; request review.",
        state.intent_id,
    )
    return "human_approval"


def route_after_diagnostics(
    state: GlobalAgentState,
) -> Literal["verify_status", "collect_diagnostics", "__end__"]:
    diagnostic = state.diagnostic_state or {}
    if not violation_is_current(state) or not diagnostic_basis_is_current(state):
        report = state.verification_report or {}
        report.update({
            "verification_state": "ASSURANCE_REQUIRED",
            "route_decision": "END_ASSURANCE_REQUIRED",
            "diagnostics_required": False,
        })
        state.verification_report = report
        state.diagnostics_required = False
        state.measurement_status = "INCONCLUSIVE"
        mark_degraded(state, reason="ASSURANCE_REQUIRED", next_action="MEASUREMENT_CONFIRMATION")
        return "__end__"
    round_no = int(diagnostic.get("diagnostic_round") or 0)
    maximum = int(diagnostic.get("max_diagnostic_rounds") or 3)
    # The ceiling is intentionally first: material change may justify one
    # reanalysis of the completed round, never a fourth collection round.
    if round_no >= maximum:
        report = state.verification_report or {}
        report.update({
            "verification_state": "DEGRADED_ROOT_CAUSE_UNCONFIRMED",
            "terminal_reason": "ROOT_CAUSE_UNCONFIRMED_AFTER_MAX_DIAGNOSTICS",
            "route_decision": "END_DEGRADED",
            "diagnostics_required": False,
            "healing_ready": False,
        })
        state.verification_report = report
        state.diagnostics_required = False
        state.healing_ready = False
        mark_degraded(state, reason="ROOT_CAUSE_UNCONFIRMED_AFTER_MAX_DIAGNOSTICS", next_action="OPERATOR_REVIEW")
        return "__end__"
    if diagnostic.get("loop_guard_triggered"):
        return "__end__"
    if diagnostic.get("new_material_evidence", diagnostic.get("new_evidence")):
        return "verify_status"
    return "collect_diagnostics"


# ═══════════════════════════════════════════════════════════════════════════════
# IDNOrchestrator — LangGraph 编排器主类
# ═══════════════════════════════════════════════════════════════════════════════


class IDNOrchestrator:
    """基于 LangGraph 的 STAV 编排器，用于意图驱动网络。

    此类是 IDN-System 的核心引擎，负责：
    1. 构建 LangGraph StateGraph（12 个节点 + 条件边）
    2. 管理 Checkpoint 持久化（SQLite/Redis）
    3. 驱动完整工作流执行

    # 生命周期
    1. 实例化 IDNOrchestrator(config)
    2. await initialize() — 初始化 Checkpointer 并编译图
    3. await run(state) — 执行工作流（可多次调用，每次一个 intent）
    4. await close() — 关闭 Checkpointer 连接

    # Checkpoint 机制
    LangGraph 在每个节点执行后自动保存 GlobalAgentState 快照。
    thread_id（即 intent_id::session_id）用于隔离不同意图的状态。
    支持暂停/恢复：如果在 human_approval 节点暂停，可通过相同 thread_id
    恢复执行。

    # 线程安全
    当前实现不保证线程安全——每个 IDNOrchestrator 实例应在单个
    asyncio 事件循环中使用。多租户场景建议每个请求创建独立实例。
    """

    def __init__(
        self,
        config: Dict[str, Any] | None = None,
        *,
        dry_run: bool = False,
    ):
        """初始化编排器。

        Args:
            config: 可选配置字典，支持的 key：
                - checkpoint_backend: "sqlite"（默认）| "redis"
            dry_run: 仅预览模式，流水线在 verify_rules 后停止，
                     不下发流表、不写持久化 Checkpoint。
        """
        self.config = config or {}
        self.dry_run = dry_run
        self._graph: CompiledStateGraph | None = None
        self._checkpointer = None

    async def initialize(self):
        """初始化 Checkpoint 存储并编译 LangGraph 状态图。

        此方法必须在 run() 之前调用。初始化顺序：
        1. 根据配置选择 Checkpoint 后端（SQLite/Redis）；
           dry_run 模式强制使用内存后端（不持久化）。
        2. 构建并编译 StateGraph

        Raises:
            RuntimeError: 如果 Checkpoint 后端初始化失败
        """
        if self.dry_run:
            # Dry-run uses in-memory checkpointer — no persistence
            self._checkpointer = await get_async_checkpointer("memory")
        else:
            backend = self.config.get("checkpoint_backend", "sqlite")
            self._checkpointer = await get_async_checkpointer(backend)
        self._build_graph()
        logger.info("IDN orchestrator initialized (dry_run=%s).", self.dry_run)

    def _build_graph(self) -> None:
        """构建 LangGraph 状态机。

        此方法定义完整的 12 节点 STAV 工作流拓扑：
        1. 添加节点（每个节点对应一个 STAV 阶段）
        2. 添加固定边（确定性流转，如 sense → parse_intent）
        3. 添加条件边（决策流转，如 parse_intent → check_conflict / __end__ / end_failed）

        【难点解释】条件边使用三参数版本 add_conditional_edges(node, route_fn, mapping)：
        - route_fn 返回字符串（下一节点名称）
        - mapping 将 route_fn 的返回值映射到实际节点
        - 这种模式确保 route_fn 中不会出现无效的目标节点名

        风险分级安全评审路由（v2.0）：
        verify_rules → risk_classify → {execute_flow (LOW)
                                        | mini_council (MEDIUM)
                                        | safety_council (HIGH/CRITICAL)}
        """
        # 创建以 GlobalAgentState 为状态容器的状态图
        node = GraphNodeName
        registered_nodes = {item.value for item in GraphNodeName}
        # The maps below are the executable routing contract. A missing value
        # here must abort initialization rather than surface as a LangGraph
        # KeyError after a real flow has been deployed.
        route_maps = {
            node.UNDERSTAND_INTENT.value: {"sense": node.SENSE.value, END_ROUTE: END_ROUTE, FAILED_ROUTE: END_ROUTE},
            node.HUMAN_APPROVAL.value: {"execute_flow": node.EXECUTE_FLOW.value, "generate_policy": node.GENERATE_POLICY.value, END_ROUTE: END_ROUTE, FAILED_ROUTE: END_ROUTE},
            node.PARSE_INTENT.value: {"check_conflict": node.CHECK_CONFLICT.value, END_ROUTE: END_ROUTE, FAILED_ROUTE: END_ROUTE},
            node.CHECK_CONFLICT.value: {"generate_policy": node.GENERATE_POLICY.value, END_ROUTE: END_ROUTE, FAILED_ROUTE: END_ROUTE},
            node.VERIFY_RULES.value: {"risk_classify": node.RISK_CLASSIFY.value, "generate_policy": node.GENERATE_POLICY.value, "human_approval": node.HUMAN_APPROVAL.value, FAILED_ROUTE: END_ROUTE},
            node.RISK_CLASSIFY.value: {"execute_flow": node.EXECUTE_FLOW.value, "mini_council": node.MINI_COUNCIL.value, "safety_council": node.SAFETY_COUNCIL.value, "human_approval": node.HUMAN_APPROVAL.value, FAILED_ROUTE: END_ROUTE},
            node.MINI_COUNCIL.value: {"execute_flow": node.EXECUTE_FLOW.value, "human_approval": node.HUMAN_APPROVAL.value, FAILED_ROUTE: END_ROUTE},
            node.SAFETY_COUNCIL.value: {"execute_flow": node.EXECUTE_FLOW.value, "human_approval": node.HUMAN_APPROVAL.value, FAILED_ROUTE: END_ROUTE},
            node.VERIFY_STATUS.value: {END_ROUTE: END_ROUTE, "check_conflict": node.CHECK_CONFLICT.value, "generate_policy": node.GENERATE_POLICY.value, "human_approval": node.HUMAN_APPROVAL.value, "collect_diagnostics": node.COLLECT_DIAGNOSTICS.value, "reconcile_execution": node.RECONCILE_EXECUTION.value, "verify_status": node.VERIFY_STATUS.value},
            node.RECONCILE_EXECUTION.value: {"verify_status": node.VERIFY_STATUS.value, "reconcile_execution": node.RECONCILE_EXECUTION.value, END_ROUTE: END_ROUTE},
            node.COLLECT_DIAGNOSTICS.value: {"verify_status": node.VERIFY_STATUS.value, "collect_diagnostics": node.COLLECT_DIAGNOSTICS.value, END_ROUTE: END_ROUTE},
        }
        validate_graph_routes(
            registered_nodes=registered_nodes,
            static_targets=[node.UNDERSTAND_INTENT.value, node.PARSE_INTENT.value, node.VERIFY_RULES.value, node.VERIFY_STATUS.value],
            conditional_routes=route_maps,
        )
        graph = StateGraph(GlobalAgentState)

        # ── 注册所有节点 ──
        # 每个节点通过 run_with_timeout() 包装，提供超时和自动重试
        llm = self.config.get("llm")
        graph.add_node(node.UNDERSTAND_INTENT.value, _cancellation_guard(node.UNDERSTAND_INTENT.value, UnderstandingNode(llm=llm).run_with_timeout))
        graph.add_node(node.SENSE.value, _cancellation_guard(node.SENSE.value, SenseNode(llm=llm).run_with_timeout))
        graph.add_node(node.PARSE_INTENT.value, _cancellation_guard(node.PARSE_INTENT.value, ThinkNode(llm=llm).run_with_timeout))
        graph.add_node(node.CHECK_CONFLICT.value, _cancellation_guard(node.CHECK_CONFLICT.value, CheckConflictNode(llm=llm).run_with_timeout))
        graph.add_node(node.GENERATE_POLICY.value, _cancellation_guard(node.GENERATE_POLICY.value, GeneratePolicyNode(llm=llm).run_with_timeout))
        graph.add_node(node.VERIFY_RULES.value, _cancellation_guard(node.VERIFY_RULES.value, VerifyRulePlanNode(llm=llm).run_with_timeout))
        graph.add_node(node.RISK_CLASSIFY.value, _cancellation_guard(node.RISK_CLASSIFY.value, RiskClassifyNode(llm=llm).run_with_timeout))
        graph.add_node(node.MINI_COUNCIL.value, _cancellation_guard(node.MINI_COUNCIL.value, MiniCouncilNode(llm=llm).run_with_timeout))
        graph.add_node(node.SAFETY_COUNCIL.value, _cancellation_guard(node.SAFETY_COUNCIL.value, SafetyCouncilNode(llm=llm).run_with_timeout))
        graph.add_node(node.EXECUTE_FLOW.value, _cancellation_guard(node.EXECUTE_FLOW.value, ExecuteFlowNode(llm=llm).run_with_timeout))
        graph.add_node(node.VERIFY_STATUS.value, _cancellation_guard(node.VERIFY_STATUS.value, VerifyStatusNode(llm=llm).run_with_timeout))
        graph.add_node(node.RECONCILE_EXECUTION.value, _cancellation_guard(node.RECONCILE_EXECUTION.value, ReconcileExecutionNode(llm=llm).run_with_timeout))
        graph.add_node(node.COLLECT_DIAGNOSTICS.value, _cancellation_guard(node.COLLECT_DIAGNOSTICS.value, CollectDiagnosticEvidenceNode(llm=llm).run_with_timeout))
        graph.add_node(node.HUMAN_APPROVAL.value, _cancellation_guard(node.HUMAN_APPROVAL.value, HumanApprovalNode(llm=llm).run_with_timeout))

        # ── 固定边（确定性流转）──
        graph.add_edge(START, node.UNDERSTAND_INTENT.value)
        graph.add_edge(node.SENSE.value, node.PARSE_INTENT.value)
        graph.add_edge(node.GENERATE_POLICY.value, node.VERIFY_RULES.value)
        graph.add_edge(node.EXECUTE_FLOW.value, node.VERIFY_STATUS.value)

        # ── 条件边（决策流转）──

        graph.add_conditional_edges(
            node.UNDERSTAND_INTENT.value,
            route_after_understanding,
            {key: (END if value == END_ROUTE else value) for key, value in route_maps[node.UNDERSTAND_INTENT.value].items()},
        )

        # 人工审批路由
        # HumanApprovalNode 通过 LangGraph interrupt() 暂停工作流，
        # 当通过 Command(resume={decision}) 恢复时，节点返回并继续路由
        graph.add_conditional_edges(
            node.HUMAN_APPROVAL.value,
            route_after_human_approval,
            {key: (END if value == END_ROUTE else value) for key, value in route_maps[node.HUMAN_APPROVAL.value].items()},
        )

        # 意图解析路由（parse → check_conflict / end / end_failed）
        graph.add_conditional_edges(
            node.PARSE_INTENT.value,
            route_after_parse_intent,
            {key: (END if value == END_ROUTE else value) for key, value in route_maps[node.PARSE_INTENT.value].items()},
        )

        # 冲突检测路由（check → generate_policy / end / end_failed）
        graph.add_conditional_edges(
            node.CHECK_CONFLICT.value,
            route_after_conflict_check,
            {key: (END if value == END_ROUTE else value) for key, value in route_maps[node.CHECK_CONFLICT.value].items()},
        )

        # ── 风险分级安全评审路由（v2.0）──
        # verify_rules → risk_classify → {execute_flow | mini_council | safety_council}
        if self.dry_run:
            # Dry-run: stop after verify_rules — don't execute or persist
            graph.add_edge(node.VERIFY_RULES.value, END)
        else:
            graph.add_conditional_edges(
                node.VERIFY_RULES.value,
                route_after_rule_verification,
                {key: (END if value == END_ROUTE else value) for key, value in route_maps[node.VERIFY_RULES.value].items()},
            )

        graph.add_conditional_edges(
            node.RISK_CLASSIFY.value,
            route_after_risk_classify,
            {key: (END if value == END_ROUTE else value) for key, value in route_maps[node.RISK_CLASSIFY.value].items()},
        )

        graph.add_conditional_edges(
            node.MINI_COUNCIL.value,
            route_after_mini_council,
            {key: (END if value == END_ROUTE else value) for key, value in route_maps[node.MINI_COUNCIL.value].items()},
        )

        graph.add_conditional_edges(
            node.SAFETY_COUNCIL.value,
            route_after_safety_council,
            {key: (END if value == END_ROUTE else value) for key, value in route_maps[node.SAFETY_COUNCIL.value].items()},
        )

        # ── KPI 验证路由（verify → end / check_conflict / generate_policy / human_approval）──
        graph.add_conditional_edges(
            node.VERIFY_STATUS.value,
            route_after_verification,
            {key: (END if value == END_ROUTE else value) for key, value in route_maps[node.VERIFY_STATUS.value].items()},
        )
        graph.add_conditional_edges(
            node.RECONCILE_EXECUTION.value,
            lambda state: (
                "verify_status"
                if state.execution_readback_status == "RECONCILIATION_VERIFIED"
                else "reconcile_execution"
                if state.execution_readback_status == "EXECUTION_READBACK_MISMATCH"
                and state.reconciliation_attempt < state.max_reconciliation_attempts
                else "__end__"
            ),
            {key: (END if value == END_ROUTE else value) for key, value in route_maps[node.RECONCILE_EXECUTION.value].items()},
        )
        graph.add_conditional_edges(
            node.COLLECT_DIAGNOSTICS.value,
            route_after_diagnostics,
            {key: (END if value == END_ROUTE else value) for key, value in route_maps[node.COLLECT_DIAGNOSTICS.value].items()},
        )

        # 编译状态图（如果有 Checkpointer，则启用持久化）
        self._graph = (
            graph.compile(checkpointer=self._checkpointer)
            if self._checkpointer
            else graph.compile()
        )
        logger.info("IDN LangGraph state machine compiled.")

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        """执行一个意图工作流并返回最终全局状态。

        Args:
            state: 初始 GlobalAgentState（含 UserIntent 和 NetworkSnapshot）

        Returns:
            工作流执行完成后的最终 GlobalAgentState，
            包含完整的执行日志、策略产物和验证结果
        """
        if not self._graph:
            await self.initialize()
        if self._graph is None:  # pragma: no cover - initialize invariant
            raise RuntimeError("orchestrator graph was not initialized")

        # 配置 LangGraph 执行参数 —— thread_id 用于 Checkpoint 会话隔离
        config = {"configurable": {"thread_id": state.get_thread_id()}}
        logger.info(
            "Starting IDN workflow intent_id=%s thread_id=%s",
            state.intent_id,
            state.get_thread_id(),
        )
        result = await self._graph.ainvoke(state, config=config)
        if isinstance(result, GlobalAgentState):
            return result
        return GlobalAgentState.model_validate(result)

    def get_graph_image(self) -> bytes:
        """导出状态图为 PNG 图片（用于调试和文档）。

        使用 Mermaid 渲染引擎将 LangGraph 拓扑转换为 PNG 格式。

        Returns:
            包含 PNG 图片的字节数据
        """
        if self._graph is None:
            raise RuntimeError("orchestrator graph was not initialized")
        return self._graph.get_graph().draw_mermaid_png()

    @property
    def graph(self) -> CompiledStateGraph:
        """获取编译后的 LangGraph 状态图实例。

        可用于调试、导出图片或直接操作图结构。
        """
        return self._graph

    async def close(self):
        """每个 pipeline 结束时的清理。

        Checkpointer 是全局单例，由应用 shutdown 统一关闭。
        本方法不关闭共享连接，避免并发 pipeline 之间的竞争。
        """
        logger.debug("IDN orchestrator instance released (checkpointer kept alive).")

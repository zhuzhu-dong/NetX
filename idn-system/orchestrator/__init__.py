"""
orchestrator/__init__.py
========================

智能体编排子系统入口 —— LangGraph 状态机引擎。

# 系统角色
本模块是 IDN-System 的"大脑"，封装了基于 LangGraph 的多智能体编排引擎。
对外暴露简洁的 API（IDNOrchestrator 类和 run_intent_workflow 函数），
隐藏内部 12 个节点的复杂状态机逻辑。

# 主要导出
- IDNOrchestrator：LangGraph 编排器类，管理整个 STAV 闭环生命周期
- run_intent_workflow()：便捷函数，将自然语言意图通过完整编排流水线
  转换为网络配置并执行
- 各阶段节点类：SenseNode, ThinkNode, ..., VerifyStatusNode,
  HumanApprovalNode
- get_checkpointer：获取 Checkpoint 持久化实例

# 工作流概述
1. 用户输入自然语言意图 → run_intent_workflow()
2. 构造 GlobalAgentState（包含 UserIntent 和 NetworkSnapshot）
3. IDNOrchestrator 驱动 LangGraph 状态机执行 12 节点流水线
4. 返回包含完整流水线结果的 dict（兼容旧 pipeline.py 的返回格式）

# 主要依赖
- langgraph.graph.StateGraph：状态机引擎
- models.agent_state.GlobalAgentState：全局状态容器
- .graph.IDNOrchestrator：编排器核心
- .nodes.*：12 个阶段节点实现
- .checkpointer：Checkpoint 持久化管理
"""

import uuid
from typing import Any, Optional

from langchain_core.language_models import BaseChatModel

from .checkpointer import get_checkpointer
from .graph import IDNOrchestrator
from .nodes import (
    CheckConflictNode,
    ExecuteFlowNode,
    GeneratePolicyNode,
    HumanApprovalNode,
    SenseNode,
    ThinkNode,
    VerifyRulePlanNode,
    VerifyStatusNode,
)

__all__ = [
    "IDNOrchestrator",
    "run_intent_workflow",
    "SenseNode",
    "ThinkNode",
    "CheckConflictNode",
    "GeneratePolicyNode",
    "VerifyRulePlanNode",
    "ExecuteFlowNode",
    "VerifyStatusNode",
    "HumanApprovalNode",
    "get_checkpointer",
]


def _build_user_intent(user_input: str, context: Optional[dict[str, Any]] = None):
    """Preserve the model's generated session id unless the caller supplied one."""
    from models.intent import UserIntent

    values: dict[str, Any] = {"text": user_input}
    if context and context.get("session_id"):
        values["session_id"] = str(context["session_id"])
    return UserIntent(**values)


async def run_intent_workflow(
    user_input: str,
    *,
    llm: Optional[BaseChatModel] = None,
    network_snapshot: Any = None,
    context: Optional[dict[str, Any]] = None,
    wait_secs: int = 0,
) -> dict[str, Any]:
    """将自然语言意图通过 IDNOrchestrator LangGraph 工作流执行。

    此函数是原 ``pipeline.py:run_intent_pipeline()`` 的直接替代品，
    返回与旧流水线相同形状的 dict，但后端由 LangGraph 状态机驱动。

    执行流程：
    1. 构造 UserIntent → GlobalAgentState
    2. 初始化 IDNOrchestrator（含 Checkpointer）
    3. 调用 orchestrator.run(state) 驱动 LangGraph 状态机
    4. 将 GlobalAgentState 转换为 pipeline 兼容的 dict 格式

    Args:
        user_input: 用户自然语言意图文本
        llm: 可选的外部 LLM 实例（用于测试注入）
        network_snapshot: 可选的预采集网络快照
        context: 可选上下文（intent_id, session_id 等）
        wait_secs: 等待秒数（保留参数，兼容旧接口）

    Returns:
        包含完整流水线结果的 dict，key 包括：
        - input: 原始用户输入
        - final_status: 最终状态（SUCCESS/FAILED/VERIFICATION_FAILED）
        - summary: 摘要信息（策略名、动作类型、带宽、安全决策等）
        - network_snapshot: 网络拓扑快照 JSON
        - intent_plan: 意图计划 JSON
        - yang_policy: YANG 策略 JSON
        - flow_table_plan: 流表计划
        - flow_table_commands: 可读的 P4Runtime 命令列表
        - execution_result: 执行结果
        - status_verification_report: KPI 验证报告
    """
    from models.agent_state import GlobalAgentState

    # 生成或复用 intent_id
    intent_id = context.get("intent_id") if context else None
    intent_id = intent_id or f"TEST-{uuid.uuid4().hex[:8].upper()}"

    # 构造初始全局状态 —— 这是 LangGraph 工作流的输入
    user_intent = _build_user_intent(user_input, context)
    state = GlobalAgentState(
        intent_id=intent_id,
        user_intent=user_intent,
        network_snapshot=network_snapshot,
    )

    # Compatibility callers bypass the HTTP submission route. Register the
    # intent so timeline/A2A events retain their foreign-key-backed audit trail.
    from api.store import get_store

    store = get_store()
    if store.get(intent_id) is None:
        store.create(
            intent_id=intent_id,
            text=user_input,
            session_id=user_intent.session_id,
            context=context or {},
        )

    # 初始化编排器并执行工作流
    orch = IDNOrchestrator(config={"llm": llm} if llm is not None else None)
    await orch.initialize()
    try:
        final_state = await orch.run(state)
    finally:
        # This compatibility entry point is used for bounded one-shot runs,
        # unlike the long-lived FastAPI lifespan. Close shared HTTP pools so
        # command-line callers and integration tests do not leak sessions.
        await orch.close()
        from api._probe_session import close_probe_session
        from mcp_servers.onos_server import get_onos_client

        await close_probe_session()
        await get_onos_client().close()

    # ── 以下代码将 GlobalAgentState 转换为 pipeline 兼容的 dict 格式 ──

    # 提取流表计划（兼容 Pydantic 模型和普通 dict）
    flow_plan = final_state.flow_table_plan or {}
    flow_table_plan = (
        flow_plan
        if isinstance(flow_plan, dict)
        else getattr(flow_plan, "model_dump", lambda: {})()
    )
    write_requests = (
        flow_table_plan.get("write_requests", [])
        if isinstance(flow_table_plan, dict)
        else []
    )
    execution_result = final_state.execution_result or {}
    safety_report = final_state.safety_council_report or {}
    verification_report = final_state.verification_report or {}

    intent_plan = final_state.intent_plan
    yang_draft = final_state.yang_draft
    policy_results = final_state.policy_results or []
    conflict_report = final_state.conflict_report

    # 提取策略摘要信息（兼容旧格式，需处理嵌套属性链）
    policy_name = (
        getattr(getattr(yang_draft, "network_policy", None), "policy_name", None)
        if yang_draft
        else None
    )
    action_type = None
    bandwidth_mbps = None
    if yang_draft and hasattr(yang_draft, "network_policy"):
        np_ = yang_draft.network_policy
        if hasattr(np_, "action"):
            # ActionType 可能是 enum，需提取 .value
            action_type = getattr(np_.action, "action_type", None)
            action_type = getattr(action_type, "value", action_type)
            qos = getattr(np_.action, "qos_params", None)
            if qos:
                bandwidth_mbps = getattr(qos, "bandwidth", None)

    # 统计下发的流表项总数
    total_entries = (
        sum(len(wr.get("table_entries", [])) for wr in write_requests)
        if write_requests
        else 0
    )

    # 判定最终状态：SUCCESS → VERIFICATION_FAILED → FAILED
    final_status = (
        "SUCCESS"
        if (
            verification_report.get("passed")
            and execution_result.get("status") == "SUCCESS"
        )
        else (
            "VERIFICATION_FAILED"
            if (
                execution_result.get("status") == "SUCCESS"
                and not verification_report.get("passed")
            )
            else (
                str(getattr(final_state, "current_status", "ERROR"))
                .split(".")[-1]
                .upper()
                if hasattr(getattr(final_state, "current_status", None), "value")
                else "ERROR"
            )
        )
    )

    commands = _format_flow_commands(write_requests)

    return {
        "input": user_input,
        "final_status": final_status,
        "summary": {
            "final_status": final_status,
            "policy_name": policy_name,
            "action_type": action_type,
            "bandwidth_mbps": bandwidth_mbps,
            "flow_entries": total_entries,
            "safety_decision": safety_report.get("overall_decision"),
            "safety_risk_score": safety_report.get("overall_risk_score"),
            "write_success": execution_result.get("status") == "SUCCESS",
            "intent_achieved": verification_report.get("passed", False),
        },
        "network_snapshot": (
            getattr(final_state.network_snapshot, "model_dump", lambda: {})()
            if final_state.network_snapshot
            else None
        ),
        "intent_plan": (
            getattr(intent_plan, "to_envelope", lambda: None)() if intent_plan else None
        ),
        "yang_policy": (
            getattr(yang_draft, "model_dump", lambda: None)() if yang_draft else None
        ),
        "policy_conflict_report": (
            getattr(conflict_report, "model_dump", lambda: None)()
            if conflict_report
            else None
        ),
        "flow_table_plan": flow_table_plan,
        "flow_table_commands": commands,
        "rule_verification_report": (
            getattr(
                getattr(final_state, "rule_verification_report", None),
                "model_dump",
                lambda: None,
            )()
            if hasattr(final_state, "rule_verification_report")
            else None
        ),
        "safety_council_report": safety_report,
        "execution_result": execution_result,
        "status_verification_report": verification_report,
        "policy_results": policy_results,
    }


def _format_flow_commands(write_requests: list) -> list[str]:
    """将 WriteRequest 序列转换为可读的 P4Runtime 风格命令字符串。

    用于 demo 输出和前端流表展示，使非 P4 专家也能理解下发了哪些规则。

    输出格式示例：
    "WRITE device:bmv2:1 | TABLE MyIngress.ipv4_routing | MATCH hdr.ipv4.dstAddr lpm 10.0.0.0/8 | ACTION MyIngress.set_nhop(port=2) | PRIORITY 100"

    Args:
        write_requests: WriteRequest dict 列表

    Returns:
        可读的命令字符串列表
    """
    commands: list[str] = []
    for request in write_requests:
        if not isinstance(request, dict):
            continue
        device_id = request.get("device_id", "?")
        # 遍历该设备的所有流表项
        for entry in request.get("table_entries", []):
            if not isinstance(entry, dict):
                continue
            parts = [
                f"WRITE {device_id}",
                f"TABLE {entry.get('table_name', '?')}",
            ]
            # 构建 MATCH 子句
            matches = []
            for field in entry.get("match_fields", []):
                if not isinstance(field, dict):
                    continue
                value = field.get("value", "")
                match_type = field.get("match_type", "exact")
                prefix_len = field.get("prefix_len")
                # lpm 匹配类型需显示前缀长度
                if prefix_len is not None:
                    value = f"{value}/{prefix_len}"
                matches.append(f"{field.get('field_name', '?')} {match_type} {value}")
            if matches:
                parts.append("MATCH " + "; ".join(matches))
            # 构建 ACTION 子句
            params = ", ".join(
                f"{p.get('param_name', '?')}={p.get('value', '?')}"
                for p in entry.get("action_params", [])
            )
            parts.append(f"ACTION {entry.get('action_name', '?')}({params})")
            parts.append(f"PRIORITY {entry.get('priority', '?')}")
            commands.append(" | ".join(parts))
    return commands

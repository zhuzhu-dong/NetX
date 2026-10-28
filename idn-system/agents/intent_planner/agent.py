"""
agents/intent_planner/agent.py
===============================

意图规划 Agent —— IDN-System 的语义入口。

# 系统角色
IntentPlannerAgent 将自然语言网络意图转换为结构化的 IntentPlan JSON。
这是整个 STAV 流水线的第一个智能体，负责"理解用户想要什么"。

# 双路径解析架构
Agent 内部有两条独立解析路径，最终合并：
1. LLM 语义路径：利用大模型进行语义理解和多意图拆分
2. 确定性规则路径：对 IP 地址、端口、协议、动作类型、带宽等可验证字段
   进行正则匹配和启发式规则提取，不依赖 LLM

合并策略：规则路径的提取结果覆盖 LLM 路径的对应字段
（规则确定性 > LLM 概率性），冲突时记录到 critic_report。

# 关键设计
- 澄清机制：当意图模糊时生成 clarification_questions，
  支持内联澄清（用户在同一输入中补充说明）和外部澄清（多轮对话）
- 命名业务参数：仅从 ServiceProfile 注册表填充协议、端口、端点和 DSCP
- Watcher 自愈路径：KPI Watcher 触发时使用结构化自愈上下文
  构造计划，跳过 LLM 调用

# 主要依赖
- agents/base_agent.py：LLM 和 MCP 客户端基类
- agents/intent_planner/ip_extractor.py：IP 地址和带宽提取
- agents/intent_planner/action_keywords.py：动作类型关键词识别
- agents/intent_planner/site_aliases.py：站点别名映射
- utils/llm_json.py：从 LLM 输出中提取 JSON 对象

# 与其他模块交互
- orchestrator/nodes/think_node.py：调用 plan() 方法
- agents/policy_modeling.py：消费生成的 IntentPlan
"""

from __future__ import annotations

import json
import logging
import re
from ipaddress import ip_network
from pathlib import Path
from typing import Any, Callable, Optional

from agents.base_agent import BaseAgent
from agents.llm_client import LLMInvocationError, invoke_structured_llm
from agents.intent_planner.action_keywords import (
    ACTION_KEYWORDS,
    has_named_application,
    has_explicit_dscp,
    infer_action_type,
    infer_priority,
    infer_traffic,
    mentions_video_service,
)

# NLP 提取模块 —— 独立抽取以便于单元测试
from agents.intent_planner.ip_extractor import (
    chinese_number,
    extract_bandwidth_mbps,
    extract_directional_ip_roles,
    extract_ip_prefixes,
    extract_latency_ms,
    normalize_cidr,
)
from agents.intent_planner.site_aliases import (
    add_alias_site,
    lookup_alias,
    lookup_alias_by_order,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from models.intent_plan import IntentPlan, SubIntent
from models.network import NetworkSnapshot
from models.yang_policy import ActionType, Protocol
from utils.llm_json import extract_json_object

logger = logging.getLogger(__name__)

# 站点别名配置文件路径（相对于项目根目录）
SITE_ALIASES_PATH = Path(__file__).resolve().parents[2] / "config" / "site_aliases.json"

# ── LLM 系统提示词 ──────────────────────────────────────────────────────────
# 指导 LLM 生成符合预期 JSON Schema 的 IntentPlan
INTENT_PLANNER_SYSTEM_PROMPT = """
# IntentPlannerAgent Prompt

## Role

You are the first planning agent in an intent-driven autonomous network
system. Translate one natural-language request into a JSON intent plan. Do not
generate YANGPolicy, P4Runtime table entries, paths, or device commands.

## Output Contract

Return only one JSON object with this shape:

{
  "intent_plan": {
    "raw_intent": "Guarantee a registered service bandwidth.",
    "sub_intents": [
      {
        "sequence": 1,
        "objective": "Guarantee registered service bandwidth",
        "service_id": null,
        "validation_profile_id": null,
        "intent_type": "SET_PRIORITY",
        "match": {
          "source_ip": null,
          "destination_ip": null,
          "protocol": "ANY",
          "destination_port": null
        },
        "action": {
          "action_type": "SET_PRIORITY",
          "qos_params": {
            "dscp_value": null,
            "bandwidth": 100,
            "burst_size": 1024
          }
        },
        "constraints": {
          "policy_name": "video-qos",
          "bandwidth_mbps": 100
        },
        "priority": 100
      }
    ],
    "confidence_score": 0.95,
    "parse_warnings": [],
    "clarification_questions": []
  }
}

## Requirements

- Split independent business requirements into separate `sub_intents`.
- Allowed action values: FORWARD, MIRROR, RATE_LIMIT, DROP, SET_PRIORITY.
- Allowed protocol values: TCP, UDP, ICMP, ANY.
- Express bandwidth as integer Mbps.
- Never invent service protocol, port, DSCP, or endpoint defaults.  Emit the
  service_id only when it is an exact ID from Runtime context known_services;
  otherwise emit null. Let the deterministic ServiceProfile registry fill
  protocol, port, DSCP, and endpoint selectors.
- For Chinese input, write objectives, warnings, and clarification questions in Chinese.
- Output ONLY the JSON object. No explanations, no markdown fences, no <think> tags.
"""


class IntentPlannerAgent(BaseAgent):
    """自然语言 → 合并与标准化的 JSON IntentPlan。

    此类是 IDN-System 中"理解"网络意图的核心组件。
    通过 LLM 语义理解 + 确定性规则提取的双路径架构，
    确保关键网络字段（IP/端口/协议/带宽）的准确性。

    # 关键设计决策
    - 规则路径不受 LLM 幻觉影响，保证 IP/端口/带宽字段的可靠性
    - LLM 路径负责语义拆解（多意图拆分、业务目标理解）
    - 合并时规则路径覆盖 LLM 路径（规则确定性 > LLM 概率性）
    - 所有命名业务参数均来自配置化 ServiceProfile，不在规划器中保存业务常量
    """

    agent_id = "intent_planner"
    description = (
        "Plan network intent using LLM semantics and deterministic field extraction"
    )

    async def plan(
        self,
        user_input: str,
        network_snapshot: Optional[NetworkSnapshot] = None,
        context: Optional[dict[str, Any]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        intent_id: Optional[str] = None,
        revision: int = 1,
        trace_id: Optional[str] = None,
    ) -> IntentPlan:
        """解析自然语言意图为 IntentPlan。

        执行流程：
        1. 提取内联澄清信息（如用户输入中 "补充说明: ..."）
        2. 检查是否为 Watcher 自愈上下文（跳过 LLM，直接用结构化上下文构造）
        3. 第一次 LLM + 规则双路径规划
        4. 如果需要澄清且有上下文，用澄清后的输入重试一次

        Args:
            user_input: 用户自然语言输入
            network_snapshot: 可选的网络拓扑快照
            context: 可选的附加上下文（对话历史、偏好等）
            on_token: 可选的 token 回调函数（用于 SSE 流式推送）

        Returns:
            包含子意图列表和置信度的 IntentPlan

        Raises:
            ValueError: user_input 为空
        """
        if not user_input or not user_input.strip():
            raise ValueError("user_input must not be empty")

        # ── Step 1: 提取内联澄清 ──
        base_input, inline_clarifications = self._extract_inline_clarifications(
            user_input
        )
        effective_context = self._merge_inline_clarifications(
            context, inline_clarifications
        )

        # ── Step 2: Watcher 自愈路径（不使用 LLM）──
        if self._is_watcher_self_healing_context(effective_context):
            return self._watcher_self_healing_plan(base_input, effective_context)

        # ── Step 3: 第一次双路径规划 ──
        initial_plan = await self._plan_once(
            base_input,
            network_snapshot,
            effective_context,
            on_token,
            intent_id=intent_id,
            revision=revision,
            trace_id=trace_id,
        )

        # ── Step 4: 澄清重试 ──
        if not self._should_use_clarification_context(initial_plan, effective_context):
            return initial_plan

        clarified_input = self._compose_clarified_input(base_input, effective_context)
        if clarified_input == base_input:
            return initial_plan
        return await self._plan_once(
            clarified_input,
            network_snapshot,
            effective_context,
            on_token,
            intent_id=intent_id,
            revision=revision,
            trace_id=trace_id,
        )

    async def _plan_once(
        self,
        user_input: str,
        network_snapshot: Optional[NetworkSnapshot],
        context: Optional[dict[str, Any]],
        on_token: Optional[Callable[[str], None]] = None,
        *,
        intent_id: Optional[str] = None,
        revision: int = 1,
        trace_id: Optional[str] = None,
    ) -> IntentPlan:
        """单次双路径规划（LLM + 规则）并合并结果。

        LLM 路径失败时静默降级为纯规则路径 —— 这是 IDN-System
        的重要降级策略，确保即使大模型服务不可用也能提供基础功能。

        Args:
            user_input: 用户自然语言输入
            network_snapshot: 网络快照
            context: 上下文
            on_token: token 回调

        Returns:
            合并后的 IntentPlan
        """
        # 先执行确定性规则路径（不依赖网络，可离线完成）
        rule_plan = self._rule_intent_plan(user_input, network_snapshot, context)
        try:
            llm_plan = await self._llm_intent_plan(
                user_input,
                network_snapshot,
                context,
                on_token,
                intent_id=intent_id,
                revision=revision,
                trace_id=trace_id,
            )
        except Exception as exc:
            # ── LLM 降级：直接使用规则路径结果 ──
            code = (
                exc.code
                if isinstance(exc, LLMInvocationError)
                else "LLM_PROVIDER_ERROR"
            )
            record = exc.record if isinstance(exc, LLMInvocationError) else None
            logger.warning(
                "[%s] LLM planning failed; deterministic fallback code=%s",
                self.agent_id,
                code,
            )
            if on_token:
                try:
                    on_token(f"\n[规则解析完成，未使用 LLM]")
                except Exception:
                    pass
            degraded = self._finalize_plan(rule_plan, user_input)
            critic = dict(degraded.critic_report or {})
            critic.update(
                {
                    "mode": "deterministic_fallback",
                    "llm_status": code,
                    "llm_runtime_mode": "DETERMINISTIC_FALLBACK",
                    "llm_call_id": record.call_id if record else None,
                    "llm_provider": record.provider if record else None,
                    "llm_model": record.model if record else None,
                    "structured_output_valid": (
                        record.structured_output_valid if record else False
                    ),
                    "validator_result": (
                        record.validator_result if record else "NOT_RUN"
                    ),
                    "fallback_used": True,
                    "llm_error": code,
                    "final_authority": "schema_validator_and_rule_extractor",
                }
            )
            return degraded.model_copy(
                update={
                    "critic_report": critic,
                    "parse_warnings": self._dedupe(
                        list(degraded.parse_warnings)
                        + [
                            f"{code}: semantic interpretation used deterministic fallback"
                        ]
                    ),
                }
            )

        # 合并两条路径的结果
        return self._merge_plans(llm_plan, rule_plan, user_input)

    async def request_clarification(self, user_input: str) -> str:
        """当意图模糊时生成澄清问题。

        Args:
            user_input: 用户原始输入

        Returns:
            用中文表述的澄清问题
        """
        plan = self._rule_intent_plan(user_input, None, None)
        if plan.clarification_questions:
            return " ".join(plan.clarification_questions)
        return "请明确源对象、目的对象、业务类型以及需要保障或限制的指标。"

    # ═══════════════════════════════════════════════════════════════════════════
    # Watcher 自愈路径 —— KPI 监控触发的自动修复
    # ═══════════════════════════════════════════════════════════════════════════

    def _is_watcher_self_healing_context(
        self, context: Optional[dict[str, Any]]
    ) -> bool:
        """判断是否为 KPI Watcher 触发的自愈请求。

        Watcher 检测到 KPI 异常后，构造包含结构化自愈参数的上下文，
        此时不需要 LLM 参与语义理解——直接用参数构造 IntentPlan。
        """
        if not context:
            return False
        healing = context.get("self_healing") or {}
        return (
            context.get("triggered_by") == "kpi_watcher"
            and bool(healing.get("enabled"))
            and bool(healing.get("autonomous", True))
        )

    def _watcher_self_healing_plan(
        self,
        user_input: str,
        context: Optional[dict[str, Any]],
    ) -> IntentPlan:
        """从 Watcher 结构化上下文直接构造自愈 IntentPlan。

        不经 LLM，完全基于确定性参数生成策略：
        - 目标流：从 target_flow 提取源/目的 IP
        - 修复动作：从 repair_actions 列表获取
        - 约束：标记 autonomous_self_healing=True，跳过人工审批
        """
        context = context or {}
        target_flow = context.get("target_flow") or {}
        healing = context.get("self_healing") or {}
        from api.service_profiles import get_service_profile, list_service_profiles

        service_id = str(target_flow.get("service_id") or "").strip()
        if not service_id:
            source = self._normalize_cidr(target_flow.get("source_ip"))
            destination = self._normalize_cidr(target_flow.get("destination_ip"))
            candidates = [
                profile
                for profile in list_service_profiles()
                if self._normalize_cidr(profile.src_ip) == source
                and self._normalize_cidr(profile.dst_ip) == destination
                and profile.protocol.value != "ICMP"
            ]
            if len(candidates) == 1:
                service_id = candidates[0].service_id
        if not service_id:
            raise ValueError(
                "watcher self-healing context requires target_flow.service_id"
            )
        service_profile = get_service_profile(service_id)
        # A watcher repair is an execution-capable path.  Its target selector
        # must therefore originate in the structured incident context, not in
        # the ServiceProfile's canonical/validation endpoint pair.  Profiles
        # describe a service, but are not an authority to invent the flow that
        # an incident should repair.
        source_ip = self._normalize_cidr(target_flow.get("source_ip"))
        destination_ip = self._normalize_cidr(target_flow.get("destination_ip"))
        if not source_ip or not destination_ip:
            raise ValueError(
                "watcher self-healing context requires target_flow.source_ip "
                "and target_flow.destination_ip"
            )
        configured_minimum = service_profile.validation_thresholds.get(
            "minimum_received_mbps"
        )
        bandwidth = self._normalize_int(
            healing.get("min_throughput_mbps")
            or configured_minimum
            or service_profile.default_offered_rate_mbps,
            1,
        )
        if bandwidth is None:
            raise ValueError(
                f"service {service_id} has no bandwidth objective for self-healing"
            )
        repair_actions = list(
            healing.get("repair_actions") or ["reroute_constrained_path"]
        )

        return IntentPlan(
            raw_intent=user_input,
            sub_intents=[
                SubIntent(
                    sequence=1,
                    objective="Watcher 检测到 KPI 异常，执行自治拥塞修复并验证目标业务恢复。",
                    intent_type=ActionType.SET_PRIORITY.value,
                    match={
                        "source_ip": source_ip,
                        "destination_ip": destination_ip,
                        "protocol": service_profile.protocol.value,
                        "destination_port": service_profile.dst_port,
                    },
                    action={
                        "action_type": ActionType.SET_PRIORITY.value,
                        "qos_params": {
                            "dscp_value": service_profile.dscp,
                            "bandwidth": bandwidth,
                            "burst_size": 1024,
                        },
                    },
                    constraints={
                        "policy_name": f"watcher-{service_profile.service_id}-self-healing",
                        "bandwidth_mbps": bandwidth,
                        "dscp_value": service_profile.dscp,
                        "autonomous_self_healing": True,
                        "triggered_by": "kpi_watcher",
                        "incident_id": context.get("incident_id"),
                        "target_path": healing.get("target_path") or [],
                        "repair_actions": repair_actions,
                        "low_priority_source": healing.get("low_priority_source") or {},
                    },
                    priority=50,
                    service_id=service_profile.service_id,
                    service_profile_snapshot=service_profile.model_dump(mode="json"),
                    validation_profile_id=service_profile.service_id,
                )
            ],
            confidence_score=0.9,
            parse_warnings=[
                (
                    "Watcher 内部意图按结构化 self_healing 上下文执行自治修复；"
                    "不进入人工澄清流程。"
                )
            ],
            clarification_questions=[],
            critic_report={
                "mode": "watcher_self_healing_contract",
                "conflict": False,
                "missing_slots": [],
                "final_authority": "kpi_watcher_context_and_schema_validator",
                "repair_actions": repair_actions,
            },
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # 内联澄清 —— 支持用户在单次输入中补充说明
    # ═══════════════════════════════════════════════════════════════════════════

    def _extract_inline_clarifications(self, user_input: str) -> tuple[str, list[str]]:
        """从用户输入中分离内联澄清信息。

        支持格式："用户补充说明：....."（以行为单位）
        用于在初始意图模糊时，用户在同一消息中补充细节。
        """
        base_lines: list[str] = []
        clarification_answers: list[str] = []
        for line in str(user_input).splitlines():
            stripped = line.strip()
            match = re.match(r"^用户补充说明\s*[:：]\s*(.+)$", stripped)
            if match:
                clarification_answers.append(match.group(1).strip())
            elif stripped:
                base_lines.append(stripped)
        base_input = " ".join(base_lines).strip() or user_input.strip()
        return base_input, self._dedupe(clarification_answers)

    def _merge_inline_clarifications(
        self,
        context: Optional[dict[str, Any]],
        answers: list[str],
    ) -> Optional[dict[str, Any]]:
        if not answers:
            return context
        merged = dict(context or {})
        clarifications = list(merged.get("clarifications") or [])
        for answer in answers:
            clarifications.append({"answer": answer, "source": "inline_transcript"})
        merged["clarifications"] = clarifications
        return merged

    def _should_use_clarification_context(
        self,
        plan: IntentPlan,
        context: Optional[dict[str, Any]],
    ) -> bool:
        if not self._clarification_answers(context):
            return False
        return bool(plan.clarification_questions or self._missing_slots(plan))

    def _compose_clarified_input(
        self,
        base_input: str,
        context: Optional[dict[str, Any]],
    ) -> str:
        answers = self._clarification_answers(context)
        if not answers:
            return base_input
        return f"{base_input} 补充信息：{'；'.join(answers)}"

    def _clarification_answers(self, context: Optional[dict[str, Any]]) -> list[str]:
        if not context:
            return []
        answers: list[str] = []
        for item in context.get("clarifications") or []:
            if isinstance(item, dict):
                value = item.get("answer") or item.get("text") or item.get("content")
            else:
                value = item
            if value:
                answers.append(str(value))
        for key in ("latest_clarification_answer", "clarification_answer"):
            if context.get(key):
                answers.append(str(context[key]))
        return self._dedupe(answers)

    def _missing_slots(self, plan: IntentPlan) -> list[str]:
        missing: list[str] = []
        critic = plan.critic_report or {}
        missing.extend(critic.get("missing_slots") or [])
        for item in plan.sub_intents:
            slot = (item.constraints or {}).get("slot_filling") or {}
            missing.extend(slot.get("missing_slots") or [])
        return self._dedupe(missing)

    # ═══════════════════════════════════════════════════════════════════════════
    # LLM 路径 —— 语义理解和多意图拆分
    # ═══════════════════════════════════════════════════════════════════════════

    async def _llm_intent_plan(
        self,
        user_input: str,
        network_snapshot: Optional[NetworkSnapshot],
        context: Optional[dict[str, Any]],
        on_token: Optional[Callable[[str], None]] = None,
        *,
        intent_id: Optional[str] = None,
        revision: int = 1,
        trace_id: Optional[str] = None,
    ) -> IntentPlan:
        """LLM 语义路径：将自然语言转换为粗粒度的 IntentPlan。

        构建运行时上下文（已知主机列表、设备/链路数量），
        组装 SystemMessage + HumanMessage，调用 LLM 生成 JSON。

        支持流式 Token 输出（通过 on_token 回调连接 SSE 推送）。

        Args:
            user_input: 用户自然语言
            network_snapshot: 网络快照
            context: 上下文
            on_token: SSE token 回调

        Returns:
            粗粒度 IntentPlan（之后会与规则路径合并）

        Raises:
            ValueError: LLM 输出不包含 intent_plan
        """
        runtime_context = self._build_runtime_context(network_snapshot, context)
        messages = [
            SystemMessage(
                content=f"{INTENT_PLANNER_SYSTEM_PROMPT}\n\n{runtime_context}"
            ),
            HumanMessage(content=f"用户意图: {user_input}"),
        ]

        def validate(payload: dict[str, Any]) -> IntentPlan:
            if "intent_plan" not in payload and "sub_intents" not in payload:
                raise ValueError("LLM output does not contain intent_plan")
            return self._finalize_plan(
                payload.get("intent_plan") or payload,
                user_input,
                require_items=True,
                validate_fields=False,
            )

        invocation = await invoke_structured_llm(
            messages,
            agent="intent_understanding",
            intent_id=intent_id,
            revision=revision,
            trace_id=trace_id,
            input_refs=[intent_id] if intent_id else [],
            input_schema_version="NaturalLanguageIntentInput.v1",
            output_schema_version="IntentPlanSemanticOutput.v1",
            validator=validate,
            llm=self.llm,
            fallback_on_error=True,
            on_token=on_token,
        )
        critic = dict(invocation.value.critic_report or {})
        critic.update(
            {
                "llm_status": "SUCCESS",
                "llm_runtime_mode": invocation.record.runtime_mode.value,
                "llm_call_id": invocation.record.call_id,
                "llm_provider": invocation.record.provider,
                "llm_model": invocation.record.model,
                "structured_output_valid": True,
                "validator_result": invocation.record.validator_result,
                "fallback_used": False,
            }
        )
        return invocation.value.model_copy(update={"critic_report": critic})

    # ═══════════════════════════════════════════════════════════════════════════
    # 确定性规则路径 —— IP/端口/协议/带宽的可验证提取
    # ═══════════════════════════════════════════════════════════════════════════

    def _rule_intent_plan(
        self,
        user_input: str,
        network_snapshot: Optional[NetworkSnapshot],
        context: Optional[dict[str, Any]],
    ) -> IntentPlan:
        """确定性规则路径：用正则和启发式规则提取可验证字段。

        不依赖 LLM，完全基于模式匹配。提取的内容包括：
        - IP 地址/CIDR 前缀
        - 方向性 IP 角色（来自 X 到 Y）
        - 动作类型（DROP/RATE_LIMIT/SET_PRIORITY/FORWARD）
        - 协议/端口/DSCP/VLAN
        - 带宽值（Mbps）
        - 站点别名映射
        """
        aliases = self._build_alias_map(network_snapshot, context)
        # 按分号、换行、同时/并且等分隔符拆分多个子意图
        clauses = self._split_sub_intents(user_input)
        sub_intents: list[SubIntent] = []
        warnings: list[str] = []
        questions: list[str] = []
        confidence_scores: list[float] = []

        for sequence, clause in enumerate(clauses, start=1):
            item, item_warnings, item_questions, confidence = self._rule_sub_intent(
                clause, sequence, aliases
            )
            sub_intents.append(item)
            warnings.extend(item_warnings)
            questions.extend(item_questions)
            confidence_scores.append(confidence)

        # A compound request can state one explicit parent scope followed by
        # parallel objectives ("for X, guarantee A and cap B").  The split
        # child which contains only B must inherit that *same user-provided*
        # scope, never a ServiceProfile, active policy, binding, or history.
        # A child with its own selector/service remains authoritative.
        if len(sub_intents) > 1:
            parent, _, _, _ = self._rule_sub_intent(user_input, 1, aliases)
            parent_match = dict(parent.match or {})
            parent_service = parent.service_id
            if (
                parent_match.get("source_ip")
                and parent_match.get("destination_ip")
                and parent_service
            ):
                parent_endpoints = dict(
                    (parent.constraints or {}).get("endpoint_resolution") or {}
                )
                for item in sub_intents:
                    # A child that explicitly names a different registered
                    # service owns its own semantic scope.  It must never
                    # inherit the parent's endpoints merely because the rule
                    # splitter has not yet hydrated its service_id.
                    from agents.semantic_resolver import ServiceSemanticResolver
                    from models.semantic_resolution import ResolutionStatus

                    child_semantics = ServiceSemanticResolver().resolve(
                        str(item.objective or "")
                    )
                    if (
                        child_semantics.status == ResolutionStatus.RESOLVED
                        and child_semantics.selected_service_id != parent_service
                    ):
                        continue
                    match = dict(item.match or {})
                    inherited = []
                    for field in ("source_ip", "destination_ip"):
                        if not match.get(field):
                            match[field] = parent_match[field]
                            inherited.append(field)
                    if not item.service_id:
                        item.service_id = parent_service
                        item.validation_profile_id = parent.validation_profile_id
                        inherited.append("service_id")
                    if not inherited:
                        continue
                    constraints = dict(item.constraints or {})
                    provenance = dict(constraints.get("field_provenance") or {})
                    for field in inherited:
                        provenance[field] = "COMPOUND_PARENT_EXPLICIT_SCOPE"
                    constraints["field_provenance"] = provenance
                    constraints["compound_parent_scope_context"] = {
                        "source_ip": parent_match["source_ip"],
                        "destination_ip": parent_match["destination_ip"],
                        "service_id": parent_service,
                        "provenance": "COMPOUND_PARENT_EXPLICIT_SCOPE",
                    }
                    constraints["endpoint_resolution"] = parent_endpoints
                    item.match = match
                    item.constraints = constraints
                # Recompute structural questions after the only permitted
                # scope inheritance; stale child-local questions are invalid.
                questions = [question for item in sub_intents for question in self._slot_questions(item)]

        # 整体置信度取所有子意图的最低值
        confidence = min(confidence_scores) if confidence_scores else 0.0
        # 存在问题则降低置信度上限
        if questions:
            confidence = min(confidence, 0.65)
        # Keep the plan-level evidence contract in sync with the individual
        # rule-extracted clauses.  Later semantic enrichment starts from this
        # mapping, so dropping it here can incorrectly downgrade an explicit
        # protocol/port (for example UDP/5004) to a profile default.
        # Per-item provenance remains authoritative for compound requests;
        # this aggregate only carries fields that have rule evidence.
        field_provenance: dict[str, str] = {}
        for item in sub_intents:
            field_provenance.update(
                {
                    str(field): str(origin)
                    for field, origin in dict(item.constraints.get("field_provenance") or {}).items()
                    if origin
                }
            )
        return IntentPlan(
            raw_intent=user_input,
            sub_intents=sub_intents,
            confidence_score=round(confidence, 2),
            parse_warnings=self._dedupe(warnings),
            clarification_questions=self._dedupe(questions),
            field_provenance=field_provenance,
        )

    def _rule_sub_intent(
        self,
        text: str,
        sequence: int,
        aliases: dict[str, dict[str, str]],
    ) -> tuple[SubIntent, list[str], list[str], float]:
        """对单个子意图文本执行确定性规则提取。

        【难点解释】IP 角色分配的默认策略是一个重要的启发式规则：
        当用户没有明确"来源"和"目的"时（如只说"阻断 10.0.0.50"）：
        - DROP/阻断 → 默认将 IP 分配给 destination（阻断去往该 IP 的流量）
        - 其他动作 → 默认将 IP 分配给 source（保障来自该 IP 的流量）
        这基于网络运维的常识——阻断通常是阻断去向目标，保障通常是保障来源。

        Returns:
            (SubIntent, warnings, questions, confidence)
        """
        lowered = text.lower()
        from agents.semantic_resolver import EndpointResolver, ServiceSemanticResolver
        from models.semantic_resolution import FieldProvenance, ResolutionStatus

        endpoint_resolution = EndpointResolver().resolve_text(text)
        ips = self._extract_ip_prefixes(text)
        directional_source, directional_destination = (
            self._extract_directional_ip_roles(text)
        )
        action_type, action_matched = self._infer_action_type(lowered)

        # ── IP 角色默认分配策略 ─────────────────────────────────────────
        # 当无方向词（无"来自 X 到 Y"）且只有一个 IP 时：
        #   DROP / 阻断 → 默认分配给 destination（阻断去往该 IP 的流量）
        #   其他动作     → 默认分配给 source（保障来自该 IP 的流量）
        if not directional_source and not directional_destination and len(ips) == 1:
            if action_type == ActionType.DROP:
                directional_destination = ips[0]
            else:
                directional_source = ips[0]

        # 从剩余 IP 列表中分配未赋值的 source/destination
        remaining = list(ips)
        explicit_source = directional_source
        explicit_destination = directional_destination
        if explicit_source and explicit_source in remaining:
            remaining.remove(explicit_source)
        if explicit_destination and explicit_destination in remaining:
            remaining.remove(explicit_destination)
        if explicit_source is None and remaining:
            explicit_source = remaining.pop(0)
        if explicit_destination is None and remaining:
            explicit_destination = remaining.pop(0)

        # 尝试通过站点别名解析未赋值的 source/destination
        alias_source = self._lookup_alias(text, aliases, "source")
        alias_destination = self._lookup_alias(text, aliases, "destination")
        source_ip = (
            explicit_source
            or endpoint_resolution.source.selector_prefix
            or alias_source
        )
        destination_ip = (
            explicit_destination
            or endpoint_resolution.destination.selector_prefix
            or alias_destination
        )

        # 提取协议、端口、DSCP、VLAN、带宽
        protocol, destination_port, dscp_value, traffic_assumption = (
            self._infer_traffic(text, lowered)
        )
        explicit_dscp = has_explicit_dscp(text)
        from api.service_profiles import get_service_profile

        service_resolution = ServiceSemanticResolver().resolve(
            text,
            source_endpoint=endpoint_resolution.source.endpoint_id,
            destination_endpoint=endpoint_resolution.destination.endpoint_id,
            source_ip=source_ip,
            destination_ip=destination_ip,
            protocol=protocol.value,
            destination_port=destination_port,
        )
        service_id = service_resolution.selected_service_id
        if not service_id:
            # A uniquely matched registered service is safe to carry through
            # planning even when its configured endpoints are about to be
            # hydrated.  This preserves the semantic identity of a named
            # clause so an unrelated LLM sub-intent cannot replace it during
            # the later index-aligned merge.
            candidate_ids = {
                str(candidate.service_id)
                for candidate in service_resolution.candidate_services
            }
            if len(candidate_ids) == 1:
                service_id = next(iter(candidate_ids))
        service_profile = get_service_profile(service_id) if service_id else None
        if protocol == Protocol.ANY and service_resolution.inferred_fields.get("protocol"):
            protocol = Protocol(service_resolution.inferred_fields["protocol"])
        if service_profile:
            if protocol == Protocol.ANY:
                protocol = Protocol(service_profile.protocol.value)
            if destination_port is None:
                destination_port = service_profile.dst_port
            if dscp_value is None:
                dscp_value = service_profile.dscp
        bandwidth = self._extract_bandwidth_mbps(text)
        vlan_id = self._extract_vlan_id(text)
        named_application = self._has_named_application(lowered)

        # ── 警告和澄清问题生成 ──
        warnings: list[str] = []
        questions: list[str] = []
        if traffic_assumption:
            warnings.append(f"假设: {traffic_assumption}")
        if not explicit_source and alias_source:
            warnings.append(f"假设: 源站点按配置映射为 {alias_source}")
        if not explicit_destination and alias_destination:
            warnings.append(f"假设: 目的站点按配置映射为 {alias_destination}")

        has_rate_selector = action_type == ActionType.RATE_LIMIT and any(
            [named_application, vlan_id, destination_port]
        )
        # DROP 可以仅指定 destination 而不指定 source（阻断从任何来源）
        if not source_ip and not has_rate_selector and action_type != ActionType.DROP:
            warnings.append("缺少源IP、源网段或源站点")
            questions.append("请指定策略的源对象，例如主机IP、源网段或站点名称。")
        if (
            not destination_ip
            and action_type != ActionType.RATE_LIMIT
            and action_type != ActionType.DROP
        ):
            warnings.append("缺少目的IP、目的网段或目的站点")
            questions.append("请指定策略的目的对象，例如服务器IP、目的网段或站点名称。")
        if (
            action_type in {ActionType.SET_PRIORITY, ActionType.RATE_LIMIT}
            and bandwidth is None
        ):
            warnings.append("缺少明确带宽约束")
            questions.append(
                "请明确带宽目标或限制阈值，例如不低于100Mbps或不超过20Mbps。"
            )
        if action_type == ActionType.RATE_LIMIT and not any(
            [source_ip, named_application, vlan_id, destination_port]
        ):
            warnings.append("限速对象不明确")
            questions.append("请指定要限制的源对象、应用类型、VLAN 或端口。")
        if ("优化" in text or "optimize" in lowered) and not (
            bandwidth or self._extract_latency_ms(text)
        ):
            warnings.append("优化目标不明确")
            questions.append(
                "请明确优化目标：保障带宽、降低时延、降低丢包率或提升优先级？"
            )
        if service_resolution.status == ResolutionStatus.AMBIGUOUS:
            questions.append(service_resolution.clarification_question().question)
        elif (
            service_resolution.status == ResolutionStatus.NOT_FOUND
            and service_resolution.matched_semantics
        ):
            questions.append(service_resolution.clarification_question().question)

        # ── 置信度计算：基于提取到的字段数量评估 ──
        # 每个成功提取的字段增加置信度，上限 0.95
        confidence = 0.45
        confidence += 0.15 if source_ip or action_type == ActionType.DROP else 0
        confidence += (
            0.15
            if destination_ip
            or action_type == ActionType.RATE_LIMIT
            or action_type == ActionType.DROP
            else 0
        )
        confidence += 0.10 if action_matched else 0
        confidence += (
            0.10 if bandwidth is not None or action_type == ActionType.DROP else 0
        )
        confidence += 0.05 if protocol != Protocol.ANY or named_application else 0
        confidence = min(confidence, 0.95)

        # ── 构造 QoS 参数 ──
        qos_params: Optional[dict[str, Any]] = None
        if action_type in {ActionType.SET_PRIORITY, ActionType.RATE_LIMIT}:
            qos_params = {
                "qos_mode": (
                    "RATE_LIMIT"
                    if action_type == ActionType.RATE_LIMIT
                    else "MIN_BANDWIDTH"
                ),
                "dscp_value": dscp_value,
                "bandwidth": bandwidth,
                "burst_size": (
                    1024 if dscp_value == 46 or bandwidth is not None else None
                ),
            }

        # ── 记录提取证据（供合并时决定哪些字段可安全覆盖 LLM 值）──
        evidence = {
            "source_ip": source_ip is not None,
            "destination_ip": destination_ip is not None,
            "protocol": protocol != Protocol.ANY,
            "destination_port": destination_port is not None,
            "vlan_id": vlan_id is not None,
            "action_type": action_matched,
            "dscp_value": dscp_value is not None,
            "bandwidth": bandwidth is not None,
            "burst_size": qos_params is not None
            and qos_params.get("burst_size") is not None,
        }
        constraints: dict[str, Any] = {
            "policy_name": self._make_policy_name(
                source_ip, destination_ip, action_type, lowered
            ),
            "_rule_evidence": evidence,
            "endpoint_resolution": endpoint_resolution.model_dump(mode="json"),
            "service_resolution": service_resolution.model_dump(mode="json"),
            "field_provenance": {
                "source_ip": (
                    FieldProvenance.USER_EXPLICIT.value
                    if explicit_source
                    else FieldProvenance.ENDPOINT_REGISTRY.value
                ),
                "destination_ip": (
                    FieldProvenance.USER_EXPLICIT.value
                    if explicit_destination
                    else FieldProvenance.ENDPOINT_REGISTRY.value
                ),
                "bandwidth_mbps": (
                    FieldProvenance.USER_EXPLICIT.value if bandwidth is not None else ""
                ),
                "dscp": (
                    FieldProvenance.USER_EXPLICIT.value
                    if explicit_dscp
                    else FieldProvenance.SERVICE_PROFILE.value
                    if service_profile
                    else FieldProvenance.SYSTEM_DEFAULT.value
                ),
                "dscp_value": (
                    FieldProvenance.USER_EXPLICIT.value
                    if explicit_dscp
                    else FieldProvenance.SERVICE_PROFILE.value
                    if service_profile
                    else FieldProvenance.SYSTEM_DEFAULT.value
                ),
                **service_resolution.field_provenance,
            },
        }
        if bandwidth is not None:
            constraints["bandwidth_mbps"] = bandwidth
        if dscp_value is not None:
            constraints["dscp_value"] = dscp_value

        return (
            SubIntent(
                sequence=sequence,
                objective=text.strip(),
                intent_type=action_type.value,
                match={
                    "source_ip": source_ip,
                    "destination_ip": destination_ip,
                    "protocol": protocol.value,
                    "destination_port": destination_port,
                    "vlan_id": vlan_id,
                },
                action={"action_type": action_type.value, "qos_params": qos_params},
                constraints=constraints,
                priority=self._infer_priority(text, action_type),
                service_id=service_id,
                service_profile_snapshot=(
                    service_profile.model_dump(mode="json") if service_profile else None
                ),
                validation_profile_id=service_id,
            ),
            warnings,
            questions,
            confidence,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # 双路径合并 —— LLM 语义 + 规则精确性
    # ═══════════════════════════════════════════════════════════════════════════

    def _merge_plans(
        self, llm_plan: IntentPlan, rule_plan: IntentPlan, raw_intent: str
    ) -> IntentPlan:
        """合并 LLM 路径和规则路径的 IntentPlan。

        合并策略：
        1. 逐子意图对齐（按 index 匹配）
        2. 对每个字段，如果有规则证据（_rule_evidence），规则值覆盖 LLM 值
        3. 记录冲突到 warn列表（供前端展示）
        4. 如果规则路径发现了 LLM 未拆分的子意图，追加到结果末尾

        Args:
            llm_plan: LLM 语义路径产出的 IntentPlan
            rule_plan: 确定性规则路径产出的 IntentPlan
            raw_intent: 原始用户输入

        Returns:
            合并并标准化后的最终 IntentPlan
        """
        merged_items: list[SubIntent] = []
        warnings = list(llm_plan.parse_warnings) + list(rule_plan.parse_warnings)
        questions = list(llm_plan.clarification_questions)
        conflicts: list[str] = []

        clarification_context = "补充信息" in str(raw_intent)

        # 逐子意图合并
        for index, llm_item in enumerate(llm_plan.sub_intents):
            rule_item = (
                rule_plan.sub_intents[index]
                if index < len(rule_plan.sub_intents)
                else None
            )
            # A resumed conversation is planned over the complete transcript.
            # Some models emit the final answer fragment (for example, just a
            # protocol/port/service label) as a second FORWARD sub-intent in
            # addition to the already complete original QoS request.  That
            # fragment has no independent selector or objective and therefore
            # is evidence for the original item, not another operation.  If
            # retained it can overwrite the complete item when IntentSpec is
            # projected, reopening source/destination clarification forever.
            #
            # This is deliberately structural rather than service/text
            # specific.  A real second operation is retained whenever it has
            # an endpoint pair or an explicit QoS objective.
            if (
                clarification_context
                and self._is_nonoperational_clarification_fragment(llm_item)
                and (
                    rule_item is None
                    or self._is_nonoperational_clarification_fragment(rule_item)
                )
            ):
                warnings.append(
                    "已将无独立目标的澄清回答片段并入原始意图。"
                )
                continue
            merged_item, item_conflicts = self._merge_sub_intent(llm_item, rule_item)
            merged_items.append(merged_item)
            conflicts.extend(item_conflicts)

        # 追加规则路径发现的额外子意图
        # Index alignment is with the original LLM list, not ``merged_items``:
        # clarification fragments above may intentionally be skipped.  Using
        # the shortened merged length would append that same rule-side
        # fragment again as if it were an LLM-missing operation.
        if len(rule_plan.sub_intents) > len(llm_plan.sub_intents):
            extras = rule_plan.sub_intents[len(llm_plan.sub_intents) :]
            if clarification_context:
                extras = [
                    item for item in extras
                    if not self._is_nonoperational_clarification_fragment(item)
                ]
            if extras:
                merged_items.extend(extras)
                warnings.append("规则解析器识别出 LLM 未拆分的附加子意图，已加入执行计划。")

        if not merged_items:
            return self._finalize_plan(rule_plan, raw_intent)

        warnings.extend(conflicts)
        provisional = IntentPlan(
            raw_intent=raw_intent,
            sub_intents=merged_items,
            confidence_score=llm_plan.confidence_score,
            parse_warnings=self._dedupe(warnings),
            clarification_questions=self._dedupe(questions),
        )
        # 最终标准化 + 槽位验证
        final_plan = self._finalize_plan(provisional, raw_intent)
        validation_questions = self._validate_plan(final_plan)
        # Keep semantic resolver questions as well as structural slot checks.
        # Applicability mismatches have complete source/destination slots, so
        # they are not rediscovered by _validate_plan; dropping them here would
        # incorrectly turn a candidate interpretation into an executable plan.
        all_questions = self._dedupe(
            [*final_plan.clarification_questions, *validation_questions]
        )
        confidence = final_plan.confidence_score
        if conflicts or all_questions:
            confidence = min(confidence, 0.65)
        critic_report = self._critic_report(
            llm_plan=llm_plan,
            rule_plan=rule_plan,
            final_plan=final_plan,
            conflicts=conflicts,
            questions=all_questions,
        )
        return final_plan.model_copy(
            update={
                "confidence_score": confidence,
                "clarification_questions": all_questions,
                "critic_report": critic_report,
            }
        )

    @staticmethod
    def _is_nonoperational_clarification_fragment(item: SubIntent) -> bool:
        """True only for a standalone answer fragment, never for an operation."""
        match = dict(item.match or {})
        action = dict(item.action or {})
        qos = dict(action.get("qos_params") or {})
        has_endpoint_pair = bool(match.get("source_ip") and match.get("destination_ip"))
        has_qos_objective = qos.get("bandwidth") not in (None, "")
        action_type = str(action.get("action_type") or item.intent_type or "").upper()
        return (
            action_type == ActionType.FORWARD.value
            and not has_endpoint_pair
            and not has_qos_objective
        )

    def _merge_sub_intent(
        self,
        llm_item: SubIntent,
        rule_item: Optional[SubIntent],
    ) -> tuple[SubIntent, list[str]]:
        """合并单个子意图的 LLM 值和规则值。

        核心原则：规则证据（_rule_evidence）中的 True 字段覆盖 LLM 值。
        如果 LLM 值已存在且与规则值不同，记录冲突告警。
        """
        if rule_item is None:
            return llm_item, []

        evidence = dict(rule_item.constraints.get("_rule_evidence") or {})
        match = dict(llm_item.match)
        conflicts: list[str] = []

        # ── 合并匹配字段（IP/端口/协议/VLAN）──
        for key in (
            "source_ip",
            "destination_ip",
            "protocol",
            "source_port",
            "destination_port",
            "vlan_id",
        ):
            value = rule_item.match.get(key)
            if value is None or not evidence.get(key, False):
                continue
            current = match.get(key)
            unspecified = current in (None, "", "null") or (
                key == "protocol"
                and self._normalize_protocol(current) == Protocol.ANY.value
            )
            if not unspecified and self._normalize_match_value(
                key, current
            ) != self._normalize_match_value(key, value):
                conflicts.append(
                    f"LLM 与规则解析器在 {key} 上存在冲突：LLM={current}, rule={value}，已采用规则值。"
                )
            match[key] = value

        # ── 合并动作类型 ──
        action = dict(llm_item.action)
        current_action = self._normalize_action_type(
            action.get("action_type") or llm_item.intent_type
        )
        rule_action = self._normalize_action_type(
            rule_item.action.get("action_type") or rule_item.intent_type
        )
        if evidence.get("action_type"):
            if current_action and current_action != rule_action:
                conflicts.append(
                    f"LLM 与规则解析器在 action_type 上存在冲突：LLM={current_action}, rule={rule_action}，已采用规则值。"
                )
            current_action = rule_action
        action["action_type"] = current_action or ActionType.FORWARD.value

        # ── 合并 QoS 参数（DSCP/带宽/突发）──
        qos = dict(action.get("qos_params") or {})
        rule_qos = dict(rule_item.action.get("qos_params") or {})
        for key in ("dscp_value", "bandwidth", "burst_size"):
            value = rule_qos.get(key)
            if value is None or not evidence.get(key, False):
                continue
            current = qos.get(key)
            if current not in (None, "", "null") and self._normalize_qos_value(
                key, current
            ) != self._normalize_qos_value(key, value):
                conflicts.append(
                    f"LLM 与规则解析器在 qos_params.{key} 上存在冲突：LLM={current}, rule={value}，已采用规则值。"
                )
            qos[key] = value
        # qos_mode is coupled to the action.  If a deterministic cap phrase
        # changes SET_PRIORITY into RATE_LIMIT, retaining the LLM's former
        # MIN_BANDWIDTH mode would create a self-contradictory policy.
        if action["action_type"] in {ActionType.SET_PRIORITY.value, ActionType.RATE_LIMIT.value}:
            rule_mode = rule_qos.get("qos_mode")
            expected_mode = (
                "RATE_LIMIT"
                if action["action_type"] == ActionType.RATE_LIMIT.value
                else "MIN_BANDWIDTH"
            )
            if rule_mode:
                expected_mode = str(rule_mode).upper()
            if qos.get("qos_mode") not in (None, expected_mode):
                conflicts.append(
                    "LLM 与规则解析器在 qos_params.qos_mode 上存在冲突，已采用规则动作语义。"
                )
            qos["qos_mode"] = expected_mode
        # DROP 动作不需要 QoS 参数
        action["qos_params"] = (
            None if action["action_type"] == ActionType.DROP.value else qos
        )

        # ── 合并约束（保留非内部字段）──
        constraints = {
            key: value
            for key, value in llm_item.constraints.items()
            if not str(key).startswith("_")
        }
        for key, value in rule_item.constraints.items():
            if not str(key).startswith("_") and key not in constraints:
                constraints[key] = value
        # Policy names are system-owned identifiers, not a user-controlled
        # semantic field.  Keeping an LLM-proposed label here can make a new
        # selector appear to belong to an unrelated active service (for
        # example an h_ai UDP/5004 research-video draft labelled
        # ``ai-data-qos``).  The deterministic rule clause derives its name
        # from the validated selector and must therefore be authoritative.
        rule_policy_name = str(rule_item.constraints.get("policy_name") or "").strip()
        if rule_policy_name:
            constraints["policy_name"] = rule_policy_name
        # Provenance is an authority record, not merely decorative metadata.
        # The rule clause is derived from the raw request, so it must replace
        # an LLM-provided default while retaining an actual USER_EXPLICIT DSCP.
        rule_provenance = dict(rule_item.constraints.get("field_provenance") or {})
        if rule_provenance:
            constraints["field_provenance"] = {
                **dict(constraints.get("field_provenance") or {}),
                **rule_provenance,
            }
        # Endpoint resolution is first-class semantic evidence.  Keep it even
        # when a clause has no named service so final normalization can merge
        # a fresh parse without allowing an LLM-truncated objective to erase a
        # selector that was deterministically resolved from user text.
        rule_endpoints = dict(rule_item.constraints.get("endpoint_resolution") or {})
        if rule_endpoints:
            constraints["endpoint_resolution"] = rule_endpoints
            constraints["endpoint_evidence_authority"] = (
                "DETERMINISTIC_CURRENT_TEXT"
            )
        if rule_item.service_id:
            # The rule clause was anchored to the user's own text.  Preserve
            # that identity and its resolver evidence explicitly; otherwise a
            # later hydration pass can re-derive a different service from a
            # hallucinated LLM objective string.
            constraints["service"] = rule_item.service_id
            constraints["service_resolution"] = dict(
                rule_item.constraints.get("service_resolution") or {}
            )
            constraints["service_authority"] = "RULE_EXTRACTOR"
        if qos.get("bandwidth") is not None:
            constraints["bandwidth_mbps"] = qos["bandwidth"]
        if qos.get("dscp_value") is not None:
            constraints["dscp_value"] = qos["dscp_value"]

        return (
            SubIntent(
                sub_intent_id=llm_item.sub_intent_id,
                sequence=llm_item.sequence,
                objective=llm_item.objective or rule_item.objective,
                intent_type=action["action_type"],
                match=match,
                action=action,
                constraints=constraints,
                priority=(
                    llm_item.priority
                    if llm_item.priority is not None
                    else rule_item.priority
                ),
                service_id=rule_item.service_id or llm_item.service_id,
                service_profile_snapshot=(
                    rule_item.service_profile_snapshot
                    or llm_item.service_profile_snapshot
                ),
                validation_profile_id=(
                    rule_item.validation_profile_id or llm_item.validation_profile_id
                ),
            ),
            conflicts,
        )

    def _finalize_plan(
        self,
        plan: IntentPlan | dict[str, Any],
        raw_intent: str,
        require_items: bool = False,
        validate_fields: bool = True,
    ) -> IntentPlan:
        """标准化和验证 IntentPlan。

        执行步骤：
        1. 标准化每个 SubIntent 的字段（CIDR、端口范围、协议名等）
        2. 从 ServiceProfile 解析命名业务参数
        3. 运行槽位验证（_validate_plan）
        4. 如有缺失字段，降低置信度并生成澄清问题
        """
        data = plan.model_dump() if isinstance(plan, IntentPlan) else dict(plan)
        raw_items = list(data.get("sub_intents") or [])
        if require_items and not raw_items:
            raise ValueError("LLM intent_plan contains no sub_intents")

        items = [
            self._normalize_sub_intent(item, index)
            for index, item in enumerate(raw_items, start=1)
        ]
        output = IntentPlan(
            plan_id=data.get("plan_id") or IntentPlan(raw_intent=raw_intent).plan_id,
            artifact_type=str(data.get("artifact_type") or "INTENT_PLAN"),
            interpretation_id=str(
                data.get("interpretation_id")
                or IntentPlan(raw_intent=raw_intent).interpretation_id
            ),
            raw_intent=raw_intent,
            sub_intents=items,
            confidence_score=self._normalize_confidence(
                data.get("confidence_score", 0.5)
            ),
            parse_warnings=self._dedupe(data.get("parse_warnings") or []),
            clarification_questions=self._dedupe(
                data.get("clarification_questions") or []
            ),
            clarification_details=list(data.get("clarification_details") or []),
            field_provenance=dict(data.get("field_provenance") or {}),
            semantic_resolution=dict(data.get("semantic_resolution") or {}),
            critic_report=dict(data.get("critic_report") or {}),
        )
        # Resolve configured services without embedding business defaults.
        output = self._apply_named_service_defaults(output, raw_intent)
        questions = self._validate_plan(output) if validate_fields else []
        if questions or output.clarification_questions:
            from models.semantic_resolution import ClarificationQuestion

            details = list(output.clarification_details)
            known = {item.question for item in details}
            for question in [*output.clarification_questions, *questions]:
                if question in known:
                    continue
                field = (
                    "source"
                    if "源" in question
                    else "destination"
                    if "目的" in question
                    else "objective"
                    if "目标" in question or "带宽" in question
                    else "service"
                )
                details.append(
                    ClarificationQuestion(
                        field=field,
                        reason=f"MISSING_{field.upper()}",
                        question=question,
                    )
                )
            output = output.model_copy(
                update={
                    "plan_id": None,
                    "artifact_type": "CANDIDATE_INTERPRETATION",
                    "confidence_score": min(output.confidence_score, 0.65),
                    "clarification_details": details,
                    "clarification_questions": self._dedupe(
                        output.clarification_questions + questions
                    ),
                }
            )
        return output

    def _apply_named_service_defaults(
        self, plan: IntentPlan, raw_intent: str
    ) -> IntentPlan:
        """Resolve endpoints and services through the central typed resolvers."""
        from agents.semantic_resolver import EndpointResolver, ServiceSemanticResolver
        from api.service_profiles import get_service_profile
        from models.semantic_resolution import FieldProvenance, ResolutionStatus

        updated_items: list[SubIntent] = []
        warnings = list(plan.parse_warnings)
        details = list(plan.clarification_details)
        provenance = dict(plan.field_provenance)
        semantic_payload: dict[str, Any] = dict(plan.semantic_resolution)
        changed = False
        # Slot questions are refreshed after service semantic enrichment.  A
        # ServiceProfile may fill transport attributes, never an omitted source
        # or destination selector, so endpoint questions must remain until the
        # user explicitly resolves those endpoints.
        stale_slot_questions = set(self._validate_plan(plan))
        for item in plan.sub_intents:
            # A compound request can name unrelated services with distinct
            # endpoint pairs.  Resolve each atomic clause in isolation: using
            # the full request here lets an endpoint/service mentioned by one
            # clause silently overwrite another clause's selector.
            clause_text = str(item.objective or "").strip()
            # A single semantic item may have originated before a later
            # conversational clarification was appended to ``raw_intent``.
            # In that case the item objective is deliberately the original
            # terse wording (for example “A to B”), while raw_intent now
            # contains the authoritative user answer (``A is h_research``).
            # Resolve the complete current conversation for a single item.
            # Compound requests remain clause-scoped so one service can never
            # borrow another clause's endpoint pair.
            resolution_text = (
                raw_intent
                if len(plan.sub_intents) == 1
                or (item.constraints or {}).get("compound_parent_scope_context")
                else clause_text or raw_intent
            )
            constraints = dict(item.constraints or {})
            parsed_endpoints = EndpointResolver().resolve_text(resolution_text)
            endpoints = self._merge_endpoint_evidence(
                parsed_endpoints,
                constraints.get("endpoint_resolution"),
            )
            match = dict(item.match)
            # Explicit raw IPs/subnets remain useful evidence even if they are
            # not registered endpoints (the understanding gate will report
            # them invalid rather than execute them).  Everything else must
            # be treated as an untrusted model proposal unless the endpoint
            # resolver found it in the user's current conversation.
            user_prefixes = {
                self._normalize_cidr(value)
                for value in self._extract_ip_prefixes(resolution_text)
            }
            if endpoints.source.status == ResolutionStatus.RESOLVED:
                if match.get("source_ip") != endpoints.source.selector_prefix:
                    match["source_ip"] = endpoints.source.selector_prefix
                    changed = True
                provenance["source_ip"] = endpoints.source.provenance.get(
                    "selector_prefix", "ENDPOINT_REGISTRY"
                )
            else:
                # LLM output is a semantic candidate, never authorization to
                # invent an execution selector.  If the current user text (or
                # its same-Intent clarification transcript) did not resolve a
                # source through EndpointResolver, remove a model-proposed
                # source rather than carrying it into applicability checks.
                # This also prevents it from being mislabeled as registry
                # provenance later in the pipeline.
                if (
                    match.get("source_ip")
                    and self._normalize_cidr(match.get("source_ip")) not in user_prefixes
                    and match.pop("source_ip", None) is not None
                ):
                    changed = True
            if endpoints.destination.status == ResolutionStatus.RESOLVED:
                if match.get("destination_ip") != endpoints.destination.selector_prefix:
                    match["destination_ip"] = endpoints.destination.selector_prefix
                    changed = True
                provenance["destination_ip"] = endpoints.destination.provenance.get(
                    "selector_prefix", "ENDPOINT_REGISTRY"
                )
            else:
                if (
                    match.get("destination_ip")
                    and self._normalize_cidr(match.get("destination_ip")) not in user_prefixes
                    and match.pop("destination_ip", None) is not None
                ):
                    changed = True

            # A deterministic rule clause may carry the named service in the
            # shared compound prefix while the LLM objective contains only an
            # operation ("cap at 10 Mbps").  Validate that trusted service
            # against the complete user request; using the truncated model
            # wording would turn a resolved service into NOT_FOUND.
            trusted_service_id = (
                str(item.service_id)
                if item.service_id
                and constraints.get("service_authority") == "RULE_EXTRACTOR"
                else None
            )
            service_resolution_text = raw_intent if trusted_service_id else resolution_text
            resolution = ServiceSemanticResolver().resolve(
                service_resolution_text,
                source_endpoint=endpoints.source.endpoint_id,
                destination_endpoint=endpoints.destination.endpoint_id,
                source_ip=match.get("source_ip"),
                destination_ip=match.get("destination_ip"),
                protocol=match.get("protocol"),
                destination_port=match.get("destination_port"),
            )
            semantic_payload = resolution.model_dump(mode="json")
            candidate_ids = {
                str(candidate.service_id)
                for candidate in resolution.candidate_services
            }
            unique_candidate_id = (
                next(iter(candidate_ids)) if len(candidate_ids) == 1 else None
            )
            profile_id = trusted_service_id or (
                str(resolution.selected_service_id)
                if resolution.status == ResolutionStatus.RESOLVED
                else str(item.service_id)
                if item.service_id and str(item.service_id) in candidate_ids
                # A uniquely named service can be hydrated before its endpoint
                # pair is known.  ServiceSemanticResolver deliberately reports
                # NOT_FOUND in that case to prevent direct enforcement without
                # a selector; the planner supplies the selector only from the
                # verified ServiceProfile and revalidates below.
                else unique_candidate_id
            )
            if profile_id is None:
                constraints = dict(item.constraints or {})
                constraints["endpoint_resolution"] = endpoints.model_dump(mode="json")
                constraints["service_resolution"] = semantic_payload
                updated_items.append(
                    item.model_copy(update={"match": match, "constraints": constraints})
                )
                if resolution.matched_semantics:
                    question = resolution.clarification_question()
                    if question.question not in plan.clarification_questions:
                        details.append(question)
                continue
            profile = get_service_profile(profile_id)
            prefer_profile_selector = bool(trusted_service_id)
            if (
                item.service_id != profile_id
                or item.validation_profile_id != profile_id
            ):
                changed = True
            action = dict(item.action)
            qos = dict(action.get("qos_params") or {})
            # Service profiles describe traffic semantics, not a user's intent
            # scope.  In particular, profile.src_host/src_ip and dst_host/dst_ip
            # are applicability/validation metadata and must never complete an
            # omitted endpoint pair.  Values in ``match`` above come solely
            # from EndpointResolver resolving the current user conversation.
            if prefer_profile_selector or match.get("protocol") in (None, "", Protocol.ANY.value):
                match["protocol"] = profile.protocol.value
                changed = True
            if prefer_profile_selector or match.get("destination_port") is None:
                match["destination_port"] = profile.dst_port
                changed = True
            resolution = ServiceSemanticResolver().resolve(
                service_resolution_text,
                source_endpoint=endpoints.source.endpoint_id,
                destination_endpoint=endpoints.destination.endpoint_id,
                source_ip=match.get("source_ip"),
                destination_ip=match.get("destination_ip"),
                protocol=match.get("protocol"),
                destination_port=match.get("destination_port"),
            )
            if (
                resolution.status != ResolutionStatus.RESOLVED
                or str(resolution.selected_service_id) != profile_id
            ):
                constraints = dict(item.constraints or {})
                constraints["endpoint_resolution"] = endpoints.model_dump(mode="json")
                constraints["service_resolution"] = resolution.model_dump(mode="json")
                semantic_payload = resolution.model_dump(mode="json")
                # Endpoint/profile applicability is a semantic gate.  Preserve
                # the user's selector for an actionable mismatch response, but
                # never let an unresolved profile relation masquerade as a
                # complete IntentPlan and reach PolicyModeling.
                question = resolution.clarification_question()
                if question.question not in {detail.question for detail in details}:
                    details.append(question)
                updated_items.append(
                    item.model_copy(update={"match": match, "constraints": constraints})
                )
                continue
            semantic_payload = resolution.model_dump(mode="json")
            if action.get("action_type") == ActionType.SET_PRIORITY.value:
                item_provenance = dict(constraints.get("field_provenance") or {})
                dscp_origin = item_provenance.get("dscp_value") or item_provenance.get("dscp")
                # Only an actual user expression may override a named
                # ServiceProfile.  LLM defaults/inferences are candidates,
                # not hard selector constraints.
                if dscp_origin != FieldProvenance.USER_EXPLICIT.value:
                    if qos.get("dscp_value") != profile.dscp:
                        qos["dscp_value"] = profile.dscp
                        changed = True
                    item_provenance["dscp"] = FieldProvenance.SERVICE_PROFILE.value
                    item_provenance["dscp_value"] = FieldProvenance.SERVICE_PROFILE.value
                    constraints["field_provenance"] = item_provenance
            if qos:
                action["qos_params"] = qos
            constraints = dict(item.constraints or {})
            provenance.update(dict(constraints.get("field_provenance") or {}))
            constraints["service"] = constraints.get("service") or profile.service_id
            constraints["endpoint_resolution"] = endpoints.model_dump(mode="json")
            constraints["service_resolution"] = semantic_payload
            constraints["field_provenance"] = {
                **dict(constraints.get("field_provenance") or {}),
                **provenance,
                **resolution.field_provenance,
            }
            provenance.update(resolution.field_provenance)
            if qos.get("dscp_value") is not None:
                constraints["dscp_value"] = qos["dscp_value"]
            updated_items.append(
                item.model_copy(
                    update={
                        "match": match,
                        "action": action,
                        "constraints": constraints,
                        "service_id": profile_id,
                        "service_profile_snapshot": profile.model_dump(mode="json"),
                        "validation_profile_id": profile_id,
                    }
                )
            )

        if changed:
            warnings.append("命名业务选择器来自集中 ServiceProfile 配置")
        current_slot_questions = {
            question
            for item in updated_items
            for question in self._slot_questions(item)
        }
        def _endpoint_question_slot(question: str) -> str | None:
            normalized = str(question).casefold()
            if "source" in normalized or "源" in normalized:
                return "source"
            if "destination" in normalized or "目的" in normalized:
                return "destination"
            return None

        questions = [
            question
            for question in plan.clarification_questions
            if question not in stale_slot_questions
            # Endpoint slot prompts are always regenerated from the current
            # merged selector.  Comparing wording is unsafe (the rule and LLM
            # use different Chinese phrasing), and could re-ask a source or
            # destination already supplied during the same conversation.
            and _endpoint_question_slot(question) is None
        ]
        questions.extend(current_slot_questions)
        questions.extend(detail.question for detail in details)
        return plan.model_copy(
            update={
                "sub_intents": updated_items,
                "parse_warnings": self._dedupe(warnings),
                "clarification_questions": self._dedupe(questions),
                "clarification_details": details,
                "field_provenance": provenance,
                "semantic_resolution": semantic_payload,
            }
        )

    @staticmethod
    def _merge_endpoint_evidence(parsed: Any, persisted: Any) -> Any:
        """Merge endpoint evidence without degrading an authoritative scope.

        ``parsed`` is evidence from the current user text.  ``persisted`` is
        a deterministic rule/compound-parent result carried through the LLM
        merge.  A current explicit endpoint (resolved or explicitly unknown)
        wins; an empty/truncated LLM objective cannot replace a previously
        resolved deterministic endpoint.  The method intentionally operates
        only on typed semantic evidence and never reads ServiceProfile
        endpoints, policy history, or dataplane state.
        """
        from models.semantic_resolution import (
            EndpointPairResolution,
            EndpointResolution,
            ResolutionStatus,
        )

        try:
            prior = EndpointPairResolution.model_validate(persisted or {})
        except (TypeError, ValueError):
            prior = None

        def _select(side: str) -> EndpointResolution:
            current = getattr(parsed, side)
            previous = getattr(prior, side) if prior is not None else None
            # A resolved endpoint or a NOT_FOUND carrying a reference both
            # came from the current user text.  The latter is deliberately
            # retained so an explicit child typo cannot silently inherit a
            # parent selector.
            if current.status == ResolutionStatus.RESOLVED or current.matched_reference:
                return current
            if previous is not None and previous.status == ResolutionStatus.RESOLVED:
                return previous
            return current

        source = _select("source")
        destination = _select("destination")
        unresolved = [
            side
            for side, endpoint in (("source", source), ("destination", destination))
            if endpoint.status != ResolutionStatus.RESOLVED
        ]
        return EndpointPairResolution(
            source=source,
            destination=destination,
            unresolved_fields=unresolved,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # 字段标准化和验证
    # ═══════════════════════════════════════════════════════════════════════════

    def _normalize_sub_intent(
        self, item: SubIntent | dict[str, Any], sequence: int
    ) -> SubIntent:
        """标准化单个 SubIntent 的所有字段。

        对 IP 地址进行 CIDR 标准化、端口范围校验、协议名规范化等。
        移除值为 None 的字段以保持 JSON 简洁。
        """
        data = item.model_dump() if isinstance(item, SubIntent) else dict(item or {})
        action = dict(data.get("action") or {})
        action_type = self._normalize_action_type(
            action.get("action_type") or data.get("intent_type")
        )
        match = dict(data.get("match") or {})
        match = {
            "source_ip": self._normalize_cidr(match.get("source_ip")),
            "destination_ip": self._normalize_cidr(match.get("destination_ip")),
            "source_port": self._normalize_port(match.get("source_port")),
            "destination_port": self._normalize_port(match.get("destination_port")),
            "protocol": self._normalize_protocol(match.get("protocol")),
            "vlan_id": self._normalize_vlan(match.get("vlan_id")),
        }
        match = {key: value for key, value in match.items() if value is not None}

        qos_input = dict(action.get("qos_params") or {})
        qos = {
            "qos_mode": (
                str(
                    qos_input.get("qos_mode")
                    or (
                        "RATE_LIMIT"
                        if action_type == ActionType.RATE_LIMIT.value
                        else "MIN_BANDWIDTH"
                    )
                ).upper()
                if action_type
                in {ActionType.SET_PRIORITY.value, ActionType.RATE_LIMIT.value}
                else None
            ),
            "dscp_value": self._normalize_int(qos_input.get("dscp_value"), 0, 63),
            "bandwidth": self._normalize_int(qos_input.get("bandwidth"), 1),
            "burst_size": self._normalize_int(qos_input.get("burst_size"), 1),
        }
        qos = {key: value for key, value in qos.items() if value is not None}
        if action_type == ActionType.DROP.value:
            qos = {}

        constraints = {
            str(key): value
            for key, value in dict(data.get("constraints") or {}).items()
            if not str(key).startswith("_")
        }
        if constraints.get("policy_name"):
            constraints["policy_name"] = self._slugify(constraints["policy_name"])
        if qos.get("bandwidth") is not None:
            constraints["bandwidth_mbps"] = qos["bandwidth"]
        if qos.get("dscp_value") is not None:
            constraints["dscp_value"] = qos["dscp_value"]

        priority = self._normalize_int(data.get("priority"), 1, 65535)
        return SubIntent(
            sub_intent_id=data.get("sub_intent_id") or SubIntent().sub_intent_id,
            sequence=self._normalize_int(data.get("sequence"), 1) or sequence,
            objective=str(data.get("objective") or "").strip(),
            intent_type=action_type,
            match=match,
            action={
                "action_type": action_type,
                **({"qos_params": qos} if qos else {}),
            },
            constraints=constraints,
            priority=(
                priority
                if priority is not None
                else self._default_priority(action_type)
            ),
            service_id=data.get("service_id"),
            service_profile_snapshot=data.get("service_profile_snapshot"),
            validation_profile_id=data.get("validation_profile_id"),
        )

    def _validate_plan(self, plan: IntentPlan) -> list[str]:
        """对 IntentPlan 执行槽位验证，返回缺失字段的澄清问题列表。

        【难点解释】每种动作类型有必须填充的槽位（slot）：
        - RATE_LIMIT：source_ip + destination_ip + bandwidth
        - SET_PRIORITY：source_ip + destination_ip + service_or_protocol
        - DROP：source_ip 或 destination_ip 至少一个（单向阻断范围合法）
        - FORWARD：source_ip + destination_ip + (optional) target_path
        缺失槽位将通过 clarification_questions 返回给用户。
        """
        questions: list[str] = []
        for item in plan.sub_intents:
            questions.extend(self._slot_questions(item))
        return self._dedupe(questions)

    def _slot_questions(self, item: SubIntent) -> list[str]:
        """对单个 SubIntent 执行槽位检查并生成澄清问题。"""
        action_type = item.action.get("action_type") or item.intent_type
        match = item.match or {}
        qos = item.action.get("qos_params") or {}
        missing: list[str] = []
        questions: list[str] = []
        if action_type == ActionType.RATE_LIMIT.value:
            required = ["source_ip", "destination_ip", "bandwidth"]
            if not match.get("source_ip"):
                missing.append("source_ip")
                questions.append("请补充限速对象的源主机或源网段。")
            if not match.get("destination_ip"):
                missing.append("destination_ip")
                questions.append("请补充限速流量的目的地址或目的网段。")
            if qos.get("bandwidth") is None:
                missing.append("bandwidth")
                questions.append("请补充限速阈值，例如不超过10Mbps。")
        elif action_type == ActionType.SET_PRIORITY.value:
            required = ["source_ip", "destination_ip", "service_or_protocol"]
            if not match.get("source_ip"):
                missing.append("source_ip")
                questions.append(
                    "请指定需要保障业务的源对象，例如主机IP、源网段或站点名称。"
                )
            if not match.get("destination_ip"):
                missing.append("destination_ip")
                questions.append(
                    "请指定需要保障业务的目的对象，例如服务器IP、目的网段或站点名称。"
                )
            service_known = (
                bool(item.service_id)
                or match.get("protocol") not in {None, "", Protocol.ANY.value}
                or match.get("destination_port") is not None
            )
            if not service_known:
                missing.append("service_or_protocol")
                questions.append("请说明希望保障的业务类型，例如科研视频、AI 数据或备份业务。")
        elif action_type == ActionType.DROP.value:
            required = ["source_ip_or_destination_ip"]
            if not (match.get("source_ip") or match.get("destination_ip")):
                missing.append("source_ip_or_destination_ip")
                questions.append("请至少指定要阻断的源对象或目的对象。")
        elif action_type == ActionType.FORWARD.value:
            required = ["source_ip", "destination_ip"]
            if not match.get("source_ip"):
                missing.append("source_ip")
                questions.append("请指定需要转发/重路由的源对象。")
            if not match.get("destination_ip"):
                missing.append("destination_ip")
                questions.append("请指定需要转发/重路由的目的对象。")
            constraints = item.constraints or {}
            if self._requires_explicit_forward_target(item) and not (
                constraints.get("target_path") or constraints.get("avoid_link")
            ):
                required.append("target_path_or_avoid_link")
                missing.append("target_path_or_avoid_link")
                questions.append("请指定目标路径或需要避开的链路。")
        else:
            required = []

        if missing:
            slot_filling = dict(item.constraints.get("slot_filling") or {})
            slot_filling.update(
                {
                    "required_slots": required,
                    "missing_slots": missing,
                    "status": "need_clarification",
                }
            )
            item.constraints["slot_filling"] = slot_filling
        elif item.constraints.get("slot_filling"):
            item.constraints["slot_filling"] = {
                "required_slots": required,
                "missing_slots": [],
                "status": "complete",
            }
        return questions

    def _requires_explicit_forward_target(self, item: SubIntent) -> bool:
        """判断 FORWARD 动作是否需要目标路径信息。

        包含重路由/绕行/切换/备用/避开等关键词时，
        需要用户明确指定目标路径或避开链路。
        """
        text = f"{item.objective or ''} {item.constraints or {}}".lower()
        keywords = (
            "重路由",
            "绕行",
            "切换",
            "备用",
            "避开",
            "reroute",
            "avoid",
            "secondary",
            "backup",
        )
        return any(keyword in text for keyword in keywords)

    def _critic_report(
        self,
        *,
        llm_plan: IntentPlan,
        rule_plan: IntentPlan,
        final_plan: IntentPlan,
        conflicts: list[str],
        questions: list[str],
    ) -> dict[str, Any]:
        """生成内置 Critic 的校验报告。

        记录 LLM 与规则路径的差异、冲突和缺失槽位，
        供下游 Agent 和前端展示使用。
        """
        missing_slots: list[str] = []
        for item in final_plan.sub_intents:
            slot = (item.constraints or {}).get("slot_filling") or {}
            missing_slots.extend(slot.get("missing_slots") or [])
        llm_audit = {
            key: value
            for key, value in (llm_plan.critic_report or {}).items()
            if key
            in {
                "llm_status",
                "llm_runtime_mode",
                "llm_call_id",
                "llm_provider",
                "llm_model",
                "structured_output_valid",
                "validator_result",
                "fallback_used",
            }
        }
        return {
            "mode": "llm_rule_schema_cross_check",
            "llm_sub_intents": len(llm_plan.sub_intents),
            "rule_sub_intents": len(rule_plan.sub_intents),
            "conflict": bool(conflicts),
            "conflicts": conflicts,
            "missing_slots": self._dedupe(missing_slots),
            "clarification_questions": questions,
            "final_authority": "schema_validator_and_rule_extractor",
            **llm_audit,
            "explanation": (
                "LLM 负责语义拆解，规则抽取器校正 IP/端口/协议/带宽，"
                "IntentCritic 记录差异并在冲突或缺槽位时降低置信度。"
            ),
        }

    # ═══════════════════════════════════════════════════════════════════════════
    # 辅助方法 —— 运行时上下文、文本拆分、别名映射
    # ═══════════════════════════════════════════════════════════════════════════

    def _build_runtime_context(
        self,
        network_snapshot: Optional[NetworkSnapshot],
        context: Optional[dict[str, Any]],
    ) -> str:
        """构造 LLM 提示词中的运行时上下文。

        IntentPlanner 仅需要知道存在哪些主机（用于别名解析和 IP 分配），
        完整的设备/链路拓扑在此阶段无关 —— LLM 不能也不应推理路径。
        """
        payload: dict[str, Any] = {}
        if context:
            allowed_context = {
                key: context[key]
                for key in (
                    "known_sites",
                    "site_aliases",
                    "clarifications",
                    "latest_clarification_answer",
                    "clarification_answer",
                    "service_hint",
                    "source",
                    "destination",
                    "service",
                )
                if key in context
            }
            if allowed_context:
                payload["context"] = allowed_context
        if network_snapshot:
            payload["known_hosts"] = [
                {"id": host.host_id, "ips": host.ip_addresses}
                for host in network_snapshot.hosts[:20]
            ]
            payload["device_count"] = len(network_snapshot.devices)
            payload["link_count"] = len(network_snapshot.links)
            # ── 注入主机名→IP 别名映射，帮助 LLM 解析用户输入中的主机名 ──
            aliases = self._build_alias_map(network_snapshot, context)
            if aliases:
                payload["known_aliases"] = {
                    name: info.get("source", info.get("destination", ""))
                    for name, info in aliases.items()
                }
        try:
            from api.service_profiles import list_service_profiles

            payload["known_services"] = [
                {
                    "service_id": profile.service_id,
                    "aliases": list(profile.aliases),
                    "source_endpoint": profile.src_host,
                    "destination_endpoint": profile.dst_host,
                }
                for profile in list_service_profiles()
                if profile.enabled
            ]
        except (OSError, ValueError):
            pass
        return (
            "Runtime context:\n" + json.dumps(payload, ensure_ascii=False)
            if payload
            else ""
        )

    def _split_sub_intents(self, text: str) -> list[str]:
        """将用户输入按分隔符拆分为多个子意图文本。

        分隔符包括：分号、换行、逗号+同时/并且、also 等。
        """
        # "既 A，又 B" is a frequent Chinese compound construction.  The
        # prefix normally carries the common traffic scope (for example
        # "对 h_backup 到 h_research 的备份数据业务").  Preserve that scope
        # on each atomic operation before the generic separator split; losing
        # it creates a one-item deterministic plan which cannot safely align
        # with an LLM's two-item plan.
        compound = re.match(
            r"^(?P<prefix>.+?)(?:，|,)\s*既\s*(?P<first>.+?)(?:，|,)\s*又\s*(?P<second>.+)$",
            str(text or "").strip(),
            flags=re.IGNORECASE,
        )
        if compound:
            prefix = compound.group("prefix").strip(" ，,")
            parts = [
                f"{prefix}，{compound.group('first').strip(' ，,')}",
                f"{prefix}，{compound.group('second').strip(' ，,')}",
            ]
        else:
            parts = re.split(
            r"(?:[；;\n]+|，?\s*同时\s*|，?\s*并且\s*|\balso\b|,\s*(?:and|then)\s+)",
            text,
            flags=re.IGNORECASE,
            )
        clauses = [part.strip(" ，,") for part in parts if part.strip(" ，,")]
        return clauses or [text.strip()]

    def _build_alias_map(
        self,
        network_snapshot: Optional[NetworkSnapshot],
        context: Optional[dict[str, Any]],
    ) -> dict[str, dict[str, str]]:
        """构建站点别名映射表。

        来源按优先级从低到高为：site_aliases.json、请求上下文和
        当前网络快照。规划器不保存任何拓扑专用主机/IP 映射。

        Returns:
            {alias_name: {"source": cidr, "destination": cidr}}
        """
        aliases: dict[str, dict[str, str]] = {}
        # Configuration file.
        if SITE_ALIASES_PATH.exists():
            try:
                payload = json.loads(SITE_ALIASES_PATH.read_text(encoding="utf-8"))
                for site in payload.get("known_sites", []):
                    self._add_alias_site(aliases, site)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("[%s] cannot read site aliases: %s", self.agent_id, exc)
        # Registered service endpoints provide exact host aliases without
        # duplicating topology-specific names or addresses in the planner.
        try:
            from api.service_profiles import list_service_profiles

            for profile in list_service_profiles():
                # Service profiles know the endpoint's role.  Preserve that
                # role instead of marking every known destination as a valid
                # source (and vice versa); otherwise text containing both
                # h_ai and h_dc can resolve h_dc for both sides before the
                # order heuristic is reached.
                aliases.setdefault(profile.src_host.casefold(), {})[
                    "source"
                ] = profile.src_ip
                aliases.setdefault(profile.dst_host.casefold(), {})[
                    "destination"
                ] = profile.dst_ip
        except (OSError, ValueError) as exc:
            logger.warning(
                "[%s] cannot load service endpoint aliases: %s", self.agent_id, exc
            )
        # Request context.
        if context:
            for site in context.get("known_sites", []):
                self._add_alias_site(aliases, site)
        # Live network snapshot.
        if network_snapshot:
            for host in network_snapshot.hosts:
                if host.ip_addresses:
                    prefix = self._normalize_cidr(host.ip_addresses[0])
                    if prefix is None:
                        continue
                    key = host.host_id.lower()
                    mapping = aliases.get(key)
                    if mapping is None:
                        aliases[key] = {"source": prefix, "destination": prefix}
                    else:
                        # Refresh only roles already established by service
                        # profiles.  This keeps live IPs authoritative without
                        # inventing the opposite role for a known endpoint.
                        for role in tuple(mapping):
                            mapping[role] = prefix
        return aliases

    # ═══════════════════════════════════════════════════════════════════════════
    # 委托方法 —— 将具体识别逻辑委托给专用 NLP 模块
    # ═══════════════════════════════════════════════════════════════════════════

    def _add_alias_site(
        self, aliases: dict[str, dict[str, str]], site: dict[str, Any]
    ) -> None:
        return add_alias_site(aliases, site)

    def _lookup_alias(
        self, text: str, aliases: dict[str, dict[str, str]], role: str
    ) -> Optional[str]:
        return lookup_alias(text, aliases, role)

    def _lookup_alias_by_order(
        self,
        lowered: str,
        aliases: dict[str, dict[str, str]],
        alias: str,
        role: str,
    ) -> Optional[str]:
        return lookup_alias_by_order(lowered, aliases, alias, role)

    def _extract_ip_prefixes(self, text: str) -> list[str]:
        return extract_ip_prefixes(text)

    def _extract_directional_ip_roles(
        self, text: str
    ) -> tuple[Optional[str], Optional[str]]:
        return extract_directional_ip_roles(text)

    def _extract_bandwidth_mbps(self, text: str) -> Optional[int]:
        return extract_bandwidth_mbps(text)

    def _chinese_number(self, text: str) -> Optional[int]:
        return chinese_number(text)

    def _extract_latency_ms(self, text: str) -> Optional[float]:
        return extract_latency_ms(text)

    def _extract_vlan_id(self, text: str) -> Optional[int]:
        match = re.search(r"vlan\s*(\d{1,4})", text, re.IGNORECASE)
        return self._normalize_vlan(match.group(1)) if match else None

    def _infer_action_type(self, lowered: str) -> tuple[ActionType, bool]:
        return infer_action_type(lowered)

    def _infer_traffic(
        self, text: str, lowered: str
    ) -> tuple[Protocol, Optional[int], Optional[int], Optional[str]]:
        return infer_traffic(text, lowered, explicit_port=self._extract_port(text))

    def _has_named_application(self, lowered: str) -> bool:
        return has_named_application(lowered)

    def _mentions_video_service(self, lowered: str) -> bool:
        return mentions_video_service(lowered)

    def _extract_port(self, text: str) -> Optional[int]:
        for pattern in (
            r"(?:端口|port|dst_port|destination_port)\s*[:=]?\s*(\d{1,5})",
            r"(?:tcp|udp)\s*[/ :]\s*(\d{1,5})",
            r":(\d{1,5})\b",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return self._normalize_port(match.group(1))
        return None

    def _infer_priority(self, text: str, action_type: ActionType) -> int:
        return infer_priority(text, action_type)

    def _make_policy_name(
        self,
        source_ip: Optional[str],
        destination_ip: Optional[str],
        action_type: ActionType,
        lowered: str,
    ) -> str:
        """生成人类可读的策略名称。

        格式：{source}-to-{dest}-{action-type}（如 "192.168.10.0/24-to-10.0.0.0/8-video-qos"）
        """
        del lowered
        suffix = action_type.value.lower().replace("_", "-")
        prefix = (
            f"{source_ip}-to-{destination_ip}-" if source_ip and destination_ip else ""
        )
        return self._slugify(prefix + suffix)

    # ═══════════════════════════════════════════════════════════════════════════
    # 值标准化工具方法
    # ═══════════════════════════════════════════════════════════════════════════

    def _normalize_match_value(self, key: str, value: Any) -> Any:
        if key in {"source_ip", "destination_ip"}:
            return self._normalize_cidr(value)
        if key == "protocol":
            return self._normalize_protocol(value)
        if key in {"source_port", "destination_port"}:
            return self._normalize_port(value)
        if key == "vlan_id":
            return self._normalize_vlan(value)
        return value

    def _normalize_qos_value(self, key: str, value: Any) -> Any:
        return (
            self._normalize_int(value, 0, 63)
            if key == "dscp_value"
            else self._normalize_int(value, 1)
        )

    def _normalize_cidr(self, value: Any) -> Optional[str]:
        return normalize_cidr(value)

    def _normalize_protocol(self, value: Any) -> str:
        text = str(value or Protocol.ANY.value).upper()
        return text if text in {item.value for item in Protocol} else Protocol.ANY.value

    def _normalize_action_type(self, value: Any) -> str:
        """标准化动作类型名称。

        支持别名映射（如 GUARANTEE → SET_PRIORITY, BLOCK → DROP），
        提高用户输入容错性。
        """
        text = str(value or ActionType.FORWARD.value).upper().replace("-", "_")
        aliases = {
            "GUARANTEE": "SET_PRIORITY",
            "PRIORITIZE": "SET_PRIORITY",
            "OPTIMIZE": "SET_PRIORITY",
            "LIMIT": "RATE_LIMIT",
            "BLOCK": "DROP",
            "DENY": "DROP",
            "REROUTE": "FORWARD",
        }
        text = aliases.get(text, text)
        return (
            text
            if text in {item.value for item in ActionType}
            else ActionType.FORWARD.value
        )

    def _normalize_port(self, value: Any) -> Optional[int]:
        return self._normalize_int(value, 1, 65535)

    def _normalize_vlan(self, value: Any) -> Optional[int]:
        return self._normalize_int(value, 1, 4094)

    def _normalize_int(
        self,
        value: Any,
        min_value: Optional[int] = None,
        max_value: Optional[int] = None,
    ) -> Optional[int]:
        """安全地将任意值转换为限定范围的整数。

        超出范围的值返回 None（而非抛出异常），
        因为此方法用于数据标准化，非用户输入校验。
        """
        if value in (None, "", "null"):
            return None
        try:
            result = int(float(value))
        except (TypeError, ValueError):
            return None
        if min_value is not None and result < min_value:
            return None
        if max_value is not None and result > max_value:
            return None
        return result

    def _normalize_confidence(self, value: Any) -> float:
        try:
            return max(0.0, min(float(value), 1.0))
        except (TypeError, ValueError):
            return 0.5

    def _default_priority(self, action_type: str) -> int:
        """返回每种动作类型的默认策略优先级。

        DROP 优先级最高(50)、SET_PRIORITY 次之(100)、其他最低(1000)。
        数值越小优先级越高（参考 Linux tc 和 ONOS 的约定）。
        """
        return (
            50
            if action_type == ActionType.DROP.value
            else 100 if action_type == ActionType.SET_PRIORITY.value else 1000
        )

    def _slugify(self, value: Any) -> str:
        """将任意字符串转换为 URL 友好的 slug 格式。"""
        text = (
            str(value or "unnamed-policy")
            .lower()
            .replace("/", "-")
            .replace(".", "-")
            .replace("_", "-")
        )
        return (
            re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", text)).strip("-")
            or "unnamed-policy"
        )

    def _dedupe(self, values: list[Any]) -> list[str]:
        """去重列表中的字符串元素，保持原始顺序。

        ``_warning_key`` removes punctuation and spacing so equivalent
        warnings from the LLM and deterministic parser are emitted once.
        """
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value).strip()
            key = self._warning_key(text)
            if text and key not in seen:
                output.append(text)
                seen.add(key)
        return output

    def _warning_key(self, text: str) -> str:
        """Generate a scenario-neutral warning de-duplication key."""
        return re.sub(r"[\s，。,:：；;、]+", "", text.lower()).removeprefix("假设")

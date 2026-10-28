"""
orchestrator/nodes/think_node.py
=================================

意图解析与策略建模节点 (Think Node) —— STAV 闭环的第二阶段。

# 系统角色
执行两步转换：
1. 自然语言 → IntentPlan（IntentPlannerAgent，含 LLM 语义理解）
2. IntentPlan → YANGPolicy（PolicyModelingAgent，确定性规则转换）

同时负责：
- LLM 流式 Token 推送（SSE thinking_token 事件）
- 置信度检查和澄清问题生成
- 子意图拆分为多个 YANGPolicy

# 输入/输出
- 输入：state.user_intent.text + state.network_snapshot
- 输出：state.intent_plan, state.yang_policies, state.yang_draft
"""

from __future__ import annotations

import logging
import os
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


# ──────────────────────────────────────────────
# 基础节点类
# ──────────────────────────────────────────────


class ThinkNode(BaseNode):
    """
    意图计划与策略建模节点
    职责: 调用 IntentPlannerAgent 生成 JSON intent_plan，
          再调用 PolicyModelingAgent 转换为 YANGPolicy。

    输入: state.user_intent.text (自然语言)
    输出: state.yang_draft (YANGPolicy 对象)
    """

    agent_id = "intent_planner"
    stage = ExecutionStage.PARSE

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        state.current_status = IntentStatus.THINKING
        try:
            # Step 1: 自然语言 -> JSON intent_plan
            from agents.intent_planner import IntentPlannerAgent
            from agents.policy_modeling import PolicyModelingAgent

            from agents.llm_client import llm_runtime_mode

            mode = llm_runtime_mode().value
            if state.intent_plan is not None:
                progress_text = "大模型语义理解已完成，正在进行确定性策略建模..."
            elif mode == "REAL_LLM":
                progress_text = "正在调用真实大模型理解自然语言意图..."
            elif mode == "MOCK_LLM":
                progress_text = "测试模式：正在使用模拟大模型响应..."
            else:
                progress_text = "大模型不可用，将使用确定性解析继续处理..."
            self._push_progress(
                state, progress_text, "intent_parsing"
            )

            # ── Build a token callback that pushes streaming SSE events ──
            _token_seq = 0

            def _make_on_token(intent_id: str):
                nonlocal _token_seq

                def _on_token(token: str):
                    nonlocal _token_seq
                    seq = _token_seq
                    _token_seq += 1
                    try:
                        from api.events import get_event_manager

                        get_event_manager().push(
                            intent_id,
                            "thinking_token",
                            {
                                "stage": "parse",
                                "agent_id": "intent_planner",
                                "seq": seq,
                                "token": token,
                                "timestamp": datetime.utcnow().isoformat(),
                            },
                        )
                    except Exception:
                        pass

                return _on_token

            planner = IntentPlannerAgent(llm=self.llm)
            # Some deterministic/test planners implement the stable planning
            # contract without the optional streaming callback.  Inspect the
            # callable instead of catching a broad TypeError, which could hide
            # a genuine error raised inside a planner implementation.
            import inspect

            intent_plan = state.intent_plan
            if intent_plan is None:
                plan_parameters = inspect.signature(planner.plan).parameters
                plan_kwargs = {
                    "user_input": state.user_intent.text,
                    "network_snapshot": state.network_snapshot,
                    "context": state.user_intent.context,
                }
                if "intent_id" in plan_parameters:
                    plan_kwargs["intent_id"] = state.intent_id
                if "revision" in plan_parameters:
                    plan_kwargs["revision"] = 1
                if "on_token" in plan_parameters or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in plan_parameters.values()
                ):
                    plan_kwargs["on_token"] = _make_on_token(state.intent_id)
                intent_plan = await planner.plan(**plan_kwargs)
                # ── Emit stream_end so the frontend can finalize the typewriter ──
                try:
                    from api.events import get_event_manager

                    get_event_manager().push(
                        state.intent_id,
                        "stream_end",
                        {
                            "stage": "parse",
                            "agent_id": "intent_planner",
                            "total_tokens": _token_seq,
                            "timestamp": datetime.utcnow().isoformat(),
                        },
                    )
                except Exception:
                    pass
            state.intent_plan = intent_plan

            # Semantic validation is a hard gate.  An incomplete candidate is
            # not a formal IntentPlan and must never reach policy modeling.
            missing_slots = self._intent_plan_missing_slots(intent_plan)
            needs_clarification = bool(
                intent_plan.clarification_questions or missing_slots
            )
            if needs_clarification:
                details = [
                    item.model_copy(update={"intent_id": state.intent_id})
                    for item in intent_plan.clarification_details
                ]
                intent_plan = intent_plan.model_copy(
                    update={
                        "plan_id": None,
                        "artifact_type": "CANDIDATE_INTERPRETATION",
                        "clarification_details": details,
                    }
                )
                state.intent_plan = intent_plan
                state.yang_policies = []
                state.yang_draft = None
                state.flow_table_plan = None
                state.needs_clarification = True
                question = (
                    intent_plan.clarification_questions[0]
                    if intent_plan.clarification_questions
                    else "请补充完成该网络目标所需的业务信息。"
                )
                state.clarification_question = question
                state.clarification_questions = [
                    item.model_dump(mode="json")
                    for item in details
                ]
                state.current_status = IntentStatus.PENDING_CLARIFICATION
                self._push_progress(
                    state,
                    "我需要确认一个业务信息；规划、策略建模和执行尚未开始。",
                    "clarification_required",
                )
                return self._end_log(
                    state,
                    start,
                    StageStatus.SUCCESS,
                    "语义理解需要澄清，流水线已在规划前暂停",
                )

            # Emit progress between steps — step 1 done, starting step 2
            self._push_progress(
                state,
                f"意图解析完成 (plan_id={intent_plan.plan_id}, "
                f"sub_intents={len(intent_plan.sub_intents)}, "
                f"confidence={intent_plan.confidence_score:.2f})，正在进行策略建模...",
                "policy_modeling",
            )

            # Step 2: JSON intent_plan -> YANGPolicy
            modeler = PolicyModelingAgent(llm=self.llm)
            try:
                yang_policies = await modeler.model_all(
                    intent_plan=intent_plan,
                    network_snapshot=state.network_snapshot,
                )
            except Exception as exc:
                # A ServiceProfile mismatch is a semantic ambiguity: an
                # inferred service does not agree with the available selector.
                # It is not an LLM transport failure and must not reach the
                # generic terminal-failure handler below.
                from api.service_profiles import ValidationProfileMismatch

                if not isinstance(exc, ValidationProfileMismatch):
                    raise
                from models.semantic_resolution import ClarificationQuestion

                service_id = next(
                    (
                        str(item.service_id)
                        for item in intent_plan.sub_intents
                        if item.service_id
                    ),
                    "",
                )
                service_label = service_id or "该业务"
                if service_id:
                    try:
                        from api.service_profiles import get_service_profile

                        profile = get_service_profile(service_id)
                        service_label = profile.display_name or profile.service_name
                    except (KeyError, ValueError):
                        # The mismatch remains actionable even if a registry
                        # refresh races with the persisted candidate.
                        pass
                question = (
                    f"已识别为{service_label}，但当前源/目的端点不在该业务配置的适用范围内。"
                    "请确认端点，或选择其他业务类型。"
                )
                detail = ClarificationQuestion(
                    intent_id=state.intent_id,
                    field="service",
                    reason="SERVICE_SELECTOR_MISMATCH",
                    question=question,
                )
                questions = list(intent_plan.clarification_questions or [])
                if question not in questions:
                    questions.append(question)
                details = list(intent_plan.clarification_details or []) + [detail]
                state.intent_plan = intent_plan.model_copy(
                    update={
                        "plan_id": None,
                        "artifact_type": "CANDIDATE_INTERPRETATION",
                        "clarification_questions": questions,
                        "clarification_details": details,
                    }
                )
                state.yang_policies = []
                state.yang_draft = None
                state.flow_table_plan = None
                state.needs_clarification = True
                state.clarification_question = question
                state.clarification_questions = [
                    item.model_dump(mode="json") for item in details
                ]
                state.current_status = IntentStatus.PENDING_CLARIFICATION
                self._push_progress(
                    state,
                    "服务语义仍不明确；策略建模和执行已暂停，等待用户补充。",
                    "clarification_required",
                )
                return self._end_log(
                    state,
                    start,
                    StageStatus.SUCCESS,
                    f"服务选择条件不匹配，已请求澄清：{exc}",
                )
            state.yang_policies = yang_policies
            state.current_policy_index = 0
            state.policy_results = []
            state.yang_draft = yang_policies[0] if yang_policies else None

            if not state.yang_draft:
                state.error_message = (
                    "PolicyModelingAgent did not produce any YANGPolicy"
                )
                return self._end_log(
                    state,
                    start,
                    StageStatus.FAILED,
                    "YANG policy modeling produced no policies",
                    error=state.error_message,
                )

            # ── 检查置信度 ──
            policy_confidence = min(policy.confidence_score for policy in yang_policies)
            confidence = min(intent_plan.confidence_score, policy_confidence)
            state.needs_clarification = False
            state.clarification_question = None
            state.clarification_questions = []

            execution_mode = str(
                ((state.user_intent.context or {}).get("execution_mode") or "")
            ).upper()
            if execution_mode == "SEMANTIC_ONLY":
                # This is an explicit, API-level pre-execution request.  Keep
                # the real LLM's IntentPlan and deterministic YANG modelling,
                # but make the no-execution boundary durable in graph state.
                state.semantic_only_completed = True
                state.execution_status = "NOT_RUN_SEMANTIC_ONLY"
                state.execution_readback_status = "NOT_RUN_SEMANTIC_ONLY"
                state.terminal_reason = "SEMANTIC_ONLY_DECOMPOSITION_COMPLETED"
                self._push_progress(
                    state,
                    "语义分解已完成；该请求明确禁止策略编译、南向执行和状态验证。",
                    "semantic_only_complete",
                )

            return self._end_log(
                state,
                start,
                StageStatus.SUCCESS,
                f"意图计划与策略建模完成: plan_id={intent_plan.plan_id}, "
                f"policy_count={len(yang_policies)}, "
                f"current_policy={state.yang_draft.network_policy.policy_name}, "
                f"confidence={confidence:.2f}",
            )
        except Exception as e:
            state.error_message = f"意图解析失败 (LLM调用异常): {str(e)}"
            logger.error(f"[{self.agent_id}] {state.error_message}")
            return self._end_log(
                state, start, StageStatus.FAILED, "意图解析失败", error=str(e)
            )

    def _intent_plan_missing_slots(self, intent_plan) -> list[str]:
        missing: list[str] = []
        critic_report = getattr(intent_plan, "critic_report", {}) or {}
        missing.extend(critic_report.get("missing_slots") or [])
        for item in getattr(intent_plan, "sub_intents", []) or []:
            slot = (getattr(item, "constraints", {}) or {}).get("slot_filling") or {}
            missing.extend(slot.get("missing_slots") or [])
        output: list[str] = []
        for value in missing:
            text = str(value).strip()
            if text and text not in output:
                output.append(text)
        return output


# ──────────────────────────────────────────────
# Think阶段 — 冲突检测节点
# ──────────────────────────────────────────────

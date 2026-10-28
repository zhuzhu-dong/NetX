"""
orchestrator/nodes/base_node.py
================================

LangGraph 状态机节点的抽象基类和通用基础设施。

# 系统角色
本文件定义了所有 STAV 编排节点共享的基础能力：
- 执行超时控制（per-node configurable timeout + auto-retry）
- 执行日志记录（start_log / end_log + ExecutionLogEntry）
- A2A 追踪集成（delegation / response / consensus_sync）
- SSE 实时流式推送（stage_change / phase_progress）
- 多策略顺序处理（current_policy_index 滚动）
- 状态字段预校验（_required_fields）

# 设计决策
- BaseNode 采用 Template Method 模式：子类实现 run()，
  基类提供 run_with_timeout() 包装（超时 + 重试）
- A2A 追踪方法（_a2a_*）提供默认实现，子类可覆盖
- _prepare_next_policy() 支持多策略顺序处理，
  切换策略时自动重置 per-policy 中间状态
- SSE 推送失败时静默降级（catch Exception → log warning），
  不阻塞主流程

# 主要依赖
- models.agent_state：GlobalAgentState, ExecutionLogEntry, StageStatus 等
- api.events：SSE 事件管理器
- api.a2a_trace：Agent-to-Agent 消息追踪

# 与其他模块交互
- 所有 11 个子节点类继承自此基类
- graph.py 通过 run_with_timeout() 调用各节点
- api.events.get_event_manager()：推送实时进度到前端
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Any

from models.agent_state import (
    ConsensusDecision,
    ConsensusReasonCode,
    ConsensusStatus,
    ExecutionLogEntry,
    ExecutionStage,
    GlobalAgentState,
    StageStatus,
)
from models.intent import IntentStatus

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 基础节点类
# ═══════════════════════════════════════════════════════════════════════════════


class BaseNode:
    """所有编排节点的抽象基类，提供日志记录、超时控制和执行耗时统计。

    采用 Template Method 设计模式：
    - 子类实现 async run(state) → GlobalAgentState
    - 基类提供 run_with_timeout(state) 包装（超时 + 自动重试）

    # 生命周期
    1. graph.py 调用 node.run_with_timeout(state)
    2. run_with_timeout 调用 self.run(state)（子类实现）
    3. 子类 run() 内部调用 self._start_log() / self._end_log()

    # 超时机制
    _DEFAULT_TIMEOUTS 定义每个阶段的默认超时时间，可通过环境变量
    IDN_NODE_TIMEOUT_{STAGE} 覆盖（以秒为单位）。
    超时后自动重试一次，两次超时后抛出 TimeoutError。

    # A2A 追踪机制
    每个节点执行前后，通过 api.a2a_trace 记录 Agent-to-Agent 通信：
    - delegation：上游委派任务给本节点
    - response：本节点返回结果给上游
    - consensus_sync：多方协商决议同步
    """

    agent_id: str = "base_node"
    stage: ExecutionStage = ExecutionStage.SENSE
    _required_fields: tuple[str, ...] = ()

    def __init__(self, llm: Any = None):
        self.llm = llm

    # ── 各阶段默认超时时间 (秒)，可通过同名环境变量覆盖 ──
    # 超时值的选择基于：ONOS REST 通常 1-3s，LLM 调用合计 ~6-15s，
    # 确定性检测为毫秒级，多设备下发按设备数递增
    _DEFAULT_TIMEOUTS: dict[str, int] = {
        "understanding": 25,
        "sense": 10,  # ONOS REST 通常 1-3s
        "parse": 25,  # 2×LLM 调用 (qwen-plus 合计 ~6-15s)
        # A normal conflict check is quick, but a first MIN_BANDWIDTH
        # admission can legitimately perform one fenced 5×3s candidate
        # calibration with P4/TC/receiver readback on both sides of every
        # sample.  Ten seconds cancelled that safe measurement mid-window and
        # retried it.  Keep the bound finite while allowing the formal probe
        # plus conservative readback to complete.
        "check": 90,
        "generate": 15,  # 1×LLM 编译
        "execute": 20,  # ONOS REST 多设备下发
        "verify": 15,  # 等待 + 遥测采集 + LLM 根因分析
    }

    # ── 各阶段默认重试配置 ──
    # max_attempts: 总尝试次数（含首次）
    # base_delay_s: 首次重试等待秒数（指数退避：delay = base * 2^(attempt-1)）
    # 可通过环境变量 IDN_NODE_RETRY_{STAGE} 以 JSON 覆盖
    #   例: IDN_NODE_RETRY_EXECUTE='{"max_attempts":5,"base_delay_s":3.0}'
    _DEFAULT_RETRY_CONFIG: dict[str, dict] = {
        "understanding": {"max_attempts": 3, "base_delay_s": 1.0},
        "sense": {"max_attempts": 3, "base_delay_s": 1.0},
        "parse": {"max_attempts": 3, "base_delay_s": 1.0},
        "check": {"max_attempts": 3, "base_delay_s": 1.0},
        "generate": {"max_attempts": 3, "base_delay_s": 1.0},
        "execute": {"max_attempts": 5, "base_delay_s": 2.0},
        "verify": {"max_attempts": 3, "base_delay_s": 2.0},
    }

    def _get_timeout(self) -> int:
        """获取本节点的超时时间（秒），支持环境变量覆盖。

        优先级：环境变量 IDN_NODE_TIMEOUT_{STAGE} > 默认值 > 30s 兜底

        Returns:
            超时秒数
        """
        default = self._DEFAULT_TIMEOUTS.get(self.stage.value, 30)
        env_key = f"IDN_NODE_TIMEOUT_{self.stage.value.upper()}"
        return int(os.getenv(env_key, str(default)))

    def _get_retry_config(self) -> dict:
        """获取本节点的重试配置，支持环境变量覆盖。

        优先级：环境变量 IDN_NODE_RETRY_{STAGE}（JSON）> 默认值
        """
        default = dict(
            self._DEFAULT_RETRY_CONFIG.get(
                self.stage.value,
                {"max_attempts": 2, "base_delay_s": 1.0},
            )
        )
        env_key = f"IDN_NODE_RETRY_{self.stage.value.upper()}"
        env_val = os.getenv(env_key)
        if env_val:
            try:
                import json as _json

                default.update(_json.loads(env_val))
            except Exception:
                pass
        return default

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        """Execute the concrete node implementation."""
        raise NotImplementedError(f"{type(self).__name__}.run() is not implemented")

    async def run_with_timeout(self, state: GlobalAgentState) -> GlobalAgentState:
        """带超时、自动重试和指数退避的节点执行包装。

        重试策略：
        - 总尝试次数由 _DEFAULT_RETRY_CONFIG 决定（通常 3-5 次）
        - 指数退避：delay = base_delay_s × 2^(attempt-1)
        - 重试范围：asyncio.TimeoutError + 传输层异常（ConnectionError,
          OSError, aiohttp.ClientError）
        - 应用层异常（ValueError, ValidationError 等）不重试，立即传播

        Args:
            state: 当前全局状态

        Returns:
            执行后的全局状态

        Raises:
            asyncio.TimeoutError: 所有尝试均超时
        """
        timeout = self._get_timeout()
        retry_cfg = self._get_retry_config()
        max_attempts = retry_cfg["max_attempts"]
        base_delay = retry_cfg["base_delay_s"]

        # ── Retryable exception types ──
        _RETRYABLE: tuple[type[BaseException], ...] = (
            asyncio.TimeoutError,
            ConnectionError,
            OSError,  # ConnectionRefusedError, etc.
        )
        try:
            import aiohttp

            _RETRYABLE = (*_RETRYABLE, aiohttp.ClientError)
        except ImportError:
            pass

        _diag_t0 = time.monotonic()

        for attempt in range(1, max_attempts + 1):
            delay = base_delay * (2 ** (attempt - 1)) if attempt > 1 else 0.0
            logger.info(
                "[DIAG-NODE] %s ENTER attempt=%d/%d timeout=%ds%s",
                self.agent_id,
                attempt,
                max_attempts,
                timeout,
                f" delay={delay:.1f}s" if delay > 0 else "",
            )

            try:
                result = await asyncio.wait_for(self.run(state), timeout=timeout)
                _diag_t1 = time.monotonic()
                logger.info(
                    "[DIAG-NODE] %s EXIT attempt=%d elapsed=%.2fs",
                    self.agent_id,
                    attempt,
                    _diag_t1 - _diag_t0,
                )
                return result

            except asyncio.TimeoutError:
                if attempt == max_attempts:
                    logger.error(
                        "[%s] timeout after %ds (attempt %d/%d), failing",
                        self.agent_id,
                        timeout,
                        attempt,
                        max_attempts,
                    )
                    state.error_message = (
                        f"{self.agent_id} timed out after {timeout}s "
                        f"({max_attempts} attempts)"
                    )
                    raise
                logger.warning(
                    "[%s] timeout after %ds (attempt %d/%d), retrying in %.1fs...",
                    self.agent_id,
                    timeout,
                    attempt,
                    max_attempts,
                    delay,
                )
                await asyncio.sleep(delay)

            except _RETRYABLE as exc:
                if attempt == max_attempts:
                    logger.error(
                        "[%s] retryable error after %d attempts: %s",
                        self.agent_id,
                        max_attempts,
                        exc,
                    )
                    state.error_message = f"{self.agent_id} failed: {exc}"
                    raise
                logger.warning(
                    "[%s] retryable error (attempt %d/%d): %s, retrying in %.1fs...",
                    self.agent_id,
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

            # Non-retryable exceptions propagate immediately

    # ── A2A 代理别名映射：agent_id → A2A 协议中的 canonical name ──
    _A2A_AGENT_ALIASES = {
        "sense_agent": "network_sensing",
        "human_approval_gate": "human_approval",
    }
    # ── A2A 上游依赖映射：每个 Agent 的 upstream Agent ──
    _A2A_UPSTREAM = {
        "intent_understanding": "orchestrator",
        "network_sensing": "orchestrator",
        "intent_planner": "network_sensing",
        "policy_conflict_checker": "policy_modeling",
        "policy_compiler": "policy_conflict_checker",
        "p4_rule_verifier": "policy_compiler",
        "safety_council": "p4_rule_verifier",
        "p4_runtime_executor": "safety_council",
        "status_verifier": "p4_runtime_executor",
        "human_approval": "status_verifier",
    }

    def _validate_state(self, state: GlobalAgentState) -> None:
        """在执行前校验所有必填状态字段是否已就绪。

        将运行时 AttributeError-on-None 转为清晰的 ValueError，
        包含缺失字段的完整列表，方便调试。

        Args:
            state: 当前全局状态

        Raises:
            ValueError: 存在必填字段为 None
        """
        missing = [f for f in self._required_fields if getattr(state, f, None) is None]
        if missing:
            raise ValueError(
                f"[{self.agent_id}] cannot start: required state fields are missing: "
                f"{', '.join(missing)}"
            )

    def _start_log(self, state: GlobalAgentState) -> float:
        """记录节点开始执行。

        执行以下操作：
        1. 校验必填字段
        2. 写入 info 日志
        3. 通过 A2A trace 记录 delegation（上游委派给本节点）

        Args:
            state: 当前全局状态

        Returns:
            阶段开始时间（time.monotonic() 值），供 _end_log 计算耗时
        """
        self._validate_state(state)
        logger.info(f"[{self.agent_id}] 开始执行 | intent_id={state.intent_id}")
        try:
            from api.a2a_trace import record_delegation

            target_agent = self._a2a_agent_id()
            record_delegation(
                state,
                source_agent=self._a2a_source_agent(target_agent),
                target_agent=target_agent,
                stage=self.stage,
                input_summary=self._a2a_input_summary(state),
                payload_schema=f"{target_agent}.input.v1",
                payload=self._a2a_input_payload(state),
                evidence_refs=self._a2a_input_evidence_refs(state),
                confidence=self._a2a_confidence(state),
                risk_score=self._a2a_risk_score(state, StageStatus.RUNNING),
                metadata={"node_agent_id": self.agent_id},
            )
        except Exception as exc:
            logger.warning("[%s] A2A trace delegation failed: %s", self.agent_id, exc)
        return time.monotonic()

    def _end_log(
        self,
        state: GlobalAgentState,
        start_time: float,
        status: StageStatus,
        summary: str,
        error: str | None = None,
        artifacts: dict | None = None,
    ) -> GlobalAgentState:
        """记录节点执行结束，追加审计日志条目。

        执行以下操作：
        1. 计算耗时（time.monotonic() 差值 × 1000 = ms）
        2. 创建 ExecutionLogEntry 并追加到 state.execution_log
        3. 更新 state.updated_at 时间戳
        4. 推送 SSE stage_change 事件到前端
        5. 通过 A2A trace 记录 response（本节点返回给上游）
        6. 通过 A2A trace 记录 consensus_sync（多方协商同步）

        Args:
            state: 当前全局状态
            start_time: _start_log 返回的阶段开始时间
            status: 阶段执行结果状态
            summary: 阶段输出摘要
            error: 错误信息（status=FAILED 时）
            artifacts: 阶段产出的关键数据快照

        Returns:
            更新后的全局状态（与输入 state 是同一个对象）
        """
        duration_ms = (time.monotonic() - start_time) * 1000
        entry = ExecutionLogEntry(
            stage=self.stage,
            agent_id=self.agent_id,
            status=status,
            output_summary=summary,
            error_message=error,
            duration_ms=round(duration_ms, 2),
            artifacts=artifacts,
        )
        state.execution_log.append(entry)
        state.updated_at = datetime.utcnow()
        logger.info(
            f"[{self.agent_id}] 执行完成 | status={status.value} | "
            f"耗时={duration_ms:.0f}ms | {summary}"
        )

        # ── 推送 SSE stage_change 事件 → 前端实时更新阶段时间线 ──
        try:
            from api.events import get_event_manager

            get_event_manager().push(
                state.intent_id,
                "stage_change",
                {
                    "stage": self.stage.value,
                    "agent_id": self.agent_id,
                    "status": status.value,
                    "summary": summary,
                    "duration_ms": round(duration_ms, 2),
                    "timestamp": datetime.utcnow().isoformat(),
                },
            )
        except Exception as exc:
            logger.warning("[%s] SSE stage_change push failed: %s", self.agent_id, exc)

        # ── A2A 追踪：记录 response + consensus_sync ──
        try:
            from api.a2a_trace import record_consensus_sync, record_response

            source_agent = self._a2a_agent_id()
            record_response(
                state,
                source_agent=source_agent,
                target_agent=self._a2a_source_agent(source_agent),
                stage=self.stage,
                status=status,
                output_summary=summary,
                duration_ms=round(duration_ms, 2),
                failure_reason=error,
                retry_suggestion=self._a2a_retry_suggestion(state, status),
                payload_schema=f"{source_agent}.output.v1",
                payload=self._a2a_output_payload(state, artifacts),
                evidence_refs=self._a2a_output_evidence_refs(state),
                confidence=self._a2a_confidence(state),
                risk_score=self._a2a_risk_score(state, status),
                metadata={"node_agent_id": self.agent_id},
            )
            consensus = self._a2a_consensus_decision(state, status, summary)
            if consensus:
                record_consensus_sync(
                    state,
                    source_agent=source_agent,
                    target_agents=consensus["target_agents"],
                    stage=self.stage,
                    decision=consensus["decision"],
                    consensus=consensus["consensus"],
                    confidence=self._a2a_confidence(state),
                    risk_score=self._a2a_risk_score(state, status),
                    evidence_refs=self._a2a_output_evidence_refs(state),
                    payload=consensus["payload"],
                )
        except Exception as exc:
            logger.warning("[%s] A2A response trace failed: %s", self.agent_id, exc)

        return state

    # ═══════════════════════════════════════════════════════════════════════
    # A2A 追踪辅助方法 — 子类可覆盖以提供阶段特定的追踪信息
    # ═══════════════════════════════════════════════════════════════════════

    def _a2a_agent_id(self) -> str:
        """获取本节点的 A2A 协议 canonical name。

        通过 _A2A_AGENT_ALIASES 映射将内部 agent_id 转换为
        A2A 协议中使用的标准名称。
        """
        return self._A2A_AGENT_ALIASES.get(self.agent_id, self.agent_id)

    def _a2a_source_agent(self, target_agent: str) -> str:
        """获取本节点在 A2A 协议中的上游 Agent。

        通过 _A2A_UPSTREAM 映射查找上游依赖。
        """
        return self._A2A_UPSTREAM.get(target_agent, "orchestrator")

    def _a2a_input_summary(self, state: GlobalAgentState) -> str:
        """生成 A2A delegation 的输入摘要描述。

        每个阶段有不同的输入语义——sense 采集拓扑，parse 解析意图，
        check 检测冲突等。
        """
        if self.agent_id == "sense_agent":
            return "采集当前网络拓扑、主机、链路和运行时流表，形成 NetworkSnapshot。"
        if self.agent_id == "intent_planner":
            return f"解析用户自然语言意图并建模为 YANGPolicy：{state.user_intent.text}"
        if self.agent_id == "policy_conflict_checker":
            return "基于 YANGPolicy、NetworkSnapshot 和存量策略执行冲突、资源、拓扑兼容性检查。"
        if self.agent_id == "policy_compiler":
            return "将通过审批的 YANGPolicy 编译为 P4/ONOS 可执行流表计划。"
        if self.agent_id == "p4_rule_verifier":
            return "根据 P4Info、运行时规则和审批令牌验证候选流表计划。"
        if self.agent_id == "safety_council":
            return "执行前安全评审：不变量检查、影子仿真、红队审查、回滚证明和历史风险评估。"
        if self.agent_id == "p4_runtime_executor":
            return "把已验证的 WriteRequest 转换为 ONOS FlowRule 并下发到数据平面。"
        if self.agent_id == "status_verifier":
            return "采集实时遥测并验证业务 KPI，必要时触发 ReAct 自愈或重试。"
        if self.agent_id == "human_approval_gate":
            return "自动闭环无法继续时，挂起流程并请求人工审批。"
        return f"执行 {self.stage.value} 阶段任务。"

    def _a2a_retry_suggestion(
        self,
        state: GlobalAgentState,
        status: StageStatus,
    ) -> str | None:
        """生成 A2A response 的重试建议。

        仅在失败时返回建议——成功时不需要重试。
        """
        if status == StageStatus.SUCCESS:
            return None
        if state.react_state.last_failure_reason:
            return "根据失败原因重新规划策略、回滚或请求人工审批。"
        if state.error_message:
            return "请检查该阶段依赖的工具、输入数据和网络状态。"
        return None

    def _a2a_input_payload(self, state: GlobalAgentState) -> dict[str, Any]:
        """构建 A2A delegation 的输入载荷。

        每个阶段构造其特有的输入上下文，供 A2A 追踪系统记录。
        """
        snapshot = state.network_snapshot
        policy = state.yang_draft.network_policy if state.yang_draft else None
        plan = state.verified_flow_table_plan or state.flow_table_plan or {}
        if self.agent_id == "sense_agent":
            return {
                "requested_artifacts": ["topology", "hosts", "links", "runtime_flows"]
            }
        if self.agent_id == "intent_planner":
            return {
                "raw_intent": state.user_intent.text,
                "context": state.user_intent.context or {},
                "network_snapshot": self._snapshot_counts(snapshot),
            }
        if self.agent_id == "policy_conflict_checker":
            return {
                "policy_id": policy.policy_id if policy else None,
                "policy_name": policy.policy_name if policy else None,
                "action_type": (
                    self._enum_value(policy.action.action_type) if policy else None
                ),
                "existing_flow_count": len(snapshot.flows) if snapshot else 0,
            }
        if self.agent_id == "policy_compiler":
            return {
                "policy_id": policy.policy_id if policy else None,
                "approval_token_present": bool(
                    state.conflict_report and state.conflict_report.approval_token
                ),
                "retry_count": state.react_state.retry_count,
                "last_failure_stage": state.react_state.last_failure_stage,
            }
        if self.agent_id == "p4_rule_verifier":
            return {
                "total_entries": plan.get("total_entries"),
                "affected_devices": plan.get("affected_devices") or [],
                "approval_token_present": bool(
                    state.conflict_report and state.conflict_report.approval_token
                ),
            }
        if self.agent_id == "safety_council":
            return {
                "policy_id": policy.policy_id if policy else None,
                "plan_id": plan.get("plan_id"),
                "total_entries": plan.get("total_entries"),
                "affected_devices": plan.get("affected_devices") or [],
                "rule_verification_valid": (
                    state.rule_verification_report.is_valid
                    if state.rule_verification_report
                    else None
                ),
            }
        if self.agent_id == "p4_runtime_executor":
            return {
                "delivery_mode": "onos_rest",
                "write_request_count": len(plan.get("write_requests") or []),
                "affected_devices": plan.get("affected_devices") or [],
            }
        if self.agent_id == "status_verifier":
            return {
                "execution_status": (state.execution_result or {}).get("status"),
                "policy_id": policy.policy_id if policy else None,
                "action_type": (
                    self._enum_value(policy.action.action_type) if policy else None
                ),
            }
        return {}

    def _a2a_output_payload(
        self,
        state: GlobalAgentState,
        artifacts: dict | None = None,
    ) -> dict[str, Any]:
        """构建 A2A response 的输出载荷。

        返回每个阶段的典型输出摘要，供 A2A 追踪系统记录。
        """
        if self.agent_id == "sense_agent":
            topology_summary = getattr(state.network_snapshot, "topology_summary", None)
            return {
                "network_snapshot": self._snapshot_counts(state.network_snapshot),
                "topology_summary": (
                    topology_summary.model_dump(mode="json")
                    if topology_summary
                    else None
                ),
            }
        if self.agent_id == "intent_planner":
            return {
                "plan_id": state.intent_plan.plan_id if state.intent_plan else None,
                "sub_intent_count": (
                    len(state.intent_plan.sub_intents) if state.intent_plan else 0
                ),
                "policy_count": len(state.yang_policies),
                "current_policy_index": state.current_policy_index,
                "confidence_score": self._a2a_confidence(state),
                "needs_clarification": state.needs_clarification,
                "clarification_question": state.clarification_question,
            }
        if self.agent_id == "policy_conflict_checker":
            report = state.conflict_report
            return {
                "has_conflict": report.has_conflict if report else None,
                "conflict_detected": report.conflict_detected if report else None,
                "stop_category": report.stop_category.value if report else None,
                "conflict_type": report.conflict_type if report else None,
                "blocking": report.blocking if report else None,
                "reason_detail": report.explanation if report else None,
                "conflict_items": report.conflict_items if report else [],
                "resolution": report.resolution if report else None,
                "execution_mode": report.execution_mode if report else None,
                "existing_policy_id": report.existing_policy_id if report else None,
                "traffic_scope_key": report.traffic_scope_key if report else None,
                "approval_token_present": bool(report and report.approval_token),
                "approval_token": report.approval_token if report else None,
                "conflict_count": len(report.conflict_items) if report else 0,
                "warning_count": len(report.warning_items) if report else 0,
            }
        if self.agent_id == "policy_compiler":
            plan = state.flow_table_plan or {}
            return {
                "total_entries": plan.get("total_entries"),
                "affected_devices": plan.get("affected_devices") or [],
                "react_adjustments": plan.get("react_adjustments") or [],
            }
        if self.agent_id == "p4_rule_verifier":
            report = state.rule_verification_report
            return {
                "is_valid": report.is_valid if report else None,
                "violation_type": report.violation_type if report else None,
                "checked_items": report.checked_items if report else 0,
                "warning_count": len(report.warning_items) if report else 0,
                "violation_count": len(report.violation_items) if report else 0,
            }
        if self.agent_id == "safety_council":
            report = state.safety_council_report or {}
            return {
                "overall_decision": report.get("overall_decision"),
                "overall_risk_score": report.get("overall_risk_score"),
                "risk_level": report.get("risk_level"),
                "hard_blockers": report.get("hard_blockers") or [],
                "agent_reviews": [
                    {
                        "agent_id": item.get("agent_id"),
                        "decision": item.get("decision"),
                        "risk_score": item.get("risk_score"),
                    }
                    for item in report.get("agent_reviews") or []
                ],
            }
        if self.agent_id == "p4_runtime_executor":
            result = state.execution_result or {}
            return {
                "status": result.get("status"),
                "devices_confirmed": result.get("devices_confirmed") or [],
                "devices_failed": result.get("devices_failed") or [],
                "total_entries_written": result.get("total_entries_written"),
                "qos_enforcement": result.get("qos_enforcement"),
            }
        if self.agent_id == "status_verifier":
            report = state.verification_report or {}
            return {
                "passed": report.get("passed"),
                "intent_achieved": report.get("intent_achieved"),
                "telemetry_source": report.get("telemetry_source"),
                "kpi": report.get("kpi") or {},
                "violations": report.get("violations") or [],
                "root_cause": report.get("root_cause"),
                "auto_healing": bool(report.get("auto_healing")),
            }
        return artifacts or {}

    def _a2a_input_evidence_refs(self, state: GlobalAgentState) -> list[str]:
        """返回 A2A delegation 的证据引用列表。

        每个阶段列出其输入数据来源，用于审计追溯。
        """
        input_refs = {
            "sense_agent": [
                "MCP:onOS /topology/current",
                "MCP:onOS /hosts",
                "MCP:onOS /flows",
            ],
            "intent_planner": ["UserIntent.text", "NetworkSnapshot"],
            "policy_conflict_checker": [
                "YANGPolicy",
                "NetworkSnapshot.flows",
                "existing_policies",
            ],
            "policy_compiler": ["YANGPolicy", "P4Info", "NetworkSnapshot"],
            "p4_rule_verifier": [
                "FlowTablePlan",
                "P4Info",
                "approval_token",
                "existing_runtime_rules",
            ],
            "safety_council": [
                "RuleVerificationReport",
                "FlowTablePlan",
                "NetworkSnapshot",
                "YANGPolicy",
            ],
            "p4_runtime_executor": [
                "verified_flow_table_plan",
                "FlowAdapter",
                "FlowDelivery",
            ],
            "status_verifier": [
                "execution_result",
                "Telemetry API",
                "IntentConstraints",
            ],
            "human_approval_gate": ["ReactState", "last_failure_reason"],
        }
        return input_refs.get(self.agent_id, [])

    def _a2a_output_evidence_refs(self, state: GlobalAgentState) -> list[str]:
        """返回 A2A response 的证据引用列表。

        每个阶段列出其输出产物引用，供审计追溯。
        """
        if self.agent_id == "status_verifier":
            report = state.verification_report or {}
            refs = ["verification_report.kpi", "verification_report.violations"]
            if report.get("int_analysis"):
                refs.append("verification_report.int_analysis")
            if report.get("device_counters"):
                refs.append("verification_report.device_counters")
            if report.get("auto_healing"):
                refs.append("verification_report.auto_healing")
            return refs
        static_refs = {
            "sense_agent": [
                "NetworkSnapshot.devices",
                "NetworkSnapshot.links",
                "NetworkSnapshot.hosts",
            ],
            "intent_planner": ["IntentPlan", "YANGPolicy"],
            "policy_conflict_checker": ["ConflictReport", "approval_token"],
            "policy_compiler": [
                "FlowTablePlan.write_requests",
                "FlowTablePlan.rollback_requests",
            ],
            "p4_rule_verifier": ["RuleVerificationReport", "verified_flow_table_plan"],
            "safety_council": [
                "SafetyCouncilReport",
                "InvariantVerifierReport",
                "RollbackPlan",
            ],
            "p4_runtime_executor": ["execution_result", "qos_enforcement"],
            "human_approval_gate": ["hitl_payload"],
        }
        return static_refs.get(self.agent_id, [])

    def _a2a_confidence(self, state: GlobalAgentState) -> float | None:
        """计算 A2A 消息的综合置信度。

        取所有可用置信度值的最小值 —— 置信度链中最薄弱的环节
        决定了整体的可信程度。

        Returns:
            最小置信度 (0~1)，四舍五入到 3 位小数；无可用值时为 None
        """
        values: list[float] = []
        if state.intent_plan is not None:
            values.append(float(state.intent_plan.confidence_score))
        if state.yang_policies:
            values.extend(
                float(policy.confidence_score) for policy in state.yang_policies
            )
        if state.yang_draft is not None:
            values.append(float(state.yang_draft.confidence_score))
        return round(min(values), 3) if values else None

    def _a2a_risk_score(self, state: GlobalAgentState, status: StageStatus) -> int:
        """计算当前 A2A 消息的风险评分 (0~100)。

        风险评分的启发式规则：
        - 失败状态 → 90（最高风险，需要人工介入）
        - 存在策略冲突 → 85
        - P4 规则验证失败 → 78
        - KPI 验证失败 → 70
        - 需要用户澄清 → 55
        - 正在运行 → 35
        - 默认 → 20（低风险）

        Returns:
            风险评分整数 (0~100)
        """
        if status == StageStatus.FAILED:
            return 90
        if state.conflict_report and state.conflict_report.blocking:
            return 85
        if (
            state.rule_verification_report
            and not state.rule_verification_report.is_valid
        ):
            return 78
        if state.verification_passed is False:
            return 70
        if state.needs_clarification:
            return 55
        if status == StageStatus.RUNNING:
            return 35
        return 20

    def _a2a_consensus_decision(
        self,
        state: GlobalAgentState,
        status: StageStatus,
        summary: str,
    ) -> dict[str, Any] | None:
        """Generate and persist a structured, evidence-backed consensus.

        Risk score is useful evidence but is not an authorization decision.
        Only a concrete blocking report or failed stage can stop the pipeline.
        This prevents a successful same-scope QoS update from being converted
        into ``needs_repair`` merely because an aggregate score crossed 70.
        """
        source_agent = self._a2a_agent_id()
        # 定义 A2A 流水线中的下一个 Agent
        next_targets = {
            "intent_understanding": ["network_sensing"],
            "network_sensing": ["intent_planner"],
            "intent_planner": ["policy_conflict_checker"],
            "policy_conflict_checker": ["policy_compiler"],
            "policy_compiler": ["p4_rule_verifier"],
            "p4_rule_verifier": ["safety_council"],
            "safety_council": ["p4_runtime_executor"],
            "p4_runtime_executor": ["status_verifier"],
            "status_verifier": ["orchestrator"],
        }
        if source_agent not in next_targets:
            return None
        structured = self._build_consensus_decision(state, status, summary)
        state.consensus_decision = structured
        if state.yang_draft is not None:
            self._current_policy_result(state)["consensus_decision"] = (
                structured.model_dump(mode="json")
            )

        decision_payload = structured.model_dump(mode="json")
        consensus = structured.status.value.lower()
        decision = structured.summary
        policy = state.yang_draft.network_policy if state.yang_draft else None
        logger.info(
            json.dumps(
                {
                    "event": "consensus_decision",
                    "intent_id": state.intent_id,
                    "plan_id": state.intent_plan.plan_id if state.intent_plan else None,
                    "policy_id": policy.policy_id if policy else None,
                    "scope_key": structured.affected_scope_key,
                    "stage": self.stage.value,
                    "agent": source_agent,
                    "status": structured.status.value,
                    "decision_code": structured.decision_code,
                    "blocking": structured.blocking,
                    "reason_code": structured.reason_code.value,
                    "reason_detail": structured.reason_detail,
                    "duration_ms": None,
                    "exception_type": (
                        type(state.error_message).__name__
                        if status == StageStatus.FAILED and state.error_message
                        else None
                    ),
                    "retry_count": state.react_state.retry_count,
                },
                ensure_ascii=False,
                default=str,
            )
        )
        return {
            "target_agents": next_targets[source_agent],
            "decision": decision,
            "consensus": consensus,
            "payload": {
                **decision_payload,
                "decision": decision,
                "stage": self.stage.value,
                "stage_status": status.value,
                "risk_score": self._a2a_risk_score(state, status),
                "current_policy_index": state.current_policy_index,
            },
        }

    def _build_consensus_decision(
        self,
        state: GlobalAgentState,
        status: StageStatus,
        summary: str,
    ) -> ConsensusDecision:
        policy = state.yang_draft.network_policy if state.yang_draft else None
        report = (
            state.conflict_report
            if self.agent_id == "policy_conflict_checker"
            else None
        )
        affected_policy_id = (
            report.existing_policy_id
            if report and report.existing_policy_id
            else policy.policy_id if policy else None
        )
        affected_scope_key = (report.traffic_scope_key if report else None) or (
            state.execution_result or {}
        ).get("traffic_scope_key")
        base = {
            "summary": summary,
            "affected_policy_id": affected_policy_id,
            "affected_scope_key": affected_scope_key,
            "approval_token": report.approval_token if report else None,
            "original_policy_preserved": True,
        }

        if state.needs_clarification:
            return ConsensusDecision(
                status=ConsensusStatus.NEEDS_CLARIFICATION,
                decision_code="CLARIFICATION_REQUIRED",
                summary=state.clarification_question or summary,
                blocking=True,
                blocking_stage=self.stage.value,
                blocking_agent=self._a2a_agent_id(),
                reason_code=ConsensusReasonCode.SEMANTIC_AMBIGUITY,
                reason_detail={
                    "questions": list(state.clarification_questions or []),
                    "resume_intent_id": state.intent_id,
                },
                evidence=list(state.clarification_questions or []),
                retry_allowed=False,
                **{key: value for key, value in base.items() if key != "summary"},
            )

        if report and report.blocking:
            primary = dict((report.conflict_items or [{}])[0])
            reason_code = (
                report.stop_category
                if report.stop_category != ConsensusReasonCode.NONE
                else self._conflict_reason_code(report.conflict_type, primary)
            )
            repairable_codes = {
                ConsensusReasonCode.INSUFFICIENT_CAPACITY,
                ConsensusReasonCode.CAPACITY_ADMISSION_REJECTED,
                ConsensusReasonCode.TOPOLOGY_INCONSISTENT,
                ConsensusReasonCode.PATH_NOT_FOUND,
                ConsensusReasonCode.DEVICE_UNAVAILABLE,
                ConsensusReasonCode.INFRASTRUCTURE_UNAVAILABLE,
                ConsensusReasonCode.NETWORK_CONTEXT_UNKNOWN,
                ConsensusReasonCode.NETWORK_CONTEXT_STALE,
                ConsensusReasonCode.TELEMETRY_UNAVAILABLE,
                ConsensusReasonCode.EVIDENCE_INCOMPLETE,
            }
            needs_approval = bool(report.requires_manual_review)
            repairable = reason_code in repairable_codes
            detail = {
                **primary,
                "conflict_detected": report.conflict_detected,
                "stop_category": report.stop_category.value,
                "initial_admission_reason": report.initial_admission_reason,
                "recovery_action": report.recovery_action,
                "calibration_result": report.calibration_result,
                "final_reason_code": report.final_reason_code,
                "final_decision": report.final_decision,
                "southbound_execution": report.southbound_execution,
                "conflict_type": report.conflict_type,
                "severity": report.severity,
                "explanation": report.explanation,
            }
            if (
                reason_code in {
                    ConsensusReasonCode.INSUFFICIENT_CAPACITY,
                    ConsensusReasonCode.CAPACITY_ADMISSION_REJECTED,
                }
                and report.capacity_evidence
            ):
                capacity = report.capacity_evidence
                default_view = capacity.get("default_view") or {}
                blocking_direction = default_view.get("blocking_direction") or {}
                bottleneck = default_view.get("bottleneck") or {}
                detail.update(
                    {
                        "requested": capacity.get("requested_mbps"),
                        "capacity": (
                            blocking_direction.get("configured_capacity_mbps")
                            or bottleneck.get("configured_capacity_mbps")
                        ),
                        "reserved": capacity.get("reserved_mbps"),
                        "available": blocking_direction.get("available_mbps"),
                        "blocking_link": blocking_direction.get("direction_id")
                        or bottleneck.get("link_id"),
                        "blocking_device": blocking_direction.get("src_device"),
                        "blocking_interface": blocking_direction.get("src_port"),
                        "current_path_id": capacity.get("path_id"),
                        "capacity_evidence": capacity,
                        "original_policy_preserved": True,
                    }
                )
            topology_summary = getattr(state.network_snapshot, "topology_summary", None)
            if topology_summary is not None:
                detail["topology_summary"] = topology_summary.model_dump(mode="json")
            return ConsensusDecision(
                status=(
                    ConsensusStatus.NEEDS_APPROVAL
                    if needs_approval
                    else (
                        ConsensusStatus.NEEDS_REPAIR
                        if repairable
                        else ConsensusStatus.REJECTED
                    )
                ),
                decision_code="BLOCK_POLICY",
                summary=report.explanation or report.rejection_reason or summary,
                blocking=True,
                blocking_stage=self.stage.value,
                blocking_agent=self._a2a_agent_id(),
                reason_code=reason_code,
                reason_detail=detail,
                evidence=[dict(item) for item in report.conflict_items],
                repairable=repairable,
                repair_actions=list(report.suggestions),
                retry_allowed=repairable,
                **{key: value for key, value in base.items() if key != "summary"},
            )

        if report and not report.blocking:
            if report.execution_mode == "VERIFY_EXISTING":
                return ConsensusDecision(
                    status=ConsensusStatus.ACCEPTED,
                    decision_code="VERIFY_EXISTING",
                    reason_code=ConsensusReasonCode.DUPLICATE_POLICY,
                    reason_detail={
                        "resolution": report.resolution,
                        "old_qos": report.old_qos,
                        "new_qos": report.new_qos,
                    },
                    evidence=[dict(item) for item in report.warning_items],
                    summary=report.explanation
                    or "复用同范围同参数策略并验证现有配置。",
                    **{key: value for key, value in base.items() if key != "summary"},
                )
            if (
                report.resolution == "UPDATE_EXISTING_POLICY"
                or report.execution_mode == "MODIFY_QOS_RESOURCE"
            ):
                return ConsensusDecision(
                    status=ConsensusStatus.ACCEPTED,
                    decision_code="UPDATE_EXISTING_POLICY",
                    reason_code=ConsensusReasonCode.QOS_PARAMETER_UPDATE,
                    reason_detail={
                        "resolution": report.resolution,
                        "old_qos": report.old_qos,
                        "new_qos": report.new_qos,
                        "changed_fields": report.changed_fields,
                        "change_direction": report.change_direction,
                        "mode_change": report.mode_change,
                    },
                    evidence=[dict(item) for item in report.warning_items],
                    summary=report.explanation
                    or "同一流量范围 QoS 期望已变更，执行原地更新。",
                    **{key: value for key, value in base.items() if key != "summary"},
                )

        rule_report = (
            state.rule_verification_report
            if self.agent_id == "p4_rule_verifier"
            else None
        )
        if rule_report and not rule_report.is_valid:
            return ConsensusDecision(
                status=ConsensusStatus.NEEDS_REPAIR,
                decision_code="RECOMPILE_FLOW_PLAN",
                summary=rule_report.explanation or summary,
                blocking=True,
                blocking_stage=self.stage.value,
                blocking_agent=self._a2a_agent_id(),
                reason_code=ConsensusReasonCode.EVIDENCE_INCOMPLETE,
                reason_detail={
                    "violation_type": rule_report.violation_type,
                    "severity": rule_report.severity,
                    "explanation": rule_report.explanation,
                },
                evidence=[dict(item) for item in rule_report.violation_items],
                repairable=True,
                repair_actions=list(rule_report.suggestions),
                retry_allowed=state.react_state.can_retry,
                **{key: value for key, value in base.items() if key != "summary"},
            )

        safety = state.safety_council_report or {}
        if (
            self.agent_id == "safety_council"
            and safety
            and safety.get("overall_decision") != "approve"
        ):
            return ConsensusDecision(
                status=(
                    ConsensusStatus.NEEDS_APPROVAL
                    if safety.get("requires_human_approval")
                    else ConsensusStatus.REJECTED
                ),
                decision_code="SAFETY_BLOCK",
                summary=safety.get("final_rationale") or summary,
                blocking=True,
                blocking_stage=self.stage.value,
                blocking_agent=self._a2a_agent_id(),
                reason_code=ConsensusReasonCode.SAFETY_POLICY_BLOCKED,
                reason_detail={
                    "overall_decision": safety.get("overall_decision"),
                    "hard_blockers": safety.get("hard_blockers") or [],
                    "risk_score": safety.get("overall_risk_score"),
                },
                evidence=[
                    item
                    for item in safety.get("agent_reviews") or []
                    if isinstance(item, dict)
                ],
                repairable=False,
                repair_actions=list(safety.get("recommendations") or []),
                retry_allowed=False,
                **{key: value for key, value in base.items() if key != "summary"},
            )

        if self.agent_id == "status_verifier" and state.verification_passed is False:
            verification = state.verification_report or {}
            verification_state = str(verification.get("verification_state") or "").upper()
            if verification_state == "EXECUTION_READBACK_MISMATCH":
                return ConsensusDecision(
                    status=ConsensusStatus.NEEDS_REPAIR,
                    decision_code="RECONCILIATION_REQUIRED",
                    summary="Observed execution configuration differs from desired state; SLA assurance is blocked.",
                    blocking=True, blocking_stage="execution_readback", blocking_agent=self._a2a_agent_id(),
                    reason_code=ConsensusReasonCode.CONFIG_READBACK_FAILED,
                    reason_detail={"phase": "execution_readback", "outcome": "needs_reconciliation", "execution_verified": False, "violation_confirmed": False, "reconciliation_required": True, "diagnostics_required": False},
                    evidence=[verification.get("config_verification") or {}], next_action="RECONCILE_OBSERVED_CONFIG",
                    reconciliation_required=True, diagnostics_required=False, healing_ready=False, terminal=False,
                    repairable=True, retry_allowed=True,
                    **{key: value for key, value in base.items() if key != "summary"},
                )
            if verification_state in {"MEASUREMENT_INCONCLUSIVE", "NO_SAMPLE"}:
                return ConsensusDecision(
                    status=ConsensusStatus.NEEDS_REPAIR,
                    decision_code="MEASUREMENT_CONFIRMATION_REQUIRED",
                    summary="Valid assurance samples are insufficient; no SLA violation has been confirmed.",
                    blocking=False, reason_code=ConsensusReasonCode.EVIDENCE_INCOMPLETE,
                    reason_detail={"phase": "assurance", "outcome": "needs_measurement_confirmation", "execution_verified": True, "measurement_valid": False, "violation_confirmed": False, "reconciliation_required": False, "diagnostics_required": False},
                    next_action="MEASUREMENT_CONFIRMATION_REQUIRED", healing_ready=False, diagnostics_required=False,
                    reconciliation_required=False, terminal=False, repairable=False, retry_allowed=False,
                    **{key: value for key, value in base.items() if key != "summary"},
                )
            if verification_state == "VIOLATION_CONFIRMED_ROOT_CAUSE_PENDING":
                root = verification.get("root_cause_report") or {}
                return ConsensusDecision(
                    status=ConsensusStatus.NEEDS_DIAGNOSTICS,
                    decision_code="DIAGNOSTICS_REQUIRED",
                    summary="Root cause evidence is insufficient; collect diagnostics without re-executing the policy.",
                    blocking=False,
                    reason_code=ConsensusReasonCode.ROOT_CAUSE_EVIDENCE_INSUFFICIENT,
                    reason_detail={
                        "verification_state": verification_state,
                        "missing_evidence": list(root.get("missing_evidence") or []),
                    },
                    evidence=[item for item in root.get("evidence") or [] if isinstance(item, dict)],
                    next_action="COLLECT_DIAGNOSTIC_EVIDENCE",
                    root_cause_status="PENDING",
                    healing_ready=False,
                    diagnostics_required=True,
                    reconciliation_required=False,
                    terminal=False,
                    repairable=False,
                    retry_allowed=False,
                    **{key: value for key, value in base.items() if key != "summary"},
                )
            if state.current_status == IntentStatus.UNSATISFIABLE:
                terminal_reason = str(
                    verification.get("terminal_reason")
                    or state.error_message
                    or "NO_FEASIBLE_HEALING_ACTION"
                )
                reason_code = (
                    ConsensusReasonCode.MAX_HEALING_ATTEMPTS_EXHAUSTED
                    if "MAX_HEALING_ATTEMPTS_EXHAUSTED" in terminal_reason
                    else ConsensusReasonCode.NO_FEASIBLE_HEALING_ACTION
                )
                return ConsensusDecision(
                    status=ConsensusStatus.TERMINAL,
                    decision_code="UNSATISFIABLE",
                    summary=terminal_reason,
                    blocking=True,
                    blocking_stage=self.stage.value,
                    blocking_agent=self._a2a_agent_id(),
                    reason_code=reason_code,
                    reason_detail={
                        "verification_state": "UNSATISFIABLE",
                        "terminal_reason": terminal_reason,
                        "auto_healing": verification.get("auto_healing") or {},
                    },
                    evidence=[
                        item
                        for item in verification.get("evidence") or []
                        if isinstance(item, dict)
                    ],
                    repairable=False,
                    repair_actions=[],
                    retry_allowed=False,
                    **{key: value for key, value in base.items() if key != "summary"},
                )
            reason_code = self._verification_reason_code(verification)
            return ConsensusDecision(
                status=ConsensusStatus.NEEDS_REPAIR,
                decision_code="REACT_OR_ROLLBACK",
                summary=verification.get("root_cause") or summary,
                blocking=True,
                blocking_stage=self.stage.value,
                blocking_agent=self._a2a_agent_id(),
                reason_code=reason_code,
                reason_detail={
                    "verification_state": verification.get("verification_state"),
                    "root_cause": verification.get("root_cause"),
                    "violations": verification.get("violations") or [],
                },
                evidence=[
                    item
                    for item in verification.get("evidence") or []
                    if isinstance(item, dict)
                ],
                repairable=True,
                repair_actions=list(verification.get("repair_actions") or []),
                retry_allowed=state.react_state.can_retry,
                **{key: value for key, value in base.items() if key != "summary"},
            )

        if status == StageStatus.FAILED:
            return ConsensusDecision(
                status=ConsensusStatus.RETRYABLE_FAILURE,
                decision_code="STAGE_FAILED",
                summary=state.error_message or summary,
                blocking=True,
                blocking_stage=self.stage.value,
                blocking_agent=self._a2a_agent_id(),
                reason_code=ConsensusReasonCode.INTERNAL_AGENT_ERROR,
                reason_detail={"error": state.error_message or summary},
                repairable=True,
                repair_actions=[
                    self._a2a_retry_suggestion(state, status)
                    or "检查异常堆栈后重试该阶段。"
                ],
                retry_allowed=state.react_state.can_retry,
                **{key: value for key, value in base.items() if key != "summary"},
            )

        return ConsensusDecision(
            status=ConsensusStatus.ACCEPTED,
            decision_code="CONTINUE",
            reason_code=ConsensusReasonCode.NONE,
            reason_detail={"stage_status": status.value},
            summary=self._a2a_decision_label(state, summary, "accepted"),
            **{key: value for key, value in base.items() if key != "summary"},
        )

    def _conflict_reason_code(
        self,
        conflict_type: str | None,
        primary: dict[str, Any],
    ) -> ConsensusReasonCode:
        value = str(
            conflict_type or primary.get("conflict_type") or primary.get("type") or ""
        ).upper()
        if value == "RESOURCE_CONFLICT":
            return ConsensusReasonCode.INSUFFICIENT_CAPACITY
        if value == "INFRASTRUCTURE_UNAVAILABLE":
            return ConsensusReasonCode.INFRASTRUCTURE_UNAVAILABLE
        if value == "NETWORK_CONTEXT_UNKNOWN":
            return ConsensusReasonCode.NETWORK_CONTEXT_UNKNOWN
        if value == "NETWORK_CONTEXT_STALE":
            return ConsensusReasonCode.NETWORK_CONTEXT_STALE
        if primary.get("unavailable_devices") or value == "DEVICE_UNAVAILABLE":
            return ConsensusReasonCode.DEVICE_UNAVAILABLE
        if value in {"TOPOLOGY_CONFLICT", "TOPOLOGY_INCONSISTENT"}:
            return ConsensusReasonCode.TOPOLOGY_INCONSISTENT
        if value in {"PATH_NOT_FOUND", "RESOURCE_CHECK_SCOPE_ERROR"}:
            return ConsensusReasonCode.PATH_NOT_FOUND
        if value == "LOW_CONFIDENCE_INTENT":
            return ConsensusReasonCode.EVIDENCE_INCOMPLETE
        if value == "DUPLICATE_POLICY":
            return ConsensusReasonCode.DUPLICATE_POLICY
        return ConsensusReasonCode.TRUE_POLICY_CONFLICT

    def _verification_reason_code(self, report: dict[str, Any]) -> ConsensusReasonCode:
        text = " ".join(
            str(report.get(key) or "")
            for key in ("verification_state", "root_cause", "failure_reason")
        ).upper()
        for marker, code in (
            ("P4_COUNTER_ZERO", ConsensusReasonCode.P4_COUNTER_ZERO),
            ("TC_APPLY", ConsensusReasonCode.TC_APPLY_FAILED),
            ("READBACK", ConsensusReasonCode.CONFIG_READBACK_FAILED),
            ("TELEMETRY", ConsensusReasonCode.TELEMETRY_UNAVAILABLE),
        ):
            if marker in text:
                return code
        return ConsensusReasonCode.KPI_FAILED

    def _a2a_decision_label(
        self,
        state: GlobalAgentState,
        summary: str,
        consensus: str,
    ) -> str:
        """生成 A2A 共识决策的中文标签。

        每个阶段有特定的决策文案，直接用于前端展示。
        """
        if self.agent_id == "policy_conflict_checker" and state.conflict_report:
            return (
                "拒绝策略并停止下发"
                if state.conflict_report.blocking
                else "签发 approval_token，允许进入编译"
            )
        if self.agent_id == "p4_rule_verifier" and state.rule_verification_report:
            return (
                "流表计划通过表项级验证"
                if state.rule_verification_report.is_valid
                else "阻断流表计划并要求重编译"
            )
        if self.agent_id == "safety_council" and state.safety_council_report:
            decision = state.safety_council_report.get("overall_decision")
            return (
                "安全评审通过，允许进入执行"
                if decision == "approve"
                else "安全评审未通过，阻断执行"
            )
        if self.agent_id == "status_verifier":
            if state.verification_passed:
                return "KPI 验证通过，意图达成"
            return "KPI 未达标，触发 ReAct 或自愈"
        if consensus == "blocked":
            return f"{self._a2a_agent_id()} 阻断当前阶段"
        return summary

    # ── 通用辅助方法 ──

    def _snapshot_counts(self, snapshot: Any) -> dict[str, int]:
        """统计网络快照中的实体数量。

        Args:
            snapshot: NetworkSnapshot 对象或 None

        Returns:
            {"devices": N, "links": N, "hosts": N, "flows": N}
        """
        if not snapshot:
            return {"devices": 0, "links": 0, "hosts": 0, "flows": 0}
        return {
            "devices": len(getattr(snapshot, "devices", []) or []),
            "links": len(getattr(snapshot, "links", []) or []),
            "hosts": len(getattr(snapshot, "hosts", []) or []),
            "flows": len(getattr(snapshot, "flows", []) or []),
        }

    def _enum_value(self, value: Any) -> Any:
        """安全提取枚举值：如果是 Enum 则返回 .value，否则返回原值。

        【难点解释】Python Enum 的 str 表示形式可能包含类名前缀，
        使用 getattr 提取 .value 确保获得纯字符串值。
        """
        return getattr(value, "value", value)

    def _current_policy_result(self, state: GlobalAgentState) -> dict[str, Any]:
        """获取当前策略（current_policy_index）对应的结果桶。

        如果 policy_results 列表长度不足，自动扩展并填充默认字段。

        Returns:
            当前策略的结果 dict（可变引用，直接修改即可持久化）
        """
        while len(state.policy_results) <= state.current_policy_index:
            state.policy_results.append({})

        result = state.policy_results[state.current_policy_index]
        result.setdefault("policy_index", state.current_policy_index)
        if state.yang_draft is not None:
            result.setdefault("policy_id", state.yang_draft.network_policy.policy_id)
            result.setdefault(
                "policy_name", state.yang_draft.network_policy.policy_name
            )
        return result

    def _push_progress(
        self,
        state: GlobalAgentState,
        message: str,
        phase: str = "",
    ) -> None:
        """推送阶段内进度事件到前端（SSE phase_progress 事件）。

        与 _end_log 不同，此方法不结束阶段 —— 它通知前端
        长耗时节点内部正在进行的工作（如 LLM 调用、批量编译）。

        Args:
            state: 当前全局状态
            message: 进度消息（中文）
            phase: 子阶段名称（如 "intent_parsing", "policy_modeling"）
        """
        try:
            from api.events import get_event_manager

            get_event_manager().push(
                state.intent_id,
                "phase_progress",
                {
                    "stage": self.stage.value,
                    "agent_id": self.agent_id,
                    "phase": phase,
                    "message": message,
                    "timestamp": datetime.utcnow().isoformat(),
                },
            )
        except Exception as exc:
            logger.warning(
                "[%s] SSE phase_progress push failed: %s", self.agent_id, exc
            )

    def _prepare_next_policy(self, state: GlobalAgentState) -> bool:
        """将工作流光标移动到下一个 YANG Policy。

        当当前策略的所有阶段（check → compile → verify → execute → verify）
        都成功完成后调用，切换到下一个策略。

        操作：
        1. 递增 current_policy_index
        2. 设置 yang_draft = 下一个 YANGPolicy
        3. 重置所有 per-policy 中间状态为 None
        4. 重置 ReAct 重试计数

        Returns:
            True: 已切换到下一个策略
            False: 没有更多策略（所有策略处理完毕）
        """
        next_index = state.current_policy_index + 1
        if next_index >= len(state.yang_policies):
            return False

        state.current_policy_index = next_index
        state.yang_draft = state.yang_policies[next_index]
        # 重置 per-policy 中间状态 —— 每个策略从头开始
        state.conflict_report = None
        state.flow_table_plan = None
        state.rule_verification_report = None
        state.verified_flow_table_plan = None
        state.safety_council_report = None
        state.execution_result = None
        state.pre_execution_telemetry = None
        state.telemetry_data = None
        state.verification_passed = None
        state.verification_report = None
        state.error_message = None
        # 重置 ReAct 重试计数 —— 每个策略独立享有 3 次重试机会
        from models.agent_state import ReactState

        state.react_state = ReactState()
        return True

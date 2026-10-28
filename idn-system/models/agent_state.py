"""
models/agent_state.py
=====================

LangGraph 全局智能体状态定义文件，是整个 STAV（Sense → Think → Act → Verify）
闭环工作流的核心状态容器。

# 系统角色
本文件定义了 IDN-System 中贯穿意图处理全生命周期的所有数据结构，
包括：全局状态、阶段执行日志、ReAct 回溯状态、冲突检测报告、
P4Runtime 表项级验证报告，以及标准化错误信息格式。

# 主要依赖
- pydantic.BaseModel：所有状态的基类，提供类型校验和序列化能力
- models.intent：用户意图模型（UserIntent, IntentStatus）
- models.intent_plan：意图计划模型（IntentPlan）
- models.yang_policy：YANG 策略模型（YANGPolicy）
- models.network：网络拓扑快照（NetworkSnapshot）
- models.telemetry：遥测数据模型（TelemetryData）

# 与其他模块交互
- orchestrator.graph：使用 GlobalAgentState 作为 LangGraph StateGraph 的泛型参数，
  每个节点函数的输入/输出均为 GlobalAgentState
- orchestrator.nodes.*：各个阶段节点通过读写 GlobalAgentState 的属性进行通信
- api.*：REST API 将 GlobalAgentState 序列化为 JSON 返回给前端，
  前端通过 ErrorInfo 结构体获取标准化错误信息

# 设计意图
- GlobalAgentState 作为单一数据源（Single Source of Truth），
  所有 Agent 节点共享此对象，避免数据分散
- ReactState 控制重试上限，防止无限循环（最多 3 次重试）
- ConflictReport / RuleVerificationReport 提供结构化的阻断/警告信息，
  供路由函数决策下一步流向
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from .intent import UserIntent, IntentStatus
from .intent_plan import IntentPlan
from .yang_policy import YANGPolicy
from .network import NetworkSnapshot
from .telemetry import TelemetryData


class ErrorType(str, Enum):
    """
    标准化错误类别枚举

    用于 API 层向客户端返回机器可读的错误分类，
    使前端可以根据不同错误类型采取差异化处理策略。

    【难点解释】使用 str+Enum 的混合继承，使枚举值可直接用于 JSON 序列化，
    避免 FastAPI 响应中额外的 .value 转换。
    """
    NETWORK = "NETWORK"           # 网络层错误（连接失败、ONOS 不可达）
    LLM = "LLM"                   # 大模型错误（调用失败、响应格式错误）
    VALIDATION = "VALIDATION"     # 数据校验错误（Pydantic 模型校验失败）
    TIMEOUT = "TIMEOUT"           # 超时错误（节点执行超时、LLM 调用超时）
    INTERNAL = "INTERNAL"         # 内部未知错误（兜底分类）


class ConsensusStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    NEEDS_REPAIR = "NEEDS_REPAIR"
    TERMINAL = "TERMINAL"
    REJECTED = "REJECTED"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    NEEDS_DIAGNOSTICS = "NEEDS_DIAGNOSTICS"


class ConsensusReasonCode(str, Enum):
    NONE = "NONE"
    # Terminal stop categories are intentionally part of the existing
    # consensus reason-code vocabulary.  A workflow may be blocked for a
    # capacity-admission, semantic, resource, execution, or assurance reason
    # without falsely describing all of them as policy conflicts.
    TRUE_POLICY_CONFLICT = "TRUE_POLICY_CONFLICT"
    CAPACITY_ADMISSION_REJECTED = "CAPACITY_ADMISSION_REJECTED"
    RESOURCE_UNRESOLVED = "RESOURCE_UNRESOLVED"
    SEMANTIC_VALIDATION_FAILED = "SEMANTIC_VALIDATION_FAILED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    ASSURANCE_FAILED = "ASSURANCE_FAILED"
    QOS_PARAMETER_UPDATE = "QOS_PARAMETER_UPDATE"
    DUPLICATE_POLICY = "DUPLICATE_POLICY"
    INSUFFICIENT_CAPACITY = "INSUFFICIENT_CAPACITY"
    TOPOLOGY_INCONSISTENT = "TOPOLOGY_INCONSISTENT"
    PATH_NOT_FOUND = "PATH_NOT_FOUND"
    DEVICE_UNAVAILABLE = "DEVICE_UNAVAILABLE"
    INFRASTRUCTURE_UNAVAILABLE = "INFRASTRUCTURE_UNAVAILABLE"
    NETWORK_CONTEXT_UNKNOWN = "NETWORK_CONTEXT_UNKNOWN"
    NETWORK_CONTEXT_STALE = "NETWORK_CONTEXT_STALE"
    FLOW_INSTALL_FAILED = "FLOW_INSTALL_FAILED"
    TC_APPLY_FAILED = "TC_APPLY_FAILED"
    CONFIG_READBACK_FAILED = "CONFIG_READBACK_FAILED"
    P4_COUNTER_ZERO = "P4_COUNTER_ZERO"
    KPI_FAILED = "KPI_FAILED"
    SAFETY_POLICY_BLOCKED = "SAFETY_POLICY_BLOCKED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    TELEMETRY_UNAVAILABLE = "TELEMETRY_UNAVAILABLE"
    INTERNAL_AGENT_ERROR = "INTERNAL_AGENT_ERROR"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    SEMANTIC_AMBIGUITY = "SEMANTIC_AMBIGUITY"
    NO_FEASIBLE_HEALING_ACTION = "NO_FEASIBLE_HEALING_ACTION"
    MAX_HEALING_ATTEMPTS_EXHAUSTED = "MAX_HEALING_ATTEMPTS_EXHAUSTED"
    ROOT_CAUSE_EVIDENCE_INSUFFICIENT = "ROOT_CAUSE_EVIDENCE_INSUFFICIENT"


class ConsensusDecision(BaseModel):
    """Auditable decision emitted by every A2A consensus synchronization."""

    status: ConsensusStatus = ConsensusStatus.ACCEPTED
    decision_code: str = "CONTINUE"
    summary: str = ""
    blocking: bool = False
    blocking_stage: Optional[str] = None
    blocking_agent: Optional[str] = None
    reason_code: ConsensusReasonCode = ConsensusReasonCode.NONE
    reason_detail: Dict[str, Any] = Field(default_factory=dict)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    affected_policy_id: Optional[str] = None
    affected_scope_key: Optional[str] = None
    approval_token: Optional[str] = None
    repairable: bool = False
    repair_actions: List[str] = Field(default_factory=list)
    retry_allowed: bool = False
    original_policy_preserved: bool = True
    # These fields keep diagnostics distinct from a repair/recompile request.
    next_action: Optional[str] = None
    root_cause_status: Optional[str] = None
    healing_ready: bool = False
    diagnostics_required: bool = False
    reconciliation_required: bool = False
    terminal: bool = False
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ErrorInfo(BaseModel):
    """
    标准化错误载荷

    贯穿整个 STAV 流水线的统一错误表示。无论在哪个阶段发生错误，
    都使用此结构体返回给 API 层，确保前端获取一致的错误信息格式。

    属性说明：
    - error_code：机器可读的错误码（如 "TIMEOUT-0001"），便于自动处理
    - message：人类可读的中文错误描述
    - stage：错误发生的流水线阶段（sense/parse/check/generate/execute/verify）
    - retryable：是否可重试，影响前端是否显示"重试"按钮
    - details：附加上下文，键值对形式，灵活扩展
    """
    error_type: ErrorType = Field(default=ErrorType.INTERNAL, description="Error category")
    error_code: str = Field(default="INTERNAL-0000", description="Machine-readable error code")
    message: str = Field(default="", description="Human-readable error message")
    stage: Optional[str] = Field(default=None, description="Pipeline stage where the error occurred")
    agent_id: Optional[str] = Field(default=None, description="Agent that reported the error")
    retryable: bool = Field(default=False, description="Whether the operation can be retried")
    retry_after_seconds: Optional[int] = Field(default=None, description="Suggested wait before retry")
    details: Dict[str, Any] = Field(default_factory=dict, description="Additional context")

    @classmethod
    def from_exception(cls, exc: Exception, stage: str | None = None, agent_id: str | None = None) -> "ErrorInfo":
        """
        将 Python 异常自动分类为标准化 ErrorInfo

        【难点解释】此方法通过异常类型名称的字符串匹配来实现自动分类，
        这是一种 pragmatic 的做法——避免了在每个 try/except 中手动构造 ErrorInfo。
        分类顺序很重要：先检查特定类型（TimeoutError），再检查关键字匹配，
        最后以 INTERNAL 作为兜底。

        Args:
            exc: 原始 Python 异常对象
            stage: 错误发生的流水线阶段（可选）
            agent_id: 产生错误的 Agent 标识（可选）

        Returns:
            包含分类信息和错误上下文的 ErrorInfo 实例
        """
        import asyncio
        exc_name = type(exc).__name__
        exc_msg = str(exc)

        # ── 超时类错误：async wait_for 超时或被取消 ──
        if isinstance(exc, asyncio.TimeoutError) or "Timeout" in exc_name:
            return cls(error_type=ErrorType.TIMEOUT, error_code="TIMEOUT-0001",
                       message=exc_msg, stage=stage, agent_id=agent_id, retryable=True, retry_after_seconds=10)

        # ── 网络类错误：ONOS 流表下发失败 ──
        try:
            from infra.flow_transaction import FlowDeliveryError
            if isinstance(exc, FlowDeliveryError):
                return cls(error_type=ErrorType.NETWORK, error_code="ONOS-0001",
                           message=exc_msg, stage=stage, agent_id=agent_id, retryable=False)
        except ImportError:
            pass
        # 通用网络异常关键字匹配（ConnectionError, HTTPError, socket 超时等）
        if any(k in exc_name.lower() for k in ("connection", "http", "socket", "timeout", "httpx")):
            return cls(error_type=ErrorType.NETWORK, error_code="NET-0001",
                       message=exc_msg, stage=stage, agent_id=agent_id, retryable=True, retry_after_seconds=5)

        # ── LLM 类错误：大模型服务不可用或调用失败 ──
        if "llm" in exc_name.lower() or "langchain" in exc_name.lower() or "openai" in exc_name.lower():
            return cls(error_type=ErrorType.LLM, error_code="LLM-0001",
                       message=exc_msg, stage=stage, agent_id=agent_id, retryable=True, retry_after_seconds=10)

        # ── 校验类错误：Pydantic 模型校验失败 ──
        if "validation" in exc_name.lower() or "pydantic" in exc_name.lower():
            return cls(error_type=ErrorType.VALIDATION, error_code="VAL-0001",
                       message=exc_msg, stage=stage, agent_id=agent_id, retryable=False)

        # ── 兜底：无法分类的内部错误 ──
        return cls(error_type=ErrorType.INTERNAL, error_code="INTERNAL-0000",
                   message=exc_msg, stage=stage, agent_id=agent_id, retryable=False)


class StageStatus(str, Enum):
    """阶段执行状态枚举

    用于 ExecutionLogEntry 中的状态标记，
    表示单个 STAV 阶段的执行结果。ROLLBACK 和 SKIPPED
    为特殊状态，分别用于回滚和路由跳过的场景。
    """
    PENDING = "pending"    # 等待执行
    RUNNING = "running"    # 正在执行
    SUCCESS = "success"    # 执行成功
    FAILED = "failed"      # 执行失败
    ROLLBACK = "rollback"  # 已触发回滚
    SKIPPED = "skipped"    # 被跳过（conditional edge 未进入该分支）


class ExecutionStage(str, Enum):
    """执行阶段枚举 (STAV 闭环)

    对应 IDN-System 核心闭环的六个阶段，
    每个阶段在 LangGraph 中对应一个或多个节点。

    数据流向：SENSE → PARSE → CHECK → GENERATE → EXECUTE → VERIFY
    失败回溯：VERIFY 失败 → GENERATE（ReAct 重试）或 → human_approval
    """
    UNDERSTANDING = "understanding"  # 业务语义与端点/服务解析（前置门禁）
    SENSE = "sense"       # 感知阶段：通过网络感知模块采集拓扑和流表快照
    PARSE = "parse"       # 意图解析：自然语言 → 结构化 IntentPlan
    CHECK = "check"       # 冲突检测：检查新策略是否与存量策略冲突
    GENERATE = "generate" # 策略生成：YANGPolicy → P4Runtime WriteRequest 序列
    EXECUTE = "execute"   # 流表下发：通过 ONOS REST 将表项写入数据平面
    VERIFY = "verify"     # 效果验证：采集遥测数据，比对 KPI 约束


class ExecutionLogEntry(BaseModel):
    """
    单次执行阶段的日志记录

    每个节点执行完成后生成一条 ExecutionLogEntry，
    追加到 GlobalAgentState.execution_log 列表中，
    构成完整的 Sense→Think→Act→Verify 审计链。

    用途：
    - 前端时间线展示：按 execution_log 渲染阶段状态时间线
    - 审计追溯：事后排查问题时可回溯每个阶段的输入输出
    - A2A 追踪：通过 artifacts 字段记录关键中间产物
    """
    stage: ExecutionStage = Field(..., description="执行阶段")
    agent_id: str = Field(..., description="执行该阶段的Agent标识")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="阶段开始时间戳"
    )
    status: StageStatus = Field(
        default=StageStatus.PENDING,
        description="阶段执行状态"
    )
    output_summary: Optional[str] = Field(
        default=None,
        description="阶段输出摘要"
    )
    error_message: Optional[str] = Field(
        default=None,
        description="错误信息 (status=FAILED时)"
    )
    duration_ms: Optional[float] = Field(
        default=None,
        description="执行耗时 (毫秒)"
    )
    artifacts: Optional[Dict[str, Any]] = Field(
        default=None,
        description="该阶段产出的关键数据快照"
    )


class ReactState(BaseModel):
    """
    ReAct 重试状态跟踪

    监控验证失败时的自动修正循环。当 VERIFY 阶段检测到 KPI 不达标时，
    系统不会立即放弃，而是通过 ReAct 机制回退到 GENERATE 阶段重新编译策略。
    此模型跟踪重试次数和失败原因，防止无限循环。

    重试策略：
    - 最多重试 3 次（retry_count ∈ [0, 3]）
    - 每次重试尝试不同策略（调整优先级、放宽约束、换用备用路径）
    - 3 次后仍未达标 → needs_human_approval = True → 升级到人工审批
    """
    retry_count: int = Field(
        default=0,
        ge=0, le=3,
        description="当前重试次数 (最多3次)"
    )
    max_retries: int = Field(
        default=3,
        description="最大重试次数上限"
    )
    last_failure_reason: Optional[str] = Field(
        default=None,
        description="上次失败的根因分析结果"
    )
    last_failure_stage: Optional[str] = Field(
        default=None,
        description="上次失败发生的阶段"
    )
    adjustment_history: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="历次策略调整记录"
    )

    @property
    def can_retry(self) -> bool:
        """判断是否还可以重试

        在 route_after_verification 和 route_after_rule_verification 中使用，
        决定是走 ReAct 回溯还是升级到人工审批。
        """
        return self.retry_count < self.max_retries

    @property
    def needs_human_approval(self) -> bool:
        """判断是否需要人工审批

        当所有自动重试次数用尽后，系统不再自行修复，
        而是挂起流程等待人工介入。
        """
        return self.retry_count >= self.max_retries


class ConflictReport(BaseModel):
    """
    冲突检测报告

    由 ConflictCheckerAgent（或 CheckConflictNode）在 CHECK 阶段生成。
    检测新意图策略与网络中已有策略之间的冲突，包括：
    - 资源冲突（带宽竞争、优先级冲突）
    - 语义冲突（规则动作互斥，如 DROP vs FORWARD）
    - 拓扑兼容性（路径是否存在、设备是否可达）

    approval_token 机制：
    - 无冲突时签发 approval_token，作为下游阶段的"通行证"
    - 有冲突时不签发 token，流程在 conflict check 后终止
    """
    has_conflict: bool = Field(default=False, description="是否存在冲突")
    # ``has_conflict`` is reserved for an actual candidate-vs-existing-policy
    # contradiction.  ``blocking`` may also be true for independent admission
    # or infrastructure gates.  These explicit fields avoid presenting a
    # capacity rejection as a policy conflict in consensus and UI projections.
    conflict_detected: bool = Field(default=False, description="是否检测到真实现有策略冲突")
    stop_category: ConsensusReasonCode = Field(
        default=ConsensusReasonCode.NONE,
        description="统一终止类别；与具体 final_reason_code 分开",
    )
    initial_admission_reason: Optional[str] = Field(
        default=None,
        description="候选校准前的准入原因",
    )
    recovery_action: Optional[str] = Field(
        default=None,
        description="为恢复准入而实际执行的有界动作",
    )
    calibration_result: Optional[str] = Field(
        default=None,
        description="候选容量校准结果，例如 CALIBRATED/REJECTED",
    )
    final_reason_code: Optional[str] = Field(
        default=None,
        description="当前流程最终、最具体的机器可读原因",
    )
    final_decision: Optional[str] = Field(
        default=None,
        description="最终准入结论，例如 UNSATISFIABLE",
    )
    southbound_execution: Optional[str] = Field(
        default=None,
        description="南向执行状态，例如 NOT_EXECUTED",
    )
    conflict_type: Optional[str] = Field(
        default=None,
        description="最高优先级冲突类型"
    )
    severity: Optional[str] = Field(
        default=None,
        description="最高冲突严重程度: HIGH/MEDIUM/LOW"
    )
    explanation: Optional[str] = Field(
        default=None,
        description="中文冲突解释"
    )
    suggestions: List[str] = Field(
        default_factory=list,
        description="中文处理建议"
    )
    warnings: List[str] = Field(
        default_factory=list,
        description="不阻断流程的低风险警告"
    )
    warning_items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="不阻断流程的低风险警告清单"
    )
    conflict_items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="冲突清单"
    )
    capacity_evidence: Dict[str, Any] = Field(
        default_factory=dict,
        description="方向性原始链路容量与物理链路聚合摘要",
    )
    admission_decision: Dict[str, Any] = Field(
        default_factory=dict,
        description="Authoritative pre-compiler guaranteed-capacity admission decision",
    )
    approval_token: Optional[str] = Field(
        default=None,
        description="审批令牌 (无冲突时签发)"
    )
    rejection_reason: Optional[str] = Field(
        default=None,
        description="拒绝原因 (有冲突时)"
    )
    blocking: bool = Field(default=False, description="该决策是否阻断执行")
    context_refresh_required: bool = Field(
        default=False,
        description="Availability unknown/stale and one bounded refresh is required before a decision",
    )
    requires_manual_review: bool = Field(default=False, description="是否需要人工审批")
    resolution: Optional[str] = Field(default=None, description="冲突/重复项的解析语义")
    execution_mode: str = Field(default="CREATE", description="CREATE/MODIFY/VERIFY_EXISTING")
    existing_policy_id: Optional[str] = Field(default=None, description="被复用或更新的策略ID")
    traffic_scope_key: Optional[str] = Field(default=None, description="不含QoS参数的稳定流量范围键")
    old_qos: Optional[Dict[str, Any]] = Field(default=None, description="更新前QoS参数")
    new_qos: Optional[Dict[str, Any]] = Field(default=None, description="更新后QoS参数")
    changed_fields: List[str] = Field(default_factory=list, description="发生变化的QoS字段")
    old_bandwidth_mbps: Optional[int] = None
    new_bandwidth_mbps: Optional[int] = None
    change_direction: Optional[str] = Field(default=None, description="UPGRADE/DOWNGRADE/PARAMETER_CHANGE")
    mode_change: Optional[str] = Field(
        default=None,
        description="QoS mode transition, e.g. RATE_LIMIT -> MIN_BANDWIDTH",
    )
    released_bandwidth_mbps: int = Field(default=0, description="降级时释放的带宽资源")
    checked_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="检测时间"
    )


class RuleVerificationReport(BaseModel):
    """
    P4Runtime 表项级验证报告

    由 P4RuleVerifierAgent 在 GENERATE 阶段之后生成。
    验证 PolicyCompiler 生成的流表项是否满足：
    - P4Info schema 一致性（字段名、匹配类型、动作参数）
    - 资源约束（TCAM 容量、SRAM 容量）
    - 运行时规则无冲突（不与已安装规则重叠）

    验证失败时触发 ReAct 重试（重新编译策略），
    而非直接升级到人工审批——只有在 can_retry=False 时才升级。
    """
    is_valid: bool = Field(default=True, description="生成的流表计划是否可以继续下发")
    violation_type: Optional[str] = Field(
        default=None,
        description="最高优先级违规类型"
    )
    severity: Optional[str] = Field(
        default=None,
        description="最高违规严重程度: HIGH/MEDIUM/LOW"
    )
    explanation: Optional[str] = Field(
        default=None,
        description="中文验证解释"
    )
    suggestions: List[str] = Field(
        default_factory=list,
        description="中文处理建议"
    )
    warnings: List[str] = Field(
        default_factory=list,
        description="不阻断流程的低风险警告"
    )
    warning_items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="不阻断流程的低风险警告清单"
    )
    violation_items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="阻断性违规清单"
    )
    checked_items: int = Field(default=0, description="已检查的表项数量")
    verified_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="验证时间"
    )


class GlobalAgentState(BaseModel):
    """
    全局智能体状态 (LangGraph StateGraph 的核心状态容器)

    贯穿整个 STAV 闭环周期，所有 Agent 节点共享此状态对象。
    LangGraph 会在每个节点执行前后自动进行 Checkpoint 持久化，
    支持暂停/恢复和故障恢复。

    # 生命周期
    1. API 层创建 UserIntent → 构造 GlobalAgentState
    2. LangGraph 工作流启动，state 在 12 个节点间流转
    3. 每个节点读取 state 中的上游产物，写入自己的产物
    4. 最终 state 包含完整的审计链（execution_log）

    # 多策略处理
    当 IntentPlan 包含多个 SubIntent 时（如"限制 P2P 并保障视频"），
    yang_policies 列表可能包含多个 YANGPolicy。
    系统逐策略处理：current_policy_index 跟踪当前处理位置，
    每个策略完成 VERIFY 阶段后递增索引，处理下一个策略。

    # Checkpoint 持久化
    LangGraph 默认使用 SqliteSaver 存储每个节点执行后的 state 快照。
    通过 thread_id 进行会话隔离，支持在不同请求间恢复状态。
    """
    # --- 意图上下文 ---
    intent_id: str = Field(..., description="全局唯一意图标识")
    # A workflow run is intentionally distinct from a policy scope.  It is
    # part of the checkpoint identity so a new intent/revision never restores
    # transient assurance or diagnostic facts from an earlier run.
    revision: int = Field(default=1, ge=1)
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    user_intent: UserIntent = Field(..., description="原始用户意图")
    current_status: IntentStatus = Field(
        default=IntentStatus.PENDING,
        description="当前意图处理状态"
    )

    # --- 网络感知数据 ---
    network_snapshot: Optional[NetworkSnapshot] = Field(
        default=None,
        description="MCP获取的当前网络拓扑快照"
    )
    context_refresh_attempts: int = Field(
        default=0,
        ge=0,
        le=1,
        description="Bounded availability-evidence refreshes performed during CHECK",
    )

    # --- 策略数据流 ---
    intent_plan: Optional[IntentPlan] = Field(
        default=None,
        description="IntentPlannerAgent生成的JSON意图计划"
    )
    yang_draft: Optional[YANGPolicy] = Field(
        default=None,
        description="当前正在处理的YANGPolicy，由PolicyModelingAgent从IntentPlan转换得到"
    )
    yang_policies: List[YANGPolicy] = Field(
        default_factory=list,
        description="All YANG policies produced from the current intent plan"
    )
    current_policy_index: int = Field(
        default=0,
        ge=0,
        description="Index of the YANG policy currently being processed"
    )
    policy_results: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Per-policy conflict, compile, execution, and verification results"
    )
    conflict_report: Optional[ConflictReport] = Field(
        default=None,
        description="Conflict Checker的冲突检测报告"
    )
    consensus_decision: Optional[ConsensusDecision] = Field(
        default=None,
        description="最新一轮可审计 A2A 共识决策",
    )
    flow_table_plan: Optional[Dict[str, Any]] = Field(
        default=None,
        description="PolicyCompiler生成的P4Runtime WriteRequest序列"
    )
    rule_verification_report: Optional[RuleVerificationReport] = Field(
        default=None,
        description="P4RuleVerifierAgent 的表项级验证报告"
    )
    verified_flow_table_plan: Optional[Dict[str, Any]] = Field(
        default=None,
        description="P4RuleVerifierAgent验证通过后允许下发的FlowTablePlan"
    )
    safety_council_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Safety Assurance Council 在执行前生成的多智能体安全评审报告"
    )
    risk_level: str = Field(
        default="HIGH",
        description="风险等级: LOW | MEDIUM | HIGH | CRITICAL。RiskClassifyNode 设置，"
                    "决定 SafetyCouncil 评审深度。默认 HIGH 以保持向后兼容。"
    )
    execution_result: Optional[Dict[str, Any]] = Field(
        default=None,
        description="ExecuteFlowNode 的流表下发结果"
    )
    pre_execution_telemetry: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Telemetry snapshot captured immediately before flow delivery"
    )

    # --- 验证数据 ---
    telemetry_data: Optional[TelemetryData] = Field(
        default=None,
        description="Status Verifier采集的遥测数据"
    )
    verification_passed: Optional[bool] = Field(
        default=None,
        description="KPI验证是否通过"
    )
    verification_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="详细验证报告"
    )
    # Explicit phase gates.  These values are never inferred from a message
    # string: assurance, diagnosis and healing inspect them directly.
    execution_status: str = "EXECUTION_PENDING"
    # A caller may explicitly request semantic decomposition only.  This is a
    # bounded pre-execution mode: the LLM, IntentPlan and policy modelling run,
    # but compiler, southbound delivery and assurance must remain untouched.
    semantic_only_completed: bool = False
    execution_generation: int = Field(default=0, ge=0)
    execution_effect_signature: Optional[str] = None
    execution_readback_status: str = "EXECUTION_PENDING"
    execution_readback_generation: Optional[int] = None
    execution_mismatch: Dict[str, Any] = Field(default_factory=dict)
    reconciliation_required: bool = False
    reconciliation_attempt: int = Field(default=0, ge=0)
    reconciliation_generation: int = Field(default=0, ge=0)
    max_reconciliation_attempts: int = Field(default=2, ge=1)
    assurance_generation: int = Field(default=0, ge=0)
    assurance_basis_execution_generation: Optional[int] = None
    assurance_id: Optional[str] = None
    assurance_samples: List[Dict[str, Any]] = Field(default_factory=list)
    measurement_status: str = "NOT_STARTED"
    valid_assurance_sample_count: int = Field(default=0, ge=0)
    violation_confirmed: bool = False
    violation_id: Optional[str] = None
    violation_basis_assurance_generation: Optional[int] = None
    root_cause_status: str = "NOT_STARTED"
    root_cause_analysis_id: Optional[str] = None
    root_cause_basis_violation_id: Optional[str] = None
    diagnostic_basis_root_cause_id: Optional[str] = None
    diagnostics_required: bool = False
    healing_ready: bool = False
    terminal_reason: Optional[str] = None
    next_action: Optional[str] = None
    state_audit: List[Dict[str, Any]] = Field(default_factory=list)
    diagnostic_state: Dict[str, Any] = Field(
        default_factory=dict,
        description="Read-only root-cause diagnostic rounds and evidence identity",
    )

    # --- 执行轨迹 (审计链) ---
    execution_log: List[ExecutionLogEntry] = Field(
        default_factory=list,
        description="完整执行轨迹，构成Sense→Think→Act→Verify审计链"
    )

    # --- ReAct回溯状态 ---
    react_state: ReactState = Field(
        default_factory=ReactState,
        description="ReAct回溯状态跟踪"
    )

    # --- 错误处理 ---
    error_message: Optional[str] = Field(
        default=None,
        description="最近一次错误信息"
    )
    needs_clarification: bool = Field(
        default=False,
        description="是否需要向用户发起澄清对话"
    )
    clarification_question: Optional[str] = Field(
        default=None,
        description="需要澄清的问题 (needs_clarification=True时)"
    )
    clarification_questions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="结构化 ClarificationQuestion 列表",
    )
    approval_decision: Optional[str] = Field(
        default=None,
        description="人工审批决策: approve / reject / abort"
    )

    # --- 时间戳 ---
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="状态创建时间"
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="状态最后更新时间"
    )

    def add_log_entry(self, entry: ExecutionLogEntry) -> None:
        """追加执行日志条目

        将一条阶段执行记录追加到审计链中，
        并更新全局状态的时间戳。

        Args:
            entry: 执行日志条目实例
        """
        self.execution_log.append(entry)
        self.updated_at = datetime.utcnow()

    def get_thread_id(self) -> str:
        """获取 LangGraph 线程 ID

        格式为 "{intent_id}::{session_id}"，
        用于 LangGraph checkpoint 的会话隔离。
        同一会话的多次调用共享同一个线程，
        支持暂停/恢复和状态回溯。

        Returns:
            线程标识字符串
        """
        return f"{self.intent_id}::r{self.revision}::run-{self.run_id}"

    class Config:
        arbitrary_types_allowed = True

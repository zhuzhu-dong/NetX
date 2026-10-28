"""
models/intent_plan.py
=====================

意图计划数据模型 —— 自然语言意图与 YANG 策略之间的固定中间 Schema。

# 系统角色
IntentPlan 是 IntentPlannerAgent 的输出、PolicyModelingAgent 的输入，
是 IDN-System 中连接"语义理解"和"策略建模"两个阶段的关键契约。
其设计目的类似于编译原理中的 IR（中间表示）：
- 上游（LLM 路径）：从自然语言中提取语义结构
- 下游（确定性规则路径）：补充 IP、端口、带宽等可验证字段 → 最终生成 YANGPolicy

# 主要依赖
- pydantic.BaseModel
- uuid（用于生成唯一 SubIntent ID）

# 与其他模块交互
- agents/intent_planner/agent.py：生成 IntentPlan
- agents/policy_modeling.py：消费 IntentPlan，转换为 YANGPolicy
- api/routes.py：将 IntentPlan 序列化为 JSON 返回前端

# 设计意图
- SubIntent 建模为独立的原子需求单元，支持一个意图拆分为多个子意图
- confidence_score 记录 LLM 解析的可信度，供下游决策使用
- parse_warnings / clarification_questions 提供人工介入的入口
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from models.semantic_resolution import ClarificationQuestion


class SubIntent(BaseModel):
    """
    子意图：从用户原始意图中提取的一个原子执行需求

    每个 SubIntent 对应一条可以独立执行的策略规则。
    例如用户说"限制 P2P 流量并保障视频 QoS"，
    会被拆分为两个 SubIntent：
    1. DROP P2P 流量
    2. SET_PRIORITY 视频流量

    sequence 字段控制子意图的执行顺序（如先限速再保障）。
    priority 不同于 PolicyMetadata.priority（YANG 策略优先级），
    这里是意图级优先级——如用户说"最重要的是视频保障"，video=1, p2p=2。
    """
    sub_intent_id: str = Field(default_factory=lambda: f"SUB-{uuid.uuid4().hex[:8].upper()}")
    sequence: int = Field(default=1, ge=1, description="子意图执行序号")
    objective: str = Field(default="", description="Business goal of this sub-intent (业务目标描述)")
    intent_type: str = Field(default="network_policy", description="DROP / SET_PRIORITY / RATE_LIMIT / FORWARD")
    match: Dict[str, Any] = Field(default_factory=dict, description="Traffic selector (流量五元组等匹配条件)")
    action: Dict[str, Any] = Field(default_factory=dict, description="Abstract action and parameters (抽象动作定义)")
    constraints: Dict[str, Any] = Field(default_factory=dict, description="KPI or QoS constraints (带宽/时延等约束)")
    priority: Optional[int] = Field(default=None, description="Policy priority when explicitly known (策略优先级)")
    service_id: Optional[str] = Field(default=None, description="Matched ServiceProfile identifier")
    service_profile_snapshot: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Immutable ServiceProfile snapshot used to compile and validate this sub-intent",
    )
    validation_profile_id: Optional[str] = Field(
        default=None,
        description="ServiceProfile used by the active validation probe",
    )


class IntentPlan(BaseModel):
    """
    意图计划：IntentPlannerAgent 产出的结构化 JSON 计划

    这是整个 IDN-System 流水线中第一个结构化产物，
    桥接了非结构化的自然语言与结构化的 YANG 策略。

    # 关键属性
    - raw_intent：保留原始用户输入，用于审计追溯
    - confidence_score：LLM 的语义理解置信度（0~1），低分可能触发人工确认
    - parse_warnings：解析过程中的非致命警告
    - clarification_questions：当意图模糊时生成的澄清问题列表
    - critic_report：内置 Critic 的校验报告（检测遗漏、矛盾等）
    """
    plan_id: Optional[str] = Field(default_factory=lambda: f"IPLAN-{uuid.uuid4().hex[:8].upper()}")
    artifact_type: str = Field(default="INTENT_PLAN")
    interpretation_id: str = Field(default_factory=lambda: f"INTERP-{uuid.uuid4().hex[:8].upper()}")
    raw_intent: str = Field(..., description="原始用户意图文本")
    sub_intents: List[SubIntent] = Field(default_factory=list, description="子意图列表")
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0, description="LLM 解析置信度 (0~1)")
    parse_warnings: List[str] = Field(default_factory=list, description="解析过程中的警告信息")
    clarification_questions: List[str] = Field(default_factory=list, description="需要用户澄清的问题列表")
    clarification_details: List[ClarificationQuestion] = Field(
        default_factory=list,
        description="Structured clarification questions for business-facing clients",
    )
    field_provenance: Dict[str, str] = Field(
        default_factory=dict,
        description="Authority used for each resolved semantic field",
    )
    semantic_resolution: Dict[str, Any] = Field(default_factory=dict)
    critic_report: Dict[str, Any] = Field(default_factory=dict, description="内置 Critic 的校验报告")

    def to_envelope(self) -> Dict[str, Any]:
        """
        返回 A2A/MCP 通信中使用的顶层 JSON 结构

        【难点解释】A2A 协议要求负载以 {"intent_plan": {...}} 的形式包装，
        而不是直接传递 IntentPlan。这个包装（envelope）使下游 Agent
        可以通过统一的 key 来访问 IntentPlan，无需了解具体模型命名。

        Returns:
            {"intent_plan": <IntentPlan 的 JSON 序列化>}
        """
        return {"intent_plan": self.model_dump(mode="json")}


class IntentPlanEnvelope(BaseModel):
    """
    IntentPlan 的 A2A 通信包装器

    将 IntentPlan 封装为 {"intent_plan": {...}} 的标准格式，
    与 A2A/MCP 协议的消息格式保持一致。
    使下游 Agent 可以通过 intent_plan_envelope.intent_plan 直接访问。
    """
    intent_plan: IntentPlan = Field(..., description="封装的意图计划")

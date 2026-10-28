"""
models/intent.py
================

用户意图数据模型 —— IDN-System 的入口数据结构。

# 系统角色
定义用户通过 API 提交的自然语言网络意图的数据格式。
这是整个 STAV 流水线的起点：用户意图 → IntentPlan → YANGPolicy → FlowTablePlan → ONOS 下发。

# 主要类型
- IntentStatus：意图处理状态的完整生命周期枚举（PENDING → SENSING → ... → SUCCESS/FAILED）
- IntentPriority：意图优先级（LOW/MEDIUM/HIGH/CRITICAL）
- UserIntent：用户意图数据模型（文本 + 优先级 + 上下文 + 会话信息）

# 主要依赖
- pydantic.BaseModel / Field
- uuid / datetime

# 与其他模块交互
- api/routes.py：接收前端 POST 请求，反序列化为 UserIntent
- api/worker.py：从 UserIntent 构造 GlobalAgentState 并启动工作流
- orchestrator/graph.py：LangGraph StateGraph 以此为输入启动闭环

# 设计意图
- session_id 用于 LangGraph 线程隔离，同一会话多次请求共享 checkpointer
- context 字段支持传递附加上下文（历史对话、偏好设置等）
- deadline 支持设置意图执行截止时间
"""
from __future__ import annotations
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class IntentStatus(str, Enum):
    """意图处理状态枚举"""
    PENDING = "pending"       # 等待处理
    SENSING = "sensing"       # 感知阶段
    THINKING = "thinking"     # 推理阶段
    ACTING = "acting"         # 执行阶段
    VERIFYING = "verifying"   # 验证阶段
    SUCCESS = "success"       # 成功完成
    UNSATISFIABLE = "unsatisfiable"  # 无合法有效动作可满足目标
    FAILED = "failed"         # 执行失败
    DEGRADED = "degraded"     # 已执行，但保证/诊断尚未形成成功结论
    ROLLBACK = "rollback"     # 回滚中
    PENDING_APPROVAL = "pending_approval"  # 等待人工审批
    PENDING_CLARIFICATION = "pending_clarification"  # 等待用户澄清
    WAITING_FOR_INFRASTRUCTURE = "waiting_for_infrastructure"  # 等待控制器/设备证据恢复
    CANCELLING = "cancelling"  # 已请求取消，等待安全边界确认
    CANCELLED = "cancelled"  # 取消于任何南向提交之前
    CANCELLED_AFTER_COMMIT = "cancelled_after_commit"  # 停止后续执行，已提交策略保留


class IntentPriority(str, Enum):
    """意图优先级枚举"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class UserIntent(BaseModel):
    """
    用户意图数据模型
    
    用户通过自然语言提交网络意图，系统将其解析为结构化策略并执行。
    """
    intent_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="全局唯一意图标识 (UUID v4)"
    )
    text: str = Field(
        ...,
        description="用户自然语言意图描述",
        min_length=1,
        max_length=2000,
        examples=["保障科研大楼到数据中心的视频业务带宽不低于10Gbps"]
    )
    priority: IntentPriority = Field(
        default=IntentPriority.MEDIUM,
        description="意图优先级"
    )
    user_id: str = Field(
        default="anonymous",
        description="提交者用户ID"
    )
    session_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="会话ID，用于LangGraph线程隔离"
    )
    context: Optional[dict] = Field(
        default=None,
        description="附加上下文信息（如历史对话、偏好设置等）"
    )
    deadline: Optional[datetime] = Field(
        default=None,
        description="意图执行截止时间"
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="提交时间戳"
    )
    status: IntentStatus = Field(
        default=IntentStatus.PENDING,
        description="当前处理状态"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "text": "保障科研大楼到数据中心的视频业务带宽不低于10Gbps，时延不超过5ms",
                "priority": "high",
                "user_id": "admin",
                "context": {
                    "network_instance": "research-to-dc",
                    "service_type": "video"
                }
            }
        }

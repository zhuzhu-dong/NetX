"""
models/yang_policy.py
=====================

YANG 策略数据模型 —— 基于 RFC 8341 简化定制的 idn-policy 模块。

# 系统角色
本文件定义了 IDN-System 中网络策略的标准化数据结构，
是 IntentPlannerAgent（意图解析）与 PolicyCompiler（策略编译）之间的契约语言。
所有策略以 YANG 风格的层次化结构组织：NetworkPolicy → MatchCriteria + ActionRules + PolicyMetadata。

# 模型层次
- Protocol / ActionType：传输协议和动作类型的枚举定义
- MatchCriteria：流量五元组匹配条件（源/目的IP、端口、协议、VLAN）
- ForwardParams / QoSParams：转发参数和 QoS 参数
- ActionRules：动作定义（转发/镜像/限速/丢弃/优先级）
- PolicyMetadata：策略元数据（优先级、有效期、创建者）
- NetworkPolicy：网络策略主体（match + action + metadata）
- YANGPolicy：完整的 YANG 策略实例（策略 + 原始意图 + 置信度）

# 主要依赖
- pydantic.BaseModel / Field / field_validator
- uuid（生成策略ID）

# 与其他模块交互
- agents/intent_planner/agent.py：将自然语言转换为 YANGPolicy
- agents/policy_modeling.py：将 IntentPlan 中的 SubIntent 转换为 YANGPolicy
- agents/policy_compiler.py：将 YANGPolicy 编译为 FlowTablePlan（P4 流表项）
- agents/policy_conflict_checker.py：基于 YANGPolicy 做策略冲突检测

# 设计意图
- 五元组匹配模型覆盖网络策略的核心匹配维度
- ForwardParams 和 QoSParams 分离，不同 action_type 使用不同参数子集
- confidence_score 跟踪 LLM 翻译的可信度，支持下游做不确定性处理
"""
from __future__ import annotations
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class Protocol(str, Enum):
    """传输层协议枚举"""
    TCP = "TCP"
    UDP = "UDP"
    ICMP = "ICMP"
    ANY = "ANY"


class ActionType(str, Enum):
    """执行动作类型枚举"""
    FORWARD = "FORWARD"           # 正常转发
    MIRROR = "MIRROR"             # 流量镜像
    RATE_LIMIT = "RATE_LIMIT"     # 速率限制
    DROP = "DROP"                 # 丢弃
    SET_PRIORITY = "SET_PRIORITY" # 设置优先级
    ACL_BLOCK = "ACL_BLOCK"       # ACL 访问控制阻断


class QoSMode(str, Enum):
    """Bandwidth semantics, independent from the traffic selector identity."""

    MIN_BANDWIDTH = "MIN_BANDWIDTH"
    RATE_LIMIT = "RATE_LIMIT"


class MatchCriteria(BaseModel):
    """
    流量匹配条件（五元组 + 端口/VLAN）
    对应YANG module中的 container match
    """
    source_ip: Optional[str] = Field(
        default=None,
        description="源IP地址段 (CIDR格式, e.g. 192.168.10.0/24)"
    )
    destination_ip: Optional[str] = Field(
        default=None,
        description="目的IP地址段 (CIDR格式, e.g. 10.0.0.0/8)"
    )
    source_port: Optional[int] = Field(
        default=None,
        ge=1, le=65535,
        description="源端口号 (可选)"
    )
    destination_port: Optional[int] = Field(
        default=None,
        ge=1, le=65535,
        description="目的端口号 (可选)"
    )
    protocol: Protocol = Field(
        default=Protocol.ANY,
        description="传输层协议"
    )
    vlan_id: Optional[int] = Field(
        default=None,
        ge=1, le=4094,
        description="VLAN标识 (可选)"
    )


class ForwardParams(BaseModel):
    """转发参数"""
    egress_port: Optional[str] = Field(default=None, description="出端口标识")
    next_hop: Optional[str] = Field(default=None, description="下一跳IPv4地址")


class QoSParams(BaseModel):
    """QoS参数"""
    qos_mode: Optional[QoSMode] = Field(
        default=None,
        description="QoS语义：最低带宽保障或最大速率限制",
    )
    dscp_value: Optional[int] = Field(
        default=None,
        ge=0, le=63,
        description="DSCP标记值 (0-63, EF=46)"
    )
    bandwidth: Optional[int] = Field(
        default=None,
        description="带宽预留或限制值 (Mbps)"
    )
    burst_size: Optional[int] = Field(
        default=None,
        description="突发流量容限 (KB)"
    )


class ActionRules(BaseModel):
    """
    执行动作定义
    对应YANG module中的 container action
    """
    action_type: ActionType = Field(..., description="动作类型")
    forward_params: Optional[ForwardParams] = Field(
        default=None,
        description="转发参数 (action_type=FORWARD时生效)"
    )
    qos_params: Optional[QoSParams] = Field(
        default=None,
        description="QoS参数 (action_type=SET_PRIORITY或RATE_LIMIT时生效)"
    )


class PolicyMetadata(BaseModel):
    """
    策略元数据
    对应YANG module中的 container metadata
    """
    priority: int = Field(
        default=1000,
        ge=1, le=65535,
        description="策略优先级 (数值越小优先级越高)"
    )
    valid_from: Optional[datetime] = Field(
        default_factory=datetime.utcnow,
        description="策略生效时间"
    )
    valid_until: Optional[datetime] = Field(
        default=None,
        description="策略失效时间 (可选，不填则永久有效)"
    )
    created_by: str = Field(
        default="IDN-System",
        description="创建者标识"
    )
    description: Optional[str] = Field(
        default=None,
        description="策略描述 (自然语言，便于审计)"
    )


class NetworkPolicy(BaseModel):
    """
    网络策略主体
    对应YANG module idn-policy中的 container network-policy
    """
    policy_id: str = Field(
        default_factory=lambda: f"POL-{datetime.utcnow().strftime('%Y%m%d')}-{str(uuid.uuid4())[:8].upper()}",
        description="策略唯一标识"
    )
    policy_name: str = Field(
        ...,
        description="策略名称 (业务语义)"
    )
    match: MatchCriteria = Field(..., description="流量匹配条件")
    action: ActionRules = Field(..., description="执行动作")
    metadata: PolicyMetadata = Field(
        default_factory=PolicyMetadata,
        description="策略元数据"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "policy_id": "POL-20260508-001",
                "policy_name": "research-to-dc-video-qos",
                "match": {
                    "source_ip": "192.168.10.0/24",
                    "destination_ip": "10.0.0.0/8",
                    "protocol": "UDP",
                    "destination_port": 5004
                },
                "action": {
                    "action_type": "SET_PRIORITY",
                    "qos_params": {
                        "dscp_value": 46,
                        "bandwidth": 10000,
                        "burst_size": 1024
                    }
                },
                "metadata": {
                    "priority": 100,
                    "created_by": "IDN-System",
                    "description": "保障科研大楼到数据中心的视频业务QoS"
                }
            }
        }


class YANGPolicy(BaseModel):
    """
    完整的YANG策略实例
    作为Agent间传递策略意图的标准化数据结构
    """
    network_policy: NetworkPolicy = Field(
        ...,
        description="网络策略主体"
    )
    source_plan_id: Optional[str] = Field(
        default=None,
        description="Formal plan that authorized policy compilation",
    )
    source_plan_status: Optional[str] = Field(
        default=None,
        description="Must be PLAN_READY before PolicyCompiler is invoked",
    )
    raw_intent: Optional[str] = Field(
        default=None,
        description="生成此策略的原始用户意图文本 (用于审计)"
    )
    confidence_score: float = Field(
        default=0.0,
        ge=0.0, le=1.0,
        description="LLM解析置信度 (0-1)"
    )
    parse_warnings: List[str] = Field(
        default_factory=list,
        description="解析过程中的警告信息"
    )
    service_id: Optional[str] = Field(
        default=None,
        description="ServiceProfile identifier bound to this policy selector",
    )
    service_profile_snapshot: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Immutable profile snapshot used for compilation and validation",
    )
    validation_profile_id: Optional[str] = Field(
        default=None,
        description="Profile identifier required by the Verify stage",
    )

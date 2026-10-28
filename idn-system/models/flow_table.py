"""
models/flow_table.py
====================

P4Runtime 流表计划数据模型定义文件。

# 系统角色
本文件定义了从策略编译到流表下发的完整数据模型体系，涵盖：
- 单条流表项（TableEntry）：对应 P4Runtime 的 TableEntry proto message
- 动作配置组（ActionProfileGroup/Member）：ECMP/WCMP 多路径选择
- 写入请求（WriteRequest）：原子性的批量下发单元
- 流表计划（FlowTablePlan）：完整的、依赖有序的流表下发计划

# 主要依赖
- pydantic.BaseModel：所有模型的基类
- 无内部业务模块依赖（最底层模型）

# 与其他模块交互
- agents/policy_compiler.py：生成 FlowTablePlan 实例
- agents/p4_rule_verifier.py：对 FlowTablePlan 中的 TableEntry 逐项验证
- mcp_servers/flow_adapter.py：将 WriteRequest 转换为 ONOS FlowRule JSON
- infra/flow_execution.py：通过 ONOS REST API 逐设备下发 WriteRequest

# P4Runtime 知识背景
- P4Runtime 定义了三层写入实体：Member → Group → TableEntry
- TableEntry 的 action 字段可引用 ActionProfileGroup（通过 group_id），
  实现基于 hash 的 ECMP 多路径选择
- WriteRequest 支持原子性模式：ROLLBACK_ON_ERROR（任何一条失败则全部回滚）
  或 CONTINUE_ON_ERROR（部分成功）

# 设计意图
- 提供与 P4Runtime proto 定义一致的 Python 数据模型
- 支持 Action Profile 的 member → group → table_entry 分层写入顺序
- capacity_check_passed / capacity_warnings 支持在编译阶段提前发现容量问题
- ai_degraded / degradation_reason 记录 AI 降级情况，便于审计
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AtomicityMode(str, Enum):
    """
    流表下发原子性模式枚举

    控制 WriteRequest 中多条表项的下发策略：
    - CONTINUE_ON_ERROR：容错模式，某条失败不影响后续写入
    - ROLLBACK_ON_ERROR：严格模式，任何一条失败则整体回滚
    """
    CONTINUE_ON_ERROR = "CONTINUE_ON_ERROR"
    ROLLBACK_ON_ERROR = "ROLLBACK_ON_ERROR"


class MatchField(BaseModel):
    """
    P4 表项匹配字段

    对应 P4Runtime TableEntry 中的 match field，描述一个匹配条件。
    支持四种 P4 标准匹配类型：

    - exact：精确匹配（如 VLAN ID = 100）
    - lpm：最长前缀匹配（如 IP 前缀 10.0.0.0/8），需填写 prefix_len
    - ternary：三元匹配（值 + 掩码），需填写 mask
    - range：范围匹配（如端口 8000-9000）

    字段名必须与 P4Info 中定义的 match_field name 完全一致。
    """
    field_name: str = Field(..., description="匹配字段名 (对应P4Info中定义)")
    match_type: str = Field(..., description="匹配类型: exact | lpm | ternary | range")
    value: Any = Field(..., description="匹配值")
    mask: Optional[Any] = Field(default=None, description="掩码 (ternary时使用)")
    prefix_len: Optional[int] = Field(default=None, description="前缀长度 (lpm时使用)")


class ActionParam(BaseModel):
    """
    动作参数

    对应 P4Runtime Action 中的 param，传递动作执行所需的参数值。
    例如 set_nhop 动作的 port 参数（出端口号）或 drop_packet 动作（无参数）。

    param_name 必须与 P4Info 中定义的 action param name 完全一致。
    """
    param_name: str = Field(..., description="参数名")
    value: Any = Field(..., description="参数值")


class MeterConfig(BaseModel):
    """
    P4 Direct Meter 配置

    Direct Meter 与流表项一一绑定，用于流量计量和速率控制。
    典型应用场景：
    - 限速（RATE_LIMIT）：通过 CIR/CBS 限制流量速率
    - 带宽保障：通过 PIR/PBS 设置峰值速率
    - QoS 队列选择：根据 metering 结果选择不同出队队列

    cir/cburst 为必填（保证基础速率），pir/pburst 为可选的峰值参数。
    """
    cir: int = Field(..., ge=1, description="Committed information rate (保证速率)")
    cburst: int = Field(..., ge=1, description="Committed burst size (保证突发)")
    pir: Optional[int] = Field(default=None, ge=1, description="Peak information rate (峰值速率)")
    pburst: Optional[int] = Field(default=None, ge=1, description="Peak burst size (峰值突发)")


class TableEntry(BaseModel):
    """
    P4 流表项

    对应 P4Runtime 中的 TableEntry proto message，
    是 P4 数据平面最基本的转发控制单元。

    一条完整的 TableEntry 包含：
    - 目标表名（定位到 P4 程序中的具体 match-action 表）
    - 匹配字段列表（定义哪些包匹配此规则）
    - 动作名 + 动作参数（定义匹配后的处理方式）
    - 优先级（多条规则匹配时的选择顺序）
    - 可选的 Meter / Counter / Queue / Clone 配置

    idle_timeout_ns 用于自动老化：非 0 值表示经过指定纳秒无命中后自动删除。
    metadata 字段用于控制器与数据平面之间的带外通信。

    【难点解释】metadata 类型为 Optional[bytes]，这是因为 P4Runtime 规范
    中 @controller_header 元数据必须是 bit<32> 对齐的字节序列。
    使用 16-bit 宽度会导致静默帧损坏和链路发现失败（已验证）。
    """
    table_name: str = Field(..., description="目标表名 (e.g. MyIngress.ipv4_routing)")
    match_fields: List[MatchField] = Field(..., description="匹配字段列表")
    action_name: str = Field(..., description="动作名 (e.g. MyIngress.set_nhop)")
    action_params: List[ActionParam] = Field(default_factory=list, description="动作参数列表")
    priority: int = Field(default=1, ge=1, description="表项优先级")
    meter_id: Optional[int] = Field(default=None, description="Optional P4 meter id used by this entry")
    meter_config: Optional[MeterConfig] = Field(default=None, description="Optional direct meter config for this entry")
    counter_id: Optional[int] = Field(default=None, description="Optional P4 counter id used by this entry")
    queue_id: Optional[int] = Field(default=None, description="Optional egress queue id used by this entry")
    egress_port: Optional[Any] = Field(default=None, description="Optional egress port selected by this entry")
    clone_session_id: Optional[int] = Field(default=None, description="Optional clone/mirror session id used by this entry")
    idle_timeout_ns: int = Field(default=0, description="空闲超时 (纳秒), 0=永久")
    metadata: Optional[bytes] = Field(default=None, description="Controller元数据")

    class Config:
        json_schema_extra = {
            "example": {
                "table_name": "MyIngress.ipv4_routing",
                "match_fields": [
                    {"field_name": "hdr.ipv4.dstAddr", "match_type": "lpm", "value": "10.0.0.0", "prefix_len": 8}
                ],
                "action_name": "MyIngress.set_nhop",
                "action_params": [
                    {"param_name": "port", "value": 2}
                ],
                "priority": 100
            }
        }


class ActionProfileMember(BaseModel):
    """
    Action Profile Member — 对应 P4Runtime ActionProfileMember 实体

    定义 Action Profile Group 中的一个可选动作实例（动作 + 参数 + 权重）。
    在 ECMP 场景中，每个下一跳对应一个 Member。

    Action Selector 根据数据包五元组的 hash 值从 Group 的多个 Members 中
    选择一个执行，weight 越高被选中的概率越大。

    weight=0 表示备用成员，仅在主成员不可达时使用。
    """
    member_id: int = Field(..., ge=0, description="Member ID (P4Runtime member_id)")
    action_name: str = Field(..., description="动作名 (e.g. MyIngress.set_nhop)")
    action_params: List[ActionParam] = Field(
        default_factory=list, description="动作参数列表"
    )
    weight: int = Field(default=1, ge=0, description="选择权重 (ECMP 权重，0=备用)")

    class Config:
        json_schema_extra = {
            "example": {
                "member_id": 1,
                "action_name": "MyIngress.set_nhop",
                "action_params": [{"param_name": "port", "value": 1}],
                "weight": 1,
            }
        }


class ActionProfileGroup(BaseModel):
    """
    Action Profile Group — 对应 P4Runtime ActionProfileGroup 实体

    将多个 Member 组合成一个逻辑组，供 Action Selector 进行 hash 选择。
    典型应用是 ECMP（等成本多路径）：一条 LPM 表项引用一个 group_id，
    数据面实时根据五元组 hash 选择一个 member（即一个下一跳）。

    【难点解释】P4Runtime 的 Action Profile 写入顺序必须严格遵循
    member → group → table_entry。这是因为 group 依赖 member 的存在，
    table_entry 又依赖 group 的存在。不按此顺序会导致 gRPC 错误。
    """
    group_id: int = Field(..., ge=0, description="Group ID")
    profile_name: str = Field(
        ..., description="Action Profile 名称 (e.g. ipv4_routing_ecmp)"
    )
    members: List[ActionProfileMember] = Field(
        ..., min_length=1, description="组成员列表"
    )
    max_size: int = Field(default=16, description="Group 最大 member 数")

    class Config:
        json_schema_extra = {
            "example": {
                "group_id": 100,
                "profile_name": "ipv4_routing_ecmp",
                "members": [
                    {
                        "member_id": 1,
                        "action_name": "MyIngress.set_nhop",
                        "action_params": [{"param_name": "port", "value": 1}],
                        "weight": 1,
                    },
                    {
                        "member_id": 2,
                        "action_name": "MyIngress.set_nhop",
                        "action_params": [{"param_name": "port", "value": 2}],
                        "weight": 1,
                    },
                ],
                "max_size": 16,
            }
        }


class ActionProfileWriteRequest(BaseModel):
    """
    Action Profile 批量写入请求

    包含 member → group → table_entry 三层实体，按 P4Runtime 依赖顺序排列。

    下发时严格按顺序执行：
    1. 先写所有 Member（原子动作定义）
    2. 再写所有 Group（动作组合定义）
    3. 最后写引用 Group 的 TableEntry（LPM → group 映射）

    如果任何一步失败，根据 atomicity 配置决定是否回滚。
    """
    device_id: str = Field(..., description="目标交换机设备ID")
    profile_name: str = Field(
        ..., description="Action Profile 名称 (e.g. ipv4_routing_ecmp)"
    )
    members: List[ActionProfileMember] = Field(
        default_factory=list, description="待下发的 member 列表"
    )
    groups: List[ActionProfileGroup] = Field(
        default_factory=list, description="待下发的 group 列表"
    )
    table_entries: List[TableEntry] = Field(
        default_factory=list,
        description="引用 group_id 的流表项 (e.g. LPM → group)",
    )
    atomicity: AtomicityMode = Field(default=AtomicityMode.ROLLBACK_ON_ERROR)


class WriteRequest(BaseModel):
    """
    P4Runtime WriteRequest

    对应一次原子性的流表下发批次。一个 WriteRequest 针对一台设备，
    包含多条 TableEntry 的 INSERT/UPDATE 操作。

    election_id 用于 P4Runtime 的 master election 机制：
    - election_id_high：epoch 编号
    - election_id_low：controller 编号
    默认 (0, 1) 表示单控制器场景。
    """
    device_id: str = Field(..., description="目标交换机设备ID")
    table_entries: List[TableEntry] = Field(..., description="待下发的流表项列表")
    atomicity: AtomicityMode = Field(
        default=AtomicityMode.ROLLBACK_ON_ERROR,
        description="原子性控制模式"
    )
    election_id_high: int = Field(default=0, description="选举ID高位")
    election_id_low: int = Field(default=1, description="选举ID低位")


class DependencyEdge(BaseModel):
    """
    流表项间的依赖关系边

    用于拓扑排序，确保表项按下发依赖顺序执行。
    例如：先创建 ActionProfileGroup，再创建引用该 Group 的 LPM 表项。

    from_entry_idx / to_entry_idx 均指 FlowTablePlan.write_requests 中的索引。
    """
    from_entry_idx: int = Field(..., description="依赖源表项索引")
    to_entry_idx: int = Field(..., description="依赖目标表项索引")
    reason: str = Field(..., description="依赖原因说明")


class FlowTablePlan(BaseModel):
    """
    完整的流表下发计划

    由 PolicyCompiler 生成，包含依赖有序的 WriteRequest 序列。
    这是从 YANG 策略到数据平面配置的最终产物。

    # 生命周期
    1. PolicyCompiler 生成 FlowTablePlan（含 WriteRequest 序列）
    2. P4RuleVerifier 逐项验证表项的正确性
    3. ExecuteFlowNode 按设备顺序逐一下发 WriteRequest
    4. 下发前执行 capacity_check 防止 TCAM/SRAM 溢出

    # 回滚机制
    当执行失败时，rollback_requests 包含逆向的操作序列：
    - 对已下发的 INSERT 生成 DELETE
    - 对已修改的 MODIFY 恢复为原始值
    """
    plan_id: str = Field(..., description="计划唯一标识")
    intent_id: str = Field(..., description="关联的意图ID")
    policy_operation: str = Field(
        default="CREATE",
        description="CREATE/MODIFY_QOS_RESOURCE/VERIFY_EXISTING",
    )
    traffic_scope_key: Optional[str] = Field(default=None, description="稳定流量范围键")
    existing_policy_id: Optional[str] = Field(default=None, description="被复用或更新的策略ID")
    policy_change: Dict[str, Any] = Field(default_factory=dict, description="旧值、新值及变更方向")

    # Action Profile 写入 (member → group → table_entry，先于 WriteRequest)
    action_profile_requests: List[ActionProfileWriteRequest] = Field(
        default_factory=list,
        description="Action Profile 写入序列 (先于 WriteRequest 下发)",
    )

    # 按设备组织的WriteRequest序列
    write_requests: List[WriteRequest] = Field(
        default_factory=list,
        description="按拓扑有序排列的WriteRequest序列",
    )

    # 依赖图
    dependency_graph: List[DependencyEdge] = Field(
        default_factory=list,
        description="表项间依赖关系图"
    )

    # 容量预检结果
    capacity_check_passed: bool = Field(
        default=True,
        description="设备表项容量预检是否通过"
    )
    capacity_warnings: List[str] = Field(
        default_factory=list,
        description="容量预警信息"
    )

    # AI 降级追踪
    ai_degraded: bool = Field(
        default=False,
        description="True when the LLM path failed and deterministic rules were used instead"
    )
    degradation_reason: str = Field(
        default="",
        description="Human-readable explanation of why the deterministic fallback was used"
    )

    # 回滚信息
    react_adjustments: List[str] = Field(
        default_factory=list,
        description="Adjustments applied during feedback-driven recompilation"
    )
    rollback_requests: Optional[List[WriteRequest]] = Field(
        default=None,
        description="执行失败时的回滚WriteRequest序列"
    )

    @property
    def total_entries(self) -> int:
        """计算流表计划中的总表项数量

        包含 Action Profile 的三类实体（member + group + table_entry）
        以及普通 WriteRequest 中的 table_entries。
        用于前端进度展示和资源容量估算。

        Returns:
            所有待下发实体的总数
        """
        ap = sum(
            len(r.members) + len(r.groups) + len(r.table_entries)
            for r in self.action_profile_requests
        )
        return ap + sum(len(r.table_entries) for r in self.write_requests)

    @property
    def affected_devices(self) -> List[str]:
        """获取本次下发涉及的所有设备 ID 列表

        去重处理：一台设备可能包含多个 WriteRequest。

        Returns:
            去重后的设备 ID 列表
        """
        return list({r.device_id for r in self.write_requests})

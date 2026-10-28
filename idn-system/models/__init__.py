"""
models/__init__.py
==================

核心数据模型包入口。

# 系统角色
作为 models 包的公共 API 层，统一暴露所有核心数据模型。
其他模块通过 `from models import GlobalAgentState, ...` 即可导入
所需的全部模型类，无需深入子模块。

# 导出清单
- 全局状态：GlobalAgentState, ReactState, ExecutionLogEntry
- 意图模型：UserIntent, IntentStatus, IntentPlan, IntentPlanEnvelope, SubIntent
- 策略模型：YANGPolicy, NetworkPolicy, MatchCriteria, ActionRules, PolicyMetadata
- 网络模型：NetworkSnapshot, DeviceInfo, LinkInfo, FlowEntry
- 遥测模型：TelemetryData, KPIMetrics
- 流表模型：FlowTablePlan, MeterConfig, TableEntry, WriteRequest

# 使用规范
- 外部模块应通过此 __init__.py 导入，不应直接 import 子模块
- __all__ 列表明确声明公开 API，IDE 可据此提供自动补全
"""
from .agent_state import GlobalAgentState, ReactState, ExecutionLogEntry
from .intent import UserIntent, IntentStatus
from .intent_plan import IntentPlan, IntentPlanEnvelope, SubIntent
from .yang_policy import YANGPolicy, NetworkPolicy, MatchCriteria, ActionRules, PolicyMetadata
from .network import NetworkSnapshot, DeviceInfo, LinkInfo, FlowEntry
from .telemetry import TelemetryData, KPIMetrics
from .flow_table import FlowTablePlan, MeterConfig, TableEntry, WriteRequest

__all__ = [
    "GlobalAgentState",
    "ReactState",
    "ExecutionLogEntry",
    "UserIntent",
    "IntentStatus",
    "IntentPlan",
    "IntentPlanEnvelope",
    "SubIntent",
    "YANGPolicy",
    "NetworkPolicy",
    "MatchCriteria",
    "ActionRules",
    "PolicyMetadata",
    "NetworkSnapshot",
    "DeviceInfo",
    "LinkInfo",
    "FlowEntry",
    "TelemetryData",
    "KPIMetrics",
    "FlowTablePlan",
    "MeterConfig",
    "TableEntry",
    "WriteRequest",
]

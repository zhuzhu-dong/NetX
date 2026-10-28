"""
orchestrator/nodes/__init__.py
===============================

LangGraph 状态机节点包入口。

# 系统角色
将分散在独立文件中的 11 个节点类统一导出，
保持 `from orchestrator.nodes import SenseNode` 的向后兼容导入语法。

# 导出清单
- BaseNode：所有节点的抽象基类
- SenseNode：网络感知（ONOS 拓扑 + 流表快照采集）
- ThinkNode：意图解析（自然语言 → IntentPlan → YANGPolicy）
- CheckConflictNode：策略冲突检测（三层校验）
- GeneratePolicyNode：策略编译（YANGPolicy → FlowTablePlan）
- VerifyRulePlanNode：P4 表项级验证（P4Info schema + 运行时规则）
- RiskClassifyNode：风险分级（LOW/MEDIUM/HIGH/CRITICAL）
- MiniCouncilNode：轻量级安全评审（MEDIUM 风险）
- SafetyCouncilNode：完整安全评审委员会（HIGH/CRITICAL 风险）
- ExecuteFlowNode：流表下发（ONOS REST 事务性写入）
- VerifyStatusNode：KPI 效果验证（遥测采集 + ReAct 自愈）
- HumanApprovalNode：人工审批挂起/恢复（LangGraph interrupt）

# 设计意图
每个节点类独立一个文件，遵循单一职责原则。
节点之间的依赖关系由 orchestrator/graph.py 中的 StateGraph 定义，
而非通过代码直接调用。
"""
from .base_node import BaseNode
from .sense_node import SenseNode
from .think_node import ThinkNode
from .understanding_node import UnderstandingNode
from .check_conflict_node import CheckConflictNode
from .generate_policy_node import GeneratePolicyNode
from .verify_rule_plan_node import VerifyRulePlanNode
from .risk_classify_node import RiskClassifyNode
from .safety_council_node import SafetyCouncilNode
from .mini_council_node import MiniCouncilNode
from .execute_flow_node import ExecuteFlowNode
from .verify_status_node import VerifyStatusNode
from .collect_diagnostic_evidence_node import CollectDiagnosticEvidenceNode
from .reconcile_execution_node import ReconcileExecutionNode
from .human_approval_node import HumanApprovalNode

__all__ = [
    "BaseNode",
    "SenseNode",
    "ThinkNode",
    "UnderstandingNode",
    "CheckConflictNode",
    "GeneratePolicyNode",
    "VerifyRulePlanNode",
    "RiskClassifyNode",
    "SafetyCouncilNode",
    "MiniCouncilNode",
    "ExecuteFlowNode",
    "VerifyStatusNode",
    "CollectDiagnosticEvidenceNode",
    "ReconcileExecutionNode",
    "HumanApprovalNode",
]

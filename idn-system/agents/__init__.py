"""
agents/__init__.py
==================

领域专家 Agent 包入口。

# 系统角色
统一导出 IDN-System 中所有 10 个领域专家 Agent 类，
每个 Agent 负责 STAV 闭环中的一个专业领域。

# 导出清单
- IntentPlannerAgent：自然语言 → IntentPlan（LLM + 规则双路径）
- NetworkSensingAgent：ONOS 拓扑感知（MCP 采集）
- PolicyModelingAgent：IntentPlan → YANGPolicy（确定性转换）
- PolicyConflictCheckerAgent：策略冲突检测（三层校验）
- PolicyCompilerAgent：YANGPolicy → FlowTablePlan（P4 编译）
- P4RuleVerifierAgent：P4 表项级验证（P4Info schema 校验）
- SafetyCouncilAgent：多智能体安全评审委员会
- StatusVerifierAgent：KPI 效果验证（遥测采集 + ReAct 自愈）
- ExperienceMemoryAgent：经验记忆记录与查询
- RootCauseAnalyzerAgent：故障根因分析
"""
from importlib import import_module


_EXPORTS = {
    "IntentPlannerAgent": ("agents.intent_planner", "IntentPlannerAgent"),
    "NetworkSensingAgent": ("agents.network_sensing", "NetworkSensingAgent"),
    "PolicyModelingAgent": ("agents.policy_modeling", "PolicyModelingAgent"),
    "PolicyConflictCheckerAgent": ("agents.policy_conflict_checker", "PolicyConflictCheckerAgent"),
    "PolicyCompilerAgent": ("agents.policy_compiler", "PolicyCompilerAgent"),
    "P4RuleVerifierAgent": ("agents.p4_rule_verifier", "P4RuleVerifierAgent"),
    "ExperienceMemoryAgent": ("agents.experience_memory", "ExperienceMemoryAgent"),
    "RootCauseAnalyzerAgent": ("agents.root_cause_analyzer", "RootCauseAnalyzerAgent"),
    "SafetyCouncilAgent": ("agents.safety_council", "SafetyCouncilAgent"),
    "StatusVerifierAgent": ("agents.status_verifier", "StatusVerifierAgent"),
}


def __getattr__(name):
    """Load optional P4/LLM dependencies only when that agent is requested."""
    if name not in _EXPORTS:
        raise AttributeError(name)
    module_name, attribute = _EXPORTS[name]
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value

__all__ = [
    "IntentPlannerAgent",
    "NetworkSensingAgent",
    "PolicyModelingAgent",
    "PolicyConflictCheckerAgent",
    "PolicyCompilerAgent",
    "P4RuleVerifierAgent",
    "ExperienceMemoryAgent",
    "RootCauseAnalyzerAgent",
    "SafetyCouncilAgent",
    "StatusVerifierAgent",
]

"""Agent capability cards exposed for A2A-style discovery."""
from __future__ import annotations

from typing import Any


AGENT_CARDS: list[dict[str, Any]] = [
    {
        "agent_id": "intent_planner",
        "role": "自然语言意图规划智能体",
        "capabilities": [
            "llm_semantic_planning",
            "multi_intent_decomposition",
            "rule_based_field_extraction",
            "slot_filling_clarification",
            "llm_rule_schema_cross_check",
        ],
        "input_schema": ["raw_intent", "network_snapshot_summary", "known_sites"],
        "output_schema": "IntentPlanEnvelope",
        "tools": ["site_alias_config", "schema_validator"],
        "permission": "read_only",
        "prompt_contract": "PromptContract.IntentPlanner.v1",
        "cannot_do": ["direct_device_write", "onos_flow_rule", "shell_command", "bypass_schema_validator"],
    },
    {
        "agent_id": "policy_modeling",
        "role": "抽象策略建模智能体",
        "capabilities": ["intent_plan_to_yang_policy", "multi_policy_modeling", "policy_metadata_normalization"],
        "input_schema": ["IntentPlan", "NetworkSnapshot"],
        "output_schema": "YANGPolicy",
        "tools": ["schema_validator"],
        "permission": "read_only",
        "cannot_do": ["compile_flow_table", "execute_flow", "modify_network_state"],
    },
    {
        "agent_id": "policy_conflict_checker",
        "role": "策略冲突检查智能体",
        "capabilities": [
            "action_conflict_detection",
            "range_overlap_detection",
            "qos_conflict_detection",
            "resource_conflict_detection",
            "conflict_visualization_projection",
        ],
        "input_schema": ["YANGPolicy", "NetworkSnapshot", "candidate_path_link_ids"],
        "output_schema": "ConflictReport",
        "tools": ["network_snapshot", "existing_policy_index"],
        "permission": "read_only",
        "cannot_do": ["compile_flow_table", "execute_flow", "override_conflict_block"],
    },
    {
        "agent_id": "policy_compiler",
        "role": "P4/ONOS 策略编译智能体",
        "capabilities": ["yang_to_flow_table_plan", "p4info_contract_loading", "rate_limit_meter_mapping", "react_recompile"],
        "input_schema": ["YANGPolicy", "P4Info", "NetworkSnapshot", "ReactFeedback"],
        "output_schema": "FlowTablePlan",
        "tools": ["p4info_mcp", "topology_context"],
        "permission": "plan_only",
        "cannot_do": ["execute_flow", "skip_rule_verifier", "skip_safety_council"],
    },
    {
        "agent_id": "p4_rule_verifier",
        "role": "P4 表项级验证智能体",
        "capabilities": ["p4info_table_validation", "match_field_validation", "priority_conflict_detection", "meter_counter_resource_check"],
        "input_schema": ["FlowTablePlan", "P4Info", "runtime_rules", "approval_token"],
        "output_schema": "RuleVerificationReport",
        "tools": ["p4info_mcp", "runtime_rule_snapshot"],
        "permission": "read_only",
        "cannot_do": ["execute_flow", "modify_flow_table_plan", "bypass_approval_token"],
    },
    {
        "agent_id": "safety_council",
        "role": "执行前安全协商委员会",
        "capabilities": [
            "invariant_verification",
            "digital_twin_simulation",
            "llm_redteam_critique",
            "rollback_planning",
            "history_risk_review",
            "multi_agent_negotiation",
        ],
        "input_schema": ["RuleVerificationReport", "FlowTablePlan", "NetworkSnapshot", "YANGPolicy"],
        "output_schema": "SafetyCouncilReport",
        "tools": ["experience_memory", "network_snapshot", "rollback_plan_builder"],
        "permission": "approval_gate",
        "cannot_do": ["execute_flow", "ignore_hard_blocker", "bypass_human_approval"],
    },
    {
        "agent_id": "p4_runtime_executor",
        "role": "南向执行智能体",
        "capabilities": ["onos_flow_delivery", "qos_tc_enforcement"],
        "input_schema": ["verified_flow_table_plan"],
        "output_schema": "ExecutionResult",
        "tools": ["onos_rest", "mininet_tc"],
        "permission": "write_with_verified_plan",
        "cannot_do": ["compile_policy", "override_safety_council", "execute_unverified_plan"],
    },
    {
        "agent_id": "status_verifier",
        "role": "KPI 闭环验证智能体",
        "capabilities": ["telemetry_collection", "kpi_validation", "react_triggering", "experience_case_recording"],
        "input_schema": ["YANGPolicy", "execution_result", "IntentConstraints"],
        "output_schema": "VerificationReport",
        "tools": ["telemetry_api", "onos_counter", "mininet_probe", "p4_int"],
        "permission": "read_only",
        "cannot_do": ["modify_policy", "execute_repair_without_safety_review"],
    },
    {
        "agent_id": "root_cause_analyzer",
        "role": "根因因果推理智能体",
        "capabilities": [
            "candidate_cause_scoring",
            "evidence_graph_building",
            "counterfactual_probe_generation",
            "experience_case_retrieval",
            "hybrid_experience_retrieval",
            "guarded_llm_diagnosis",
        ],
        "input_schema": ["TelemetryData", "violations", "YANGPolicy", "ExperienceMemory"],
        "output_schema": "RootCauseReport",
        "tools": ["telemetry_analysis", "experience_memory", "guarded_llm"],
        "permission": "read_only",
        "prompt_contract": "PromptContract.RootCauseAnalyzer.v1",
        "cannot_do": ["execute_flow", "modify_qdisc", "bypass_safety_council", "invent_telemetry"],
    },
    {
        "agent_id": "experience_memory",
        "role": "长期经验记忆智能体",
        "capabilities": [
            "case_recording",
            "similar_case_retrieval",
            "optional_vector_retrieval",
            "hybrid_rerank_scoring",
            "repair_success_rate_summary",
            "lesson_reuse",
        ],
        "input_schema": ["VerificationReport", "YANGPolicy", "RootCauseReport"],
        "output_schema": "ExperienceCase",
        "tools": ["sqlite_experience_cases", "local_hash_embedding"],
        "permission": "memory_read_write",
        "cannot_do": ["execute_flow", "approve_policy", "rewrite_history_without_case_id"],
    },
    {
        "agent_id": "kpi_watcher",
        "role": "后台自治监控智能体",
        "capabilities": ["periodic_kpi_sampling", "incident_generation", "internal_intent_triggering", "a2a_incident_delegation"],
        "input_schema": ["TelemetrySnapshot", "WatcherConfig"],
        "output_schema": "WatcherIncident",
        "tools": ["telemetry_api", "intent_api", "sqlite_watcher_state"],
        "permission": "trigger_intent_only",
        "cannot_do": ["write_flow_rule", "modify_qdisc", "skip_intent_pipeline"],
    },
]


def list_agent_cards() -> list[dict[str, Any]]:
    return [dict(card) for card in AGENT_CARDS]


def get_agent_card(agent_id: str) -> dict[str, Any] | None:
    for card in AGENT_CARDS:
        if card["agent_id"] == agent_id:
            return dict(card)
    return None

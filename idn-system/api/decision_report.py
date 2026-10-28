"""Build user-facing multi-agent evidence artifacts from a finished run."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from api.topology_paths import topology_paths


def _configured_primary_path_id() -> str | None:
    primary = topology_paths.get("primary")
    return primary.path_id if primary else None


def build_decision_artifacts(
    final_state: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return an A2A-style timeline and a compact decision report."""
    state = _jsonable(final_state)
    timeline = _build_a2a_timeline(state)
    report = _build_decision_report(state, timeline)
    return timeline, report


def _build_a2a_timeline(state: dict[str, Any]) -> list[dict[str, Any]]:
    logs = state.get("execution_log") or []
    intent_plan = state.get("intent_plan") or {}
    yang_policies = state.get("yang_policies") or []
    conflict_report = state.get("conflict_report") or {}
    flow_plan = state.get("flow_table_plan") or {}
    rule_report = state.get("rule_verification_report") or {}
    safety_report = state.get("safety_council_report") or {}
    execution_result = state.get("execution_result") or {}
    verification_report = state.get("verification_report") or {}
    network_summary = _network_summary(state.get("network_snapshot"))
    policy_results = state.get("policy_results") or []
    pre_execution = _pre_execution_rejection(state)
    pre_execution_reason = pre_execution.get("final_reason_code") or pre_execution.get("stop_category")
    not_applicable = f"NOT_APPLICABLE: rejected during capacity admission ({pre_execution_reason})."

    entries = [
        _entry(
            index=1,
            message_type="Task_Delegation",
            stage="sense",
            source_agent="orchestrator",
            target_agent="network_sensing",
            log=_stage_log(logs, "sense"),
            input_summary="采集当前网络拓扑、主机、链路与运行时流表。",
            output_summary=(
                f"获得 {network_summary['devices']} 台设备、"
                f"{network_summary['links']} 条链路、{network_summary['hosts']} 台主机。"
            ),
            evidence_refs=[
                "MCP:onOS /topology/current",
                "MCP:onOS /hosts",
                "MCP:onOS /flows",
            ],
        ),
        _entry(
            index=2,
            message_type="Task_Delegation",
            stage="parse",
            source_agent="network_sensing",
            target_agent="intent_planner",
            log=_stage_log(logs, "parse"),
            input_summary=_trim(state.get("user_intent", {}).get("text") or ""),
            output_summary=_intent_plan_summary(intent_plan),
            confidence=intent_plan.get("confidence_score"),
            evidence_refs=_list_values(intent_plan.get("parse_warnings")),
            failure_reason=(
                state.get("clarification_question")
                if state.get("needs_clarification")
                else None
            ),
        ),
        _entry(
            index=3,
            message_type="Task_Delegation",
            stage="parse",
            source_agent="intent_planner",
            target_agent="policy_modeling",
            log=_stage_log(logs, "parse"),
            input_summary=f"IntentPlan {intent_plan.get('plan_id') or '-'}，子意图 {_count(intent_plan.get('sub_intents'))} 个。",
            output_summary=_yang_policy_summary(yang_policies),
            confidence=_min_confidence(yang_policies)
            or intent_plan.get("confidence_score"),
            evidence_refs=["IntentPlan.sub_intents", "YANGPolicy.network_policy"],
        ),
        _entry(
            index=4,
            message_type="Task_Delegation",
            stage="check",
            source_agent="policy_modeling",
            target_agent="policy_conflict_checker",
            log=_stage_log(logs, "check"),
            input_summary="检查策略级动作、范围、QoS、资源与拓扑兼容性。",
            output_summary=conflict_report.get("explanation") or "未返回冲突检测解释。",
            status="failed" if conflict_report.get("blocking") else None,
            evidence_refs=_refs_from_report(
                conflict_report, "conflict_items", "warning_items"
            ),
            failure_reason=conflict_report.get("rejection_reason"),
            retry_suggestion=_first(conflict_report.get("suggestions")),
        ),
        _entry(
            index=5,
            message_type="Task_Delegation",
            stage="generate",
            source_agent="policy_conflict_checker",
            target_agent="policy_compiler",
            log=_stage_log(logs, "generate", "policy_compiler"),
            input_summary="使用审批令牌、YANGPolicy、NetworkSnapshot 与 P4Info 编译流表计划。",
            output_summary=not_applicable if pre_execution else _flow_plan_summary(flow_plan),
            status="skipped" if pre_execution else None,
            evidence_refs=["P4Info tables/actions", "YANGPolicy action mapping"],
            retry_suggestion=_react_adjustment_summary(flow_plan),
        ),
        _entry(
            index=6,
            message_type="Task_Delegation",
            stage="generate",
            source_agent="policy_compiler",
            target_agent="p4_rule_verifier",
            log=_stage_log(logs, "generate", "p4_rule_verifier"),
            input_summary=f"验证 {_flow_entry_count(flow_plan)} 条候选 P4Runtime 表项。",
            output_summary=not_applicable if pre_execution else rule_report.get("explanation") or "未返回表项级验证解释。",
            status="skipped" if pre_execution else ("success" if rule_report.get("is_valid") else "failed"),
            evidence_refs=_refs_from_report(
                rule_report, "violation_items", "warning_items"
            ),
            failure_reason=(
                None
                if rule_report.get("is_valid", True)
                else rule_report.get("explanation")
            ),
            retry_suggestion=_first(rule_report.get("suggestions")),
        ),
        _entry(
            index=7,
            message_type="Task_Delegation",
            stage="execute",
            source_agent="p4_rule_verifier",
            target_agent="safety_council",
            log=_stage_log(logs, "generate", "safety_council"),
            input_summary="基于已验证 FlowTablePlan 执行不变量、影子仿真、红队审查和回滚评审。",
            output_summary=not_applicable if pre_execution else _safety_summary(safety_report),
            status="skipped" if pre_execution else _safety_status(safety_report),
            evidence_refs=_safety_refs(safety_report),
            failure_reason="; ".join(_list_values(safety_report.get("hard_blockers")))
            or None,
            retry_suggestion=(
                "安全评审未通过时进入人工审批或触发 ReAct 重编译。"
                if safety_report and safety_report.get("overall_decision") != "approve"
                else None
            ),
        ),
        _entry(
            index=8,
            message_type="Task_Delegation",
            stage="execute",
            source_agent="safety_council",
            target_agent="p4_runtime_executor",
            log=_stage_log(logs, "execute"),
            input_summary="将已验证的 WriteRequest 转换为 ONOS FlowRule 并下发。",
            output_summary=(
                "NOT_EXECUTED: network configuration was not started after capacity admission rejection."
                if pre_execution else _execution_summary(execution_result)
            ),
            status="skipped" if pre_execution else _status_from_execution(execution_result),
            evidence_refs=["FlowAdapter batch_convert", "FlowDelivery post_flows"],
            failure_reason=_execution_failure(execution_result),
        ),
        _entry(
            index=9,
            message_type="Task_Delegation",
            stage="verify",
            source_agent="p4_runtime_executor",
            target_agent="status_verifier",
            log=_stage_log(logs, "verify"),
            input_summary="采集真实遥测并验证业务 KPI 是否达成。",
            output_summary=(
                "NOT_APPLICABLE: KPI verification was skipped because southbound execution was not started."
                if pre_execution else _verification_summary(verification_report)
            ),
            status="skipped" if pre_execution else ("success" if verification_report.get("passed") else "failed"),
            evidence_refs=_telemetry_refs(verification_report),
            failure_reason="; ".join(
                _list_values(verification_report.get("violations"))
            )
            or None,
            retry_suggestion=verification_report.get("react_suggestion"),
        ),
    ]

    auto_healing = _find_auto_healing(verification_report, policy_results)
    if auto_healing:
        entries.append(
            _entry(
                index=len(entries) + 1,
                message_type="ReAct_Trigger",
                stage="verify",
                source_agent="status_verifier",
                target_agent="auto_healing_controller",
                log=_stage_log(logs, "verify"),
                input_summary=auto_healing.get("reason") or "KPI 未达标或主路径拥塞。",
                output_summary=_healing_summary(auto_healing),
                status="success" if auto_healing.get("status") == "ok" else "failed",
                evidence_refs=_healing_refs(auto_healing),
                retry_suggestion="切换备用路径并限制低优先级流量后重新验证。",
            )
        )
        entries.append(
            _entry(
                index=len(entries) + 1,
                message_type="Task_Response",
                stage="verify",
                source_agent="auto_healing_controller",
                target_agent="status_verifier",
                log=_stage_log(logs, "verify"),
                input_summary="返回自愈动作与复测结果。",
                output_summary=_healing_comparison_summary(auto_healing),
                status="success" if auto_healing.get("status") == "ok" else "failed",
                evidence_refs=[
                    "pre_heal_verification",
                    "post_heal_verification",
                    "healing_comparison",
                ],
            )
        )

    return [entry for entry in entries if entry]


def _build_decision_report(
    state: dict[str, Any],
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    verification_report = state.get("verification_report") or {}
    intent_plan = state.get("intent_plan") or {}
    final_status = state.get("current_status") or "unknown"
    auto_healing = _find_auto_healing(
        verification_report, state.get("policy_results") or []
    )
    intent_understanding = _intent_understanding(state, intent_plan)
    network_observation = _network_observation(state, verification_report, auto_healing)
    policy_conflict = _policy_conflict(state)
    problem_diagnosis = _problem_diagnosis(
        verification_report, auto_healing, network_observation
    )
    candidate_solutions = _candidate_solutions(state, problem_diagnosis, auto_healing)
    agent_votes = _agent_votes(timeline, problem_diagnosis, candidate_solutions)
    final_action = _final_action(state, auto_healing, candidate_solutions)
    verification_result = _verification_result(verification_report, auto_healing, state)
    execution_result = state.get("execution_result") or {}
    pre_execution = _pre_execution_rejection(state)
    execution_plane = _execution_plane(
        execution_result, verification_report, pre_execution=pre_execution,
    )
    fallback_plan = _fallback_plan(state, verification_report)
    safety_assurance = state.get("safety_council_report") or {}
    consensus_decision = state.get("consensus_decision") or {}

    decision_points = []
    for item in timeline:
        if item["message_type"] not in {"Task_Delegation", "ReAct_Trigger"}:
            continue
        decision_points.append(
            {
                "stage": item["stage"],
                "agent_id": item["target_agent"],
                "title": _decision_title(item["target_agent"], item["message_type"]),
                "decision": item["output_summary"],
                "status": item["status"],
                "confidence": item.get("confidence"),
                "evidence": item.get("evidence_refs") or [],
                "failure_reason": item.get("failure_reason"),
                "retry_suggestion": item.get("retry_suggestion"),
            }
        )

    return {
        "report_id": f"ADR-{state.get('intent_id', 'unknown')}",
        "intent_id": state.get("intent_id"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "final_status": final_status,
        "stop_category": pre_execution.get("stop_category"),
        "admission": pre_execution,
        "user_intent": (state.get("user_intent") or {}).get("text"),
        "summary": _final_summary(
            final_status, verification_report, auto_healing, state
        ),
        "network_evidence": _network_summary(state.get("network_snapshot")),
        "intent_understanding": intent_understanding,
        "network_observation": network_observation,
        "policy_conflict": policy_conflict,
        "consensus_decision": consensus_decision,
        "problem_diagnosis": problem_diagnosis,
        "candidate_solutions": candidate_solutions,
        "agent_votes": agent_votes,
        "final_action": final_action,
        "planned_actions": final_action.get("planned_actions") or [],
        "actual_actions": final_action.get("actual_actions") or [],
        "safety_assurance": safety_assurance,
        "verification_result": verification_result,
        "execution_result": execution_result,
        "execution_plane": execution_plane,
        "policy_change": {
            "operation": execution_result.get("operation"),
            "executor_status": execution_result.get("executor_status"),
            "no_change": execution_result.get("no_change"),
            "traffic_scope_key": execution_result.get("traffic_scope_key"),
            **(execution_result.get("policy_change") or {}),
        },
        "fallback_plan": fallback_plan,
        "collaboration": {
            "message_count": len(timeline),
            "agents": sorted(
                {
                    value
                    for entry in timeline
                    for value in (entry.get("source_agent"), entry.get("target_agent"))
                    if value
                }
            ),
            "react_triggered": any(
                entry.get("message_type") == "ReAct_Trigger" for entry in timeline
            ),
        },
        "decision_points": decision_points,
        "kpi": (verification_report.get("kpi") or {}),
        "auto_healing": auto_healing,
        "evidence_sources": _evidence_sources(
            timeline, verification_report, auto_healing
        ),
        "final_conclusion": _final_conclusion(
            final_status, verification_report, auto_healing, state
        ),
    }


def _execution_plane(
    execution_result: dict[str, Any],
    verification_report: dict[str, Any],
    *,
    pre_execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose controller, classifier, scheduler and KPI evidence as one view."""
    if pre_execution:
        reason = pre_execution.get("final_reason_code") or pre_execution.get("stop_category")
        na = "NOT_APPLICABLE"
        return {
            "architecture": "P4_CLASSIFIER + SWITCH_EGRESS_HTB",
            "execution_state": "NOT_EXECUTED",
            "execution_reason": reason,
            "qos_mode": None,
            "traffic_scope_key": pre_execution.get("traffic_scope_key"),
            "classifier": {
                "verification_state": na,
                "reason": "Southbound execution was not started; no P4 classifier was installed.",
                "flow_state": na,
            },
            "p4_counter": {
                "verification_state": na,
                "reason": "No P4 classifier was installed before admission rejection.",
            },
            "scheduler": {
                "verification_state": na,
                "reason": "No switch egress QoS scheduler configuration was applied.",
                "readback_verified": None,
            },
            "path": {"display_state": "NOT_APPLICABLE"},
            "competition_kpi": {
                "verification_state": na,
                "reason": "Intent was rejected before service execution.",
                "traffic_generated": False,
                "passed": False,
            },
            "verification_states": [na, na, na, na],
            "all_evidence_consistent": False,
            "rollback": None,
        }
    qos = execution_result.get("qos_enforcement") or {}
    config = verification_report.get("config_verification") or {}
    classifier = (
        config.get("p4_classifier_after_traffic") or config.get("p4_classifier") or {}
    )
    switch_qos = config.get("switch_egress_qos") or qos
    binding = switch_qos.get("qos_binding") or qos.get("qos_binding") or {}
    readback = switch_qos.get("tc_readback") or qos.get("tc_readback") or {}
    competition = verification_report.get("policy_traffic_probe") or {}
    kpi = verification_report.get("kpi") or {}
    counter = config.get("p4_counter") or competition.get("p4_counter") or {}
    stats = readback.get("stats") or {}
    mode = switch_qos.get("qos_mode") or qos.get("qos_mode")
    flow_state = str(classifier.get("flow_state") or "").upper()
    classifier_state = (
        "P4_CLASSIFIER_VERIFIED" if flow_state == "ADDED" else "P4_CLASSIFIER_NOT_FOUND"
    )
    counter_state = counter.get("verification_state") or (
        "P4_COUNTER_VERIFIED" if counter.get("counter_hit") else "P4_COUNTER_ZERO"
    )
    switch_state = (
        "SWITCH_EGRESS_QOS_VERIFIED"
        if switch_qos.get("config_applied")
        else "SWITCH_EGRESS_QOS_FAILED"
    )
    kpi_state = (
        verification_report.get("verification_state") or "CONFIG_APPLIED_NO_TRAFFIC"
    )
    statuses = [classifier_state, counter_state, switch_state, kpi_state]
    all_consistent = bool(
        classifier_state == "P4_CLASSIFIER_VERIFIED"
        and counter_state == "P4_COUNTER_VERIFIED"
        and switch_state == "SWITCH_EGRESS_QOS_VERIFIED"
        and kpi_state in {"MIN_BANDWIDTH_KPI_VERIFIED", "RATE_LIMIT_KPI_VERIFIED"}
    )
    current_path_id = qos.get("current_path_id") or binding.get("path_id")
    path_changed = bool(qos.get("path_changed"))
    path_change_reason = qos.get("path_change_reason") or (
        "NO_PATH_CHANGE" if current_path_id else "PATH_UNAVAILABLE"
    )
    path_display_state = (
        "路径已变化"
        if path_changed
        else "无路径变化" if current_path_id else "路径不可用"
    )
    return {
        "architecture": "P4_CLASSIFIER + SWITCH_EGRESS_HTB",
        "qos_mode": mode,
        "traffic_scope_key": execution_result.get("traffic_scope_key")
        or qos.get("scope_key"),
        "classifier": {
            "device_id": classifier.get("device_id")
            or binding.get("classifier_device_id"),
            "switch_name": classifier.get("switch_name") or binding.get("switch_name"),
            "flow_id": classifier.get("flow_id"),
            "flow_state": classifier.get("flow_state"),
            "app_id": classifier.get("app_id"),
            "priority": classifier.get("priority"),
            "source_ip": classifier.get("source_ip"),
            "destination_ip": classifier.get("destination_ip"),
            "protocol": classifier.get("protocol"),
            "destination_port": classifier.get("destination_port"),
            "table_name": classifier.get("table_name") or "MyIngress.sla_enforcement",
            "action_name": classifier.get("action_name") or "MyIngress.set_dscp",
            "dscp": classifier.get("dscp") or binding.get("dscp"),
            "classifier_reused": bool(
                classifier.get("classifier_reused")
                or execution_result.get("classifier_reused")
            ),
            "read_at": classifier.get("read_at"),
            "selector": classifier.get("selector") or qos.get("selector"),
            "treatment": classifier.get("treatment"),
            "packets": classifier.get("packets"),
            "bytes": classifier.get("bytes"),
            "counter_hit": bool(classifier.get("counter_hit")),
            "verification_state": classifier_state,
        },
        "p4_counter": {
            "packets_before": counter.get("packets_before"),
            "packets_after": counter.get("packets_after"),
            "packets_delta": counter.get("packets_delta"),
            "bytes_before": counter.get("bytes_before"),
            "bytes_after": counter.get("bytes_after"),
            "bytes_delta": counter.get("bytes_delta"),
            "nonzero": bool(counter.get("nonzero")),
            "counter_hit": bool(counter.get("counter_hit")),
            "read_at": counter.get("read_at"),
            "verification_state": counter_state,
        },
        "scheduler": {
            "device_id": binding.get("device_id"),
            "switch": binding.get("switch_name"),
            "egress_port": binding.get("egress_port"),
            "interface": binding.get("interface_name"),
            "peer_device": binding.get("peer_device"),
            "class_id": readback.get("class_id") or binding.get("class_id"),
            "filter_id": readback.get("filter_id") or binding.get("filter_id"),
            "filter_priority": readback.get("filter_priority")
            or binding.get("filter_id"),
            "rate_mbps": switch_qos.get("rate_mbps") or readback.get("rate_mbps"),
            "ceil_mbps": switch_qos.get("ceil_mbps") or readback.get("ceil_mbps"),
            "link_capacity_mbps": qos.get("link_capacity_mbps")
            or binding.get("link_capacity_mbps"),
            "priority": readback.get("priority"),
            "default_class_id": readback.get("default_class_id"),
            "default_class_present": readback.get("default_class_present"),
            "filter_kind": readback.get("filter_kind"),
            "classification_key": readback.get("classification_key") or {},
            "readback_verified": bool(switch_qos.get("config_applied")),
            "verification_state": switch_state,
            "sent_bytes": stats.get("sent_bytes"),
            "sent_packets": stats.get("sent_packets"),
            "overlimits": stats.get("overlimits"),
            "borrowed": stats.get("borrowed"),
            "backlog_bytes": stats.get("backlog_bytes"),
            "backlog_packets": stats.get("backlog_packets"),
        },
        "path": {
            "configured_primary_path_id": binding.get("configured_primary_path_id")
            or _configured_primary_path_id(),
            "path_id": current_path_id,
            "current_path_id": current_path_id,
            "current_path": binding.get("current_path") or [],
            "alternative_path_ids": binding.get("alternative_path_ids") or [],
            "path_changed": path_changed,
            "previous_path_id": qos.get("previous_path_id"),
            "path_change_reason": path_change_reason,
            "migrated": bool(qos.get("qos_resource_migrated", qos.get("migrated"))),
            "qos_resource_migrated": bool(
                qos.get("qos_resource_migrated", qos.get("migrated"))
            ),
            "migration_sequence": qos.get("migration_sequence"),
            "previous_binding": qos.get("previous_binding"),
            "display_state": path_display_state,
        },
        "competition_kpi": {
            "traffic_generated": bool(competition.get("traffic_generated")),
            "test_type": competition.get("test_type"),
            "test_profile": competition.get("test_profile"),
            "background_started": bool(competition.get("background_started")),
            "congestion_created": bool(competition.get("congestion_created")),
            "target_offered_mbps": competition.get("target_offered_mbps"),
            "target_throughput_mbps": competition.get("throughput_mbps")
            or kpi.get("throughput_mbps"),
            "target_loss_percent": (competition.get("target") or {}).get(
                "loss_percent"
            ),
            "target_jitter_ms": (competition.get("target") or {}).get("jitter_ms"),
            "background_offered_mbps": competition.get("background_offered_mbps"),
            "background_throughput_mbps": (competition.get("background") or {}).get(
                "throughput_mbps"
            ),
            "background_loss_percent": (competition.get("background") or {}).get(
                "loss_percent"
            ),
            "background_jitter_ms": (competition.get("background") or {}).get(
                "jitter_ms"
            ),
            "total_offered_mbps": competition.get("total_offered_mbps"),
            "total_received_mbps": competition.get("total_received_mbps"),
            "bottleneck_egress_sent_mbps": competition.get(
                "bottleneck_egress_sent_mbps"
            ),
            "link_capacity_mbps": competition.get("link_capacity_mbps"),
            "duration_seconds": competition.get("duration"),
            "minimum_acceptable_mbps": competition.get("minimum_acceptable_mbps"),
            "maximum_acceptable_mbps": competition.get("maximum_acceptable_mbps"),
            "tolerance_ratio": competition.get("tolerance_ratio"),
            "tc_class_delta": competition.get("tc_class_delta") or {},
            "tc_root_delta": competition.get("tc_root_delta") or {},
            "process_cleanup_verified": competition.get("process_cleanup_verified"),
            "tc_class_counter_increased": bool(
                competition.get("tc_class_counter_increased")
            ),
            "verification_state": kpi_state,
            "passed": bool(verification_report.get("passed")),
        },
        "verification_states": statuses,
        "all_evidence_consistent": all_consistent,
        "rollback": execution_result.get("rollback")
        or execution_result.get("cross_plane_rollback"),
    }


def _entry(
    *,
    index: int,
    message_type: str,
    stage: str,
    source_agent: str,
    target_agent: str,
    log: dict[str, Any] | None = None,
    input_summary: str,
    output_summary: str,
    status: str | None = None,
    confidence: Any = None,
    evidence_refs: list[str] | None = None,
    failure_reason: str | None = None,
    retry_suggestion: str | None = None,
) -> dict[str, Any]:
    resolved_status = status or (log or {}).get("status") or "success"
    return {
        "id": f"A2A-{index:02d}",
        "message_type": message_type,
        "stage": stage,
        "source_agent": source_agent,
        "target_agent": target_agent,
        "status": resolved_status,
        "timestamp": (log or {}).get("timestamp"),
        "duration_ms": (log or {}).get("duration_ms"),
        "input_summary": _trim(input_summary),
        "output_summary": _trim(output_summary),
        "confidence": _round_confidence(confidence),
        "evidence_refs": [item for item in (evidence_refs or []) if item],
        "failure_reason": _trim(failure_reason) if failure_reason else None,
        "retry_suggestion": _trim(retry_suggestion) if retry_suggestion else None,
    }


def _intent_understanding(
    state: dict[str, Any],
    intent_plan: dict[str, Any],
) -> dict[str, Any]:
    policy = _primary_policy(state)
    network_policy = policy.get("network_policy") or {}
    match = network_policy.get("match") or {}
    action = network_policy.get("action") or {}
    qos = action.get("qos_params") or {}
    sub_intent = _first_sub_intent(intent_plan)
    sub_match = sub_intent.get("match") or {}
    sub_action = sub_intent.get("action") or {}
    sub_constraints = sub_intent.get("constraints") or {}

    return {
        "raw_input": (state.get("user_intent") or {}).get("text")
        or intent_plan.get("raw_intent"),
        "plan_id": intent_plan.get("plan_id"),
        "business_object": (
            sub_intent.get("objective")
            or network_policy.get("policy_name")
            or _infer_business_object((state.get("user_intent") or {}).get("text"))
        ),
        "source_ip": match.get("source_ip")
        or sub_match.get("source_ip")
        or sub_match.get("src_ip"),
        "destination_ip": (
            match.get("destination_ip")
            or sub_match.get("destination_ip")
            or sub_match.get("dst_ip")
        ),
        "protocol": match.get("protocol") or sub_match.get("protocol"),
        "source_port": match.get("source_port") or sub_match.get("source_port"),
        "destination_port": match.get("destination_port")
        or sub_match.get("destination_port"),
        "action_type": action.get("action_type")
        or sub_action.get("action_type")
        or sub_intent.get("intent_type"),
        "bandwidth_target_mbps": qos.get("bandwidth")
        or sub_constraints.get("bandwidth_mbps"),
        "dscp_value": qos.get("dscp_value") or sub_action.get("dscp_value"),
        "sub_intent_count": _count(intent_plan.get("sub_intents")),
        "confidence_score": intent_plan.get("confidence_score")
        or policy.get("confidence_score"),
        "warnings": _list_values(intent_plan.get("parse_warnings"))
        + _list_values(policy.get("parse_warnings")),
        "clarification_questions": _list_values(
            intent_plan.get("clarification_questions")
        ),
        "planner_critic": intent_plan.get("critic_report") or {},
        "slot_filling": (sub_constraints.get("slot_filling") or {}),
    }


def _network_observation(
    state: dict[str, Any],
    verification_report: dict[str, Any],
    auto_healing: dict[str, Any] | None,
) -> dict[str, Any]:
    int_analysis = _primary_int_analysis(verification_report, auto_healing)
    pre_int = _pre_int_analysis(verification_report, auto_healing)
    post_int = _post_int_analysis(verification_report, auto_healing)
    kpi = verification_report.get("kpi") or {}
    device_counters = verification_report.get("device_counters") or {}
    qdisc = device_counters.get("tc_qdisc") or {}
    p4_counter = device_counters.get("p4_direct_counter") or {}
    snapshot = _network_summary(state.get("network_snapshot"))
    comparison = (
        (auto_healing or {}).get("comparison")
        or verification_report.get("healing_comparison")
        or {}
    )
    execution = state.get("execution_result") or {}
    execution_qos = execution.get("qos_enforcement") or {}
    verified_qos = (verification_report.get("config_verification") or {}).get(
        "switch_egress_qos"
    ) or {}
    binding = execution_qos.get("qos_binding") or verified_qos.get("qos_binding") or {}
    int_trusted = bool(int_analysis.get("used_for_decision"))
    actual_path = (
        (int_analysis.get("switch_path") if int_trusted else None)
        or binding.get("current_path")
        or []
    )
    actual_devices = (
        (int_analysis.get("device_path") if int_trusted else None)
        or binding.get("current_device_path")
        or []
    )

    return {
        "topology": snapshot,
        "current_path_id": execution_qos.get("current_path_id")
        or binding.get("path_id"),
        "configured_primary_path_id": binding.get("configured_primary_path_id")
        or _configured_primary_path_id(),
        "alternative_path_ids": binding.get("alternative_path_ids") or [],
        "current_path": actual_path,
        "current_path_text": _path_text(actual_path),
        "pre_heal_path": pre_int.get("switch_path") or [],
        "pre_heal_path_text": _path_text(pre_int.get("switch_path")),
        "post_heal_path": post_int.get("switch_path") or [],
        "post_heal_path_text": _path_text(post_int.get("switch_path")),
        "device_path": actual_devices,
        "links": int_analysis.get("links") or [],
        "path_state": _path_state(int_analysis, comparison),
        "int": {
            "status": int_analysis.get("status"),
            "quality_status": int_analysis.get("quality_status", "UNAVAILABLE"),
            "quality_reason": int_analysis.get("quality_reason", "没有可用的INT记录"),
            "used_for_decision": int_trusted,
            "validation_checks": int_analysis.get("validation_checks") or [],
            "hop_count": int_analysis.get("hop_count"),
            "path_latency_ms": (
                (int_analysis.get("path_latency_ms") or kpi.get("int_path_latency_ms"))
                if int_trusted
                else None
            ),
            "max_queue_occupancy": (
                (
                    int_analysis.get("max_queue_occupancy")
                    if int_analysis.get("max_queue_occupancy") is not None
                    else kpi.get("int_max_queue_occupancy")
                )
                if int_trusted
                else None
            ),
            "worst_latency_hop": int_analysis.get("worst_latency_hop"),
            "worst_queue_hop": int_analysis.get("worst_queue_hop"),
        },
        "before_int": _int_digest(pre_int),
        "after_int": _int_digest(post_int),
        "qdisc": {
            "queue_depth": kpi.get("queue_depth") or qdisc.get("queue_depth"),
            "queue_depth_bytes": qdisc.get("queue_depth_bytes"),
            "drops": qdisc.get("dropped_packets"),
            "overlimits": qdisc.get("overlimits"),
            "congested_interfaces": qdisc.get("congested_interfaces"),
        },
        "kpi": {
            "latency_ms": kpi.get("latency_ms"),
            "packet_loss_rate": kpi.get("packet_loss_rate"),
            "throughput_mbps": kpi.get("throughput_mbps"),
            "jitter_ms": kpi.get("jitter_ms"),
        },
        "p4_direct_counter": {
            "packets": p4_counter.get("packets"),
            "bytes": p4_counter.get("bytes"),
            "direct_entries": p4_counter.get("direct_entries"),
        },
        "telemetry_source": verification_report.get("telemetry_source"),
    }


def _pre_execution_rejection(state: dict[str, Any]) -> dict[str, Any]:
    """Return the authoritative admission-stop projection, if applicable.

    A blocked capacity decision has no execution receipt by design.  Keep it
    separate from controller/readback failure so consumers never translate
    missing southbound artefacts into stale or failed evidence.
    """
    report = _jsonable(state.get("conflict_report") or {}) or {}
    if not isinstance(report, dict) or not report.get("blocking"):
        return {}
    category = str(report.get("stop_category") or "").upper()
    final_reason = str(report.get("final_reason_code") or "").upper()
    capacity_rejected = category == "CAPACITY_ADMISSION_REJECTED" or final_reason in {
        "RUNTIME_CAPACITY_UNVERIFIED", "RUNTIME_CAPACITY_INSUFFICIENT",
        "CALIBRATION_DOES_NOT_SUPPORT_CANDIDATE",
    }
    if not capacity_rejected:
        return {}
    admission = report.get("admission_decision") or {}
    item = next(
        (
            value for value in _dict_items(report.get("conflict_items"))
            if value.get("admission_decision")
        ),
        {},
    )
    item_admission = item.get("admission_decision") or {}
    return {
        "stop_category": "CAPACITY_ADMISSION_REJECTED",
        "conflict_detected": False,
        "initial_admission_reason": report.get("initial_admission_reason"),
        "recovery_action": report.get("recovery_action"),
        "calibration_result": report.get("calibration_result"),
        "final_reason_code": report.get("final_reason_code") or item_admission.get("reason_code"),
        "final_decision": report.get("final_decision") or state.get("current_status") or "UNSATISFIABLE",
        "southbound_execution": report.get("southbound_execution") or "NOT_EXECUTED",
        "traffic_scope_key": report.get("traffic_scope_key"),
        "requested_mbps": admission.get("requested_mbps") or item_admission.get("requested_mbps"),
        "physical_capacity_mbps": admission.get("physical_capacity_mbps") or admission.get("capacity_mbps"),
        "runtime_available_mbps": admission.get("available_mbps") or item_admission.get("available_mbps"),
        "capacity_evidence": report.get("capacity_evidence") or {},
    }


def _policy_conflict(state: dict[str, Any]) -> dict[str, Any]:
    report = state.get("conflict_report") or {}
    if not isinstance(report, dict):
        report = _jsonable(report) or {}
    if not isinstance(report, dict):
        report = {}

    conflict_items = _dict_items(report.get("conflict_items"))
    warning_items = _dict_items(report.get("warning_items"))
    if not warning_items:
        warning_items = [
            {
                "conflict_type": "WARNING",
                "severity": "LOW",
                "explanation": warning,
            }
            for warning in _list_values(report.get("warnings"))
        ]

    if not report:
        policy = _primary_policy(state)
        return {
            "status": "not_checked",
            "has_conflict": None,
            "conflict_type": None,
            "severity": None,
            "summary": "尚未执行策略冲突检查。",
            "approval_token": None,
            "rejection_reason": None,
            "suggestions": [],
            "warnings": [],
            "items": [],
            "warning_items": [],
            "severity_counts": _conflict_severity_counts([], []),
            "matrix": [],
            "conflict_graph": _conflict_graph(policy, [], [], "not_checked", None),
            "evidence_refs": [],
        }

    has_conflict = bool(report.get("has_conflict"))
    conflict_detected = bool(report.get("conflict_detected", has_conflict))
    stop_category = report.get("stop_category") or "NONE"
    resolution = report.get("resolution")
    blocking = bool(report.get("blocking", has_conflict))
    status = (
        "capacity_rejected"
        if blocking and str(stop_category).upper() == "CAPACITY_ADMISSION_REJECTED"
        else "blocked"
        if blocking
        else (
            "reused"
            if resolution == "REUSE_EXISTING_POLICY"
            else (
                "update"
                if resolution == "UPDATE_EXISTING_POLICY"
                else "warning" if warning_items else "clear"
            )
        )
    )
    severity = report.get("severity") or _max_conflict_severity(
        conflict_items + warning_items
    )
    summary = report.get("explanation") or (
        "容量准入未通过，未开始网络配置下发。"
        if blocking and str(stop_category).upper() == "CAPACITY_ADMISSION_REJECTED"
        else "发现阻断性策略冲突，需修正后再下发。"
        if conflict_detected
        else "未发现阻断性策略冲突，可以进入策略编译与下发。"
    )
    policy = _primary_policy(state)
    graph = _conflict_graph(policy, conflict_items, warning_items, status, severity)

    return {
        "status": status,
        "has_conflict": conflict_detected,
        "conflict_detected": conflict_detected,
        "stop_category": stop_category,
        "conflict_type": report.get("conflict_type"),
        "severity": severity,
        "summary": summary,
        "approval_token": report.get("approval_token"),
        "blocking": blocking,
        "requires_manual_review": bool(
            report.get("requires_manual_review", has_conflict)
        ),
        "resolution": resolution,
        "execution_mode": report.get("execution_mode"),
        "existing_policy_id": report.get("existing_policy_id"),
        "traffic_scope_key": report.get("traffic_scope_key"),
        "old_qos": report.get("old_qos"),
        "new_qos": report.get("new_qos"),
        "changed_fields": report.get("changed_fields") or [],
        "old_bandwidth_mbps": report.get("old_bandwidth_mbps"),
        "new_bandwidth_mbps": report.get("new_bandwidth_mbps"),
        "change_direction": report.get("change_direction"),
        "released_bandwidth_mbps": report.get("released_bandwidth_mbps", 0),
        "rejection_reason": report.get("rejection_reason"),
        "initial_admission_reason": report.get("initial_admission_reason"),
        "recovery_action": report.get("recovery_action"),
        "calibration_result": report.get("calibration_result"),
        "final_reason_code": report.get("final_reason_code"),
        "final_decision": report.get("final_decision"),
        "southbound_execution": report.get("southbound_execution"),
        "suggestions": _list_values(report.get("suggestions")),
        "warnings": _list_values(report.get("warnings")),
        "items": conflict_items,
        "warning_items": warning_items,
        "capacity_evidence": report.get("capacity_evidence") or {},
        "capacity_default_view": (report.get("capacity_evidence") or {}).get(
            "default_view"
        )
        or {},
        "severity_counts": _conflict_severity_counts(conflict_items, warning_items),
        "matrix": _conflict_matrix(conflict_items, warning_items),
        "conflict_graph": graph,
        "evidence_refs": _refs_from_report(report, "conflict_items", "warning_items"),
    }


def _dict_items(value: Any) -> list[dict[str, Any]]:
    value = _jsonable(value)
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _conflict_severity_counts(
    conflict_items: list[dict[str, Any]],
    warning_items: list[dict[str, Any]],
) -> dict[str, int]:
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0}
    for item in conflict_items + warning_items:
        severity = str(item.get("severity") or "INFO").upper()
        if severity not in counts:
            severity = "INFO"
        counts[severity] += 1
    return counts


def _max_conflict_severity(items: list[dict[str, Any]]) -> str | None:
    rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}
    best: str | None = None
    best_rank = -1
    for item in items:
        severity = str(item.get("severity") or "INFO").upper()
        score = rank.get(severity, 0)
        if score > best_rank:
            best = severity
            best_rank = score
    return best


def _conflict_graph(
    policy: dict[str, Any],
    conflict_items: list[dict[str, Any]],
    warning_items: list[dict[str, Any]],
    status: str,
    severity: Any,
) -> dict[str, list[dict[str, Any]]]:
    network_policy = policy.get("network_policy") or {}
    current_policy_id = (
        network_policy.get("policy_id") or policy.get("policy_id") or "new_policy"
    )
    current_policy_name = (
        network_policy.get("policy_name") or policy.get("policy_name") or "当前策略"
    )
    source_id = f"policy:{current_policy_id}"
    nodes: dict[str, dict[str, Any]] = {
        source_id: {
            "id": source_id,
            "label": current_policy_name,
            "kind": "current_policy",
            "status": status,
            "severity": severity,
        }
    }
    edges: list[dict[str, Any]] = []

    for kind, items in (("conflict", conflict_items), ("warning", warning_items)):
        for index, item in enumerate(items):
            target = _conflict_target(item, index)
            nodes.setdefault(target["id"], target)
            conflict_type = item.get("conflict_type") or item.get("type") or "UNKNOWN"
            item_severity = str(
                item.get("severity") or ("LOW" if kind == "warning" else "INFO")
            ).upper()
            edges.append(
                {
                    "id": f"{source_id}->{target['id']}:{kind}:{index}",
                    "source": source_id,
                    "target": target["id"],
                    "source_label": current_policy_name,
                    "target_label": target["label"],
                    "kind": kind,
                    "conflict_type": conflict_type,
                    "severity": item_severity,
                    "label": f"{conflict_type} / {item_severity}",
                    "evidence": _trim(
                        item.get("explanation") or item.get("description")
                    ),
                    "suggestion": _trim(
                        item.get("suggestion") or _first(item.get("suggestions"))
                    ),
                }
            )

    return {"nodes": list(nodes.values()), "edges": edges}


def _conflict_target(item: dict[str, Any], index: int = 0) -> dict[str, Any]:
    if item.get("existing_policy_id") or item.get("existing_policy_name"):
        policy_id = item.get("existing_policy_id") or item.get("existing_policy_name")
        return {
            "id": f"policy:{policy_id}",
            "label": item.get("existing_policy_name") or str(policy_id),
            "kind": "existing_policy",
            "severity": item.get("severity"),
        }
    if item.get("link_id"):
        return {
            "id": f"link:{item['link_id']}",
            "label": str(item["link_id"]),
            "kind": "link",
            "severity": item.get("severity"),
        }
    devices = (
        item.get("unavailable_devices")
        or item.get("devices")
        or item.get("affected_devices")
    )
    if isinstance(devices, list) and devices:
        label = ", ".join(str(device) for device in devices[:3])
        return {
            "id": f"device:{label}",
            "label": label,
            "kind": "device",
            "severity": item.get("severity"),
        }
    if item.get("resource_check_scope"):
        scope = str(item["resource_check_scope"])
        return {
            "id": f"resource:{scope}",
            "label": scope,
            "kind": "resource",
            "severity": item.get("severity"),
        }
    conflict_type = item.get("conflict_type") or item.get("type") or f"item_{index}"
    return {
        "id": f"check:{conflict_type}:{index}",
        "label": str(conflict_type),
        "kind": "check",
        "severity": item.get("severity"),
    }


def _conflict_matrix(
    conflict_items: list[dict[str, Any]],
    warning_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for kind, items in (("conflict", conflict_items), ("warning", warning_items)):
        for index, item in enumerate(items):
            target = _conflict_target(item, index)
            rows.append(
                {
                    "kind": kind,
                    "conflict_type": item.get("conflict_type")
                    or item.get("type")
                    or "UNKNOWN",
                    "severity": str(
                        item.get("severity") or ("LOW" if kind == "warning" else "INFO")
                    ).upper(),
                    "target": target["label"],
                    "target_kind": target["kind"],
                    "evidence": _trim(
                        item.get("explanation") or item.get("description")
                    ),
                    "resource": _conflict_resource_summary(item),
                    "suggestion": _trim(
                        item.get("suggestion") or _first(item.get("suggestions"))
                    ),
                }
            )
    return rows


def _conflict_resource_summary(item: dict[str, Any]) -> str | None:
    fields = [
        ("requested_bandwidth", "requested"),
        ("reserved_bandwidth", "reserved"),
        ("available_bandwidth", "available"),
        ("required_bandwidth", "required"),
    ]
    parts = [
        f"{label}={item[key]}" for key, label in fields if item.get(key) is not None
    ]
    return ", ".join(parts) if parts else None


def _problem_diagnosis(
    verification_report: dict[str, Any],
    auto_healing: dict[str, Any] | None,
    network_observation: dict[str, Any],
) -> dict[str, Any]:
    root_cause_report = verification_report.get("root_cause_report") or {}
    int_analysis = _pre_int_analysis(
        verification_report, auto_healing
    ) or _primary_int_analysis(
        verification_report,
        auto_healing,
    )
    qdisc = network_observation.get("qdisc") or {}
    violations = _list_values(verification_report.get("violations"))
    congested = bool(
        int_analysis.get("congestion_on_primary")
        or qdisc.get("overlimits")
        or qdisc.get("drops")
        or qdisc.get("queue_depth")
    )
    evidence = []
    if int_analysis.get("bottleneck_link"):
        evidence.append(f"INT bottleneck link: {int_analysis.get('bottleneck_link')}")
    if int_analysis.get("worst_queue_hop"):
        hop = int_analysis["worst_queue_hop"]
        evidence.append(
            f"INT worst queue: {hop.get('switch_name') or hop.get('device_id')} queue={hop.get('queue_occupancy')}"
        )
    if qdisc.get("overlimits") is not None:
        evidence.append(f"tc overlimits={qdisc.get('overlimits')}")
    if violations:
        evidence.extend(violations)
    for item in root_cause_report.get("evidence") or []:
        observation = item.get("observation")
        source = item.get("source")
        if observation:
            evidence.append(f"{source}: {observation}" if source else str(observation))

    return {
        "is_congestion": congested
        or root_cause_report.get("primary_cause") == "primary_path_congestion",
        "primary_cause": root_cause_report.get("primary_cause"),
        "confidence": root_cause_report.get("confidence"),
        "confidence_level": root_cause_report.get("confidence_level"),
        "bottleneck_link": root_cause_report.get("bottleneck_link")
        or int_analysis.get("bottleneck_link"),
        "bottleneck_device": root_cause_report.get("bottleneck_device")
        or _bottleneck_device(int_analysis),
        "primary_path_affected": bool(
            int_analysis.get("primary_path")
            or int_analysis.get("congestion_on_primary")
        ),
        "root_cause": root_cause_report.get("summary")
        or verification_report.get("root_cause")
        or "; ".join(evidence)
        or "未发现明确瓶颈。",
        "evidence": _dedupe(evidence),
        "violations": violations,
        "candidate_causes": root_cause_report.get("candidate_causes") or [],
        "rejected_hypotheses": root_cause_report.get("rejected_hypotheses") or [],
        "candidate_cause_graph": root_cause_report.get("candidate_cause_graph") or {},
        "counterfactual_checks": root_cause_report.get("counterfactual_checks") or [],
        "next_probe": root_cause_report.get("next_probe") or {},
        "experience_memory": root_cause_report.get("experience_memory") or {},
        "recommended_action": root_cause_report.get("recommended_action"),
    }


def _candidate_solutions(
    state: dict[str, Any],
    diagnosis: dict[str, Any],
    auto_healing: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    selected_actions = {
        item.get("action")
        for item in (auto_healing or {}).get("actions") or []
        if item.get("action")
    }
    congested = bool(diagnosis.get("is_congestion"))
    action_type = (
        (_primary_policy(state).get("network_policy") or {}).get("action") or {}
    ).get("action_type")

    candidates = [
        {
            "id": "keep_primary_path",
            "title": "保持主路径并只观察",
            "recommended": not congested,
            "selected": False,
            "risk_score": 70 if congested else 25,
            "reason": "主路径拥塞时继续保持会扩大 KPI 失败风险；无拥塞时可避免不必要变更。",
        },
        {
            "id": "reroute_secondary_path",
            "title": "评估可绕开当前瓶颈的替代路径",
            "recommended": False,
            "selected": "reroute_secondary_path" in selected_actions,
            "risk_score": 25 if congested else 55,
            "reason": "候选路径必须结合当前活动路径与瓶颈链路进行适用性验证；相同路径或仍经过瓶颈的路径不可执行。",
        },
        {
            "id": "throttle_low_priority_source",
            "title": "评估共享瓶颈上的可抢占竞争流量",
            "recommended": action_type == "RATE_LIMIT",
            "selected": "throttle_low_priority_source" in selected_actions
            or action_type == "RATE_LIMIT",
            "risk_score": 35,
            "reason": "仅当运行态证据确认存在可安全降级且能够释放足够带宽的竞争 Intent 时可执行。",
        },
        {
            "id": "rollback_last_policy",
            "title": "回滚最近一次策略",
            "recommended": False,
            "selected": False,
            "risk_score": 45,
            "reason": "仅当写入成功但验证持续失败、且自愈无效时启用。",
        },
    ]
    return candidates


def _agent_votes(
    timeline: list[dict[str, Any]],
    diagnosis: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected = [
        item for item in candidates if item.get("selected") or item.get("recommended")
    ]
    selected_title = selected[0]["title"] if selected else "继续观察并等待人工确认"
    risk_by_agent = {
        "intent_planner": 20,
        "policy_modeling": 25,
        "policy_conflict_checker": 35,
        "policy_compiler": 30,
        "p4_rule_verifier": 28,
        "status_verifier": 65 if diagnosis.get("is_congestion") else 20,
        "auto_healing_controller": 30,
    }
    votes = []
    seen: set[str] = set()
    for entry in timeline:
        agent = entry.get("target_agent")
        if not agent or agent in seen or agent == "orchestrator":
            continue
        seen.add(agent)
        vote = selected_title
        if agent == "policy_conflict_checker":
            vote = "允许继续下发" if entry.get("status") != "failed" else "拒绝当前策略"
        elif agent == "p4_rule_verifier":
            vote = (
                "流表计划可安全下发"
                if entry.get("status") == "success"
                else "阻断下发并要求修复"
            )
        elif agent == "status_verifier":
            vote = (
                "未执行：容量准入阶段拒绝，未生成 KPI 验证结论"
                if entry.get("status") == "skipped"
                else "触发自愈" if diagnosis.get("is_congestion") else "KPI 达标，保持策略"
            )
        votes.append(
            {
                "agent_id": agent,
                "vote": vote,
                "risk_score": risk_by_agent.get(agent, 40),
                "reason": entry.get("output_summary") or entry.get("input_summary"),
                "evidence": entry.get("evidence_refs") or [],
            }
        )
    return votes


def _final_action(
    state: dict[str, Any],
    auto_healing: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    execution = state.get("execution_result") or {}
    flow_plan = (
        state.get("verified_flow_table_plan") or state.get("flow_table_plan") or {}
    )
    selected = [item for item in candidates if item.get("selected")]
    healing_actions = (auto_healing or {}).get("actions") or []
    pre_execution = _pre_execution_rejection(state)
    planned_actions = ["install_compiled_policy"] if _primary_policy(state) else []
    actual_actions = [] if pre_execution else (
        [item.get("action") or item.get("id") or item.get("title") for item in healing_actions]
        or (["install_compiled_policy"] if execution else [])
    )
    return {
        # Planned actions express intent; actual actions require a real
        # execution receipt.  Never promote the former into execution proof.
        "planned_actions": planned_actions,
        "actual_actions": actual_actions,
        "selected_actions": actual_actions or [item["id"] for item in selected],
        "execution_state": pre_execution.get("southbound_execution") or ("EXECUTED" if execution else "NOT_CREATED"),
        "execution_reason": pre_execution.get("final_reason_code"),
        "rationale": _final_action_rationale(auto_healing, selected),
        "flow_entries": execution.get("total_entries_written")
        or _flow_entry_count(flow_plan),
        "affected_devices": (
            execution.get("devices_confirmed")
            or flow_plan.get("affected_devices")
            or _devices_from_write_requests(flow_plan)
        ),
        "delivery_status": execution.get("status"),
        "rollback_strategy": "保留 rollback_requests；验证失败时由 ReAct 重新编译、回滚或进入人工审批。",
    }


def _verification_result(
    verification_report: dict[str, Any],
    auto_healing: dict[str, Any] | None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    admission = _pre_execution_rejection(state or {})
    if admission:
        return {
            "passed": False,
            "verification_state": "NOT_APPLICABLE",
            "config_verification": {"verification_state": "NOT_APPLICABLE"},
            "policy_commit": {"status": "NOT_CREATED"},
            "intent_achieved": False,
            "before_kpi": {},
            "after_kpi": {},
            "kpi_deltas": {},
            "path_changed": None,
            "switched_to_backup": None,
            "violations": [],
            "root_cause_report": {},
            "evidence": [],
            "reason": "Intent was rejected during capacity admission before network configuration.",
        }
    comparison = (
        (auto_healing or {}).get("comparison")
        or verification_report.get("healing_comparison")
        or {}
    )
    pre_report = verification_report.get("pre_heal_verification") or {}
    pre_execution = verification_report.get("pre_execution_verification") or {}
    experience_case = _experience_case(state or {})
    before_kpi = _first_non_empty_kpi(
        pre_report.get("kpi"),
        verification_report.get("before_kpi"),
        pre_execution.get("kpi"),
        comparison.get("before_kpi"),
        experience_case.get("before_kpi"),
    )
    after_kpi = _first_non_empty_kpi(
        verification_report.get("after_kpi"),
        verification_report.get("kpi"),
        comparison.get("after_kpi"),
        experience_case.get("after_kpi"),
    )
    return {
        "passed": verification_report.get("passed"),
        "verification_state": verification_report.get("verification_state"),
        "config_verification": verification_report.get("config_verification") or {},
        "policy_commit": verification_report.get("policy_commit") or {},
        "intent_achieved": verification_report.get("intent_achieved"),
        "before_kpi": before_kpi,
        "after_kpi": after_kpi,
        "kpi_deltas": comparison.get("kpi_deltas") or {},
        "path_changed": comparison.get("path_changed"),
        "switched_to_backup": comparison.get("switched_to_backup"),
        "violations": _list_values(verification_report.get("violations")),
        "root_cause_report": verification_report.get("root_cause_report") or {},
        "evidence": _telemetry_refs(verification_report),
    }


def _experience_case(state: dict[str, Any]) -> dict[str, Any]:
    for result in state.get("policy_results") or []:
        case = result.get("experience_case")
        if isinstance(case, dict):
            return case
    return {}


def _first_non_empty_kpi(*items: Any) -> dict[str, Any]:
    for item in items:
        if not isinstance(item, dict):
            continue
        if any(value is not None for value in item.values()):
            return item
    return {}


def _fallback_plan(
    state: dict[str, Any],
    verification_report: dict[str, Any],
) -> dict[str, Any]:
    react = state.get("react_state") or {}
    can_retry = react.get("can_retry")
    if can_retry is None:
        retry_count = int(react.get("retry_count") or 0)
        max_retries = int(react.get("max_retries") or 3)
        can_retry = retry_count < max_retries
    return {
        "if_verification_failed": verification_report.get("react_suggestion")
        or "重新采样遥测，根据失败原因触发 ReAct 重编译或自愈。",
        "next_react_step": (
            "回传 StatusVerifier 的 root_cause 给 PolicyCompiler，调整路径、优先级或限速参数。"
        ),
        "human_approval_when": "ReAct 超过最大重试次数、冲突检查高危、或回滚仍无法恢复 KPI 时进入人工审批。",
        "retry_count": react.get("retry_count", 0),
        "max_retries": react.get("max_retries", 3),
        "can_retry": can_retry,
    }


def _primary_policy(state: dict[str, Any]) -> dict[str, Any]:
    if isinstance(state.get("yang_draft"), dict):
        return state["yang_draft"]
    policies = state.get("yang_policies") or []
    if policies and isinstance(policies[0], dict):
        return policies[0]
    policy_results = state.get("policy_results") or []
    for result in policy_results:
        for key in ("yang_policy", "policy"):
            if isinstance(result.get(key), dict):
                return result[key]
    return {}


def _first_sub_intent(intent_plan: dict[str, Any]) -> dict[str, Any]:
    sub_intents = intent_plan.get("sub_intents") or []
    return sub_intents[0] if sub_intents and isinstance(sub_intents[0], dict) else {}


def _infer_business_object(text: Any) -> str:
    value = str(text or "")
    if "视频" in value or "video" in value.lower():
        return "视频业务"
    if "AI" in value or "ai" in value.lower():
        return "AI 训练/推理业务"
    if "备份" in value:
        return "备份业务"
    if "科研" in value:
        return "科研业务"
    return "通用网络业务"


def _primary_int_analysis(
    verification_report: dict[str, Any],
    auto_healing: dict[str, Any] | None,
) -> dict[str, Any]:
    comparison = (
        (auto_healing or {}).get("comparison")
        or verification_report.get("healing_comparison")
        or {}
    )
    if (
        isinstance(comparison.get("post_int"), dict)
        and comparison["post_int"].get("status") == "ok"
    ):
        return comparison["post_int"]
    if isinstance(verification_report.get("int_analysis"), dict):
        return verification_report["int_analysis"]
    if isinstance(comparison.get("pre_int"), dict):
        return comparison["pre_int"]
    if isinstance((auto_healing or {}).get("pre_heal_int_analysis"), dict):
        return (auto_healing or {})["pre_heal_int_analysis"]
    return {}


def _pre_int_analysis(
    verification_report: dict[str, Any],
    auto_healing: dict[str, Any] | None,
) -> dict[str, Any]:
    comparison = (
        (auto_healing or {}).get("comparison")
        or verification_report.get("healing_comparison")
        or {}
    )
    if isinstance(comparison.get("pre_int"), dict):
        return comparison["pre_int"]
    if isinstance((auto_healing or {}).get("pre_heal_int_analysis"), dict):
        return (auto_healing or {})["pre_heal_int_analysis"]
    if isinstance(verification_report.get("pre_heal_int_analysis"), dict):
        return verification_report["pre_heal_int_analysis"]
    if isinstance(verification_report.get("int_analysis"), dict):
        return verification_report["int_analysis"]
    return {}


def _post_int_analysis(
    verification_report: dict[str, Any],
    auto_healing: dict[str, Any] | None,
) -> dict[str, Any]:
    comparison = (
        (auto_healing or {}).get("comparison")
        or verification_report.get("healing_comparison")
        or {}
    )
    if isinstance(comparison.get("post_int"), dict):
        return comparison["post_int"]
    if isinstance(verification_report.get("int_analysis"), dict):
        return verification_report["int_analysis"]
    return {}


def _int_digest(int_analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": int_analysis.get("switch_path") or [],
        "path_text": _path_text(int_analysis.get("switch_path")),
        "status": int_analysis.get("status"),
        "quality_status": int_analysis.get("quality_status", "UNAVAILABLE"),
        "quality_reason": int_analysis.get("quality_reason"),
        "used_for_decision": bool(int_analysis.get("used_for_decision")),
        "primary_path": int_analysis.get("primary_path"),
        "secondary_path": int_analysis.get("secondary_path"),
        "bottleneck_link": int_analysis.get("bottleneck_link"),
        "congestion_on_primary": int_analysis.get("congestion_on_primary"),
        "max_queue_occupancy": int_analysis.get("max_queue_occupancy"),
        "path_latency_ms": int_analysis.get("path_latency_ms"),
    }


def _path_text(path: Any) -> str:
    if not path:
        return "-"
    if isinstance(path, list):
        return " -> ".join(str(item) for item in path)
    return str(path)


def _path_state(int_analysis: dict[str, Any], comparison: dict[str, Any]) -> str:
    if comparison.get("switched_to_backup") or int_analysis.get("secondary_path"):
        return "secondary_path"
    if int_analysis.get("congestion_on_primary"):
        return "primary_congested"
    if int_analysis.get("primary_path"):
        return "primary_path"
    return "unknown"


def _bottleneck_device(int_analysis: dict[str, Any]) -> str | None:
    for key in ("worst_queue_hop", "worst_latency_hop"):
        hop = int_analysis.get(key)
        if isinstance(hop, dict):
            return hop.get("switch_name") or hop.get("device_id")
    link = int_analysis.get("bottleneck_link")
    if isinstance(link, str) and "-" in link:
        return link.split("-", 1)[0]
    return None


def _final_action_rationale(
    auto_healing: dict[str, Any] | None,
    selected: list[dict[str, Any]],
) -> str:
    if auto_healing:
        actions = [
            item.get("action") or item.get("title") or "unknown"
            for item in auto_healing.get("actions") or []
        ]
        return f"验证发现 KPI 未达标或主路径拥塞，因此执行 {', '.join(actions) or '自愈动作'} 并复测。"
    if selected:
        return f"选择 {', '.join(item['title'] for item in selected)}，因为它们与当前诊断证据匹配。"
    return "冲突检查、流表验证和 KPI 验证均通过，因此保持已编译策略。"


def _devices_from_write_requests(plan: dict[str, Any]) -> list[str]:
    return [
        str(item.get("device_id"))
        for item in plan.get("write_requests") or []
        if item.get("device_id")
    ]


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _dedupe(values: list[Any]) -> list[Any]:
    result = []
    seen: set[str] = set()
    for value in values:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _stage_log(
    logs: list[dict[str, Any]],
    stage: str,
    agent_id: str | None = None,
) -> dict[str, Any] | None:
    for item in logs:
        if item.get("stage") != stage:
            continue
        if agent_id and item.get("agent_id") != agent_id:
            continue
        return item
    return None


def _network_summary(snapshot: Any) -> dict[str, Any]:
    snapshot = snapshot or {}
    summary = {
        "devices": _count(snapshot.get("devices")),
        "links": _count(snapshot.get("links")),
        "hosts": _count(snapshot.get("hosts")),
        "runtime_flows": _count(snapshot.get("flows")),
        "existing_policies": _count(snapshot.get("existing_policies")),
    }
    topology_summary = snapshot.get("topology_summary") or {}
    if isinstance(topology_summary, dict):
        summary.update(topology_summary)
    return summary


def _intent_plan_summary(intent_plan: dict[str, Any]) -> str:
    sub_intents = intent_plan.get("sub_intents") or []
    actions = [
        (
            (item.get("action") or {}).get("action_type")
            or item.get("intent_type")
            or "UNKNOWN"
        )
        for item in sub_intents
    ]
    action_text = ", ".join(actions) if actions else "无动作"
    return f"拆解 {len(sub_intents)} 个子意图，动作类型: {action_text}。"


def _yang_policy_summary(policies: list[dict[str, Any]]) -> str:
    names = [
        ((policy.get("network_policy") or {}).get("policy_name") or "unnamed-policy")
        for policy in policies
    ]
    return f"生成 {len(policies)} 个 YANGPolicy: {', '.join(names) or '-'}。"


def _flow_plan_summary(plan: dict[str, Any]) -> str:
    return (
        f"生成 {_flow_entry_count(plan)} 条表项，影响设备: "
        f"{', '.join(plan.get('affected_devices') or []) or '-'}。"
    )


def _execution_summary(result: dict[str, Any]) -> str:
    if not result:
        return "尚无下发结果。"
    if result.get("operation") == "VERIFY_EXISTING":
        return f"状态 SUCCESS，复用并读回验证现有策略 {result.get('traffic_scope_key') or '-'}，未重复下发。"
    if result.get("operation") == "MODIFY_QOS_RESOURCE":
        change = result.get("policy_change") or {}
        return (
            f"状态 SUCCESS，原地更新 QoS 资源："
            f"{change.get('old_bandwidth_mbps')} → {change.get('new_bandwidth_mbps')} Mbps，"
            f"方向 {change.get('change_direction') or '-'}。"
        )
    return (
        f"状态 {result.get('status', 'UNKNOWN')}，写入 "
        f"{result.get('total_entries_written', 0)} 条表项，确认设备 "
        f"{', '.join(result.get('devices_confirmed') or []) or '-'}。"
    )


def _verification_summary(report: dict[str, Any]) -> str:
    if not report:
        return "尚无 KPI 验证报告。"
    kpi = report.get("kpi") or {}
    state = report.get("verification_state") or (
        "KPI_VERIFIED" if report.get("passed") else "KPI_FAILED"
    )
    return (
        f"验证状态 {state}；"
        f"KPI {'通过' if report.get('passed') else '未通过'}："
        f"latency={kpi.get('latency_ms')}ms, "
        f"throughput={kpi.get('throughput_mbps')}Mbps, "
        f"loss={kpi.get('packet_loss_rate')}。"
    )


def _healing_summary(auto_healing: dict[str, Any]) -> str:
    actions = [
        f"{item.get('action', 'unknown')}:{item.get('status', 'unknown')}"
        for item in auto_healing.get("actions") or []
    ]
    return f"执行自愈动作 {', '.join(actions) or '-'}。"


def _healing_comparison_summary(auto_healing: dict[str, Any]) -> str:
    comparison = auto_healing.get("comparison") or {}
    before_path = comparison.get("self_heal_before_path_text") or comparison.get(
        "before_path_text"
    )
    after_path = comparison.get("after_path_text")
    switched = comparison.get("switched_to_backup")
    return f"复测路径 {before_path or '-'} -> {after_path or '-'}，backup_switched={switched}。"


def _safety_summary(report: dict[str, Any]) -> str:
    if not report:
        return "尚无 Safety Assurance Council 评审报告。"
    return (
        f"安全评审 {report.get('overall_decision', 'unknown')}："
        f"risk={report.get('overall_risk_score')}, "
        f"level={report.get('risk_level')}, "
        f"blockers={len(report.get('hard_blockers') or [])}。"
    )


def _safety_status(report: dict[str, Any]) -> str:
    if not report:
        return "skipped"
    return "success" if report.get("overall_decision") == "approve" else "failed"


def _safety_refs(report: dict[str, Any]) -> list[str]:
    refs = ["SafetyCouncilReport"]
    for review in report.get("agent_reviews") or []:
        agent_id = review.get("agent_id")
        decision = review.get("decision")
        if agent_id:
            refs.append(f"{agent_id}:{decision or 'reviewed'}")
    if report.get("rollback_plan", {}).get("rollback_ready"):
        refs.append("rollback_plan_ready")
    return refs


def _refs_from_report(report: dict[str, Any], *keys: str) -> list[str]:
    refs: list[str] = []
    for key in keys:
        for item in report.get(key) or []:
            label = (
                item.get("conflict_type")
                or item.get("violation_type")
                or item.get("type")
            )
            explanation = item.get("explanation") or item.get("description")
            if label or explanation:
                refs.append(_trim(f"{label or 'item'}: {explanation or ''}"))
    return refs


def _telemetry_refs(report: dict[str, Any]) -> list[str]:
    refs = []
    if report.get("telemetry_source"):
        refs.append(f"telemetry_source:{report['telemetry_source']}")
    if report.get("int_path_records"):
        quality = report.get("int_quality") or {}
        if quality.get("used_for_decision"):
            refs.append("P4 INT trusted hop-by-hop path records")
        else:
            refs.append("P4 INT raw records (excluded from decisions)")
    int_analysis = report.get("int_analysis") or {}
    if int_analysis:
        refs.append("INT/qdisc root-cause analysis")
    if (report.get("kpi") or {}).get("throughput_mbps") is not None:
        refs.append("Mininet iperf or ONOS flow counter throughput")
    return refs


def _healing_refs(auto_healing: dict[str, Any]) -> list[str]:
    refs = []
    analysis = auto_healing.get("pre_heal_int_analysis") or {}
    if analysis.get("primary_path"):
        refs.append("pre_heal_int_analysis.primary_path")
    if analysis.get("congestion_on_primary"):
        refs.append("pre_heal_int_analysis.congestion_on_primary")
    for action in auto_healing.get("actions") or []:
        refs.append(f"healing_action:{action.get('action', 'unknown')}")
    return refs


def _evidence_sources(
    timeline: list[dict[str, Any]],
    report: dict[str, Any],
    auto_healing: dict[str, Any] | None,
) -> list[str]:
    refs = {
        ref for entry in timeline for ref in entry.get("evidence_refs") or [] if ref
    }
    if report.get("telemetry_source"):
        refs.add(f"telemetry:{report['telemetry_source']}")
    if auto_healing:
        refs.add("auto_healing.actions")
    return sorted(refs)


def _find_auto_healing(
    verification_report: dict[str, Any],
    policy_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if verification_report.get("auto_healing"):
        return verification_report["auto_healing"]
    for result in policy_results:
        if result.get("auto_healing"):
            return result["auto_healing"]
        nested = (result.get("status_verification_report") or {}).get("auto_healing")
        if nested:
            return nested
    return None


def _final_summary(
    status: str,
    verification_report: dict[str, Any],
    auto_healing: dict[str, Any] | None,
    state: dict[str, Any],
) -> str:
    if state.get("needs_clarification"):
        return "意图信息不足，系统已暂停并请求用户补充澄清。"
    if status == "success" and auto_healing:
        return (
            "系统完成意图解析、策略下发、KPI 验证，并在验证失败后触发自动自愈与复测。"
        )
    if status == "success":
        return "系统完成意图解析、策略下发和闭环 KPI 验证。"
    reason = _failure_reason(state, verification_report)
    return reason or f"意图处理结束，状态为 {status}。"


def _final_conclusion(
    status: str,
    verification_report: dict[str, Any],
    auto_healing: dict[str, Any] | None,
    state: dict[str, Any],
) -> str:
    if state.get("needs_clarification"):
        return (
            f"需要用户澄清：{state.get('clarification_question') or '请补充意图约束。'}"
        )
    if status == "success":
        if auto_healing:
            return "最终结论：自愈后业务 KPI 达标，备用路径和低优先级限速动作生效。"
        return "最终结论：业务意图已达成，策略写入成功且 KPI 验证通过。"
    return f"最终结论：当前未达成业务意图，原因：{_failure_reason(state, verification_report) or '未知'}"


def _failure_reason(
    state: dict[str, Any],
    verification_report: dict[str, Any],
) -> str | None:
    """Return the most specific terminal failure reason available.

    Conflict-blocked workflows never reach execution or KPI verification, so
    looking only at ``error_message`` and verification violations produces the
    misleading user-facing reason "未知".  Prefer the explicit conflict
    rejection before falling back to later-stage evidence.
    """
    if state.get("error_message"):
        return str(state["error_message"])

    conflict = state.get("conflict_report") or state.get("policy_conflict_report") or {}
    if conflict.get("blocking", conflict.get("has_conflict", False)):
        return str(
            conflict.get("final_reason_code")
            or conflict.get("rejection_reason")
            or conflict.get("explanation")
            or "策略冲突检查未通过。"
        )

    violations = _list_values(verification_report.get("violations"))
    if violations:
        return f"KPI 验证未通过：{'; '.join(violations)}"

    safety = state.get("safety_council_report") or {}
    blockers = _list_values(safety.get("hard_blockers"))
    if blockers:
        return f"安全评审未通过：{'; '.join(str(item) for item in blockers)}"
    return None


def _decision_title(agent_id: str, message_type: str) -> str:
    if message_type == "ReAct_Trigger":
        return "验证失败后的自愈触发"
    titles = {
        "network_sensing": "网络状态感知",
        "intent_planner": "自然语言意图解析",
        "policy_modeling": "抽象策略建模",
        "policy_conflict_checker": "策略冲突与资源检查",
        "policy_compiler": "P4/ONOS 策略编译",
        "p4_rule_verifier": "流表级安全验证",
        "safety_council": "执行前安全评审",
        "p4_runtime_executor": "南向规则下发",
        "status_verifier": "KPI 闭环验证",
    }
    return titles.get(agent_id, agent_id)


def _status_from_execution(result: dict[str, Any]) -> str:
    status = result.get("status")
    if status == "SUCCESS":
        return "success"
    if status in {"FAILED", "PARTIAL"}:
        return "failed"
    if status == "SKIPPED":
        return "skipped"
    return "success" if result else "skipped"


def _execution_failure(result: dict[str, Any]) -> str | None:
    if not result:
        return None
    errors = result.get("errors") or result.get("error")
    if isinstance(errors, list):
        return "; ".join(str(item) for item in errors)
    return str(errors) if errors else None


def _react_adjustment_summary(plan: dict[str, Any]) -> str | None:
    adjustments = plan.get("react_adjustments") or []
    return "; ".join(str(item) for item in adjustments) if adjustments else None


def _flow_entry_count(plan: dict[str, Any]) -> int:
    if isinstance(plan.get("total_entries"), int):
        return plan["total_entries"]
    return sum(
        len(request.get("table_entries") or [])
        for request in plan.get("write_requests") or []
    )


def _min_confidence(items: list[dict[str, Any]]) -> float | None:
    values: list[float] = [
        float(item["confidence_score"])
        for item in items
        if isinstance(item.get("confidence_score"), (int, float))
    ]
    return min(values) if values else None


def _round_confidence(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return round(float(value), 3)


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _list_values(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)]


def _first(value: Any) -> str | None:
    values = _list_values(value)
    return values[0] if values else None


def _trim(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else f"{text[: limit - 1]}..."

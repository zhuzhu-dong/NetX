from services.conversation_results import build_assistant_intent_message


def record(status, reason="", **extra):
    return {"intent_id": "INT-CONV-1", "status": status, "text": "raise video bandwidth", "intent_runtime": {"lifecycle": status, "last_transition_reason": reason}, **extra}


def test_satisfied_result_is_human_readable_and_has_evidence_link():
    message = build_assistant_intent_message(record("SATISFIED"))
    assert message["result_type"] == "INTENT_SATISFIED"
    assert message["title"] == "意图已生效"
    assert message["evidence_ref"].endswith("INT-CONV-1")


def test_capacity_rejection_preserves_old_state_without_secret():
    message = build_assistant_intent_message(record(
        "UNSATISFIABLE", "NO_FEASIBLE_HEALING_ACTION",
        result={"policy_conflict_report": {"capacity_evidence": {"admission_decision": {
            "operation": "INCREASE_UPDATE", "reason_code": "INSUFFICIENT_CAPACITY",
            "existing_target_mbps": 10, "requested_target_mbps": 30,
            "requested_delta_mbps": 20, "requested_mbps": 30,
            "contract_available_mbps": 29, "effective_available_mbps": 19,
            "schedulable_capacity_mbps": 49,
        }}}},
    ))
    assert message["result_type"] == "INTENT_REJECTED"
    assert message["preserved_state"]["old_policy_preserved"] is True
    assert message["reason_code"] == "INSUFFICIENT_CAPACITY"
    assert message["facts"]["capacity_admission"]["requested_delta_mbps"] == 20
    assert message["facts"]["secondary_reason_code"] == "NO_FEASIBLE_HEALING_ACTION"
    assert "10 Mbps" in message["summary"]
    assert "30 Mbps" in message["summary"]
    assert "20 Mbps" in message["summary"]
    assert "19 Mbps" in message["summary"]


def test_capacity_contract_keeps_missing_runtime_evidence_explicit():
    message = build_assistant_intent_message(record(
        "UNSATISFIABLE", "NO_FEASIBLE_HEALING_ACTION",
        result={"policy_conflict_report": {"capacity_evidence": {"admission_decision": {
            "operation": "INCREASE_UPDATE", "reason_code": "RUNTIME_CAPACITY_UNVERIFIED",
            "existing_target_mbps": 10, "requested_target_mbps": 30,
            "requested_delta_mbps": 20, "contract_available_mbps": 29,
            "contract_capacity_mbps": 49, "current_guaranteed_allocation_mbps": 20,
            "runtime_capacity_status": "UNVERIFIED", "runtime_schedulable_total_mbps": None,
            "runtime_current_allocation_mbps": 20, "runtime_available_mbps": None,
            "effective_available_mbps": None,
        }}}},
    ))
    capacity = message["facts"]["capacity_admission"]
    assert message["reason_code"] == "RUNTIME_CAPACITY_UNVERIFIED"
    assert message["message_id"].endswith("RUNTIME_CAPACITY_UNVERIFIED")
    assert capacity["runtime_available_mbps"] is None
    assert capacity["runtime_current_allocation_mbps"] == 20
    assert capacity["contract_capacity_mbps"] == 49
    assert capacity["effective_available_mbps"] is None
    assert "API_KEY" not in str(message)


def test_clarification_keeps_authoritative_questions():
    message = build_assistant_intent_message(record("NEEDS_CLARIFICATION", snapshot={
        "clarification_questions": ["请选择协议"],
        "clarification_details": [{"field": "protocol", "question": "请选择协议"}],
        "unresolved_fields": ["protocol"],
    }))
    assert message["result_type"] == "INTENT_NEEDS_CLARIFICATION"
    assert message["facts"]["clarification_questions"] == ["请选择协议"]
    assert message["facts"]["clarification_details"][0]["field"] == "protocol"
    assert message["facts"]["unresolved_fields"] == ["protocol"]
    assert message["message_id"].startswith("assistant-result:INT-CONV-1:NEEDS_CLARIFICATION:")


def test_clarification_turn_identity_changes_only_when_authoritative_facts_change():
    first = record("NEEDS_CLARIFICATION", "missing endpoint", snapshot={
        "clarification_questions": ["Please identify A and B"], "unresolved_fields": ["source", "destination"],
    }, intent_spec={"revision": 1})
    unchanged = record("NEEDS_CLARIFICATION", "missing endpoint", snapshot={
        "clarification_questions": ["Please identify A and B"], "unresolved_fields": ["source", "destination"],
    }, intent_spec={"revision": 1})
    second = record("NEEDS_CLARIFICATION", "missing service", snapshot={
        "clarification_questions": ["Please identify service and transport"], "unresolved_fields": ["service"],
    }, intent_spec={"revision": 2})
    assert build_assistant_intent_message(first)["message_id"] == build_assistant_intent_message(unchanged)["message_id"]
    assert build_assistant_intent_message(first)["message_id"] != build_assistant_intent_message(second)["message_id"]


def test_chinese_clarification_contract_hides_schema_fallbacks_and_dedupes_by_slot():
    message = build_assistant_intent_message(record("NEEDS_CLARIFICATION", snapshot={
        "clarification_questions": [
            "请指定需要保障业务的源对象，例如主机IP、源网段或站点名称。",
            "Please provide the missing value for source_ip.",
            "请指定需要保障业务的目的对象，例如服务器IP、目的网段或站点名称。",
            "Please provide the missing value for destination_ip.",
            "请说明希望保障的业务类型，例如科研视频、AI 数据或备份业务。",
            "Please provide the missing value for service_or_protocol.",
        ],
        "clarification_details": [
            {"field": "source", "question": "请指定需要保障业务的源对象，例如主机IP、源网段或站点名称。"},
            {"field": "destination", "question": "请指定需要保障业务的目的对象，例如服务器IP、目的网段或站点名称。"},
            {"field": "service", "question": "请说明希望保障的业务类型，例如科研视频、AI 数据或备份业务。"},
        ],
        "unresolved_fields": ["source_ip", "destination_ip", "service_or_protocol"],
    }, intent_spec={"original_text": "保障A点到B点的带宽大于10Mbps"}))
    visible = message["facts"]["user_facing_questions"]
    assert len(visible) == 3
    assert all(any("\u4e00" <= char <= "\u9fff" for char in question) for question in visible)
    assert "Please provide" not in "\n".join(visible)
    assert message["facts"]["raw_clarification_questions"] != visible


def test_chinese_objective_and_service_diagnostics_share_one_business_question():
    message = build_assistant_intent_message(record("NEEDS_CLARIFICATION", snapshot={
        "clarification_details": [
            {"field": "service", "question": "请明确端点"},
            {"field": "objective", "question": "该带宽保障是否关联某项已知业务？"},
        ],
    }, intent_spec={"original_text": "保障A点到B点的带宽大于10Mbps"}))
    assert message["facts"]["user_facing_questions"] == [
        "请说明希望保障的业务类型，例如科研视频、AI 数据或备份业务。"
    ]


def test_chinese_service_selector_mismatch_keeps_its_specific_question():
    question = "已识别业务类型，但它与当前显式源/目的端点不适用。请确认业务类型，或提供与该业务匹配的源和目的端点。"
    message = build_assistant_intent_message(record("NEEDS_CLARIFICATION", snapshot={
        "clarification_questions": [question],
        "clarification_details": [{
            "field": "service",
            "reason": "SERVICE_SELECTOR_MISMATCH",
            "question": question,
        }],
        "unresolved_fields": ["service"],
    }, intent_spec={"original_text": "保障科研视频最低带宽10Mbps"}))
    assert message["facts"]["user_facing_questions"] == [question]


def test_withdraw_and_execution_failure_have_different_wording():
    assert build_assistant_intent_message(record("WITHDRAWN"))["result_type"] == "INTENT_WITHDRAWN"
    assert build_assistant_intent_message(record("EXECUTION_FAILED"))["title"] == "意图下发失败"


def test_rollback_and_degraded_are_distinct_and_preserve_deterministic_facts():
    rollback = build_assistant_intent_message(record("ROLLED_BACK", "VERIFICATION_FAILED"))
    degraded = build_assistant_intent_message(record("DEGRADED", "STALE_EVIDENCE"))
    assert rollback["result_type"] == "INTENT_ROLLED_BACK"
    assert rollback["preserved_state"]["old_policy_preserved"] is True
    assert degraded["result_type"] == "INTENT_DEGRADED"
    assert degraded["reason_code"] == "STALE_EVIDENCE"


def test_historical_semantic_ambiguity_is_not_projected_as_execution_failure():
    original = record(
        "FAILED", "SEMANTIC_AMBIGUITY",
        intent_runtime={"lifecycle": "FAILED", "last_transition_reason": "SEMANTIC_AMBIGUITY", "failure_stage": "understanding"},
        snapshot={
            "current_stage": "understanding", "needs_clarification": True,
            "clarification_details": [{"field": "source"}, {"field": "destination"}],
            "unresolved_fields": ["source", "destination"],
        },
        result={},
    )
    message = build_assistant_intent_message(original)
    assert original["status"] == "FAILED"  # presentation never mutates history
    assert message["result_type"] == "HISTORICAL_SEMANTIC_INCOMPLETE"
    assert message["title"] != "\u610f\u56fe\u4e0b\u53d1\u5931\u8d25"
    assert "\u672a\u8fdb\u5165\u7f51\u7edc\u7b56\u7565\u4e0b\u53d1\u9636\u6bb5" in message["summary"]
    assert message["facts"]["execution_attempted"] is False
    assert message["facts"]["southbound_attempted"] is False
    assert message["facts"]["policy_created"] is False
    assert message["facts"]["binding_created"] is False


def test_failed_record_without_execution_evidence_never_claims_deployment_failure():
    message = build_assistant_intent_message(record(
        "FAILED", "PARSER_REJECTED",
        intent_runtime={"lifecycle": "FAILED", "last_transition_reason": "PARSER_REJECTED", "failure_stage": "parsing"},
    ))
    assert message["result_type"] == "HISTORICAL_PROCESSING_INCOMPLETE"
    assert message["facts"]["execution_attempted"] is False


def test_minimum_bandwidth_history_does_not_inherit_unrelated_priority_action():
    message = build_assistant_intent_message(record(
        "FAILED", "SEMANTIC_AMBIGUITY",
        text="Guarantee A to B bandwidth greater than 10Mbps",
        intent_runtime={"lifecycle": "FAILED", "last_transition_reason": "SEMANTIC_AMBIGUITY", "failure_stage": "understanding"},
        intent_spec={"original_text": "Guarantee A to B bandwidth greater than 10Mbps", "objectives": [{"type": "MIN_BANDWIDTH", "value": 10}]},
        snapshot={"current_stage": "understanding", "needs_clarification": True},
        result={},
    ))
    assert message["result_type"] == "HISTORICAL_SEMANTIC_INCOMPLETE"
    assert "SET_PRIORITY" not in str(message)

"""Deterministic, human-readable intent results for the Dashboard chat."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def _number(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return ""


def _objective_value(spec: dict[str, Any], kinds: set[str]) -> str:
    for item in spec.get("objectives") or []:
        if str(item.get("type") or "") in kinds:
            return _number(item.get("value"))
    return ""


def _capacity_admission(result: dict[str, Any]) -> dict[str, Any]:
    """Return the persisted admission decision, never a reconstructed estimate."""
    report = result.get("policy_conflict_report") or {}
    evidence = report.get("capacity_evidence") or {}
    decision = evidence.get("admission_decision") or report.get("admission_decision") or {}
    return decision if isinstance(decision, dict) else {}


def _failure_projection_facts(
    record: dict[str, Any], runtime: dict[str, Any], result: dict[str, Any], snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Classify a failure only from this Intent's persisted evidence.

    A historical ``FAILED`` lifecycle is an audit fact, not proof that a
    controller/P4/TC execution began.  The user-facing projection must inspect
    the failure stage and this Intent's own policy/execution evidence before it
    calls something a deployment failure.
    """
    reason_values = (
        runtime.get("reason_code"), runtime.get("last_transition_reason"),
        runtime.get("failure_reason"), result.get("reason_code"),
        result.get("terminal_reason"), snapshot.get("reason_code"),
        snapshot.get("error_code"), snapshot.get("error_message"),
    )
    reason_code = next((str(value).upper() for value in reason_values if str(value or "").strip()), "")
    stage_values = (
        runtime.get("failure_stage"), result.get("failure_stage"), result.get("stage"),
        snapshot.get("failure_stage"), snapshot.get("current_stage"),
    )
    stage = next((str(value).strip().lower() for value in stage_values if str(value or "").strip()), "")
    execution = record.get("execution") or result.get("execution_result") or {}
    if not isinstance(execution, dict):
        execution = {}
    receipts = execution.get("device_receipts") or execution.get("southbound_receipts") or []
    explicit_attempt = execution.get("execution_attempted")
    if explicit_attempt is None:
        explicit_attempt = result.get("execution_attempted")
    execution_attempted = str(runtime.get("lifecycle") or "").upper() == "EXECUTION_FAILED" or bool(explicit_attempt) or bool(
        execution.get("execution_id") or execution.get("started_at") or receipts
    )
    southbound_attempted = bool(
        execution.get("southbound_attempted") or execution.get("flow_receipts")
        or execution.get("p4_receipts") or execution.get("onos_receipts") or receipts
    )
    policy_created = bool(
        record.get("policies") or result.get("network_policy") or result.get("yang_policy")
        or execution.get("policy_id")
    )
    binding_created = bool(
        execution.get("resource_binding")
        or (execution.get("qos_enforcement") or {}).get("qos_binding")
    )
    semantic_reason = any(token in reason_code for token in (
        "SEMANTIC_AMBIGUITY", "NEEDS_CLARIFICATION", "MISSING_SEMANTIC", "MISSING_ENDPOINT",
    ))
    semantic_stage = stage in {"understanding", "understand", "parsing", "parse", "pending_clarification"}
    clarification_required = bool(
        snapshot.get("needs_clarification") or snapshot.get("clarification_questions")
        or snapshot.get("clarification_details")
    )
    return {
        "reason_code": reason_code,
        "failure_stage": stage or None,
        "clarification_required": clarification_required,
        "execution_attempted": execution_attempted,
        "southbound_attempted": southbound_attempted,
        "policy_created": policy_created,
        "binding_created": binding_created,
        "semantic_before_execution": (semantic_reason or clarification_required) and semantic_stage and not execution_attempted,
    }


def _is_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _slot_key(value: Any) -> str:
    field = str(value or "").strip().lower()
    if field in {"source", "source_ip", "src", "src_ip"}:
        return "source"
    if field in {"destination", "destination_ip", "dst", "dst_ip"}:
        return "destination"
    if field in {"service", "service_or_protocol", "protocol", "destination_port", "port", "objective"}:
        return "service"
    return field


_CHINESE_SLOT_QUESTIONS = {
    "source": "请指定需要保障业务的源对象，例如主机IP、源网段或站点名称。",
    "destination": "请指定需要保障业务的目的对象，例如服务器IP、目的网段或站点名称。",
    "service": "请说明希望保障的业务类型，例如科研视频、AI 数据或备份业务。",
}


def _user_facing_clarification_questions(
    raw_questions: list[Any], details: list[Any], unresolved: list[Any], *, locale_text: str,
) -> list[str]:
    """Build the chat contract from semantic slots, retaining raw diagnostics elsewhere."""
    detail_by_slot: dict[str, str] = {}
    detail_reason_by_slot: dict[str, str] = {}
    slots: list[str] = []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        slot = _slot_key(detail.get("field"))
        if slot and slot not in slots:
            slots.append(slot)
        question = str(detail.get("question") or "").strip()
        if question:
            detail_by_slot[slot] = question
        detail_reason_by_slot[slot] = str(detail.get("reason") or "").upper()
    for field in unresolved:
        slot = _slot_key(field)
        if slot and slot not in slots:
            slots.append(slot)

    if _is_chinese(locale_text):
        # The Chinese conversation has one stable business order, independent
        # of which worker or validator supplied the internal diagnostics.
        slots = [slot for slot in ("source", "destination", "service") if slot in slots]

    chinese = _is_chinese(locale_text)
    result: list[str] = []
    for slot in slots:
        question = detail_by_slot.get(slot, "")
        if chinese:
            # A Chinese conversation never exposes internal/schema fallbacks.
            # A semantic applicability conflict is already precise Chinese
            # guidance.  Do not collapse it into the generic service prompt,
            # which would re-ask a service that was recognized successfully.
            if detail_reason_by_slot.get(slot) != "SERVICE_SELECTOR_MISMATCH":
                question = _CHINESE_SLOT_QUESTIONS.get(slot, question if _is_chinese(question) else "")
        elif not question:
            question = next((str(item) for item in raw_questions if str(item).strip()), "")
        if question and question not in result:
            result.append(question)
    if not result:
        for item in raw_questions:
            question = item.get("question") if isinstance(item, dict) else str(item)
            question = str(question or "").strip()
            if question and (not chinese or _is_chinese(question)) and question not in result:
                result.append(question)
    return result


def build_assistant_intent_message(
    record: dict[str, Any], *, runtime: dict[str, Any] | None = None,
    spec: dict[str, Any] | None = None, decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project persisted facts into the single conversation-result contract.

    No model is invoked here.  Wording is selected deterministically from the
    authoritative lifecycle and retained evidence, so a chat response cannot
    alter policy, capacity, reason codes, or execution claims.
    """
    intent_id = str(record.get("intent_id") or "")
    runtime = runtime or record.get("intent_runtime") or {}
    spec = spec or record.get("intent_spec") or {}
    lifecycle = str(runtime.get("lifecycle") or record.get("status") or "").upper()
    snapshot = record.get("snapshot") or {}
    result = record.get("result") or {}
    reason = str(runtime.get("last_transition_reason") or snapshot.get("error_message") or result.get("terminal_reason") or "")
    # Candidate calibration is a recovery phase: preserve the initial
    # RUNTIME_CAPACITY_UNVERIFIED admission fact in evidence, but speak the
    # terminal calibration result in the user-facing conversation outcome.
    conflict = result.get("policy_conflict_report") or result.get("conflict_report") or {}
    final_admission_reason = str(conflict.get("final_reason_code") or "").strip()
    if lifecycle == "UNSATISFIABLE" and final_admission_reason:
        reason = final_admission_reason
    text = str(spec.get("original_text") or record.get("text") or "该网络意图")
    evidence_ref = f"/dashboard/evidence/{intent_id}" if intent_id else "/dashboard/evidence"
    base = {
        "message_id": f"assistant-result:{intent_id}:{lifecycle}:{reason}",
        "intent_id": intent_id, "lifecycle": lifecycle, "reason_code": reason or None,
        "facts": {"user_request": text}, "preserved_state": {},
        "suggestions": [], "evidence_ref": evidence_ref,
    }
    if lifecycle == "SATISFIED":
        return {**base, "result_type": "INTENT_SATISFIED", "severity": "success",
                "title": "意图已生效", "summary": "系统已完成该意图，并已记录当前验证结果。",
                "suggestions": ["可查看详细证据，了解计划、执行与验证记录。"]}
    if lifecycle == "NEEDS_CLARIFICATION":
        questions = snapshot.get("clarification_questions") or spec.get("clarification_questions") or []
        raw_questions = [item.get("question") if isinstance(item, dict) else str(item) for item in questions]
        details = snapshot.get("clarification_details") or spec.get("clarification_details") or []
        unresolved = snapshot.get("unresolved_fields") or spec.get("unresolved_fields") or []
        normalized = _user_facing_clarification_questions(
            raw_questions, details, unresolved, locale_text=text,
        )
        # A message identity denotes an authoritative conversational turn, not
        # merely a lifecycle.  Polling unchanged facts stays idempotent, while
        # a new question/reason/fact revision necessarily produces a new turn.
        question_fingerprint = hashlib.sha256(
            json.dumps({
                "lifecycle": lifecycle,
                "result_type": "INTENT_NEEDS_CLARIFICATION",
                "reason_code": reason or None,
                "questions": normalized,
                "details": details,
                "unresolved": unresolved,
                "spec_revision": (spec or {}).get("revision"),
            }, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        return {**base, "result_type": "INTENT_NEEDS_CLARIFICATION", "severity": "info",
                "message_id": f"assistant-result:{intent_id}:NEEDS_CLARIFICATION:{question_fingerprint}",
                "title": "需要补充信息", "summary": "我还需要你补充以下信息后才能继续处理该意图。",
                "facts": {**base["facts"], "user_facing_questions": normalized,
                          "clarification_questions": normalized,
                          "raw_clarification_questions": [x for x in raw_questions if x],
                          "clarification_details": details, "unresolved_fields": unresolved},
                "suggestions": ["请直接在当前对话中回复补充信息。"]}
    if lifecycle == "WITHDRAWN":
        return {**base, "result_type": "INTENT_WITHDRAWN", "severity": "info",
                "title": "意图已撤销", "summary": "该意图已按正式生命周期撤销；审计历史仍被保留。"}
    if lifecycle in {"ROLLED_BACK", "ROLLBACK_COMPLETED", "UPDATE_ROLLED_BACK"}:
        return {**base, "result_type": "INTENT_ROLLED_BACK", "severity": "warning",
                "title": "更新未能完成，已恢复原策略",
                "summary": "本次更新没有生效，系统已恢复到更新前已提交的网络策略。",
                "preserved_state": {"old_policy_preserved": True, "rollback_completed": True},
                "suggestions": ["可查看详细证据，了解回滚原因和已恢复的策略版本。"]}
    if lifecycle in {"UPDATED", "UPDATE_COMMITTED"}:
        return {**base, "result_type": "INTENT_UPDATED", "severity": "success",
                "title": "意图更新已生效", "summary": "系统已提交并验证该意图的最新策略版本。",
                "suggestions": ["可查看详细证据，了解策略版本和验证记录。"]}
    if lifecycle in {"UNSATISFIABLE", "NEEDS_APPROVAL"}:
        current = _objective_value(spec, {"MIN_BANDWIDTH", "RATE_LIMIT"})
        admission = _capacity_admission(result)
        admission_reason = str(admission.get("reason_code") or "").upper()
        is_capacity = (
            any(token in reason.upper() for token in ("CAPACITY", "INSUFFICIENT", "ADMISSION"))
            or "CAPACITY" in admission_reason
            or "INSUFFICIENT" in admission_reason
        )
        facts = {**base["facts"], "reason": reason or "约束无法同时满足"}
        if current:
            facts["requested_value_mbps"] = current
        if is_capacity:
            capacity = {
                key: admission.get(key)
                for key in (
                    "operation", "requested_mbps", "requested_target_mbps",
                    "existing_target_mbps", "requested_delta_mbps", "available_mbps",
                    "contract_capacity_mbps", "contract_allocated_mbps",
                    "current_guaranteed_allocation_mbps", "contract_available_mbps",
                    "runtime_capacity_status",
                    "runtime_schedulable_mbps", "runtime_schedulable_total_mbps",
                    "runtime_current_allocation_mbps", "runtime_available_mbps",
                    "effective_available_mbps", "schedulable_capacity_mbps",
                    "allocated_mbps", "safety_margin_mbps",
                )
                if key in admission
            }
            if capacity:
                facts["capacity_admission"] = capacity
            # A candidate-calibration terminal outcome supersedes the
            # admission gate that initiated the recovery attempt.
            primary_reason = (
                reason if reason.startswith("CALIBRATION_")
                else admission_reason or reason
            )
            operation = str(admission.get("operation") or admission.get("purpose") or "").upper()
            existing = _number(admission.get("existing_target_mbps"))
            requested = _number(admission.get("requested_target_mbps") or capacity.get("requested_mbps")) or current
            delta = _number(capacity.get("requested_delta_mbps"))
            effective = _number(capacity.get("effective_available_mbps"))
            contract_available = _number(capacity.get("contract_available_mbps"))
            runtime_status = str(admission.get("runtime_capacity_status") or "").upper()
            summary = "当前网络的可安全调度容量不足，因此系统没有执行本次更新。"
            if operation == "INCREASE_UPDATE" and existing and requested and delta:
                summary = (
                    f"你希望将该业务的最低带宽从 {existing} Mbps 提高到 {requested} Mbps，"
                    f"本次需额外申请 {delta} Mbps 的保证带宽。"
                )
                if effective:
                    summary += f" 当前可用于新增保证的有效容量为 {effective} Mbps，未执行本次更新。"
                elif contract_available and runtime_status in {"UNVERIFIED", "STALE", "MISSING"}:
                    summary += (
                        f" 合同阶段可用容量为 {contract_available} Mbps，但缺少当前运行时容量验证，"
                        "因此系统没有执行本次更新。"
                    )
                elif contract_available:
                    summary += f" 当前可用于新增保证的合同容量为 {contract_available} Mbps，未执行本次更新。"
            elif requested and effective:
                summary = f"当前请求新增 {requested} Mbps，但可安全调度的有效容量为 {effective} Mbps；系统没有执行本次更新。"
            return {**base,
                    "message_id": f"assistant-result:{intent_id}:{lifecycle}:{primary_reason}",
                    "reason_code": primary_reason, "result_type": "INTENT_REJECTED", "severity": "warning",
                    "title": "当前意图未能生效",
                    "summary": summary,
                    "facts": {**facts, "reason": primary_reason, "secondary_reason_code": reason or None},
                    "preserved_state": {"old_policy_preserved": True, "southbound_mutations": 0},
                    "suggestions": ["可降低目标带宽，或等待网络资源恢复后重新提交。"]}
        return {**base, "result_type": "INTENT_UNSATISFIABLE", "severity": "warning",
                "title": "当前意图无法满足", "summary": "该意图的约束无法同时成立，系统未执行网络配置变更。",
                "facts": facts, "preserved_state": {"southbound_mutations": 0}}
    if lifecycle in {"EXECUTION_FAILED", "FAILED"}:
        failure = _failure_projection_facts(record, runtime, result, snapshot)
        questions = snapshot.get("clarification_questions") or spec.get("clarification_questions") or []
        raw_questions = [item.get("question") if isinstance(item, dict) else str(item) for item in questions]
        details = snapshot.get("clarification_details") or spec.get("clarification_details") or []
        unresolved = snapshot.get("unresolved_fields") or spec.get("unresolved_fields") or []
        normalized_questions = _user_facing_clarification_questions(raw_questions, details, unresolved, locale_text=text)
        facts = {**base["facts"], **failure,
                 "reason": failure["reason_code"] or reason or "FAILED",
                 "user_facing_questions": normalized_questions,
                 "clarification_questions": normalized_questions,
                 "raw_clarification_questions": [item for item in raw_questions if item],
                 "clarification_details": details, "unresolved_fields": unresolved}
        if failure["semantic_before_execution"]:
            return {**base, "result_type": "HISTORICAL_SEMANTIC_INCOMPLETE", "severity": "warning",
                    "reason_code": failure["reason_code"] or reason or None,
                    "title": "\u5386\u53f2\u5904\u7406\u672a\u5b8c\u6210",
                    "summary": "\u8be5\u610f\u56fe\u5f53\u65f6\u56e0\u5fc5\u8981\u8bed\u4e49\u4fe1\u606f\u4e0d\u8db3\u800c\u7ec8\u6b62\uff0c\u672a\u8fdb\u5165\u7f51\u7edc\u7b56\u7565\u4e0b\u53d1\u9636\u6bb5\u3002",
                    "facts": facts,
                    "preserved_state": {"historical_lifecycle_preserved": True, "southbound_mutations": 0},
                    "suggestions": ["\u53ef\u67e5\u770b\u5f53\u65f6\u7684\u8bed\u4e49\u6f84\u6e05\u8bb0\u5f55\uff0c\u4e86\u89e3\u7f3a\u5931\u4fe1\u606f\u3002"]}
        if not failure["execution_attempted"]:
            return {**base, "result_type": "HISTORICAL_PROCESSING_INCOMPLETE", "severity": "warning",
                    "reason_code": failure["reason_code"] or reason or "FAILED",
                    "title": "\u5386\u53f2\u5904\u7406\u672a\u5b8c\u6210",
                    "summary": "\u8be5\u5386\u53f2\u610f\u56fe\u672a\u5b8c\u6210\uff0c\u4e14\u6ca1\u6709\u8bc1\u636e\u8868\u660e\u5df2\u8fdb\u5165\u7f51\u7edc\u7b56\u7565\u4e0b\u53d1\u9636\u6bb5\u3002",
                    "facts": facts,
                    "preserved_state": {"historical_lifecycle_preserved": True, "southbound_mutations": 0}}
    if lifecycle in {"EXECUTION_FAILED", "FAILED"}:
        return {**base, "result_type": "INTENT_EXECUTION_FAILED", "severity": "error",
                "title": "意图下发失败", "summary": "系统在执行阶段未能完成该意图；请查看证据中的具体执行原因。",
                "facts": {**base["facts"], "reason": reason or "EXECUTION_FAILED"}}
    if lifecycle in {"ASSURANCE_FAILED", "DEGRADED"}:
        return {**base, "result_type": "INTENT_DEGRADED", "severity": "warning",
                "title": "策略仍在生效，等待重新验证", "summary": "当前没有新的有效违约结论；系统正在等待可信的重新验证结果。"}
    return {**base, "result_type": "INTENT_UNVERIFIED", "severity": "info",
            "title": "意图正在处理中", "summary": "系统正在理解、规划或验证该意图，请稍候。"}

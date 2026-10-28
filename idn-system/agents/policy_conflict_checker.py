"""
agents/policy_conflict_checker.py
==================================

策略冲突检测 Agent —— 编译前的安全闸门。

# 系统角色
在策略编译前检查新 YANGPolicy 与网络中存量策略的兼容性，
生成 ConflictReport（含 approval_token 或 rejection_reason）。

# 三层校验
1. 逻辑冲突：新策略的匹配条件（五元组）是否与现有规则重叠/矛盾
2. 资源冲突：带宽等资源需求是否超出物理链路剩余容量
3. 拓扑兼容：IP 段/端口是否在拓扑中存在对应主机的活跃链路

# 主要依赖
- models/agent_state.py：ConflictReport
- models/network.py：NetworkSnapshot
- models/yang_policy.py：YANGPolicy, ActionType, Protocol

# 设计意图
- 冲突检测发生在策略编译之前，避免浪费 LLM 编译成本
- 无冲突时签发 approval_token（UUID 令牌），作为下游阶段的通行证
- severity 继承自 P4 规则验证的分级模式：HIGH > MEDIUM > LOW
"""
from __future__ import annotations

import logging
import os
import uuid
from ipaddress import ip_network
from typing import Any, Optional

from models.agent_state import ConsensusReasonCode, ConflictReport
from models.network import NetworkSnapshot
from models.yang_policy import ActionType, NetworkPolicy, Protocol, YANGPolicy
from models.policy_update import qos_change, traffic_scope_key
from agents.base_agent import BaseAgent
from api.capacity_evidence import (
    CapacityEvaluationPurpose,
    aggregate_capacity_evidence,
    authoritative_admission_decision,
    configured_capacity_mbps,
    primary_direction_pairs,
    schedulable_capacity_mbps,
)
from infra.qos_resource_allocator import QoSResourceAllocator
from infra.resource_registry import get_resource_registry

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


class PolicyConflictCheckerAgent(BaseAgent):
    """
    Check a parsed YANG policy draft against existing policies and resources.

    The report focuses on four MVP conflict classes:
    - ACTION_CONFLICT: overlapping traffic selectors have incompatible actions.
    - RANGE_OVERLAP: policy scopes overlap and may duplicate or shadow intent.
    - PRIORITY_CONFLICT: overlapping rules have equal or suspicious priority.
    - QOS_CONFLICT: overlapping QoS rules disagree on DSCP/bandwidth/burst.
    - LOW_CONFIDENCE_INTENT: parser already marked the intent as unsafe.
    """

    agent_id = "policy_conflict_checker"
    description = "Detect policy conflicts before P4Runtime rule generation"

    def intrinsic_qos_interval_conflict(
        self, policies: list[YANGPolicy],
    ) -> dict[str, Any] | None:
        """Find an infeasible QoS interval within one incoming IntentPlan.

        This check is intentionally independent of the live resource ledger.
        A plan that asks for ``bandwidth >= 30`` and ``bandwidth <= 10`` for
        the same canonical traffic scope is unsatisfiable even on an otherwise
        empty, unlimited network.  It therefore has to run before any
        resource-dependent admission query can mask the actual semantic
        rejection reason.
        """
        scopes: dict[str, dict[str, Any]] = {}
        for draft in policies:
            policy = draft.network_policy
            qos = policy.action.qos_params
            if qos is None or qos.bandwidth is None:
                continue
            # The action is the canonical operation semantics.  QoSParams
            # carries the same mode in normal modeled policies, but legacy
            # callers can omit it or retain its model default.
            if policy.action.action_type == ActionType.SET_PRIORITY:
                mode = "MIN_BANDWIDTH"
            elif policy.action.action_type == ActionType.RATE_LIMIT:
                mode = "RATE_LIMIT"
            else:
                mode = str(getattr(getattr(qos, "qos_mode", None), "value", getattr(qos, "qos_mode", ""))).upper()
            if mode not in {"MIN_BANDWIDTH", "RATE_LIMIT"}:
                continue
            scope_key = traffic_scope_key(policy).key
            entry = scopes.setdefault(scope_key, {
                "scope_key": scope_key,
                "selector": policy.match.model_dump(mode="json"),
                "minimums": [],
                "maximums": [],
            })
            objective = {
                "policy_id": policy.policy_id,
                "policy_name": policy.policy_name,
                "qos_mode": mode,
                "bandwidth_mbps": float(qos.bandwidth),
            }
            if mode == "MIN_BANDWIDTH":
                entry["minimums"].append(objective)
            else:
                entry["maximums"].append(objective)

        for entry in scopes.values():
            if not entry["minimums"] or not entry["maximums"]:
                continue
            minimum = max(item["bandwidth_mbps"] for item in entry["minimums"])
            maximum = min(item["bandwidth_mbps"] for item in entry["maximums"])
            if minimum <= maximum:
                continue
            explanation = (
                "QoS objectives for the same canonical selector have an empty "
                f"feasible interval: bandwidth >= {minimum:g} Mbps and "
                f"bandwidth <= {maximum:g} Mbps."
            )
            return {
                "type": "INFEASIBLE_INTERVAL",
                "conflict_type": "INFEASIBLE_INTERVAL",
                "severity": "HIGH",
                "description": explanation,
                "explanation": explanation,
                "suggestion": "Submit compatible minimum-bandwidth and rate-limit objectives for this traffic scope.",
                "traffic_scope_key": entry["scope_key"],
                "selector": entry["selector"],
                "minimum_mbps": minimum,
                "maximum_mbps": maximum,
                "feasible_interval": "EMPTY",
                "minimum_objectives": entry["minimums"],
                "maximum_objectives": entry["maximums"],
            }
        return None

    async def check(
        self,
        yang_draft: YANGPolicy,
        network_snapshot: NetworkSnapshot,
        candidate_path_link_ids: Optional[list[str]] = None,
        required_device_ids: Optional[list[str]] = None,
    ) -> ConflictReport:
        logger.info(
            "[%s] checking policy conflicts: policy=%s",
            self.agent_id,
            yang_draft.network_policy.policy_name,
        )
        draft_scope_key = traffic_scope_key(yang_draft.network_policy).key

        parser_confidence_item = self._check_parser_confidence(yang_draft)
        if parser_confidence_item and parser_confidence_item.get("severity") == "HIGH":
            return ConflictReport(
                # ``has_conflict`` remains the legacy broad "blocking check"
                # signal for existing integrations.  ``conflict_detected`` is
                # the authoritative *policy-vs-policy* fact exposed to users.
                has_conflict=True,
                conflict_detected=False,
                blocking=True,
                stop_category=ConsensusReasonCode.SEMANTIC_VALIDATION_FAILED,
                requires_manual_review=True,
                conflict_type="LOW_CONFIDENCE_INTENT",
                severity="HIGH",
                explanation=parser_confidence_item["explanation"],
                suggestions=list(parser_confidence_item.get("suggestions") or []),
                conflict_items=[parser_confidence_item],
                rejection_reason=parser_confidence_item["explanation"],
                traffic_scope_key=draft_scope_key,
                final_reason_code="LOW_CONFIDENCE_INTENT",
                final_decision="REJECTED",
                southbound_execution="NOT_EXECUTED",
            )

        exact_qos_decision = self._find_exact_qos_decision(
            yang_draft.network_policy,
            network_snapshot.existing_policies,
        )
        decision_fields = {
            key: exact_qos_decision.get(key)
            for key in (
                "resolution", "execution_mode", "existing_policy_id", "traffic_scope_key",
                "old_qos", "new_qos", "changed_fields", "old_bandwidth_mbps",
                "new_bandwidth_mbps", "change_direction", "mode_change",
                "released_bandwidth_mbps",
            )
        } if exact_qos_decision else {"traffic_scope_key": draft_scope_key}
        capacity_evidence: dict[str, Any] = {}
        qos = yang_draft.network_policy.action.qos_params
        if (
            yang_draft.network_policy.action.action_type == ActionType.SET_PRIORITY
            and qos
            and qos.bandwidth
        ):
            reserved = self._reserved_bandwidth_for_overlapping_policies(
                yang_draft.network_policy,
                network_snapshot.existing_policies,
            )
            capacity_evidence = aggregate_capacity_evidence(
                network_snapshot.links,
                requested_mbps=int(qos.bandwidth),
                reserved_mbps=reserved,
                headroom_mbps=max(int(os.getenv("IDN_CAPACITY_HEADROOM_MBPS", "1")), 0),
                candidate_link_ids=candidate_path_link_ids,
                default_capacity_mbps=int(os.getenv("IDN_DEFAULT_LINK_CAPACITY_MBPS", "100")),
                evidence_time=network_snapshot.timestamp.isoformat(),
            )
        conflict_items: list[dict[str, Any]] = []
        if parser_confidence_item:
            conflict_items.append(parser_confidence_item)
        conflict_items.extend(self._check_existing_policy_conflicts(yang_draft, network_snapshot))
        if not exact_qos_decision or exact_qos_decision["execution_mode"] != "VERIFY_EXISTING":
            reclaim_mbps = 0.0
            old_qos = (exact_qos_decision or {}).get("old_qos") or {}
            if (
                yang_draft.network_policy.action.action_type == ActionType.SET_PRIORITY
                and str(old_qos.get("qos_mode") or "").upper() == "MIN_BANDWIDTH"
            ):
                reclaim_mbps = float(old_qos.get("bandwidth") or 0.0)
            resource_conflicts = self._check_resource_conflicts(
                yang_draft,
                network_snapshot,
                candidate_path_link_ids=candidate_path_link_ids,
                reclaim_mbps=reclaim_mbps,
                capacity_purpose=self._capacity_evaluation_purpose(exact_qos_decision),
            )
            conflict_items.extend(resource_conflicts)
            admission = next((item.get("admission_decision") for item in resource_conflicts if item.get("admission_decision")), None)
            if admission is not None:
                capacity_evidence["admission_decision"] = admission
        conflict_items.extend(
            self._check_topology_compatibility(
                network_snapshot,
                required_device_ids=(
                    required_device_ids
                    if required_device_ids is not None
                    else self._required_endpoint_devices(yang_draft, network_snapshot)
                ),
            )
        )
        conflict_items = self._dedupe_conflicts(conflict_items)

        blocking_conflicts = [
            item for item in conflict_items if SEVERITY_RANK.get(item.get("severity", "LOW"), 0) >= 2
        ]
        warning_items = [
            item for item in conflict_items if SEVERITY_RANK.get(item.get("severity", "LOW"), 0) < 2
        ]

        if blocking_conflicts:
            primary = self._primary_conflict(blocking_conflicts)
            explanation = self._summarize_conflicts(blocking_conflicts)
            suggestions = self._collect_suggestions(blocking_conflicts)
            stop_category = self._blocking_stop_category(blocking_conflicts, primary)
            is_true_policy_conflict = (
                stop_category == ConsensusReasonCode.TRUE_POLICY_CONFLICT
            )
            admission = capacity_evidence.get("admission_decision") or {}
            initial_admission_reason = admission.get("reason_code")
            blocking_decision_fields = {
                key: value
                for key, value in decision_fields.items()
                if key not in {"resolution", "execution_mode"}
            }
            blocking_decision_fields["traffic_scope_key"] = (
                blocking_decision_fields.get("traffic_scope_key") or draft_scope_key
            )
            blocking_decision_fields["existing_policy_id"] = (
                blocking_decision_fields.get("existing_policy_id")
                or primary.get("existing_policy_id")
            )
            logger.warning(
                "[%s] found %s conflicts: %s",
                self.agent_id,
                len(blocking_conflicts),
                explanation,
            )
            return ConflictReport(
                # Keep the legacy blocking flag stable for integrations that
                # still consume ``has_conflict``.  The explicit
                # ``conflict_detected`` field is the authoritative
                # policy-vs-policy fact and remains false for capacity,
                # resource, and semantic stops.
                has_conflict=True,
                conflict_detected=is_true_policy_conflict,
                blocking=True,
                stop_category=stop_category,
                conflict_type=primary.get("conflict_type"),
                severity=primary.get("severity"),
                explanation=explanation,
                suggestions=suggestions,
                warnings=[item.get("explanation", "") for item in warning_items if item.get("explanation")],
                warning_items=warning_items,
                conflict_items=blocking_conflicts,
                capacity_evidence=capacity_evidence,
                admission_decision=admission,
                rejection_reason=explanation,
                initial_admission_reason=initial_admission_reason,
                final_reason_code=(
                    str(initial_admission_reason)
                    if stop_category == ConsensusReasonCode.CAPACITY_ADMISSION_REJECTED
                    and initial_admission_reason
                    else str(primary.get("conflict_type") or "") or None
                ),
                final_decision=(
                    "UNSATISFIABLE"
                    if stop_category == ConsensusReasonCode.CAPACITY_ADMISSION_REJECTED
                    else "REJECTED"
                ),
                southbound_execution="NOT_EXECUTED",
                resolution=(
                    "BLOCK_ADMISSION"
                    if stop_category == ConsensusReasonCode.CAPACITY_ADMISSION_REJECTED
                    else "BLOCK_CONFLICT" if exact_qos_decision else None
                ),
                # Legacy ``execution_mode`` describes the candidate's plan
                # disposition, not an execution receipt.  Preserve it for
                # compatibility; the explicit ``southbound_execution`` above
                # is the authoritative fact that capacity rejection caused no
                # controller/P4/TC write.
                execution_mode="BLOCK_POLICY" if exact_qos_decision else "CREATE",
                **blocking_decision_fields,
            )

        token = str(uuid.uuid4())
        logger.info("[%s] conflict check passed, approval_token=%s...", self.agent_id, token[:8])
        if exact_qos_decision:
            warning_items.insert(0, exact_qos_decision)
        warnings = [item.get("explanation", "") for item in warning_items if item.get("explanation")]
        context_refresh_required = any(
            bool(item.get("context_refresh_required")) for item in warning_items
        )
        return ConflictReport(
            has_conflict=False,
            conflict_detected=False,
            blocking=False,
            requires_manual_review=False,
            conflict_type=(exact_qos_decision or {}).get("conflict_type"),
            severity=(exact_qos_decision or {}).get("severity"),
            explanation=(
                exact_qos_decision.get("explanation")
                if exact_qos_decision
                else "未发现阻断性策略冲突，可以进入策略生成阶段。"
                if warning_items
                else "未发现策略冲突，可以进入策略生成阶段。"
            ),
            suggestions=(
                ["继续生成 P4Runtime 流表计划；低风险范围重叠可在人工审计中关注。"]
                + self._collect_suggestions(warning_items)
                if warning_items
                else ["继续生成 P4Runtime 流表计划。"]
            ),
            warnings=warnings,
            warning_items=warning_items,
            conflict_items=[],
            capacity_evidence=capacity_evidence,
            admission_decision=capacity_evidence.get("admission_decision") or {},
            approval_token=token,
            context_refresh_required=context_refresh_required,
            **decision_fields,
        )

    @staticmethod
    def _blocking_stop_category(
        blocking_items: list[dict[str, Any]],
        primary: dict[str, Any],
    ) -> ConsensusReasonCode:
        """Classify a blocking check without overloading policy conflict.

        The checker still returns one ``ConflictReport`` for compatibility with
        the workflow gate.  Its category is authoritative: only a conflict
        against a persisted existing policy can be ``TRUE_POLICY_CONFLICT``.
        """
        types = {
            str(item.get("conflict_type") or item.get("type") or "").upper()
            for item in blocking_items
        }
        if any(item.get("admission_decision") for item in blocking_items) or types & {
            "INSUFFICIENT_CAPACITY", "RESOURCE_CONFLICT",
        }:
            return ConsensusReasonCode.CAPACITY_ADMISSION_REJECTED
        if types & {"RESOURCE_CHECK_SCOPE_ERROR", "PATH_NOT_FOUND", "TOPOLOGY_INCONSISTENT"}:
            return ConsensusReasonCode.RESOURCE_UNRESOLVED
        if types & {
            "LOW_CONFIDENCE_INTENT", "INFEASIBLE_INTERVAL", "OBJECTIVE_CONFLICT",
            "SEMANTIC_CONFLICT", "QOS_SEMANTIC_CONFLICT",
        }:
            return ConsensusReasonCode.SEMANTIC_VALIDATION_FAILED
        if any(item.get("existing_policy_id") for item in blocking_items):
            return ConsensusReasonCode.TRUE_POLICY_CONFLICT
        if str(primary.get("conflict_type") or "").upper() in {
            "INFRASTRUCTURE_UNAVAILABLE", "NETWORK_CONTEXT_UNKNOWN", "NETWORK_CONTEXT_STALE",
        }:
            return ConsensusReasonCode.RESOURCE_UNRESOLVED
        # A non-admission blocking item without an existing policy is not a
        # policy conflict.  Keep the category honest and fail closed.
        return ConsensusReasonCode.SEMANTIC_VALIDATION_FAILED

    def _find_exact_qos_decision(
        self,
        new_policy: NetworkPolicy,
        existing_policies: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """Return the idempotent/upsert decision for an exact QoS scope.

        This is intentionally evaluated before generic overlap logic.  A QoS
        value change on the same selector is desired-state replacement, not a
        conflicting second reservation.
        """
        qos_actions = {ActionType.SET_PRIORITY.value, ActionType.RATE_LIMIT.value}
        new_action = new_policy.action.action_type.value
        if new_action not in qos_actions:
            return None
        new_match = new_policy.match.model_dump()
        scope = traffic_scope_key(new_policy).key
        for existing in existing_policies:
            if not self._match_equivalent(new_match, self._existing_match(existing)):
                continue
            if self._existing_action_type(existing) not in qos_actions:
                continue
            change = qos_change(existing, new_policy)
            duplicate = not change["changed_fields"]
            return {
                "type": "DUPLICATE_POLICY" if duplicate else "QOS_PARAMETER_CHANGE",
                "conflict_type": "DUPLICATE_POLICY" if duplicate else "QOS_PARAMETER_CHANGE",
                "severity": "LOW",
                "blocking": False,
                "requires_manual_review": False,
                "resolution": "REUSE_EXISTING_POLICY" if duplicate else "UPDATE_EXISTING_POLICY",
                "execution_mode": "VERIFY_EXISTING" if duplicate else "MODIFY_QOS_RESOURCE",
                "existing_policy_id": self._existing_policy_id(existing),
                "traffic_scope_key": scope,
                "explanation": (
                    "同一流量范围和 QoS 参数已存在；复用现有策略并验证实际配置，不重复下发。"
                    if duplicate
                    else (
                        "检测到同一流量范围的 QoS 期望值变化；按覆盖语义原地更新，"
                        f"方向={change['change_direction']}，无需人工审批。"
                    )
                ),
                "suggestion": "继续执行实际配置读回。" if duplicate else "原子更新 QoS 资源并保留版本审计与回滚信息。",
                **change,
            }
        return None

    def _check_parser_confidence(self, yang_draft: YANGPolicy) -> Optional[dict[str, Any]]:
        if yang_draft.confidence_score >= 0.7:
            return None

        missing_slots = self._missing_required_slots(yang_draft)
        if missing_slots:
            explanation = (
                f"意图解析置信度较低：{yang_draft.confidence_score}，"
                f"且关键槽位缺失：{', '.join(missing_slots)}，"
                "需要先完成澄清，不应进入策略生成阶段。"
            )
            return {
                "type": "LOW_CONFIDENCE_INTENT",
                "conflict_type": "LOW_CONFIDENCE_INTENT",
                "severity": "HIGH",
                "description": explanation,
                "explanation": explanation,
                "suggestion": "请根据 parse_warnings 补充带宽、源/目的对象或优化目标。",
                "suggestions": ["请先完成意图澄清，再进入冲突检测和策略生成。"],
                "parse_warnings": list(yang_draft.parse_warnings or []),
                "missing_slots": missing_slots,
            }

        explanation = (
            f"意图解析置信度为 {yang_draft.confidence_score}，低于常规阈值，"
            "但源/目的/动作等关键槽位已完整；低置信度来自 LLM 与规则解析器差异，"
            "系统已采用规则解析器和 Schema Validator 的确定性结果继续执行。"
        )
        return {
            "type": "LOW_CONFIDENCE_INTENT",
            "conflict_type": "LOW_CONFIDENCE_INTENT",
            "severity": "LOW",
            "description": explanation,
            "explanation": explanation,
            "suggestion": "继续执行，并在决策报告中保留 LLM/规则差异作为审计证据。",
            "suggestions": ["继续生成 P4Runtime 流表计划；保留低置信度原因供审计。"],
            "parse_warnings": list(yang_draft.parse_warnings or []),
        }

    def _missing_required_slots(self, yang_draft: YANGPolicy) -> list[str]:
        policy = yang_draft.network_policy
        action_type = policy.action.action_type
        match = policy.match
        qos = policy.action.qos_params
        missing: list[str] = []

        if action_type in {
            ActionType.SET_PRIORITY,
            ActionType.RATE_LIMIT,
            ActionType.DROP,
            ActionType.FORWARD,
        }:
            if not match.source_ip:
                missing.append("source_ip")
            if not match.destination_ip:
                missing.append("destination_ip")

        if action_type == ActionType.SET_PRIORITY:
            if match.protocol == Protocol.ANY and match.destination_port is None:
                missing.append("service_or_protocol")

        if action_type == ActionType.RATE_LIMIT:
            if not qos or qos.bandwidth is None:
                missing.append("bandwidth")

        return missing

    def _check_existing_policy_conflicts(
        self,
        yang_draft: YANGPolicy,
        snapshot: NetworkSnapshot,
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        new_policy = yang_draft.network_policy

        for existing in snapshot.existing_policies:
            if not self._matches_overlap(new_policy.match.model_dump(), self._existing_match(existing)):
                continue

            existing_action = self._existing_action_type(existing)
            qos_actions = {ActionType.SET_PRIORITY.value, ActionType.RATE_LIMIT.value}
            if (
                self._match_equivalent(new_policy.match.model_dump(), self._existing_match(existing))
                and existing_action in qos_actions
                and new_policy.action.action_type.value in qos_actions
            ):
                # Exact-scope QoS reuse/update is handled by
                # _find_exact_qos_decision and must not fall through to the
                # generic duplicate/QOS_CONFLICT blockers below.
                continue
            existing_priority = self._existing_priority(existing)
            new_priority = new_policy.metadata.priority

            if self._duplicate_policy(new_policy, existing, existing_action, existing_priority):
                conflicts.append(
                    self._make_conflict(
                        "DUPLICATE_POLICY",
                        "MEDIUM",
                        "新策略与已有策略完全重复。",
                        "建议复用已有策略或跳过重复下发。",
                        existing,
                    )
                )
                continue

            action_conflict = self._action_conflict(new_policy.action.action_type, existing_action)
            if action_conflict:
                explanation, suggestion = self._action_conflict_guidance(
                    new_policy.action.action_type,
                    existing_action,
                )
                conflicts.append(
                    self._make_conflict(
                        "ACTION_CONFLICT",
                        "HIGH" if ActionType.DROP.value in {new_policy.action.action_type.value, existing_action} else "MEDIUM",
                        explanation,
                        suggestion,
                        existing,
                    )
                )

            semantic_conflict = self._qos_action_semantic_conflict(
                new_policy.action.action_type,
                existing_action,
            )
            if semantic_conflict:
                conflicts.append(
                    self._make_conflict(
                        "QOS_SEMANTIC_CONFLICT",
                        "MEDIUM",
                        (
                            "同一流量同时存在保障类策略和限速类策略："
                            f"新策略={new_policy.action.action_type.value}，已有策略={existing_action}，"
                            "业务目标可能互相冲突。"
                        ),
                        "请确认该业务应保障带宽还是限制速率；必要时要求人工审批或拆分匹配范围。",
                        existing,
                    )
                )

            if self._is_partial_overlap(new_policy.match.model_dump(), self._existing_match(existing)):
                conflicts.append(
                    self._make_conflict(
                        "RANGE_OVERLAP",
                        "LOW",
                        (
                            "新策略与已有策略的源/目的网段、协议或端口范围存在部分重叠，"
                            "但当前未发现阻断性动作或 QoS 冲突，可作为低风险审计提示。"
                        ),
                        "请考虑收窄匹配范围、合并重复策略，或为两条策略设置清晰的优先级。",
                        existing,
                    )
                )

            priority_conflict = self._priority_conflict_message(
                new_policy,
                existing,
                new_priority,
                existing_priority,
                action_conflict,
            )
            if priority_conflict:
                conflicts.append(
                    self._make_conflict(
                        "PRIORITY_CONFLICT",
                        priority_conflict["severity"],
                        priority_conflict["explanation"],
                        priority_conflict["suggestion"],
                        existing,
                    )
                )

            qos_conflict = self._qos_conflict_message(new_policy, existing)
            if qos_conflict:
                conflicts.append(
                    self._make_conflict(
                        "QOS_CONFLICT",
                        qos_conflict["severity"],
                        qos_conflict["explanation"],
                        qos_conflict["suggestion"],
                        existing,
                    )
                )

        return conflicts

    @staticmethod
    def _capacity_evaluation_purpose(
        exact_qos_decision: dict[str, Any] | None,
    ) -> CapacityEvaluationPurpose:
        """Classify a QoS revision before invoking the capacity authority.

        A lower minimum-bandwidth revision releases a committed reservation;
        it is not a new admission and must not be rejected merely because the
        latest runtime-capacity evidence is degraded.  Increases retain the
        stricter runtime health gate.  This is selector/scope based, not tied
        to a named service.
        """
        decision = exact_qos_decision or {}
        if str(decision.get("execution_mode") or "").upper() != "MODIFY_QOS_RESOURCE":
            return CapacityEvaluationPurpose.NEW_ADMISSION
        direction = str(decision.get("change_direction") or "").upper()
        if direction in {"DECREASE", "DOWNGRADE"}:
            return CapacityEvaluationPurpose.DECREASE_UPDATE
        return CapacityEvaluationPurpose.INCREASE_UPDATE

    def _check_resource_conflicts(
        self,
        yang_draft: YANGPolicy,
        snapshot: NetworkSnapshot,
        candidate_path_link_ids: Optional[list[str]] = None,
        reclaim_mbps: float = 0.0,
        capacity_purpose: CapacityEvaluationPurpose = CapacityEvaluationPurpose.NEW_ADMISSION,
    ) -> list[dict[str, Any]]:
        if yang_draft.network_policy.action.action_type != ActionType.SET_PRIORITY:
            return []

        qos = yang_draft.network_policy.action.qos_params
        if not qos or not qos.bandwidth:
            return []

        conflicts: list[dict[str, Any]] = []
        reserved = self._reserved_bandwidth_for_overlapping_policies(
            yang_draft.network_policy,
            snapshot.existing_policies,
        )
        requested = qos.bandwidth
        profile_resource = self._profile_qos_resource(yang_draft)
        if profile_resource is not None:
            resource_id, src_device, dst_device = profile_resource
            # A preference-only policy revision may intentionally leave its
            # bandwidth field empty while its unique active ResourceBinding
            # remains the durable owner of the guaranteed class.  The named
            # service-profile path must use the same safe, exact-scope reclaim
            # as the generic path below; otherwise a 10 -> 30 revision is
            # misclassified as a brand-new 30 Mbps admission.
            if capacity_purpose == CapacityEvaluationPurpose.INCREASE_UPDATE:
                reclaim_mbps = max(
                    float(reclaim_mbps or 0.0),
                    self._active_binding_reclaim_for_bottleneck(
                        traffic_scope_key(yang_draft.network_policy).key,
                        resource_id,
                        src_device,
                        dst_device,
                    ),
                )
            admission = authoritative_admission_decision(
                link_id=resource_id,
                src_device=src_device,
                dst_device=dst_device,
                requested_mbps=float(requested),
                reclaim_mbps=reclaim_mbps,
                purpose=capacity_purpose,
            )
            if admission["decision"] != "ADMITTED":
                explanation = (
                    f"Guaranteed bandwidth admission rejected: {admission['reason_code']}; "
                    f"requested={requested}Mbps, available={admission['available_mbps']}Mbps."
                )
                return [{
                    "type": "INSUFFICIENT_CAPACITY",
                    "conflict_type": "INSUFFICIENT_CAPACITY",
                    "severity": "HIGH",
                    "description": explanation,
                    "explanation": explanation,
                    "suggestion": "Release an active reservation or submit a request no greater than the verified schedulable capacity.",
                    "link_id": resource_id,
                    "admission_decision": admission,
                    "requested_bandwidth": requested,
                    "resource_check_scope": "service_profile_qos_binding",
                }]
            # The profile's typed QoS binding was resolved from the canonical
            # selector before this safety gate.  Its ledger decision is the
            # authoritative resource decision; the generic snapshot loop below
            # remains the fallback for unprofiled policies.
            return []
        links, resource_check_scope, missing_link_ids = self._links_for_resource_check(
            snapshot,
            candidate_path_link_ids,
        )
        if missing_link_ids:
            missing_text = ", ".join(missing_link_ids)
            explanation = (
                f"候选路径包含当前网络快照中不存在的链路：{missing_text}，"
                "无法可靠完成资源容量检查。"
            )
            return [
                {
                    "type": "RESOURCE_CHECK_SCOPE_ERROR",
                    "conflict_type": "RESOURCE_CHECK_SCOPE_ERROR",
                    "severity": "HIGH",
                    "description": explanation,
                    "explanation": explanation,
                    "suggestion": "请重新运行 PathPlannerAgent 或刷新 NetworkSnapshot 后再进行冲突检测。",
                    "missing_link_ids": missing_link_ids,
                    "resource_check_scope": resource_check_scope,
                }
            ]

        default_capacity = int(os.getenv("IDN_DEFAULT_LINK_CAPACITY_MBPS", "100"))
        headroom = max(int(os.getenv("IDN_CAPACITY_HEADROOM_MBPS", "1")), 0)
        # Guaranteed admission is made once, at the narrowest physical
        # resource in the selected path.  It is a hard gate: an absent stable
        # record is not permission to fall back to the 100 Mbps link rate.
        # This applies to any resolved path and does not depend on service or
        # host names.
        ranked = []
        for link in links:
            physical, _ = configured_capacity_mbps(
                link.src_device, link.dst_device, link.bandwidth_mbps,
                default_capacity=default_capacity,
            )
            ranked.append((physical, str(link.link_id), link))
        if ranked:
            _physical, _link_id, bottleneck = min(ranked, key=lambda item: (item[0], item[1]))
            # The durable policy document can lag a successfully allocated
            # binding after an interrupted revision.  For an exact-scope
            # replacement, the binding is still the current resource owner:
            # reclaim its guarantee only when it is on this same authoritative
            # bottleneck.  This prevents both double-accounting an in-place
            # revision and incorrectly reclaiming capacity from another path.
            reclaim_mbps = max(
                float(reclaim_mbps or 0.0),
                self._active_binding_reclaim_for_bottleneck(
                    traffic_scope_key(yang_draft.network_policy).key,
                    bottleneck.link_id,
                    bottleneck.src_device,
                    bottleneck.dst_device,
                ),
            )
            admission = authoritative_admission_decision(
                link_id=bottleneck.link_id,
                src_device=bottleneck.src_device,
                dst_device=bottleneck.dst_device,
                requested_mbps=float(requested),
                reclaim_mbps=reclaim_mbps,
                purpose=capacity_purpose,
            )
            if admission["decision"] != "ADMITTED":
                explanation = (
                    f"Guaranteed bandwidth admission rejected: {admission['reason_code']}; "
                    f"requested={requested}Mbps, available={admission['available_mbps']}Mbps."
                )
                return [{
                    "type": "INSUFFICIENT_CAPACITY",
                    "conflict_type": "INSUFFICIENT_CAPACITY",
                    "severity": "HIGH",
                    "description": explanation,
                    "explanation": explanation,
                    "suggestion": "Release an active reservation or submit a request no greater than the verified schedulable capacity.",
                    "link_id": bottleneck.link_id,
                    "admission_decision": admission,
                    "requested_bandwidth": requested,
                    "resource_check_scope": resource_check_scope,
                }]
        for link in links:
            # ONOS /links does not carry bandwidth.  The emulated fabric has a
            # configured 100 Mbps ceiling, so use that explicit deployment
            # capacity instead of silently skipping the safety check.
            capacity, capacity_source = configured_capacity_mbps(
                link.src_device,
                link.dst_device,
                link.bandwidth_mbps,
                default_capacity=default_capacity,
            )
            capacity, schedulable_source, capacity_record_id = schedulable_capacity_mbps(
                link.link_id, physical_capacity_mbps=capacity,
                src_device=link.src_device, dst_device=link.dst_device,
            )
            if schedulable_source == "stable_dataplane_capacity":
                capacity_source = schedulable_source
            available = capacity - reserved - headroom
            if requested > available:
                conflicts.append(
                    {
                        "type": "RESOURCE_CONFLICT",
                        "conflict_type": "RESOURCE_CONFLICT",
                        "severity": "HIGH",
                        "description": (
                            f"链路 {link.link_id} 带宽不足：请求 {requested}Mbps，"
                            f"已有预留约 {reserved}Mbps，安全余量 {headroom}Mbps，"
                            f"可用约 {max(available, 0)}Mbps。"
                        ),
                        "explanation": (
                            f"链路 {link.link_id} 带宽不足：请求 {requested}Mbps，"
                            f"已有预留约 {reserved}Mbps，安全余量 {headroom}Mbps，"
                            f"可用约 {max(available, 0)}Mbps。"
                        ),
                        "suggestion": "请降低带宽需求、释放已有预留，或选择更高容量路径后再下发。",
                        "link_id": link.link_id,
                        "requested_bandwidth": requested,
                        "link_capacity": capacity,
                        "capacity_source": capacity_source,
                        "capacity_record_id": capacity_record_id,
                        "reserved_bandwidth": reserved,
                        "capacity_headroom": headroom,
                        "available_bandwidth": max(available, 0),
                        "resource_check_scope": resource_check_scope,
                        "blocking_device": link.src_device,
                        "blocking_interface": str(link.src_port),
                        "src_device": link.src_device,
                        "src_port": str(link.src_port),
                        "dst_device": link.dst_device,
                        "dst_port": str(link.dst_port),
                    }
                )
        return conflicts

    @staticmethod
    def _profile_qos_resource(yang_draft: YANGPolicy) -> tuple[str, str, str] | None:
        """Resolve the exact scope's authoritative QoS resource.

        A profile may declare a shared bottleneck for a whole service family.
        When it does not, an already-active exact selector has a more precise
        durable authority: its ResourceBinding.  Falling back to a generic
        primary path in that case conflates unrelated scopes and can discard a
        fresh capacity record attached to the actual egress resource.
        """
        service_id = str(getattr(yang_draft, "service_id", "") or "")
        if not service_id:
            return None
        try:
            from api.service_profiles import get_service_profile

            profile = get_service_profile(service_id)
            configured = dict(profile.qos_binding or {})
            alias = str(configured.get("bottleneck_link") or "")
            registry = get_resource_registry()
            if alias:
                resource_id = registry.resolve(alias)
            else:
                # This is deliberately exact-scope only.  It cannot borrow a
                # binding from another host pair merely because both use the
                # same service name.
                binding = QoSResourceAllocator().get_active_for_scope(
                    traffic_scope_key(yang_draft.network_policy).key
                )
                if binding is None:
                    return None
                resource_id = str(binding.resource_key)
            snapshot = registry.snapshot()
            link = next(item for item in snapshot.links if item.resource_id == resource_id)
            interface_devices = {
                item.resource_id: item.device_id for item in snapshot.interfaces
            }
            src_device = interface_devices.get(link.src_interface_id)
            dst_device = interface_devices.get(link.dst_interface_id)
            if not src_device or not dst_device:
                return None
            return resource_id, str(src_device), str(dst_device)
        except (KeyError, StopIteration, ValueError):
            return None

    @staticmethod
    def _active_binding_reclaim_for_bottleneck(
        scope_key: str,
        bottleneck_link_id: str,
        src_device: str | None = None,
        dst_device: str | None = None,
    ) -> float:
        """Return an in-place replacement's current guarantee, if safe.

        A binding survives a policy document revision and is the source of
        truth for its resource ownership.  We never use it as a broad capacity
        credit: the binding must be active, guaranteed, and resolve to exactly
        the bottleneck receiving this admission request.
        """
        try:
            binding = QoSResourceAllocator().get_active_for_scope(scope_key)
            if binding is None or str(binding.reservation_kind).upper() != "GUARANTEED":
                return 0.0
            registry = get_resource_registry()
            try:
                resource_id = registry.resolve(str(bottleneck_link_id))
            except KeyError:
                # ONOS link identifiers include runtime port annotations (for
                # example ``link-device:...-[eth3](3)-to-...``) and are not
                # stable registry aliases.  The topology's two endpoint
                # devices are the durable identity for an undirected resource.
                endpoints = {str(src_device), str(dst_device)} - {"", "None"}
                snapshot = registry.snapshot()
                interface_devices = {
                    item.resource_id: item.device_id for item in snapshot.interfaces
                }
                resource_id = next(
                    item.resource_id
                    for item in snapshot.links
                    if {
                        str(interface_devices.get(item.src_interface_id)),
                        str(interface_devices.get(item.dst_interface_id)),
                    } == endpoints
                )
            if str(binding.resource_key) != str(resource_id):
                return 0.0
            return max(0.0, float(binding.rate_mbps))
        except Exception as exc:
            logger.info(
                "Unable to establish same-scope binding reclaim for scope=%s link=%s: %s",
                scope_key, bottleneck_link_id, exc,
            )
            return 0.0

    def _links_for_resource_check(
        self,
        snapshot: NetworkSnapshot,
        candidate_path_link_ids: Optional[list[str]],
    ) -> tuple[list[Any], str, list[str]]:
        if candidate_path_link_ids is None:
            primary_directions = primary_direction_pairs()
            current_path_links = [
                link for link in snapshot.links
                if (str(link.src_device), str(link.dst_device)) in primary_directions
            ]
            if current_path_links:
                return current_path_links, "configured_primary_runtime_directions", []
            return list(snapshot.links), "all_snapshot_links", []

        candidate_ids = {
            str(link_id).strip()
            for link_id in candidate_path_link_ids
            if link_id not in (None, "", "null")
        }
        snapshot_link_ids = {str(link.link_id) for link in snapshot.links}
        missing_link_ids = sorted(candidate_ids - snapshot_link_ids)
        return (
            [link for link in snapshot.links if str(link.link_id) in candidate_ids],
            "candidate_path",
            missing_link_ids,
        )

    def _check_topology_compatibility(
        self,
        snapshot: NetworkSnapshot,
        *,
        required_device_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Classify only explicit runtime evidence for devices this plan needs.

        UNKNOWN and STALE are evidence-quality states, never aliases for an
        offline device.  The node owning refresh will retry once before making
        either state a recoverable deployment pause.
        """
        scoped = [
            device for device in snapshot.devices
            if device.device_id in set(required_device_ids)
        ]
        unavailable = [
            device.device_id for device in scoped if device.status == "UNAVAILABLE"
        ]
        if unavailable:
            return [{
                "type": "INFRASTRUCTURE_UNAVAILABLE",
                "conflict_type": "INFRASTRUCTURE_UNAVAILABLE",
                "severity": "HIGH",
                "description": f"以下计划依赖设备当前不可用，Intent 暂未部署：{', '.join(unavailable)}。",
                "explanation": f"以下计划依赖设备当前不可用，Intent 暂未部署：{', '.join(unavailable)}。",
                "suggestion": "等待设备与控制器恢复后，刷新网络上下文并继续当前 Intent。",
                "unavailable_devices": unavailable,
                "required_device_ids": required_device_ids,
                "retryable": True,
            }]
        unknown = [device.device_id for device in scoped if device.status in {"UNKNOWN", "STALE"}]
        if not unknown:
            return []
        return [{
            "type": "NETWORK_CONTEXT_STALE" if any(device.status == "STALE" for device in scoped) else "NETWORK_CONTEXT_UNKNOWN",
            "conflict_type": "NETWORK_CONTEXT_STALE" if any(device.status == "STALE" for device in scoped) else "NETWORK_CONTEXT_UNKNOWN",
            "severity": "LOW",
            "description": f"无法确认以下计划依赖设备的实时可用状态，正在刷新控制器与 P4Runtime 证据：{', '.join(unknown)}。",
            "explanation": f"无法确认以下计划依赖设备的实时可用状态，正在刷新控制器与 P4Runtime 证据：{', '.join(unknown)}。",
            "suggestion": "刷新 NetworkContext 并重新读取 ONOS/P4Runtime 证据。",
            "unknown_devices": unknown,
            "required_device_ids": required_device_ids,
            "context_refresh_required": True,
            "retryable": True,
        }]

    def _required_endpoint_devices(
        self,
        policy: YANGPolicy,
        snapshot: NetworkSnapshot,
    ) -> list[str]:
        """Find the endpoint-attached devices for a policy without inventing a path."""
        match = policy.network_policy.match
        selectors = {str(match.source_ip or ""), str(match.destination_ip or "")}
        required: list[str] = []
        for host in snapshot.hosts:
            if host.location_device and any(
                self._ip_overlaps(selector, address)
                for selector in selectors
                for address in host.ip_addresses
                if selector and address
            ):
                if host.location_device not in required:
                    required.append(host.location_device)
        return required

    def _make_conflict(
        self,
        conflict_type: str,
        severity: str,
        explanation: str,
        suggestion: str,
        existing: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "type": conflict_type,
            "conflict_type": conflict_type,
            "severity": severity,
            "description": explanation,
            "explanation": explanation,
            "suggestion": suggestion,
            "existing_policy_id": self._existing_policy_id(existing),
            "existing_policy_name": existing.get("policy_name"),
        }

    def _matches_overlap(self, new_match: dict[str, Any], existing_match: dict[str, Any]) -> bool:
        return all(
            [
                self._ip_overlaps(new_match.get("source_ip"), existing_match.get("source_ip")),
                self._ip_overlaps(new_match.get("destination_ip"), existing_match.get("destination_ip")),
                self._protocol_overlaps(new_match.get("protocol"), existing_match.get("protocol")),
                self._port_overlaps(new_match.get("source_port"), existing_match.get("source_port")),
                self._port_overlaps(new_match.get("destination_port"), existing_match.get("destination_port")),
                self._port_overlaps(new_match.get("vlan_id"), existing_match.get("vlan_id")),
            ]
        )

    def _is_partial_overlap(self, new_match: dict[str, Any], existing_match: dict[str, Any]) -> bool:
        if not self._matches_overlap(new_match, existing_match):
            return False
        return not self._match_equivalent(new_match, existing_match)

    def _match_equivalent(self, left: dict[str, Any], right: dict[str, Any]) -> bool:
        keys = ["source_ip", "destination_ip", "protocol", "source_port", "destination_port", "vlan_id"]
        return all(self._normalize_match_value(key, left.get(key)) == self._normalize_match_value(key, right.get(key)) for key in keys)

    def _duplicate_policy(
        self,
        new_policy: NetworkPolicy,
        existing: dict[str, Any],
        existing_action: Optional[str],
        existing_priority: int,
    ) -> bool:
        return all(
            [
                self._match_equivalent(new_policy.match.model_dump(), self._existing_match(existing)),
                new_policy.action.action_type.value == existing_action,
                self._qos_equivalent(new_policy, existing),
                new_policy.metadata.priority == existing_priority,
            ]
        )

    def _qos_equivalent(self, new_policy: NetworkPolicy, existing: dict[str, Any]) -> bool:
        new_qos = new_policy.action.qos_params
        existing_qos = self._existing_qos(existing)
        if not new_qos and not existing_qos:
            return True
        if bool(new_qos) != bool(existing_qos):
            return False
        return all(
            self._to_int(getattr(new_qos, field)) == self._to_int(existing_qos.get(field))
            for field in ("dscp_value", "bandwidth", "burst_size")
        )

    def _match_covers(self, broader: dict[str, Any], narrower: dict[str, Any]) -> bool:
        return all(
            [
                self._ip_covers(broader.get("source_ip"), narrower.get("source_ip")),
                self._ip_covers(broader.get("destination_ip"), narrower.get("destination_ip")),
                self._field_covers(broader.get("protocol"), narrower.get("protocol"), wildcard=Protocol.ANY.value),
                self._field_covers(broader.get("source_port"), narrower.get("source_port")),
                self._field_covers(broader.get("destination_port"), narrower.get("destination_port")),
                self._field_covers(broader.get("vlan_id"), narrower.get("vlan_id")),
            ]
        )

    def _action_conflict(self, new_action: ActionType, existing_action: Optional[str]) -> bool:
        if not existing_action:
            return False
        if new_action.value == existing_action:
            return False
        if ActionType.DROP.value in {new_action.value, existing_action}:
            return True
        qos_actions = {ActionType.SET_PRIORITY.value, ActionType.RATE_LIMIT.value}
        if new_action.value in qos_actions and existing_action in qos_actions:
            return False
        if ActionType.MIRROR.value in {new_action.value, existing_action}:
            return False
        return False

    def _action_conflict_guidance(
        self,
        new_action: ActionType,
        existing_action: Optional[str],
    ) -> tuple[str, str]:
        if ActionType.MIRROR.value in {new_action.value, existing_action} and ActionType.DROP.value in {
            new_action.value,
            existing_action,
        }:
            return (
                "新策略与已有策略匹配到重叠流量，且存在 MIRROR 与 DROP 的组合；"
                "是否能先镜像再丢弃取决于 P4 流水线中镜像动作发生的位置。"
                f"新策略={new_action.value}，已有策略={existing_action}。",
                "请确认 P4 流水线中镜像与丢弃的执行顺序；必要时要求人工审批或拆分策略。",
            )
        return (
            "新策略与已有策略匹配到重叠流量，但动作不一致："
            f"新策略={new_action.value}，已有策略={existing_action}。",
            "请人工确认应保留哪一个动作；如果新策略优先，应显式覆盖旧策略或调整优先级。",
        )

    def _qos_action_semantic_conflict(
        self,
        new_action: ActionType,
        existing_action: Optional[str],
    ) -> bool:
        return {new_action.value, existing_action} == {
            ActionType.SET_PRIORITY.value,
            ActionType.RATE_LIMIT.value,
        }

    def _priority_conflict_message(
        self,
        new_policy: NetworkPolicy,
        existing: dict[str, Any],
        new_priority: int,
        existing_priority: int,
        has_action_conflict: bool,
    ) -> Optional[dict[str, str]]:
        existing_match = self._existing_match(existing)
        new_match = new_policy.match.model_dump()
        if has_action_conflict and new_priority == existing_priority:
            return {
                "severity": "HIGH",
                "explanation": (
                    f"新策略与已有策略使用相同优先级 {new_priority}，但动作不同，"
                    "设备匹配结果可能依赖下发表项顺序。"
                ),
                "suggestion": "请为更具体或更重要的策略设置更高优先级，避免同优先级动作冲突。",
            }
        if has_action_conflict and new_priority < existing_priority and self._match_covers(new_match, existing_match):
            return {
                "severity": "MEDIUM",
                "explanation": "新策略优先级更高且范围覆盖已有策略，可能遮蔽已有策略。",
                "suggestion": "请确认这是有意覆盖；否则降低新策略优先级或收窄匹配范围。",
            }
        if has_action_conflict and existing_priority < new_priority and self._match_covers(existing_match, new_match):
            return {
                "severity": "MEDIUM",
                "explanation": "已有策略优先级更高且范围覆盖新策略，新策略可能无法生效。",
                "suggestion": "请提高新策略优先级，或修改已有策略范围后再下发。",
            }
        return None

    def _qos_conflict_message(
        self,
        new_policy: NetworkPolicy,
        existing: dict[str, Any],
    ) -> Optional[dict[str, str]]:
        new_qos = new_policy.action.qos_params
        existing_qos = self._existing_qos(existing)
        if not new_qos or not existing_qos:
            return None

        differences: list[str] = []
        for field in ("dscp_value", "bandwidth", "burst_size"):
            new_value = getattr(new_qos, field)
            existing_value = existing_qos.get(field)
            new_int = self._to_int(new_value)
            existing_int = self._to_int(existing_value)
            if new_int is not None and existing_int is not None and new_int != existing_int:
                differences.append(f"{field}: 新策略={new_value}, 已有策略={existing_value}")

        if not differences:
            return None

        return {
            "severity": "MEDIUM",
            "explanation": "新策略与已有 QoS 策略匹配范围重叠，但 QoS 参数不同：" + "；".join(differences) + "。",
            "suggestion": "请确认业务优先级需求；可合并 QoS 参数、调整优先级，或要求人工审批后覆盖旧策略。",
        }

    def _ip_overlaps(self, left: Any, right: Any) -> bool:
        if left in (None, "", "null") or right in (None, "", "null"):
            return True
        left_net = self._to_network(left)
        right_net = self._to_network(right)
        if not left_net or not right_net:
            return str(left) == str(right)
        return left_net.overlaps(right_net)

    def _ip_covers(self, broader: Any, narrower: Any) -> bool:
        if broader in (None, "", "null"):
            return True
        if narrower in (None, "", "null"):
            return broader in (None, "", "null")
        broader_net = self._to_network(broader)
        narrower_net = self._to_network(narrower)
        if not broader_net or not narrower_net:
            return str(broader) == str(narrower)
        return narrower_net.subnet_of(broader_net)

    def _to_network(self, value: Any):
        text = str(value).strip()
        if "/" not in text:
            text = f"{text}/32"
        try:
            return ip_network(text, strict=False)
        except ValueError:
            return None

    def _protocol_overlaps(self, left: Any, right: Any) -> bool:
        return self._field_covers(left, right, wildcard=Protocol.ANY.value) or self._field_covers(
            right, left, wildcard=Protocol.ANY.value
        )

    def _port_overlaps(self, left: Any, right: Any) -> bool:
        return left in (None, "", "null") or right in (None, "", "null") or self._value_text(left) == self._value_text(right)

    def _field_covers(self, broader: Any, narrower: Any, wildcard: Optional[str] = None) -> bool:
        if broader in (None, "", "null"):
            return True
        broader_text = self._value_text(broader).upper()
        if wildcard is not None and broader_text == wildcard:
            return True
        if narrower in (None, "", "null"):
            return broader in (None, "", "null") or (wildcard is not None and broader_text == wildcard)
        return broader_text == self._value_text(narrower).upper()

    def _normalize_match_value(self, key: str, value: Any) -> Any:
        if value in (None, "", "null"):
            return None
        if key in {"source_ip", "destination_ip"}:
            network = self._to_network(value)
            return str(network) if network else str(value)
        if key == "protocol":
            return self._value_text(value).upper()
        if key in {"source_port", "destination_port", "vlan_id"}:
            return self._to_int(value)
        return value

    def _existing_match(self, existing: dict[str, Any]) -> dict[str, Any]:
        return dict(existing.get("match") or {})

    def _existing_action_type(self, existing: dict[str, Any]) -> Optional[str]:
        action = existing.get("action") or {}
        action_type = action.get("action_type") or existing.get("action_type")
        if isinstance(action_type, ActionType):
            return action_type.value
        return str(action_type).upper() if action_type not in (None, "", "null") else None

    def _existing_qos(self, existing: dict[str, Any]) -> dict[str, Any]:
        action = existing.get("action") or {}
        return dict(action.get("qos_params") or existing.get("qos_params") or {})

    def _existing_priority(self, existing: dict[str, Any]) -> int:
        metadata = existing.get("metadata") or {}
        try:
            return int(metadata.get("priority", existing.get("priority", 1000)))
        except (TypeError, ValueError):
            return 1000

    def _existing_policy_id(self, existing: dict[str, Any]) -> str:
        return str(existing.get("policy_id") or existing.get("id") or existing.get("policy_name") or "unknown")

    def _value_text(self, value: Any) -> str:
        if hasattr(value, "value"):
            return str(value.value)
        return str(value)

    def _to_int(self, value: Any) -> Optional[int]:
        if value in (None, "", "null"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _reserved_bandwidth_for_overlapping_policies(
        self,
        new_policy: NetworkPolicy,
        existing_policies: list[dict[str, Any]],
    ) -> int:
        total = 0
        new_match = new_policy.match.model_dump()
        for policy in existing_policies:
            action_type = self._existing_action_type(policy)
            if action_type != ActionType.SET_PRIORITY.value:
                continue
            if not self._matches_overlap(new_match, self._existing_match(policy)):
                continue
            if self._match_equivalent(new_match, self._existing_match(policy)):
                # This is the resource being replaced.  Counting it again
                # would turn 10→30 Mbps into a fictitious 40 Mbps request.
                continue
            qos = self._existing_qos(policy)
            try:
                total += int(qos.get("bandwidth") or 0)
            except (TypeError, ValueError):
                continue
        return total

    def _primary_conflict(self, conflicts: list[dict[str, Any]]) -> dict[str, Any]:
        return sorted(
            conflicts,
            key=lambda item: SEVERITY_RANK.get(item.get("severity", "LOW"), 0),
            reverse=True,
        )[0]

    def _summarize_conflicts(self, conflicts: list[dict[str, Any]]) -> str:
        primary = self._primary_conflict(conflicts)
        high = sum(1 for item in conflicts if item.get("severity") == "HIGH")
        medium = sum(1 for item in conflicts if item.get("severity") == "MEDIUM")
        low = sum(1 for item in conflicts if item.get("severity") == "LOW")
        parts = []
        if high:
            parts.append(f"{high} 个高危冲突")
        if medium:
            parts.append(f"{medium} 个中等冲突")
        if low:
            parts.append(f"{low} 个低风险重叠")
        return f"冲突检测未通过：发现 {'，'.join(parts)}。主要问题：{primary.get('explanation')}"

    def _collect_suggestions(self, conflicts: list[dict[str, Any]]) -> list[str]:
        seen: set[str] = set()
        suggestions: list[str] = []
        for conflict in sorted(
            conflicts,
            key=lambda item: SEVERITY_RANK.get(item.get("severity", "LOW"), 0),
            reverse=True,
        ):
            suggestion = str(conflict.get("suggestion") or "").strip()
            if suggestion and suggestion not in seen:
                suggestions.append(suggestion)
                seen.add(suggestion)
        return suggestions

    def _dedupe_conflicts(self, conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, ...]] = set()
        output: list[dict[str, Any]] = []
        for conflict in conflicts:
            key = (
                conflict.get("conflict_type"),
                conflict.get("severity"),
                conflict.get("existing_policy_id"),
                conflict.get("link_id"),
                conflict.get("explanation"),
            )
            if key in seen:
                continue
            seen.add(key)
            output.append(conflict)
        return output

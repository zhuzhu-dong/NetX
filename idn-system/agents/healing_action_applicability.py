"""Deterministic applicability and expected-effect boundary for healing actions.

The LLM and root-cause agents may propose high-level actions.  This module owns
the network-truth decision about whether an action is useful *now*.  It does not
write devices and it does not modify topology.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from models.intent_domain import (
    CandidatePlan,
    HealingAction,
    HealingActionApplicability,
    HealingActionEvaluation,
    HealingActionType,
    HealingExpectedEffect,
    HealingRejectionReason,
    IntentPlanIR,
    IntentSpec,
    NetworkContext,
    ObjectiveType,
    RootCauseAnalysis,
    RootCauseCategory,
)


def canonical_edge(left: str, right: str) -> str:
    """Return an orientation-independent edge identifier."""

    return "--".join(sorted((str(left), str(right))))


def path_edges(path: Iterable[str]) -> list[str]:
    nodes = [str(item) for item in path]
    return [canonical_edge(left, right) for left, right in zip(nodes, nodes[1:])]


def _edge_aliases(left: str, right: str) -> set[str]:
    pairs = ((left, right), (right, left))
    aliases: set[str] = set()
    for first, second in pairs:
        aliases.update(
            {
                f"{first}--{second}",
                f"link:{first}--{second}",
                f"{first}-{second}",
                f"link:{first}-{second}",
                f"{first}->{second}",
            }
        )
    return {item.lower() for item in aliases}


def path_contains_link(path: Iterable[str], link_id: str) -> bool:
    wanted = str(link_id or "").strip().lower()
    if not wanted:
        return False
    nodes = [str(item) for item in path]
    return any(
        wanted in _edge_aliases(left, right)
        for left, right in zip(nodes, nodes[1:])
    )


def context_signature(
    context: NetworkContext,
    *,
    current_path: list[str],
    bottleneck_link_ids: list[str],
) -> str:
    """Hash only material runtime state, excluding capture time and nonce."""

    if context.material_state_hash:
        payload = {
            "material_state_hash": context.material_state_hash,
            "current_path": list(current_path),
            "bottleneck_link_ids": sorted(str(item) for item in bottleneck_link_ids),
        }
    else:
        payload = {
            "current_path": list(current_path),
            "bottleneck_link_ids": sorted(str(item) for item in bottleneck_link_ids),
            "capacities": context.capacities,
            "utilization": context.utilization,
            "reservations": context.reservations,
            "active_intents": context.active_intents,
            "congestion": context.congestion,
            "int_observations": context.int_observations,
            "tc_state": context.tc_state,
            "p4_state": context.p4_state,
            "queue_state": context.queue_state,
            "kpi": context.kpi,
        }
    material = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(str(material).encode("utf-8")).hexdigest()


def action_fingerprint(
    action: HealingAction,
    *,
    root_cause: str,
    bottleneck_link_ids: list[str],
) -> str:
    relevant_params = {
        key: value
        for key, value in action.params.items()
        if key
        not in {
            "evidence_refs",
            "context_id",
            "captured_at",
            "context_signature",
        }
    }
    payload = {
        "action_type": action.type.value,
        "target": action.target,
        "parameters": relevant_params,
        "root_cause": root_cause,
        "bottleneck_link_ids": sorted(str(item) for item in bottleneck_link_ids),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PathRootCauseImpactEvaluator:
    """Compare real edge deltas and determine whether a reroute removes cause."""

    @staticmethod
    def evaluate(
        current_path: list[str],
        candidate_path: list[str],
        bottleneck_link_ids: list[str],
    ) -> dict[str, Any]:
        old_edges = path_edges(current_path)
        new_edges = path_edges(candidate_path)
        old_set, new_set = set(old_edges), set(new_edges)
        affected = [
            link_id
            for link_id in bottleneck_link_ids
            if path_contains_link(candidate_path, link_id)
        ]
        current_affected = [
            link_id
            for link_id in bottleneck_link_ids
            if path_contains_link(current_path, link_id)
        ]
        return {
            "old_edges": old_edges,
            "new_edges": new_edges,
            "changed_edges": sorted(old_set ^ new_set),
            "shared_edges": sorted(old_set & new_set),
            "current_path_contains_bottleneck": bool(current_affected),
            "candidate_path_contains_bottleneck": bool(affected),
            "affected_bottleneck_link_ids": affected,
            "bottleneck_avoided": bool(current_affected) and not affected,
            "path_changed": list(current_path) != list(candidate_path),
        }


class HealingActionApplicabilityValidator:
    """Reject redundant, ineffective, unsafe, or repeated healing actions."""

    def validate(
        self,
        action: HealingAction,
        *,
        context: NetworkContext,
        current_plan: IntentPlanIR | None,
        current_path: list[str],
        root_cause: RootCauseAnalysis | dict[str, Any],
        bottleneck_link_ids: list[str],
        active_intents: list[dict[str, Any]],
        previously_attempted_actions: list[dict[str, Any]],
    ) -> HealingActionEvaluation:
        del current_plan  # reserved for policy/resource diff extensions
        category = self._root_value(root_cause, "category", RootCauseCategory.UNKNOWN.value)
        signature = context_signature(
            context,
            current_path=current_path,
            bottleneck_link_ids=bottleneck_link_ids,
        )
        fingerprint = action_fingerprint(
            action,
            root_cause=str(category),
            bottleneck_link_ids=bottleneck_link_ids,
        )
        candidate_id = str(action.params.get("candidate_id") or action.action_id)
        evidence_refs = list(
            dict.fromkeys(
                [
                    *context.evidence_refs,
                    *[str(item) for item in action.params.get("evidence_refs") or []],
                ]
            )
        )

        if action.type == HealingActionType.REROUTE:
            target_path = [str(item) for item in action.params.get("target_path") or []]
            impact = PathRootCauseImpactEvaluator.evaluate(
                current_path, target_path, bottleneck_link_ids
            )
            expected_available = self._number(
                action.params.get("expected_available_mbps")
            )
            current_mbps = self._number(action.params.get("current_measurement_mbps"))
            required_mbps = self._number(action.params.get("effective_min_mbps"))
            gain = (
                expected_available - current_mbps
                if expected_available is not None and current_mbps is not None
                else None
            )
            effect = HealingExpectedEffect(
                bottleneck_avoided=impact["bottleneck_avoided"],
                expected_available_mbps=expected_available,
                expected_gain_mbps=gain,
                affected_intents=[
                    str(item) for item in action.affected_intent_ids
                ],
                confidence=action.confidence,
                evidence_refs=evidence_refs,
            )
            if not target_path:
                return self._reject(
                    action,
                    candidate_id,
                    HealingActionApplicability.NOT_APPLICABLE,
                    HealingRejectionReason.NO_FEASIBLE_HEALING_ACTION,
                    "reroute candidate has no target path",
                    effect,
                    evidence_refs,
                    fingerprint,
                    signature,
                )
            if list(current_path) == target_path:
                return self._reject(
                    action,
                    candidate_id,
                    HealingActionApplicability.REDUNDANT,
                    HealingRejectionReason.TARGET_PATH_ALREADY_ACTIVE,
                    "target path is already the active path",
                    effect,
                    evidence_refs,
                    fingerprint,
                    signature,
                )
            if bottleneck_link_ids and impact["candidate_path_contains_bottleneck"]:
                scope = self._root_value(root_cause, "scope", "UNKNOWN")
                reason_code = (
                    HealingRejectionReason.SHARED_BOTTLENECK_NOT_AVOIDED
                    if str(scope) in {"SHARED_PATH", "ACCESS"}
                    else HealingRejectionReason.BOTTLENECK_STILL_ON_PATH
                )
                return self._reject(
                    action,
                    candidate_id,
                    HealingActionApplicability.NO_EXPECTED_EFFECT,
                    reason_code,
                    "candidate path still traverses the diagnosed bottleneck",
                    effect,
                    evidence_refs,
                    fingerprint,
                    signature,
                )
            if (
                required_mbps is not None
                and expected_available is not None
                and expected_available < required_mbps
            ):
                return self._reject(
                    action,
                    candidate_id,
                    HealingActionApplicability.NO_EXPECTED_EFFECT,
                    HealingRejectionReason.INSUFFICIENT_EXPECTED_CAPACITY,
                    f"estimated {expected_available:g} Mbps is below required {required_mbps:g} Mbps",
                    effect,
                    evidence_refs,
                    fingerprint,
                    signature,
                )
            repeated = self._repeated(
                previously_attempted_actions, fingerprint, signature
            )
            if repeated:
                return self._reject(
                    action,
                    candidate_id,
                    HealingActionApplicability.NOT_APPLICABLE,
                    HealingRejectionReason.REPEATED_FAILED_ACTION,
                    "the same action already failed under materially unchanged runtime state",
                    effect,
                    evidence_refs,
                    fingerprint,
                    signature,
                )
            return self._accept(
                action,
                candidate_id,
                "target path differs, avoids the diagnosed bottleneck, and has sufficient estimated capacity",
                effect,
                evidence_refs,
                fingerprint,
                signature,
            )

        if action.type in {
            HealingActionType.THROTTLE_LOWER_PRIORITY,
            HealingActionType.QOS_REALLOCATION,
        }:
            target_ids = {
                str(item) for item in action.params.get("target_intent_ids") or []
            }
            candidates = [
                item
                for item in active_intents
                if str(item.get("intent_id")) in target_ids
            ]
            protected = [
                str(item.get("intent_id"))
                for item in candidates
                if not (
                    bool(item.get("preemptible"))
                    and str(item.get("priority") or "").lower() == "low"
                )
            ]
            released = self._number(action.params.get("expected_released_mbps")) or 0.0
            required_gain = self._number(action.params.get("required_gain_mbps")) or 0.0
            current_mbps = self._number(action.params.get("current_measurement_mbps"))
            effect = HealingExpectedEffect(
                bottleneck_avoided=False,
                expected_available_mbps=(
                    current_mbps + released if current_mbps is not None else released
                ),
                expected_gain_mbps=released,
                affected_intents=sorted(target_ids),
                confidence=action.confidence,
                evidence_refs=evidence_refs,
            )
            if protected:
                return self._reject(
                    action,
                    candidate_id,
                    HealingActionApplicability.UNSAFE,
                    HealingRejectionReason.PROTECTED_INTENT,
                    f"protected intents cannot be throttled: {', '.join(protected)}",
                    effect,
                    evidence_refs,
                    fingerprint,
                    signature,
                )
            if not candidates:
                return self._reject(
                    action,
                    candidate_id,
                    HealingActionApplicability.NOT_APPLICABLE,
                    HealingRejectionReason.NO_PREEMPTIBLE_RESOURCE,
                    "no low-priority preemptible competing intent is present on the bottleneck",
                    effect,
                    evidence_refs,
                    fingerprint,
                    signature,
                )
            if released <= 0 or released + 1e-9 < required_gain:
                return self._reject(
                    action,
                    candidate_id,
                    HealingActionApplicability.NO_EXPECTED_EFFECT,
                    HealingRejectionReason.INSUFFICIENT_RECOVERABLE_CAPACITY,
                    f"recoverable {released:g} Mbps is below required gain {required_gain:g} Mbps",
                    effect,
                    evidence_refs,
                    fingerprint,
                    signature,
                )
            if self._repeated(previously_attempted_actions, fingerprint, signature):
                return self._reject(
                    action,
                    candidate_id,
                    HealingActionApplicability.NOT_APPLICABLE,
                    HealingRejectionReason.REPEATED_FAILED_ACTION,
                    "the same resource action already failed under materially unchanged runtime state",
                    effect,
                    evidence_refs,
                    fingerprint,
                    signature,
                )
            return self._accept(
                action,
                candidate_id,
                "safe preemptible resources can release enough bandwidth",
                effect,
                evidence_refs,
                fingerprint,
                signature,
            )

        if action.type == HealingActionType.RELEASE_STALE_RESERVATION:
            stale = list(action.params.get("reservation_ids") or [])
            released = self._number(action.params.get("expected_released_mbps")) or 0.0
            required_gain = self._number(action.params.get("required_gain_mbps")) or 0.0
            effect = HealingExpectedEffect(
                expected_gain_mbps=released,
                expected_available_mbps=released,
                confidence=action.confidence,
                evidence_refs=evidence_refs,
            )
            if not stale or released <= 0:
                return self._reject(
                    action,
                    candidate_id,
                    HealingActionApplicability.NOT_APPLICABLE,
                    HealingRejectionReason.INSUFFICIENT_RECOVERABLE_CAPACITY,
                    "no stale reservation with measured recoverable capacity exists",
                    effect,
                    evidence_refs,
                    fingerprint,
                    signature,
                )
            if released < required_gain:
                return self._reject(
                    action,
                    candidate_id,
                    HealingActionApplicability.NO_EXPECTED_EFFECT,
                    HealingRejectionReason.INSUFFICIENT_RECOVERABLE_CAPACITY,
                    "stale reservations cannot release enough capacity",
                    effect,
                    evidence_refs,
                    fingerprint,
                    signature,
                )
            return self._accept(
                action,
                candidate_id,
                "stale reservations are evidenced and sufficient",
                effect,
                evidence_refs,
                fingerprint,
                signature,
            )

        effect = HealingExpectedEffect(
            confidence=action.confidence, evidence_refs=evidence_refs
        )
        if action.type == HealingActionType.NO_FEASIBLE_ACTION:
            return self._reject(
                action,
                candidate_id,
                HealingActionApplicability.NOT_APPLICABLE,
                HealingRejectionReason.NO_FEASIBLE_HEALING_ACTION,
                action.reason,
                effect,
                evidence_refs,
                fingerprint,
                signature,
            )
        if self._repeated(previously_attempted_actions, fingerprint, signature):
            return self._reject(
                action,
                candidate_id,
                HealingActionApplicability.NOT_APPLICABLE,
                HealingRejectionReason.REPEATED_FAILED_ACTION,
                "the same action already failed under materially unchanged runtime state",
                effect,
                evidence_refs,
                fingerprint,
                signature,
            )
        return self._accept(
            action,
            candidate_id,
            "action has a distinct non-path configuration effect",
            effect,
            evidence_refs,
            fingerprint,
            signature,
        )

    @staticmethod
    def _root_value(root_cause: RootCauseAnalysis | dict[str, Any], key: str, default: Any) -> Any:
        value = (
            root_cause.get(key, default)
            if isinstance(root_cause, dict)
            else getattr(root_cause, key, default)
        )
        return value.value if hasattr(value, "value") else value

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _repeated(
        previous: list[dict[str, Any]], fingerprint: str, signature: str
    ) -> bool:
        terminal_success = {"VERIFIED", "SATISFIED"}
        return any(
            str(item.get("action_fingerprint") or "") == fingerprint
            and str(item.get("context_signature") or "") == signature
            and str(item.get("status") or item.get("outcome") or "").upper()
            not in terminal_success
            for item in previous
        )

    @staticmethod
    def _reject(
        action: HealingAction,
        candidate_id: str,
        applicability: HealingActionApplicability,
        code: HealingRejectionReason,
        reason: str,
        effect: HealingExpectedEffect,
        evidence_refs: list[str],
        fingerprint: str,
        signature: str,
    ) -> HealingActionEvaluation:
        return HealingActionEvaluation(
            candidate_id=candidate_id,
            action_id=action.action_id,
            action=action.type,
            target=action.target,
            applicability=applicability,
            reason_code=code,
            reason=reason,
            evidence_refs=evidence_refs,
            expected_effect=effect,
            affected_intents=list(effect.affected_intents),
            validator_result="REJECTED",
            action_fingerprint=fingerprint,
            context_signature=signature,
        )

    @staticmethod
    def _accept(
        action: HealingAction,
        candidate_id: str,
        reason: str,
        effect: HealingExpectedEffect,
        evidence_refs: list[str],
        fingerprint: str,
        signature: str,
    ) -> HealingActionEvaluation:
        return HealingActionEvaluation(
            candidate_id=candidate_id,
            action_id=action.action_id,
            action=action.type,
            target=action.target,
            applicability=HealingActionApplicability.APPLICABLE,
            reason_code=HealingRejectionReason.NONE,
            reason=reason,
            evidence_refs=evidence_refs,
            expected_effect=effect,
            affected_intents=list(effect.affected_intents),
            validator_result="PASS",
            action_fingerprint=fingerprint,
            context_signature=signature,
        )


class CandidateActionGenerator:
    """Generate options from root cause and current resources, without selecting."""

    def generate(
        self,
        spec: IntentSpec,
        context: NetworkContext,
        *,
        current_path: list[str],
        root_cause: RootCauseAnalysis,
        path_candidates: list[CandidatePlan],
        current_measurement_mbps: float | None,
        tolerance_ratio: float = 1.0,
    ) -> list[HealingAction]:
        required = self._required_bandwidth(spec)
        effective_min = required * tolerance_ratio if required is not None else None
        required_gain = max(
            0.0,
            (effective_min or 0.0) - float(current_measurement_mbps or 0.0),
        )
        bottlenecks = list(root_cause.affected_link_ids)
        if root_cause.bottleneck_link_id and root_cause.bottleneck_link_id not in bottlenecks:
            bottlenecks.append(root_cause.bottleneck_link_id)
        actions: list[HealingAction] = []
        reroute_causes = {
            RootCauseCategory.PATH_FAILURE,
            RootCauseCategory.PATH_CONGESTION,
            RootCauseCategory.PATH_SEGMENT_CONGESTION,
            RootCauseCategory.SHARED_BOTTLENECK_CONGESTION,
            RootCauseCategory.ACCESS_UPLINK_CONGESTION,
            RootCauseCategory.CORE_PATH_CONGESTION,
            RootCauseCategory.EGRESS_CONGESTION,
            RootCauseCategory.DEVICE_UNAVAILABLE,
            RootCauseCategory.RESOURCE_CONTENTION,
        }
        if root_cause.category in reroute_causes:
            for candidate in path_candidates:
                if not candidate.path:
                    continue
                estimates = [
                    value
                    for value in (
                        candidate.available_capacity_mbps,
                        candidate.empirical_capacity_mbps,
                    )
                    if value is not None
                ]
                expected_available = min(estimates) if estimates else None
                actions.append(
                    HealingAction(
                        type=HealingActionType.REROUTE,
                        target=candidate.candidate_id,
                        params={
                            "candidate_id": candidate.candidate_id,
                            "target_path": list(candidate.path),
                            "current_path": list(current_path),
                            "bottleneck_link_ids": bottlenecks,
                            "expected_available_mbps": expected_available,
                            "current_measurement_mbps": current_measurement_mbps,
                            "requested_mbps": required,
                            "effective_min_mbps": effective_min,
                            "evidence_refs": candidate.capability_evidence_ids,
                        },
                        reason="evaluate whether this physical path removes the diagnosed bottleneck",
                        expected_effect={
                            "bottleneck_avoided": None,
                            "expected_available_mbps": expected_available,
                        },
                        affected_intent_ids=list(candidate.affected_intent_ids),
                        risk=candidate.risk,
                        confidence=root_cause.final_confidence or root_cause.confidence,
                    )
                )

            competitors, recoverable = self._preemptible_competitors(
                context, bottlenecks, spec.intent_id
            )
            target_ids = [str(item.get("intent_id")) for item in competitors]
            common_params = {
                "target_intent_ids": target_ids,
                "expected_released_mbps": recoverable,
                "required_gain_mbps": required_gain,
                "current_measurement_mbps": current_measurement_mbps,
                "requested_mbps": required,
                "effective_min_mbps": effective_min,
                "bottleneck_link_ids": bottlenecks,
                "evidence_refs": list(context.evidence_refs),
            }
            actions.append(
                HealingAction(
                    type=HealingActionType.QOS_REALLOCATION,
                    target="shared_bottleneck_resources",
                    params=dict(common_params),
                    reason="evaluate safe resource reallocation on the diagnosed bottleneck",
                    expected_effect={"expected_released_mbps": recoverable},
                    affected_intent_ids=target_ids,
                    risk="medium",
                    confidence=root_cause.final_confidence or root_cause.confidence,
                )
            )
            actions.append(
                HealingAction(
                    type=HealingActionType.THROTTLE_LOWER_PRIORITY,
                    target=",".join(target_ids) if target_ids else "none",
                    params=dict(common_params),
                    reason="only currently competing low-priority preemptible intents may be throttled",
                    expected_effect={"expected_released_mbps": recoverable},
                    affected_intent_ids=target_ids,
                    risk="high",
                    confidence=root_cause.final_confidence or root_cause.confidence,
                )
            )
            stale = [
                item
                for item in context.reservations
                if bool(item.get("stale"))
                or str(item.get("status") or "").upper() in {"STALE", "EXPIRED"}
            ]
            stale_recoverable = sum(
                float(item.get("bandwidth_mbps") or 0) for item in stale
            )
            actions.append(
                HealingAction(
                    type=HealingActionType.RELEASE_STALE_RESERVATION,
                    target="stale_reservations",
                    params={
                        "reservation_ids": [
                            str(item.get("reservation_id") or item.get("id"))
                            for item in stale
                        ],
                        "expected_released_mbps": stale_recoverable,
                        "required_gain_mbps": required_gain,
                        "evidence_refs": list(context.evidence_refs),
                    },
                    reason="release only reservations evidenced as stale or expired",
                    expected_effect={"expected_released_mbps": stale_recoverable},
                    risk="low",
                    confidence=root_cause.final_confidence or root_cause.confidence,
                )
            )
        elif root_cause.category == RootCauseCategory.TC_CONFIGURATION_DRIFT:
            actions.append(
                HealingAction(
                    type=HealingActionType.QOS_REALLOCATION,
                    target=spec.intent_id,
                    params={"target_intent_ids": [], "expected_released_mbps": 0},
                    reason="restore desired TC allocation after verified drift",
                    expected_effect={"tc_readback_matches": True},
                    risk="low",
                    confidence=root_cause.final_confidence or root_cause.confidence,
                )
            )
        elif root_cause.category == RootCauseCategory.P4_ENTRY_MISSING:
            actions.append(
                HealingAction(
                    type=HealingActionType.POLICY_UPDATE,
                    target=spec.intent_id,
                    reason="restore missing P4 entry after deterministic readback",
                    expected_effect={"p4_readback_matches": True},
                    risk="low",
                    confidence=root_cause.final_confidence or root_cause.confidence,
                )
            )
        if not actions:
            actions.append(
                HealingAction(
                    type=HealingActionType.NO_FEASIBLE_ACTION,
                    target=spec.intent_id,
                    reason="telemetry or causal evidence is insufficient for a safe action",
                    risk="low",
                )
            )
        return actions

    @staticmethod
    def _required_bandwidth(spec: IntentSpec) -> float | None:
        return next(
            (
                float(item.value)
                for item in spec.objectives
                if item.type == ObjectiveType.MIN_BANDWIDTH
            ),
            None,
        )

    @staticmethod
    def _preemptible_competitors(
        context: NetworkContext,
        bottlenecks: list[str],
        protected_intent_id: str,
    ) -> tuple[list[dict[str, Any]], float]:
        competitors = []
        for item in context.active_intents:
            intent_id = str(item.get("intent_id") or "")
            if not intent_id or intent_id == protected_intent_id:
                continue
            path = list(item.get("current_path") or [])
            competes = not bottlenecks or any(
                path_contains_link(path, link_id) for link_id in bottlenecks
            )
            if (
                competes
                and bool(item.get("preemptible"))
                and str(item.get("priority") or "").lower() == "low"
            ):
                competitors.append(item)
        recoverable = 0.0
        ids = {str(item.get("intent_id")) for item in competitors}
        for item in competitors:
            recoverable += float(item.get("current_usage_mbps") or 0)
        if recoverable <= 0:
            recoverable = sum(
                float(item.get("bandwidth_mbps") or 0)
                for item in context.reservations
                if str(item.get("intent_id") or "") in ids
                and str(item.get("reservation_kind") or "GUARANTEED").upper()
                in {"GUARANTEED", "RATE_LIMIT"}
            )
        return competitors, recoverable


class FeasibilityEvaluator:
    """Summarize deterministic maximum achievable capacity across legal actions."""

    @staticmethod
    def evaluate(
        evaluations: list[HealingActionEvaluation],
        *,
        effective_min_mbps: float | None,
        current_measurement_mbps: float | None,
    ) -> dict[str, Any]:
        estimates = [
            float(item.expected_effect.expected_available_mbps)
            for item in evaluations
            if item.expected_effect.expected_available_mbps is not None
            and item.applicability == HealingActionApplicability.APPLICABLE
        ]
        maximum = max(estimates, default=float(current_measurement_mbps or 0.0))
        return {
            "effective_min_mbps": effective_min_mbps,
            "current_measurement_mbps": current_measurement_mbps,
            "maximum_achievable_estimate_mbps": maximum,
            "has_applicable_action": any(
                item.applicability == HealingActionApplicability.APPLICABLE
                for item in evaluations
            ),
            "estimated_satisfiable": (
                None
                if effective_min_mbps is None
                else maximum >= effective_min_mbps
            ),
            "evidence_based": True,
        }

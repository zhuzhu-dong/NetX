"""Fact-first diagnosis with constrained LLM ranking and deterministic authority."""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.base_agent import BaseAgent
from agents.llm_client import (
    LLMInvocationError,
    finalize_llm_call_record,
    invoke_structured_llm,
)
from models.intent_domain import (
    DiagnosisFact,
    EvidenceItem,
    IntentAssuranceResult,
    LLMRuntimeMode,
    RootCauseAnalysis,
    RootCauseCategory,
    RootCauseScope,
    NetworkContext,
)
from agents.healing_action_applicability import path_contains_link
from models.llm_contracts import RootCauseLLMSelection


class IntentRootCauseAgent(BaseAgent):
    agent_id = "intent_root_cause"

    async def diagnose(
        self,
        assurance: IntentAssuranceResult,
        evidence: list[EvidenceItem],
        *,
        active_faults: list[dict] | None = None,
        revision: int | None = None,
        correlation_id: str | None = None,
        input_refs: list[str] | None = None,
        network_context: NetworkContext | None = None,
    ) -> RootCauseAnalysis:
        active_faults = list(active_faults or [])
        bottleneck_links = self._bottleneck_links(
            evidence, active_faults, network_context
        )
        scope, affected_candidates, location = self._scope(
            bottleneck_links, network_context
        )
        facts = self._facts(
            assurance,
            evidence,
            active_faults,
            bottleneck_links=bottleneck_links,
            scope=scope,
            affected_candidates=affected_candidates,
        )
        category = self._category(
            assurance,
            evidence,
            active_faults,
            scope=scope,
            location=location,
        )
        candidates = self._candidates(category)
        deterministic_confidence = (
            0.85 if category != RootCauseCategory.UNKNOWN else 0.3
        )
        explanation = self._explanation(
            category, scope, bottleneck_links, affected_candidates
        )
        llm_status = "NOT_ATTEMPTED"
        runtime_mode = LLMRuntimeMode.NOT_USED
        llm_ranking: list[dict] = []
        call_record = None
        candidate_ids = {item["cause"] for item in candidates}
        fact_ids = {item.fact_id for item in facts}

        def validate(payload: dict) -> RootCauseLLMSelection:
            selection = RootCauseLLMSelection.model_validate(payload)
            ranked_causes = [item.cause for item in selection.ranking]
            if len(ranked_causes) != len(set(ranked_causes)):
                raise ValueError("root-cause ranking contains duplicates")
            if not set(ranked_causes) <= candidate_ids:
                raise ValueError("root-cause ranking contains an unknown cause")
            if selection.selected_cause != category.value:
                raise ValueError("LLM root cause contradicts deterministic authority")
            if any(
                not set(item.evidence_fact_ids) <= fact_ids
                for item in selection.ranking
            ):
                raise ValueError("LLM cited a nonexistent deterministic fact")
            return selection

        try:
            invocation = await invoke_structured_llm(
                [
                    SystemMessage(
                        content=(
                            "Rank only the supplied root-cause candidates using only the supplied deterministic "
                            "facts. Return ONLY one JSON object with no wrapper and exactly this value structure: "
                            '{"selected_cause":"CAUSE","ranking":[{"cause":"CAUSE","rank":1,'
                            '"confidence":0.8,"reason":"reason","evidence_fact_ids":["FACT-ID"]}],'
                            '"explanation":"explanation","confidence":0.8}. '
                            "selected_cause MUST equal deterministic_authority exactly. Use only exact cause values "
                            "from candidates and exact fact_id values from facts; use an empty evidence_fact_ids "
                            "array when no supplied fact applies. All confidence values must be JSON numbers from "
                            "0 through 1, never quoted strings. Never invent facts or emit device/shell commands."
                        )
                    ),
                    HumanMessage(
                        content=json.dumps(
                            {
                                "deterministic_authority": category.value,
                                "candidates": candidates,
                                "facts": [item.model_dump(mode="json") for item in facts],
                                "runtime_state": {
                                    "current_path": (
                                        (network_context.current_paths or {}).get(
                                            assurance.intent_id, []
                                        )
                                        if network_context
                                        else []
                                    ),
                                    "bottleneck_link_ids": bottleneck_links,
                                    "root_cause_scope": scope.value,
                                    "candidate_paths_affected": affected_candidates,
                                    "active_intents": (
                                        network_context.active_intents
                                        if network_context
                                        else []
                                    ),
                                },
                                "failed_objectives": [
                                    {
                                        "objective_id": item.objective_id,
                                        "type": item.objective_type.value,
                                        "status": item.status.value,
                                        "expected": item.expected,
                                        "observed": item.observed,
                                    }
                                    for item in assurance.assessments
                                    if item.status.value == "FAIL"
                                ],
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                    ),
                ],
                agent=self.agent_id,
                intent_id=assurance.intent_id,
                revision=revision,
                correlation_id=correlation_id,
                input_refs=list(input_refs or [])
                + [assurance.assurance_id]
                + [item.evidence_id for item in evidence],
                input_schema_version="RootCauseFactsInput.v1",
                output_schema_version="RootCauseLLMSelection.v1",
                validator=validate,
                llm=self.llm,
                fallback_on_error=True,
            )
            selection = invocation.value
            call_record = invocation.record
            explanation = selection.explanation
            llm_ranking = [item.model_dump(mode="json") for item in selection.ranking]
            llm_status = "SUCCESS"
            runtime_mode = invocation.record.runtime_mode
            finalize_llm_call_record(call_record, validator_result="PASS")
        except LLMInvocationError as exc:
            call_record = exc.record
            llm_status = exc.code
            runtime_mode = LLMRuntimeMode.DETERMINISTIC_FALLBACK
            explanation += f" {exc.code}; deterministic diagnosis retained authority."

        rejected = [
            item for item in candidates if item["cause"] != category.value
        ]
        return RootCauseAnalysis(
            intent_id=assurance.intent_id,
            assurance_id=assurance.assurance_id,
            category=category,
            selected_cause=category,
            confidence=deterministic_confidence,
            final_confidence=deterministic_confidence,
            facts=facts,
            deterministic_candidates=candidates,
            llm_ranking=llm_ranking,
            rejected_causes=rejected,
            explanation=explanation,
            scope=scope,
            bottleneck_link_id=(bottleneck_links[0] if bottleneck_links else None),
            affected_link_ids=bottleneck_links,
            candidate_paths_affected=affected_candidates,
            llm_status=llm_status,
            llm_runtime_mode=runtime_mode,
            llm_call_id=call_record.call_id if call_record else None,
            llm_provider=call_record.provider if call_record else None,
            llm_model=call_record.model if call_record else None,
            llm_structured_output_valid=(
                call_record.structured_output_valid if call_record else None
            ),
            llm_validator_result=(
                call_record.validator_result if call_record else "NOT_RUN"
            ),
            fallback_used=bool(call_record and call_record.fallback_used),
        )

    @staticmethod
    def _candidates(category: RootCauseCategory) -> list[dict]:
        return [
            {
                "cause": candidate.value,
                "deterministic_rank": 1 if candidate == category else index + 2,
                "supported": candidate == category,
                "reason": (
                    "supported by deterministic fact rules"
                    if candidate == category
                    else "not selected by deterministic fact rules"
                ),
            }
            for index, candidate in enumerate(RootCauseCategory)
        ]

    @staticmethod
    def _facts(
        assurance,
        evidence,
        faults,
        *,
        bottleneck_links: list[str] | None = None,
        scope: RootCauseScope = RootCauseScope.UNKNOWN,
        affected_candidates: list[str] | None = None,
    ) -> list[DiagnosisFact]:
        result = [
            DiagnosisFact(
                source="intent_assurance",
                statement=item.reason,
                strength="strong" if item.status.value == "FAIL" else "medium",
                evidence_ids=item.evidence_ids,
            )
            for item in assurance.assessments
        ]
        for item in evidence:
            if item.raw.get("p4_status") not in {None, "VERIFIED"}:
                result.append(
                    DiagnosisFact(
                        source=item.source,
                        statement=f"P4 status is {item.raw.get('p4_status')}",
                        evidence_ids=[item.evidence_id],
                    )
                )
            if item.raw.get("tc_status") not in {None, "MEASURED", "VERIFIED"}:
                result.append(
                    DiagnosisFact(
                        source=item.source,
                        statement=f"TC status is {item.raw.get('tc_status')}",
                        evidence_ids=[item.evidence_id],
                    )
                )
        for fault in faults:
            result.append(
                DiagnosisFact(
                    source="fault_store",
                    statement=(
                        f"active {fault.get('fault_type')} on "
                        f"{fault.get('target_resource_id')}"
                    ),
                    strength="strong",
                )
            )
        if bottleneck_links:
            result.append(
                DiagnosisFact(
                    source="network_context",
                    statement=(
                        f"bottleneck links {', '.join(bottleneck_links)} have scope "
                        f"{scope.value}; affected candidates: "
                        f"{', '.join(affected_candidates or []) or 'none'}"
                    ),
                    strength="strong",
                )
            )
        return result

    @staticmethod
    def _category(
        assurance,
        evidence,
        faults,
        *,
        scope: RootCauseScope = RootCauseScope.UNKNOWN,
        location: RootCauseScope = RootCauseScope.UNKNOWN,
    ) -> RootCauseCategory:
        if assurance.evidence_quality.value in {"MISSING", "STALE", "UNTRUSTED"}:
            return RootCauseCategory.TELEMETRY_INSUFFICIENT
        if any(
            str(item.raw.get("p4_status"))
            in {"NO_MATCHING_ENTRY", "COUNTER_NOT_BOUND"}
            for item in evidence
        ):
            return RootCauseCategory.P4_ENTRY_MISSING
        if any(
            str(item.raw.get("tc_status")) in {"NO_SAMPLE", "MISMATCH", "DRIFT"}
            for item in evidence
        ):
            return RootCauseCategory.TC_CONFIGURATION_DRIFT
        if any(str(item.get("fault_type")) == "DEVICE_DOWN" for item in faults):
            return RootCauseCategory.DEVICE_UNAVAILABLE
        if any(str(item.get("fault_type")) == "LINK_DOWN" for item in faults):
            return RootCauseCategory.PATH_FAILURE
        if any(
            str(item.get("fault_type"))
            in {"LINK_RATE_LIMIT", "SERVICE_OVERLOAD"}
            for item in faults
        ):
            return IntentRootCauseAgent._congestion_category(scope, location)
        failed = {
            item.objective_type.value
            for item in assurance.assessments
            if item.status.value == "FAIL"
        }
        if failed & {
            "MIN_BANDWIDTH",
            "MAX_LATENCY",
            "MAX_PACKET_LOSS",
            "MAX_JITTER",
        }:
            return IntentRootCauseAgent._congestion_category(scope, location)
        return RootCauseCategory.UNKNOWN

    @staticmethod
    def _congestion_category(
        scope: RootCauseScope, location: RootCauseScope
    ) -> RootCauseCategory:
        if scope == RootCauseScope.SHARED_PATH:
            return RootCauseCategory.SHARED_BOTTLENECK_CONGESTION
        if location == RootCauseScope.ACCESS:
            return RootCauseCategory.ACCESS_UPLINK_CONGESTION
        if location == RootCauseScope.EGRESS:
            return RootCauseCategory.EGRESS_CONGESTION
        if scope == RootCauseScope.PATH_SPECIFIC:
            return RootCauseCategory.CORE_PATH_CONGESTION
        return RootCauseCategory.PATH_CONGESTION

    @staticmethod
    def _bottleneck_links(evidence, faults, context) -> list[str]:
        values: list[str] = []
        for fault in faults:
            if str(fault.get("fault_type") or "") in {
                "LINK_RATE_LIMIT",
                "LINK_DOWN",
                "SERVICE_OVERLOAD",
            } and fault.get("target_resource_id"):
                values.append(str(fault["target_resource_id"]))
        for item in evidence:
            raw = item.raw or {}
            for key in ("bottleneck_link", "bottleneck_link_id", "blocking_link"):
                if raw.get(key):
                    values.append(str(raw[key]))
        if context is not None:
            values.extend(
                str(item.get("link_id"))
                for item in context.bottleneck_candidates
                if item.get("link_id")
            )
            values.extend(
                str(item.get("resource_id"))
                for item in context.congestion
                if item.get("resource_id")
            )
        return list(dict.fromkeys(item for item in values if item))

    @staticmethod
    def _scope(
        bottleneck_links: list[str], context: NetworkContext | None
    ) -> tuple[RootCauseScope, list[str], RootCauseScope]:
        if not bottleneck_links or context is None:
            return RootCauseScope.UNKNOWN, [], RootCauseScope.UNKNOWN
        paths = [
            item
            for item in context.candidate_paths
            if item.get("path") and item.get("feasible", True)
        ]
        affected = [
            str(item.get("candidate_id") or "")
            for item in paths
            if any(
                path_contains_link(item.get("path") or [], link_id)
                for link_id in bottleneck_links
            )
        ]
        scope = (
            RootCauseScope.SHARED_PATH
            if paths and len(affected) == len(paths)
            else RootCauseScope.PATH_SPECIFIC
            if affected
            else RootCauseScope.UNKNOWN
        )
        path = list(paths[0].get("path") or []) if paths else []
        # Candidate paths normally include the source/destination hosts.  Root-
        # cause location is defined over network links, so classify the first
        # and last *switch-to-switch* segments rather than the host attachment.
        switch_path = [
            node
            for node in path
            if not str(node).lower().startswith(("h_", "host:"))
        ]
        location = RootCauseScope.UNKNOWN
        if len(switch_path) >= 2 and any(
            path_contains_link(switch_path[:2], link_id)
            for link_id in bottleneck_links
        ):
            location = RootCauseScope.ACCESS
        elif len(switch_path) >= 2 and any(
            path_contains_link(switch_path[-2:], link_id)
            for link_id in bottleneck_links
        ):
            location = RootCauseScope.EGRESS
        return scope, [item for item in affected if item], location

    @staticmethod
    def _explanation(
        category: RootCauseCategory,
        scope: RootCauseScope,
        bottleneck_links: list[str],
        affected_candidates: list[str],
    ) -> str:
        links = ", ".join(bottleneck_links) or "unresolved link"
        if scope == RootCauseScope.SHARED_PATH:
            return (
                f"Deterministic evidence identifies {links} as a shared bottleneck "
                f"affecting candidate paths {', '.join(affected_candidates)}; switching "
                "between those paths cannot remove the cause."
            )
        return (
            f"Deterministic evidence ranks {category.value} at {links} with "
            f"scope {scope.value}."
        )

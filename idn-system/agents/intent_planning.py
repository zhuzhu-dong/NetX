"""Deterministic candidate generation, constrained LLM selection and validation."""

from __future__ import annotations

import json
from typing import Any

from agents.base_agent import BaseAgent
from agents.llm_client import (
    LLMInvocationError,
    finalize_llm_call_record,
    invoke_structured_llm,
)
from infra.constrained_path_planner import ConstrainedPathPlanner
from langchain_core.messages import HumanMessage, SystemMessage
from models.faults import PathConstraints
from models.intent_domain import (
    AgentDecision,
    CandidatePlan,
    ComparisonOperator,
    ConstraintType,
    ContextEvidenceQuality,
    IntentPlanIR,
    IntentSpec,
    LLMRuntimeMode,
    NetworkContext,
    ObjectiveType,
    PlanDiff,
    PlanningRiskStatus,
    PlanValidationStatus,
    RequiredAction,
)
from models.llm_contracts import PlanningLLMSelection
from persistence.capability_repository import CapabilityRepository
from utils.llm_json import extract_json_object
from agents.healing_action_applicability import (
    PathRootCauseImpactEvaluator,
    path_edges,
    path_contains_link,
)


class IntentNotPlannableError(ValueError):
    """Raised when semantic validation has not produced a complete IntentSpec."""

    code = "INTENT_NOT_PLANNABLE"


class CandidateGenerator:
    def __init__(
        self,
        planner: ConstrainedPathPlanner | None = None,
        capabilities: CapabilityRepository | None = None,
    ):
        self.planner = planner or ConstrainedPathPlanner()
        self.capabilities = capabilities

    def generate(
        self, spec: IntentSpec, context: NetworkContext
    ) -> list[CandidatePlan]:
        source = self._node_for(
            spec.source.get("ip") or spec.selector.get("source_ip"), context
        )
        destination = self._node_for(
            spec.destination.get("ip") or spec.selector.get("destination_ip"), context
        )
        if not source or not destination:
            return []
        constraints = self._constraints(spec)
        candidates: list[CandidatePlan] = []
        attempted: set[tuple[str, ...]] = set()

        def compute(current: PathConstraints, label: str) -> None:
            try:
                path = self.planner.compute(
                    context.topology, source, destination, current
                )
            except (ValueError, KeyError) as exc:
                reason = str(exc) or "no path satisfies deterministic constraints"
                candidates.append(
                    CandidatePlan(
                        context_id=context.context_id,
                        feasible=False,
                        violations=[reason],
                        rejection_reasons=[reason],
                        risk_status=PlanningRiskStatus.INFEASIBLE,
                        deterministic_facts=[
                            label,
                            "candidate rejected during constrained search",
                        ],
                    )
                )
                return
            signature = tuple(path.nodes)
            if signature in attempted:
                return
            attempted.add(signature)
            runtime_available = self._available_capacity(context, path.nodes)
            available_capacity = (
                runtime_available
                if runtime_available is not None
                else path.bottleneck_available_mbps
            )
            actions = [
                RequiredAction(
                    action_type="INSTALL_NETWORK_POLICY",
                    target=source,
                    parameters={
                        "selector": spec.selector,
                        "path": path.nodes,
                        "objectives": [
                            item.model_dump(mode="json") for item in spec.objectives
                        ],
                    },
                    preconditions=[
                        "candidate passes PlanValidator",
                        "conflict and safety checks pass",
                    ],
                )
            ]
            risk_status, capability_ids = self._risk(spec, context)
            candidates.append(
                CandidatePlan(
                    context_id=context.context_id,
                    path=path.nodes,
                    required_actions=actions,
                    expected_outcomes={
                        "estimated_latency_ms": path.estimated_latency_ms,
                        "bottleneck_available_mbps": available_capacity,
                    },
                    available_capacity_mbps=available_capacity,
                    empirical_capacity_mbps=self._empirical_capacity(
                        context, path.nodes
                    ),
                    current_utilization_percent=self._path_utilization(
                        context, path.nodes
                    ),
                    estimated_latency_ms=path.estimated_latency_ms,
                    reservations=list(context.reservations),
                    affected_intent_ids=self._affected_intents(context, path.nodes),
                    expected_objective_results=[
                        {
                            "objective_id": item.objective_id,
                            "type": item.type.value,
                            "expected": item.value,
                            "predicted": (
                                available_capacity
                                if item.type == ObjectiveType.MIN_BANDWIDTH
                                else path.estimated_latency_ms
                                if item.type == ObjectiveType.MAX_LATENCY
                                else True
                            ),
                        }
                        for item in spec.objectives
                    ],
                    score=round(100 - path.total_cost, 3),
                    risk_status=risk_status,
                    capability_evidence_ids=capability_ids,
                    deterministic_facts=[
                        label,
                        f"computed_cost={path.total_cost:g}",
                        f"estimated_latency_ms={path.estimated_latency_ms:g}",
                        f"bottleneck_available_mbps={available_capacity}",
                    ],
                )
            )

        compute(constraints, "lowest-cost feasible path")
        first_feasible = next((item for item in candidates if item.feasible), None)
        if first_feasible:
            for intermediate in first_feasible.path[1:-1]:
                alternative = constraints.model_copy(
                    update={
                        "avoid_devices": set(constraints.avoid_devices) | {intermediate}
                    }
                )
                compute(alternative, f"alternative avoiding {intermediate}")
        return candidates

    @staticmethod
    def _path_utilization(context: NetworkContext, path: list[str]) -> float | None:
        values: list[float] = []
        path_nodes = set(path)
        raw = context.utilization
        items = raw.get("links") if isinstance(raw, dict) else []
        if isinstance(raw, dict) and "links" not in raw:
            items = [
                {"link_id": key, "utilization_percent": value}
                for key, value in raw.items()
            ]
        if isinstance(items, dict):
            items = [dict(value, link_id=key) for key, value in items.items()]
        for item in items or []:
            link_id = str(item.get("link_id") or item.get("resource_id") or "")
            endpoints = {
                str(item.get("src") or item.get("source") or ""),
                str(item.get("dst") or item.get("destination") or ""),
            }
            if (
                (link_id and path_contains_link(path, link_id))
                or (endpoints and "" not in endpoints and endpoints <= path_nodes)
            ):
                value = item.get("utilization_percent", item.get("utilization"))
                if value is not None:
                    values.append(float(value))
        return max(values) if values else None

    @staticmethod
    def _available_capacity(
        context: NetworkContext, path: list[str]
    ) -> float | None:
        values = []
        for resource_id, raw in context.capacities.items():
            if not path_contains_link(path, str(resource_id)):
                continue
            if not isinstance(raw, dict):
                continue
            value = raw.get("available_guaranteed_mbps")
            if value is not None:
                values.append(float(value))
        return min(values) if values else None

    @staticmethod
    def _empirical_capacity(
        context: NetworkContext, path: list[str]
    ) -> float | None:
        path_nodes = set(path)
        values = [
            float(item["capacity_mbps"])
            for item in context.empirical_capabilities
            if item.get("capacity_mbps") is not None
            and set(item.get("path") or []) <= path_nodes
        ]
        return min(values) if values else None

    @staticmethod
    def _affected_intents(context: NetworkContext, path: list[str]) -> list[str]:
        path_nodes = set(path)
        return [
            str(item.get("intent_id"))
            for item in context.active_intents
            if item.get("intent_id")
            and len(path_nodes & set(item.get("current_path") or [])) >= 2
        ]

    def _risk(
        self, spec: IntentSpec, context: NetworkContext
    ) -> tuple[PlanningRiskStatus, list[str]]:
        if context.evidence_quality == ContextEvidenceQuality.STALE:
            return PlanningRiskStatus.STALE_EVIDENCE, []
        if context.evidence_quality == ContextEvidenceQuality.UNTRUSTED:
            return PlanningRiskStatus.UNTRUSTED_EVIDENCE, []
        if context.evidence_quality != ContextEvidenceQuality.TRUSTED:
            return PlanningRiskStatus.DEGRADED_CONTEXT, []
        if self.capabilities is None:
            return PlanningRiskStatus.UNVERIFIED_CAPABILITY, []
        evidence = []
        for objective in spec.objectives:
            evidence.extend(
                self.capabilities.find(
                    metric=objective.type.value,
                    epoch=context.epoch,
                )
            )
        proven = [item for item in evidence if item.status.value == "PROVEN"]
        if not proven:
            return PlanningRiskStatus.UNVERIFIED_CAPABILITY, []
        return PlanningRiskStatus.EMPIRICALLY_SUPPORTED, [
            item.capability_id for item in proven
        ]

    @staticmethod
    def _constraints(spec: IntentSpec) -> PathConstraints:
        min_bw = next(
            (
                float(item.value)
                for item in spec.objectives
                if item.type == ObjectiveType.MIN_BANDWIDTH
            ),
            None,
        )
        max_latency = next(
            (
                float(item.value)
                for item in spec.objectives
                if item.type == ObjectiveType.MAX_LATENCY
            ),
            None,
        )
        avoid_devices: set[str] = set()
        avoid_links: set[str] = set()
        required_devices: list[str] = []
        for item in spec.constraints:
            values = item.value if isinstance(item.value, list) else [item.value]
            if item.type == ConstraintType.AVOID_DEVICE:
                avoid_devices.update(str(value) for value in values)
            elif item.type == ConstraintType.AVOID_LINK:
                avoid_links.update(str(value) for value in values)
            elif item.type == ConstraintType.REQUIRE_DEVICE:
                required_devices.extend(str(value) for value in values)
        return PathConstraints(
            min_bandwidth_mbps=min_bw,
            max_latency_ms=max_latency,
            avoid_devices=avoid_devices,
            avoid_links=avoid_links,
            required_devices=required_devices,
            tenant_id=spec.tenant_id,
        )

    @staticmethod
    def _node_for(value: Any, context: NetworkContext) -> str | None:
        if value is None:
            return None
        value = str(value).split("/", 1)[0]
        aliases = context.topology.get("aliases") or {}
        resource_id = aliases.get(value)
        for endpoint in context.inventory.get("endpoints") or []:
            endpoint_ip = endpoint.get("ip_address") or endpoint.get("ip")
            endpoint_cidr = endpoint.get("ip_cidr")
            endpoint_host = endpoint.get("host_name") or endpoint.get("name")
            endpoint_resource = endpoint.get("resource_id")
            if value in {
                str(endpoint_ip).split("/", 1)[0],
                str(endpoint_cidr).split("/", 1)[0],
                str(endpoint_host),
                str(endpoint_resource),
                str(resource_id),
            }:
                return str(endpoint_host)
        return (
            value
            if any(
                str(node.get("id")) == value
                for node in context.topology.get("nodes") or []
            )
            else None
        )


class PlanValidator:
    def validate(self, spec: IntentSpec, plan: IntentPlanIR) -> IntentPlanIR:
        errors: list[str] = []
        selected = next(
            (
                item
                for item in plan.candidates
                if item.candidate_id == plan.selected_candidate_id
            ),
            None,
        )
        if selected is None:
            errors.append("no selected candidate")
        elif not selected.feasible:
            errors.append("selected candidate failed deterministic feasibility")
        elif not selected.path:
            errors.append("selected candidate has no path")
        if selected:
            latency = selected.expected_outcomes.get("estimated_latency_ms")
            capacity = selected.expected_outcomes.get("bottleneck_available_mbps")
            for objective in spec.objectives:
                if (
                    objective.type == ObjectiveType.MAX_LATENCY
                    and latency is not None
                    and float(latency) > float(objective.value)
                ):
                    errors.append(
                        f"candidate latency {latency}ms exceeds {objective.value}ms"
                    )
                if (
                    objective.type == ObjectiveType.MIN_BANDWIDTH
                    and capacity is not None
                    and float(capacity) < float(objective.value)
                ):
                    errors.append(
                        f"candidate capacity {capacity}Mbps is below {objective.value}Mbps"
                    )
        status = (
            PlanValidationStatus.VALID
            if not errors
            else (
                PlanValidationStatus.UNSATISFIABLE
                if plan.candidates
                else PlanValidationStatus.INVALID
            )
        )
        return plan.model_copy(
            update={"validation_status": status, "validation_errors": errors}
        )


class PlanningAgent(BaseAgent):
    agent_id = "intent_planning"

    def __init__(self, llm=None, capabilities: CapabilityRepository | None = None):
        super().__init__(llm)
        self.capabilities = capabilities or CapabilityRepository()
        self.generator = CandidateGenerator(capabilities=self.capabilities)
        self.validator = PlanValidator()

    async def plan(
        self, spec: IntentSpec, context: NetworkContext
    ) -> tuple[IntentPlanIR, AgentDecision]:
        errors = spec.plannability_errors()
        if errors:
            raise IntentNotPlannableError("; ".join(errors))
        candidates = self.generator.generate(spec, context)
        return await self._build_plan(spec, context, candidates, decision_type="CANDIDATE_SELECTION")

    async def _build_plan(
        self,
        spec: IntentSpec,
        context: NetworkContext,
        candidates: list[CandidatePlan],
        *,
        decision_type: str,
    ) -> tuple[IntentPlanIR, AgentDecision]:
        from services.preference_relations import rank_path_candidates

        bonuses = rank_path_candidates(candidates, spec.preferences)
        candidates = [
            item.model_copy(update={"score": item.score + bonuses.get(item.candidate_id, 0)})
            for item in candidates
        ]
        feasible = [item for item in candidates if item.feasible]
        deterministic = max(feasible, key=lambda item: item.score) if feasible else None
        selected_id = deterministic.candidate_id if deterministic else None
        reasoning = [
            "selected the highest-scoring candidate that passed deterministic generation"
        ]
        llm_status = "NOT_NEEDED"
        runtime_mode = LLMRuntimeMode.NOT_USED
        llm_output: dict[str, Any] = {}
        call_record = None
        should_call_llm = len(feasible) > 1 or (
            decision_type == "REPLAN_CANDIDATE_SELECTION" and len(feasible) == 1
        )
        if should_call_llm:
            try:
                feasible_ids = {item.candidate_id for item in feasible}

                def validate(payload: dict[str, Any]) -> PlanningLLMSelection:
                    selection = PlanningLLMSelection.model_validate(payload)
                    ranked = [item.candidate_id for item in selection.ranking]
                    if len(ranked) != len(set(ranked)):
                        raise ValueError("ranking contains duplicate candidate IDs")
                    if not set(ranked) <= feasible_ids:
                        raise ValueError("ranking contains an unknown candidate ID")
                    if selection.selected_candidate_id not in feasible_ids:
                        raise ValueError("LLM selected an unknown candidate")
                    return selection

                invocation = await invoke_structured_llm(
                    [
                        SystemMessage(
                            content=(
                                "Rank the supplied feasible candidate plans and select exactly one. Return ONLY "
                                "one JSON object with no wrapper and exactly this value structure: "
                                '{"selected_candidate_id":"CAND-ID","ranking":['
                                '{"candidate_id":"CAND-ID","rank":1,"reason":"reason"}],'
                                '"rationale":"rationale","tradeoffs":["tradeoff"],"confidence":0.8}. '
                                "Use only exact IDs present in candidates. Include every candidate exactly once "
                                "in ranking; ranks are unique positive integers; confidence is a number from 0 "
                                "through 1. Never emit device or shell commands. Deterministic validators retain "
                                "authority."
                            )
                        ),
                        HumanMessage(
                            content=json.dumps(
                                self._planning_payload(spec, context, feasible),
                                ensure_ascii=False,
                                default=str,
                            )
                        ),
                    ],
                    agent=self.agent_id,
                    intent_id=spec.intent_id,
                    revision=spec.revision,
                    input_schema_version="PlanningDecisionInput.v1",
                    output_schema_version="PlanningLLMSelection.v1",
                    input_refs=[
                        context.context_id,
                        *[item.candidate_id for item in feasible],
                    ],
                    validator=validate,
                    llm=self.llm,
                    fallback_on_error=True,
                )
                selection = invocation.value
                call_record = invocation.record
                llm_output = selection.model_dump(mode="json")
                selected_id = selection.selected_candidate_id
                reasoning = [selection.rationale, *selection.tradeoffs]
                llm_status = "SUCCESS"
                runtime_mode = invocation.record.runtime_mode
            except LLMInvocationError as exc:
                call_record = exc.record
                llm_status = exc.code
                runtime_mode = LLMRuntimeMode.DETERMINISTIC_FALLBACK
                reasoning.append(
                    f"{exc.code}; deterministic ranking retained authority"
                )
        selected = next(
            (item for item in candidates if item.candidate_id == selected_id), None
        )
        rejected = {
            item.candidate_id: (
                list(item.rejection_reasons)
                if not item.feasible
                else ["not selected after deterministic feasibility and ranking"]
            )
            for item in candidates
            if item.candidate_id != selected_id
        }
        plan = IntentPlanIR(
            intent_id=spec.intent_id,
            intent_revision=spec.revision,
            context_id=context.context_id,
            candidates=candidates,
            selected_candidate_id=selected_id,
            selected_reasoning=reasoning,
            rejected_reasoning=rejected,
            required_actions=list(selected.required_actions) if selected else [],
            expected_outcomes=dict(selected.expected_outcomes) if selected else {},
            rollback_strategy=[
                RequiredAction(action_type="RESTORE_PREVIOUS_POLICY_STATE")
            ],
            llm_status=llm_status,
            llm_runtime_mode=runtime_mode,
            llm_call_id=call_record.call_id if call_record else None,
            llm_provider=call_record.provider if call_record else None,
            llm_model=call_record.model if call_record else None,
            llm_structured_output_valid=(
                call_record.structured_output_valid if call_record else None
            ),
            fallback_used=bool(call_record and call_record.fallback_used),
            llm_selection=llm_output,
        )
        plan = self.validator.validate(spec, plan)
        validator_result = (
            (
                "PASS"
                if plan.validation_status == PlanValidationStatus.VALID
                else "REJECTED_BY_PLAN_VALIDATOR"
            )
            if call_record and call_record.status == "SUCCESS"
            else (call_record.validator_result if call_record else "NOT_RUN")
        )
        plan.llm_validator_result = validator_result if call_record else "NOT_RUN"
        if call_record and call_record.status == "SUCCESS":
            finalize_llm_call_record(
                call_record,
                validator_result=validator_result,
                fallback_used=plan.fallback_used,
            )
        decision = AgentDecision(
            intent_id=spec.intent_id,
            intent_revision=spec.revision,
            agent_id=self.agent_id,
            decision_type=decision_type,
            plan_id=plan.plan_id,
            context_id=context.context_id,
            inputs={"candidate_ids": [item.candidate_id for item in candidates]},
            output={
                "selected_candidate_id": selected_id,
                "validation_status": plan.validation_status.value,
                "llm_selection": llm_output,
            },
            facts=reasoning,
            alternatives=[item.model_dump(mode="json") for item in candidates],
            llm_status=llm_status,
            llm_runtime_mode=plan.llm_runtime_mode,
            llm_call_id=plan.llm_call_id,
            llm_provider=plan.llm_provider,
            llm_model=plan.llm_model,
            llm_structured_output_valid=plan.llm_structured_output_valid,
            llm_validator_result=plan.llm_validator_result,
            fallback_used=plan.fallback_used,
        )
        return plan, decision

    @staticmethod
    def _planning_payload(
        spec: IntentSpec,
        context: NetworkContext,
        feasible: list[CandidatePlan],
    ) -> dict[str, Any]:
        reservation_keys = {
            "intent_id",
            "path",
            "bandwidth_mbps",
            "priority",
            "preemptible",
            "protected",
        }
        return {
            "intent": {
                "intent_id": spec.intent_id,
                "revision": spec.revision,
                "priority": spec.priority,
                "source": spec.source,
                "destination": spec.destination,
                "service": spec.service,
                "selector": spec.selector,
                "objectives": [item.model_dump(mode="json") for item in spec.objectives],
                "constraints": [item.model_dump(mode="json") for item in spec.constraints],
            },
            "network_context": {
                "context_id": context.context_id,
                "evidence_quality": context.evidence_quality.value,
                "decision_usable": context.decision_usable,
                "current_path": context.current_paths.get(spec.intent_id) or [],
                "bottleneck_candidates": context.bottleneck_candidates,
                "current_utilization": context.utilization,
                "available_capacity": context.capacities,
                "active_intents": [
                    {
                        key: item.get(key)
                        for key in (
                            "intent_id",
                            "priority",
                            "preemptible",
                            "current_path",
                            "current_usage_mbps",
                        )
                    }
                    for item in context.active_intents
                ],
                "previous_attempts": context.healing_attempt_history,
            },
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "path": item.path,
                    "score": item.score,
                    "available_capacity_mbps": item.available_capacity_mbps,
                    "empirical_capacity_mbps": item.empirical_capacity_mbps,
                    "current_utilization_percent": item.current_utilization_percent,
                    "estimated_latency_ms": item.estimated_latency_ms,
                    "risk_status": item.risk_status.value,
                    "risk": item.risk,
                    "affected_intent_ids": item.affected_intent_ids,
                    "reservations": [
                        {key: value for key, value in reservation.items() if key in reservation_keys}
                        for reservation in item.reservations
                    ],
                    "expected_objective_results": item.expected_objective_results,
                    "path_impact": PathRootCauseImpactEvaluator.evaluate(
                        context.current_paths.get(spec.intent_id) or [],
                        list(item.path),
                        [
                            str(entry.get("link_id"))
                            for entry in context.bottleneck_candidates
                            if entry.get("link_id")
                        ],
                    ),
                }
                for item in feasible
            ],
        }

    async def replan(
        self,
        spec: IntentSpec,
        context: NetworkContext,
        *,
        parent_plan_id: str | None,
        current_path: list[str],
        excluded_paths: list[list[str]] | None = None,
        allowed_paths: list[list[str]] | None = None,
        reason: str = "INTENT_VIOLATION",
    ) -> tuple[IntentPlanIR, AgentDecision]:
        """Create a new formal PLAN generation for a violation.

        Replanning never mutates the intent revision and never treats a legacy
        IPLAN identifier as the new transaction key.  Identical and previously
        tried paths are rejected before deterministic validation.
        """
        # Preserve the public ``plan`` override point used by integrations that
        # inject candidate generation.  The built-in path excludes attempted
        # paths before its single LLM selection.  An injected planner's candidates
        # are re-ranked deterministically because its LLM semantics are unknown.
        has_injected_plan = "plan" in self.__dict__ or type(self).plan is not PlanningAgent.plan
        base_plan = None
        decision = None
        if has_injected_plan:
            base_plan, decision = await self.plan(spec, context)
            generated_candidates = list(base_plan.candidates)
        else:
            errors = spec.plannability_errors()
            if errors:
                raise IntentNotPlannableError("; ".join(errors))
            generated_candidates = self.generator.generate(spec, context)
        excluded = {tuple(str(node) for node in item) for item in excluded_paths or []}
        allowed = {
            tuple(str(node) for node in item) for item in allowed_paths or []
        }
        if current_path:
            excluded.add(tuple(str(node) for node in current_path))
        candidates: list[CandidatePlan] = []
        for item in generated_candidates:
            if item.feasible and tuple(str(node) for node in item.path) in excluded:
                item = item.model_copy(
                    update={
                        "feasible": False,
                        "rejection_reason": "identical or previously attempted path",
                        "rejection_reasons": [
                            *item.rejection_reasons,
                            "identical or previously attempted path",
                        ],
                    }
                )
            if (
                item.feasible
                and allowed
                and tuple(str(node) for node in item.path) not in allowed
            ):
                item = item.model_copy(
                    update={
                        "feasible": False,
                        "rejection_reason": "rejected by HealingActionApplicabilityValidator",
                        "rejection_reasons": [
                            *item.rejection_reasons,
                            "rejected by HealingActionApplicabilityValidator",
                        ],
                    }
                )
            candidates.append(item)
        if has_injected_plan:
            eligible = [item for item in candidates if item.feasible]
            selected_candidate = max(eligible, key=lambda item: item.score) if eligible else None
            plan = base_plan.model_copy(
                update={
                    "candidates": candidates,
                    "selected_candidate_id": (
                        selected_candidate.candidate_id if selected_candidate else None
                    ),
                }
            )
        else:
            plan, decision = await self._build_plan(
                spec,
                context,
                candidates,
                decision_type="REPLAN_CANDIDATE_SELECTION",
            )
        selected = next(
            (
                item
                for item in candidates
                if item.candidate_id == plan.selected_candidate_id
            ),
            None,
        )
        plan = plan.model_copy(
            update={
                "parent_plan_id": parent_plan_id,
                "replan_reason": reason,
                "candidates": candidates,
                "selected_reasoning": (
                    [
                        "violation-driven replan selected a different untried path",
                        *plan.selected_reasoning,
                    ]
                    if selected
                    else ["no different untried feasible path remains"]
                ),
            }
        )
        plan = self.validator.validate(spec, plan)
        new_path = list(selected.path) if selected else []
        old_edge_set = set(path_edges(current_path))
        new_edge_set = set(path_edges(new_path))
        path_changed = bool(new_path) and list(current_path) != new_path
        plan.plan_diff = PlanDiff(
            parent_plan_id=parent_plan_id,
            new_plan_id=plan.plan_id,
            old_path=list(current_path),
            new_path=new_path,
            changed_edges=sorted(old_edge_set ^ new_edge_set),
            shared_edges=sorted(old_edge_set & new_edge_set),
            resources_added=[
                {"type": "PATH_RULE_SET", "path": new_path},
                {"type": "PATH_COUPLED_QOS", "path": new_path},
            ]
            if path_changed
            else [],
            resources_updated=[
                {
                    "type": "INTENT_ACTIVE_GENERATION",
                    "from_plan_id": parent_plan_id,
                    "to_plan_id": plan.plan_id,
                }
            ]
            if path_changed
            else [],
            resources_removed=[
                {"type": "SUPERSEDED_PATH_RULE_SET", "path": list(current_path)}
            ]
            if current_path and path_changed
            else [],
            path_changed=path_changed,
        )
        plan.effect_signature = PlanDiff.effect_signature(PlanDiff.effect_payload(plan))
        if plan.plan_diff.is_noop():
            plan = plan.model_copy(
                update={
                    "selected_candidate_id": None,
                    "required_actions": [],
                    "expected_outcomes": {},
                    "validation_status": PlanValidationStatus.UNSATISFIABLE,
                    "validation_errors": [
                        *plan.validation_errors,
                        "NO_OP_PLAN: selected plan has no data-plane delta",
                    ],
                    "selected_reasoning": [
                        *plan.selected_reasoning,
                        "PlanDiff.is_noop rejected deployment",
                    ],
                }
            )
        decision.plan_id = plan.plan_id
        decision.output.update(
            {
                "selected_candidate_id": plan.selected_candidate_id,
                "validation_status": plan.validation_status.value,
                "parent_plan_id": parent_plan_id,
                "replan_reason": reason,
                "plan_diff": plan.plan_diff.model_dump(mode="json"),
            }
        )
        decision.facts = list(plan.selected_reasoning)
        decision.alternatives = [item.model_dump(mode="json") for item in candidates]
        decision.rejected_alternatives = [
            item.model_dump(mode="json")
            for item in candidates
            if item.candidate_id != plan.selected_candidate_id
        ]
        return plan, decision

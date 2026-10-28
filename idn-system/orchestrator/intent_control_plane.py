"""Top-level intent lifecycle, assurance and bounded healing orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from agents.intent_assurance import IntentAssuranceAgent
from agents.intent_execution import IntentExecutionAgent
from agents.experience_memory import ExperienceMemoryAgent
from agents.intent_planning import PlanningAgent
from agents.intent_root_cause import IntentRootCauseAgent
from agents.intent_understanding import IntentUnderstandingAgent
from agents.healing_action_applicability import (
    CandidateActionGenerator,
    FeasibilityEvaluator,
    HealingActionApplicabilityValidator,
)
from api.store import IntentStore, get_store
from infra.network_context_builder import NetworkContextBuilder
from models.intent_domain import (
    AffectedIntentAnalysis,
    AgentDecision,
    AssessmentStatus,
    CandidatePlan,
    EvidenceItem,
    EvidenceQuality,
    ExecutionStatus,
    HealingAction,
    HealingActionApplicability,
    HealingActionType,
    HealingAttempt,
    HealingAttemptStatus,
    HealingAttemptType,
    HealingProposalStatus,
    IntentConflict,
    IntentExecutionResult,
    IntentLifecycle,
    IntentAssuranceResult,
    MigrationProof,
    ObjectiveAssessment,
    ObjectiveType,
    IntentPlanIR,
    IntentRevision,
    IntentRuntimeState,
    IntentSpec,
    PlanDiff,
    PlanValidationStatus,
    PlanningRiskStatus,
    PreferenceRelation,
    RequiredAction,
    RevisionStatus,
    RootCauseCategory,
)
from persistence.capability_repository import CapabilityRepository
from persistence.intent_domain_store import IntentDomainStore, get_intent_domain_store
from persistence.preference_relation_store import PreferenceRelationStore, preference_scope_key
from services.formal_assurance_evidence import FormalAssuranceEvidenceService
from services.preference_relations import (
    PreferenceCycleError,
    path_preference_projection,
    scheduler_projection,
    validate_acyclic,
)

logger = logging.getLogger(__name__)


class IntentControlPlane:
    """Coordinates agents while deterministic components retain execution authority."""

    def __init__(
        self,
        store: IntentStore | None = None,
        domain: IntentDomainStore | None = None,
    ):
        self.store = store or get_store()
        self.domain = domain or get_intent_domain_store()
        self.capabilities = CapabilityRepository(self.store)
        self.preference_relations = PreferenceRelationStore(self.store)
        self.understanding = IntentUnderstandingAgent()
        self.context_builder = NetworkContextBuilder(self.store)
        self.planning = PlanningAgent(capabilities=self.capabilities)
        self.execution = IntentExecutionAgent()
        self.assurance = IntentAssuranceAgent()
        self.formal_evidence = FormalAssuranceEvidenceService(self.domain)
        self.root_cause = IntentRootCauseAgent()
        self.healing_action_generator = CandidateActionGenerator()
        self.healing_action_validator = HealingActionApplicabilityValidator()
        self.feasibility = FeasibilityEvaluator()
        self.experience = ExperienceMemoryAgent(self.store)
        self.failure_threshold = max(
            1, int(os.getenv("IDN_INTENT_FAILURE_THRESHOLD", "2"))
        )
        self.pass_threshold = max(1, int(os.getenv("IDN_INTENT_PASS_THRESHOLD", "1")))
        self.cooldown_seconds = max(
            0, int(os.getenv("IDN_INTENT_HEALING_COOLDOWN_SECONDS", "60"))
        )
        self.grace_seconds = max(0, int(os.getenv("IDN_INTENT_GRACE_SECONDS", "5")))
        self.low_confidence_threshold = float(
            os.getenv("IDN_HEALING_LOW_CONFIDENCE_THRESHOLD", "0.4")
        )
        self.high_confidence_threshold = float(
            os.getenv("IDN_HEALING_HIGH_CONFIDENCE_THRESHOLD", "0.75")
        )
        self._intent_locks: dict[str, asyncio.Lock] = {}

    def register_received(
        self,
        *,
        intent_id: str,
        text: str,
        priority: str,
        context: dict[str, Any] | None = None,
    ) -> IntentSpec:
        existing = self.domain.get_spec(intent_id)
        if existing:
            runtime = self.domain.get_runtime(intent_id)
            if runtime is None:
                self.domain.save_runtime(
                    IntentRuntimeState(
                        intent_id=intent_id,
                        revision=existing.revision,
                        current_revision=existing.revision,
                        lifecycle=existing.lifecycle,
                    )
                )
            if self.domain.get_revision(intent_id, existing.revision) is None:
                self.domain.save_revision(
                    IntentRevision(
                        intent_id=intent_id,
                        revision=existing.revision,
                        status=RevisionStatus.ACTIVE,
                        spec_snapshot=existing.model_dump(mode="json"),
                        actor="intent_api",
                        reason="backfilled active revision metadata",
                    )
                )
            return existing
        draft = IntentSpec(
            intent_id=intent_id,
            original_text=text,
            normalized_text=" ".join(text.split()),
            priority=priority,
            tenant_id=str((context or {}).get("tenant_id") or "default"),
            clarification_required=True,
            clarification_questions=["Intent understanding has not completed yet."],
            lifecycle=IntentLifecycle.RECEIVED,
        )
        self.domain.save_spec(draft)
        self.domain.save_runtime(IntentRuntimeState(intent_id=intent_id))
        self.domain.save_revision(
            IntentRevision(
                intent_id=intent_id,
                revision=1,
                status=RevisionStatus.ACTIVE,
                spec_snapshot=draft.model_dump(mode="json"),
                actor="intent_api",
                reason="initial revision accepted",
            )
        )
        self.domain.transition(
            intent_id,
            IntentLifecycle.RECEIVED,
            actor="intent_api",
            reason="natural-language intent accepted",
        )
        return draft

    def resume_after_clarification(self, intent_id: str) -> IntentRuntimeState:
        """Formally move a user-answered conversational intent back to work.

        The API worker may take a moment to claim the queued continuation.  By
        committing this lifecycle transition before enqueueing, both the
        current-intent projection and a reload truthfully show that the same
        Intent is being processed rather than leaving it in a stale waiting
        state until the whole workflow eventually completes.
        """
        runtime = self.domain.get_runtime(intent_id)
        if runtime is None:
            raise RuntimeError("INTENT_RUNTIME_NOT_FOUND")
        if runtime.lifecycle == IntentLifecycle.NEEDS_CLARIFICATION:
            return self.domain.transition(
                intent_id,
                IntentLifecycle.UNDERSTANDING,
                actor="conversation_clarification",
                reason="user supplied clarification; resume same intent",
            )
        if runtime.lifecycle == IntentLifecycle.UNDERSTANDING:
            return runtime
        raise RuntimeError(f"INTENT_NOT_AWAITING_CLARIFICATION:{runtime.lifecycle.value}")

    async def ingest_pipeline_result(
        self,
        *,
        intent_id: str,
        legacy_status: str,
        result: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> IntentRuntimeState:
        async with self._lock(intent_id):
            record = self._require_record(intent_id)
            current_spec = self.domain.get_spec(intent_id)
            revision = current_spec.revision if current_spec else 1
            self.domain.transition(
                intent_id,
                IntentLifecycle.UNDERSTANDING,
                actor="intent_understanding",
                reason="converting parser output to IntentSpec",
            )
            understanding_result = self.understanding.understand_result(
                intent_id=intent_id,
                text=record.get("execution_text") or record["text"],
                legacy_plan=result.get("intent_plan"),
                priority=record.get("priority") or "medium",
                context=record.get("context") or {},
                revision=revision,
            )
            spec = understanding_result.intent_spec
            self.domain.save_spec(spec)
            self.domain.save_decision(
                AgentDecision(
                    intent_id=intent_id,
                    agent_id="intent_understanding",
                    decision_type="INTENT_SPEC_EXTRACTION",
                    inputs={"original_text": spec.original_text},
                    output=understanding_result.model_dump(mode="json"),
                    facts=list(spec.assumptions),
                    llm_status=spec.llm_status,
                    llm_runtime_mode=spec.llm_runtime_mode,
                    llm_call_id=spec.llm_call_id,
                    llm_provider=spec.llm_provider,
                    llm_model=spec.llm_model,
                    llm_structured_output_valid=spec.llm_structured_output_valid,
                    llm_validator_result=spec.llm_validator_result,
                    fallback_used=spec.fallback_used,
                )
            )
            if spec.preference_validation_error:
                for lifecycle, actor, reason in (
                    (IntentLifecycle.PARSED, "intent_understanding", "self preference parsed"),
                    (IntentLifecycle.SENSING, "network_context_builder", "semantic-only preference context captured"),
                    (IntentLifecycle.PLANNING, "preference_scheduler", "self preference validation started"),
                ):
                    self.domain.transition(intent_id, lifecycle, actor=actor, reason=reason,
                        data={"execution_mode": "SEMANTIC_ONLY", "southbound_execution_permitted": False})
                return self.domain.transition(
                    intent_id, IntentLifecycle.UNSATISFIABLE, actor="preference_scheduler",
                    reason=spec.preference_validation_error,
                    data={"southbound_execution_permitted": False, "p4_writes": 0, "tc_writes": 0, "onos_writes": 0},
                )
            # Do not materialize a scheduler relation until its preference
            # scope is complete; a path request awaiting clarification has no
            # valid owner or endpoint pair yet.
            if spec.clarification_required or legacy_status == "pending_clarification":
                return self.domain.transition(
                    intent_id,
                    IntentLifecycle.NEEDS_CLARIFICATION,
                    actor="intent_understanding",
                    reason="required semantic fields are missing",
                    data={"questions": spec.clarification_questions},
                )
            if spec.preferences:
                try:
                    self._validate_preference_relations(spec)
                    projection = scheduler_projection(spec.preferences)
                    relation_results = [
                        self.preference_relations.apply(
                            intent_id=intent_id,
                            intent_revision=spec.revision,
                            relation=relation,
                            projection=projection,
                        )
                        for relation in spec.preferences
                    ]
                    self.domain.save_decision(
                        AgentDecision(
                            intent_id=intent_id,
                            intent_revision=spec.revision,
                            agent_id="preference_scheduler",
                            decision_type="PREFERENCE_RELATION_PROJECTION",
                            inputs={"relations": [item.model_dump(mode="json") for item in spec.preferences]},
                            output={
                                "projection": projection,
                                "relation_results": relation_results,
                                "execution_capability": "CONTROL_PLANE_SCHEDULING_PREFERENCE",
                                "southbound_mutation_required": False,
                            },
                            facts=["preference is a soft business ordering; SLA and queue contracts are unchanged"],
                            rationale_summary="validated acyclic preference relation and projected scheduler rank only",
                        )
                    )
                except PreferenceCycleError as exc:
                    self.domain.save_conflict(IntentConflict(
                        intent_id=intent_id,
                        conflict_type=exc.code,
                        severity="high",
                        facts=[str(exc)],
                        resolutions=[{"type": "REJECT_NEW_RELATION"}],
                        status="OPEN",
                    ))
                    # A directed cycle cannot be made valid by an approval.
                    # Preserve the completed semantic gates, then end in the
                    # project's formal terminal UNSATISFIABLE state before
                    # conflict approval, compilation or execution can begin.
                    for lifecycle, actor, reason in (
                        (IntentLifecycle.PARSED, "intent_understanding", "preference relations extracted"),
                        (IntentLifecycle.SENSING, "network_context_builder", "semantic-only preference context captured"),
                        (IntentLifecycle.PLANNING, "intent_planning", "preference DAG validation started"),
                    ):
                        self.domain.transition(
                            intent_id, lifecycle, actor=actor, reason=reason,
                            data={"execution_mode": "SEMANTIC_ONLY", "southbound_execution_permitted": False},
                        )
                    return self.domain.transition(
                        intent_id,
                        IntentLifecycle.UNSATISFIABLE,
                        actor="conflict_safety",
                        reason=exc.code,
                        data={"dimension": exc.dimension.value, "cycle": exc.cycle, "southbound_execution_permitted": False},
                    )
            if not spec.is_plannable():
                return self.domain.transition(
                    intent_id,
                    IntentLifecycle.NEEDS_CLARIFICATION,
                    actor="intent_understanding",
                    reason="IntentSpec failed the planning gate",
                    data={"errors": spec.plannability_errors()},
                )
            if legacy_status == "pending_approval":
                return self.domain.transition(
                    intent_id,
                    IntentLifecycle.NEEDS_APPROVAL,
                    actor="conflict_safety",
                    reason="legacy safety council requires operator approval",
                )
            if spec.preferences and not spec.objectives:
                return await self._complete_preference_runtime(
                    intent_id=intent_id,
                    spec=spec,
                    relation_results=relation_results,
                )
            if legacy_status == "semantic_complete":
                # Explicit parse-only requests remain parse-only.  Preference
                # requests use the auditable control-plane completion above.
                for lifecycle, actor, reason in (
                    (IntentLifecycle.PARSED, "intent_understanding", "semantic IntentSpec validated"),
                    (IntentLifecycle.SENSING, "network_context_builder", "semantic decomposition context captured"),
                    (IntentLifecycle.PLANNING, "intent_planning", "real LLM IntentPlan decomposition completed"),
                    (IntentLifecycle.PLAN_READY, "plan_validator", "SEMANTIC_ONLY request ends before conflict, compiler and execution"),
                ):
                    self.domain.transition(intent_id, lifecycle, actor=actor, reason=reason, data={"execution_mode": "SEMANTIC_ONLY", "southbound_execution_permitted": False})
                return self.domain.get_runtime(intent_id)
            if legacy_status == "unsatisfiable":
                # The legacy graph has already completed sensing/planning.
                # Mirror those completed lifecycle gates before recording its
                # bounded UNSATISFIABLE conclusion; jumping directly from
                # UNDERSTANDING violates the authoritative state machine and
                # leaves the formal audit trail incomplete.
                self.domain.transition(
                    intent_id,
                    IntentLifecycle.PARSED,
                    actor="intent_understanding",
                    reason="legacy IntentSpec validated",
                )
                self.domain.transition(
                    intent_id,
                    IntentLifecycle.SENSING,
                    actor="network_context_builder",
                    reason="legacy workflow completed network sensing",
                )
                self.domain.transition(
                    intent_id,
                    IntentLifecycle.PLANNING,
                    actor="intent_planning",
                    reason="legacy workflow completed feasibility planning",
                )
                return self.domain.transition(
                    intent_id,
                    IntentLifecycle.UNSATISFIABLE,
                    actor="healing_controller",
                    reason=str(
                        snapshot.get("error_message")
                        or (result.get("verification_report") or {}).get("terminal_reason")
                        or "NO_FEASIBLE_HEALING_ACTION"
                    ),
                    data={
                        "legacy_status": legacy_status,
                        "verification_report": result.get("verification_report") or {},
                    },
                )
            # The legacy workflow historically used ``failed`` for both a
            # southbound install failure and a post-install KPI violation.
            # An active persisted policy proves the latter must enter formal
            # assurance as ASSURANCE_FAILED/DEGRADED, not be mislabeled as execution
            # failure.
            active_policy_ids = self._policy_ids(intent_id)
            if legacy_status == "failed" and self._legacy_kpi_violation(result):
                return await self._ingest_legacy_violation(
                    spec=spec,
                    result=result,
                    snapshot=snapshot,
                    active_policy_ids=active_policy_ids,
                )
            if legacy_status == "failed" and not active_policy_ids:
                # The legacy result identifies a southbound deployment
                # failure. Mirror the already-completed gates so the durable
                # transition is both legal and stage-accurate.
                for lifecycle, actor, reason in (
                    (
                        IntentLifecycle.PARSED,
                        "intent_understanding",
                        "legacy IntentSpec validated",
                    ),
                    (
                        IntentLifecycle.SENSING,
                        "network_context_builder",
                        "legacy workflow completed network sensing",
                    ),
                    (
                        IntentLifecycle.PLANNING,
                        "intent_planning",
                        "legacy workflow completed feasibility planning",
                    ),
                    (
                        IntentLifecycle.PLAN_READY,
                        "plan_validator",
                        "legacy workflow produced an executable plan",
                    ),
                    (
                        IntentLifecycle.CHECKING,
                        "conflict_safety",
                        "legacy workflow passed conflict checks",
                    ),
                    (
                        IntentLifecycle.DEPLOYING,
                        "execution_agent",
                        "legacy workflow attempted southbound deployment",
                    ),
                ):
                    self.domain.transition(
                        intent_id,
                        lifecycle,
                        actor=actor,
                        reason=reason,
                    )
                return self.domain.transition(
                    intent_id,
                    IntentLifecycle.EXECUTION_FAILED,
                    actor="execution_agent",
                    reason=str(
                        snapshot.get("error_message")
                        or "legacy deployment pipeline failed"
                    ),
                )
            self.domain.transition(
                intent_id,
                IntentLifecycle.PARSED,
                actor="intent_understanding",
                reason="IntentSpec validated",
            )
            self.domain.transition(
                intent_id,
                IntentLifecycle.SENSING,
                actor="network_context_builder",
                reason="building versioned network context",
            )
            context = await self.context_builder.build_async()
            self.domain.save_context(intent_id, context)
            self.domain.transition(
                intent_id,
                IntentLifecycle.PLANNING,
                actor="intent_planning",
                reason="generating and evaluating feasible candidates",
                context_id=context.context_id,
            )
            plan, decision = await self.planning.plan(spec, context)
            context.candidate_paths = [
                item.model_dump(mode="json") for item in plan.candidates
            ]
            self.domain.save_context(intent_id, context)
            self.domain.save_plan(plan)
            self.domain.save_decision(decision)
            if plan.validation_status != PlanValidationStatus.VALID:
                return self.domain.transition(
                    intent_id,
                    IntentLifecycle.UNSATISFIABLE,
                    actor="plan_validator",
                    reason="; ".join(plan.validation_errors) or "no feasible candidate",
                    plan_id=plan.plan_id,
                    context_id=context.context_id,
                )
            self.domain.transition(
                intent_id,
                IntentLifecycle.PLAN_READY,
                actor="plan_validator",
                reason="selected plan passed deterministic validation",
                plan_id=plan.plan_id,
                context_id=context.context_id,
            )
            conflicts = self._check_conflicts(spec)
            for conflict in conflicts:
                self.domain.save_conflict(conflict)
            blocking = [
                item
                for item in conflicts
                if item.status == "OPEN" and item.severity in {"high", "critical"}
            ]
            if blocking:
                return self.domain.transition(
                    intent_id,
                    IntentLifecycle.NEEDS_APPROVAL,
                    actor="conflict_safety",
                    reason="a blocking multi-intent conflict requires approval",
                    data={"conflict_ids": [item.conflict_id for item in blocking]},
                )
            self.domain.transition(
                intent_id,
                IntentLifecycle.CHECKING,
                actor="conflict_safety",
                reason="conflict and safety checks passed",
            )
            self.domain.transition(
                intent_id,
                IntentLifecycle.DEPLOYING,
                actor="execution_agent",
                reason="validated IntentPlan handed to deterministic executor",
            )
            runtime = self.domain.get_runtime(intent_id) or IntentRuntimeState(
                intent_id=intent_id
            )
            runtime.active_policy_ids = active_policy_ids
            self.domain.save_runtime(runtime)
            execution = await self._execute_plan_locked(plan)
            if execution.status != ExecutionStatus.COMPLETED:
                return self.domain.transition(
                    intent_id,
                    IntentLifecycle.EXECUTION_FAILED,
                    actor="execution_agent",
                    reason=execution.error
                    or "device configuration did not pass readback",
                    plan_id=plan.plan_id,
                    context_id=context.context_id,
                    data={"execution_id": execution.execution_id},
                )
            runtime = self.domain.get_runtime(intent_id) or runtime
            runtime.grace_until = datetime.now(timezone.utc) + timedelta(
                seconds=self.grace_seconds
            )
            self.domain.save_runtime(runtime)
            self.domain.transition(
                intent_id,
                IntentLifecycle.VERIFYING,
                actor="intent_assurance",
                reason="deployment success requires independent objective evidence",
            )
            return await self._assess_locked(intent_id, spec=spec, plan=plan)

    @staticmethod
    def _legacy_kpi_violation(result: dict[str, Any]) -> bool:
        verification = result.get("status_verification_report") or {}
        execution = result.get("execution_result") or {}
        return bool(
            verification
            and verification.get("passed") is False
            and str(verification.get("verification_state") or "").upper()
            in {"KPI_FAILED", "INTENT_VIOLATED", "FAILED"}
            and str(execution.get("status") or "").upper() == "SUCCESS"
        )

    async def _ingest_legacy_violation(
        self,
        *,
        spec: IntentSpec,
        result: dict[str, Any],
        snapshot: dict[str, Any],
        active_policy_ids: list[str],
    ) -> IntentRuntimeState:
        """Import a post-install KPI failure as a formal violated baseline."""
        intent_id = spec.intent_id
        self.domain.transition(
            intent_id,
            IntentLifecycle.PARSED,
            actor="intent_understanding",
            reason="IntentSpec validated; legacy KPI failure is not an install failure",
        )
        self.domain.transition(
            intent_id,
            IntentLifecycle.SENSING,
            actor="network_context_builder",
            reason="capturing the violated deployed state",
        )
        context = await self.context_builder.build_async()
        current_path = self._legacy_current_path(spec, result)
        context.current_paths[intent_id] = list(current_path)
        self.domain.save_context(intent_id, context)
        self.domain.transition(
            intent_id,
            IntentLifecycle.PLANNING,
            actor="intent_planning",
            reason="importing the observed legacy deployment as a formal source plan",
            context_id=context.context_id,
        )
        verification = result.get("status_verification_report") or {}
        kpi = verification.get("kpi") or {}
        throughput = self._first_number(
            kpi,
            "throughput_mbps",
            "received_mbps",
            "receiver_mbps",
            "bandwidth_mbps",
        )
        candidate = CandidatePlan(
            context_id=context.context_id,
            path=current_path,
            required_actions=[
                RequiredAction(
                    action_type="OBSERVE_IMPORTED_DEPLOYMENT",
                    target=intent_id,
                    parameters={"legacy": True},
                )
            ],
            expected_outcomes={"observed_throughput_mbps": throughput},
            empirical_capacity_mbps=throughput,
            estimated_latency_ms=self._first_number(kpi, "latency_ms"),
            score=0,
            feasible=True,
            risk_status=PlanningRiskStatus.DEGRADED_CONTEXT,
            deterministic_facts=[
                "formal source state imported from controller and TC readback",
                "the source deployment is configured but violates its SLA",
            ],
        )
        legacy_plan = result.get("intent_plan") or {}
        initial_plan = IntentPlanIR(
            intent_id=intent_id,
            intent_revision=spec.revision,
            context_id=context.context_id,
            candidates=[candidate],
            selected_candidate_id=candidate.candidate_id,
            selected_reasoning=["observed active primary path"],
            validation_status=PlanValidationStatus.VALID,
            required_actions=list(candidate.required_actions),
            expected_outcomes=dict(candidate.expected_outcomes),
            replan_reason="LEGACY_DEPLOYED_STATE_IMPORT",
            legacy_plan_id=str(legacy_plan.get("plan_id") or "") or None,
            llm_status="NOT_USED",
        )
        context.candidate_paths = [candidate.model_dump(mode="json")]
        self.domain.save_context(intent_id, context)
        self.domain.save_plan(initial_plan)
        self.domain.transition(
            intent_id,
            IntentLifecycle.PLAN_READY,
            actor="plan_validator",
            reason="observed source plan imported; SLA outcome remains unverified",
            plan_id=initial_plan.plan_id,
            context_id=context.context_id,
        )
        self.domain.transition(
            intent_id,
            IntentLifecycle.CHECKING,
            actor="conflict_safety",
            reason="source-state import performs no new mutation",
        )
        self.domain.transition(
            intent_id,
            IntentLifecycle.DEPLOYING,
            actor="intent_execution",
            reason="recording the already deployed source generation",
        )
        switch_path = current_path[1:-1] if len(current_path) > 2 else current_path
        source_execution = IntentExecutionResult(
            intent_id=intent_id,
            plan_id=initial_plan.plan_id,
            context_id=context.context_id,
            selected_candidate_id=candidate.candidate_id,
            selected_path=current_path,
            status=ExecutionStatus.COMPLETED,
            config_verified=True,
            commit_verified=True,
            migration_verified=False,
            device_config_receipts=[
                {
                    "resource": "LEGACY_OBSERVED_PATH",
                    "status": "VERIFIED",
                    "observed_path": switch_path,
                }
            ],
            migration_proofs=[
                MigrationProof(
                    source="TC_QOS_BINDING_READBACK",
                    expected_path=switch_path,
                    observed_path=switch_path,
                    passed=bool(switch_path),
                    evidence_refs=["legacy.status_verification.config_verification"],
                )
            ],
            phase_history=[
                {
                    "status": ExecutionStatus.COMPLETED.value,
                    "reason": "imported observed legacy source generation",
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            ],
            completed_at=datetime.now(timezone.utc),
        )
        self.domain.save_execution(source_execution)
        runtime = self.domain.get_runtime(intent_id) or IntentRuntimeState(
            intent_id=intent_id
        )
        runtime.active_plan_id = initial_plan.plan_id
        runtime.initial_plan_id = initial_plan.plan_id
        runtime.active_execution_id = source_execution.execution_id
        runtime.active_policy_ids = list(active_policy_ids)
        runtime.current_path = list(current_path)
        runtime.context_id = context.context_id
        desired = result.get("yang_policies") or {}
        if isinstance(desired, list):
            desired = desired[0] if desired else {}
        runtime.desired_policy = dict(desired) if isinstance(desired, dict) else {}
        self.domain.save_runtime(runtime)
        self.domain.transition(
            intent_id,
            IntentLifecycle.VERIFYING,
            actor="intent_assurance",
            reason="converting legacy KPI evidence into a formal assurance result",
            data={"execution_id": source_execution.execution_id},
        )
        assurance, evidence = self._legacy_assurance(
            spec,
            initial_plan,
            source_execution,
            context.context_id,
            verification,
        )
        for item in evidence:
            self.domain.save_evidence(intent_id, item)
        self.domain.save_assurance(assurance)
        runtime = self.domain.get_runtime(intent_id) or runtime
        runtime.last_assurance_id = assurance.assurance_id
        runtime.assurance_status = IntentLifecycle.ASSURANCE_FAILED.value
        runtime.last_assurance_at = assurance.observed_at
        runtime.last_violation_at = assurance.observed_at
        runtime.violation_count += 1
        runtime.consecutive_failures = max(
            self.failure_threshold,
            int((snapshot.get("react_state") or {}).get("retry_count") or 1),
        )
        runtime.objective_results = [
            item.model_dump(mode="json") for item in assurance.assessments
        ]
        runtime.evidence_refs = [item.evidence_id for item in evidence]
        runtime.observed_metrics = self._runtime_observed_metrics(assurance, evidence)
        self.domain.save_runtime(runtime)
        runtime = self.domain.transition(
            intent_id,
            IntentLifecycle.ASSURANCE_FAILED,
            actor="violation_detector",
            reason=assurance.summary,
            data={
                "assurance_id": assurance.assurance_id,
                "legacy_plan_id": initial_plan.legacy_plan_id,
                "source_execution_id": source_execution.execution_id,
                "failure_reason": "SLA_VIOLATED",
                "failure_stage": "ASSURANCE",
                "recoverable": True,
            },
        )
        await self._diagnose_and_heal(spec, runtime, assurance, evidence)
        return self.domain.get_runtime(intent_id) or runtime

    def _legacy_assurance(
        self,
        spec: IntentSpec,
        plan: IntentPlanIR,
        execution: IntentExecutionResult,
        context_id: str,
        verification: dict[str, Any],
    ) -> tuple[IntentAssuranceResult, list[EvidenceItem]]:
        kpi = verification.get("kpi") or {}
        telemetry_source = str(verification.get("telemetry_source") or "legacy_probe")
        quality = (
            EvidenceQuality.DEGRADED
            if "demo" in telemetry_source.lower()
            else EvidenceQuality.TRUSTED
        )
        metric_map = {
            "MIN_BANDWIDTH": (
                "throughput_mbps",
                self._first_number(
                    kpi,
                    "throughput_mbps",
                    "received_mbps",
                    "receiver_mbps",
                    "bandwidth_mbps",
                ),
            ),
            "MAX_BANDWIDTH": (
                "throughput_mbps",
                self._first_number(kpi, "throughput_mbps", "received_mbps"),
            ),
            "MAX_LATENCY": ("latency_ms", self._first_number(kpi, "latency_ms")),
            "MAX_PACKET_LOSS": (
                "packet_loss_rate",
                self._first_number(kpi, "packet_loss_rate", "loss_percent"),
            ),
            "MAX_JITTER": ("jitter_ms", self._first_number(kpi, "jitter_ms")),
            "CONNECTIVITY": ("connectivity", True),
        }
        evidence: list[EvidenceItem] = []
        assessments: list[ObjectiveAssessment] = []
        for objective in spec.objectives:
            metric, observed = metric_map.get(
                objective.type.value, (objective.type.value.lower(), None)
            )
            item = EvidenceItem(
                intent_id=spec.intent_id,
                objective_id=objective.objective_id,
                source=telemetry_source,
                metric=metric,
                value=observed,
                unit=objective.unit,
                quality=quality if observed is not None else EvidenceQuality.MISSING,
                sampled_at=datetime.now(timezone.utc),
                correlation={
                    "plan_id": plan.plan_id,
                    "execution_id": execution.execution_id,
                    "context_id": context_id,
                },
                raw={
                    "verification_state": verification.get("verification_state"),
                    "p4_status": "VERIFIED",
                    "tc_status": "VERIFIED",
                },
            )
            evidence.append(item)
            passed = (
                observed is not None
                and self.assurance._compare(
                    observed,
                    objective.operator,
                    objective.value,
                )
            )
            status = (
                AssessmentStatus.PASS
                if passed
                else AssessmentStatus.FAIL
                if observed is not None
                else AssessmentStatus.UNKNOWN
            )
            assessments.append(
                ObjectiveAssessment(
                    objective_id=objective.objective_id,
                    objective_type=objective.type,
                    status=status,
                    expected=objective.value,
                    observed=observed,
                    unit=objective.unit,
                    evidence_ids=[item.evidence_id],
                    reason=(
                        f"legacy measured {observed} {objective.unit or ''}; expected "
                        f"{objective.operator.value} {objective.value}"
                    ),
                )
            )
        return (
            IntentAssuranceResult(
                intent_id=spec.intent_id,
                plan_id=plan.plan_id,
                context_id=context_id,
                execution_id=execution.execution_id,
                lifecycle=IntentLifecycle.ASSURANCE_FAILED,
                evidence_quality=quality,
                assessments=assessments,
                satisfied=False,
                summary="legacy deployment readback passed but measured KPI violated the intent",
                failure_reason="SLA_VIOLATED",
                failure_stage="ASSURANCE",
                recoverable=True,
                consecutive_failures=max(self.failure_threshold, 1),
            ),
            evidence,
        )

    @staticmethod
    def _legacy_current_path(spec: IntentSpec, result: dict[str, Any]) -> list[str]:
        execution = result.get("execution_result") or {}
        binding = (execution.get("qos_enforcement") or {}).get("qos_binding") or {}
        switches = [str(item) for item in binding.get("current_path") or []]
        if not switches:
            verification = result.get("status_verification_report") or {}
            config = verification.get("config_verification") or {}
            binding = (
                (config.get("switch_egress_qos") or {}).get("qos_binding") or {}
            )
            switches = [str(item) for item in binding.get("current_path") or []]
        source = str(spec.source.get("endpoint_id") or spec.source.get("hostname") or "")
        destination = str(
            spec.destination.get("endpoint_id")
            or spec.destination.get("hostname")
            or ""
        )
        return [item for item in [source, *switches, destination] if item]

    @staticmethod
    def _first_number(payload: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    @staticmethod
    def _runtime_observed_metrics(
        assurance: IntentAssuranceResult,
        evidence: list[EvidenceItem],
    ) -> dict[str, Any]:
        """Project metrics without contradicting the final objective verdict."""
        observed = {
            item.metric: item.value
            for item in evidence
            if item.value is not None
            and item.quality in {EvidenceQuality.TRUSTED, EvidenceQuality.DEGRADED}
        }
        metric_by_objective = {
            ObjectiveType.CONNECTIVITY: "connectivity",
            ObjectiveType.MIN_BANDWIDTH: "throughput_mbps",
            ObjectiveType.MAX_BANDWIDTH: "throughput_mbps",
            ObjectiveType.MAX_LATENCY: "latency_ms",
            ObjectiveType.MAX_PACKET_LOSS: "packet_loss_pct",
            ObjectiveType.MAX_JITTER: "jitter_ms",
            ObjectiveType.AVAILABILITY: "availability_pct",
            ObjectiveType.PRIORITY: "priority",
            ObjectiveType.ISOLATION: "isolation",
        }
        for assessment in assurance.assessments:
            metric = metric_by_objective.get(assessment.objective_type)
            if metric and assessment.observed is not None:
                observed[metric] = assessment.observed
        return observed

    async def reevaluate(self, intent_id: str) -> IntentRuntimeState:
        async with self._lock(intent_id):
            current = self.domain.get_runtime(intent_id)
            if current and current.lifecycle in {
                IntentLifecycle.UNDERSTANDING,
                IntentLifecycle.PARSED,
                IntentLifecycle.SENSING,
                IntentLifecycle.PLANNING,
                IntentLifecycle.PLAN_READY,
                IntentLifecycle.CHECKING,
                IntentLifecycle.DEPLOYING,
                IntentLifecycle.DIAGNOSING,
                IntentLifecycle.REPLANNING,
                IntentLifecycle.REDEPLOYING,
            }:
                # A periodic/manual reevaluation may have selected this row
                # before another coroutine entered the healing transaction.
                # Once the per-intent lock is acquired, re-check lifecycle and
                # never overwrite an in-flight planning/deployment state with
                # a stale assurance result.
                return current
            # A registered formal batch is authoritative for its exact
            # generation.  Never discard it and synthesize a fresh telemetry
            # result merely because ordinary probes have no historical data.
            if self.formal_evidence.repository.batches(intent_id):
                return await self._formal_reevaluate_locked(intent_id)
            runtime = await self._assess_locked(intent_id)
            # Missing/stale evidence is UNKNOWN, never PASS.  It is also one
            # of the bounded event triggers for a heavyweight active probe.
            if (
                runtime.lifecycle == IntentLifecycle.DEGRADED
                and runtime.assurance_status == "UNKNOWN"
            ):
                spec = self._require_spec(intent_id)
                await self._trigger_service_validation(spec)
                return await self._assess_locked(intent_id, spec=spec)
            return runtime

    async def formal_reevaluate(self, intent_id: str) -> IntentRuntimeState:
        """Assess registered evidence only; no probe, LLM, planner, or southbound I/O."""
        async with self._lock(intent_id):
            return await self._formal_reevaluate_locked(intent_id)

    async def _formal_reevaluate_locked(self, intent_id: str) -> IntentRuntimeState:
        runtime = self.domain.get_runtime(intent_id) or IntentRuntimeState(intent_id=intent_id)
        trace_id = f"FORMAL-ASSURANCE-{intent_id}"
        decision, batch = self.formal_evidence.assess_current(intent_id, trace_id=trace_id)
        committed = any(
            item.get("event_type") == "ASSURANCE_DECISION_COMMITTED"
            and (item.get("data") or {}).get("decision_id") == decision.decision_id
            for item in self.domain.history(intent_id)
        )
        if committed:
            return self.domain.get_runtime(intent_id) or runtime
        self.domain.record_event(
            intent_id, "ASSURANCE_REEVALUATION_STARTED", actor="formal_assurance",
            reason="evaluating current trusted evidence batch without dataplane activity",
            data={"evidence_batch_id": decision.evidence_batch_id, "trace_id": trace_id},
        )
        spec = self._require_spec(intent_id)
        objective = next((item for item in spec.objectives if item.type == ObjectiveType.MIN_BANDWIDTH), None)
        assessments = []
        if objective:
            assessments.append(ObjectiveAssessment(
                objective_id=objective.objective_id, objective_type=objective.type,
                status=AssessmentStatus.PASS if decision.status == "SATISFIED" else AssessmentStatus.FAIL if decision.status == "VIOLATED" else AssessmentStatus.UNKNOWN,
                expected=objective.value, observed=decision.minimum_received_mbps,
                unit=objective.unit, evidence_ids=[decision.evidence_batch_id] if decision.evidence_batch_id else [],
                reason=f"formal decision {decision.reason_code}; valid samples={decision.valid_sample_count}/{decision.required_sample_count}",
            ))
        assurance = IntentAssuranceResult(
            assurance_id=decision.decision_id, intent_id=intent_id,
            plan_id=runtime.active_plan_id, context_id=runtime.context_id,
            execution_id=runtime.active_execution_id,
            lifecycle=IntentLifecycle.SATISFIED if decision.status == "SATISFIED" else IntentLifecycle.ASSURANCE_FAILED if decision.status == "VIOLATED" else IntentLifecycle.DEGRADED,
            evidence_quality=EvidenceQuality.TRUSTED if batch else EvidenceQuality.MISSING,
            assessments=assessments, satisfied=True if decision.status == "SATISFIED" else False if decision.status == "VIOLATED" else None,
            summary=f"formal assurance: {decision.reason_code}",
            failure_reason=None if decision.status == "SATISFIED" else decision.reason_code,
            failure_stage=None if decision.status == "SATISFIED" else "ASSURANCE",
        )
        runtime.last_assurance_id = assurance.assurance_id
        runtime.assurance_status = decision.status
        runtime.last_assurance_at = assurance.observed_at
        runtime.objective_results = [item.model_dump(mode="json") for item in assessments]
        runtime.evidence_refs = [decision.evidence_batch_id] if decision.evidence_batch_id else []
        runtime.observed_metrics = {"throughput_mbps": decision.minimum_received_mbps, "mean_throughput_mbps": decision.mean_received_mbps}
        self.domain.save_assurance(assurance)
        self.domain.save_runtime(runtime)
        self.domain.record_event(
            intent_id, "ASSURANCE_DECISION_COMMITTED", actor="formal_assurance",
            reason=decision.reason_code,
            data={"evidence_batch_id": decision.evidence_batch_id, "decision_id": decision.decision_id, "reason_code": decision.reason_code, "trace_id": trace_id},
        )
        target = IntentLifecycle.SATISFIED if decision.status == "SATISFIED" else IntentLifecycle.ASSURANCE_FAILED if decision.status == "VIOLATED" else IntentLifecycle.DEGRADED
        if target == IntentLifecycle.SATISFIED:
            self.domain.record_event(intent_id, "INTENT_SATISFIED", actor="formal_assurance", reason=decision.reason_code, data={"evidence_batch_id": decision.evidence_batch_id, "decision_id": decision.decision_id, "trace_id": trace_id})
        return self.domain.transition(
            intent_id, target, actor="formal_assurance", reason=decision.reason_code,
            data={"evidence_batch_id": decision.evidence_batch_id, "decision_id": decision.decision_id, "reason_code": decision.reason_code, "trace_id": trace_id},
        )

    async def _assess_locked(
        self,
        intent_id: str,
        *,
        spec: IntentSpec | None = None,
        plan: IntentPlanIR | None = None,
        auto_heal: bool = True,
        force_violation: bool = False,
    ) -> IntentRuntimeState:
        spec = spec or self._require_spec(intent_id)
        runtime = self.domain.get_runtime(intent_id) or IntentRuntimeState(
            intent_id=intent_id
        )
        if runtime.lifecycle == IntentLifecycle.WITHDRAWN:
            return runtime
        if runtime.active_execution_id:
            active_execution = next(
                (
                    item
                    for item in self.domain.executions(intent_id)
                    if item.get("execution_id") == runtime.active_execution_id
                ),
                None,
            )
            if (
                active_execution
                and active_execution.get("status") == ExecutionStatus.COMPLETED.value
                and active_execution.get("config_verified") is True
            ):
                runtime.last_error = None
        if plan is None:
            raw = self.domain.plans(intent_id)
            plan = IntentPlanIR.model_validate(raw[0]) if raw else None
        assurance, evidence = self.assurance.assess(
            spec,
            plan_id=plan.plan_id if plan else runtime.active_plan_id,
            context_id=runtime.context_id,
        )
        assurance.execution_id = runtime.active_execution_id
        for item in evidence:
            self.domain.save_evidence(intent_id, item)
        now = datetime.now(timezone.utc)
        in_grace = bool(runtime.grace_until and runtime.grace_until > now)
        if assurance.satisfied is True:
            runtime.consecutive_passes += 1
            runtime.consecutive_failures = 0
            runtime.grace_until = None
        elif assurance.satisfied is False:
            if not in_grace:
                runtime.consecutive_failures += 1
            runtime.consecutive_passes = 0
        else:
            runtime.consecutive_passes = 0
        assurance.consecutive_failures = runtime.consecutive_failures
        assurance.consecutive_passes = runtime.consecutive_passes
        runtime.last_assurance_id = assurance.assurance_id
        runtime.assurance_status = (
            assurance.lifecycle.value if assurance.satisfied is not None else "UNKNOWN"
        )
        runtime.last_assurance_at = assurance.observed_at
        runtime.objective_results = [
            item.model_dump(mode="json") for item in assurance.assessments
        ]
        runtime.evidence_refs = [item.evidence_id for item in evidence]
        runtime.observed_metrics = self._runtime_observed_metrics(assurance, evidence)
        self.domain.save_assurance(assurance)
        context_row = next(
            (
                item
                for item in self.domain.contexts(intent_id)
                if item.get("context_id") == assurance.context_id
            ),
            None,
        )
        if context_row is not None:
            from models.intent_domain import NetworkContext

            self.capabilities.record_assurance(
                assurance,
                NetworkContext.model_validate(context_row),
            )
        self.domain.save_decision(
            AgentDecision(
                intent_id=intent_id,
                intent_revision=runtime.pending_revision or runtime.current_revision,
                agent_id="intent_assurance",
                decision_type="OBJECTIVE_ASSESSMENT",
                plan_id=runtime.active_plan_id,
                context_id=runtime.context_id,
                inputs={"evidence_ids": [item.evidence_id for item in evidence]},
                output=assurance.model_dump(mode="json"),
                facts=[item.reason for item in assurance.assessments],
            )
        )
        self.domain.save_runtime(runtime)
        if assurance.satisfied is False and in_grace:
            return self.domain.transition(
                intent_id,
                IntentLifecycle.DEGRADED,
                actor="violation_detector",
                reason="objective failed during the post-deployment grace period",
                data={
                    "grace_until": (
                        runtime.grace_until.isoformat() if runtime.grace_until else None
                    )
                },
            )
        if (
            assurance.satisfied is True
            and runtime.consecutive_passes >= self.pass_threshold
        ):
            runtime.degradation_since = None
            self.domain.save_runtime(runtime)
            return self.domain.transition(
                intent_id,
                IntentLifecycle.SATISFIED,
                actor="intent_assurance",
                reason=assurance.summary,
            )
        if (
            assurance.satisfied is False
            and (
                force_violation
                or runtime.consecutive_failures >= self.failure_threshold
            )
        ):
            runtime.last_violation_at = datetime.now(timezone.utc)
            runtime.violation_count += 1
            self.domain.save_runtime(runtime)
            runtime = self.domain.transition(
                intent_id,
                IntentLifecycle.ASSURANCE_FAILED,
                actor="violation_detector",
                reason=assurance.summary,
                data={
                    "assurance_id": assurance.assurance_id,
                    "failure_reason": assurance.failure_reason or "SLA_VIOLATED",
                    "failure_stage": assurance.failure_stage or "ASSURANCE",
                    "recoverable": (
                        assurance.recoverable
                        if assurance.recoverable is not None
                        else True
                    ),
                },
            )
            if auto_heal:
                await self._diagnose_and_heal(spec, runtime, assurance, evidence)
            return self.domain.get_runtime(intent_id) or runtime
        if runtime.degradation_since is None:
            runtime.degradation_since = datetime.now(timezone.utc)
            self.domain.save_runtime(runtime)
        return self.domain.transition(
            intent_id,
            IntentLifecycle.DEGRADED,
            actor="violation_detector",
            reason=assurance.summary,
            data={
                "consecutive_failures": runtime.consecutive_failures,
                "threshold": self.failure_threshold,
            },
        )

    async def replan(
        self,
        intent_id: str,
        *,
        deploy: bool = False,
        reason: str = "operator requested replanning",
    ) -> IntentPlanIR:
        async with self._lock(intent_id):
            spec = self._require_spec(intent_id)
            runtime = self.domain.get_runtime(intent_id) or IntentRuntimeState(
                intent_id=intent_id
            )
            runtime.last_replan_reason = reason
            self.domain.save_runtime(runtime)
            self.domain.transition(
                intent_id,
                IntentLifecycle.REPLANNING,
                actor="intent_planning",
                reason=reason,
            )
            context = await self.context_builder.build_async()
            self.domain.save_context(intent_id, context)
            attempted_paths = [
                list(item.get("selected_path") or [])
                for item in self.domain.executions(intent_id)
            ]
            plan, decision = await self.planning.replan(
                spec,
                context,
                parent_plan_id=runtime.active_plan_id,
                current_path=list(runtime.current_path),
                excluded_paths=attempted_paths,
                reason=reason,
            )
            context.candidate_paths = [
                item.model_dump(mode="json") for item in plan.candidates
            ]
            self.domain.save_context(intent_id, context)
            self.domain.save_plan(plan)
            self.domain.save_decision(decision)
            lifecycle = (
                IntentLifecycle.PLAN_READY
                if plan.validation_status == PlanValidationStatus.VALID
                else IntentLifecycle.UNSATISFIABLE
            )
            self.domain.transition(
                intent_id,
                lifecycle,
                actor="plan_validator",
                reason=(
                    "replan validated"
                    if lifecycle == IntentLifecycle.PLAN_READY
                    else "; ".join(plan.validation_errors)
                ),
                plan_id=plan.plan_id,
                context_id=context.context_id,
            )
            if deploy and lifecycle == IntentLifecycle.PLAN_READY:
                conflicts = self._check_conflicts(spec)
                for conflict in conflicts:
                    self.domain.save_conflict(conflict)
                blocking = [
                    item
                    for item in conflicts
                    if item.status == "OPEN" and item.severity in {"high", "critical"}
                ]
                if blocking:
                    self.domain.transition(
                        intent_id,
                        IntentLifecycle.NEEDS_APPROVAL,
                        actor="conflict_safety",
                        reason="replan has a blocking conflict",
                        data={"conflict_ids": [item.conflict_id for item in blocking]},
                    )
                    return plan
                self.domain.transition(
                    intent_id,
                    IntentLifecycle.CHECKING,
                    actor="conflict_safety",
                    reason="replan safety checks passed",
                    plan_id=plan.plan_id,
                    context_id=context.context_id,
                )
                self.domain.transition(
                    intent_id,
                    IntentLifecycle.REDEPLOYING,
                    actor="execution_agent",
                    reason=reason,
                    plan_id=plan.plan_id,
                    context_id=context.context_id,
                )
                execution = await self._execute_plan_locked(plan)
                if execution.status == ExecutionStatus.COMPLETED:
                    self.domain.transition(
                        intent_id,
                        IntentLifecycle.VERIFYING,
                        actor="intent_assurance",
                        reason="replanned configuration passed readback",
                    )
                    await self._trigger_service_validation(spec)
                    await self._assess_locked(intent_id, spec=spec, plan=plan)
                else:
                    self.domain.transition(
                        intent_id,
                        IntentLifecycle.EXECUTION_FAILED,
                        actor="execution_agent",
                        reason=execution.error or "replan execution failed",
                    )
            return plan

    def update_intent(self, intent_id: str, patch: dict[str, Any]) -> IntentSpec:
        current = self._require_spec(intent_id)
        allowed = {
            "original_text",
            "normalized_text",
            "priority",
            "scope",
            "tenant_id",
            "validity",
            "objectives",
            "constraints",
            "preferences",
            "selector",
            "source",
            "destination",
            "service",
        }
        update = {key: value for key, value in patch.items() if key in allowed}
        runtime = self.domain.get_runtime(intent_id) or IntentRuntimeState(
            intent_id=intent_id
        )
        update.update(
            {
                "revision": self.domain.next_revision(intent_id),
                "updated_at": datetime.now(timezone.utc),
                "lifecycle": runtime.lifecycle,
            }
        )
        spec = current.model_copy(update=update)
        spec = IntentSpec.model_validate(spec.model_dump(mode="json"))
        self.domain.stage_revision(
            spec,
            actor="intent_api",
            reason=f"intent revision {spec.revision} staged for make-before-break validation",
        )
        self.domain.save_decision(
            AgentDecision(
                intent_id=intent_id,
                intent_revision=spec.revision,
                agent_id="intent_api",
                decision_type="REVISION_STAGED",
                inputs={"patch": update},
                output={
                    "current_revision": runtime.current_revision,
                    "pending_revision": spec.revision,
                    "active_lifecycle": runtime.lifecycle.value,
                },
                facts=[
                    "active revision remains authoritative until replacement readback succeeds"
                ],
            )
        )
        return spec

    async def apply_pending_revision(
        self,
        intent_id: str,
        *,
        deploy: bool = True,
    ) -> IntentRevision:
        """Validate a pending revision without disturbing the active revision."""
        async with self._lock(intent_id):
            runtime = self.domain.get_runtime(intent_id) or IntentRuntimeState(
                intent_id=intent_id
            )
            if runtime.pending_revision is None:
                raise ValueError("intent has no pending revision")
            revision = self.domain.get_revision(intent_id, runtime.pending_revision)
            spec = self.domain.get_spec(intent_id, runtime.pending_revision)
            if revision is None or spec is None:
                raise RuntimeError("pending revision record is incomplete")
            revision.status = RevisionStatus.PLANNING
            self.domain.save_revision(revision)
            context = await self.context_builder.build_async()
            self.domain.save_context(intent_id, context)
            plan, decision = await self.planning.plan(spec, context)
            self.domain.save_plan(plan)
            self.domain.save_decision(decision)
            revision.plan_id = plan.plan_id
            if plan.validation_status != PlanValidationStatus.VALID:
                return self.domain.finish_revision(
                    revision,
                    runtime,
                    status=RevisionStatus.FAILED,
                    plan_id=plan.plan_id,
                    error="; ".join(plan.validation_errors)
                    or "pending revision is unsatisfiable",
                )
            if not deploy:
                revision.status = RevisionStatus.PENDING
                return self.domain.save_revision(revision)

            revision.status = RevisionStatus.DEPLOYING
            self.domain.save_revision(revision)
            execution = await self._execute_plan_locked(plan)
            latest_runtime = self.domain.get_runtime(intent_id) or runtime
            if execution.status != ExecutionStatus.COMPLETED:
                return self.domain.finish_revision(
                    revision,
                    latest_runtime,
                    status=RevisionStatus.FAILED,
                    plan_id=plan.plan_id,
                    execution_id=execution.execution_id,
                    error=execution.error
                    or "replacement deployment failed and was rolled back",
                )
            return self.domain.finish_revision(
                revision,
                latest_runtime,
                status=RevisionStatus.ACTIVE,
                plan_id=plan.plan_id,
                execution_id=execution.execution_id,
            )

    async def withdraw(self, intent_id: str) -> IntentRuntimeState:
        from orchestrator.intent_withdrawal import IntentWithdrawalService

        return await IntentWithdrawalService(
            self.store, self.domain, self.execution
        ).withdraw(intent_id)

    async def run_loop(self, shutdown: asyncio.Event) -> None:
        interval = max(
            2.0, float(os.getenv("IDN_INTENT_ASSURANCE_INTERVAL_SECONDS", "15"))
        )
        while not shutdown.is_set():
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            with self.store._connect() as connection:
                rows = connection.execute(
                    "SELECT intent_id FROM intent_runtime_v1 WHERE lifecycle IN ('SATISFIED','DEGRADED','VIOLATED','VERIFYING')"
                ).fetchall()
            for row in rows:
                try:
                    await self.reevaluate(str(row["intent_id"]))
                except Exception:
                    logger.exception(
                        "Intent assurance cycle failed for %s", row["intent_id"]
                    )

    async def _diagnose_and_heal(self, spec, runtime, assurance, evidence) -> None:
        """Run one bounded, evidence-correlated healing transaction chain."""
        violation_id = f"VIO-{assurance.assurance_id.removeprefix('ASR-')}"
        self.domain.transition(
            spec.intent_id,
            IntentLifecycle.DIAGNOSING,
            actor="intent_root_cause",
            reason="violation threshold reached",
            data={"violation_id": violation_id, "assurance_id": assurance.assurance_id},
        )
        self.domain.record_event(
            spec.intent_id,
            "INTENT_VIOLATED",
            actor="violation_detector",
            reason=assurance.summary,
            plan_id=runtime.active_plan_id,
            context_id=runtime.context_id,
            data={
                "violation_id": violation_id,
                "assurance_id": assurance.assurance_id,
                "execution_id": runtime.active_execution_id,
            },
        )
        context = await self.context_builder.build_async()
        current_path = list(runtime.current_path) or self._active_plan_path(
            spec.intent_id, runtime.active_plan_id
        )
        context.current_paths[spec.intent_id] = list(current_path)
        path_candidates = self.planning.generator.generate(spec, context)
        context.candidate_paths = [
            item.model_dump(mode="json") for item in path_candidates
        ]
        context.healing_attempt_history = self.domain.healing(spec.intent_id)
        self.domain.save_context(spec.intent_id, context)
        diagnosis = await self.root_cause.diagnose(
            assurance,
            evidence,
            active_faults=context.faults,
            revision=spec.revision,
            correlation_id=violation_id,
            input_refs=[violation_id, context.context_id],
            network_context=context,
        )
        self.domain.save_decision(
            AgentDecision(
                intent_id=spec.intent_id,
                intent_revision=spec.revision,
                agent_id="intent_root_cause",
                decision_type="ROOT_CAUSE",
                context_id=context.context_id,
                input_refs=[violation_id, assurance.assurance_id],
                inputs={"assurance_id": assurance.assurance_id},
                output=diagnosis.model_dump(mode="json"),
                facts=[item.statement for item in diagnosis.facts],
                confidence=diagnosis.confidence,
                evidence_refs=[item.evidence_id for item in evidence],
                llm_status=diagnosis.llm_status,
                llm_runtime_mode=diagnosis.llm_runtime_mode,
                llm_call_id=diagnosis.llm_call_id,
                llm_provider=diagnosis.llm_provider,
                llm_model=diagnosis.llm_model,
                llm_structured_output_valid=diagnosis.llm_structured_output_valid,
                llm_validator_result=diagnosis.llm_validator_result,
                fallback_used=diagnosis.fallback_used,
            )
        )
        self.domain.record_event(
            spec.intent_id,
            "ROOT_CAUSE_SELECTED",
            actor="intent_root_cause",
            reason=diagnosis.explanation,
            plan_id=runtime.active_plan_id,
            context_id=context.context_id,
            data={
                "violation_id": violation_id,
                "assurance_id": assurance.assurance_id,
                "root_cause_id": diagnosis.diagnosis_id,
                "confidence": diagnosis.confidence,
            },
        )
        runtime = self.domain.get_runtime(spec.intent_id) or runtime
        runtime.current_root_cause = diagnosis.category.value
        # Pre-schema history may have counted candidate checks as executions.
        # Rebuild the quota from explicit data-plane mutations before deciding
        # whether the current violation is allowed to evaluate candidates.
        actual_execution_attempts = self._actual_healing_execution_attempts(spec.intent_id)
        if runtime.healing_attempts != actual_execution_attempts:
            runtime.healing_attempts = actual_execution_attempts
        self.domain.save_runtime(runtime)

        while True:
            runtime = self.domain.get_runtime(spec.intent_id) or runtime
            if runtime.healing_attempts >= runtime.max_healing_attempts:
                self._mark_unsatisfiable(
                    spec.intent_id,
                    "MAX_HEALING_ATTEMPTS_EXHAUSTED: bounded healing attempts exhausted",
                )
                return
            if self.domain.healing(spec.intent_id):
                context = await self.context_builder.build_async()
                current_path = list(runtime.current_path) or self._active_plan_path(
                    spec.intent_id, runtime.active_plan_id
                )
                context.current_paths[spec.intent_id] = list(current_path)
                path_candidates = self.planning.generator.generate(spec, context)
                context.candidate_paths = [
                    item.model_dump(mode="json") for item in path_candidates
                ]
                context.healing_attempt_history = self.domain.healing(spec.intent_id)
                self.domain.save_context(spec.intent_id, context)
                diagnosis = await self.root_cause.diagnose(
                    assurance,
                    evidence,
                    active_faults=context.faults,
                    revision=spec.revision,
                    correlation_id=violation_id,
                    input_refs=[violation_id, context.context_id],
                    network_context=context,
                )
                runtime.current_root_cause = diagnosis.category.value
                self.domain.save_runtime(runtime)
                self.domain.save_decision(
                    AgentDecision(
                        intent_id=spec.intent_id,
                        intent_revision=spec.revision,
                        agent_id="intent_root_cause",
                        decision_type="ROOT_CAUSE_REFRESH",
                        context_id=context.context_id,
                        input_refs=[violation_id, assurance.assurance_id],
                        inputs={
                            "assurance_id": assurance.assurance_id,
                            "previous_attempts": context.healing_attempt_history,
                        },
                        output=diagnosis.model_dump(mode="json"),
                        facts=[item.statement for item in diagnosis.facts],
                        confidence=diagnosis.confidence,
                        evidence_refs=[item.evidence_id for item in evidence],
                        llm_status=diagnosis.llm_status,
                        llm_runtime_mode=diagnosis.llm_runtime_mode,
                        llm_call_id=diagnosis.llm_call_id,
                        llm_provider=diagnosis.llm_provider,
                        llm_model=diagnosis.llm_model,
                        llm_structured_output_valid=diagnosis.llm_structured_output_valid,
                        llm_validator_result=diagnosis.llm_validator_result,
                        fallback_used=diagnosis.fallback_used,
                    )
                )
            attempt_number = runtime.healing_attempts + 1
            current_measurement = self._observed_bandwidth(assurance)
            sla_tolerance_ratio = float(
                os.getenv("IDN_QOS_KPI_TOLERANCE_RATIO", "0.95")
            )
            actions = self.healing_action_generator.generate(
                spec,
                context,
                current_path=current_path,
                root_cause=diagnosis,
                path_candidates=path_candidates,
                current_measurement_mbps=current_measurement,
                tolerance_ratio=sla_tolerance_ratio,
            )
            previous_attempts = self.domain.healing(spec.intent_id)
            evaluations = [
                self.healing_action_validator.validate(
                    action,
                    context=context,
                    current_plan=self._active_plan(spec.intent_id, runtime.active_plan_id),
                    current_path=current_path,
                    root_cause=diagnosis,
                    bottleneck_link_ids=list(diagnosis.affected_link_ids),
                    active_intents=context.active_intents,
                    previously_attempted_actions=previous_attempts,
                )
                for action in actions
            ]
            applicable = [
                item
                for item in evaluations
                if item.applicability == HealingActionApplicability.APPLICABLE
            ]
            action_order = {
                HealingActionType.REROUTE: 0,
                HealingActionType.THROTTLE_LOWER_PRIORITY: 1,
                HealingActionType.QOS_REALLOCATION: 2,
                HealingActionType.RELEASE_STALE_RESERVATION: 3,
                HealingActionType.POLICY_UPDATE: 4,
            }
            selected_evaluation = min(
                applicable,
                key=lambda item: action_order.get(item.action, 99),
                default=None,
            )
            selected = next(
                (
                    action
                    for action in actions
                    if selected_evaluation
                    and action.action_id == selected_evaluation.action_id
                ),
                None,
            )
            confidence = self._healing_confidence(
                diagnosis.confidence,
                evidence,
                context,
                alternative_count=max(len(actions), 1),
            )
            source_plan_id = runtime.active_plan_id
            proposal = HealingAttempt(
                intent_id=spec.intent_id,
                revision=spec.revision,
                violation_id=violation_id,
                assurance_id=assurance.assurance_id,
                root_cause_id=diagnosis.diagnosis_id,
                source_context_id=context.context_id,
                source_plan_id=source_plan_id,
                source_execution_id=runtime.active_execution_id,
                trigger_reason=diagnosis.explanation,
                proposed_actions=actions,
                confidence=confidence,
                evidence_refs=[item.evidence_id for item in evidence],
                requires_replan=bool(
                    selected and selected.type == HealingActionType.REROUTE
                ),
                requires_approval=bool(
                    selected
                    and selected.risk == "high"
                    and confidence < self.low_confidence_threshold
                ),
                status=HealingProposalStatus.PROPOSED,
                diagnosis_id=diagnosis.diagnosis_id,
                attempt=attempt_number,
                candidate_evaluations=evaluations,
                selected_action=selected,
                before_assurance_id=assurance.assurance_id,
                reason=diagnosis.explanation,
                action_fingerprint=(
                    selected_evaluation.action_fingerprint
                    if selected_evaluation
                    else None
                ),
                context_signature=(
                    selected_evaluation.context_signature
                    if selected_evaluation
                    else None
                ),
            )
            self.domain.save_healing(proposal)
            self._record_healing_event(
                proposal,
                "HEALING_PROPOSED",
                actor="intent_healing",
                reason=proposal.trigger_reason,
            )
            runtime.last_replan_reason = "INTENT_VIOLATION"
            self.domain.save_runtime(runtime)
            self.domain.save_decision(
                AgentDecision(
                    intent_id=spec.intent_id,
                    intent_revision=spec.revision,
                    agent_id="intent_healing",
                    decision_type="HEALING_PROPOSAL",
                    context_id=context.context_id,
                    input_refs=[
                        violation_id,
                        assurance.assurance_id,
                        diagnosis.diagnosis_id,
                    ],
                    inputs={
                        "root_cause": diagnosis.category.value,
                        "root_cause_scope": diagnosis.scope.value,
                        "current_path": current_path,
                        "bottleneck_link_ids": diagnosis.affected_link_ids,
                        "previous_attempts": previous_attempts,
                    },
                    output=proposal.model_dump(mode="json"),
                    alternatives=[item.model_dump(mode="json") for item in actions],
                    rejected_alternatives=[
                        item.model_dump(mode="json")
                        for item in evaluations
                        if item.applicability
                        != HealingActionApplicability.APPLICABLE
                    ],
                    rationale_summary=diagnosis.explanation,
                    confidence=confidence,
                    evidence_refs=proposal.evidence_refs,
                )
            )

            if not selected or selected.type == HealingActionType.NO_FEASIBLE_ACTION:
                proposal.status = HealingProposalStatus.REJECTED
                proposal.attempt_status = HealingAttemptStatus.REJECTED
                proposal.outcome = "NO_FEASIBLE_ACTION"
                proposal.reason = "NO_FEASIBLE_HEALING_ACTION"
                proposal.skipped_no_effect = True
                self.domain.save_healing(proposal)
                runtime.skipped_healing_actions += len(evaluations)
                self.domain.save_runtime(runtime)
                required = self._required_bandwidth(spec)
                feasibility = self.feasibility.evaluate(
                    evaluations,
                    effective_min_mbps=(
                        required * sla_tolerance_ratio
                        if required is not None
                        else None
                    ),
                    current_measurement_mbps=current_measurement,
                )
                self._record_healing_event(
                    proposal,
                    "HEALING_CANDIDATES_REJECTED",
                    actor="healing_action_applicability_validator",
                    reason=proposal.reason,
                    context_id=context.context_id,
                    data={
                        "candidate_evaluations": [
                            item.model_dump(mode="json") for item in evaluations
                        ],
                        "feasibility": feasibility,
                    },
                )
                self._record_healing_event(
                    proposal,
                    "NO_FEASIBLE_HEALING_ACTION",
                    actor="healing_controller",
                    reason="all evaluated candidates were rejected before plan execution",
                    context_id=context.context_id,
                )
                self._mark_unsatisfiable(
                    spec.intent_id,
                    "NO_FEASIBLE_HEALING_ACTION: all candidates were rejected, unsafe, redundant, or had no expected effect",
                )
                return
            if proposal.requires_approval:
                proposal.status = HealingProposalStatus.REJECTED
                proposal.outcome = "NEEDS_APPROVAL"
                proposal.reason = "low confidence blocks a high-impact action"
                self.domain.save_healing(proposal)
                self.domain.transition(
                    spec.intent_id,
                    IntentLifecycle.NEEDS_APPROVAL,
                    actor="conflict_safety",
                    reason=proposal.reason,
                    data=self._correlation_data(proposal),
                )
                return
            if selected.type != HealingActionType.REROUTE:
                resource_types = {
                    HealingActionType.THROTTLE_LOWER_PRIORITY,
                    HealingActionType.RELEASE_STALE_RESERVATION,
                }
                if selected.type not in resource_types:
                    proposal.status = HealingProposalStatus.REJECTED
                    proposal.outcome = "NO_AUTONOMOUS_EXECUTOR"
                    proposal.reason = (
                        f"no autonomous executor is authorized for {selected.type.value}"
                    )
                    self.domain.save_healing(proposal)
                    self._mark_unsatisfiable(spec.intent_id, proposal.reason)
                    return

                proposal.status = HealingProposalStatus.APPROVED
                self.domain.save_healing(proposal)
                self.domain.save_decision(
                    AgentDecision(
                        intent_id=spec.intent_id,
                        intent_revision=spec.revision,
                        agent_id="conflict_safety",
                        decision_type="HEALING_RESOURCE_SAFETY_CHECK",
                        context_id=context.context_id,
                        input_refs=[proposal.healing_id],
                        output={
                            "approved": True,
                            "action": selected.model_dump(mode="json"),
                            "validator": selected_evaluation.model_dump(mode="json"),
                        },
                        facts=[
                            "only low-priority preemptible competitors or evidenced stale reservations are affected"
                        ],
                        confidence=confidence,
                    )
                )
                self.domain.transition(
                    spec.intent_id,
                    IntentLifecycle.REDEPLOYING,
                    actor="resource_healing_executor",
                    reason=f"validated {selected.type.value} action approved",
                    context_id=context.context_id,
                    data=self._correlation_data(proposal),
                )
                proposal.status = HealingProposalStatus.EXECUTING
                proposal.attempt_type = HealingAttemptType.EXECUTION
                self.domain.save_healing(proposal)
                resource_result = await self._execute_resource_healing_action(
                    selected,
                    context=context,
                    protected_intent_id=spec.intent_id,
                )
                proposal.execution_id = str(
                    resource_result.get("execution_id") or f"RESOURCE-{proposal.healing_id}"
                )
                proposal.execution_result = resource_result
                proposal.data_plane_changed = bool(resource_result.get("data_plane_changed"))
                if proposal.data_plane_changed:
                    runtime = self.domain.get_runtime(spec.intent_id) or runtime
                    runtime.healing_attempts += 1
                    self.domain.save_runtime(runtime)
                if resource_result.get("status") != "COMPLETED":
                    proposal.status = HealingProposalStatus.FAILED
                    proposal.attempt_status = HealingAttemptStatus.FAILED
                    proposal.outcome = "RESOURCE_EXECUTION_FAILED"
                    proposal.reason = str(
                        resource_result.get("error") or "resource healing failed"
                    )
                    self.domain.save_healing(proposal)
                    self._record_healing_attempt(proposal)
                    runtime = self.domain.get_runtime(spec.intent_id) or runtime
                    if runtime.healing_attempts >= runtime.max_healing_attempts:
                        self._mark_unsatisfiable(spec.intent_id, proposal.reason)
                        return
                    self.domain.transition(
                        spec.intent_id,
                        IntentLifecycle.REPLANNING,
                        actor="healing_controller",
                        reason="resource action failed; rebuild context before considering another candidate",
                        data=self._correlation_data(proposal),
                    )
                    continue

                self.domain.transition(
                    spec.intent_id,
                    IntentLifecycle.VERIFYING,
                    actor="intent_assurance",
                    reason="resource action completed; collecting independent measurements",
                    context_id=context.context_id,
                    data=self._correlation_data(proposal),
                )
                validation = await self._trigger_service_validation(
                    spec, selected_path=current_path
                )
                proposal.execution_result["post_resource_service_validation"] = validation
                active_plan = self._active_plan(spec.intent_id, runtime.active_plan_id)
                latest = await self._assess_locked(
                    spec.intent_id,
                    spec=spec,
                    plan=active_plan,
                    auto_heal=False,
                    force_violation=True,
                )
                proposal.after_assurance_id = latest.last_assurance_id
                if latest.lifecycle == IntentLifecycle.SATISFIED:
                    proposal.status = HealingProposalStatus.VERIFIED
                    proposal.attempt_status = HealingAttemptStatus.VERIFIED
                    proposal.outcome = IntentLifecycle.SATISFIED.value
                    proposal.reason = "resource reclamation was independently measured and satisfied the intent"
                    runtime = self.domain.get_runtime(spec.intent_id) or latest
                    runtime.cooldown_until = datetime.now(timezone.utc) + timedelta(
                        seconds=self.cooldown_seconds
                    )
                    self.domain.save_runtime(runtime)
                    self.domain.save_healing(proposal)
                    self._record_healing_attempt(proposal)
                    self._record_healing_final(
                        spec.intent_id, IntentLifecycle.SATISFIED.value
                    )
                    self._record_healing_event(
                        proposal,
                        "INTENT_SATISFIED",
                        actor="intent_assurance",
                        reason=proposal.reason,
                        context_id=context.context_id,
                    )
                    return

                proposal.status = HealingProposalStatus.FAILED
                proposal.attempt_status = HealingAttemptStatus.FAILED
                proposal.outcome = latest.lifecycle.value
                proposal.reason = "post-resource assurance did not satisfy the intent"
                self.domain.save_healing(proposal)
                self._record_healing_attempt(proposal)
                runtime = self.domain.get_runtime(spec.intent_id) or latest
                if runtime.healing_attempts >= runtime.max_healing_attempts:
                    self._mark_unsatisfiable(spec.intent_id, proposal.reason)
                    return
                latest_assurance = self.domain.assurance(spec.intent_id)
                if latest_assurance:
                    assurance = IntentAssuranceResult.model_validate(latest_assurance[0])
                evidence = [
                    EvidenceItem.model_validate(item)
                    for item in self.domain.evidence(spec.intent_id)
                    if item.get("evidence_id") in set(runtime.evidence_refs)
                ]
                self.domain.transition(
                    spec.intent_id,
                    IntentLifecycle.REPLANNING,
                    actor="healing_controller",
                    reason="post-resource assurance failed; rebuild context before another candidate",
                    data=self._correlation_data(proposal),
                )
                continue

            self._record_healing_event(
                proposal,
                "CONTEXT_REFRESHED",
                actor="network_sensing",
                reason="fresh operational context captured for replanning",
                context_id=context.context_id,
                data={"fresh_context_id": context.context_id},
            )
            self.domain.save_decision(
                AgentDecision(
                    intent_id=spec.intent_id,
                    intent_revision=spec.revision,
                    agent_id="network_sensing",
                    decision_type="NETWORK_CONTEXT_REFRESH",
                    context_id=context.context_id,
                    input_refs=[proposal.healing_id, proposal.source_context_id],
                    inputs={"trigger": "HEALING_PROPOSAL"},
                    output={
                        "context_id": context.context_id,
                        "captured_at": context.captured_at.isoformat(),
                        "evidence_quality": context.evidence_quality.value,
                        "active_intents": context.active_intents,
                        "current_paths": context.current_paths,
                        "congestion": context.congestion,
                        "reservations": context.reservations,
                        "faults": context.faults,
                        "freshness": context.freshness,
                    },
                    facts=[
                        f"fresh context {context.context_id} captured after healing proposal",
                        f"decision_usable={context.decision_usable}",
                    ],
                    evidence_refs=list(context.evidence_refs),
                )
            )

            self.domain.transition(
                spec.intent_id,
                IntentLifecycle.REPLANNING,
                actor="intent_planning",
                reason="INTENT_VIOLATION",
                context_id=context.context_id,
                data=self._correlation_data(proposal),
            )
            self._record_healing_event(
                proposal,
                "REPLAN_STARTED",
                actor="intent_planning",
                reason="INTENT_VIOLATION",
                context_id=context.context_id,
            )
            proposal.status = HealingProposalStatus.VALIDATING
            self.domain.save_healing(proposal)
            attempted_paths = [
                list(item.get("selected_path") or [])
                for item in self.domain.executions(spec.intent_id)
            ]
            fresh_plan, decision = await self.planning.replan(
                spec,
                context,
                parent_plan_id=source_plan_id,
                current_path=current_path,
                excluded_paths=attempted_paths,
                allowed_paths=[
                    list(selected.params.get("target_path") or [])
                ],
                reason="INTENT_VIOLATION",
            )
            # The planner can use a fresh context and therefore creates a fresh
            # ID.  Healing must compare its desired data-plane semantics against
            # the committed plan, never against those identifiers.
            current_committed_plan = self._active_plan(
                spec.intent_id, runtime.active_plan_id
            )
            fresh_plan.plan_diff = PlanDiff.compare(current_committed_plan, fresh_plan)
            fresh_plan.effect_signature = fresh_plan.plan_diff.proposed_effect_signature
            context.candidate_paths = [
                item.model_dump(mode="json") for item in fresh_plan.candidates
            ]
            self.domain.save_context(spec.intent_id, context)
            self.domain.save_plan(fresh_plan)
            self.domain.save_decision(decision)
            proposal.plan_id = fresh_plan.plan_id
            proposal.plan_diff = fresh_plan.plan_diff
            proposal.candidates = [
                item.model_dump(mode="json") for item in fresh_plan.candidates
            ]
            proposal.selected_candidate_id = fresh_plan.selected_candidate_id
            proposal.tried_plan_ids.append(fresh_plan.plan_id)
            self._record_healing_event(
                proposal,
                "PLAN_SELECTED",
                actor="intent_planning",
                reason="constrained planner selected a formal healing plan",
                context_id=context.context_id,
                data={"selected_candidate_id": fresh_plan.selected_candidate_id},
            )
            if fresh_plan.plan_diff and fresh_plan.plan_diff.is_noop():
                proposal.status = HealingProposalStatus.REJECTED
                proposal.attempt_status = HealingAttemptStatus.SKIPPED
                proposal.outcome = "SKIPPED_NO_EFFECT"
                proposal.reason = "NO_OP_PLAN"
                proposal.skipped_no_effect = True
                self.domain.save_healing(proposal)
                runtime.skipped_healing_actions += 1
                self.domain.save_runtime(runtime)
                self._record_healing_event(
                    proposal,
                    "HEALING_PLAN_SKIPPED",
                    actor="plan_diff_gate",
                    reason="NO_OP_PLAN: desired data-plane effect equals the committed plan",
                    context_id=context.context_id,
                    data={"reason_code": "NO_OP_PLAN", "execution_id": None},
                )
                # NO_OP applies to this selected candidate only.  It is not
                # proof that the healing search space is empty; preserve the
                # failed assurance state for the candidate enumerator instead
                # of falsely terminalising the intent.
                self._record_healing_event(
                    proposal,
                    "HEALING_CANDIDATE_SKIPPED",
                    actor="plan_diff_gate",
                    reason="NO_DATAPLANE_CHANGE",
                    context_id=context.context_id,
                    data={"reason_code": "NO_OP_PLAN", "search_status": "CONTINUE"},
                )
                return
            if fresh_plan.validation_status != PlanValidationStatus.VALID:
                proposal.status = HealingProposalStatus.REJECTED
                proposal.attempt_status = HealingAttemptStatus.REJECTED
                proposal.outcome = "UNSATISFIABLE"
                proposal.reason = (
                    "; ".join(fresh_plan.validation_errors)
                    or "no different feasible path"
                )
                self.domain.save_healing(proposal)
                self._mark_unsatisfiable(spec.intent_id, proposal.reason)
                return

            affected = self._analyze_affected_intents(
                spec, context, fresh_plan.selected_candidate_id
            )
            proposal.affected_intent_analysis = affected
            conflicts = self._check_conflicts(spec)
            for conflict in conflicts:
                self.domain.save_conflict(conflict)
            blocking = [
                item
                for item in conflicts
                if item.status == "OPEN" and item.severity in {"high", "critical"}
            ]
            if affected.requires_approval or blocking:
                proposal.requires_approval = True
                proposal.status = HealingProposalStatus.REJECTED
                proposal.outcome = "NEEDS_APPROVAL"
                proposal.reason = "protected or conflicting intent blocks autonomous migration"
                self.domain.save_healing(proposal)
                self.domain.transition(
                    spec.intent_id,
                    IntentLifecycle.NEEDS_APPROVAL,
                    actor="conflict_safety",
                    reason=proposal.reason,
                    event_plan_id=fresh_plan.plan_id,
                    context_id=context.context_id,
                    data={
                        **self._correlation_data(proposal),
                        "conflict_ids": [item.conflict_id for item in blocking],
                        "protected_intent_ids": affected.protected_intent_ids,
                    },
                )
                return

            proposal.status = HealingProposalStatus.APPROVED
            self.domain.save_healing(proposal)
            self.domain.save_decision(
                AgentDecision(
                    intent_id=spec.intent_id,
                    intent_revision=spec.revision,
                    agent_id="conflict_safety",
                    decision_type="HEALING_SAFETY_CHECK",
                    plan_id=fresh_plan.plan_id,
                    context_id=context.context_id,
                    input_refs=[proposal.healing_id],
                    output={
                        "approved": True,
                        "affected_intent_analysis": affected.model_dump(mode="json"),
                    },
                    facts=["no protected intent is degraded by the selected reroute"],
                    confidence=confidence,
                )
            )
            self._record_healing_event(
                proposal,
                "HEALING_CHECKED",
                actor="conflict_safety",
                reason="selected reroute passed protected-intent and conflict checks",
                context_id=context.context_id,
                data={
                    "affected_intent_analysis_id": affected.analysis_id,
                    "protected_intent_ids": affected.protected_intent_ids,
                },
            )
            self.domain.transition(
                spec.intent_id,
                IntentLifecycle.REDEPLOYING,
                actor="intent_execution",
                reason="make-before-break healing generation approved",
                event_plan_id=fresh_plan.plan_id,
                context_id=context.context_id,
                data=self._correlation_data(proposal),
            )
            self._record_healing_event(
                proposal,
                "REDEPLOY_STARTED",
                actor="intent_execution",
                reason="make-before-break deployment started",
                context_id=context.context_id,
            )
            proposal.status = HealingProposalStatus.EXECUTING
            proposal.attempt_type = HealingAttemptType.EXECUTION
            self.domain.save_healing(proposal)
            execution = await self._execute_plan_locked(fresh_plan)
            proposal.execution_id = execution.execution_id
            proposal.execution_result = execution.model_dump(mode="json")
            proposal.migration_proofs = list(execution.migration_proofs)
            proposal.data_plane_changed = bool(execution.flows_installed > 0)
            if proposal.data_plane_changed:
                runtime = self.domain.get_runtime(spec.intent_id) or runtime
                runtime.healing_attempts += 1
                self.domain.save_runtime(runtime)
            if execution.status != ExecutionStatus.COMPLETED:
                proposal.status = (
                    HealingProposalStatus.ROLLED_BACK
                    if execution.status == ExecutionStatus.ROLLED_BACK
                    else HealingProposalStatus.FAILED
                )
                proposal.attempt_status = HealingAttemptStatus.FAILED
                proposal.outcome = execution.status.value
                proposal.reason = execution.error or "path migration failed"
                self.domain.save_healing(proposal)
                self._record_healing_event(
                    proposal,
                    "HEALING_FAILED",
                    actor="intent_execution",
                    reason=proposal.reason,
                    context_id=context.context_id,
                    data={"execution_status": execution.status.value},
                )
                if execution.status == ExecutionStatus.ROLLED_BACK:
                    self._record_healing_event(
                        proposal,
                        "PLAN_ROLLED_BACK",
                        actor="intent_execution",
                        reason="new generation failed verification; old generation retained",
                        context_id=context.context_id,
                    )
                self._record_healing_attempt(proposal)
                runtime = self.domain.get_runtime(spec.intent_id) or runtime
                if runtime.healing_attempts >= runtime.max_healing_attempts:
                    self._mark_unsatisfiable(spec.intent_id, proposal.reason)
                    return
                self.domain.transition(
                    spec.intent_id,
                    IntentLifecycle.REPLANNING,
                    actor="healing_controller",
                    reason="previous healing execution rolled back; selecting next candidate",
                    data=self._correlation_data(proposal),
                )
                continue

            proof_ids = [
                item.proof_id for item in execution.migration_proofs if item.passed
            ]
            self._record_healing_event(
                proposal,
                "NEW_RESOURCES_INSTALLED",
                actor="intent_execution",
                reason="new ONOS and path-coupled TC generation installed",
                context_id=context.context_id,
                data={"execution_id": execution.execution_id},
            )
            self._record_healing_event(
                proposal,
                "NEW_RESOURCES_VERIFIED",
                actor="status_verifier",
                reason="independent migration readbacks passed",
                context_id=context.context_id,
                data={"migration_proof_ids": proof_ids},
            )
            self._record_healing_event(
                proposal,
                "PLAN_COMMITTED",
                actor="intent_execution",
                reason="new plan committed after migration proof",
                context_id=context.context_id,
                data={"commit_verified": execution.commit_verified},
            )
            self._record_healing_event(
                proposal,
                "OLD_RESOURCES_CLEANED",
                actor="intent_execution",
                reason="superseded generation cleaned after commit",
                context_id=context.context_id,
                data={"old_path_retained_until_commit": execution.old_path_retained_until_commit},
            )
            runtime = self.domain.get_runtime(spec.intent_id) or runtime
            runtime.last_healing_plan_id = fresh_plan.plan_id
            self.domain.save_runtime(runtime)
            self.domain.transition(
                spec.intent_id,
                IntentLifecycle.VERIFYING,
                actor="intent_assurance",
                reason="two migration proofs passed; collecting a new measurement run",
                plan_id=fresh_plan.plan_id,
                context_id=context.context_id,
                data={
                    **self._correlation_data(proposal),
                    "execution_id": execution.execution_id,
                    "migration_proof_ids": proof_ids,
                },
            )
            validation = await self._trigger_service_validation(
                spec, selected_path=execution.selected_path
            )
            proposal.execution_result["post_commit_service_validation"] = validation
            latest = await self._assess_locked(
                spec.intent_id,
                spec=spec,
                plan=fresh_plan,
                auto_heal=False,
                force_violation=True,
            )
            proposal.after_assurance_id = latest.last_assurance_id
            self._record_healing_event(
                proposal,
                "ASSURANCE_RESULT",
                actor="intent_assurance",
                reason=latest.last_transition_reason or "post-healing assurance completed",
                context_id=context.context_id,
                data={
                    "after_assurance_id": latest.last_assurance_id,
                    "assurance_status": latest.assurance_status,
                },
            )
            if latest.lifecycle == IntentLifecycle.SATISFIED:
                proposal.status = HealingProposalStatus.VERIFIED
                proposal.attempt_status = HealingAttemptStatus.VERIFIED
                proposal.outcome = IntentLifecycle.SATISFIED.value
                proposal.reason = "new measurement assurance satisfied every objective"
                self._commit_desired_policy(spec.intent_id, execution)
                runtime = self.domain.get_runtime(spec.intent_id) or latest
                runtime.cooldown_until = datetime.now(timezone.utc) + timedelta(
                    seconds=self.cooldown_seconds
                )
                self.domain.save_runtime(runtime)
                self.domain.save_healing(proposal)
                self._record_healing_attempt(proposal)
                self._record_healing_final(spec.intent_id, IntentLifecycle.SATISFIED.value)
                self._record_healing_event(
                    proposal,
                    "INTENT_SATISFIED",
                    actor="intent_assurance",
                    reason=proposal.reason,
                    context_id=context.context_id,
                )
                return

            proposal.status = HealingProposalStatus.FAILED
            proposal.attempt_status = HealingAttemptStatus.FAILED
            proposal.outcome = latest.lifecycle.value
            proposal.reason = "post-migration assurance did not satisfy the intent"
            self.domain.save_healing(proposal)
            self._record_healing_attempt(proposal)
            runtime = self.domain.get_runtime(spec.intent_id) or latest
            if runtime.healing_attempts >= runtime.max_healing_attempts:
                self._mark_unsatisfiable(spec.intent_id, proposal.reason)
                return
            latest_assurance = self.domain.assurance(spec.intent_id)
            if latest_assurance:
                assurance = IntentAssuranceResult.model_validate(latest_assurance[0])
            evidence = [
                EvidenceItem.model_validate(item)
                for item in self.domain.evidence(spec.intent_id)
                if item.get("evidence_id") in set(runtime.evidence_refs)
            ]
            self.domain.transition(
                spec.intent_id,
                IntentLifecycle.REPLANNING,
                actor="healing_controller",
                reason="post-migration assurance failed; selecting next candidate",
                data=self._correlation_data(proposal),
            )

        self._mark_unsatisfiable(
            spec.intent_id,
            "bounded healing attempts exhausted without a satisfying plan",
        )

    async def _execute_resource_healing_action(
        self,
        action: HealingAction,
        *,
        context: Any,
        protected_intent_id: str,
    ) -> dict[str, Any]:
        """Execute only the resource delta authorized by applicability and safety."""

        execution_id = f"RESOURCE-{action.action_id}"
        if action.type == HealingActionType.THROTTLE_LOWER_PRIORITY:
            from api.healing import throttle_low_priority_source

            target_ids = {
                str(item) for item in action.params.get("target_intent_ids") or []
            }
            targets = [
                item
                for item in context.active_intents
                if str(item.get("intent_id") or "") in target_ids
            ]
            unsafe = [
                str(item.get("intent_id") or "")
                for item in targets
                if str(item.get("intent_id") or "") == protected_intent_id
                or not bool(item.get("preemptible"))
                or str(item.get("priority") or "").lower() != "low"
            ]
            if unsafe or len(targets) != len(target_ids):
                return {
                    "execution_id": execution_id,
                    "status": "FAILED",
                    "error": f"resource safety changed before execution; unsafe={unsafe}",
                    "target_intent_ids": sorted(target_ids),
                }
            remaining = float(action.params.get("required_gain_mbps") or 0)
            receipts: list[dict[str, Any]] = []
            for target in targets:
                usage = float(target.get("current_usage_mbps") or 0)
                release = min(max(0.0, usage - 1.0), remaining)
                if release <= 0:
                    continue
                receipt = await throttle_low_priority_source(
                    protected_intent_id,
                    target_intent=target,
                    limit_mbps=max(1, int(usage - release)),
                )
                receipts.append(receipt)
                if str(receipt.get("status") or "").lower() != "ok":
                    return {
                        "execution_id": execution_id,
                        "status": "FAILED",
                        "error": "a validated preemptible throttle could not be installed",
                        "receipts": receipts,
                        "data_plane_changed": bool(receipts),
                    }
                remaining = max(0.0, remaining - release)
            if remaining > 1e-9:
                return {
                    "execution_id": execution_id,
                    "status": "FAILED",
                    "error": f"recoverable capacity changed; {remaining:g} Mbps remains",
                    "receipts": receipts,
                    "data_plane_changed": bool(receipts),
                }
            return {
                "execution_id": execution_id,
                "status": "COMPLETED",
                "action": action.type.value,
                "target_intent_ids": sorted(target_ids),
                "expected_released_mbps": action.params.get(
                    "expected_released_mbps"
                ),
                "receipts": receipts,
                "data_plane_changed": bool(receipts),
            }

        if action.type == HealingActionType.RELEASE_STALE_RESERVATION:
            reservation_ids = {
                str(item) for item in action.params.get("reservation_ids") or []
            }
            evidenced_stale = {
                str(item.get("reservation_id") or item.get("id"))
                for item in context.reservations
                if (
                    bool(item.get("stale"))
                    or str(item.get("status") or "").upper() in {"STALE", "EXPIRED"}
                )
            }
            if not reservation_ids or not reservation_ids <= evidenced_stale:
                return {
                    "execution_id": execution_id,
                    "status": "FAILED",
                    "error": "reservation stale evidence changed before execution",
                }
            now = datetime.now(timezone.utc).isoformat()
            released = 0
            with self.store._lock, self.store._connect() as connection:
                for reservation_id in sorted(reservation_ids):
                    cursor = connection.execute(
                        "UPDATE resource_reservations SET active=0, updated_at=? "
                        "WHERE reservation_id=? AND active=1",
                        (now, reservation_id),
                    )
                    released += int(cursor.rowcount or 0)
                connection.commit()
            return {
                "execution_id": execution_id,
                "status": "COMPLETED" if released == len(reservation_ids) else "FAILED",
                "action": action.type.value,
                "released_reservation_ids": sorted(reservation_ids),
                "released_count": released,
                "data_plane_changed": bool(released),
                "error": (
                    None
                    if released == len(reservation_ids)
                    else "one or more reservations were no longer active"
                ),
            }

        return {
            "execution_id": execution_id,
            "status": "FAILED",
            "error": f"unsupported resource action {action.type.value}",
        }

    async def _execute_plan_locked(self, plan: IntentPlanIR) -> IntentExecutionResult:
        previous_items = self.domain.executions(plan.intent_id)
        previous = None
        for item in previous_items:
            candidate = IntentExecutionResult.model_validate(item)
            if candidate.status == ExecutionStatus.COMPLETED:
                previous = candidate
                break
        execution = await self.execution.execute(
            plan,
            previous=previous,
            generation=len(previous_items) + 1,
            prepare_commit=lambda path: self._prepare_path_resources(
                plan.intent_id,
                path,
                revision=plan.intent_revision,
                plan_id=plan.plan_id,
            ),
            precommit_verify=lambda path: self._trigger_service_validation(
                self._require_spec(plan.intent_id), selected_path=path
            ),
            postcommit_verify=lambda path: self._collect_service_int_path(
                self._require_spec(plan.intent_id), selected_path=path
            ),
            commit_resources=lambda path, receipts: self._commit_path_resources(
                plan.intent_id,
                path,
                receipts,
                plan_id=plan.plan_id,
            ),
            rollback_resources=lambda path, receipts, error: self._rollback_path_resources(
                plan.intent_id,
                path,
                receipts,
                error=error,
            ),
        )
        self.domain.save_execution(execution)
        self.domain.save_decision(
            AgentDecision(
                intent_id=plan.intent_id,
                agent_id="intent_execution",
                decision_type="DEVICE_CONFIG_EXECUTION",
                plan_id=plan.plan_id,
                context_id=plan.context_id,
                inputs={
                    "selected_candidate_id": plan.selected_candidate_id,
                    "required_actions": [
                        item.model_dump(mode="json") for item in plan.required_actions
                    ],
                },
                output=execution.model_dump(mode="json"),
                facts=[
                    f"path={' -> '.join(execution.selected_path)}",
                    f"flows_installed={execution.flows_installed}",
                    f"config_verified={execution.config_verified}",
                ],
            )
        )
        runtime = self.domain.get_runtime(plan.intent_id) or IntentRuntimeState(
            intent_id=plan.intent_id
        )
        if execution.status == ExecutionStatus.COMPLETED:
            # The first committed execution defines the immutable baseline
            # plan identity shown as "Initial Plan".  Healing generations
            # update active/last_healing_plan_id but never overwrite it.
            if not runtime.initial_plan_id and not runtime.active_execution_id:
                runtime.initial_plan_id = plan.plan_id
            runtime.active_plan_id = plan.plan_id
            runtime.active_execution_id = execution.execution_id
            runtime.current_path = list(execution.selected_path)
            runtime.grace_until = datetime.now(timezone.utc) + timedelta(
                seconds=self.grace_seconds
            )
            runtime.last_error = None
            self.domain.save_runtime(runtime)
        return execution

    async def _prepare_path_resources(
        self,
        intent_id: str,
        selected_path: list[str],
        *,
        revision: int | None = None,
        plan_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Install/read back path-coupled resources before route commit."""
        from api.qos import apply_qos_policy
        from api.service_validation import active_policy_for_service
        from models.policy_update import traffic_scope_key
        from models.yang_policy import YANGPolicy
        from persistence.policy_store import PolicyStore

        spec = (
            self.domain.get_spec(intent_id, revision)
            if revision is not None
            else self._require_spec(intent_id)
        )
        if spec is None:
            raise KeyError(intent_id)
        receipts: list[dict[str, Any]] = []
        runtime = self.domain.get_runtime(intent_id)
        desired_policy = (
            YANGPolicy.model_validate(runtime.desired_policy)
            if runtime and runtime.desired_policy
            else None
        )
        if desired_policy is not None and plan_id:
            desired_policy = desired_policy.model_copy(
                update={"source_plan_id": plan_id, "source_plan_status": "PLAN_READY"}
            )
            runtime.desired_policy = desired_policy.model_dump(mode="json")
            self.domain.save_runtime(runtime)
        if desired_policy is not None:
            staged = PolicyStore(self.store).stage(
                desired_policy,
                intent_id,
                {"execution_mode": "MODIFY_QOS_RESOURCE"},
            )
            receipts.append(
                {
                    "resource": "DESIRED_POLICY",
                    "status": "STAGED",
                    "traffic_scope_key": staged.get("traffic_scope_key"),
                    "policy_id": staged.get("policy_id"),
                }
            )
        prepared_policies: dict[str, YANGPolicy] = {}
        try:
            for service_id in [
                str(item) for item in spec.service.get("service_ids") or [] if item
            ]:
                active = active_policy_for_service(service_id)
                policy = desired_policy or (active[0] if active else None)
                if policy is None:
                    receipts.append(
                        {
                            "resource": "TC_QOS",
                            "service_id": service_id,
                            "status": "NOT_APPLICABLE",
                            "reason": "no active bandwidth policy",
                        }
                    )
                    continue
                scope = traffic_scope_key(policy.network_policy).key
                if scope in prepared_policies:
                    receipts.append(
                        {
                            "resource": "TC_QOS",
                            "service_id": service_id,
                            "status": "SHARED_SCOPE_PREPARED",
                            "scope_key": scope,
                        }
                    )
                    continue
                result = await apply_qos_policy(
                    policy, operation="PREPARE", selected_path=selected_path
                )
                prepared_policies[scope] = policy
                receipt = {
                    "resource": "TC_QOS",
                    "service_id": service_id,
                    "scope_key": scope,
                    "status": "VERIFIED" if result.get("config_applied") else "FAILED",
                    "readback": result,
                }
                receipts.append(receipt)
                if not result.get("config_applied"):
                    raise RuntimeError(f"path-coupled QoS readback failed for {service_id}")
        except Exception as exc:
            for policy in prepared_policies.values():
                try:
                    await apply_qos_policy(
                        policy, operation="ROLLBACK", selected_path=selected_path
                    )
                except Exception:
                    pass
            if desired_policy is not None:
                scope = traffic_scope_key(desired_policy.network_policy).key
                PolicyStore(self.store).rollback_staged(scope, error=str(exc))
            raise
        return receipts

    async def _commit_path_resources(
        self,
        intent_id: str,
        selected_path: list[str],
        receipts: list[dict[str, Any]],
        *,
        plan_id: str,
    ) -> dict[str, Any]:
        """Commit prepared TC bindings only after independent path proofs pass."""
        from api.qos import apply_qos_policy
        from models.policy_update import traffic_scope_key
        from models.yang_policy import YANGPolicy
        from persistence.policy_store import PolicyStore

        runtime = self.domain.get_runtime(intent_id)
        if not runtime or not runtime.desired_policy:
            return {"committed": 0, "failed": 0, "reason": "no desired QoS policy"}
        policy = YANGPolicy.model_validate(runtime.desired_policy)
        qos_receipts = [
            item
            for item in receipts
            if item.get("resource") == "TC_QOS" and item.get("status") == "VERIFIED"
        ]
        commits: list[dict[str, Any]] = []
        for _receipt in qos_receipts:
            commits.append(
                await apply_qos_policy(
                    policy, operation="COMMIT", selected_path=selected_path
                )
            )
        scope = traffic_scope_key(policy.network_policy).key
        committed_policy = PolicyStore(self.store).commit_staged(
            scope,
            intent_id=intent_id,
            runtime={
                "plan_id": plan_id,
                "selected_path": list(selected_path),
                "transaction": "MAKE_BEFORE_BREAK_COMMIT",
            },
        )
        if committed_policy:
            from infra.ingress_admission_state import IngressAdmissionDesiredRepository
            IngressAdmissionDesiredRepository(self.store).synchronize_committed_minimum_policy(
                "INGRESS-WC-20260805-03"
            )
        return {
            "committed": len(commits),
            "failed": 0,
            "tc": commits,
            "policy_status": (committed_policy or {}).get("status"),
        }

    async def _rollback_path_resources(
        self,
        intent_id: str,
        selected_path: list[str],
        receipts: list[dict[str, Any]],
        *,
        error: str,
    ) -> dict[str, Any]:
        """Restore the old TC binding and desired-state row after failed proof/readback."""
        from api.qos import apply_qos_policy
        from models.policy_update import traffic_scope_key
        from models.yang_policy import YANGPolicy
        from persistence.policy_store import PolicyStore

        runtime = self.domain.get_runtime(intent_id)
        if not runtime or not runtime.desired_policy:
            return {"rolled_back": 0, "failed": 0, "reason": "no desired QoS policy"}
        policy = YANGPolicy.model_validate(runtime.desired_policy)
        qos_receipts = [
            item
            for item in receipts
            if item.get("resource") == "TC_QOS" and item.get("status") == "VERIFIED"
        ]
        rollbacks: list[dict[str, Any]] = []
        failures: list[str] = []
        for _receipt in qos_receipts:
            try:
                rollbacks.append(
                    await apply_qos_policy(
                        policy, operation="ROLLBACK", selected_path=selected_path
                    )
                )
            except Exception as exc:
                failures.append(str(exc))
        scope = traffic_scope_key(policy.network_policy).key
        PolicyStore(self.store).rollback_staged(scope, error=error)
        return {
            "rolled_back": len(rollbacks),
            "failed": len(failures),
            "tc": rollbacks,
            "errors": failures,
        }

    def _commit_desired_policy(
        self, intent_id: str, execution: IntentExecutionResult
    ) -> dict[str, Any] | None:
        runtime = self.domain.get_runtime(intent_id)
        if not runtime or not runtime.desired_policy:
            return None
        from models.policy_update import traffic_scope_key
        from models.yang_policy import YANGPolicy
        from persistence.policy_store import PolicyStore

        desired = YANGPolicy.model_validate(runtime.desired_policy)
        scope = traffic_scope_key(desired.network_policy).key
        committed = PolicyStore(self.store).commit_staged(
            scope,
            intent_id=intent_id,
            runtime={
                "execution_id": execution.execution_id,
                "plan_id": execution.plan_id,
                "selected_path": execution.selected_path,
                "migration_proofs": [
                    item.model_dump(mode="json") for item in execution.migration_proofs
                ],
            },
        )
        if committed:
            from infra.ingress_admission_state import IngressAdmissionDesiredRepository
            IngressAdmissionDesiredRepository(self.store).synchronize_committed_minimum_policy(
                "INGRESS-WC-20260805-03"
            )
            runtime.active_policy_ids = [str(committed.get("policy_id"))]
            self.domain.save_runtime(runtime)
        return committed

    async def _trigger_service_validation(
        self,
        spec: IntentSpec,
        *,
        selected_path: list[str] | None = None,
    ) -> dict[str, Any]:
        service_ids = [
            str(item) for item in spec.service.get("service_ids") or [] if item
        ]
        if not service_ids:
            return {
                "status": "NO_SERVICE_PROFILE",
                "reason": "active probing requires a bound ServiceProfile",
            }
        from api.service_validation import run_service_validation

        results: dict[str, Any] = {}
        runtime = self.domain.get_runtime(spec.intent_id)
        selected_path = list(selected_path or (runtime.current_path if runtime else []))
        desired_policy = runtime.desired_policy if runtime else {}
        for service_id in service_ids:
            try:
                request = {
                    "intent_id": spec.intent_id,
                    "policy_id": next(iter(self._policy_ids(spec.intent_id)), None),
                    "selected_path": selected_path,
                    # User-declared mandatory objectives are strict.  The
                    # legacy demo probe's 95% operational tolerance must not
                    # weaken an explicit Intent SLA.
                    "minimum_ratio": 1.0,
                }
                result = await run_service_validation(
                    service_id,
                    request,
                    yang_policy=desired_policy or None,
                )
                error_codes = {
                    str(item.get("code") or "")
                    for item in result.get("errors") or []
                    if isinstance(item, dict)
                }
                if "SERVICE_PROBE_TIMEOUT" in error_codes:
                    await asyncio.sleep(2)
                    retry = await run_service_validation(
                        service_id,
                        request,
                        yang_policy=desired_policy or None,
                    )
                    result = {
                        **retry,
                        "retry": {
                            "reason": "SERVICE_PROBE_TIMEOUT",
                            "previous_probe_id": result.get("probe_id"),
                        },
                    }
                results[service_id] = result
            except Exception as exc:
                results[service_id] = {
                    "status": "TELEMETRY_UNAVAILABLE",
                    "error": str(exc),
                }
        return {"status": "COMPLETED", "services": results}

    async def _collect_service_int_path(
        self,
        spec: IntentSpec,
        *,
        selected_path: list[str],
    ) -> dict[str, Any]:
        """Collect a fresh, uncached P4 INT trace after path cleanup/commit."""
        service_ids = [
            str(item) for item in spec.service.get("service_ids") or [] if item
        ]
        if not service_ids:
            return {
                "status": "NO_SERVICE_PROFILE",
                "int_analysis": {},
                "expected_path": [node for node in selected_path if node.startswith("s_")],
            }
        from api._probe_session import get_probe_session
        from api.int_analysis import analyze_int_probe
        from api.int_quality import evaluate_int_probe
        from api.service_profiles import get_service_profile

        profile = get_service_profile(service_ids[0])
        session = await get_probe_session()
        await asyncio.sleep(
            max(
                0.0,
                float(os.getenv("IDN_POST_CLEANUP_P4_SETTLE_SECONDS", "2")),
            )
        )
        raw = await session.get(
            "/probe/int",
            params={
                "src": profile.src_host,
                "dst": profile.dst_host,
                "count": 1,
                "timeout": 4,
            },
        )
        evaluated = evaluate_int_probe(raw or {})
        analysis = analyze_int_probe(evaluated)
        return {
            "status": evaluated.get("status") or "error",
            "source": "P4_INT_SERVICE_ENDPOINT_TRACE",
            "expected_path": [
                node for node in selected_path if node.startswith("s_")
            ],
            "int_analysis": analysis,
            "raw_int_data": evaluated,
        }

    def _select_migration_candidate(
        self, spec: IntentSpec, plan: IntentPlanIR, current_path: list[str]
    ) -> IntentPlanIR:
        domain = getattr(self, "domain", None)
        attempted_paths = {
            tuple(str(node) for node in item.get("selected_path") or [])
            for item in (domain.executions(spec.intent_id) if domain else [])
            if item.get("status") == ExecutionStatus.COMPLETED.value
        }
        alternatives = [
            item
            for item in plan.candidates
            if item.feasible
            and list(item.path) != list(current_path)
            and tuple(str(node) for node in item.path) not in attempted_paths
        ]
        if not alternatives:
            return plan.model_copy(
                update={
                    "selected_candidate_id": None,
                    "required_actions": [],
                    "expected_outcomes": {},
                    "validation_status": PlanValidationStatus.UNSATISFIABLE,
                    "validation_errors": [
                        "no untried feasible path differs from the active path; previously deployed paths did not satisfy the intent"
                    ],
                    "selected_reasoning": [
                        "anti-flapping rejected identical or previously attempted path redeployment"
                    ],
                }
            )
        selected = max(alternatives, key=lambda item: item.score)
        updated = plan.model_copy(
            update={
                "selected_candidate_id": selected.candidate_id,
                "required_actions": list(selected.required_actions),
                "expected_outcomes": dict(selected.expected_outcomes),
                "selected_reasoning": [
                    "violation-driven healing requires a path different from the active path",
                    *selected.deterministic_facts,
                ],
            }
        )
        return self.planning.validator.validate(spec, updated)

    def _active_plan_path(self, intent_id: str, plan_id: str | None) -> list[str]:
        for raw in self.domain.plans(intent_id):
            if plan_id and raw.get("plan_id") != plan_id:
                continue
            plan = IntentPlanIR.model_validate(raw)
            selected = next(
                (
                    item
                    for item in plan.candidates
                    if item.candidate_id == plan.selected_candidate_id
                ),
                None,
            )
            return list(selected.path) if selected else []
        return []

    def _active_plan(
        self, intent_id: str, plan_id: str | None
    ) -> IntentPlanIR | None:
        for raw in self.domain.plans(intent_id):
            if plan_id and raw.get("plan_id") != plan_id:
                continue
            return IntentPlanIR.model_validate(raw)
        return None

    @staticmethod
    def _observed_bandwidth(assurance: IntentAssuranceResult) -> float | None:
        for item in assurance.assessments:
            if item.objective_type == ObjectiveType.MIN_BANDWIDTH:
                try:
                    return float(item.observed) if item.observed is not None else None
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _required_bandwidth(spec: IntentSpec) -> float | None:
        for item in spec.objectives:
            if item.type == ObjectiveType.MIN_BANDWIDTH:
                try:
                    return float(item.value)
                except (TypeError, ValueError):
                    return None
        return None

    async def _complete_preference_runtime(
        self,
        *,
        intent_id: str,
        spec: IntentSpec,
        relation_results: list[dict[str, Any]],
    ) -> IntentRuntimeState:
        """Commit and verify a scheduler-only preference without device writes."""
        projection = scheduler_projection(spec.preferences)
        if any(item.dimension.value == "PATH_PREFERENCE" for item in spec.preferences):
            from api.topology import collect_current_topology

            projection["path_selection"] = path_preference_projection(
                spec.preferences,
                src_host=str((spec.source or {}).get("endpoint_id") or ""),
                dst_host=str((spec.destination or {}).get("endpoint_id") or ""),
                service_id=next(
                    (item for item in (spec.service or {}).get("service_ids", [])
                     if item not in {scope for relation in spec.preferences for scope in (relation.higher_scope, relation.lower_scope)}),
                    "",
                ),
                runtime_topology=await collect_current_topology(),
            )
        effective = [item["effective"] for item in relation_results]
        context_id = f"CTX-PREFERENCE-{intent_id}"
        plan = IntentPlanIR(
            intent_id=intent_id,
            intent_revision=spec.revision,
            context_id=context_id,
            selected_candidate_id="CAND-CONTROL-PLANE-SCHEDULER",
            selected_reasoning=[
                "validated ServiceProfile relation projected to the control-plane scheduler",
                "southbound_mutation_required=false",
            ],
            validation_status=PlanValidationStatus.VALID,
            expected_outcomes={
                "execution_capability": "CONTROL_PLANE_SCHEDULING_PREFERENCE",
                "southbound_mutation_required": False,
                "effective_relations": effective,
                "scheduler_projection": projection,
                "p4_writes": 0,
                "tc_writes": 0,
                "onos_writes": 0,
            },
            llm_status=spec.llm_status,
            llm_runtime_mode=spec.llm_runtime_mode,
            llm_call_id=spec.llm_call_id,
            llm_provider=spec.llm_provider,
            llm_model=spec.llm_model,
            llm_structured_output_valid=spec.llm_structured_output_valid,
            llm_validator_result=spec.llm_validator_result,
            fallback_used=spec.fallback_used,
        )
        self.domain.save_plan(plan)
        for lifecycle, actor, reason in (
            (IntentLifecycle.PARSED, "intent_understanding", "preference relation resolved to registered services"),
            (IntentLifecycle.SENSING, "network_context_builder", "control-plane scheduler context captured"),
            (IntentLifecycle.PLANNING, "preference_scheduler", "scheduler ordering validated"),
            (IntentLifecycle.PLAN_READY, "plan_validator", "scheduler-only plan is valid"),
        ):
            self.domain.transition(
                intent_id, lifecycle, actor=actor, reason=reason,
                data={"execution_capability": "CONTROL_PLANE_SCHEDULING_PREFERENCE", "southbound_execution_permitted": False},
                plan_id=plan.plan_id, context_id=context_id,
            )
        runtime = self.domain.get_runtime(intent_id) or IntentRuntimeState(intent_id=intent_id)
        runtime.active_plan_id = plan.plan_id
        runtime.context_id = context_id
        runtime.desired_policy = {
            "execution_capability": "CONTROL_PLANE_SCHEDULING_PREFERENCE",
            "southbound_mutation_required": False,
            "effective_relations": effective,
            "scheduler_projection": projection,
        }
        self.domain.save_runtime(runtime)
        execution = IntentExecutionResult(
            intent_id=intent_id,
            plan_id=plan.plan_id,
            context_id=context_id,
            selected_candidate_id="CAND-CONTROL-PLANE-SCHEDULER",
            status=ExecutionStatus.COMPLETED,
            config_verified=True,
            commit_verified=True,
            flows_installed=0,
            phase_history=[{
                "phase": "CONTROL_PLANE_SCHEDULER_READBACK",
                "effective_relations": effective,
                "scheduler_projection": projection,
                "southbound_writes": {"p4": 0, "tc": 0, "onos": 0},
            }],
        )
        self.domain.save_execution(execution)
        runtime.active_execution_id = execution.execution_id
        self.domain.save_runtime(runtime)
        for lifecycle, actor, reason in (
            (IntentLifecycle.CHECKING, "conflict_safety", "control-plane preference has no dataplane conflict surface"),
            (IntentLifecycle.DEPLOYING, "preference_scheduler", "scheduler projection committed without southbound mutation"),
            (IntentLifecycle.VERIFYING, "status_verifier", "effective scheduler relation read back"),
        ):
            self.domain.transition(intent_id, lifecycle, actor=actor, reason=reason, plan_id=plan.plan_id, context_id=context_id,
                data={"effective_relations": effective, "southbound_writes": {"p4": 0, "tc": 0, "onos": 0}})
        evidence = EvidenceItem(
            intent_id=intent_id,
            source="preference_relation_store",
            metric="effective_scheduler_relation",
            value=effective,
            quality=EvidenceQuality.TRUSTED,
            correlation={"plan_id": plan.plan_id, "execution_id": execution.execution_id},
            raw={"scheduler_projection": projection, "readback": effective},
        )
        self.domain.save_evidence(intent_id, evidence)
        assurance = IntentAssuranceResult(
            intent_id=intent_id,
            plan_id=plan.plan_id,
            context_id=context_id,
            execution_id=execution.execution_id,
            lifecycle=IntentLifecycle.SATISFIED,
            evidence_quality=EvidenceQuality.TRUSTED,
            satisfied=True,
            summary="effective scheduler relation read back and matches desired preference; no southbound mutation required",
        )
        self.domain.save_assurance(assurance)
        runtime = self.domain.get_runtime(intent_id) or runtime
        runtime.last_assurance_id = assurance.assurance_id
        runtime.assurance_status = "PREFERENCE_RELATION_READBACK_VERIFIED"
        runtime.last_assurance_at = datetime.now(timezone.utc)
        runtime.evidence_refs = list(dict.fromkeys([*runtime.evidence_refs, evidence.evidence_id]))
        self.domain.save_runtime(runtime)
        return self.domain.transition(
            intent_id, IntentLifecycle.SATISFIED, actor="status_verifier",
            reason="PREFERENCE_RELATION_READBACK_VERIFIED", plan_id=plan.plan_id, context_id=context_id,
            data={"assurance_id": assurance.assurance_id, "effective_relations": effective, "southbound_writes": {"p4": 0, "tc": 0, "onos": 0}},
        )

    def _validate_preference_relations(self, spec: IntentSpec) -> None:
        """Validate new relations against every active relation in the domain.

        This runs before conflict/compile/execution.  A rejected cycle therefore
        cannot allocate a binding or issue an ONOS, P4Runtime or TC mutation.
        """
        replacing_scopes = {preference_scope_key(item) for item in spec.preferences}
        relations = list(spec.preferences)
        for stored in self.preference_relations.active():
            # The same unordered pair is an update/reuse candidate, not a
            # simultaneous second edge.  All other active relations remain in
            # cycle validation.
            if stored["scope_key"] in replacing_scopes:
                continue
            relations.append(PreferenceRelation.model_validate(stored["relation"]))
        validate_acyclic(relations)

    def _check_conflicts(self, spec: IntentSpec) -> list[IntentConflict]:
        conflicts: list[IntentConflict] = []
        for record in self.store.list(page=1, page_size=100).get("items", []):
            other_id = str(record.get("intent_id"))
            if other_id == spec.intent_id:
                continue
            other_runtime = self.domain.get_runtime(other_id)
            if not other_runtime or other_runtime.lifecycle in {
                IntentLifecycle.WITHDRAWN,
                IntentLifecycle.FAILED,
                IntentLifecycle.EXECUTION_FAILED,
            }:
                continue
            other = self.domain.get_spec(other_id)
            if not other:
                continue
            scope_relation = self._scope_relation(spec, other)
            selectors_overlap = scope_relation in {"SAME_SCOPE", "OVERLAP"}
            destination = spec.selector.get("destination_ip")
            same_destination = bool(
                destination and destination == other.selector.get("destination_ip")
            )
            if same_destination and not selectors_overlap:
                current_path = self._active_plan_path(
                    spec.intent_id,
                    (
                        self.domain.get_runtime(spec.intent_id)
                        or IntentRuntimeState(intent_id=spec.intent_id)
                    ).active_plan_id,
                )
                other_path = list(other_runtime.current_path) or self._active_plan_path(
                    other_id, other_runtime.active_plan_id
                )
                if current_path and other_path and current_path != other_path:
                    conflicts.append(
                        IntentConflict(
                            intent_id=spec.intent_id,
                            conflicting_intent_id=other.intent_id,
                            scope_relation="OVERLAP",
                            conflict_type="RESOURCE_CONFLICT",
                            severity="high",
                            facts=[
                                "P4 ipv4_routing matches destination /32",
                                "different paths target the same destination",
                            ],
                            resolutions=[
                                {"type": "USE_SHARED_PATH"},
                                {"type": "EXTEND_PIPELINE_SELECTOR"},
                                {"type": "NEEDS_APPROVAL"},
                            ],
                            status="OPEN",
                        )
                    )
                continue
            if not selectors_overlap:
                continue
            left = {(item.type.value, str(item.value)) for item in spec.objectives}
            right = {(item.type.value, str(item.value)) for item in other.objectives}
            explicit_update = (
                spec.validity.get("supersedes_intent_id") == other.intent_id
            )
            shared_types = {kind for kind, _ in left} & {kind for kind, _ in right}
            if left == right:
                conflict_type = "DUPLICATE"
                severity = "low"
            elif explicit_update:
                conflict_type = "UPDATE"
                severity = "low"
            elif self._priority(spec.priority) < self._priority(other.priority):
                conflict_type = "DOMINATED"
                severity = "medium"
            elif shared_types & {"MIN_BANDWIDTH", "MAX_BANDWIDTH", "PRIORITY"}:
                conflict_type = "RESOURCE_CONFLICT"
                severity = "high"
            elif shared_types:
                conflict_type = "POLICY_CONFLICT"
                severity = "high"
            else:
                continue
            if conflict_type:
                winner = (
                    spec.intent_id
                    if self._priority(spec.priority) > self._priority(other.priority)
                    else other.intent_id
                )
                conflicts.append(
                    IntentConflict(
                        intent_id=spec.intent_id,
                        conflicting_intent_id=other.intent_id,
                        scope_relation=scope_relation,
                        conflict_type=conflict_type,
                        severity=severity,
                        facts=[
                            f"scope relation is {scope_relation}",
                            f"classification is {conflict_type}",
                        ],
                        resolutions=[
                            {"type": "PRIORITY", "winner": winner},
                            {"type": "NARROW_SELECTOR"},
                            {"type": "REJECT_NEW_INTENT"},
                        ],
                        selected_resolution=(
                            {"type": "NOOP", "reason": conflict_type}
                            if conflict_type in {"DUPLICATE", "UPDATE"}
                            else (
                                {"type": "PRIORITY", "winner": winner}
                                if winner == other.intent_id
                                else None
                            )
                        ),
                        status=(
                            "RESOLVED"
                            if conflict_type in {"DUPLICATE", "UPDATE"}
                            or winner == spec.intent_id
                            else "OPEN"
                        ),
                    )
                )
        self.domain.save_decision(
            AgentDecision(
                intent_id=spec.intent_id,
                intent_revision=spec.revision,
                agent_id="conflict_safety",
                decision_type="CONFLICT_SAFETY_CHECK",
                inputs={"selector": spec.selector, "priority": spec.priority},
                output={
                    "conflicts": [item.model_dump(mode="json") for item in conflicts]
                },
                facts=(
                    ["blocking conflicts require approval"]
                    if conflicts
                    else ["no blocking intent conflict found"]
                ),
                rationale_summary="deterministic multi-intent selector, priority, and data-plane safety evaluation",
            )
        )
        return conflicts

    @staticmethod
    def _selectors_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
        shared = set(left) & set(right)
        return bool(shared) and all(left[key] == right[key] for key in shared)

    @classmethod
    def _scope_relation(cls, left: IntentSpec, right: IntentSpec) -> str:
        if (
            left.scope == right.scope
            and left.tenant_id == right.tenant_id
            and left.selector == right.selector
        ):
            return "SAME_SCOPE"
        if cls._selectors_overlap(left.selector, right.selector):
            return "OVERLAP"
        return "DISJOINT"

    @staticmethod
    def _priority(value: str) -> int:
        return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(
            str(value).lower(), 2
        )

    @staticmethod
    def _healing_actions(
        category: RootCauseCategory,
        spec: IntentSpec,
        context,
    ) -> list[HealingAction]:
        evidence_refs = list(context.evidence_refs)
        reroute_categories = {
            RootCauseCategory.PATH_FAILURE,
            RootCauseCategory.PATH_CONGESTION,
            RootCauseCategory.DEVICE_UNAVAILABLE,
        }
        if category in reroute_categories:
            return [
                HealingAction(
                    type=HealingActionType.REROUTE,
                    target=spec.intent_id,
                    params={"planner": "ConstrainedPathPlanner", "reason": "INTENT_VIOLATION"},
                    reason=f"{category.value} requires a different feasible path",
                    expected_effect={"path_changed": True, "sla_remeasured": True},
                    risk="medium",
                    confidence=0.0,
                ),
                HealingAction(
                    type=HealingActionType.THROTTLE_LOWER_PRIORITY,
                    target="eligible_preemptible_intents",
                    params={"evidence_refs": evidence_refs},
                    reason="fallback only when protected-intent analysis permits degradation",
                    expected_effect={"free_capacity": True},
                    risk="high",
                    confidence=0.0,
                ),
            ]
        if category == RootCauseCategory.TC_CONFIGURATION_DRIFT:
            return [
                HealingAction(
                    type=HealingActionType.QOS_REALLOCATION,
                    target=spec.intent_id,
                    reason="TC readback differs from desired QoS allocation",
                    expected_effect={"tc_readback_matches": True},
                    risk="low",
                )
            ]
        if category == RootCauseCategory.P4_ENTRY_MISSING:
            return [
                HealingAction(
                    type=HealingActionType.POLICY_UPDATE,
                    target=spec.intent_id,
                    reason="P4 entry readback is missing",
                    expected_effect={"p4_readback_matches": True},
                    risk="low",
                )
            ]
        if category == RootCauseCategory.CAPACITY_EXHAUSTED:
            return [
                HealingAction(
                    type=HealingActionType.THROTTLE_LOWER_PRIORITY,
                    target="eligible_preemptible_intents",
                    reason="all candidate paths are capacity constrained",
                    expected_effect={"free_capacity": True},
                    risk="high",
                )
            ]
        return [
            HealingAction(
                type=HealingActionType.NO_FEASIBLE_ACTION,
                target=spec.intent_id,
                reason="telemetry or causal evidence is insufficient for a safe action",
                expected_effect={},
                risk="low",
            )
        ]

    @staticmethod
    def _healing_confidence(
        diagnosis_confidence: float,
        evidence: list[EvidenceItem],
        context,
        *,
        alternative_count: int,
    ) -> float:
        usable = [
            item
            for item in evidence
            if item.quality in {EvidenceQuality.TRUSTED, EvidenceQuality.DEGRADED}
        ]
        coverage = min(len(usable) / max(len(evidence), 1), 1.0)
        consistency = max(0.0, min(float(diagnosis_confidence), 1.0))
        freshness = {
            "TRUSTED": 1.0,
            "DEGRADED": 0.65,
            "STALE": 0.25,
            "UNTRUSTED": 0.15,
            "UNAVAILABLE": 0.0,
        }.get(str(context.evidence_quality.value), 0.0)
        alternatives = min(max(alternative_count - 1, 0) / 2, 1.0)
        return round(
            0.35 * coverage
            + 0.30 * consistency
            + 0.25 * freshness
            + 0.10 * alternatives,
            3,
        )

    def _analyze_affected_intents(
        self,
        spec: IntentSpec,
        context,
        candidate_id: str | None,
    ) -> AffectedIntentAnalysis:
        candidate = next(
            (
                item
                for item in context.candidate_paths
                if item.get("candidate_id") == candidate_id
            ),
            {},
        )
        affected_ids = [
            str(value)
            for value in candidate.get("affected_intent_ids") or []
            if value and str(value) != spec.intent_id
        ]
        affected: list[dict[str, Any]] = []
        protected: list[str] = []
        degradable: list[str] = []
        for intent_id in affected_ids:
            other = self.domain.get_spec(intent_id)
            if other is None:
                protected.append(intent_id)
                affected.append(
                    {
                        "intent_id": intent_id,
                        "classification": "PROTECTED_UNKNOWN",
                        "preemptible": False,
                    }
                )
                continue
            preemptible = bool(other.validity.get("preemptible", False))
            best_effort = other.priority.lower() == "low" and preemptible
            classification = "BEST_EFFORT_PREEMPTIBLE" if best_effort else "PROTECTED"
            affected.append(
                {
                    "intent_id": intent_id,
                    "priority": other.priority,
                    "preemptible": preemptible,
                    "classification": classification,
                }
            )
            (degradable if best_effort else protected).append(intent_id)
        # A pure reroute does not degrade a protected intent by itself.  The
        # separate conflict checker blocks selector/path collisions; this
        # analysis gates only actions that consume insufficient shared capacity.
        available = candidate.get("available_capacity_mbps")
        reserved_for_protected = sum(
            float(item.get("bandwidth_mbps") or item.get("amount") or 0)
            for item in context.reservations
            if str(item.get("intent_id") or "") in protected
        )
        capacity_conflict = bool(
            protected
            and available is not None
            and float(available) < reserved_for_protected
        )
        return AffectedIntentAnalysis(
            intent_id=spec.intent_id,
            candidate_id=candidate_id,
            affected_intents=affected,
            protected_intent_ids=protected,
            auto_degradable_intent_ids=degradable,
            safe=not capacity_conflict,
            requires_approval=capacity_conflict,
            reasons=(
                ["candidate capacity would consume protected reservations"]
                if capacity_conflict
                else ["reroute does not degrade another intent"]
            ),
        )

    @staticmethod
    def _correlation_data(proposal: HealingAttempt) -> dict[str, Any]:
        return {
            "healing_id": proposal.healing_id,
            "violation_id": proposal.violation_id,
            "assurance_id": proposal.assurance_id,
            "root_cause_id": proposal.root_cause_id,
            "source_context_id": proposal.source_context_id,
            "source_plan_id": proposal.source_plan_id,
            "source_execution_id": proposal.source_execution_id,
            "plan_id": proposal.plan_id,
            "execution_id": proposal.execution_id,
        }

    def _record_healing_event(
        self,
        proposal: HealingAttempt,
        event_type: str,
        *,
        actor: str,
        reason: str,
        context_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.domain.record_event(
            proposal.intent_id,
            event_type,
            actor=actor,
            reason=reason,
            plan_id=proposal.plan_id or proposal.source_plan_id,
            context_id=context_id or proposal.source_context_id,
            data={**self._correlation_data(proposal), **(data or {})},
        )

    def _mark_unsatisfiable(self, intent_id: str, reason: str) -> None:
        healing = self.domain.healing(intent_id)
        plans = self.domain.plans(intent_id)
        executions = self.domain.executions(intent_id)
        assurances = self.domain.assurance(intent_id)
        contexts = self.domain.contexts(intent_id)
        spec = self.domain.get_spec(intent_id)
        runtime = self.domain.get_runtime(intent_id)
        latest_assurance = assurances[0] if assurances else {}
        latest_context = contexts[0] if contexts else {}
        requested_sla = [
            item.model_dump(mode="json")
            for item in (spec.objectives if spec else [])
        ]
        evaluated_candidates = [
            evaluation
            for item in healing
            for evaluation in item.get("candidate_evaluations") or []
        ]
        unsat_evidence = {
            "reason_code": "NO_FEASIBLE_HEALING_ACTION",
            "requested_sla": requested_sla,
            "current_measurement": (
                runtime.observed_metrics if runtime else {}
            ),
            "latest_assurance_id": latest_assurance.get("assurance_id"),
            "bottleneck_candidates": latest_context.get("bottleneck_candidates") or [],
            "current_path": list(runtime.current_path) if runtime else [],
            "evaluated_candidates": evaluated_candidates,
            "rejection_reasons": [
                {
                    "candidate_id": item.get("candidate_id"),
                    "reason_code": item.get("reason_code"),
                    "reason": item.get("reason"),
                }
                for item in evaluated_candidates
                if item.get("validator_result") == "REJECTED"
            ],
            "maximum_achievable_estimate_mbps": max(
                [
                    float(
                        (item.get("expected_effect") or {}).get(
                            "expected_available_mbps"
                        )
                    )
                    for item in evaluated_candidates
                    if (item.get("expected_effect") or {}).get(
                        "expected_available_mbps"
                    )
                    is not None
                ],
                default=None,
            ),
            "effective_attempts": runtime.healing_attempts if runtime else 0,
            "skipped_no_effect": runtime.skipped_healing_actions if runtime else 0,
        }
        self.domain.transition(
            intent_id,
            IntentLifecycle.UNSATISFIABLE,
            actor="healing_controller",
            reason=reason,
            data={
                "tried_plan_ids": [item.get("plan_id") for item in plans],
                "tried_execution_ids": [
                    item.get("execution_id") for item in executions
                ],
                "attempt_outcomes": [
                    {
                        "healing_id": item.get("healing_id"),
                        "attempt": item.get("attempt"),
                        "status": item.get("status"),
                        "outcome": item.get("outcome"),
                        "reason": item.get("reason"),
                    }
                    for item in healing
                ],
                "candidate_summaries": [
                    candidate
                    for item in healing
                    for candidate in item.get("candidates") or []
                ],
                "capability_limit": reason,
                "unsatisfiable_evidence": unsat_evidence,
            },
        )
        self.domain.record_event(
            intent_id,
            "INTENT_UNSATISFIABLE",
            actor="healing_controller",
            reason=reason,
            data={
                "tried_plan_ids": [item.get("plan_id") for item in plans],
                "tried_execution_ids": [
                    item.get("execution_id") for item in executions
                ],
                "healing_ids": [item.get("healing_id") for item in healing],
                "unsatisfiable_evidence": unsat_evidence,
            },
        )
        self._record_healing_final(intent_id, IntentLifecycle.UNSATISFIABLE.value)

    def _record_healing_attempt(self, proposal: HealingAttempt) -> None:
        self.experience.record_case(
            {
                "case_id": f"CASE-{proposal.healing_id}-ATTEMPT",
                "record_type": "HEALING_ATTEMPT",
                "intent_id": proposal.intent_id,
                "intent_revision": proposal.revision,
                "healing_id": proposal.healing_id,
                "attempt": proposal.attempt,
                "root_cause": proposal.root_cause_id,
                "repair_action": [
                    proposal.selected_action.type.value
                    if proposal.selected_action
                    else HealingActionType.NO_FEASIBLE_ACTION.value
                ],
                "plan_id": proposal.plan_id,
                "execution_id": proposal.execution_id,
                "before_assurance_id": proposal.before_assurance_id,
                "after_assurance_id": proposal.after_assurance_id,
                "success": proposal.status == HealingProposalStatus.VERIFIED,
                "attempt_status": proposal.status.value,
                "attempt_outcome": proposal.outcome,
                "evidence": proposal.evidence_refs,
                "lesson": proposal.reason,
            }
        )

    def _actual_healing_execution_attempts(self, intent_id: str) -> int:
        """Count only persisted attempts that explicitly changed the data plane."""

        return sum(
            1
            for item in self.domain.healing(intent_id)
            if item.get("data_plane_changed") is True
        )

    def _record_healing_final(self, intent_id: str, outcome: str) -> None:
        attempts = self.domain.healing(intent_id)
        self.experience.record_case(
            {
                "case_id": f"CASE-{intent_id}-FINAL-{len(attempts)}",
                "record_type": "HEALING_FINAL_OUTCOME",
                "intent_id": intent_id,
                "attempt_ids": [item.get("healing_id") for item in attempts],
                "repair_action": [
                    (item.get("selected_action") or {}).get("type")
                    for item in attempts
                    if (item.get("selected_action") or {}).get("type")
                ],
                "success": outcome == IntentLifecycle.SATISFIED.value,
                "final_outcome": outcome,
                "lesson": (
                    "bounded healing ended with a newly measured satisfied intent"
                    if outcome == IntentLifecycle.SATISFIED.value
                    else "all safe candidates were exhausted; do not learn a failed attempt as success"
                ),
            }
        )

    def _policy_ids(self, intent_id: str) -> list[str]:
        with self.store._connect() as connection:
            return [
                str(row["policy_id"])
                for row in connection.execute(
                    "SELECT policy_id FROM policies WHERE intent_id=?", (intent_id,)
                ).fetchall()
            ]

    def _require_record(self, intent_id: str) -> dict[str, Any]:
        record = self.store.get(intent_id)
        if not record:
            raise KeyError(intent_id)
        return record

    def _require_spec(self, intent_id: str) -> IntentSpec:
        runtime = self.domain.get_runtime(intent_id)
        spec = self.domain.get_spec(
            intent_id,
            runtime.current_revision if runtime is not None else None,
        )
        if not spec:
            raise KeyError(intent_id)
        return spec

    def _lock(self, intent_id: str) -> asyncio.Lock:
        return self._intent_locks.setdefault(intent_id, asyncio.Lock())


_control_plane: IntentControlPlane | None = None


def get_intent_control_plane() -> IntentControlPlane:
    global _control_plane
    current = get_store()
    if _control_plane is None or _control_plane.store is not current:
        _control_plane = IntentControlPlane(current, get_intent_domain_store())
    return _control_plane

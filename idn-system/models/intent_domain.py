"""First-class intent-domain contracts used by the control plane.

These models deliberately contain no device commands.  Agents may propose and
explain typed objects, while deterministic validators and executors retain the
authority to change network state.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class IntentLifecycle(str, Enum):
    RECEIVED = "RECEIVED"
    UNDERSTANDING = "UNDERSTANDING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    PARSED = "PARSED"
    SENSING = "SENSING"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    CHECKING = "CHECKING"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    DEPLOYING = "DEPLOYING"
    VERIFYING = "VERIFYING"
    SATISFIED = "SATISFIED"
    DEGRADED = "DEGRADED"
    VIOLATED = "VIOLATED"
    # A measured objective failed after the policy was installed and read back.
    # Keep this distinct from plan feasibility and southbound execution failure.
    ASSURANCE_FAILED = "ASSURANCE_FAILED"
    DIAGNOSING = "DIAGNOSING"
    REPLANNING = "REPLANNING"
    REDEPLOYING = "REDEPLOYING"
    UNSATISFIABLE = "UNSATISFIABLE"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    FAILED = "FAILED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    CANCELLED_AFTER_COMMIT = "CANCELLED_AFTER_COMMIT"
    WITHDRAW_REQUESTED = "WITHDRAW_REQUESTED"
    WITHDRAW_PENDING_CLEANUP = "WITHDRAW_PENDING_CLEANUP"
    WITHDRAW_FAILED = "WITHDRAW_FAILED"
    WITHDRAWN = "WITHDRAWN"


class ObjectiveType(str, Enum):
    CONNECTIVITY = "CONNECTIVITY"
    MIN_BANDWIDTH = "MIN_BANDWIDTH"
    MAX_BANDWIDTH = "MAX_BANDWIDTH"
    MAX_LATENCY = "MAX_LATENCY"
    MAX_PACKET_LOSS = "MAX_PACKET_LOSS"
    MAX_JITTER = "MAX_JITTER"
    AVAILABILITY = "AVAILABILITY"
    PRIORITY = "PRIORITY"
    ISOLATION = "ISOLATION"


class ConstraintType(str, Enum):
    AVOID_LINK = "AVOID_LINK"
    AVOID_DEVICE = "AVOID_DEVICE"
    REQUIRE_DEVICE = "REQUIRE_DEVICE"
    PREFER_PATH = "PREFER_PATH"
    TENANT_ISOLATION = "TENANT_ISOLATION"
    TIME_WINDOW = "TIME_WINDOW"


class ComparisonOperator(str, Enum):
    EQ = "EQ"
    GTE = "GTE"
    LTE = "LTE"
    IN = "IN"
    NOT_IN = "NOT_IN"


class ValueOrigin(str, Enum):
    USER_EXPLICIT = "USER_EXPLICIT"
    USER_EXPLICIT_UI = "USER_EXPLICIT_UI"
    SEMANTIC_RESOLUTION = "SEMANTIC_RESOLUTION"
    ENDPOINT_REGISTRY = "ENDPOINT_REGISTRY"
    ORGANIZATION_POLICY = "ORGANIZATION_POLICY"
    SERVICE_PROFILE = "SERVICE_PROFILE"
    SYSTEM_DEFAULT = "SYSTEM_DEFAULT"
    INFERRED = "INFERRED"


class PreferenceDimension(str, Enum):
    """The decision surface affected by a relative preference.

    A preference deliberately is not an SLA or a flow-table priority.  It is a
    business-level ordering that the candidate planner and scheduler may use
    only after the normal hard constraints have passed.
    """

    SERVICE_PRIORITY = "SERVICE_PRIORITY"
    PATH_PREFERENCE = "PATH_PREFERENCE"


class PreferenceStrength(str, Enum):
    SOFT = "SOFT"
    HARD = "HARD"


class PreferenceRelation(BaseModel):
    """A typed, persisted directed preference edge.

    ``higher_scope -> lower_scope`` means that the higher scope is preferred.
    Relations are kept in the IntentSpec revision, so they are subject to the
    same history, ownership and withdrawal semantics as the intent that
    introduced them.  They never alter a bandwidth objective by themselves.
    """

    relation_id: str = Field(default_factory=lambda: new_id("PREF"))
    higher_scope: str = Field(min_length=1)
    lower_scope: str = Field(min_length=1)
    dimension: PreferenceDimension
    strength: PreferenceStrength = PreferenceStrength.SOFT
    origin: ValueOrigin = ValueOrigin.USER_EXPLICIT
    source_text: str | None = None
    explicit: bool = True

    @model_validator(mode="after")
    def distinct_scopes(self) -> "PreferenceRelation":
        if self.higher_scope.casefold() == self.lower_scope.casefold():
            raise ValueError("INVALID_SELF_PREFERENCE")
        return self


class EvidenceQuality(str, Enum):
    TRUSTED = "TRUSTED"
    DEGRADED = "DEGRADED"
    UNTRUSTED = "UNTRUSTED"
    STALE = "STALE"
    MISSING = "MISSING"


class ContextEvidenceQuality(str, Enum):
    """Decision usability of a complete network-context snapshot."""

    TRUSTED = "TRUSTED"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    UNTRUSTED = "UNTRUSTED"
    UNAVAILABLE = "UNAVAILABLE"


class ContextSource(str, Enum):
    ONOS = "ONOS"
    MININET = "MININET"
    P4RUNTIME = "P4RUNTIME"
    TC = "TC"
    TELEMETRY = "TELEMETRY"
    DATABASE = "DATABASE"
    RESOURCE_LEDGER = "RESOURCE_LEDGER"


class ContextSourceState(BaseModel):
    source: ContextSource
    available: bool = False
    trusted: bool = False
    stale: bool = False
    observed_at: datetime | None = None
    epoch: str | None = None
    version: str | None = None
    error: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class AssessmentStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class PlanValidationStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNSATISFIABLE = "UNSATISFIABLE"


class PlanningRiskStatus(str, Enum):
    EMPIRICALLY_SUPPORTED = "EMPIRICALLY_SUPPORTED"
    DEGRADED_CONTEXT = "DEGRADED_CONTEXT"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    UNTRUSTED_EVIDENCE = "UNTRUSTED_EVIDENCE"
    UNVERIFIED_CAPABILITY = "UNVERIFIED_CAPABILITY"
    INFEASIBLE = "INFEASIBLE"


class ExecutionStatus(str, Enum):
    PREPARING = "PREPARING"
    INSTALLING = "INSTALLING"
    READING_BACK = "READING_BACK"
    VERIFYING_CONFIG = "VERIFYING_CONFIG"
    COMMITTING = "COMMITTING"
    COMPLETED = "COMPLETED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


class RevisionStatus(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    DEPLOYING = "DEPLOYING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"


class RootCauseCategory(str, Enum):
    P4_ENTRY_MISSING = "P4_ENTRY_MISSING"
    TC_CONFIGURATION_DRIFT = "TC_CONFIGURATION_DRIFT"
    PATH_FAILURE = "PATH_FAILURE"
    PATH_CONGESTION = "PATH_CONGESTION"
    PATH_SEGMENT_CONGESTION = "PATH_SEGMENT_CONGESTION"
    SHARED_BOTTLENECK_CONGESTION = "SHARED_BOTTLENECK_CONGESTION"
    ACCESS_UPLINK_CONGESTION = "ACCESS_UPLINK_CONGESTION"
    CORE_PATH_CONGESTION = "CORE_PATH_CONGESTION"
    EGRESS_CONGESTION = "EGRESS_CONGESTION"
    RESOURCE_CONTENTION = "RESOURCE_CONTENTION"
    UNKNOWN_CONGESTION = "UNKNOWN_CONGESTION"
    DEVICE_UNAVAILABLE = "DEVICE_UNAVAILABLE"
    CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"
    TELEMETRY_INSUFFICIENT = "TELEMETRY_INSUFFICIENT"
    UNKNOWN = "UNKNOWN"


class HealingActionType(str, Enum):
    """Only actions that the autonomous-healing policy may reason about."""

    REROUTE = "REROUTE"
    QOS_REALLOCATION = "QOS_REALLOCATION"
    THROTTLE_LOWER_PRIORITY = "THROTTLE_LOWER_PRIORITY"
    RELEASE_STALE_RESERVATION = "RELEASE_STALE_RESERVATION"
    POLICY_UPDATE = "POLICY_UPDATE"
    NO_FEASIBLE_ACTION = "NO_FEASIBLE_ACTION"


class RootCauseScope(str, Enum):
    PATH_SPECIFIC = "PATH_SPECIFIC"
    SHARED_PATH = "SHARED_PATH"
    ACCESS = "ACCESS"
    EGRESS = "EGRESS"
    GLOBAL = "GLOBAL"
    UNKNOWN = "UNKNOWN"


class HealingActionApplicability(str, Enum):
    APPLICABLE = "APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    REDUNDANT = "REDUNDANT"
    NO_EXPECTED_EFFECT = "NO_EXPECTED_EFFECT"
    UNSAFE = "UNSAFE"


class HealingRejectionReason(str, Enum):
    NONE = "NONE"
    TARGET_PATH_ALREADY_ACTIVE = "TARGET_PATH_ALREADY_ACTIVE"
    BOTTLENECK_STILL_ON_PATH = "BOTTLENECK_STILL_ON_PATH"
    SHARED_BOTTLENECK_NOT_AVOIDED = "SHARED_BOTTLENECK_NOT_AVOIDED"
    REPEATED_FAILED_ACTION = "REPEATED_FAILED_ACTION"
    NO_PREEMPTIBLE_RESOURCE = "NO_PREEMPTIBLE_RESOURCE"
    PROTECTED_INTENT = "PROTECTED_INTENT"
    INSUFFICIENT_RECOVERABLE_CAPACITY = "INSUFFICIENT_RECOVERABLE_CAPACITY"
    INSUFFICIENT_EXPECTED_CAPACITY = "INSUFFICIENT_EXPECTED_CAPACITY"
    NO_OP_PLAN = "NO_OP_PLAN"
    NO_FEASIBLE_HEALING_ACTION = "NO_FEASIBLE_HEALING_ACTION"
    TELEMETRY_INSUFFICIENT = "TELEMETRY_INSUFFICIENT"


class HealingProposalStatus(str, Enum):
    PROPOSED = "PROPOSED"
    VALIDATING = "VALIDATING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class HealingAttemptType(str, Enum):
    """The semantic unit represented by a healing audit record."""

    CANDIDATE_EVALUATION = "CANDIDATE_EVALUATION"
    EXECUTION = "EXECUTION"
    MEASUREMENT_CONFIRMATION = "MEASUREMENT_CONFIRMATION"


class HealingAttemptStatus(str, Enum):
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    VERIFIED = "VERIFIED"


class LLMRuntimeMode(str, Enum):
    """Truthful source of an agent decision involving an LLM boundary."""

    REAL_LLM = "REAL_LLM"
    MOCK_LLM = "MOCK_LLM"
    DETERMINISTIC_FALLBACK = "DETERMINISTIC_FALLBACK"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    NOT_USED = "NOT_USED"


class LLMCallRecord(BaseModel):
    """Secret-free, durable evidence for one bounded model invocation."""

    call_id: str = Field(default_factory=lambda: new_id("LLM"))
    intent_id: str | None = None
    revision: int | None = None
    agent: str
    provider: str | None = None
    model: str | None = None
    runtime_mode: LLMRuntimeMode
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    status: str = "RUNNING"
    attempt_count: int = Field(default=0, ge=0)
    fallback_used: bool = False
    input_schema_version: str
    output_schema_version: str
    structured_output_valid: bool | None = None
    validator_result: str = "NOT_RUN"
    provider_request_id: str | None = None
    trace_id: str | None = None
    correlation_id: str = Field(default_factory=lambda: new_id("CORR"))
    prompt_hash: str
    input_refs: list[str] = Field(default_factory=list)
    structured_output: dict[str, Any] | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    error_code: str | None = None


class IntentObjective(BaseModel):
    objective_id: str = Field(default_factory=lambda: new_id("OBJ"))
    type: ObjectiveType
    operator: ComparisonOperator = ComparisonOperator.EQ
    value: Any = None
    unit: str | None = None
    origin: ValueOrigin = ValueOrigin.USER_EXPLICIT
    explicit: bool = True
    weight: float = Field(default=1.0, ge=0, le=1)
    source_text: str | None = None


class IntentConstraint(BaseModel):
    constraint_id: str = Field(default_factory=lambda: new_id("CON"))
    type: ConstraintType
    operator: ComparisonOperator = ComparisonOperator.EQ
    value: Any
    hard: bool = True
    origin: ValueOrigin = ValueOrigin.USER_EXPLICIT
    source_text: str | None = None


class IntentSpec(BaseModel):
    schema_version: str = "1.0"
    intent_id: str
    original_text: str
    normalized_text: str
    intent_type: str = "NETWORK_SERVICE"
    subject: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)
    destination: dict[str, Any] = Field(default_factory=dict)
    service: dict[str, Any] = Field(default_factory=dict)
    selector: dict[str, Any] = Field(default_factory=dict)
    objectives: list[IntentObjective] = Field(default_factory=list)
    constraints: list[IntentConstraint] = Field(default_factory=list)
    preferences: list[PreferenceRelation] = Field(default_factory=list)
    # A relative business preference is a scheduler input, never an implicit
    # request to alter DSCP, queues, P4 tables, or ONOS flows.
    execution_capability: str | None = None
    southbound_mutation_required: bool | None = None
    preference_validation_error: str | None = None
    priority: str = "medium"
    scope: str = "network"
    tenant_id: str = "default"
    validity: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0, le=1)
    assumptions: list[str] = Field(default_factory=list)
    clarification_required: bool = False
    clarification_questions: list[str] = Field(default_factory=list)
    clarification_details: list[dict[str, Any]] = Field(default_factory=list)
    explicit_fields: dict[str, Any] = Field(default_factory=dict)
    inferred_fields: dict[str, Any] = Field(default_factory=dict)
    unresolved_fields: list[str] = Field(default_factory=list)
    # True only when a traffic-scoped intent has both endpoints explicitly
    # resolved through the endpoint registry.  Keeping this fact in the IR
    # makes it auditable that no policy/capacity path was entered early.
    semantic_scope_complete: bool = False
    field_provenance: dict[str, str] = Field(default_factory=dict)
    service_resolution: dict[str, Any] = Field(default_factory=dict)
    endpoint_resolution: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    llm_status: str = "NOT_ATTEMPTED"
    llm_runtime_mode: LLMRuntimeMode = LLMRuntimeMode.NOT_USED
    llm_call_id: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_structured_output_valid: bool | None = None
    llm_validator_result: str = "NOT_RUN"
    fallback_used: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    lifecycle: IntentLifecycle = IntentLifecycle.RECEIVED
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def require_objective_or_clarification(self) -> "IntentSpec":
        if (
            not self.objectives
            and not self.preferences
            and not self.preference_validation_error
            and not self.clarification_required
        ):
            self.clarification_required = True
            if not self.clarification_questions:
                self.clarification_questions.append(
                    "Which measurable network outcome should this intent guarantee?"
                )
        return self

    @field_validator("preferences", mode="before")
    @classmethod
    def normalize_legacy_preferences(cls, value: Any) -> Any:
        """Read old audit rows without treating untyped dictionaries as authority."""
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("preferences must be a list")
        normalized = []
        for item in value:
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            if "higher_scope" in item or "lower_scope" in item:
                normalized.append(item)
                continue
            # The initial prototype wrote RELATIVE_PRIORITY dictionaries.
            # Convert those records losslessly on read so a later revision can
            # be managed by the same relation validator.
            if item.get("type") == "RELATIVE_PRIORITY":
                normalized.append({
                    "higher_scope": item.get("subject"),
                    "lower_scope": item.get("relative_to"),
                    "dimension": PreferenceDimension.SERVICE_PRIORITY.value,
                    "strength": PreferenceStrength.SOFT.value,
                    "origin": item.get("origin") or ValueOrigin.USER_EXPLICIT.value,
                    "source_text": item.get("source_text"),
                    "explicit": bool(item.get("explicit", True)),
                })
            else:
                normalized.append(item)
        return normalized

    def plannability_errors(self) -> list[str]:
        errors: list[str] = []
        # A relation-only request is semantic control-plane state.  It must be
        # conflict-checked and persisted, but it has no traffic selector and
        # must never be pushed to the data plane merely to become auditable.
        if self.preferences and not self.objectives:
            if self.clarification_required or self.unresolved_fields:
                errors.append("semantic clarification is still required")
            return errors
        source = self.source.get("selector_prefix") or self.source.get("ip") or self.selector.get("source_ip")
        destination = self.destination.get("selector_prefix") or self.destination.get("ip") or self.selector.get("destination_ip")
        if not source:
            errors.append("source is unresolved")
        if not destination:
            errors.append("destination is unresolved")
        # QoS admission is only meaningful for registry-resolved endpoints.
        # A selector proposed by an LLM is not enough: without a durable
        # endpoint identity it may accidentally collide with an unrelated
        # active binding and be classified as an update/reuse.
        qos_scope = any(
            item.type in {ObjectiveType.MIN_BANDWIDTH, ObjectiveType.MAX_BANDWIDTH}
            for item in self.objectives
        )
        endpoint_resolution = self.endpoint_resolution or {}
        if qos_scope:
            for field in ("source", "destination"):
                detail = endpoint_resolution.get(field) or {}
                if str(detail.get("status") or "").upper() != "RESOLVED" or not detail.get("endpoint_id"):
                    errors.append(f"{field} endpoint is not registry-resolved")
        protocol = str(self.selector.get("protocol") or "").upper()
        transport_required = self.intent_type == "QOS" or bool(
            self.service_resolution.get("matched_semantics")
        )
        if transport_required and protocol in {"", "ANY"}:
            errors.append("traffic protocol is unresolved")
        if protocol in {"TCP", "UDP"} and self.selector.get("destination_port") is None:
            errors.append("transport destination port is unresolved")
        if not self.objectives or any(item.value is None for item in self.objectives):
            errors.append("a measurable objective is required")
        if self.clarification_required or self.unresolved_fields:
            errors.append("semantic clarification is still required")
        if self.service_resolution.get("status") == "AMBIGUOUS":
            errors.append("service ambiguity is unresolved")
        if qos_scope:
            service_resolved = str(self.service_resolution.get("status") or "").upper() == "RESOLVED"
            explicit_transport = protocol in {"TCP", "UDP"} and self.selector.get("destination_port") is not None
            if not service_resolved and not explicit_transport:
                errors.append("service or explicit transport selector is unresolved")
        if any(item.value in (None, "", [], {}) for item in self.constraints):
            errors.append("one or more constraints are invalid")
        return list(dict.fromkeys(errors))

    def is_plannable(self) -> bool:
        return not self.plannability_errors()


class IntentUnderstandingResult(BaseModel):
    intent_spec: IntentSpec
    status: str
    confidence: float = Field(ge=0, le=1)
    explicit_fields: dict[str, Any] = Field(default_factory=dict)
    inferred_fields: dict[str, Any] = Field(default_factory=dict)
    unresolved_fields: list[str] = Field(default_factory=list)
    service_resolution: dict[str, Any] = Field(default_factory=dict)
    endpoint_resolution: dict[str, Any] = Field(default_factory=dict)
    clarification_questions: list[dict[str, Any]] = Field(default_factory=list)
    llm_status: str = "NOT_ATTEMPTED"
    fallback_used: bool = False
    llm_runtime_mode: LLMRuntimeMode = LLMRuntimeMode.NOT_USED
    llm_call_id: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_structured_output_valid: bool | None = None
    llm_validator_result: str = "NOT_RUN"
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class IntentRevision(BaseModel):
    revision_id: str = Field(default_factory=lambda: new_id("REV"))
    intent_id: str
    revision: int = Field(ge=1)
    previous_revision: int | None = None
    status: RevisionStatus = RevisionStatus.PENDING
    spec_snapshot: dict[str, Any]
    plan_id: str | None = None
    execution_id: str | None = None
    actor: str = "intent_api"
    reason: str = ""
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class IntentRuntimeState(BaseModel):
    intent_id: str
    lifecycle: IntentLifecycle = IntentLifecycle.RECEIVED
    revision: int = 1
    current_revision: int = 1
    pending_revision: int | None = None
    active_plan_id: str | None = None
    initial_plan_id: str | None = None
    last_healing_plan_id: str | None = None
    active_execution_id: str | None = None
    active_policy_ids: list[str] = Field(default_factory=list)
    current_path: list[str] = Field(default_factory=list)
    context_id: str | None = None
    last_assurance_id: str | None = None
    assurance_status: str = "UNKNOWN"
    last_assurance_at: datetime | None = None
    last_violation_at: datetime | None = None
    violation_count: int = 0
    consecutive_failures: int = 0
    consecutive_passes: int = 0
    healing_attempts: int = 0
    skipped_healing_actions: int = 0
    max_healing_attempts: int = 3
    measurement_confirmations: int = 0
    max_confirmation_samples: int = 3
    current_root_cause: str | None = None
    last_replan_reason: str | None = None
    observed_metrics: dict[str, Any] = Field(default_factory=dict)
    objective_results: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    degradation_since: datetime | None = None
    last_transition_at: datetime = Field(default_factory=utc_now)
    last_transition_reason: str = "intent received"
    last_error: str | None = None
    failure_reason: str | None = None
    failure_stage: str | None = None
    recoverable: bool | None = None
    grace_until: datetime | None = None
    cooldown_until: datetime | None = None
    llm_available: bool | None = None
    desired_policy: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class NetworkContext(BaseModel):
    context_id: str = Field(default_factory=lambda: new_id("CTX"))
    version: int = Field(default=1, ge=1)
    captured_at: datetime = Field(default_factory=utc_now)
    source_versions: dict[str, str] = Field(default_factory=dict)
    quality: EvidenceQuality = EvidenceQuality.MISSING
    evidence_quality: ContextEvidenceQuality = ContextEvidenceQuality.UNAVAILABLE
    source_states: dict[ContextSource, ContextSourceState] = Field(default_factory=dict)
    state_hash: str = ""
    material_state_hash: str = ""
    epoch: str | None = None
    decision_usable: bool = False
    unavailable_reasons: list[str] = Field(default_factory=list)
    topology: dict[str, Any] = Field(default_factory=dict)
    inventory: dict[str, Any] = Field(default_factory=dict)
    capacities: dict[str, Any] = Field(default_factory=dict)
    utilization: dict[str, Any] = Field(default_factory=dict)
    faults: list[dict[str, Any]] = Field(default_factory=list)
    policies: list[dict[str, Any]] = Field(default_factory=list)
    reservations: list[dict[str, Any]] = Field(default_factory=list)
    active_intents: list[dict[str, Any]] = Field(default_factory=list)
    healing_attempt_history: list[dict[str, Any]] = Field(default_factory=list)
    current_paths: dict[str, list[str]] = Field(default_factory=dict)
    candidate_paths: list[dict[str, Any]] = Field(default_factory=list)
    congestion: list[dict[str, Any]] = Field(default_factory=list)
    bottleneck_candidates: list[dict[str, Any]] = Field(default_factory=list)
    int_observations: dict[str, Any] = Field(default_factory=dict)
    tc_state: dict[str, Any] = Field(default_factory=dict)
    p4_state: dict[str, Any] = Field(default_factory=dict)
    queue_state: dict[str, Any] = Field(default_factory=dict)
    kpi: dict[str, Any] = Field(default_factory=dict)
    empirical_capabilities: list[dict[str, Any]] = Field(default_factory=list)
    freshness: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)


class RequiredAction(BaseModel):
    action_type: str
    target: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)


class CandidatePlan(BaseModel):
    candidate_id: str = Field(default_factory=lambda: new_id("CAND"))
    context_id: str
    path: list[str] = Field(default_factory=list)
    required_actions: list[RequiredAction] = Field(default_factory=list)
    expected_outcomes: dict[str, Any] = Field(default_factory=dict)
    score: float = 0
    preference_scopes: list[str] = Field(default_factory=list)
    feasible: bool = True
    violations: list[str] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
    risk_status: PlanningRiskStatus = PlanningRiskStatus.UNVERIFIED_CAPABILITY
    capability_evidence_ids: list[str] = Field(default_factory=list)
    deterministic_facts: list[str] = Field(default_factory=list)
    available_capacity_mbps: float | None = None
    empirical_capacity_mbps: float | None = None
    current_utilization_percent: float | None = None
    estimated_latency_ms: float | None = None
    reservations: list[dict[str, Any]] = Field(default_factory=list)
    affected_intent_ids: list[str] = Field(default_factory=list)
    expected_objective_results: list[dict[str, Any]] = Field(default_factory=list)
    risk: str = "medium"
    rejection_reason: str | None = None


class PlanDiff(BaseModel):
    """Auditable delta between the active plan and a healing generation."""

    parent_plan_id: str | None = None
    new_plan_id: str
    old_path: list[str] = Field(default_factory=list)
    new_path: list[str] = Field(default_factory=list)
    changed_edges: list[str] = Field(default_factory=list)
    shared_edges: list[str] = Field(default_factory=list)
    resources_added: list[dict[str, Any]] = Field(default_factory=list)
    resources_updated: list[dict[str, Any]] = Field(default_factory=list)
    resources_removed: list[dict[str, Any]] = Field(default_factory=list)
    added_flows: list[dict[str, Any]] = Field(default_factory=list)
    updated_flows: list[dict[str, Any]] = Field(default_factory=list)
    removed_flows: list[dict[str, Any]] = Field(default_factory=list)
    added_qos: list[dict[str, Any]] = Field(default_factory=list)
    updated_qos: list[dict[str, Any]] = Field(default_factory=list)
    removed_qos: list[dict[str, Any]] = Field(default_factory=list)
    path_changed: bool = False
    reservation_changed: bool = False
    selector_changed: bool = False
    action_changed: bool = False
    resource_binding_changed: bool = False
    expected_effect_changed: bool = False
    current_effect_signature: str | None = None
    proposed_effect_signature: str | None = None

    @staticmethod
    def effect_signature(effect: dict[str, Any]) -> str:
        """Stable hash of desired data-plane effect, deliberately excluding IDs."""

        canonical = json.dumps(
            effect, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalized_action(action: Any) -> dict[str, Any]:
        if isinstance(action, BaseModel):
            action = action.model_dump(mode="json")
        raw = dict(action or {})
        params = dict(raw.get("parameters") or raw.get("params") or {})
        for key in ("action_id", "candidate_id", "context_id", "evidence_refs", "captured_at"):
            raw.pop(key, None)
            params.pop(key, None)
        return {
            "type": raw.get("action_type") or raw.get("type"),
            "target": raw.get("target"),
            "parameters": params,
        }

    @classmethod
    def effect_payload(cls, plan: Any) -> dict[str, Any]:
        """Extract only desired data-plane semantics from an IntentPlanIR-like object."""

        if isinstance(plan, BaseModel):
            plan = plan.model_dump(mode="json")
        raw = dict(plan or {})
        selected_id = raw.get("selected_candidate_id")
        selected = next(
            (
                item for item in raw.get("candidates") or []
                if isinstance(item, dict) and item.get("candidate_id") == selected_id
            ),
            {},
        )
        actions = [
            cls._normalized_action(item)
            for item in [*(raw.get("required_actions") or []), *(selected.get("required_actions") or [])]
        ]
        actions.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
        outcomes = dict(raw.get("expected_outcomes") or {})
        relevant_outcomes = {
            key: value
            for key, value in outcomes.items()
            if any(token in str(key).lower() for token in (
                "selector", "action", "priority", "dscp", "qos", "rate", "ceil",
                "tc", "reservation", "binding",
            ))
        }
        return {
            "path": list(selected.get("path") or []),
            "actions": actions,
            "expected_data_plane_effect": relevant_outcomes,
        }

    @classmethod
    def compare(cls, current_plan: Any | None, proposed_plan: Any) -> "PlanDiff":
        """Compare desired effects; a fresh plan ID never constitutes a delta."""

        current = cls.effect_payload(current_plan)
        proposed = cls.effect_payload(proposed_plan)
        old_path = list(current.get("path") or [])
        new_path = list(proposed.get("path") or [])
        path_changed = old_path != new_path
        old_actions = current.get("actions") or []
        new_actions = proposed.get("actions") or []
        action_changed = old_actions != new_actions
        old_outcomes = current.get("expected_data_plane_effect") or {}
        new_outcomes = proposed.get("expected_data_plane_effect") or {}
        expected_effect_changed = old_outcomes != new_outcomes
        qos_tokens = ("QOS", "THROTTLE", "TC", "RESERV", "BIND")
        qos_changed = any(
            any(token in str(action.get("type") or "").upper() for token in qos_tokens)
            for action in [*old_actions, *new_actions]
        ) and action_changed
        old_edges = {"--".join(sorted((left, right))) for left, right in zip(old_path, old_path[1:])}
        new_edges = {"--".join(sorted((left, right))) for left, right in zip(new_path, new_path[1:])}
        raw = proposed_plan.model_dump(mode="json") if isinstance(proposed_plan, BaseModel) else dict(proposed_plan)
        return cls(
            parent_plan_id=raw.get("parent_plan_id"),
            new_plan_id=str(raw.get("plan_id") or ""),
            old_path=old_path,
            new_path=new_path,
            changed_edges=sorted(old_edges ^ new_edges),
            shared_edges=sorted(old_edges & new_edges),
            added_flows=[{"path": new_path}] if path_changed else [],
            removed_flows=[{"path": old_path}] if path_changed and old_path else [],
            added_qos=[{"actions": new_actions}] if qos_changed else [],
            path_changed=path_changed,
            reservation_changed=any("RESERV" in json.dumps(item).upper() for item in [old_actions, new_actions]) and action_changed,
            selector_changed=any("selector" in json.dumps(item, sort_keys=True).lower() for item in [old_actions, new_actions]) and action_changed,
            action_changed=action_changed,
            resource_binding_changed=any("binding" in json.dumps(item, sort_keys=True).lower() for item in [old_actions, new_actions]) and action_changed,
            expected_effect_changed=expected_effect_changed,
            current_effect_signature=cls.effect_signature(current),
            proposed_effect_signature=cls.effect_signature(proposed),
        )

    def is_noop(self) -> bool:
        """Return true when a proposed generation has no data-plane delta."""

        return (
            not self.path_changed
            and list(self.old_path) == list(self.new_path)
            and not self.changed_edges
            and not self.resources_added
            and not self.resources_updated
            and not self.resources_removed
            and not self.added_flows
            and not self.updated_flows
            and not self.removed_flows
            and not self.added_qos
            and not self.updated_qos
            and not self.removed_qos
            and not self.reservation_changed
            and not self.selector_changed
            and not self.action_changed
            and not self.resource_binding_changed
            and not self.expected_effect_changed
            and (
                not self.current_effect_signature
                or not self.proposed_effect_signature
                or self.current_effect_signature == self.proposed_effect_signature
            )
        )


class IntentPlanIR(BaseModel):
    plan_id: str = Field(default_factory=lambda: new_id("PLAN"))
    intent_id: str
    intent_revision: int
    context_id: str
    candidates: list[CandidatePlan] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    selected_reasoning: list[str] = Field(default_factory=list)
    rejected_reasoning: dict[str, list[str]] = Field(default_factory=dict)
    validation_status: PlanValidationStatus = PlanValidationStatus.INVALID
    validation_errors: list[str] = Field(default_factory=list)
    required_actions: list[RequiredAction] = Field(default_factory=list)
    expected_outcomes: dict[str, Any] = Field(default_factory=dict)
    rollback_strategy: list[RequiredAction] = Field(default_factory=list)
    llm_status: str = "NOT_ATTEMPTED"
    llm_runtime_mode: LLMRuntimeMode = LLMRuntimeMode.NOT_USED
    llm_call_id: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_structured_output_valid: bool | None = None
    llm_validator_result: str = "NOT_RUN"
    fallback_used: bool = False
    llm_selection: dict[str, Any] = Field(default_factory=dict)
    parent_plan_id: str | None = None
    legacy_plan_id: str | None = None
    replan_reason: str | None = None
    plan_diff: PlanDiff | None = None
    effect_signature: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class DeviceExecutionReceipt(BaseModel):
    device_id: str
    requested_flows: int = 0
    installed_flows: int = 0
    flow_ids: list[str] = Field(default_factory=list)
    readback_states: dict[str, str] = Field(default_factory=dict)
    verified: bool = False
    errors: list[str] = Field(default_factory=list)


class MigrationProof(BaseModel):
    proof_id: str = Field(default_factory=lambda: new_id("PROOF"))
    source: str
    expected_path: list[str] = Field(default_factory=list)
    observed_path: list[str] = Field(default_factory=list)
    passed: bool = False
    evidence_refs: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=utc_now)


class IntentExecutionResult(BaseModel):
    execution_id: str = Field(default_factory=lambda: new_id("EXEC"))
    intent_id: str
    plan_id: str
    context_id: str
    selected_candidate_id: str
    selected_path: list[str] = Field(default_factory=list)
    status: ExecutionStatus = ExecutionStatus.PREPARING
    phase_history: list[dict[str, Any]] = Field(default_factory=list)
    device_receipts: list[DeviceExecutionReceipt] = Field(default_factory=list)
    device_config_receipts: list[dict[str, Any]] = Field(default_factory=list)
    flows_installed: int = 0
    config_verified: bool = False
    rollback_refs: list[dict[str, str]] = Field(default_factory=list)
    previous_execution_id: str | None = None
    previous_cleanup: dict[str, Any] = Field(default_factory=dict)
    migration_proofs: list[MigrationProof] = Field(default_factory=list)
    migration_verified: bool = False
    old_path_retained_until_commit: bool = True
    commit_verified: bool = False
    precommit_verification: dict[str, Any] = Field(default_factory=dict)
    postcommit_verification: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class EvidenceItem(BaseModel):
    evidence_id: str = Field(default_factory=lambda: new_id("EVD"))
    intent_id: str
    objective_id: str | None = None
    source: str
    metric: str
    value: Any = None
    unit: str | None = None
    quality: EvidenceQuality = EvidenceQuality.MISSING
    sampled_at: datetime | None = None
    expires_at: datetime | None = None
    correlation: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class ObjectiveAssessment(BaseModel):
    objective_id: str
    objective_type: ObjectiveType
    status: AssessmentStatus
    expected: Any = None
    observed: Any = None
    unit: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str


class IntentAssuranceResult(BaseModel):
    assurance_id: str = Field(default_factory=lambda: new_id("ASR"))
    intent_id: str
    plan_id: str | None = None
    context_id: str | None = None
    execution_id: str | None = None
    lifecycle: IntentLifecycle
    evidence_quality: EvidenceQuality
    assessments: list[ObjectiveAssessment] = Field(default_factory=list)
    satisfied: bool | None = None
    summary: str
    failure_reason: str | None = None
    failure_stage: str | None = None
    recoverable: bool | None = None
    consecutive_failures: int = 0
    consecutive_passes: int = 0
    observed_at: datetime = Field(default_factory=utc_now)


class DiagnosisFact(BaseModel):
    fact_id: str = Field(default_factory=lambda: new_id("FACT"))
    source: str
    statement: str
    strength: str = "medium"
    evidence_ids: list[str] = Field(default_factory=list)


class RootCauseAnalysis(BaseModel):
    diagnosis_id: str = Field(default_factory=lambda: new_id("RCA"))
    intent_id: str
    assurance_id: str
    category: RootCauseCategory
    confidence: float = Field(default=0, ge=0, le=1)
    facts: list[DiagnosisFact] = Field(default_factory=list)
    rejected_causes: list[dict[str, Any]] = Field(default_factory=list)
    deterministic_candidates: list[dict[str, Any]] = Field(default_factory=list)
    llm_ranking: list[dict[str, Any]] = Field(default_factory=list)
    selected_cause: RootCauseCategory | None = None
    final_confidence: float | None = Field(default=None, ge=0, le=1)
    explanation: str
    scope: RootCauseScope = RootCauseScope.UNKNOWN
    bottleneck_link_id: str | None = None
    affected_link_ids: list[str] = Field(default_factory=list)
    candidate_paths_affected: list[str] = Field(default_factory=list)
    llm_status: str = "NOT_ATTEMPTED"
    llm_runtime_mode: LLMRuntimeMode = LLMRuntimeMode.NOT_USED
    llm_call_id: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_structured_output_valid: bool | None = None
    llm_validator_result: str = "NOT_RUN"
    fallback_used: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class HealingAction(BaseModel):
    action_id: str = Field(default_factory=lambda: new_id("HACT"))
    type: HealingActionType
    target: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str
    expected_effect: dict[str, Any] = Field(default_factory=dict)
    affected_intent_ids: list[str] = Field(default_factory=list)
    risk: str = "medium"
    confidence: float = Field(default=0, ge=0, le=1)


class HealingExpectedEffect(BaseModel):
    bottleneck_avoided: bool | None = None
    expected_available_mbps: float | None = None
    expected_gain_mbps: float | None = None
    affected_intents: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)


class HealingActionEvaluation(BaseModel):
    candidate_id: str
    action_id: str
    action: HealingActionType
    target: str | None = None
    applicability: HealingActionApplicability
    reason_code: HealingRejectionReason = HealingRejectionReason.NONE
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    expected_effect: HealingExpectedEffect = Field(default_factory=HealingExpectedEffect)
    affected_intents: list[str] = Field(default_factory=list)
    validator_result: str = "PASS"
    action_fingerprint: str = ""
    context_signature: str = ""


class AffectedIntentAnalysis(BaseModel):
    analysis_id: str = Field(default_factory=lambda: new_id("AIA"))
    intent_id: str
    candidate_id: str | None = None
    affected_intents: list[dict[str, Any]] = Field(default_factory=list)
    protected_intent_ids: list[str] = Field(default_factory=list)
    auto_degradable_intent_ids: list[str] = Field(default_factory=list)
    safe: bool = True
    requires_approval: bool = False
    reasons: list[str] = Field(default_factory=list)


class HealingProposal(BaseModel):
    healing_id: str = Field(default_factory=lambda: new_id("HEAL"))
    intent_id: str
    revision: int
    violation_id: str
    assurance_id: str
    root_cause_id: str
    source_context_id: str
    source_plan_id: str | None = None
    source_execution_id: str | None = None
    trigger_reason: str
    proposed_actions: list[HealingAction] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)
    requires_replan: bool = False
    requires_approval: bool = False
    status: HealingProposalStatus = HealingProposalStatus.PROPOSED
    created_at: datetime = Field(default_factory=utc_now)

    # Attempt/outcome fields deliberately coexist on the proposal so one row
    # remains the correlation root for a bounded healing transaction.
    attempt: int = 1
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    candidate_evaluations: list[HealingActionEvaluation] = Field(default_factory=list)
    selected_action: HealingAction | None = None
    selected_candidate_id: str | None = None
    before_assurance_id: str | None = None
    after_assurance_id: str | None = None
    plan_id: str | None = None
    execution_id: str | None = None
    execution_result: dict[str, Any] = Field(default_factory=dict)
    plan_diff: PlanDiff | None = None
    affected_intent_analysis: AffectedIntentAnalysis | None = None
    migration_proofs: list[MigrationProof] = Field(default_factory=list)
    tried_plan_ids: list[str] = Field(default_factory=list)
    outcome: str | None = None
    action_fingerprint: str | None = None
    context_signature: str | None = None
    skipped_no_effect: bool = False
    reason: str = ""
    updated_at: datetime = Field(default_factory=utc_now)


class HealingAttempt(HealingProposal):
    """Backward-compatible name for persisted healing proposal/attempt rows."""

    diagnosis_id: str | None = None
    attempt_type: HealingAttemptType = HealingAttemptType.CANDIDATE_EVALUATION
    attempt_status: HealingAttemptStatus | None = None
    data_plane_changed: bool = False
    measurement_id: str | None = None


class IntentConflict(BaseModel):
    conflict_id: str = Field(default_factory=lambda: new_id("CONFLICT"))
    intent_id: str
    conflicting_intent_id: str | None = None
    scope_relation: str = "DISJOINT"
    conflict_type: str
    severity: str = "medium"
    facts: list[str] = Field(default_factory=list)
    resolutions: list[dict[str, Any]] = Field(default_factory=list)
    selected_resolution: dict[str, Any] | None = None
    status: str = "OPEN"
    created_at: datetime = Field(default_factory=utc_now)


class AgentDecision(BaseModel):
    decision_id: str = Field(default_factory=lambda: new_id("DEC"))
    intent_id: str
    intent_revision: int | None = None
    agent_id: str
    decision_type: str
    correlation_id: str = Field(default_factory=lambda: new_id("CORR"))
    trace_id: str | None = None
    plan_id: str | None = None
    context_id: str | None = None
    input_refs: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    facts: list[str] = Field(default_factory=list)
    rationale_summary: str = ""
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    rejected_alternatives: list[dict[str, Any]] = Field(default_factory=list)
    llm_status: str = "NOT_USED"
    llm_runtime_mode: LLMRuntimeMode = LLMRuntimeMode.NOT_USED
    llm_call_id: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_structured_output_valid: bool | None = None
    llm_validator_result: str = "NOT_RUN"
    fallback_used: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class IntentTimelineEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("EVT"))
    intent_id: str
    intent_revision: int | None = None
    lifecycle: IntentLifecycle
    event_type: str
    actor: str
    reason: str
    correlation_id: str = Field(default_factory=lambda: new_id("CORR"))
    trace_id: str | None = None
    plan_id: str | None = None
    context_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

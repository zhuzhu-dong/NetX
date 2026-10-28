"""Non-empty OpenAPI contracts for core intent endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = "REQUEST_ERROR"
    message: str
    intent_id: str | None = None
    from_state: str | None = Field(default=None, alias="from")
    to: str | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


class IntentSpecEnvelope(BaseModel):
    intent_spec: dict[str, Any] | None
    current_revision: int | None = None
    pending_revision: int | None = None
    pending_intent_spec: dict[str, Any] | None = None


class RuntimeEnvelope(BaseModel):
    runtime: dict[str, Any]


class RevisionEnvelope(BaseModel):
    revision: dict[str, Any]


class PlanEnvelope(BaseModel):
    plan: dict[str, Any]


class VersionedItemsEnvelope(BaseModel):
    intent_id: str
    active: dict[str, Any] | None = None
    versions: list[dict[str, Any]] = Field(default_factory=list)
    initial: dict[str, Any] | None = None
    healing: dict[str, Any] | None = None
    current: dict[str, Any] | None = None


class CurrentHistoryEnvelope(BaseModel):
    intent_id: str
    current: dict[str, Any] | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)


class ItemsEnvelope(BaseModel):
    intent_id: str
    items: list[dict[str, Any]] = Field(default_factory=list)


class HealingEnvelope(ItemsEnvelope):
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)


class IntentDetailsEnvelope(BaseModel):
    """Single-read model for the Intent Detail view.

    Optional sections deliberately use empty objects/lists rather than 404 so
    an intent remains inspectable while an asynchronous workflow is incomplete.
    """

    intent_id: str
    record: dict[str, Any] = Field(default_factory=dict)
    spec: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    assurance: dict[str, Any] = Field(default_factory=dict)
    history: dict[str, Any] = Field(default_factory=dict)
    decisions: dict[str, Any] = Field(default_factory=dict)
    llm_calls: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    healing: dict[str, Any] = Field(default_factory=dict)
    pipeline: dict[str, Any] = Field(default_factory=dict)
    a2a_trace_ref: str
    capabilities: dict[str, Any] = Field(default_factory=dict)


class RevisionListEnvelope(ItemsEnvelope):
    current_revision: int
    pending_revision: int | None = None


class SubmitIntentResponse(BaseModel):
    session_id: str
    intent_id: str
    job_id: str
    job_status: str
    status: str
    created_at: str
    reused: bool = False
    reuse_result: str | None = None


class PreviewIntentResponse(BaseModel):
    intent_id: str
    status: str
    user_intent: str
    intent_spec: dict[str, Any]
    network_status: dict[str, Any]
    planning_status: str
    yang_policies: list[dict[str, Any]] = Field(default_factory=list)
    flow_table_plan: dict[str, Any] = Field(default_factory=dict)
    generated_at: str


class WithdrawalResponse(BaseModel):
    intent_id: str
    status: str
    runtime: dict[str, Any]


class CancellationResponse(BaseModel):
    intent_id: str
    status: str
    terminal: bool
    idempotent: bool = False
    cancellation_requested: bool = True
    job_id: str | None = None


ERROR_RESPONSES = {
    404: {"model": ErrorEnvelope, "description": "Intent not found"},
    409: {"model": ErrorEnvelope, "description": "State or idempotency conflict"},
    422: {"model": ErrorEnvelope, "description": "Invalid intent request"},
}

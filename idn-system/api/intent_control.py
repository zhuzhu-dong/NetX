"""Unified first-class Intent Control API."""

from __future__ import annotations

from typing import Any

from api.intent_api_models import (
    ERROR_RESPONSES,
    CurrentHistoryEnvelope,
    HealingEnvelope,
    IntentDetailsEnvelope,
    IntentSpecEnvelope,
    ItemsEnvelope,
    PlanEnvelope,
    RevisionEnvelope,
    RevisionListEnvelope,
    RuntimeEnvelope,
    VersionedItemsEnvelope,
)
from api.store import get_store
from fastapi import APIRouter, Header, HTTPException
from models.intent_domain import IntentRuntimeState
from persistence.intent_domain_store import get_intent_domain_store
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1/intents", tags=["intent-control"])


def _control_plane():
    # Keep optional P4Runtime/agent dependencies lazy so lightweight API tests
    # and read-only tooling can import the FastAPI app without the P4 SDK.
    from orchestrator.intent_control_plane import get_intent_control_plane

    return get_intent_control_plane()


def _pipeline_projection(
    *,
    lifecycle: Any,
    executions: list[dict[str, Any]],
    workflow_result: dict[str, Any],
) -> dict[str, Any]:
    """Project concrete workflow-stage evidence for an Intent detail response.

    An UNSATISFIABLE lifecycle with no execution record means that admission
    stopped before compilation.  A parsed policy candidate is not evidence of
    compiler, executor, or verifier activity.
    """
    lifecycle_value = str(getattr(lifecycle, "value", lifecycle) or "").upper()
    flow_plan = workflow_result.get("flow_table_plan")
    status_verification = workflow_result.get("status_verification_report")
    pre_execution_rejection = lifecycle_value == "UNSATISFIABLE" and not executions
    return {
        "conflict_check": (
            "REJECTED"
            if pre_execution_rejection
            else "COMPLETED"
            if workflow_result.get("policy_conflict_report")
            else "NOT_RUN"
        ),
        "policy_compiler": "NOT_RUN" if not flow_plan else "COMPLETED",
        "execution": "NOT_RUN" if not executions else str(executions[0].get("status") or "UNKNOWN"),
        "status_verifier": "NOT_RUN" if not status_verification else "COMPLETED",
        "southbound_write_operation_count": 0 if not executions else None,
    }


class IntentUpdateRequest(BaseModel):
    original_text: str | None = Field(default=None, min_length=1, max_length=2000)
    normalized_text: str | None = None
    priority: str | None = None
    scope: str | None = None
    tenant_id: str | None = None
    validity: dict[str, Any] | None = None
    objectives: list[dict[str, Any]] | None = None
    constraints: list[dict[str, Any]] | None = None
    preferences: list[dict[str, Any]] | None = None
    selector: dict[str, Any] | None = None
    source: dict[str, Any] | None = None
    destination: dict[str, Any] | None = None
    service: dict[str, Any] | None = None


class ReplanRequest(BaseModel):
    deploy: bool = False
    reason: str = "operator requested replanning"


class AssuranceBackfillRequest(BaseModel):
    """A reference to a pre-authorized local evidence directory, never JSON samples."""
    evidence_batch_id: str = Field(min_length=8, max_length=120)
    evidence_root: str = Field(min_length=1, max_length=240)


class ApplyRevisionRequest(BaseModel):
    deploy: bool = True


def _ensure(intent_id: str) -> None:
    if get_store().get(intent_id) is None:
        raise HTTPException(status_code=404, detail="intent not found")


@router.patch(
    "/{intent_id}", response_model=IntentSpecEnvelope, responses=ERROR_RESPONSES
)
async def update_intent(
    intent_id: str,
    request: IntentUpdateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    _ensure(intent_id)
    store = get_store()
    operation = f"PATCH:/api/v1/intents/{intent_id}"
    payload = request.model_dump(exclude_none=True, mode="json")
    replay = store.begin_idempotent(
        operation=operation, key=idempotency_key, payload=payload
    )
    if replay is not None:
        return replay
    try:
        spec = _control_plane().update_intent(intent_id, payload)
    except ValueError as exc:
        store.abort_idempotent(operation=operation, key=idempotency_key)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        store.abort_idempotent(operation=operation, key=idempotency_key)
        raise
    runtime = get_intent_domain_store().get_runtime(intent_id)
    response = {
        "intent_spec": spec.model_dump(mode="json"),
        "current_revision": runtime.current_revision if runtime else None,
        "pending_revision": runtime.pending_revision if runtime else spec.revision,
        "pending_intent_spec": spec.model_dump(mode="json"),
    }
    store.complete_idempotent(
        operation=operation,
        key=idempotency_key,
        response=response,
        resource_id=intent_id,
    )
    return response


@router.post(
    "/{intent_id}/withdraw", response_model=RuntimeEnvelope, responses=ERROR_RESPONSES
)
async def withdraw_intent(intent_id: str):
    _ensure(intent_id)
    runtime = await _control_plane().withdraw(intent_id)
    return {"runtime": runtime.model_dump(mode="json")}


@router.post(
    "/{intent_id}/revisions/apply",
    response_model=RevisionEnvelope,
    responses=ERROR_RESPONSES,
)
async def apply_intent_revision(
    intent_id: str, request: ApplyRevisionRequest | None = None
):
    _ensure(intent_id)
    request = request or ApplyRevisionRequest()
    revision = await _control_plane().apply_pending_revision(
        intent_id, deploy=request.deploy
    )
    return {"revision": revision.model_dump(mode="json")}


@router.get(
    "/{intent_id}/revisions",
    response_model=RevisionListEnvelope,
    responses=ERROR_RESPONSES,
)
async def get_intent_revisions(intent_id: str):
    _ensure(intent_id)
    domain = get_intent_domain_store()
    runtime = domain.get_runtime(intent_id) or IntentRuntimeState(intent_id=intent_id)
    return {
        "intent_id": intent_id,
        "current_revision": runtime.current_revision,
        "pending_revision": runtime.pending_revision,
        "items": domain.revisions(intent_id),
    }


@router.get(
    "/{intent_id}/details",
    response_model=IntentDetailsEnvelope,
    responses=ERROR_RESPONSES,
)
async def get_intent_details(intent_id: str):
    """Return all stable Intent Detail sections in one, side-effect-free read."""
    _ensure(intent_id)
    store = get_store()
    domain = get_intent_domain_store()
    record = dict(store.get(intent_id) or {})
    runtime_model = domain.get_runtime(intent_id) or IntentRuntimeState(intent_id=intent_id)
    spec_model = domain.get_spec(intent_id, runtime_model.current_revision)
    plans = domain.plans(intent_id)
    plans_by_id = {item.get("plan_id"): item for item in plans}
    executions = domain.executions(intent_id)
    assurance_items = domain.assurance(intent_id)
    history_items = domain.history(intent_id)
    decisions = domain.decisions(intent_id)
    llm_calls = domain.llm_calls(intent_id)
    evidence_items = domain.evidence(intent_id)
    healing_items = domain.healing(intent_id)
    workflow_result = record.get("result") or {}
    pipeline = _pipeline_projection(
        lifecycle=runtime_model.lifecycle,
        executions=executions,
        workflow_result=workflow_result,
    )
    key_states = {
        "VIOLATED", "ASSURANCE_FAILED", "DIAGNOSING", "REPLANNING",
        "REDEPLOYING", "VERIFYING", "SATISFIED", "UNSATISFIABLE",
        "EXECUTION_FAILED", "NEEDS_APPROVAL",
    }
    return {
        "intent_id": intent_id,
        "record": record,
        "spec": {
            "intent_spec": spec_model.model_dump(mode="json") if spec_model else record.get("intent_spec"),
            "current_revision": runtime_model.current_revision,
            "pending_revision": runtime_model.pending_revision,
        },
        "runtime": {"runtime": runtime_model.model_dump(mode="json")},
        "plan": {
            "intent_id": intent_id,
            "active": plans_by_id.get(runtime_model.active_plan_id),
            "versions": plans,
            "initial": plans_by_id.get(runtime_model.initial_plan_id),
            "healing": plans_by_id.get(runtime_model.last_healing_plan_id),
            "current": plans_by_id.get(runtime_model.active_plan_id),
        },
        "execution": {"intent_id": intent_id, "current": executions[0] if executions else None, "history": executions},
        "assurance": {"intent_id": intent_id, "current": assurance_items[0] if assurance_items else None, "history": assurance_items},
        "history": {"intent_id": intent_id, "items": history_items},
        "decisions": {"intent_id": intent_id, "items": decisions},
        "llm_calls": {"intent_id": intent_id, "items": llm_calls},
        "evidence": {"intent_id": intent_id, "items": evidence_items},
        "healing": {
            "intent_id": intent_id,
            "items": healing_items,
            "conflicts": domain.conflicts(intent_id),
            "timeline": [item for item in history_items if str(item.get("lifecycle")) in key_states],
        },
        "pipeline": pipeline,
        "a2a_trace_ref": f"/api/v1/intents/{intent_id}/a2a-trace",
        "capabilities": {
            "available_sections": [
                "record", "spec", "runtime", "plan", "execution", "assurance",
                "history", "decisions", "llm_calls", "evidence", "healing",
            ],
            "a2a_trace": True,
        },
    }


@router.get(
    "/{intent_id}/spec", response_model=IntentSpecEnvelope, responses=ERROR_RESPONSES
)
async def get_intent_spec(intent_id: str):
    _ensure(intent_id)
    domain = get_intent_domain_store()
    runtime = domain.get_runtime(intent_id) or IntentRuntimeState(intent_id=intent_id)
    spec = domain.get_spec(intent_id, runtime.current_revision)
    pending = (
        domain.get_spec(intent_id, runtime.pending_revision)
        if runtime.pending_revision is not None
        else None
    )
    return {
        "intent_spec": spec.model_dump(mode="json") if spec else None,
        "current_revision": runtime.current_revision,
        "pending_revision": runtime.pending_revision,
        "pending_intent_spec": pending.model_dump(mode="json") if pending else None,
    }


@router.get(
    "/{intent_id}/runtime", response_model=RuntimeEnvelope, responses=ERROR_RESPONSES
)
async def get_intent_runtime(intent_id: str):
    _ensure(intent_id)
    runtime = get_intent_domain_store().get_runtime(intent_id) or IntentRuntimeState(
        intent_id=intent_id
    )
    return {"runtime": runtime.model_dump(mode="json")}


@router.get(
    "/{intent_id}/plan",
    response_model=VersionedItemsEnvelope,
    responses=ERROR_RESPONSES,
)
async def get_intent_plan(intent_id: str):
    _ensure(intent_id)
    domain = get_intent_domain_store()
    plans = domain.plans(intent_id)
    runtime = domain.get_runtime(intent_id) or IntentRuntimeState(intent_id=intent_id)
    by_id = {item.get("plan_id"): item for item in plans}
    initial = by_id.get(runtime.initial_plan_id)
    current = by_id.get(runtime.active_plan_id)
    healing = by_id.get(runtime.last_healing_plan_id)
    return {
        "intent_id": intent_id,
        "active": current,
        "versions": plans,
        "initial": initial,
        "healing": healing,
        "current": current,
    }


@router.get(
    "/{intent_id}/assurance",
    response_model=CurrentHistoryEnvelope,
    responses=ERROR_RESPONSES,
)
async def get_intent_assurance(intent_id: str):
    _ensure(intent_id)
    items = get_intent_domain_store().assurance(intent_id)
    return {
        "intent_id": intent_id,
        "current": items[0] if items else None,
        "history": items,
    }


@router.get(
    "/{intent_id}/execution",
    response_model=CurrentHistoryEnvelope,
    responses=ERROR_RESPONSES,
)
async def get_intent_execution(intent_id: str):
    _ensure(intent_id)
    items = get_intent_domain_store().executions(intent_id)
    return {
        "intent_id": intent_id,
        "current": items[0] if items else None,
        "history": items,
    }


@router.get(
    "/{intent_id}/history", response_model=ItemsEnvelope, responses=ERROR_RESPONSES
)
async def get_intent_history(intent_id: str):
    _ensure(intent_id)
    return {
        "intent_id": intent_id,
        "items": get_intent_domain_store().history(intent_id),
    }


@router.get(
    "/{intent_id}/decisions", response_model=ItemsEnvelope, responses=ERROR_RESPONSES
)
async def get_intent_decisions(intent_id: str):
    _ensure(intent_id)
    return {
        "intent_id": intent_id,
        "items": get_intent_domain_store().decisions(intent_id),
    }


@router.get(
    "/{intent_id}/llm-calls", response_model=ItemsEnvelope, responses=ERROR_RESPONSES
)
async def get_intent_llm_calls(intent_id: str):
    _ensure(intent_id)
    return {
        "intent_id": intent_id,
        "items": get_intent_domain_store().llm_calls(intent_id),
    }


@router.get(
    "/{intent_id}/evidence", response_model=ItemsEnvelope, responses=ERROR_RESPONSES
)
async def get_intent_evidence(intent_id: str):
    _ensure(intent_id)
    return {
        "intent_id": intent_id,
        "items": get_intent_domain_store().evidence(intent_id),
    }


@router.get(
    "/{intent_id}/healing", response_model=HealingEnvelope, responses=ERROR_RESPONSES
)
async def get_intent_healing(intent_id: str):
    _ensure(intent_id)
    domain = get_intent_domain_store()
    history = domain.history(intent_id)
    key_states = {
        "VIOLATED",
        "ASSURANCE_FAILED",
        "DIAGNOSING",
        "REPLANNING",
        "REDEPLOYING",
        "VERIFYING",
        "SATISFIED",
        "UNSATISFIABLE",
        "EXECUTION_FAILED",
        "NEEDS_APPROVAL",
    }
    return {
        "intent_id": intent_id,
        "items": domain.healing(intent_id),
        "conflicts": domain.conflicts(intent_id),
        "timeline": [
            item for item in history if str(item.get("lifecycle")) in key_states
        ],
    }


@router.post(
    "/{intent_id}/reevaluate", response_model=RuntimeEnvelope, responses=ERROR_RESPONSES
)
async def reevaluate_intent(intent_id: str):
    _ensure(intent_id)
    try:
        runtime = await _control_plane().reevaluate(intent_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=409, detail="IntentSpec has not been created"
        ) from exc
    return {"runtime": runtime.model_dump(mode="json")}


@router.post(
    "/{intent_id}/assurance/reevaluate", response_model=RuntimeEnvelope, responses=ERROR_RESPONSES
)
async def formal_reevaluate_intent(intent_id: str, trace_id: str | None = Header(default=None, alias="X-Trace-Id")):
    """Consume registered evidence only; this route cannot run a validation probe."""
    _ensure(intent_id)
    try:
        runtime = await _control_plane().formal_reevaluate(intent_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"runtime": runtime.model_dump(mode="json"), "trace_id": trace_id}


@router.post("/{intent_id}/assurance/run-formal", responses=ERROR_RESPONSES)
async def run_formal_assurance(intent_id: str, actor: str | None = Header(default=None, alias="X-Admin-Actor")):
    """Run exactly one fixed five-sample batch; API-key middleware authenticates POST."""
    _ensure(intent_id)
    if not actor or len(actor.strip()) < 3:
        raise HTTPException(status_code=403, detail="ADMIN_ACTOR_REQUIRED")
    from api.service_validation import run_formal_assurance_batch
    try:
        result = await run_formal_assurance_batch(intent_id)
        runtime = await _control_plane().formal_reevaluate(intent_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run": result, "runtime": runtime.model_dump(mode="json")}


@router.post(
    "/{intent_id}/assurance/evidence/backfill", responses=ERROR_RESPONSES
)
async def import_assurance_backfill(
    intent_id: str,
    request: AssuranceBackfillRequest,
    actor: str | None = Header(default=None, alias="X-Admin-Actor"),
    trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
):
    """Administrator-only, path-restricted historical evidence registration.

    Global API-key middleware authenticates every POST.  This endpoint accepts
    neither lifecycle nor decision fields and does not accept uploaded bytes.
    """
    _ensure(intent_id)
    if not actor or len(actor.strip()) < 3:
        raise HTTPException(status_code=403, detail="ADMIN_ACTOR_REQUIRED")
    try:
        status, batch = _control_plane().formal_evidence.import_backfill(
            intent_id=intent_id,
            evidence_root=request.evidence_root,
            evidence_batch_id=request.evidence_batch_id,
            actor=actor.strip(),
            trace_id=trace_id or f"BACKFILL-{request.evidence_batch_id}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ingestion_status": status, "batch": batch.model_dump(mode="json")}


@router.post(
    "/{intent_id}/replan", response_model=PlanEnvelope, responses=ERROR_RESPONSES
)
async def replan_intent(intent_id: str, request: ReplanRequest | None = None):
    _ensure(intent_id)
    request = request or ReplanRequest()
    try:
        plan = await _control_plane().replan(
            intent_id, deploy=request.deploy, reason=request.reason
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=409, detail="IntentSpec has not been created"
        ) from exc
    return {"plan": plan.model_dump(mode="json")}

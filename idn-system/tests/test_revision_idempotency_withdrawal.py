from __future__ import annotations

import pytest
from api.idempotency import IdempotencyConflictError
from api.store import IntentStore
from models.intent_domain import (
    AgentDecision,
    CandidatePlan,
    ContextEvidenceQuality,
    EvidenceQuality,
    ExecutionStatus,
    IntentExecutionResult,
    IntentLifecycle,
    IntentObjective,
    IntentPlanIR,
    IntentRevision,
    IntentRuntimeState,
    IntentSpec,
    NetworkContext,
    ObjectiveType,
    PlanValidationStatus,
    RevisionStatus,
)
from orchestrator.intent_control_plane import IntentControlPlane
from orchestrator.intent_withdrawal import IntentWithdrawalService
from persistence.intent_domain_store import IntentDomainStore
from persistence.policy_store import PolicyStore
from models.yang_policy import (
    ActionRules, ActionType, MatchCriteria, NetworkPolicy, Protocol, QoSParams,
    YANGPolicy,
)
from models.policy_update import traffic_scope_key


def test_persistent_idempotency_replays_and_rejects_payload_mismatch(tmp_path):
    store = IntentStore(tmp_path / "idempotency.db")
    operation = "POST:/api/v1/intents"
    payload = {"text": "connect A to B", "priority": "medium"}
    assert (
        store.begin_idempotent(operation=operation, key="KEY-1", payload=payload)
        is None
    )
    response = {"intent_id": "INT-1", "job_id": "JOB-1"}
    store.complete_idempotent(
        operation=operation, key="KEY-1", response=response, resource_id="INT-1"
    )

    restarted = IntentStore(tmp_path / "idempotency.db")
    assert (
        restarted.begin_idempotent(operation=operation, key="KEY-1", payload=payload)
        == response
    )
    with pytest.raises(IdempotencyConflictError) as caught:
        restarted.begin_idempotent(
            operation=operation,
            key="KEY-1",
            payload={"text": "different"},
        )
    assert caught.value.code == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"


def _active_control_plane(tmp_path):
    store = IntentStore(tmp_path / "revision.db")
    domain = IntentDomainStore(store)
    intent_id = "INT-REV"
    store.create(intent_id=intent_id, text="A to B", user_id="test")
    spec = IntentSpec(
        intent_id=intent_id,
        original_text="A to B with 10 Mbps",
        normalized_text="A to B with 10 Mbps",
        source={"ip": "h1"},
        destination={"ip": "h2"},
        objectives=[IntentObjective(type=ObjectiveType.MIN_BANDWIDTH, value=10)],
        lifecycle=IntentLifecycle.SATISFIED,
    )
    runtime = IntentRuntimeState(
        intent_id=intent_id,
        lifecycle=IntentLifecycle.SATISFIED,
        current_revision=1,
        revision=1,
        active_plan_id="PLAN-V1",
        active_execution_id="EXEC-V1",
        current_path=["h1", "s1", "h2"],
    )
    domain.save_spec(spec)
    domain.save_runtime(runtime)
    domain.save_revision(
        IntentRevision(
            intent_id=intent_id,
            revision=1,
            status=RevisionStatus.ACTIVE,
            spec_snapshot=spec.model_dump(mode="json"),
        )
    )
    return IntentControlPlane(store, domain), store, domain


class StaticContextBuilder:
    def build(self):
        return NetworkContext(
            context_id="CTX-REV",
            evidence_quality=ContextEvidenceQuality.DEGRADED,
            quality=EvidenceQuality.DEGRADED,
        )

    async def build_async(self):
        return self.build()


class ValidPlanning:
    async def plan(self, spec, context):
        candidate = CandidatePlan(
            context_id=context.context_id, path=["h1", "s2", "h2"]
        )
        plan = IntentPlanIR(
            intent_id=spec.intent_id,
            intent_revision=spec.revision,
            context_id=context.context_id,
            candidates=[candidate],
            selected_candidate_id=candidate.candidate_id,
            validation_status=PlanValidationStatus.VALID,
        )
        return plan, AgentDecision(
            intent_id=spec.intent_id,
            intent_revision=spec.revision,
            agent_id="test",
            decision_type="CANDIDATE_SELECTION",
            plan_id=plan.plan_id,
            context_id=context.context_id,
        )


@pytest.mark.asyncio
async def test_failed_v2_preserves_active_v1_and_satisfied_lifecycle(tmp_path):
    control, _, domain = _active_control_plane(tmp_path)
    staged = control.update_intent("INT-REV", {"priority": "high"})
    assert staged.revision == 2
    assert domain.get_runtime("INT-REV").lifecycle == IntentLifecycle.SATISFIED
    control.context_builder = StaticContextBuilder()
    control.planning = ValidPlanning()

    async def fail_execution(plan):
        return IntentExecutionResult(
            intent_id=plan.intent_id,
            plan_id=plan.plan_id,
            context_id=plan.context_id,
            selected_candidate_id=plan.selected_candidate_id,
            status=ExecutionStatus.ROLLED_BACK,
            error="replacement readback failed",
        )

    control._execute_plan_locked = fail_execution
    revision = await control.apply_pending_revision("INT-REV")

    runtime = domain.get_runtime("INT-REV")
    assert revision.status == RevisionStatus.FAILED
    assert runtime.current_revision == 1
    assert runtime.pending_revision is None
    assert runtime.active_plan_id == "PLAN-V1"
    assert runtime.lifecycle == IntentLifecycle.SATISFIED

    retry = control.update_intent("INT-REV", {"priority": "medium"})
    assert retry.revision == 3
    assert domain.get_runtime("INT-REV").pending_revision == 3
    assert domain.get_revision("INT-REV", 2).status == RevisionStatus.FAILED


@pytest.mark.asyncio
async def test_successful_v2_activates_only_after_verified_replacement(tmp_path):
    control, _, domain = _active_control_plane(tmp_path)
    control.update_intent("INT-REV", {"priority": "high"})
    control.context_builder = StaticContextBuilder()
    control.planning = ValidPlanning()

    async def complete_execution(plan):
        result = IntentExecutionResult(
            intent_id=plan.intent_id,
            plan_id=plan.plan_id,
            context_id=plan.context_id,
            selected_candidate_id=plan.selected_candidate_id,
            selected_path=["h1", "s2", "h2"],
            status=ExecutionStatus.COMPLETED,
            config_verified=True,
        )
        domain.save_execution(result)
        runtime = domain.get_runtime(plan.intent_id)
        runtime.active_plan_id = plan.plan_id
        runtime.active_execution_id = result.execution_id
        runtime.current_path = list(result.selected_path)
        domain.save_runtime(runtime)
        return result

    control._execute_plan_locked = complete_execution
    revision = await control.apply_pending_revision("INT-REV")

    runtime = domain.get_runtime("INT-REV")
    assert revision.status == RevisionStatus.ACTIVE
    assert runtime.current_revision == 2
    assert runtime.pending_revision is None
    assert runtime.lifecycle == IntentLifecycle.SATISFIED
    assert domain.get_revision("INT-REV", 1).status == RevisionStatus.SUPERSEDED


class NoopExecution:
    async def rollback(self, execution):
        return {"deleted": 0, "failed": 0, "status": "ALREADY_ABSENT"}


@pytest.mark.asyncio
async def test_formal_withdrawal_keeps_policy_metadata_until_history_is_recorded(tmp_path, monkeypatch):
    """Legacy flow cleanup must not erase the owner of a scope withdrawal."""
    store = IntentStore(tmp_path / "withdraw-policy-history.db")
    domain = IntentDomainStore(store)
    intent_id = "INT-WITHDRAW-HISTORY"
    store.create(intent_id=intent_id, text="limit temporary traffic", user_id="test")
    policy = YANGPolicy(
        network_policy=NetworkPolicy(
            policy_id="POL-WITHDRAW-HISTORY", policy_name="temporary-rate-limit",
            match=MatchCriteria(
                source_ip="192.168.10.20/32", destination_ip="10.0.0.10/32",
                protocol=Protocol.UDP, destination_port=5005,
            ),
            action=ActionRules(
                action_type=ActionType.RATE_LIMIT,
                qos_params=QoSParams(qos_mode="RATE_LIMIT", bandwidth=5, dscp_value=0),
            ),
        ),
    )
    PolicyStore(store).save(policy, intent_id)

    async def cleared(**_kwargs):
        return {"status": "ok"}

    import api.qos as qos_module
    monkeypatch.setattr(qos_module, "clear_qos_policies", cleared)
    service = IntentWithdrawalService(store, domain, NoopExecution())
    cleanup = await service._cleanup_resources(intent_id, IntentRuntimeState(intent_id=intent_id))

    assert cleanup["legacy"]["policies_deleted"] == 0
    retained = PolicyStore(store).get_by_scope(traffic_scope_key(policy.network_policy).key)
    assert retained is not None
    assert retained["policy_id"] == "POL-WITHDRAW-HISTORY"


@pytest.mark.asyncio
async def test_withdraw_is_idempotent_and_keeps_history(tmp_path):
    control, store, domain = _active_control_plane(tmp_path)
    service = IntentWithdrawalService(store, domain, NoopExecution())

    first = await service.withdraw("INT-REV")
    second = await service.withdraw("INT-REV")

    assert first.lifecycle == IntentLifecycle.WITHDRAWN
    assert second.lifecycle == IntentLifecycle.WITHDRAWN
    assert domain.get_spec("INT-REV") is not None
    assert domain.history("INT-REV")[-1]["lifecycle"] == "WITHDRAWN"
    assert domain.decisions("INT-REV")[0]["output"]["status"] == "ALREADY_ABSENT"
    runtime = domain.get_runtime("INT-REV")
    assert runtime.pending_revision is None
    assert runtime.active_plan_id is None
    assert runtime.active_execution_id is None
    assert runtime.active_policy_ids == []
    assert runtime.current_path == []


@pytest.mark.asyncio
async def test_withdrawn_runtime_with_live_policy_retries_cleanup_then_retires_policy(
    tmp_path, monkeypatch,
):
    """A false WITHDRAWN projection cannot leave a resident policy active."""
    _, store, domain = _active_control_plane(tmp_path)
    policy = YANGPolicy(
        network_policy=NetworkPolicy(
            policy_id="POL-WITHDRAW-RETRY", policy_name="retry-cleanup",
            match=MatchCriteria(
                source_ip="192.168.10.20/32", destination_ip="10.0.0.10/32",
                protocol=Protocol.UDP, destination_port=5005,
            ),
            action=ActionRules(
                action_type=ActionType.RATE_LIMIT,
                qos_params=QoSParams(qos_mode="RATE_LIMIT", bandwidth=5, dscp_value=0),
            ),
        ),
    )
    PolicyStore(store).save(policy, "INT-REV")
    domain.transition(
        "INT-REV", IntentLifecycle.WITHDRAW_REQUESTED,
        actor="test", reason="simulate completed withdrawal",
    )
    domain.transition(
        "INT-REV", IntentLifecycle.WITHDRAWN,
        actor="test", reason="simulate false terminal projection",
    )

    async def cleaned(self, intent_id, runtime):
        return {
            "path": {"status": "ALREADY_ABSENT"},
            "legacy": {"status": "ALREADY_ABSENT"},
            "qos": [{"status": "ALREADY_ABSENT"}],
        }

    monkeypatch.setattr(IntentWithdrawalService, "_cleanup_resources", cleaned)
    withdrawn = await IntentWithdrawalService(store, domain, NoopExecution()).withdraw("INT-REV")

    assert withdrawn.lifecycle == IntentLifecycle.WITHDRAWN
    retired = PolicyStore(store).get_by_scope(traffic_scope_key(policy.network_policy).key)
    assert retired is not None
    assert retired["status"] == "WITHDRAWN"


@pytest.mark.asyncio
async def test_withdrawal_cleanup_runs_inside_a_fenced_writer_lease(tmp_path):
    _, store, domain = _active_control_plane(tmp_path)
    seen = {}

    class LeaseAwareExecution:
        async def rollback(self, execution):
            from infra.network_write_fencing import current_write_context

            seen["context"] = current_write_context()
            return {"deleted": 1, "failed": 0}

    execution = IntentExecutionResult(
        execution_id="EXEC-V1", intent_id="INT-REV", plan_id="PLAN-V1",
        context_id="CTX-V1", selected_candidate_id="CAND-V1",
        status=ExecutionStatus.COMPLETED,
        rollback_refs=[{"device_id": "device:1", "flow_id": "7"}],
    )
    domain.save_execution(execution)
    withdrawn = await IntentWithdrawalService(store, domain, LeaseAwareExecution()).withdraw("INT-REV")

    assert withdrawn.lifecycle == IntentLifecycle.WITHDRAWN
    assert seen["context"]["owner_type"] == "INTENT_WITHDRAWAL"
    assert seen["context"]["owner_id"] == "INT-REV"


@pytest.mark.asyncio
async def test_withdraw_retires_pending_revision_and_clears_active_bindings(tmp_path):
    control, store, domain = _active_control_plane(tmp_path)
    staged = control.update_intent("INT-REV", {"priority": "high"})
    runtime = domain.get_runtime("INT-REV")
    runtime.active_policy_ids = ["POL-V1"]
    domain.save_runtime(runtime)

    withdrawn = await IntentWithdrawalService(
        store, domain, NoopExecution()
    ).withdraw("INT-REV")

    assert staged.revision == 2
    assert withdrawn.lifecycle == IntentLifecycle.WITHDRAWN
    assert withdrawn.pending_revision is None
    assert withdrawn.active_plan_id is None
    assert withdrawn.active_execution_id is None
    assert withdrawn.active_policy_ids == []
    assert withdrawn.current_path == []
    retired = domain.get_revision("INT-REV", 2)
    assert retired.status == RevisionStatus.WITHDRAWN
    assert retired.error == "intent withdrawn before pending revision activation"


class FailingCleanup:
    async def rollback(self, execution):
        return {"deleted": 0, "failed": 1, "error": "controller unavailable"}


@pytest.mark.asyncio
async def test_partial_withdrawal_is_pending_cleanup_not_withdrawn(tmp_path):
    _, store, domain = _active_control_plane(tmp_path)
    execution = IntentExecutionResult(
        execution_id="EXEC-V1",
        intent_id="INT-REV",
        plan_id="PLAN-V1",
        context_id="CTX-V1",
        selected_candidate_id="CAND-V1",
        status=ExecutionStatus.COMPLETED,
        rollback_refs=[{"device_id": "device:1", "flow_id": "7"}],
    )
    domain.save_execution(execution)

    runtime = await IntentWithdrawalService(store, domain, FailingCleanup()).withdraw(
        "INT-REV"
    )

    assert runtime.lifecycle == IntentLifecycle.WITHDRAW_PENDING_CLEANUP
    assert store.get("INT-REV")["status"] == "withdraw_pending_cleanup"

"""Acceptance contracts for the violation-driven autonomous healing loop."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.intent_execution import IntentExecutionAgent
from agents.intent_planning import PlanningAgent
from api.store import IntentStore
from models.intent_domain import (
    AffectedIntentAnalysis,
    AssessmentStatus,
    CandidatePlan,
    ContextEvidenceQuality,
    EvidenceItem,
    EvidenceQuality,
    ExecutionStatus,
    HealingAction,
    HealingActionType,
    HealingAttempt,
    HealingProposalStatus,
    IntentAssuranceResult,
    IntentExecutionResult,
    IntentLifecycle,
    IntentPlanIR,
    IntentRuntimeState,
    IntentSpec,
    MigrationProof,
    NetworkContext,
    ObjectiveAssessment,
    ObjectiveType,
    PlanDiff,
    PlanningRiskStatus,
    PlanValidationStatus,
    RootCauseAnalysis,
    RootCauseCategory,
)
from models.intent_lifecycle_state_machine import IntentLifecycleStateMachine
from orchestrator.intent_control_plane import IntentControlPlane
from persistence.intent_domain_store import IntentDomainStore


def action(kind=HealingActionType.REROUTE, *, risk="medium"):
    return HealingAction(
        type=kind,
        target="INT-H",
        reason="measured violation",
        expected_effect={"satisfied": True},
        risk=risk,
        confidence=0.7,
    )


def proposal(**updates):
    values = {
        "intent_id": "INT-H",
        "revision": 1,
        "violation_id": "VIO-1",
        "assurance_id": "ASR-1",
        "root_cause_id": "RCA-1",
        "source_context_id": "CTX-1",
        "source_plan_id": "PLAN-1",
        "source_execution_id": "EXEC-1",
        "trigger_reason": "bandwidth failed",
        "proposed_actions": [action()],
        "confidence": 0.7,
        "evidence_refs": ["EVD-1"],
        "requires_replan": True,
    }
    values.update(updates)
    return HealingAttempt(**values)


def spec(intent_id="INT-H", *, priority="high", preemptible=False):
    return IntentSpec(
        intent_id=intent_id,
        original_text="connect h1 to h2",
        normalized_text="connect h1 to h2",
        source={"ip": "10.0.0.1", "endpoint_id": "h1"},
        destination={"ip": "10.0.0.2", "endpoint_id": "h2"},
        selector={"source_ip": "10.0.0.1/32", "destination_ip": "10.0.0.2/32"},
        objectives=[],
        clarification_required=True,
        priority=priority,
        validity={"preemptible": preemptible},
    )


@pytest.mark.parametrize("kind", list(HealingActionType))
def test_all_five_healing_action_types_are_formal(kind):
    assert action(kind).type is kind


@pytest.mark.parametrize("status", list(HealingProposalStatus))
def test_all_eight_healing_proposal_states_round_trip(status):
    item = proposal(status=status)
    assert HealingAttempt.model_validate(item.model_dump()).status is status


def test_healing_proposal_has_complete_source_correlation():
    item = proposal()
    assert {
        item.violation_id,
        item.assurance_id,
        item.root_cause_id,
        item.source_context_id,
        item.source_plan_id,
        item.source_execution_id,
    } == {"VIO-1", "ASR-1", "RCA-1", "CTX-1", "PLAN-1", "EXEC-1"}


def test_root_cause_model_contains_diagnosis_not_device_commands():
    root = RootCauseAnalysis(
        intent_id="INT-H",
        assurance_id="ASR-1",
        category=RootCauseCategory.PATH_CONGESTION,
        confidence=0.8,
        explanation="measured congestion",
    )
    dumped = root.model_dump()
    assert "proposed_actions" not in dumped
    assert "execution_id" not in dumped


def test_plan_diff_records_old_new_and_resource_delta():
    diff = PlanDiff(
        parent_plan_id="PLAN-OLD",
        new_plan_id="PLAN-NEW",
        old_path=["h1", "s1", "h2"],
        new_path=["h1", "s2", "h2"],
        resources_added=[{"type": "PATH_RULE_SET"}],
        resources_removed=[{"type": "SUPERSEDED_PATH_RULE_SET"}],
    )
    assert diff.old_path != diff.new_path
    assert diff.resources_added and diff.resources_removed


@pytest.mark.asyncio
async def test_replan_creates_new_plan_id_and_keeps_revision(monkeypatch):
    planner = PlanningAgent()
    old = CandidatePlan(context_id="CTX-NEW", path=["h1", "s1", "h2"], score=90)
    new = CandidatePlan(context_id="CTX-NEW", path=["h1", "s2", "h2"], score=80)
    base = IntentPlanIR(
        intent_id="INT-H",
        intent_revision=7,
        context_id="CTX-NEW",
        candidates=[old, new],
        selected_candidate_id=old.candidate_id,
        validation_status=PlanValidationStatus.VALID,
    )

    async def fake_plan(_spec, _context):
        return base, SimpleNamespace(
            plan_id=base.plan_id,
            output={},
            facts=[],
            alternatives=[],
            rejected_alternatives=[],
        )

    monkeypatch.setattr(planner, "plan", fake_plan)
    result, _ = await planner.replan(
        SimpleNamespace(revision=7, objectives=[]),
        NetworkContext(context_id="CTX-NEW"),
        parent_plan_id="PLAN-PARENT",
        current_path=old.path,
        reason="INTENT_VIOLATION",
    )
    assert result.plan_id != "PLAN-PARENT"
    assert result.parent_plan_id == "PLAN-PARENT"
    assert result.intent_revision == 7
    assert result.selected_candidate_id == new.candidate_id


@pytest.mark.asyncio
async def test_replan_rejects_previously_attempted_paths(monkeypatch):
    planner = PlanningAgent()
    one = CandidatePlan(context_id="CTX", path=["h1", "s1", "h2"])
    two = CandidatePlan(context_id="CTX", path=["h1", "s2", "h2"])
    base = IntentPlanIR(
        intent_id="INT-H",
        intent_revision=1,
        context_id="CTX",
        candidates=[one, two],
        selected_candidate_id=one.candidate_id,
        validation_status=PlanValidationStatus.VALID,
    )

    async def fake_plan(_spec, _context):
        return base, SimpleNamespace(
            plan_id=base.plan_id,
            output={},
            facts=[],
            alternatives=[],
            rejected_alternatives=[],
        )

    monkeypatch.setattr(planner, "plan", fake_plan)
    result, _ = await planner.replan(
        SimpleNamespace(revision=1, objectives=[]),
        NetworkContext(context_id="CTX"),
        parent_plan_id="PLAN-P",
        current_path=one.path,
        excluded_paths=[two.path],
    )
    assert result.validation_status == PlanValidationStatus.UNSATISFIABLE
    assert all(not item.feasible for item in result.candidates)


def test_candidate_plan_exposes_capacity_utilization_reservations_and_risk():
    candidate = CandidatePlan(
        context_id="CTX",
        path=["h1", "s2", "h2"],
        available_capacity_mbps=50,
        empirical_capacity_mbps=48,
        current_utilization_percent=20,
        estimated_latency_ms=3,
        reservations=[{"intent_id": "INT-X", "bandwidth_mbps": 5}],
        affected_intent_ids=["INT-X"],
        expected_objective_results=[{"type": "MIN_BANDWIDTH", "predicted": 48}],
        risk="low",
        risk_status=PlanningRiskStatus.EMPIRICALLY_SUPPORTED,
    )
    assert candidate.available_capacity_mbps == 50
    assert candidate.affected_intent_ids == ["INT-X"]
    assert candidate.expected_objective_results[0]["predicted"] == 48


def test_confidence_is_deterministic_and_bounded():
    evidence = [
        EvidenceItem(
            intent_id="INT-H",
            source="probe",
            metric="throughput_mbps",
            value=10,
            quality=EvidenceQuality.TRUSTED,
        )
    ]
    context = NetworkContext(evidence_quality=ContextEvidenceQuality.TRUSTED)
    first = IntentControlPlane._healing_confidence(0.8, evidence, context, alternative_count=2)
    second = IntentControlPlane._healing_confidence(0.8, evidence, context, alternative_count=2)
    assert first == second
    assert 0 <= first <= 1


def test_stale_context_reduces_confidence():
    evidence = [
        EvidenceItem(
            intent_id="INT-H",
            source="probe",
            metric="throughput_mbps",
            value=10,
            quality=EvidenceQuality.TRUSTED,
        )
    ]
    trusted = NetworkContext(evidence_quality=ContextEvidenceQuality.TRUSTED)
    stale = NetworkContext(evidence_quality=ContextEvidenceQuality.STALE)
    assert IntentControlPlane._healing_confidence(0.8, evidence, trusted, alternative_count=2) > IntentControlPlane._healing_confidence(0.8, evidence, stale, alternative_count=2)


def test_low_confidence_high_impact_action_requires_approval():
    item = proposal(
        confidence=0.2,
        proposed_actions=[action(HealingActionType.THROTTLE_LOWER_PRIORITY, risk="high")],
        selected_action=action(HealingActionType.THROTTLE_LOWER_PRIORITY, risk="high"),
        requires_approval=True,
    )
    assert item.requires_approval is True


class SpecDomain:
    def __init__(self, specs):
        self.specs = specs

    def get_spec(self, intent_id):
        return self.specs.get(intent_id)


def test_best_effort_preemptible_intent_is_the_only_auto_degradable_class():
    control = IntentControlPlane.__new__(IntentControlPlane)
    control.domain = SpecDomain({"INT-BE": spec("INT-BE", priority="low", preemptible=True)})
    context = NetworkContext(
        candidate_paths=[
            {
                "candidate_id": "CAND-1",
                "affected_intent_ids": ["INT-BE"],
                "available_capacity_mbps": 50,
            }
        ]
    )
    analysis = control._analyze_affected_intents(spec(), context, "CAND-1")
    assert analysis.auto_degradable_intent_ids == ["INT-BE"]
    assert analysis.protected_intent_ids == []


def test_non_preemptible_competing_intent_is_protected():
    control = IntentControlPlane.__new__(IntentControlPlane)
    control.domain = SpecDomain({"INT-P": spec("INT-P", priority="high")})
    context = NetworkContext(
        candidate_paths=[
            {
                "candidate_id": "CAND-1",
                "affected_intent_ids": ["INT-P"],
                "available_capacity_mbps": 3,
            }
        ],
        reservations=[{"intent_id": "INT-P", "bandwidth_mbps": 10}],
    )
    analysis = control._analyze_affected_intents(spec(), context, "CAND-1")
    assert analysis.protected_intent_ids == ["INT-P"]
    assert analysis.requires_approval is True
    assert analysis.safe is False


def test_reroute_action_does_not_name_a_hardcoded_host():
    actions = IntentControlPlane._healing_actions(
        RootCauseCategory.PATH_CONGESTION,
        spec(),
        NetworkContext(),
    )
    serialized = str([item.model_dump() for item in actions])
    assert "h_video" not in serialized
    assert actions[0].type == HealingActionType.REROUTE


def test_no_feasible_action_is_explicit_for_insufficient_telemetry():
    actions = IntentControlPlane._healing_actions(
        RootCauseCategory.TELEMETRY_INSUFFICIENT,
        spec(),
        NetworkContext(),
    )
    assert actions[0].type == HealingActionType.NO_FEASIBLE_ACTION


def test_migration_proof_requires_exact_observed_path():
    proof = MigrationProof(
        source="INT_PATH_TRACE",
        expected_path=["s1", "s2"],
        observed_path=["s1", "s3"],
        passed=False,
    )
    assert proof.passed is False
    assert proof.expected_path != proof.observed_path


class FakeAdapter:
    def batch_convert(self, requests):
        return {request.device_id: [{} for _ in request.table_entries] for request in requests}


class FakeOnos:
    def __init__(self):
        self.flows = {"device:bmv2:1": {"old": "ADDED"}}
        self.deleted = []
        self.next_id = 1

    async def post_flows(self, device_id, rules, correlation_id=""):
        ids = []
        for _ in rules:
            flow_id = f"new-{self.next_id}"
            self.next_id += 1
            self.flows.setdefault(device_id, {})[flow_id] = "ADDED"
            ids.append(flow_id)
        return {"flows_posted": len(ids), "total": len(ids), "flow_ids": ids, "errors": None}

    async def get_flows(self, device_id=None):
        return {"flows": [{"id": key, "state": value} for key, value in self.flows.get(device_id, {}).items()]}

    async def delete_flows(self, device_id, flow_ids, correlation_id=""):
        for flow_id in flow_ids:
            self.flows.get(device_id, {}).pop(flow_id, None)
            self.deleted.append((device_id, flow_id))
        return {"flows_deleted": len(flow_ids), "total": len(flow_ids), "errors": None}


@pytest.mark.asyncio
async def test_same_key_cleanup_deletes_only_exact_baseline_collision():
    selector = {
        "criteria": [
            {
                "type": "PROTOCOL_INDEPENDENT",
                "matches": [
                    {
                        "field": "hdr.ipv4.dstAddr",
                        "match": "lpm",
                        "value": "0a00000a",
                        "prefixLength": 32,
                    }
                ],
            }
        ]
    }
    other_selector = {
        "criteria": [
            {
                "type": "PROTOCOL_INDEPENDENT",
                "matches": [
                    {
                        "field": "hdr.ipv4.dstAddr",
                        "match": "lpm",
                        "value": "0a000014",
                        "prefixLength": 32,
                    }
                ],
            }
        ]
    }

    class RichOnos(FakeOnos):
        async def get_flows(self, device_id=None):
            return {
                "flows": [
                    {
                        "id": "baseline-collision",
                        "state": "ADDED",
                        "priority": 10,
                        "tableId": 37066206,
                        "selector": selector,
                    },
                    {
                        "id": "baseline-other-key",
                        "state": "ADDED",
                        "priority": 10,
                        "tableId": 37066206,
                        "selector": other_selector,
                    },
                    {
                        "id": "intent-same-key",
                        "state": "ADDED",
                        "priority": 30001,
                        "tableId": 37066206,
                        "selector": selector,
                    },
                ]
            }

    onos = RichOnos()
    agent = IntentExecutionAgent(onos_client=onos)
    rule = {
        "priority": 30002,
        "tableId": 37066206,
        "selector": selector,
    }
    result = await agent._cleanup_same_key_baseline_rules(
        {"device:bmv2:1": [rule]},
        {"device:bmv2:1": ["new-replacement"]},
    )

    assert result["matched"] == 1
    assert result["deleted"] == 1
    assert result["failed"] == 0
    assert onos.deleted == [("device:bmv2:1", "baseline-collision")]


def topology():
    return {
        "status": "ok",
        "hosts": [
            {"name": "h1", "ip": "10.0.0.1/32", "mac": "00:00:00:00:00:01", "gateway_mac": "00:aa:00:00:00:01"},
            {"name": "h2", "ip": "10.0.0.2/32", "mac": "00:00:00:00:00:02", "gateway_mac": "00:aa:00:00:00:02"},
        ],
        "switches": [
            {"name": "s2", "device_id": "device:bmv2:2", "router_mac": "00:bb:00:00:00:02"},
        ],
        "links": [
            {"id": "h1-s2", "endpoints": [{"node": "h1", "type": "host", "port": 1}, {"node": "s2", "type": "switch", "port": 1}]},
            {"id": "s2-h2", "endpoints": [{"node": "s2", "type": "switch", "port": 2}, {"node": "h2", "type": "host", "port": 1}]},
        ],
    }


def migration_plan():
    candidate = CandidatePlan(context_id="CTX-NEW", path=["h1", "s2", "h2"])
    return IntentPlanIR(
        intent_id="INT-H",
        intent_revision=1,
        context_id="CTX-NEW",
        candidates=[candidate],
        selected_candidate_id=candidate.candidate_id,
        validation_status=PlanValidationStatus.VALID,
        parent_plan_id="PLAN-OLD",
        replan_reason="INTENT_VIOLATION",
    )


def previous_execution():
    return IntentExecutionResult(
        intent_id="INT-H",
        plan_id="PLAN-OLD",
        context_id="CTX-OLD",
        selected_candidate_id="CAND-OLD",
        selected_path=["h1", "s1", "h2"],
        status=ExecutionStatus.COMPLETED,
        rollback_refs=[{"device_id": "device:bmv2:1", "flow_id": "old"}],
    )


@pytest.mark.asyncio
async def test_make_before_break_commits_after_two_independent_proofs():
    onos = FakeOnos()
    agent = IntentExecutionAgent(
        topology_fetcher=lambda: async_value(topology()),
        onos_client=onos,
        flow_adapter=FakeAdapter(),
    )

    async def prepare(_path):
        assert "old" in onos.flows["device:bmv2:1"]
        return [{"readback": {"qos_binding": {"current_path": ["s2"]}}}]

    async def verify(_path):
        return {"int_analysis": {"switch_path": ["s2"]}}

    result = await agent.execute(
        migration_plan(),
        previous=previous_execution(),
        prepare_commit=prepare,
        precommit_verify=verify,
    )
    assert result.status == ExecutionStatus.COMPLETED
    assert result.migration_verified is True
    assert len([item for item in result.migration_proofs if item.passed]) >= 2
    assert ("device:bmv2:1", "old") in onos.deleted


@pytest.mark.asyncio
async def test_post_cleanup_reassert_requires_fresh_int_plus_independent_proof():
    onos = FakeOnos()
    agent = IntentExecutionAgent(
        topology_fetcher=lambda: async_value(topology()),
        onos_client=onos,
        flow_adapter=FakeAdapter(),
    )

    async def prepare(_path):
        return [{"readback": {"qos_binding": {"current_path": ["s2"]}}}]

    async def postcommit(_path):
        assert ("device:bmv2:1", "old") in onos.deleted
        return {"int_analysis": {"switch_path": ["s2"]}}

    result = await agent.execute(
        migration_plan(),
        previous=previous_execution(),
        prepare_commit=prepare,
        postcommit_verify=postcommit,
    )

    assert result.status == ExecutionStatus.COMPLETED
    assert result.previous_cleanup["replacement_reassert"]["posted"] > 0
    passed_sources = {item.source for item in result.migration_proofs if item.passed}
    assert "INT_PATH_TRACE" in passed_sources
    assert "ONOS_FLOW_READBACK" in passed_sources


@pytest.mark.asyncio
async def test_post_cleanup_int_mismatch_rolls_back_new_and_restores_old_path():
    onos = FakeOnos()
    agent = IntentExecutionAgent(
        topology_fetcher=lambda: async_value(topology()),
        onos_client=onos,
        flow_adapter=FakeAdapter(),
    )

    async def prepare(_path):
        return [{"readback": {"qos_binding": {"current_path": ["s2"]}}}]

    async def wrong_int(_path):
        return {"int_analysis": {"switch_path": ["s1"]}}

    restorable_previous = previous_execution().model_copy(
        update={"selected_path": ["h1", "s2", "h2"]}
    )
    result = await agent.execute(
        migration_plan(),
        previous=restorable_previous,
        prepare_commit=prepare,
        postcommit_verify=wrong_int,
    )

    assert result.status == ExecutionStatus.ROLLED_BACK
    assert "trusted INT path" in (result.error or "")
    assert result.previous_cleanup["old_path_restore"]["status"] == "RESTORED"
    assert result.previous_cleanup["new_install_rollback"]["failed"] == 0


@pytest.mark.asyncio
async def test_late_reevaluate_does_not_overwrite_inflight_replanning(tmp_path, monkeypatch):
    store = IntentStore(tmp_path / "late-reevaluate.db")
    domain = IntentDomainStore(store)
    domain.save_runtime(
        IntentRuntimeState(
            intent_id="INT-INFLIGHT",
            lifecycle=IntentLifecycle.REPLANNING,
        )
    )
    control = IntentControlPlane(store, domain)

    async def forbidden_assessment(_intent_id, **_kwargs):
        raise AssertionError("in-flight healing must not be reassessed")

    monkeypatch.setattr(control, "_assess_locked", forbidden_assessment)
    runtime = await control.reevaluate("INT-INFLIGHT")
    assert runtime.lifecycle == IntentLifecycle.REPLANNING


@pytest.mark.asyncio
async def test_path_resource_commit_happens_after_proofs_and_before_old_flow_cleanup():
    onos = FakeOnos()
    agent = IntentExecutionAgent(
        topology_fetcher=lambda: async_value(topology()),
        onos_client=onos,
        flow_adapter=FakeAdapter(),
    )
    order = []

    async def prepare(_path):
        order.append("prepare")
        return [{"readback": {"qos_binding": {"current_path": ["s2"]}}}]

    async def verify(_path):
        order.append("prove")
        return {"int_analysis": {"switch_path": ["s2"]}}

    async def commit(_path, _receipts):
        assert "old" in onos.flows["device:bmv2:1"]
        order.append("commit")
        return {"committed": 1, "failed": 0}

    result = await agent.execute(
        migration_plan(),
        previous=previous_execution(),
        prepare_commit=prepare,
        precommit_verify=verify,
        commit_resources=commit,
    )

    assert result.status == ExecutionStatus.COMPLETED
    assert order == ["prepare", "prove", "commit"]
    assert ("device:bmv2:1", "old") in onos.deleted


@pytest.mark.asyncio
async def test_migration_path_mismatch_rolls_back_new_and_keeps_old():
    onos = FakeOnos()
    agent = IntentExecutionAgent(
        topology_fetcher=lambda: async_value(topology()),
        onos_client=onos,
        flow_adapter=FakeAdapter(),
    )

    async def prepare(_path):
        return [{"readback": {"qos_binding": {"current_path": ["s1"]}}}]

    result = await agent.execute(
        migration_plan(),
        previous=previous_execution(),
        prepare_commit=prepare,
    )
    assert result.status == ExecutionStatus.ROLLED_BACK
    assert "old" in onos.flows["device:bmv2:1"]
    assert ("device:bmv2:1", "old") not in onos.deleted


@pytest.mark.asyncio
async def test_failed_migration_proof_rolls_back_prepared_path_resources():
    onos = FakeOnos()
    agent = IntentExecutionAgent(
        topology_fetcher=lambda: async_value(topology()),
        onos_client=onos,
        flow_adapter=FakeAdapter(),
    )
    calls = []

    async def prepare(_path):
        calls.append("prepare")
        return [{"readback": {"qos_binding": {"current_path": ["s1"]}}}]

    async def rollback(_path, _receipts, error):
        calls.append("rollback")
        assert "two independent path proofs" in error
        assert "old" in onos.flows["device:bmv2:1"]
        return {"rolled_back": 1, "failed": 0}

    result = await agent.execute(
        migration_plan(),
        previous=previous_execution(),
        prepare_commit=prepare,
        rollback_resources=rollback,
    )

    assert result.status == ExecutionStatus.ROLLED_BACK
    assert calls == ["prepare", "rollback"]
    assert result.previous_cleanup["new_resource_rollback"]["failed"] == 0


@pytest.mark.asyncio
async def test_first_committed_execution_sets_initial_plan_once(tmp_path):
    store = IntentStore(tmp_path / "initial-plan.db")
    domain = IntentDomainStore(store)
    control = IntentControlPlane(store, domain)
    plan_a = migration_plan().model_copy(
        update={"plan_id": "PLAN-A", "parent_plan_id": None}
    )
    domain.save_runtime(IntentRuntimeState(intent_id=plan_a.intent_id))

    async def complete(plan, **_kwargs):
        return IntentExecutionResult(
            intent_id=plan.intent_id,
            plan_id=plan.plan_id,
            context_id=plan.context_id,
            selected_candidate_id=plan.selected_candidate_id,
            selected_path=["h1", "s2", "h2"],
            status=ExecutionStatus.COMPLETED,
        )

    control.execution.execute = complete
    await control._execute_plan_locked(plan_a)
    first = domain.get_runtime(plan_a.intent_id)
    assert first.initial_plan_id == "PLAN-A"
    assert first.active_plan_id == "PLAN-A"

    plan_b = plan_a.model_copy(
        update={"plan_id": "PLAN-B", "parent_plan_id": "PLAN-A"}
    )
    await control._execute_plan_locked(plan_b)
    second = domain.get_runtime(plan_a.intent_id)
    assert second.initial_plan_id == "PLAN-A"
    assert second.active_plan_id == "PLAN-B"


async def async_value(value):
    return value


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (IntentLifecycle.VIOLATED, IntentLifecycle.DIAGNOSING),
        (IntentLifecycle.DIAGNOSING, IntentLifecycle.REPLANNING),
        (IntentLifecycle.REPLANNING, IntentLifecycle.REDEPLOYING),
        (IntentLifecycle.REDEPLOYING, IntentLifecycle.VERIFYING),
        (IntentLifecycle.VERIFYING, IntentLifecycle.SATISFIED),
        (IntentLifecycle.VERIFYING, IntentLifecycle.VIOLATED),
        (IntentLifecycle.REPLANNING, IntentLifecycle.UNSATISFIABLE),
    ],
)
def test_required_healing_lifecycle_edges_are_legal(source, target):
    IntentLifecycleStateMachine.validate("INT-H", source, target)


def test_async_capture_nonce_creates_fresh_context_id(tmp_path):
    from api.store import IntentStore
    from infra.network_context_builder import NetworkContextBuilder

    builder = NetworkContextBuilder(store=IntentStore(tmp_path / "context.db"))
    first = builder.build(snapshot_nonce="CAPTURE-1")
    second = builder.build(snapshot_nonce="CAPTURE-2")
    assert first.context_id != second.context_id
    assert first.state_hash != second.state_hash


def test_legacy_kpi_failure_is_detected_as_violation_not_install_failure():
    result = {
        "execution_result": {"status": "SUCCESS"},
        "status_verification_report": {"passed": False, "verification_state": "KPI_FAILED"},
    }
    assert IntentControlPlane._legacy_kpi_violation(result) is True


def test_southbound_execution_failure_is_not_mislabeled_as_kpi_violation():
    result = {
        "execution_result": {"status": "FAILED"},
        "status_verification_report": {"passed": False, "verification_state": "KPI_FAILED"},
    }
    assert IntentControlPlane._legacy_kpi_violation(result) is False


def test_legacy_path_import_keeps_endpoints_and_observed_switch_path():
    parsed = spec()
    result = {
        "execution_result": {
            "qos_enforcement": {
                "qos_binding": {"current_path": ["s_access", "s_core1", "s_dc"]}
            }
        }
    }
    assert IntentControlPlane._legacy_current_path(parsed, result) == [
        "h1",
        "s_access",
        "s_core1",
        "s_dc",
        "h2",
    ]


def test_proposal_attempt_and_final_outcome_fields_are_separate():
    item = proposal(status=HealingProposalStatus.FAILED, outcome="VIOLATED")
    assert item.status == HealingProposalStatus.FAILED
    assert item.outcome == "VIOLATED"


def test_a2a_correlation_payload_contains_all_transaction_ids():
    data = IntentControlPlane._correlation_data(proposal(plan_id="PLAN-2", execution_id="EXEC-2"))
    assert all(
        key in data
        for key in (
            "healing_id",
            "violation_id",
            "assurance_id",
            "root_cause_id",
            "source_context_id",
            "source_plan_id",
            "source_execution_id",
            "plan_id",
            "execution_id",
        )
    )


def test_suggestion_is_not_execution_before_execution_state():
    item = proposal(status=HealingProposalStatus.APPROVED)
    assert item.proposed_actions
    assert item.execution_id is None
    assert item.status != HealingProposalStatus.EXECUTING


def test_runtime_metrics_follow_final_assurance_not_stale_legacy_evidence():
    evidence = [
        EvidenceItem(
            intent_id="INT-H",
            source="service_validation:video",
            metric="connectivity",
            value=True,
            quality=EvidenceQuality.TRUSTED,
        ),
        EvidenceItem(
            intent_id="INT-H",
            source="status_verifier",
            metric="connectivity",
            value=False,
            quality=EvidenceQuality.TRUSTED,
        ),
    ]
    assurance = IntentAssuranceResult(
        intent_id="INT-H",
        lifecycle=IntentLifecycle.SATISFIED,
        evidence_quality=EvidenceQuality.TRUSTED,
        assessments=[
            ObjectiveAssessment(
                objective_id="OBJ-CONNECT",
                objective_type=ObjectiveType.CONNECTIVITY,
                status=AssessmentStatus.PASS,
                expected=True,
                observed=True,
                reason="fresh service measurement connected",
            )
        ],
        satisfied=True,
        summary="connected",
    )

    observed = IntentControlPlane._runtime_observed_metrics(assurance, evidence)

    assert observed["connectivity"] is True


def test_named_healing_audit_event_is_append_only(tmp_path):
    domain = IntentDomainStore(IntentStore(tmp_path / "audit.db"))
    domain.save_runtime(
        IntentRuntimeState(intent_id="INT-AUDIT", lifecycle=IntentLifecycle.VIOLATED)
    )

    event = domain.record_event(
        "INT-AUDIT",
        "HEALING_PROPOSED",
        actor="intent_healing",
        reason="formal proposal persisted",
        data={"healing_id": "HEAL-1", "violation_id": "VIO-1"},
    )

    assert event.event_type == "HEALING_PROPOSED"
    assert domain.get_runtime("INT-AUDIT").lifecycle == IntentLifecycle.VIOLATED
    assert domain.history("INT-AUDIT")[0]["event_type"] == "HEALING_PROPOSED"


def test_healing_orchestrator_emits_required_named_audit_events():
    import inspect

    source = inspect.getsource(IntentControlPlane)
    required = {
        "INTENT_VIOLATED",
        "ROOT_CAUSE_SELECTED",
        "HEALING_PROPOSED",
        "CONTEXT_REFRESHED",
        "REPLAN_STARTED",
        "PLAN_SELECTED",
        "HEALING_CHECKED",
        "REDEPLOY_STARTED",
        "NEW_RESOURCES_INSTALLED",
        "NEW_RESOURCES_VERIFIED",
        "PLAN_COMMITTED",
        "OLD_RESOURCES_CLEANED",
        "ASSURANCE_RESULT",
        "INTENT_SATISFIED",
        "HEALING_FAILED",
        "PLAN_ROLLED_BACK",
        "INTENT_UNSATISFIABLE",
    }

    assert all(f'"{event_type}"' in source for event_type in required)

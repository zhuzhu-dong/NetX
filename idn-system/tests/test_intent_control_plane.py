"""Acceptance contracts for the first-class Intent control plane."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from agents.intent_assurance import EvidenceAggregator, IntentAssuranceAgent
from agents.intent_execution import IntentExecutionAgent
from agents.intent_planning import PlanValidator
from agents.intent_understanding import IntentUnderstandingAgent
from api.store import IntentStore
from models.intent_domain import (
    AssessmentStatus,
    CandidatePlan,
    ComparisonOperator,
    ConstraintType,
    EvidenceItem,
    EvidenceQuality,
    ExecutionStatus,
    IntentExecutionResult,
    IntentLifecycle,
    IntentObjective,
    IntentPlanIR,
    IntentRuntimeState,
    IntentSpec,
    NetworkContext,
    ObjectiveType,
    PlanValidationStatus,
    ValueOrigin,
)
from models.intent_plan import IntentPlan, SubIntent
from orchestrator.intent_control_plane import IntentControlPlane
from persistence.intent_domain_store import IntentDomainStore


class StaticAggregator(EvidenceAggregator):
    def __init__(self, items):
        self.items = items

    def collect(self, spec):
        return self.items


class FakeFlowAdapter:
    def batch_convert(self, requests):
        return {
            request.device_id: [{} for _ in request.table_entries]
            for request in requests
        }


class FakeOnosClient:
    def __init__(self):
        self.flows = {}
        self.deleted = []
        self.next_id = 1

    async def post_flows(self, device_id, rules, correlation_id=""):
        ids = []
        for _rule in rules:
            flow_id = str(self.next_id)
            self.next_id += 1
            self.flows.setdefault(device_id, {})[flow_id] = "ADDED"
            ids.append(flow_id)
        return {
            "flows_posted": len(ids),
            "total": len(rules),
            "flow_ids": ids,
            "errors": None,
        }

    async def get_flows(self, device_id=None):
        return {
            "flows": [
                {"id": flow_id, "state": state}
                for flow_id, state in self.flows.get(device_id, {}).items()
            ]
        }

    async def delete_flows(self, device_id, flow_ids, correlation_id=""):
        for flow_id in flow_ids:
            self.flows.get(device_id, {}).pop(flow_id, None)
            self.deleted.append((device_id, flow_id))
        return {"flows_deleted": len(flow_ids), "total": len(flow_ids), "errors": None}


def tiny_topology():
    return {
        "status": "ok",
        "hosts": [
            {
                "name": "h1",
                "ip": "10.0.0.1/32",
                "mac": "00:00:00:00:00:01",
                "gateway_mac": "00:aa:00:00:00:01",
            },
            {
                "name": "h2",
                "ip": "10.0.0.2/32",
                "mac": "00:00:00:00:00:02",
                "gateway_mac": "00:aa:00:00:00:02",
            },
        ],
        "switches": [
            {
                "name": "s1",
                "device_id": "device:bmv2:1",
                "router_mac": "00:bb:00:00:00:01",
            },
        ],
        "links": [
            {
                "id": "h1-s1",
                "endpoints": [
                    {"node": "h1", "type": "host", "port": 1},
                    {"node": "s1", "type": "switch", "port": 1},
                ],
            },
            {
                "id": "s1-h2",
                "endpoints": [
                    {"node": "s1", "type": "switch", "port": 2},
                    {"node": "h2", "type": "host", "port": 1},
                ],
            },
        ],
    }


def objective(kind, value=True, operator=ComparisonOperator.EQ, unit=None):
    return IntentObjective(type=kind, value=value, operator=operator, unit=unit)


def spec_with(*objectives):
    return IntentSpec(
        intent_id="INT-ACCEPT",
        original_text="test intent",
        normalized_text="test intent",
        selector={"source_ip": "192.168.10.10", "destination_ip": "10.0.0.10"},
        objectives=list(objectives),
        service={"service_ids": ["svc"]},
    )


@pytest.mark.parametrize("kind", list(ObjectiveType))
def test_all_formal_objective_types_round_trip(kind):
    value = (
        True if kind in {ObjectiveType.CONNECTIVITY, ObjectiveType.ISOLATION} else 10
    )
    item = objective(kind, value)
    assert IntentObjective.model_validate(item.model_dump()).type is kind


def test_missing_objective_requires_clarification_instead_of_inventing_sla():
    spec = IntentSpec(
        intent_id="INT-X",
        original_text="make it better",
        normalized_text="make it better",
    )
    assert spec.clarification_required is True
    assert spec.objectives == []


def test_understanding_preserves_explicit_bandwidth_latency_loss_and_jitter():
    plan = IntentPlan(
        raw_intent="guarantee service",
        confidence_score=0.94,
        sub_intents=[
            SubIntent(
                match={"source_ip": "192.168.10.10", "destination_ip": "10.0.0.10"},
                action={"action_type": "SET_PRIORITY", "qos_params": {"bandwidth": 20}},
                constraints={
                    "max_latency_ms": 10,
                    "max_packet_loss_pct": 1,
                    "max_jitter_ms": 2,
                },
            )
        ],
        critic_report={"llm_status": "AVAILABLE"},
    )
    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-1", text="explicit qos", legacy_plan=plan
    )
    kinds = {item.type for item in spec.objectives}
    assert {
        ObjectiveType.MIN_BANDWIDTH,
        ObjectiveType.MAX_LATENCY,
        ObjectiveType.MAX_PACKET_LOSS,
        ObjectiveType.MAX_JITTER,
    } <= kinds
    assert spec.llm_status == "AVAILABLE"


def test_rate_limit_maps_to_max_bandwidth_not_minimum():
    plan = IntentPlan(
        raw_intent="cap",
        sub_intents=[
            SubIntent(
                action={"action_type": "RATE_LIMIT", "qos_params": {"bandwidth": 12}}
            )
        ],
    )
    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-2", text="cap at 12 Mbps", legacy_plan=plan
    )
    assert any(item.type == ObjectiveType.MAX_BANDWIDTH for item in spec.objectives)
    assert not any(item.type == ObjectiveType.MIN_BANDWIDTH for item in spec.objectives)


def test_chinese_bandwidth_cap_never_adds_a_conflicting_minimum_objective():
    plan = IntentPlan(
        raw_intent="将备份业务的带宽上限设置为20Mbps",
        sub_intents=[
            SubIntent(
                action={"action_type": "RATE_LIMIT", "qos_params": {"bandwidth": 20}}
            )
        ],
    )
    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-CAP-20", text="将备份业务的带宽上限设置为20Mbps", legacy_plan=plan
    )
    bandwidth_objectives = [
        item for item in spec.objectives
        if item.type in {ObjectiveType.MIN_BANDWIDTH, ObjectiveType.MAX_BANDWIDTH}
    ]
    assert [(item.type, item.operator, item.value) for item in bandwidth_objectives] == [
        (ObjectiveType.MAX_BANDWIDTH, ComparisonOperator.LTE, 20.0)
    ]


def test_internal_policy_priority_is_not_invented_as_user_objective():
    plan = IntentPlan(
        raw_intent="video",
        sub_intents=[
            SubIntent(
                priority=100,
                action={"action_type": "SET_PRIORITY", "qos_params": {"dscp": 46}},
            )
        ],
    )
    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-P", text="keep the video smooth", legacy_plan=plan
    )
    assert not any(item.type == ObjectiveType.PRIORITY for item in spec.objectives)


def test_service_profile_default_is_distinct_from_user_explicit_sla():
    plan = IntentPlan(
        raw_intent="video",
        sub_intents=[
            SubIntent(
                action={"action_type": "SET_PRIORITY", "qos_params": {"bandwidth": 20}}
            )
        ],
    )
    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-D", text="keep the video smooth", legacy_plan=plan
    )
    bandwidth = next(
        item for item in spec.objectives if item.type == ObjectiveType.MIN_BANDWIDTH
    )
    assert bandwidth.explicit is False
    assert bandwidth.origin == ValueOrigin.SERVICE_PROFILE


def test_understanding_promotes_path_constraints_to_formal_ir():
    plan = IntentPlan(
        raw_intent="avoid",
        sub_intents=[
            SubIntent(
                action={"action_type": "FORWARD"},
                constraints={
                    "avoid_devices": ["s2"],
                    "time_window": {"start": "08:00", "end": "18:00"},
                },
            )
        ],
    )
    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-C", text="avoid s2 between 08:00 and 18:00", legacy_plan=plan
    )
    assert {item.type for item in spec.constraints} == {
        ConstraintType.AVOID_DEVICE,
        ConstraintType.TIME_WINDOW,
    }


def test_llm_unavailable_is_explicit_in_intent_spec():
    plan = IntentPlan(
        raw_intent="connect",
        sub_intents=[SubIntent(action={"action_type": "FORWARD"})],
        critic_report={"llm_status": "LLM_UNAVAILABLE"},
    )
    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-3", text="connect", legacy_plan=plan
    )
    assert spec.llm_status == "LLM_UNAVAILABLE"


def test_plan_validator_accepts_feasible_candidate():
    spec = spec_with(
        objective(ObjectiveType.MIN_BANDWIDTH, 20, ComparisonOperator.GTE, "Mbps")
    )
    candidate = CandidatePlan(
        context_id="CTX",
        path=["a", "b"],
        expected_outcomes={"bottleneck_available_mbps": 30},
    )
    plan = IntentPlanIR(
        intent_id=spec.intent_id,
        intent_revision=1,
        context_id="CTX",
        candidates=[candidate],
        selected_candidate_id=candidate.candidate_id,
    )
    assert (
        PlanValidator().validate(spec, plan).validation_status
        == PlanValidationStatus.VALID
    )


def test_plan_validator_rejects_capacity_exhaustion_as_unsatisfiable():
    spec = spec_with(
        objective(ObjectiveType.MIN_BANDWIDTH, 40, ComparisonOperator.GTE, "Mbps")
    )
    candidate = CandidatePlan(
        context_id="CTX",
        path=["a", "b"],
        expected_outcomes={"bottleneck_available_mbps": 10},
    )
    plan = IntentPlanIR(
        intent_id=spec.intent_id,
        intent_revision=1,
        context_id="CTX",
        candidates=[candidate],
        selected_candidate_id=candidate.candidate_id,
    )
    validated = PlanValidator().validate(spec, plan)
    assert validated.validation_status == PlanValidationStatus.UNSATISFIABLE
    assert "below" in validated.validation_errors[0]


@pytest.mark.asyncio
async def test_execution_agent_installs_and_reads_back_real_controller_ids():
    candidate = CandidatePlan(context_id="CTX", path=["h1", "s1", "h2"])
    plan = IntentPlanIR(
        intent_id="INT-EXEC",
        intent_revision=1,
        context_id="CTX",
        candidates=[candidate],
        selected_candidate_id=candidate.candidate_id,
        validation_status=PlanValidationStatus.VALID,
    )
    onos = FakeOnosClient()
    agent = IntentExecutionAgent(
        topology_fetcher=lambda: _async_value(tiny_topology()),
        onos_client=onos,
        flow_adapter=FakeFlowAdapter(),
    )
    result = await agent.execute(plan)
    assert result.status == ExecutionStatus.COMPLETED
    assert result.config_verified is True
    assert result.flows_installed == 2
    assert all(receipt.flow_ids for receipt in result.device_receipts)


@pytest.mark.asyncio
async def test_execution_agent_make_before_break_cleans_previous_path():
    candidate = CandidatePlan(context_id="CTX", path=["h1", "s1", "h2"])
    plan = IntentPlanIR(
        intent_id="INT-EXEC",
        intent_revision=1,
        context_id="CTX",
        candidates=[candidate],
        selected_candidate_id=candidate.candidate_id,
        validation_status=PlanValidationStatus.VALID,
    )
    onos = FakeOnosClient()
    onos.flows = {"device:bmv2:1": {"old-1": "ADDED"}}
    previous = IntentExecutionResult(
        intent_id="INT-EXEC",
        plan_id="PLAN-OLD",
        context_id="CTX-OLD",
        selected_candidate_id="CAND-OLD",
        selected_path=["h1", "s1", "h2"],
        status=ExecutionStatus.COMPLETED,
        rollback_refs=[{"device_id": "device:bmv2:1", "flow_id": "old-1"}],
    )
    agent = IntentExecutionAgent(
        topology_fetcher=lambda: _async_value(tiny_topology()),
        onos_client=onos,
        flow_adapter=FakeFlowAdapter(),
    )
    result = await agent.execute(plan, previous=previous, generation=2)
    assert result.status == ExecutionStatus.COMPLETED
    assert ("device:bmv2:1", "old-1") in onos.deleted
    assert result.previous_cleanup["failed"] == 0


@pytest.mark.asyncio
async def test_execution_agent_verifies_path_resources_before_old_path_cleanup():
    candidate = CandidatePlan(context_id="CTX", path=["h1", "s1", "h2"])
    plan = IntentPlanIR(
        intent_id="INT-EXEC",
        intent_revision=1,
        context_id="CTX",
        candidates=[candidate],
        selected_candidate_id=candidate.candidate_id,
        validation_status=PlanValidationStatus.VALID,
    )
    onos = FakeOnosClient()
    onos.flows = {"device:bmv2:1": {"old-1": "ADDED"}}
    previous = IntentExecutionResult(
        intent_id="INT-EXEC",
        plan_id="PLAN-OLD",
        context_id="CTX-OLD",
        selected_candidate_id="CAND-OLD",
        selected_path=["h1", "s1", "h2"],
        status=ExecutionStatus.COMPLETED,
        rollback_refs=[{"device_id": "device:bmv2:1", "flow_id": "old-1"}],
    )
    agent = IntentExecutionAgent(
        topology_fetcher=lambda: _async_value(tiny_topology()),
        onos_client=onos,
        flow_adapter=FakeFlowAdapter(),
    )

    async def prepare(path):
        assert path == ["h1", "s1", "h2"]
        assert "old-1" in onos.flows["device:bmv2:1"]
        return [{"resource": "TC_QOS", "status": "VERIFIED"}]

    result = await agent.execute(plan, previous=previous, prepare_commit=prepare)
    assert result.status == ExecutionStatus.COMPLETED
    assert result.device_config_receipts[0]["status"] == "VERIFIED"
    assert ("device:bmv2:1", "old-1") in onos.deleted


@pytest.mark.asyncio
async def test_execution_agent_keeps_old_path_when_path_resource_prepare_fails():
    candidate = CandidatePlan(context_id="CTX", path=["h1", "s1", "h2"])
    plan = IntentPlanIR(
        intent_id="INT-EXEC",
        intent_revision=1,
        context_id="CTX",
        candidates=[candidate],
        selected_candidate_id=candidate.candidate_id,
        validation_status=PlanValidationStatus.VALID,
    )
    onos = FakeOnosClient()
    onos.flows = {"device:bmv2:1": {"old-1": "ADDED"}}
    previous = IntentExecutionResult(
        intent_id="INT-EXEC",
        plan_id="PLAN-OLD",
        context_id="CTX-OLD",
        selected_candidate_id="CAND-OLD",
        selected_path=["h1", "s1", "h2"],
        status=ExecutionStatus.COMPLETED,
        rollback_refs=[{"device_id": "device:bmv2:1", "flow_id": "old-1"}],
    )
    agent = IntentExecutionAgent(
        topology_fetcher=lambda: _async_value(tiny_topology()),
        onos_client=onos,
        flow_adapter=FakeFlowAdapter(),
    )

    async def prepare(_path):
        raise RuntimeError("TC readback failed")

    result = await agent.execute(plan, previous=previous, prepare_commit=prepare)
    assert result.status == ExecutionStatus.ROLLED_BACK
    assert "old-1" in onos.flows["device:bmv2:1"]
    assert ("device:bmv2:1", "old-1") not in onos.deleted


async def _async_value(value):
    return value


def test_violation_replan_selects_a_different_feasible_path():
    spec = spec_with(objective(ObjectiveType.CONNECTIVITY, True))
    primary = CandidatePlan(context_id="CTX", path=["h1", "s1", "h2"], score=90)
    alternate = CandidatePlan(context_id="CTX", path=["h1", "s2", "h2"], score=80)
    plan = IntentPlanIR(
        intent_id=spec.intent_id,
        intent_revision=1,
        context_id="CTX",
        candidates=[primary, alternate],
        selected_candidate_id=primary.candidate_id,
        validation_status=PlanValidationStatus.VALID,
    )
    control = IntentControlPlane.__new__(IntentControlPlane)
    control.planning = type("Planning", (), {"validator": PlanValidator()})()
    migrated = control._select_migration_candidate(spec, plan, primary.path)
    assert migrated.selected_candidate_id == alternate.candidate_id
    assert migrated.validation_status == PlanValidationStatus.VALID


def test_violation_replan_rejects_identical_path_redeployment():
    spec = spec_with(objective(ObjectiveType.CONNECTIVITY, True))
    primary = CandidatePlan(context_id="CTX", path=["h1", "s1", "h2"], score=90)
    plan = IntentPlanIR(
        intent_id=spec.intent_id,
        intent_revision=1,
        context_id="CTX",
        candidates=[primary],
        selected_candidate_id=primary.candidate_id,
        validation_status=PlanValidationStatus.VALID,
    )
    control = IntentControlPlane.__new__(IntentControlPlane)
    control.planning = type("Planning", (), {"validator": PlanValidator()})()
    migrated = control._select_migration_candidate(spec, plan, primary.path)
    assert migrated.validation_status == PlanValidationStatus.UNSATISFIABLE
    assert "differs" in migrated.validation_errors[0]


def test_violation_replan_does_not_oscillate_to_a_previously_attempted_path():
    spec = spec_with(
        objective(ObjectiveType.MIN_BANDWIDTH, 20, ComparisonOperator.GTE, "Mbps")
    )
    primary = CandidatePlan(context_id="CTX", path=["h1", "s1", "h2"], score=90)
    alternate = CandidatePlan(context_id="CTX", path=["h1", "s2", "h2"], score=80)
    plan = IntentPlanIR(
        intent_id=spec.intent_id,
        intent_revision=1,
        context_id="CTX",
        candidates=[primary, alternate],
        selected_candidate_id=alternate.candidate_id,
        validation_status=PlanValidationStatus.VALID,
    )
    control = IntentControlPlane.__new__(IntentControlPlane)
    control.domain = type(
        "Domain",
        (),
        {
            "executions": lambda _self, _intent_id: [
                {"status": "COMPLETED", "selected_path": primary.path},
                {"status": "COMPLETED", "selected_path": alternate.path},
            ]
        },
    )()
    migrated = control._select_migration_candidate(spec, plan, alternate.path)
    assert migrated.validation_status == PlanValidationStatus.UNSATISFIABLE
    assert "previously deployed" in migrated.validation_errors[0]


def test_assurance_passes_minimum_bandwidth_with_trusted_evidence():
    spec = spec_with(
        objective(ObjectiveType.MIN_BANDWIDTH, 20, ComparisonOperator.GTE, "Mbps")
    )
    item = EvidenceItem(
        intent_id=spec.intent_id,
        source="probe",
        metric="throughput_mbps",
        value=25,
        unit="Mbps",
        quality=EvidenceQuality.TRUSTED,
    )
    result, _ = IntentAssuranceAgent(StaticAggregator([item])).assess(spec)
    assert result.satisfied is True
    assert result.lifecycle == IntentLifecycle.SATISFIED


def test_assurance_uses_formal_minimum_bandwidth_tolerance_without_lowering_sla(monkeypatch):
    monkeypatch.setenv("IDN_QOS_KPI_TOLERANCE_RATIO", "0.95")
    spec = spec_with(
        objective(ObjectiveType.MIN_BANDWIDTH, 30, ComparisonOperator.GTE, "Mbps")
    )
    item = EvidenceItem(
        intent_id=spec.intent_id,
        source="probe",
        metric="throughput_mbps",
        value=29,
        unit="Mbps",
        quality=EvidenceQuality.TRUSTED,
    )
    result, _ = IntentAssuranceAgent(StaticAggregator([item])).assess(spec)
    assert result.satisfied is True
    assert result.assessments[0].expected == 30
    assert "effective acceptance threshold 28.5" in result.assessments[0].reason


def test_assurance_rejects_measurement_below_formal_tolerance(monkeypatch):
    monkeypatch.setenv("IDN_QOS_KPI_TOLERANCE_RATIO", "0.95")
    spec = spec_with(
        objective(ObjectiveType.MIN_BANDWIDTH, 30, ComparisonOperator.GTE, "Mbps")
    )
    item = EvidenceItem(
        intent_id=spec.intent_id,
        source="probe",
        metric="throughput_mbps",
        value=28.4,
        unit="Mbps",
        quality=EvidenceQuality.TRUSTED,
    )
    result, _ = IntentAssuranceAgent(StaticAggregator([item])).assess(spec)
    assert result.satisfied is False
    assert result.assessments[0].expected == 30


def test_assurance_fails_max_latency_with_trusted_evidence():
    spec = spec_with(
        objective(ObjectiveType.MAX_LATENCY, 5, ComparisonOperator.LTE, "ms")
    )
    item = EvidenceItem(
        intent_id=spec.intent_id,
        source="probe",
        metric="latency_ms",
        value=9,
        unit="ms",
        quality=EvidenceQuality.TRUSTED,
    )
    result, _ = IntentAssuranceAgent(StaticAggregator([item])).assess(spec)
    assert result.satisfied is False
    assert result.assessments[0].status == AssessmentStatus.FAIL


@pytest.mark.parametrize(
    "quality",
    [EvidenceQuality.MISSING, EvidenceQuality.STALE, EvidenceQuality.UNTRUSTED],
)
def test_missing_stale_or_untrusted_evidence_is_unknown(quality):
    spec = spec_with(objective(ObjectiveType.CONNECTIVITY, True))
    item = EvidenceItem(
        intent_id=spec.intent_id,
        source="probe",
        metric="connectivity",
        value=True,
        quality=quality,
    )
    result, _ = IntentAssuranceAgent(StaticAggregator([item])).assess(spec)
    assert result.satisfied is None
    assert result.assessments[0].status == AssessmentStatus.UNKNOWN


def test_priority_without_authoritative_adapter_is_unknown():
    spec = spec_with(objective(ObjectiveType.PRIORITY, 100))
    result, _ = IntentAssuranceAgent(StaticAggregator([])).assess(spec)
    assert result.assessments[0].status == AssessmentStatus.UNKNOWN


def test_domain_store_persists_spec_runtime_history_plan_and_assurance(tmp_path):
    base = IntentStore(tmp_path / "intent.db")
    domain = IntentDomainStore(base)
    spec = spec_with(objective(ObjectiveType.CONNECTIVITY, True))
    domain.save_spec(spec)
    domain.save_runtime(IntentRuntimeState(intent_id=spec.intent_id))
    for lifecycle in (
        IntentLifecycle.UNDERSTANDING,
        IntentLifecycle.PARSED,
        IntentLifecycle.SENSING,
        IntentLifecycle.PLANNING,
    ):
        domain.transition(spec.intent_id, lifecycle, actor="test", reason="acceptance")
    context = NetworkContext(quality=EvidenceQuality.TRUSTED)
    domain.save_context(spec.intent_id, context)
    candidate = CandidatePlan(context_id=context.context_id, path=["a", "b"])
    plan = IntentPlanIR(
        intent_id=spec.intent_id,
        intent_revision=1,
        context_id=context.context_id,
        candidates=[candidate],
        selected_candidate_id=candidate.candidate_id,
        validation_status=PlanValidationStatus.VALID,
    )
    domain.save_plan(plan)
    result, _ = IntentAssuranceAgent(StaticAggregator([])).assess(
        spec, plan_id=plan.plan_id, context_id=context.context_id
    )
    domain.save_assurance(result)
    assert domain.get_spec(spec.intent_id).intent_id == spec.intent_id
    assert domain.get_runtime(spec.intent_id).lifecycle == IntentLifecycle.PLANNING
    assert domain.history(spec.intent_id)[-1]["reason"] == "acceptance"
    assert domain.plans(spec.intent_id)[0]["plan_id"] == plan.plan_id
    assert domain.assurance(spec.intent_id)[0]["assurance_id"] == result.assurance_id


def test_domain_store_keeps_intent_revisions(tmp_path):
    domain = IntentDomainStore(IntentStore(tmp_path / "revision.db"))
    first = spec_with(objective(ObjectiveType.CONNECTIVITY, True))
    second = first.model_copy(update={"revision": 2, "priority": "critical"})
    domain.save_spec(first)
    domain.save_spec(second)
    assert domain.get_spec(first.intent_id).revision == 2
    assert domain.get_spec(first.intent_id, revision=1).priority == "medium"


def test_api_exposes_first_class_intent_subresources(app_client):
    response = app_client.post(
        "/api/v1/intents",
        headers={"X-API-Key": "demo-key-xxx"},
        json={"text": "connect 192.168.10.10 to 10.0.0.10", "priority": "high"},
    )
    assert response.status_code == 202
    intent_id = response.json()["intent_id"]
    for suffix in (
        "spec",
        "runtime",
        "plan",
        "execution",
        "assurance",
        "history",
        "decisions",
        "evidence",
        "healing",
    ):
        result = app_client.get(f"/api/v1/intents/{intent_id}/{suffix}")
        assert result.status_code == 200, (suffix, result.text)


def test_withdraw_api_moves_formal_lifecycle(app_client):
    created = app_client.post(
        "/api/v1/intents",
        headers={"X-API-Key": "demo-key-xxx"},
        json={"text": "connect 192.168.10.10 to 10.0.0.10"},
    ).json()
    result = app_client.post(
        f"/api/v1/intents/{created['intent_id']}/withdraw",
        headers={"X-API-Key": "demo-key-xxx"},
    )
    assert result.status_code == 200
    assert result.json()["runtime"]["lifecycle"] == "WITHDRAWN"

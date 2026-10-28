from __future__ import annotations

import json
from pathlib import Path

import pytest
from agents.intent_understanding import IntentUnderstandingAgent
from agents.risk_classifier import classify_risk
from agents.status_verifier import StatusVerifierAgent
from api.decision_report import _build_a2a_timeline
from api.main import create_app
from api.routes import SubmitIntentRequest, preview_intent
from api.store import IntentStore
from api.topology_paths import TopologyPathRegistry
from infra.trace import TraceBus
from models.intent import IntentPriority
from models.intent_domain import (
    IntentLifecycle,
    IntentObjective,
    IntentRuntimeState,
    IntentSpec,
    ObjectiveType,
    ValueOrigin,
)
from models.intent_plan import IntentPlan, SubIntent
from models.network import NetworkSnapshot
from models.telemetry import KPIMetrics, TelemetryData
from models.yang_policy import (
    ActionRules,
    ActionType,
    MatchCriteria,
    NetworkPolicy,
    YANGPolicy,
)
from orchestrator import _build_user_intent
from orchestrator.intent_control_plane import IntentControlPlane
from persistence.intent_domain_store import IntentDomainStore


def test_chinese_20m_guarantee_is_user_explicit_even_when_profile_matches():
    plan = IntentPlan(
        raw_intent="video",
        sub_intents=[
            SubIntent(
                action={"action_type": "SET_PRIORITY", "qos_params": {"bandwidth": 20}}
            )
        ],
    )
    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-20M", text="视频会议给我保20M", legacy_plan=plan
    )
    bandwidth = next(
        item for item in spec.objectives if item.type == ObjectiveType.MIN_BANDWIDTH
    )
    assert bandwidth.value == 20
    assert bandwidth.explicit is True
    assert bandwidth.origin == ValueOrigin.USER_EXPLICIT
    assert "20M" in bandwidth.source_text


def test_relative_priority_is_preserved_as_explicit_ir():
    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-PRIORITY",
        text="业务A优先级高于业务B",
        legacy_plan=None,
    )
    assert len(spec.preferences) == 1
    relation = spec.preferences[0]
    assert relation.higher_scope == "业务a"
    assert relation.lower_scope == "业务b"
    assert relation.dimension.value == "SERVICE_PRIORITY"
    assert relation.strength.value == "SOFT"
    assert relation.origin.value == "USER_EXPLICIT"
    assert relation.explicit is True


@pytest.mark.asyncio
async def test_preview_returns_intent_spec_when_network_is_unavailable(monkeypatch):
    from agents.network_sensing import NetworkSensingAgent

    async def unavailable(self):
        raise ConnectionError("ONOS unavailable")

    monkeypatch.setattr(NetworkSensingAgent, "collect_snapshot", unavailable)
    result = await preview_intent(
        SubmitIntentRequest(
            text="视频会议给我保20M",
            priority=IntentPriority.HIGH,
            context={"source": {"ip": "h1"}, "destination": {"ip": "h2"}},
        )
    )

    assert result["intent_spec"]["objectives"][0]["origin"] == "USER_EXPLICIT"
    assert result["network_status"]["status"] == "UNAVAILABLE"
    assert result["planning_status"] == "NOT_EVALUATED"


def test_failed_auto_healing_task_response_is_not_reported_success():
    timeline = _build_a2a_timeline(
        {
            "verification_report": {
                "passed": False,
                "auto_healing": {"status": "error", "reason": "rollback failed"},
            }
        }
    )
    healing_response = next(
        item
        for item in timeline
        if item["source_agent"] == "auto_healing_controller"
        and item["message_type"] == "Task_Response"
    )
    assert healing_response["status"] == "failed"


def test_a2a_normalization_has_all_correlation_dimensions_even_when_null():
    message = TraceBus.normalize_message({"intent_id": "INT-1"})
    for key in (
        "intent_id",
        "intent_revision",
        "plan_id",
        "context_id",
        "correlation_id",
        "trace_id",
    ):
        assert key in message


def test_topology_role_alias_does_not_depend_on_research_path_ids(tmp_path):
    source = Path(__file__).resolve().parents[1] / "config" / "topology_paths.json"
    config = json.loads(source.read_text(encoding="utf-8"))
    for index, item in enumerate(config["paths"]):
        item["path_id"] = f"portable-path-{index}"
    target = tmp_path / "portable-paths.json"
    target.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    registry = TopologyPathRegistry(str(target))

    assert registry.valid
    assert registry.get("primary").role == "PRIMARY"
    assert registry.get("backup").role == "BACKUP"
    assert not registry.get("primary").path_id.startswith("research_")


def _spec(intent_id: str, selector: dict, value: int, priority: str = "medium"):
    return IntentSpec(
        intent_id=intent_id,
        original_text=intent_id,
        normalized_text=intent_id,
        selector=selector,
        objectives=[IntentObjective(type=ObjectiveType.MIN_BANDWIDTH, value=value)],
        priority=priority,
    )


def test_scope_relation_is_classified_before_conflict_type(tmp_path):
    store = IntentStore(tmp_path / "conflict.db")
    domain = IntentDomainStore(store)
    other = _spec("INT-OTHER", {"source_ip": "a", "destination_ip": "b"}, 30, "high")
    new = _spec("INT-NEW", {"source_ip": "a", "destination_ip": "b"}, 20, "low")
    for spec in (other, new):
        store.create(intent_id=spec.intent_id, text=spec.original_text)
        domain.save_spec(spec)
        domain.save_runtime(
            IntentRuntimeState(
                intent_id=spec.intent_id, lifecycle=IntentLifecycle.SATISFIED
            )
        )
    conflict = IntentControlPlane(store, domain)._check_conflicts(new)[0]

    assert conflict.scope_relation == "SAME_SCOPE"
    assert conflict.conflict_type == "DOMINATED"


def test_core_mutation_openapi_responses_have_schemas():
    schema = create_app().openapi()
    for path, method, success in (
        ("/api/v1/intents", "post", "202"),
        ("/api/v1/intents/preview", "post", "200"),
        ("/api/v1/intents/{intent_id}", "patch", "200"),
        ("/api/v1/intents/{intent_id}", "delete", "200"),
        ("/api/v1/intents/{intent_id}/withdraw", "post", "200"),
    ):
        operation = schema["paths"][path][method]
        assert operation["responses"][success]["content"]["application/json"]["schema"]
        assert operation["responses"]["409"]["content"]["application/json"]["schema"]


def test_compatibility_workflow_generates_session_id_when_context_omits_it():
    generated = _build_user_intent("forward traffic")
    explicit = _build_user_intent("forward traffic", {"session_id": "SES-42"})

    assert isinstance(generated.session_id, str) and generated.session_id
    assert explicit.session_id == "SES-42"


@pytest.mark.asyncio
async def test_generic_forward_uses_network_evidence_without_forcing_service_profile(
    monkeypatch,
):
    policy = YANGPolicy(
        raw_intent="forward arbitrary traffic",
        network_policy=NetworkPolicy(
            policy_id="POL-GENERIC",
            policy_name="generic-forward",
            match=MatchCriteria(
                source_ip="198.51.100.0/24",
                destination_ip="203.0.113.0/24",
            ),
            action=ActionRules(action_type=ActionType.FORWARD),
        ),
    )

    async def fake_telemetry(self, intent_id, sample_count):
        return (
            TelemetryData(
                intent_id=intent_id, sample_count=sample_count, kpi=KPIMetrics()
            ),
            "unit-network-evidence",
        )

    monkeypatch.setattr(StatusVerifierAgent, "_collect_live_telemetry", fake_telemetry)
    result = await StatusVerifierAgent().verify(
        "INT-GENERIC", policy, {"status": "SUCCESS"}, wait_secs=0
    )

    assert result.passed is True
    assert result.verification_state == "DEPLOYMENT_AND_NETWORK_EVIDENCE_VERIFIED"
    assert result.config_verification["service_profile"]["status"] == "NOT_REQUIRED"


def test_risk_classifier_handles_supported_forward_action_without_missing_enum():
    policy = YANGPolicy(
        network_policy=NetworkPolicy(
            policy_id="POL-RISK",
            policy_name="risk-forward",
            match=MatchCriteria(
                source_ip="198.51.100.1/32", destination_ip="203.0.113.1/32"
            ),
            action=ActionRules(action_type=ActionType.FORWARD),
        )
    )

    assert (
        classify_risk(
            yang_policy=policy,
            flow_table_plan={},
            network_snapshot=NetworkSnapshot(),
        )
        == "LOW"
    )

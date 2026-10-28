"""Regression coverage for typed, non-destructive preference handling."""
from __future__ import annotations

import pytest

from agents.intent_understanding import IntentUnderstandingAgent
from models.intent_domain import (
    CandidatePlan,
    IntentSpec,
    PreferenceDimension,
    PreferenceRelation,
    PreferenceStrength,
    ValueOrigin,
)
from models.intent_lifecycle_state_machine import IntentLifecycleStateMachine
from models.intent_domain import IntentLifecycle
from services.preference_relations import (
    PreferenceCycleError,
    path_preference_projection,
    rank_path_candidates,
    scheduler_projection,
    validate_acyclic,
)
from models.intent_plan import IntentPlan
from models.network import NetworkSnapshot
from models.yang_policy import ActionRules, ActionType, MatchCriteria, NetworkPolicy, YANGPolicy


def relation(higher: str, lower: str, dimension=PreferenceDimension.SERVICE_PRIORITY):
    return PreferenceRelation(
        higher_scope=higher,
        lower_scope=lower,
        dimension=dimension,
        strength=PreferenceStrength.SOFT,
        origin=ValueOrigin.USER_EXPLICIT,
    )


def test_legacy_relative_priority_is_loaded_as_a_typed_relation():
    spec = IntentSpec(
        intent_id="INT-PREF-LEGACY",
        original_text="legacy",
        normalized_text="legacy",
        clarification_required=True,
        preferences=[{
            "type": "RELATIVE_PRIORITY",
            "subject": "alpha",
            "relative_to": "beta",
            "origin": "USER_EXPLICIT",
        }],
    )
    assert spec.preferences[0].higher_scope == "alpha"
    assert spec.preferences[0].lower_scope == "beta"
    assert spec.preferences[0].dimension == PreferenceDimension.SERVICE_PRIORITY


def test_relative_preference_is_not_forced_to_be_a_bandwidth_sla():
    spec = IntentSpec(
        intent_id="INT-PREF-ONLY",
        original_text="prefer alpha over beta",
        normalized_text="prefer alpha over beta",
        preferences=[relation("alpha", "beta")],
    )
    assert spec.clarification_required is False
    assert spec.plannability_errors() == []


def test_cycle_is_rejected_before_any_candidate_or_southbound_action():
    with pytest.raises(PreferenceCycleError) as caught:
        validate_acyclic([relation("alpha", "beta"), relation("beta", "alpha")])
    assert caught.value.code == "PREFERENCE_CYCLE"
    assert caught.value.dimension == PreferenceDimension.SERVICE_PRIORITY


def test_cycle_validation_includes_active_persisted_relations(tmp_path):
    from api.store import IntentStore
    from orchestrator.intent_control_plane import IntentControlPlane
    from persistence.preference_relation_store import PreferenceRelationStore

    store = IntentStore(str(tmp_path / "preferences.db"))
    repo = PreferenceRelationStore(store)
    existing = relation("alpha", "beta")
    repo.apply(intent_id="INT-ACTIVE", intent_revision=1, relation=existing, projection={})
    plane = IntentControlPlane(store=store)
    candidate = IntentSpec(
        intent_id="INT-CYCLE", original_text="beta over alpha", normalized_text="beta over alpha",
        preferences=[relation("beta", "gamma"), relation("gamma", "alpha")],
    )
    with pytest.raises(PreferenceCycleError) as caught:
        plane._validate_preference_relations(candidate)
    assert caught.value.code == "PREFERENCE_CYCLE"


def test_scheduler_projection_is_stable_and_has_no_southbound_mutation():
    projection = scheduler_projection([relation("research", "shared"), relation("shared", "background")])
    assert projection["ranked_scopes"] == ["research", "shared", "background"]
    assert projection["ranks"]["research"] < projection["ranks"]["shared"]
    assert projection["southbound_mutation_required"] is False


def test_path_preference_bonus_is_generic_and_requires_configured_candidate_role():
    candidates = [
        CandidatePlan(context_id="CTX-1", candidate_id="CAND-PRIMARY", preference_scopes=["primary"]),
        CandidatePlan(context_id="CTX-1", candidate_id="CAND-BACKUP", preference_scopes=["backup"]),
    ]
    bonuses = rank_path_candidates(candidates, [
        relation("primary", "backup", PreferenceDimension.PATH_PREFERENCE)
    ])
    assert bonuses == {"CAND-PRIMARY": 1, "CAND-BACKUP": 0}


def test_path_preference_projection_selects_only_live_configured_roles(monkeypatch):
    from api.topology_paths import TopologyPathRegistry

    def report(self, topology):
        return {
            "status": "ok",
            "paths": [
                {"path_id": "any_primary", "service_id": "", "role": "PRIMARY", "src_host": "src", "dst_host": "dst", "available": True, "switches": ["edge", "core"], "errors": []},
                {"path_id": "any_backup", "service_id": "", "role": "BACKUP", "src_host": "src", "dst_host": "dst", "available": True, "switches": ["edge", "alternate"], "errors": []},
            ],
        }

    monkeypatch.setattr(TopologyPathRegistry, "validate_runtime", report)
    projection = path_preference_projection(
        [relation("primary", "backup", PreferenceDimension.PATH_PREFERENCE)],
        src_host="src", dst_host="dst", service_id="", runtime_topology={},
    )
    assert projection["selected_path_id"] == "any_primary"
    assert projection["fallback_path_ids"] == ["any_backup"]
    assert projection["southbound_mutation_required"] is False


def test_research_video_path_config_matches_its_registered_service_endpoints():
    from api.service_profiles import get_service_profile
    from api.topology_paths import TopologyPathRegistry

    profile = get_service_profile("research_video")
    registry = TopologyPathRegistry()
    primary = registry.get("research_video_primary")
    backup = registry.get("research_video_backup")
    assert primary and backup
    assert (primary.src_host, primary.dst_host, primary.role) == (profile.src_host, profile.dst_host, "PRIMARY")
    assert (backup.src_host, backup.dst_host, backup.role) == (profile.src_host, profile.dst_host, "BACKUP")
    assert registry.get_by_role("PRIMARY", src_host="unrelated", dst_host=profile.dst_host) is None


def test_path_preference_requires_explicit_endpoints_before_path_config_lookup():
    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-PATH-PREFERENCE",
        text="Prefer the primary path for the research video service and use the backup path only if the primary path is unavailable.",
        legacy_plan=IntentPlan(raw_intent="path preference"),
    )
    assert spec.clarification_required is True
    assert spec.semantic_scope_complete is False
    assert spec.source == {}
    assert spec.destination == {}
    assert spec.unresolved_fields == ["source", "destination"]
    assert [item.dimension for item in spec.preferences] == [PreferenceDimension.PATH_PREFERENCE]


def test_text_extraction_builds_typed_relation_without_a_named_business_special_case():
    relations = IntentUnderstandingAgent._extract_preference_relations_v2(
        "Prefer alpha service over beta service."
    )
    assert len(relations) == 1
    assert relations[0].dimension == PreferenceDimension.SERVICE_PRIORITY
    assert relations[0].higher_scope == "alpha"
    assert relations[0].lower_scope == "beta"


def test_named_service_preference_resolves_each_side_independently_without_sla_slots():
    plan = IntentPlan(
        raw_intent="Prefer the research video service over the shared video service when they compete for resources.",
        critic_report={
            "llm_status": "SUCCESS",
            "llm_runtime_mode": "REAL_LLM",
            "llm_provider": "openai",
            "llm_model": "qwen-plus",
            "structured_output_valid": True,
            "validator_result": "PASS",
            "fallback_used": False,
        },
    )
    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-PREF-CANONICAL",
        text=plan.raw_intent,
        legacy_plan=plan,
    )

    assert [(relation.higher_scope, relation.lower_scope) for relation in spec.preferences] == [
        ("research_video", "shared_port_video")
    ]
    assert spec.preferences[0].higher_scope != spec.preferences[0].lower_scope
    assert spec.clarification_required is False
    assert spec.unresolved_fields == []
    assert spec.execution_capability == "CONTROL_PLANE_SCHEDULING_PREFERENCE"
    assert spec.southbound_mutation_required is False
    assert spec.llm_status == "SUCCESS"
    assert spec.llm_runtime_mode.value == "REAL_LLM"


@pytest.mark.asyncio
async def test_preview_invokes_planner_and_returns_control_plane_only_projection(monkeypatch):
    from agents.intent_planner import IntentPlannerAgent
    from agents.network_sensing import NetworkSensingAgent
    from api.routes import SubmitIntentRequest, preview_intent

    calls = []

    async def fake_snapshot(self):
        return NetworkSnapshot()

    async def fake_plan(self, **kwargs):
        calls.append(kwargs)
        return IntentPlan(
            raw_intent=kwargs["user_input"],
            critic_report={
                "llm_status": "SUCCESS",
                "llm_runtime_mode": "REAL_LLM",
                "llm_provider": "openai",
                "llm_model": "qwen-plus",
                "structured_output_valid": True,
                "validator_result": "PASS",
                "fallback_used": False,
            },
        )

    monkeypatch.setattr(NetworkSensingAgent, "collect_snapshot", fake_snapshot)
    async def fake_close(self):
        return None

    monkeypatch.setattr(NetworkSensingAgent, "close", fake_close)
    monkeypatch.setattr(IntentPlannerAgent, "plan", fake_plan)

    result = await preview_intent(SubmitIntentRequest(
        text="Prefer the research video service over the shared video service when they compete for resources."
    ))

    assert calls, "preview must invoke the production planner boundary"
    spec = result["intent_spec"]
    assert spec["llm_status"] == "SUCCESS"
    assert spec["llm_runtime_mode"] == "REAL_LLM"
    assert spec["llm_provider"] == "openai"
    assert spec["llm_model"] == "qwen-plus"
    assert spec["llm_structured_output_valid"] is True
    assert spec["llm_validator_result"] == "PASS"
    assert spec["fallback_used"] is False
    assert [(item["higher_scope"], item["lower_scope"]) for item in spec["preferences"]] == [
        ("research_video", "shared_port_video")
    ]
    assert result["yang_policies"] == []
    assert result["flow_table_plan"]["write_requests"] == []
    assert result["flow_table_plan"]["tc_mutations"] == []
    assert result["flow_table_plan"]["onos_mutations"] == []


@pytest.mark.asyncio
async def test_policy_compiler_guard_rejects_preference_southbound_writes():
    pytest.importorskip("p4.config.v1.p4info_pb2")
    from agents.policy_compiler import PolicyCompilerAgent

    policy = YANGPolicy(
        network_policy=NetworkPolicy(
            policy_id="POL-PREF-GUARD",
            policy_name="preference-guard",
            match=MatchCriteria(source_ip="192.0.2.1/32", destination_ip="198.51.100.1/32"),
            action=ActionRules(action_type=ActionType.SET_PRIORITY),
        )
    )
    plan = await PolicyCompilerAgent().compile(
        policy,
        execution_capability="CONTROL_PLANE_SCHEDULING_PREFERENCE",
        southbound_mutation_required=False,
    )

    assert plan.policy_operation == "CONTROL_PLANE_ONLY"
    assert plan.write_requests == []
    assert plan.action_profile_requests == []


def test_compound_cycle_text_preserves_both_edges_for_dag_validation():
    relations = IntentUnderstandingAgent._extract_preference_relations_v3(
        "Research video should have higher priority than shared video, "
        "while shared video should also have higher priority than research video."
    )
    assert {(item.higher_scope, item.lower_scope) for item in relations} == {
        ("research_video", "shared_port_video"),
        ("shared_port_video", "research_video"),
    }
    with pytest.raises(PreferenceCycleError):
        validate_acyclic(relations)


def test_self_preference_is_rejected_with_a_stable_reason_code():
    with pytest.raises(ValueError, match="INVALID_SELF_PREFERENCE"):
        relation("research", "research")


def test_preference_cycle_uses_semantic_gates_then_terminal_unsatisfiable():
    states = [
        IntentLifecycle.UNDERSTANDING,
        IntentLifecycle.PARSED,
        IntentLifecycle.SENSING,
        IntentLifecycle.PLANNING,
        IntentLifecycle.UNSATISFIABLE,
    ]
    for previous, current in zip(states, states[1:]):
        IntentLifecycleStateMachine.validate("INT-PREF-CYCLE", previous, current)


@pytest.mark.asyncio
async def test_preference_runtime_reuses_replaces_and_withdraws_without_southbound_state(tmp_path):
    from api.store import IntentStore
    from orchestrator.intent_control_plane import IntentControlPlane
    from persistence.intent_domain_store import IntentDomainStore

    store = IntentStore(tmp_path / "preferences.db")
    control = IntentControlPlane(store=store, domain=IntentDomainStore(store))

    async def submit(intent_id: str, text: str):
        store.create(intent_id=intent_id, text=text)
        control.register_received(intent_id=intent_id, text=text, priority="medium")
        plan = IntentPlan(
            raw_intent=text,
            critic_report={
                "llm_status": "SUCCESS", "llm_runtime_mode": "REAL_LLM",
                "llm_provider": "openai", "llm_model": "qwen-plus",
                "structured_output_valid": True, "validator_result": "PASS",
                "fallback_used": False,
            },
        )
        return await control.ingest_pipeline_result(
            intent_id=intent_id, legacy_status="semantic_complete",
            result={"intent_plan": plan.model_dump(mode="json")}, snapshot={},
        )

    forward = "Prefer the research video service over the shared video service when they compete for resources."
    reverse = "Prefer the shared video service over the research video service when they compete for resources."
    first = await submit("INT-PREF-1", forward)
    assert first.lifecycle == IntentLifecycle.SATISFIED
    active = control.preference_relations.active()
    assert len(active) == 1
    assert active[0]["relation"]["higher_scope"] == "research_video"
    execution = control.domain.executions("INT-PREF-1")[0]
    assert execution["flows_installed"] == 0
    assert execution["commit_verified"] is True

    duplicate = await submit("INT-PREF-2", forward)
    assert duplicate.lifecycle == IntentLifecycle.SATISFIED
    assert len(control.preference_relations.active()) == 1

    updated = await submit("INT-PREF-3", reverse)
    assert updated.lifecycle == IntentLifecycle.SATISFIED
    active = control.preference_relations.active()
    assert len(active) == 1
    assert active[0]["relation"]["higher_scope"] == "shared_port_video"
    history = control.preference_relations.history_for_scope(active[0]["scope_key"])
    assert {item["status"] for item in history} >= {"ACTIVE", "SUPERSEDED"}

    withdrawn = await control.withdraw("INT-PREF-3")
    assert withdrawn.lifecycle == IntentLifecycle.WITHDRAWN
    assert control.preference_relations.active() == []
    assert {item["status"] for item in control.preference_relations.history_for_scope(history[0]["scope_key"])} >= {"WITHDRAWN", "SUPERSEDED"}


def test_self_preference_is_exposed_as_a_validation_error_for_runtime_rejection():
    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-PREF-SELF",
        text="Research video should have higher priority than research video.",
        legacy_plan=IntentPlan(raw_intent="self"),
    )
    assert spec.preference_validation_error == "INVALID_SELF_PREFERENCE"
    assert spec.clarification_required is False


@pytest.mark.asyncio
async def test_real_planner_preference_stops_graph_before_policy_modelling(monkeypatch):
    from agents.intent_planner import IntentPlannerAgent
    from models.agent_state import GlobalAgentState
    from models.intent import IntentPriority, UserIntent
    from orchestrator.graph import route_after_understanding
    from orchestrator.nodes.understanding_node import UnderstandingNode

    async def fake_plan(self, **kwargs):
        return IntentPlan(
            raw_intent=kwargs["user_input"],
            critic_report={
                "llm_status": "SUCCESS", "llm_runtime_mode": "REAL_LLM",
                "llm_provider": "openai", "llm_model": "qwen-plus",
                "structured_output_valid": True, "validator_result": "PASS",
                "fallback_used": False,
            },
        )

    monkeypatch.setattr(IntentPlannerAgent, "plan", fake_plan)
    state = GlobalAgentState(
        intent_id="INT-PREF-GRAPH-GUARD",
        user_intent=UserIntent(
            text="Prefer the research video service over the shared video service when they compete for resources.",
            priority=IntentPriority.MEDIUM,
        ),
    )
    result = await UnderstandingNode().run(state)

    assert result.semantic_only_completed is True
    assert result.execution_status == "NOT_RUN_CONTROL_PLANE_PREFERENCE"
    assert result.flow_table_plan is None
    assert route_after_understanding(result) == "__end__"


@pytest.mark.asyncio
async def test_real_planner_self_preference_stops_graph_before_policy_modelling(monkeypatch):
    from agents.intent_planner import IntentPlannerAgent
    from models.agent_state import GlobalAgentState
    from models.intent import IntentPriority, UserIntent
    from orchestrator.graph import route_after_understanding
    from orchestrator.nodes.understanding_node import UnderstandingNode

    async def fake_plan(self, **kwargs):
        return IntentPlan(raw_intent=kwargs["user_input"])

    monkeypatch.setattr(IntentPlannerAgent, "plan", fake_plan)
    state = GlobalAgentState(
        intent_id="INT-PREF-SELF-GRAPH-GUARD",
        user_intent=UserIntent(
            text="Research video should have higher priority than research video.",
            priority=IntentPriority.MEDIUM,
        ),
    )
    result = await UnderstandingNode().run(state)

    assert result.semantic_only_completed is True
    assert result.flow_table_plan is None
    assert route_after_understanding(result) == "__end__"

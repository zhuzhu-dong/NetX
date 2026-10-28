"""Regression coverage for semantic resolution and pre-planning gates."""

from __future__ import annotations

import pytest

from agents.intent_planner import IntentPlannerAgent
from agents.intent_planning import IntentNotPlannableError, PlanningAgent
from agents.intent_understanding import IntentUnderstandingAgent
from agents.policy_modeling import PolicyModelingAgent
from agents.semantic_resolver import EndpointResolver, ServiceSemanticResolver
from api.service_profiles import get_service_profile
from models.agent_state import (
    ConsensusReasonCode,
    ConsensusStatus,
    ExecutionStage,
    GlobalAgentState,
    StageStatus,
)
from models.intent import UserIntent
from models.intent_domain import IntentObjective, IntentSpec, NetworkContext, ObjectiveType
from models.intent_plan import IntentPlan, SubIntent
from models.network import NetworkSnapshot
from models.semantic_resolution import ResolutionStatus
from models.yang_policy import ActionRules, ActionType, MatchCriteria, NetworkPolicy, YANGPolicy
from orchestrator.nodes import UnderstandingNode


SCREENSHOT_SENTENCE = "保障科研楼 h_research 到数据中心 h_dc 的视频业务带宽不低于10Mbps"


@pytest.mark.asyncio
async def test_current_sentence_resolves_exact_service_and_selector(rule_fallback_llm):
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(SCREENSHOT_SENTENCE)
    item = plan.sub_intents[0]

    assert plan.plan_id
    assert not plan.clarification_questions
    assert item.service_id == "research_video"
    assert item.match == {
        "source_ip": "192.168.10.10/32",
        "destination_ip": "10.0.0.10/32",
        "destination_port": 5004,
        "protocol": "UDP",
    }
    assert item.action["qos_params"]["qos_mode"] == "MIN_BANDWIDTH"
    assert item.action["qos_params"]["bandwidth"] == 10
    assert item.action["qos_params"]["dscp_value"] == 46
    assert plan.field_provenance["bandwidth_mbps"] == "USER_EXPLICIT"
    assert plan.field_provenance["service_id"] == "SEMANTIC_RESOLUTION"
    assert plan.field_provenance["protocol"] == "SERVICE_PROFILE"
    assert plan.field_provenance["destination_port"] == "SERVICE_PROFILE"


def test_endpoint_resolver_separates_host_network_and_selector():
    pair = EndpointResolver().resolve_text("h_research 到 h_dc")
    assert pair.source.host_ip == "192.168.10.10"
    assert pair.source.selector_prefix == "192.168.10.10/32"
    assert pair.source.interface_network == "192.168.10.0/24"
    assert pair.destination.host_ip == "10.0.0.10"
    assert pair.destination.selector_prefix == "10.0.0.10/32"
    assert pair.destination.selector_prefix != "10.0.0.0/8"
    assert pair.destination.mac == "02:00:00:00:00:14"
    assert pair.destination.attached_device == "device:bmv2:6"
    assert pair.destination.attached_port == 2


def test_endpoint_resolver_maps_business_site_aliases_to_configured_hosts():
    pair = EndpointResolver().resolve_text("科研楼去数据中心")
    assert pair.source.endpoint_id == "h_research"
    assert pair.source.selector_prefix == "192.168.10.10/32"
    assert pair.destination.endpoint_id == "h_dc"
    assert pair.destination.selector_prefix == "10.0.0.10/32"


def test_endpoint_resolver_maps_video_host_alias_to_h_video():
    pair = EndpointResolver().resolve_text("视频主机到数据中心")
    assert pair.source.endpoint_id == "h_video"
    assert pair.source.selector_prefix == "192.168.10.20/32"
    assert pair.destination.endpoint_id == "h_dc"


def test_endpoint_resolver_maps_registered_ip_to_same_canonical_endpoint():
    pair = EndpointResolver().resolve_text("192.168.10.10 to 10.0.0.10")
    assert pair.resolved is True
    assert pair.source.endpoint_id == "h_research"
    assert pair.destination.endpoint_id == "h_dc"


def test_endpoint_resolver_preserves_unregistered_ip_role_for_clarification():
    pair = EndpointResolver().resolve_text("192.168.99.99 to 10.0.0.10")

    assert pair.source.status == ResolutionStatus.NOT_FOUND
    assert pair.source.matched_reference == "192.168.99.99"
    assert pair.destination.endpoint_id == "h_dc"
    assert pair.unresolved_fields == ["source"]


def test_endpoint_resolver_accepts_hostnames_after_chinese_role_markers():
    """A clarification answer must not let the short ``dc`` alias win."""
    pair = EndpointResolver().resolve_text("源是h_ai，目的地是h_dc")

    assert pair.resolved is True
    assert pair.source.endpoint_id == "h_ai"
    assert pair.destination.endpoint_id == "h_dc"


def test_qos_scope_requires_registry_resolved_endpoints_before_planning():
    spec = IntentSpec(
        intent_id="INT-A-B", original_text="protect A to B at 10 Mbps", normalized_text="protect A to B at 10 Mbps",
        source={"selector_prefix": "192.168.10.10/32"}, destination={"selector_prefix": "10.0.0.10/32"},
        selector={"source_ip": "192.168.10.10/32", "destination_ip": "10.0.0.10/32", "protocol": "UDP", "destination_port": 5004},
        service_resolution={"status": "RESOLVED", "selected_service_id": "research_video"},
        endpoint_resolution={"source": {"status": "NOT_FOUND"}, "destination": {"status": "NOT_FOUND"}},
        objectives=[IntentObjective(type=ObjectiveType.MIN_BANDWIDTH, value=10, unit="Mbps")],
    )
    assert spec.is_plannable() is False
    assert "source endpoint is not registry-resolved" in spec.plannability_errors()
    assert "destination endpoint is not registry-resolved" in spec.plannability_errors()


@pytest.mark.asyncio
async def test_acceptance_phrase_resolves_without_template_host_names(rule_fallback_llm):
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "科研楼最近视频会议比较卡，希望优先保证去数据中心的业务，至少20兆。"
    )
    item = plan.sub_intents[0]
    assert not plan.clarification_questions
    assert item.service_id == "research_video"
    assert item.match["source_ip"] == "192.168.10.10/32"
    assert item.match["destination_ip"] == "10.0.0.10/32"
    assert item.match["protocol"] == "UDP"
    assert item.match["destination_port"] == 5004
    assert item.action["qos_params"]["bandwidth"] == 20


def test_named_rate_limit_keeps_endpoint_scope_unresolved_until_user_supplies_it():
    planner = IntentPlannerAgent()
    plan = IntentPlan(
        raw_intent="限制备份业务最多只能使用10Mbps",
        clarification_questions=[
            "请补充限速对象的源主机或源网段。",
            "请补充限速流量的目的地址或目的网段。",
        ],
        sub_intents=[
            SubIntent(
                intent_type="RATE_LIMIT",
                match={"protocol": "ANY"},
                action={
                    "action_type": "RATE_LIMIT",
                    "qos_params": {"qos_mode": "RATE_LIMIT", "bandwidth": 10},
                },
                service_id="backup_data",
            )
        ],
    )
    hydrated = planner._apply_named_service_defaults(plan, plan.raw_intent)
    item = hydrated.sub_intents[0]

    assert item.service_id == "backup_data"
    assert item.match == {
        "destination_port": 5203,
        "protocol": "TCP",
    }
    assert item.action["qos_params"]["qos_mode"] == "RATE_LIMIT"
    assert item.action["qos_params"]["bandwidth"] == 10
    assert len(hydrated.clarification_questions) == 2
    endpoints = item.constraints["endpoint_resolution"]
    assert endpoints["source"]["status"] == "NOT_FOUND"
    assert endpoints["destination"]["status"] == "NOT_FOUND"


def test_single_item_uses_conversation_clarification_for_registered_endpoints():
    """A later A/B answer is authoritative over the original terse clause."""
    planner = IntentPlannerAgent()
    plan = IntentPlan(
        raw_intent=(
            "Guarantee bandwidth from A to B above 10 Mbps. "
            "Clarification: A is h_research and B is h_dc."
        ),
        sub_intents=[
            SubIntent(
                objective="Guarantee bandwidth from A to B above 10 Mbps.",
                intent_type="SET_PRIORITY",
                match={"protocol": "ANY"},
                action={
                    "action_type": "SET_PRIORITY",
                    "qos_params": {"qos_mode": "MIN_BANDWIDTH", "bandwidth": 10},
                },
            )
        ],
    )

    hydrated = planner._apply_named_service_defaults(plan, plan.raw_intent)
    item = hydrated.sub_intents[0]

    assert item.match["source_ip"] == "192.168.10.10/32"
    assert item.match["destination_ip"] == "10.0.0.10/32"
    endpoints = item.constraints["endpoint_resolution"]
    assert endpoints["source"]["status"] == "RESOLVED"
    assert endpoints["destination"]["status"] == "RESOLVED"


def test_resumed_clarification_fragment_does_not_create_a_second_operation():
    planner = IntentPlannerAgent()
    complete = SubIntent(
        objective="Guarantee a known service",
        intent_type="SET_PRIORITY",
        match={
            "source_ip": "192.168.10.10/32",
            "destination_ip": "10.0.0.10/32",
            "protocol": "UDP",
            "destination_port": 5004,
        },
        action={
            "action_type": "SET_PRIORITY",
            "qos_params": {"qos_mode": "MIN_BANDWIDTH", "bandwidth": 10},
        },
        service_id="research_video",
    )
    answer_fragment = SubIntent(
        objective="The service uses UDP destination port 5004",
        intent_type="FORWARD",
        match={"protocol": "UDP", "destination_port": 5004},
        action={"action_type": "FORWARD"},
    )
    llm_plan = IntentPlan(raw_intent="llm", sub_intents=[complete, answer_fragment])
    rule_plan = IntentPlan(raw_intent="rule", sub_intents=[complete, answer_fragment])

    merged = planner._merge_plans(
        llm_plan,
        rule_plan,
        "Guarantee A to B above 10 Mbps. 补充信息：A is h_research; B is h_dc; UDP/5004.",
    )

    assert len(merged.sub_intents) == 1
    assert merged.sub_intents[0].match["source_ip"] == "192.168.10.10/32"


def test_rule_selector_name_overrides_an_untrusted_llm_policy_label():
    """A model label must not associate a new selector with an old service."""
    planner = IntentPlannerAgent()
    text = "Guarantee the research video service from h_ai to h_dc has at least 10 Mbps."
    rule_plan = planner._rule_intent_plan(text, None, None)
    rule_item = rule_plan.sub_intents[0]
    llm_plan = IntentPlan(
        raw_intent=text,
        sub_intents=[
            SubIntent(
                objective=text,
                intent_type="SET_PRIORITY",
                match=dict(rule_item.match),
                action=dict(rule_item.action),
                constraints={"policy_name": "ai-data-qos"},
                priority=100,
                service_id="research_video",
            )
        ],
    )

    merged = planner._merge_plans(llm_plan, rule_plan, text)

    assert merged.sub_intents[0].constraints["policy_name"] == rule_item.constraints["policy_name"]
    assert merged.sub_intents[0].constraints["policy_name"] != "ai-data-qos"


def test_compound_named_services_keep_independent_profiles_and_endpoints():
    """A named backup clause must not inherit a preceding video clause's path."""
    planner = IntentPlannerAgent()
    plan = IntentPlan(
        raw_intent=(
            "保障科研楼到数据中心的视频业务带宽不低于30Mbps，"
            "同时将备份业务带宽上限保持为20Mbps"
        ),
        sub_intents=[
            SubIntent(
                sequence=1,
                objective="保障科研楼到数据中心的视频业务带宽不低于30Mbps",
                intent_type="SET_PRIORITY",
                match={"protocol": "ANY"},
                action={
                    "action_type": "SET_PRIORITY",
                    "qos_params": {"qos_mode": "MIN_BANDWIDTH", "bandwidth": 30},
                },
            ),
            SubIntent(
                sequence=2,
                objective="将备份业务带宽上限保持为20Mbps",
                intent_type="RATE_LIMIT",
                match={"protocol": "ANY"},
                action={
                    "action_type": "RATE_LIMIT",
                    "qos_params": {"qos_mode": "RATE_LIMIT", "bandwidth": 20},
                },
            ),
        ],
    )

    hydrated = planner._apply_named_service_defaults(plan, plan.raw_intent)
    video, backup = hydrated.sub_intents

    assert video.service_id == "research_video"
    assert video.match == {
        "source_ip": "192.168.10.10/32",
        "destination_ip": "10.0.0.10/32",
        "destination_port": 5004,
        "protocol": "UDP",
    }
    assert backup.service_id == "backup_data"
    assert backup.match == {
        "destination_port": 5203,
        "protocol": "TCP",
    }
    assert video.match != backup.match
    assert backup.constraints["endpoint_resolution"]["source"]["status"] == "NOT_FOUND"
    assert backup.constraints["endpoint_resolution"]["destination"]["status"] == "NOT_FOUND"


def test_rule_merge_corrects_llm_misclassified_compound_service_and_cap():
    """A real LLM may split correctly but choose the wrong service for clause two.

    Registered aliases and cap semantics from the matching source clause are
    deterministic evidence, so they must prevail without any service-specific
    branch in planner code.
    """
    planner = IntentPlannerAgent()
    text = (
        "保障科研楼到数据中心的视频业务带宽不低于30Mbps，"
        "同时将备份业务带宽上限保持在20Mbps"
    )
    rule_plan = planner._rule_intent_plan(text, None, None)
    llm_plan = IntentPlan(
        raw_intent=text,
        sub_intents=[
            SubIntent(
                sequence=1, objective="保障视频业务带宽为30Mbps",
                intent_type="SET_PRIORITY", match={"protocol": "UDP"},
                action={"action_type": "SET_PRIORITY", "qos_params": {"qos_mode": "MIN_BANDWIDTH", "bandwidth": 30}},
                service_id="research_video",
            ),
            SubIntent(
                sequence=2, objective="保障背景视频带宽为20Mbps",
                intent_type="SET_PRIORITY", match={"protocol": "UDP"},
                action={"action_type": "SET_PRIORITY", "qos_params": {"qos_mode": "MIN_BANDWIDTH", "bandwidth": 20}},
                service_id="background_video",
            ),
        ],
    )

    merged = planner._merge_plans(llm_plan, rule_plan, text)
    video, backup = merged.sub_intents

    assert video.service_id == "research_video"
    assert backup.service_id == "backup_data"
    assert backup.intent_type == "RATE_LIMIT"
    assert backup.action["action_type"] == "RATE_LIMIT"
    assert backup.action["qos_params"]["qos_mode"] == "RATE_LIMIT"
    assert backup.match == {
        "destination_port": 5203,
        "protocol": "TCP",
    }


def test_compound_parent_scope_is_inherited_only_by_missing_child_fields():
    planner = IntentPlannerAgent()
    plan = planner._rule_intent_plan(
        "For the backup data service from h_backup to h_research, "
        "guarantee at least 30 Mbps, and limit it to no more than 10 Mbps.",
        None,
        None,
    )
    minimum, maximum = plan.sub_intents

    assert minimum.match["source_ip"] == maximum.match["source_ip"] == "10.0.0.20/32"
    assert minimum.match["destination_ip"] == maximum.match["destination_ip"] == "192.168.10.10/32"
    assert minimum.service_id == maximum.service_id == "backup_data"
    assert maximum.constraints["compound_parent_scope_context"]["provenance"] == "COMPOUND_PARENT_EXPLICIT_SCOPE"
    assert not plan.clarification_questions


def test_english_compound_clauses_do_not_leak_cap_semantics_into_prior_service():
    planner = IntentPlannerAgent()
    text = (
        "Guarantee the research video service from h_research to h_dc has at least 30 Mbps bandwidth, "
        "and cap the backup data service from h_backup to h_research at no more than 20 Mbps."
    )

    rule_plan = planner._rule_intent_plan(text, None, None)

    assert len(rule_plan.sub_intents) == 2
    research, backup = rule_plan.sub_intents
    assert research.action["action_type"] == "SET_PRIORITY"
    assert research.action["qos_params"]["qos_mode"] == "MIN_BANDWIDTH"
    assert research.action["qos_params"]["bandwidth"] == 30
    assert backup.action["action_type"] == "RATE_LIMIT"
    assert backup.action["qos_params"]["qos_mode"] == "RATE_LIMIT"
    assert backup.action["qos_params"]["bandwidth"] == 20


def test_equally_applicable_video_profiles_are_ambiguous():
    research = get_service_profile("research_video")
    second = research.model_copy(
        update={
            "service_id": "research_video_secondary",
            "service_name": "科研视频二路",
            "display_name": "科研视频二路",
        }
    )
    resolution = ServiceSemanticResolver([research, second]).resolve(
        "h_research 到 h_dc 的视频业务",
        source_endpoint="h_research",
        destination_endpoint="h_dc",
    )
    assert resolution.status == ResolutionStatus.AMBIGUOUS
    assert {item.service_id for item in resolution.candidate_services} == {
        "research_video",
        "research_video_secondary",
    }
    assert {item.label for item in resolution.clarification_options} == {
        "科研视频",
        "科研视频二路",
    }


@pytest.mark.asyncio
async def test_missing_destination_pauses_without_plan_or_policy(rule_fallback_llm):
    state = GlobalAgentState(
        intent_id="INT-MISSING-DST",
        user_intent=UserIntent(text="保障 h_research 的视频业务至少10Mbps"),
    )
    node = UnderstandingNode(llm=rule_fallback_llm)
    state = await node.run(state)
    assert state.needs_clarification is True
    assert state.intent_plan.plan_id is None
    assert state.intent_plan.artifact_type == "CANDIDATE_INTERPRETATION"
    assert state.yang_draft is None
    assert state.yang_policies == []
    assert state.flow_table_plan is None
    assert state.clarification_questions
    assert "service_id" not in state.clarification_question
    assert [entry.stage for entry in state.execution_log] == [ExecutionStage.UNDERSTANDING]
    decision = node._build_consensus_decision(
        state, StageStatus.SUCCESS, "clarification required"
    )
    assert decision.status == ConsensusStatus.NEEDS_CLARIFICATION
    assert decision.reason_code == ConsensusReasonCode.SEMANTIC_AMBIGUITY
    assert decision.reason_detail["resume_intent_id"] == state.intent_id


@pytest.mark.asyncio
async def test_named_service_without_endpoints_is_clarification_only(rule_fallback_llm):
    """A service name enriches transport semantics but never selects a path."""
    text = "保障科研视频最低带宽10Mbps"
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(text)
    item = plan.sub_intents[0]

    assert item.service_id == "research_video"
    assert item.match["protocol"] == "UDP"
    assert item.match["destination_port"] == 5004
    assert "source_ip" not in item.match
    assert "destination_ip" not in item.match
    assert plan.plan_id is None
    assert len(plan.clarification_questions) == 2

    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-SERVICE-NO-ENDPOINTS", text=text, legacy_plan=plan
    )
    assert spec.lifecycle.value == "NEEDS_CLARIFICATION"
    assert spec.semantic_scope_complete is False
    assert spec.unresolved_fields == ["source", "destination"]
    assert spec.is_plannable() is False

    state = GlobalAgentState(
        intent_id="INT-SERVICE-NO-ENDPOINTS",
        user_intent=UserIntent(text=text),
    )
    state = await UnderstandingNode(llm=rule_fallback_llm).run(state)
    assert state.needs_clarification is True
    assert state.intent_plan.plan_id is None
    assert state.yang_draft is None
    assert state.yang_policies == []
    assert state.flow_table_plan is None
    assert [entry.stage for entry in state.execution_log] == [ExecutionStage.UNDERSTANDING]


def test_model_proposed_endpoints_are_not_a_substitute_for_user_endpoints():
    planner = IntentPlannerAgent()
    candidate = IntentPlan(
        raw_intent="保障科研视频最低带宽10Mbps",
        sub_intents=[
            SubIntent(
                objective="保障科研视频最低带宽10Mbps",
                intent_type="SET_PRIORITY",
                # Simulate an otherwise plausible LLM completion.  Neither
                # selector is present in the user's text.
                match={
                    "source_ip": "192.168.20.10/32",
                    "destination_ip": "10.0.0.10/32",
                    "protocol": "UDP",
                    "destination_port": 5004,
                },
                action={
                    "action_type": "SET_PRIORITY",
                    "qos_params": {"qos_mode": "MIN_BANDWIDTH", "bandwidth": 10},
                },
                service_id="research_video",
            )
        ],
    )

    guarded = planner._apply_named_service_defaults(
        candidate, candidate.raw_intent
    )
    match = guarded.sub_intents[0].match
    assert "source_ip" not in match
    assert "destination_ip" not in match
    assert len(guarded.clarification_questions) == 2


@pytest.mark.asyncio
async def test_planning_agent_rejects_incomplete_intent():
    spec = IntentSpec(
        intent_id="INT-INCOMPLETE",
        original_text="网络快一点",
        normalized_text="网络快一点",
        clarification_required=True,
        clarification_questions=["请明确业务目标。"],
    )
    with pytest.raises(IntentNotPlannableError):
        await PlanningAgent().plan(spec, NetworkContext())


@pytest.mark.asyncio
async def test_policy_modeling_rejects_candidate_interpretation():
    candidate = IntentPlan(
        plan_id=None,
        artifact_type="CANDIDATE_INTERPRETATION",
        raw_intent="网络快一点",
        clarification_questions=["请明确业务目标。"],
        sub_intents=[SubIntent(objective="网络快一点")],
    )
    with pytest.raises(ValueError, match="INTENT_NOT_PLANNABLE"):
        await PolicyModelingAgent().model(candidate)


@pytest.mark.asyncio
async def test_policy_compiler_rejects_policy_without_formal_plan(rule_fallback_llm):
    from agents.policy_compiler import PolicyCompilationGateError, PolicyCompilerAgent

    policy = YANGPolicy(
        network_policy=NetworkPolicy(
            policy_name="unapproved",
            match=MatchCriteria(
                source_ip="192.168.10.10/32",
                destination_ip="10.0.0.10/32",
            ),
            action=ActionRules(action_type=ActionType.FORWARD),
        )
    )
    with pytest.raises(PolicyCompilationGateError):
        await PolicyCompilerAgent(llm=rule_fallback_llm).compile(
            policy, network_snapshot=NetworkSnapshot()
        )


@pytest.mark.asyncio
async def test_explicit_udp_port_provenance_is_user_explicit(rule_fallback_llm):
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "保障 h_research 到 h_dc 的科研视频 UDP/5004 至少10Mbps"
    )
    assert plan.field_provenance["protocol"] == "USER_EXPLICIT"
    assert plan.field_provenance["destination_port"] == "USER_EXPLICIT"


@pytest.mark.asyncio
async def test_explicit_subnet_is_preserved(rule_fallback_llm):
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "转发 192.168.10.0/24 到整个 10.0.0.0/8 网络"
    )
    assert plan.sub_intents[0].match["destination_ip"] == "10.0.0.0/8"


@pytest.mark.asyncio
async def test_registered_ai_endpoint_can_carry_research_video_when_profile_allows_it(
    rule_fallback_llm,
):
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "保障 h_ai 到 h_dc 的科研视频业务至少10Mbps"
    )
    assert plan.sub_intents[0].service_id == "research_video"
    assert plan.plan_id is not None
    assert not plan.clarification_questions
    # Explicit registered endpoints are the traffic instance.  They must not
    # be replaced with research_video's canonical validation pair.
    assert plan.sub_intents[0].match["source_ip"] == "192.168.20.10/32"
    assert plan.sub_intents[0].match["destination_ip"] == "10.0.0.10/32"
    resolution = plan.sub_intents[0].constraints["service_resolution"]
    assert resolution["status"] == "RESOLVED"

    spec = IntentUnderstandingAgent().understand(
        intent_id="INT-AI-RESEARCH-VIDEO", text=plan.raw_intent, legacy_plan=plan
    )
    assert spec.semantic_scope_complete is True
    assert spec.is_plannable() is True


def test_unregistered_source_never_satisfies_any_registered_host_applicability():
    resolution = ServiceSemanticResolver().resolve(
        "科研视频",
        source_ip="198.51.100.77/32",
        destination_endpoint="h_dc",
        destination_ip="10.0.0.10/32",
    )
    assert resolution.status == ResolutionStatus.NOT_FOUND
    assert resolution.candidate_services[0].service_id == "research_video"
    assert "source" in " ".join(resolution.candidate_services[0].conflicting_fields)


def test_explicit_allow_list_remains_a_configured_applicability_constraint():
    research = get_service_profile("research_video").model_copy(
        update={
            "src_applicability": {
                "mode": "ALLOW_LIST",
                "hosts": ["h_research"],
                "prefixes": ["192.168.10.10/32"],
            }
        }
    )
    resolution = ServiceSemanticResolver([research]).resolve(
        "科研视频",
        source_endpoint="h_ai",
        source_ip="192.168.20.10/32",
        destination_endpoint="h_dc",
        destination_ip="10.0.0.10/32",
    )
    assert resolution.status == ResolutionStatus.NOT_FOUND
    assert resolution.candidate_services[0].conflicting_fields


@pytest.mark.asyncio
async def test_vague_optimization_does_not_invent_service_or_objective(rule_fallback_llm):
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "给科研楼网络优化一下"
    )
    assert plan.plan_id is None
    assert plan.artifact_type == "CANDIDATE_INTERPRETATION"
    assert plan.clarification_questions
    assert all(item.service_id is None for item in plan.sub_intents)


@pytest.mark.asyncio
async def test_formal_intent_spec_contains_resolved_semantics(rule_fallback_llm):
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(SCREENSHOT_SENTENCE)
    result = IntentUnderstandingAgent().understand_result(
        intent_id="INT-REGRESSION",
        text=SCREENSHOT_SENTENCE,
        legacy_plan=plan,
    )
    spec = result.intent_spec
    assert result.status == "RESOLVED"
    assert result.fallback_used is True
    assert spec.source["hostname"] == "h_research"
    assert spec.destination["hostname"] == "h_dc"
    assert spec.service["service_ids"] == ["research_video"]
    assert spec.selector["protocol"] == "UDP"
    assert spec.selector["destination_port"] == 5004
    objective = next(item for item in spec.objectives if item.type.value == "MIN_BANDWIDTH")
    assert objective.operator.value == "GTE"
    assert objective.value == 10
    assert objective.origin.value == "USER_EXPLICIT"
    assert spec.is_plannable()

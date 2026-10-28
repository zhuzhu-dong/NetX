"""Regression coverage for generic business ServiceProfile applicability."""

from __future__ import annotations

import pytest

from agents.intent_planner import IntentPlannerAgent
from agents.intent_understanding import IntentUnderstandingAgent
from agents.semantic_resolver import EndpointResolver, ServiceSemanticResolver
from api.service_profiles import get_service_profile, profile_matches_policy
from models.semantic_resolution import ResolutionStatus
from models.policy_update import TrafficScopeKey
from models.yang_policy import ActionRules, ActionType, MatchCriteria, NetworkPolicy, YANGPolicy


def _resolve(service_text: str, source: str, destination: str):
    endpoints = EndpointResolver()
    src = endpoints.resolve(source)
    dst = endpoints.resolve(destination)
    return ServiceSemanticResolver().resolve(
        service_text,
        source_endpoint=src.endpoint_id,
        destination_endpoint=dst.endpoint_id,
        source_ip=src.selector_prefix,
        destination_ip=dst.selector_prefix,
    )


def test_every_enabled_profile_declares_applicability_explicitly():
    from api.service_profiles import list_service_profiles

    for profile in list_service_profiles():
        assert profile.src_applicability
        assert profile.dst_applicability


@pytest.mark.parametrize(
    ("service_id", "destination"),
    [
        ("research_video", "h_dc"),
        ("ai_data", "h_dc"),
        ("background_video", "h_dc"),
        ("shared_port_video", "h_dc"),
        ("backup_data", "h_research"),
    ],
)
def test_generic_business_profiles_accept_registered_sources(service_id, destination):
    profile = get_service_profile(service_id)
    assert profile.src_applicability["mode"] == "ANY_REGISTERED_HOST"
    resolution = _resolve(profile.display_name or profile.service_name, "h_research", destination)
    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.selected_service_id == service_id


def test_ai_data_accepts_the_dashboard_screenshot_selector():
    resolution = _resolve("AI数据服务", "192.168.10.10/32", "10.0.0.10/32")

    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.selected_service_id == "ai_data"
    assert resolution.inferred_fields["protocol"] == "TCP"
    assert resolution.inferred_fields["destination_port"] == 5202


def test_ai_data_accepts_its_original_and_a_different_registered_source():
    for source in ("h_ai", "h_research"):
        resolution = _resolve("AI数据服务", source, "h_dc")
        assert resolution.status == ResolutionStatus.RESOLVED
        assert resolution.selected_service_id == "ai_data"


@pytest.mark.asyncio
async def test_ai_data_with_explicit_registered_endpoints_is_semantically_complete(rule_fallback_llm):
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "保障 192.168.10.10 到 10.0.0.10 的AI数据服务最低带宽10Mbps"
    )

    assert plan.plan_id is not None
    assert not plan.clarification_questions
    item = plan.sub_intents[0]
    assert item.service_id == "ai_data"
    assert item.match["source_ip"] == "192.168.10.10/32"
    assert item.match["destination_ip"] == "10.0.0.10/32"
    assert item.match["protocol"] == "TCP"
    assert item.match["destination_port"] == 5202

    intent_spec = IntentUnderstandingAgent().understand(
        intent_id="INT-AI-DASHBOARD-SCENARIO",
        text="保障 192.168.10.10 到 10.0.0.10 的AI数据服务最低带宽10Mbps",
        legacy_plan=plan,
    )
    assert intent_spec.semantic_scope_complete is True
    assert intent_spec.is_plannable() is True


@pytest.mark.asyncio
async def test_omitted_endpoints_and_service_remain_clarification_slots(rule_fallback_llm):
    plan = await IntentPlannerAgent(llm=rule_fallback_llm).plan(
        "请保障最低带宽为10Mbps"
    )

    item = plan.sub_intents[0]
    assert item.match.get("source_ip") is None
    assert item.match.get("destination_ip") is None
    assert item.service_id is None
    prompts = "\n".join(plan.clarification_questions)
    assert "源" in prompts
    assert "目的" in prompts
    assert "业务" in prompts or "服务" in prompts


def test_unregistered_source_is_not_widened_by_any_registered_host():
    resolution = ServiceSemanticResolver().resolve(
        "AI数据服务",
        source_ip="198.51.100.77/32",
        destination_endpoint="h_dc",
        destination_ip="10.0.0.10/32",
    )

    assert resolution.status == ResolutionStatus.NOT_FOUND
    assert resolution.candidate_services[0].service_id == "ai_data"
    assert "source" in " ".join(resolution.candidate_services[0].conflicting_fields)


def test_real_destination_allow_list_is_retained_with_specific_mismatch_message():
    resolution = _resolve("AI数据服务", "h_research", "h_backup")

    assert resolution.status == ResolutionStatus.NOT_FOUND
    assert resolution.candidate_services[0].service_id == "ai_data"
    assert "destination" in " ".join(resolution.candidate_services[0].conflicting_fields)
    question = resolution.clarification_question().question
    assert "已识别为AI 数据" in question
    assert "当前源/目的端点" in question
    assert "请说明希望保障的业务类型" not in question


def test_named_service_without_endpoints_still_requires_user_supplied_endpoints():
    resolution = ServiceSemanticResolver().resolve("AI数据服务")

    assert resolution.status == ResolutionStatus.RESOLVED
    assert resolution.selected_service_id == "ai_data"
    # Service semantics may resolve transport defaults but never make a traffic
    # selector executable without user-provided registry endpoints.
    assert resolution.explicit_fields == {}


def test_same_service_different_sources_have_distinct_traffic_scope_keys():
    common = {
        "destination_ip": "10.0.0.10/32",
        "protocol": "TCP",
        "source_port": None,
        "destination_port": 5202,
        "vlan_id": None,
    }
    first = TrafficScopeKey.from_match(
        {**common, "source_ip": "192.168.20.10/32"},
        action_family="QOS",
        service_id="ai_data",
    )
    second = TrafficScopeKey.from_match(
        {**common, "source_ip": "192.168.10.10/32"},
        action_family="QOS",
        service_id="ai_data",
    )

    assert first.key != second.key
    assert first.canonical != second.canonical


def test_generalized_ai_profile_validates_user_selector_without_reusing_demo_source():
    policy = YANGPolicy(
        service_id="ai_data",
        network_policy=NetworkPolicy(
            policy_id="POL-AI-RESEARCH",
            policy_name="ai-data-research-to-dc",
            match=MatchCriteria(
                source_ip="192.168.10.10/32",
                destination_ip="10.0.0.10/32",
                protocol="TCP",
                destination_port=5202,
            ),
            action=ActionRules(
                action_type=ActionType.SET_PRIORITY,
                qos_params={"qos_mode": "MIN_BANDWIDTH", "bandwidth": 10, "dscp_value": 18},
            ),
        ),
    )

    matched, mismatches = profile_matches_policy(get_service_profile("ai_data"), policy)
    assert matched is True
    assert mismatches == []

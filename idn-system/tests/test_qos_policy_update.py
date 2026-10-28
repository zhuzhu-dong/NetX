"""Regression coverage for exact-scope QoS desired-state replacement."""
from __future__ import annotations

import json

import pytest

from agents.p4_rule_verifier import P4RuleVerifierAgent
from agents.policy_conflict_checker import PolicyConflictCheckerAgent
from agents.policy_modeling import PolicyModelingAgent
from agents.risk_classifier import classify_risk
from agents.status_verifier import StatusVerifierAgent
from models.flow_table import FlowTablePlan
from models.intent_plan import IntentPlan, SubIntent
from models.network import DeviceInfo, DeviceStatus, LinkInfo, NetworkSnapshot
from models.policy_update import traffic_scope_key
from models.telemetry import KPIMetrics, TelemetryData
from models.yang_policy import (
    ActionRules,
    ActionType,
    MatchCriteria,
    NetworkPolicy,
    Protocol,
    QoSParams,
    YANGPolicy,
)
from persistence.policy_store import PolicyStore
from api.qos import classify_classifier_overlap
from models.policy_update import qos_change
from orchestrator.nodes.verify_status_node import VerifyStatusNode
from models.agent_state import GlobalAgentState
from models.intent import UserIntent
from orchestrator.graph import route_after_parse_intent


def qos_policy(
    bandwidth: int = 10,
    *,
    policy_id: str = "POL-QOS-10",
    source_ip: str = "192.168.10.0/24",
    destination_port: int = 5004,
    action_type: ActionType = ActionType.SET_PRIORITY,
    dscp: int = 46,
) -> YANGPolicy:
    return YANGPolicy(
        network_policy=NetworkPolicy(
            policy_id=policy_id,
            policy_name=f"video-qos-{bandwidth}",
            match=MatchCriteria(
                source_ip=source_ip,
                destination_ip="10.0.0.0/24",
                protocol=Protocol.UDP,
                destination_port=destination_port,
            ),
            action=ActionRules(
                action_type=action_type,
                qos_params=QoSParams(dscp_value=dscp, bandwidth=bandwidth, burst_size=128),
            ),
        ),
        raw_intent=f"保障 UDP 5004 视频带宽 {bandwidth} Mbps",
        confidence_score=0.95,
    )


def snapshot_with(policy: YANGPolicy) -> NetworkSnapshot:
    return NetworkSnapshot(existing_policies=[policy.network_policy.model_dump(mode="json")])


def create_intent(store, intent_id: str) -> None:
    store.create(
        intent_id=intent_id,
        text=intent_id,
        priority="medium",
        user_id="qos-test",
    )


def test_traffic_scope_key_excludes_qos_values_and_normalizes_prefix():
    first = qos_policy(10, source_ip="192.168.10.7/24")
    second = qos_policy(30, source_ip="192.168.10.0/24")
    different_port = qos_policy(30, destination_port=5005)

    assert traffic_scope_key(first).key == traffic_scope_key(second).key
    assert traffic_scope_key(first).key != traffic_scope_key(different_port).key
    assert traffic_scope_key(first).action_family == "QOS"


def test_same_selector_classifier_identity_ignores_priority_and_generation():
    policy = qos_policy(10, source_ip="10.0.0.20/32", destination_port=5203,
                        action_type=ActionType.RATE_LIMIT, dscp=8)
    flow = {
        "id": "old-100", "deviceId": "device:bmv2:6", "state": "ADDED", "priority": 100,
        "selector": {"criteria": [{"matches": [
            {"field": "hdr.ipv4.srcAddr", "match": "ternary", "value": "0a000014", "mask": "ffffffff"},
            {"field": "hdr.ipv4.dstAddr", "match": "ternary", "value": "0a000000", "mask": "ffffff00"},
            {"field": "hdr.ipv4.protocol", "match": "ternary", "value": "11", "mask": "ff"},
            {"field": "hdr.udp.dstPort", "match": "ternary", "value": "1453", "mask": "ffff"},
        ]}]},
        "treatment": {"instructions": [{"actionId": "MyIngress.set_dscp", "actionParams": {"dscp_value": "08"}}]},
    }
    # Use a TCP policy fixture below for the protocol-specific field.  The
    # canonical identity must not include the legacy flow priority.
    policy.network_policy.match.protocol = Protocol.TCP
    flow["selector"]["criteria"][0]["matches"][2]["value"] = "06"
    flow["selector"]["criteria"][0]["matches"][3]["field"] = "hdr.tcp.dstPort"
    observations = classify_classifier_overlap(policy, "device:bmv2:6", [flow])
    assert observations[0]["classification"] == "EXACT_SAME_SELECTOR"
    assert observations[0]["treatment_equivalent"] is True
    assert observations[0]["priority"] == 100


def test_routing_rule_is_not_misclassified_as_partial_sla_overlap():
    """A route sharing dstAddr has no authority over the SLA table selector."""
    policy = qos_policy(10, source_ip="10.0.0.20/32", destination_port=5203,
                        action_type=ActionType.RATE_LIMIT, dscp=8)
    policy.network_policy.match.protocol = Protocol.TCP
    ipv4_routing_flow = {
        "id": "route-1", "deviceId": "device:bmv2:6", "state": "ADDED", "priority": 10,
        "selector": {"criteria": [{"matches": [
            {"field": "hdr.ipv4.dstAddr", "match": "lpm", "value": "c0a80a00", "prefixLen": 24},
        ]}]},
        "treatment": {"instructions": [{"actionId": "MyIngress.ipv4_forward"}]},
    }

    assert classify_classifier_overlap(policy, "device:bmv2:6", [ipv4_routing_flow]) == []


def test_same_udp_port_with_disjoint_source_hosts_is_not_classifier_overlap():
    """Two services may share UDP/5004 while retaining disjoint /32 sources."""
    policy = qos_policy(10, source_ip="192.168.10.20/32")
    classifier_for_research_host = {
        "id": "research-video", "deviceId": "device:bmv2:6", "state": "ADDED", "priority": 100,
        "selector": {"criteria": [{"matches": [
            {"field": "hdr.ipv4.srcAddr", "match": "ternary", "value": "c0a80a0a", "mask": "ffffffff"},
            {"field": "hdr.ipv4.dstAddr", "match": "ternary", "value": "0a000000", "mask": "ffffff00"},
            {"field": "hdr.ipv4.protocol", "match": "ternary", "value": "11", "mask": "ff"},
            {"field": "hdr.udp.dstPort", "match": "ternary", "value": "138c", "mask": "ffff"},
        ]}]},
        "treatment": {"instructions": [{"actionId": "MyIngress.set_dscp", "actionParams": {"dscp_value": "2e"}}]},
    }

    assert classify_classifier_overlap(policy, "device:bmv2:6", [classifier_for_research_host]) == []


@pytest.mark.asyncio
async def test_exact_same_qos_is_nonblocking_duplicate_reuse():
    old = qos_policy(10, policy_id="POL-OLD")
    report = await PolicyConflictCheckerAgent().check(qos_policy(10), snapshot_with(old))

    assert report.has_conflict is False
    assert report.blocking is False
    assert report.requires_manual_review is False
    assert report.conflict_type == "DUPLICATE_POLICY"
    assert report.resolution == "REUSE_EXISTING_POLICY"
    assert report.execution_mode == "VERIFY_EXISTING"
    assert report.existing_policy_id == "POL-OLD"
    # Exact desired-state reuse never enters positive capacity admission.
    assert report.admission_decision == {}


def test_unset_burst_does_not_turn_same_qos_semantics_into_an_update():
    """A parser omission must reuse a persisted default burst, not rewrite TC."""
    old = qos_policy(10, policy_id="POL-OLD")
    new = qos_policy(10, policy_id="POL-NEW")
    new.network_policy.action.qos_params.burst_size = None

    change = qos_change(old, new)

    assert change["changed_fields"] == []
    assert change["change_direction"] == "UNCHANGED"


@pytest.mark.asyncio
async def test_degraded_committed_scope_remains_visible_for_idempotent_reuse(temp_store):
    """Observed health must not hide a still-installed desired-state owner."""
    create_intent(temp_store, "INT-DEGRADED")
    store = PolicyStore(temp_store)
    installed = qos_policy(20, policy_id="POL-DEGRADED-20")
    store.save(installed, "INT-DEGRADED")
    with temp_store._lock, temp_store._connect() as conn:
        conn.execute(
            "UPDATE policies SET status='DEGRADED' WHERE policy_id=?",
            ("POL-DEGRADED-20",),
        )

    visible = store.list_all()

    assert len(visible) == 1
    assert visible[0]["policy_id"] == "POL-DEGRADED-20"
    assert visible[0]["_store"]["status"] == "DEGRADED"
    report = await PolicyConflictCheckerAgent().check(qos_policy(20), NetworkSnapshot(existing_policies=visible))
    assert report.execution_mode == "VERIFY_EXISTING"
    assert report.existing_policy_id == "POL-DEGRADED-20"


@pytest.mark.asyncio
async def test_exact_bandwidth_upgrade_is_nonblocking_modify():
    report = await PolicyConflictCheckerAgent().check(qos_policy(30), snapshot_with(qos_policy(10, policy_id="POL-OLD")))

    assert report.has_conflict is False
    assert report.conflict_type == "QOS_PARAMETER_CHANGE"
    assert report.resolution == "UPDATE_EXISTING_POLICY"
    assert report.execution_mode == "MODIFY_QOS_RESOURCE"
    assert report.change_direction == "UPGRADE"
    assert (report.old_bandwidth_mbps, report.new_bandwidth_mbps) == (10, 30)
    assert "bandwidth" in report.changed_fields


@pytest.mark.asyncio
async def test_exact_bandwidth_downgrade_is_nonblocking_modify():
    report = await PolicyConflictCheckerAgent().check(qos_policy(10), snapshot_with(qos_policy(30, policy_id="POL-OLD")))
    assert report.has_conflict is False
    assert report.change_direction == "DOWNGRADE"
    assert (report.old_bandwidth_mbps, report.new_bandwidth_mbps) == (30, 10)
    assert report.released_bandwidth_mbps == 20


@pytest.mark.asyncio
async def test_exact_scope_minimum_to_rate_limit_is_nonblocking_modify():
    old = qos_policy(10, policy_id="POL-MODE-STABLE")
    new = qos_policy(20, action_type=ActionType.RATE_LIMIT)

    report = await PolicyConflictCheckerAgent().check(new, snapshot_with(old))

    assert report.has_conflict is False
    assert report.blocking is False
    assert report.conflict_type == "QOS_PARAMETER_CHANGE"
    assert report.execution_mode == "MODIFY_QOS_RESOURCE"
    assert report.existing_policy_id == "POL-MODE-STABLE"
    assert "qos_mode" in report.changed_fields
    assert report.old_qos["qos_mode"] == "MIN_BANDWIDTH"
    assert report.new_qos["qos_mode"] == "RATE_LIMIT"
    assert report.mode_change == "MIN_BANDWIDTH -> RATE_LIMIT"
    assert traffic_scope_key(old).key == traffic_scope_key(new).key


@pytest.mark.asyncio
async def test_exact_scope_rate_limit_to_minimum_is_nonblocking_update():
    old = qos_policy(20, action_type=ActionType.RATE_LIMIT, policy_id="POL-RATE-20")
    new = qos_policy(10, action_type=ActionType.SET_PRIORITY)

    report = await PolicyConflictCheckerAgent().check(new, snapshot_with(old))

    assert report.has_conflict is False
    assert report.blocking is False
    assert report.resolution == "UPDATE_EXISTING_POLICY"
    assert report.execution_mode == "MODIFY_QOS_RESOURCE"
    assert report.mode_change == "RATE_LIMIT -> MIN_BANDWIDTH"


def test_intrinsic_same_scope_qos_interval_conflict_precedes_capacity_admission():
    """An empty objective interval is rejected without looking at resources."""
    minimum = qos_policy(30, policy_id="POL-PLAN-MIN-30", source_ip="10.0.0.20/32",
                         destination_port=5203, dscp=8)
    minimum.network_policy.match.destination_ip = "192.168.10.10/32"
    minimum.network_policy.match.protocol = Protocol.TCP
    limit = qos_policy(10, policy_id="POL-PLAN-LIMIT-10", source_ip="10.0.0.20/32",
                       destination_port=5203, action_type=ActionType.RATE_LIMIT, dscp=8)
    limit.network_policy.match.destination_ip = "192.168.10.10/32"
    limit.network_policy.match.protocol = Protocol.TCP

    conflict = PolicyConflictCheckerAgent().intrinsic_qos_interval_conflict([minimum, limit])

    assert conflict is not None
    assert conflict["conflict_type"] == "INFEASIBLE_INTERVAL"
    assert conflict["feasible_interval"] == "EMPTY"
    assert conflict["minimum_mbps"] == 30
    assert conflict["maximum_mbps"] == 10
    assert conflict["selector"]["source_ip"] == "10.0.0.20/32"


@pytest.mark.asyncio
async def test_conflict_node_rejects_intrinsic_interval_before_resource_admission(monkeypatch):
    """The workflow gate must not call ledger admission for an empty interval."""
    from orchestrator.nodes.check_conflict_node import CheckConflictNode

    minimum = qos_policy(30, policy_id="POL-NODE-MIN-30", source_ip="10.0.0.20/32",
                         destination_port=5203, dscp=8)
    minimum.network_policy.match.destination_ip = "192.168.10.10/32"
    minimum.network_policy.match.protocol = Protocol.TCP
    limit = qos_policy(10, policy_id="POL-NODE-LIMIT-10", source_ip="10.0.0.20/32",
                       destination_port=5203, action_type=ActionType.RATE_LIMIT, dscp=8)
    limit.network_policy.match.destination_ip = "192.168.10.10/32"
    limit.network_policy.match.protocol = Protocol.TCP

    async def resource_admission_must_not_run(*_args, **_kwargs):
        raise AssertionError("resource-dependent admission must not run for an empty QoS interval")

    monkeypatch.setattr(PolicyConflictCheckerAgent, "check", resource_admission_must_not_run)
    state = GlobalAgentState(
        intent_id="INT-NODE-INTRINSIC-CONFLICT",
        user_intent=UserIntent(intent_id="INT-NODE-INTRINSIC-CONFLICT", text="conflicting backup QoS"),
        network_snapshot=NetworkSnapshot(),
        yang_policies=[minimum, limit],
        yang_draft=minimum,
    )

    result = await CheckConflictNode().run(state)

    assert result.conflict_report is not None
    assert result.conflict_report.conflict_type == "INFEASIBLE_INTERVAL"
    assert result.current_status.value == "unsatisfiable"


def test_semantic_only_request_ends_after_real_plan_before_conflict_or_compiler():
    """An explicit API parse-only run retains its plan but cannot reach execution."""
    state = GlobalAgentState(
        intent_id="INT-SEMANTIC-ONLY",
        user_intent=UserIntent(
            intent_id="INT-SEMANTIC-ONLY",
            text="compound intent",
            context={"execution_mode": "SEMANTIC_ONLY"},
        ),
        semantic_only_completed=True,
        execution_status="NOT_RUN_SEMANTIC_ONLY",
        execution_readback_status="NOT_RUN_SEMANTIC_ONLY",
    )

    assert route_after_parse_intent(state) == "__end__"
    assert state.flow_table_plan is None
    assert state.execution_result is None
    assert state.verification_report is None


@pytest.mark.asyncio
async def test_real_capacity_shortage_is_blocking_and_preserves_replacement_credit(monkeypatch):
    monkeypatch.setenv("IDN_DEFAULT_LINK_CAPACITY_MBPS", "100")
    monkeypatch.setenv("IDN_CAPACITY_HEADROOM_MBPS", "1")
    old = qos_policy(10, policy_id="POL-ACTIVE-10")
    snapshot = snapshot_with(old)
    snapshot.devices = [
        DeviceInfo(device_id="device:bmv2:5", status=DeviceStatus.AVAILABLE),
        DeviceInfo(device_id="device:bmv2:3", status=DeviceStatus.AVAILABLE),
    ]
    snapshot.links = [
        LinkInfo(
            link_id="fabric-access-aggregation",
            src_device="device:bmv2:5",
            src_port="3",
            dst_device="device:bmv2:3",
            dst_port="2",
            bandwidth_mbps=None,
        )
    ]

    new = qos_policy(120)
    report = await PolicyConflictCheckerAgent().check(new, snapshot)

    assert report.has_conflict is True
    assert report.blocking is True
    # Admission now uses the authoritative bottleneck resolver before the
    # legacy per-link estimator, so the primary high-severity reason carries
    # the more specific capacity decision.
    assert report.conflict_type == "INSUFFICIENT_CAPACITY"
    assert report.execution_mode == "BLOCK_POLICY"
    assert report.existing_policy_id == "POL-ACTIVE-10"
    assert report.traffic_scope_key == traffic_scope_key(new.network_policy).key
    assert report.old_bandwidth_mbps == 10
    assert report.new_bandwidth_mbps == 120
    item = report.conflict_items[0]
    assert item["requested_bandwidth"] == 120
    # The outer conflict class remains INSUFFICIENT_CAPACITY.  A pristine test
    # store rejects the uncalibrated bottleneck; a store holding a real active
    # calibration record rejects the same 120-Mbps request as insufficient.
    # Both are conservative outcomes and neither fabricates capacity.
    assert item["admission_decision"]["reason_code"] in {
        "STABLE_CAPACITY_EVIDENCE_MISSING", "INSUFFICIENT_CAPACITY",
    }
    # The diagnostic is the legacy topology estimate in a pristine store, or
    # zero when a real calibrated resource has no reservable capacity left.
    # Either way it must remain below the requested 120 Mbps.
    assert item["admission_decision"]["available_mbps"] < 120.0


@pytest.mark.asyncio
async def test_profile_increase_reclaims_active_binding_when_policy_document_omits_bandwidth(monkeypatch):
    """A semantic-only revision must not turn its live 10 Mbps class into a new 30 Mbps request."""
    old = qos_policy(10).network_policy.model_dump(mode="json")
    old["action"]["qos_params"]["bandwidth"] = None
    new = qos_policy(30)
    new.service_id = "research_video"
    snapshot = NetworkSnapshot(existing_policies=[old])
    observed = {}

    monkeypatch.setattr(
        PolicyConflictCheckerAgent,
        "_profile_qos_resource",
        staticmethod(lambda _draft: ("link:a--b", "device:a", "device:b")),
    )
    monkeypatch.setattr(
        PolicyConflictCheckerAgent,
        "_active_binding_reclaim_for_bottleneck",
        staticmethod(lambda *_args: 10.0),
    )

    def admission(**kwargs):
        observed.update(kwargs)
        return {
            "decision": "ADMITTED", "reason_code": "SCHEDULABLE_CAPACITY_AVAILABLE",
            "operation": "INCREASE_UPDATE", "existing_target_mbps": kwargs["reclaim_mbps"],
            "requested_target_mbps": kwargs["requested_mbps"],
            "requested_delta_mbps": kwargs["requested_mbps"] - kwargs["reclaim_mbps"],
        }

    monkeypatch.setattr("agents.policy_conflict_checker.authoritative_admission_decision", admission)
    report = await PolicyConflictCheckerAgent().check(new, snapshot)

    assert observed["purpose"].value == "INCREASE_UPDATE"
    assert observed["reclaim_mbps"] == 10.0
    assert observed["requested_mbps"] == 30.0
    assert observed["requested_mbps"] - observed["reclaim_mbps"] == 20.0
    assert report.has_conflict is False


@pytest.mark.asyncio
async def test_qos_model_persists_default_ef_dscp_instead_of_runtime_only_default():
    modeled = await PolicyModelingAgent().model(IntentPlan(
        raw_intent="Rate limit research_video UDP/5004 to 20 Mbps",
        sub_intents=[SubIntent(
            sequence=1,
            objective="rate limit research video",
            intent_type="RATE_LIMIT",
            match={
                "source_ip": "192.168.10.10/32",
                "destination_ip": "10.0.0.10/32",
                "protocol": "UDP",
                "destination_port": 5004,
            },
            action={
                "action_type": "RATE_LIMIT",
                "qos_params": {"bandwidth": 20, "dscp_value": None},
            },
            constraints={"policy_name": "video-rate-limit"},
            priority=90,
            service_id="research_video",
            validation_profile_id="research_video",
        )],
    ))

    qos = modeled.network_policy.action.qos_params
    assert qos.qos_mode.value == "RATE_LIMIT"
    assert qos.dscp_value == 46


@pytest.mark.asyncio
async def test_partial_overlap_remains_overlap_not_update():
    old = qos_policy(10, source_ip="192.168.10.0/25", policy_id="POL-PARTIAL")
    report = await PolicyConflictCheckerAgent().check(qos_policy(30), snapshot_with(old))

    assert report.execution_mode == "CREATE"
    assert report.conflict_type != "QOS_PARAMETER_CHANGE"
    assert any(
        item.get("conflict_type") in {"RANGE_OVERLAP", "QOS_CONFLICT"}
        for item in report.conflict_items + report.warning_items
    )


@pytest.mark.asyncio
async def test_true_drop_forward_action_conflict_still_blocks():
    forward = qos_policy(10, action_type=ActionType.FORWARD, policy_id="POL-ALLOW")
    forward.network_policy.action.qos_params = None
    drop = qos_policy(10, action_type=ActionType.DROP, policy_id="POL-DROP")
    drop.network_policy.action.qos_params = None

    report = await PolicyConflictCheckerAgent().check(drop, snapshot_with(forward))
    assert report.has_conflict is True
    assert report.blocking is True
    assert report.conflict_type == "ACTION_CONFLICT"


def test_store_commits_one_active_policy_and_version_history(temp_store):
    create_intent(temp_store, "INT-10")
    create_intent(temp_store, "INT-30")
    store = PolicyStore(temp_store)
    old = qos_policy(10, policy_id="POL-STABLE")
    store.save(old, "INT-10")
    scope = traffic_scope_key(old).key

    staged = store.stage(qos_policy(30, policy_id="POL-RANDOM-NEW"), "INT-30", {
        "execution_mode": "MODIFY_QOS_RESOURCE",
        "resolution": "UPDATE_EXISTING_POLICY",
    })
    assert staged["status"] == "UPDATING"
    committed = store.commit_staged(scope, intent_id="INT-30", runtime={"mechanism": "linux_tc_htb_u32"})

    assert store.count() == 1
    assert committed["policy_id"] == "POL-STABLE"
    assert committed["status"] == "ACTIVE"
    assert committed["version"] == 2
    committed_json = json.loads(committed["yang_json"])
    assert committed_json["network_policy"]["policy_id"] == "POL-STABLE"
    assert committed_json["network_policy"]["action"]["qos_params"]["bandwidth"] == 30
    assert [item["event_type"] for item in store.history(scope)][-2:] == [
        "UPDATE_STARTED", "CONFIG_VERIFIED_COMMIT",
    ]


@pytest.mark.parametrize("readback_state", [
    "EXECUTION_READBACK_UNAVAILABLE",
    "EXECUTION_READBACK_MISMATCH",
])
def test_deployed_but_unassured_create_commits_managed_desired_owner(
    temp_store, monkeypatch, readback_state,
):
    """A repairable readback problem cannot orphan a deployed CREATE."""
    import orchestrator.nodes.verify_status_node as verify_module

    create_intent(temp_store, "INT-DEPLOYED-UNASSURED")
    policy = qos_policy(10, policy_id="POL-DEPLOYED-UNASSURED", source_ip="192.168.10.20/32")
    scope = traffic_scope_key(policy).key
    store = PolicyStore(temp_store)
    store.stage(policy, "INT-DEPLOYED-UNASSURED", {"execution_mode": "CREATE"})
    state = GlobalAgentState(
        intent_id="INT-DEPLOYED-UNASSURED",
        user_intent=UserIntent(intent_id="INT-DEPLOYED-UNASSURED", text="shared video"),
        yang_draft=policy,
        execution_result={
            "status": "SUCCESS",
            "traffic_scope_key": scope,
            "qos_enforcement": {"config_applied": True},
        },
        verification_report={"verification_state": readback_state},
    )
    monkeypatch.setattr(verify_module, "PolicyStore", lambda: store, raising=False)
    # _commit_policy imports PolicyStore locally, so patch its module symbol.
    import persistence.policy_store as store_module
    monkeypatch.setattr(store_module, "PolicyStore", lambda *args, **kwargs: store)
    VerifyStatusNode._commit_policy(state)

    committed = store.get_by_scope(scope)
    assert committed["status"] == "ACTIVE"
    assert committed["pending_yang_json"] is None
    assert committed["pending_intent_id"] is None


def test_durable_artifact_recovery_creates_managed_owner_without_observed_input(temp_store):
    """Recovery needs pre-write desired YANG; runtime does not define selector semantics."""
    create_intent(temp_store, "INT-DURABLE-RECOVERY")
    policy = qos_policy(10, policy_id="POL-DURABLE-RECOVERY", source_ip="192.168.10.20/32", dscp=0)
    committed = PolicyStore(temp_store).recover_managed_desired_from_artifact(
        policy,
        intent_id="INT-DURABLE-RECOVERY",
        execution_runtime={"installed_flows": [{"device_id": "device:bmv2:5", "flow_id": "evidence-only"}]},
    )
    assert committed["status"] == "ACTIVE"
    restored = json.loads(committed["yang_json"])
    assert restored["network_policy"]["match"]["source_ip"] == "192.168.10.20/32"
    assert restored["network_policy"]["action"]["qos_params"]["dscp_value"] == 0


def test_updating_scope_reuse_refreshes_pending_intent_lineage(temp_store):
    for intent_id in ("INT-OLD", "INT-INTERRUPTED", "INT-CURRENT"):
        create_intent(temp_store, intent_id)
    store = PolicyStore(temp_store)
    old = qos_policy(10, policy_id="POL-STABLE-LINEAGE")
    scope = traffic_scope_key(old).key
    store.stage(old, "INT-OLD", {"execution_mode": "CREATE"})
    # Simulate an interrupted first generation: it has a live classifier but
    # remains UPDATING until a later execution obtains fresh readback.
    store.stage(qos_policy(10, policy_id="POL-INTERRUPTED"), "INT-INTERRUPTED", {
        "execution_mode": "CREATE",
    })
    reused = store.stage(qos_policy(10, policy_id="POL-CURRENT"), "INT-CURRENT", {
        "execution_mode": "VERIFY_EXISTING",
    })

    assert reused["status"] == "UPDATING"
    assert reused["pending_intent_id"] == "INT-CURRENT"
    committed = store.commit_staged(scope, intent_id="INT-CURRENT", runtime={"installed_flows": []})
    assert committed["intent_id"] == "INT-CURRENT"


def test_store_rollback_restores_old_active_policy(temp_store):
    create_intent(temp_store, "INT-OLD")
    create_intent(temp_store, "INT-FAIL")
    store = PolicyStore(temp_store)
    old = qos_policy(10, policy_id="POL-ROLLBACK")
    store.save(old, "INT-OLD")
    scope = traffic_scope_key(old).key
    store.stage(qos_policy(30), "INT-FAIL", {"execution_mode": "MODIFY_QOS_RESOURCE"})

    restored = store.rollback_staged(scope, error="injected runtime failure")
    payload = json.loads(restored["yang_json"])
    assert restored["status"] == "ACTIVE"
    assert restored["version"] == 1
    assert payload["network_policy"]["action"]["qos_params"]["bandwidth"] == 10
    assert store.history(scope)[-1]["event_type"] == "UPDATE_ROLLED_BACK"


def test_terminal_workflow_pending_update_is_rolled_back_without_waiting_timeout(temp_store):
    create_intent(temp_store, "INT-OLD")
    create_intent(temp_store, "INT-TERMINAL")
    store = PolicyStore(temp_store)
    old = qos_policy(10, policy_id="POL-TERMINAL-ROLLBACK")
    store.save(old, "INT-OLD")
    scope = traffic_scope_key(old).key
    store.stage(qos_policy(30), "INT-TERMINAL", {"execution_mode": "MODIFY_QOS_RESOURCE"})
    temp_store.update("INT-TERMINAL", status="success")

    recovered = store.recover_stale_pending(timeout_seconds=3600)

    assert recovered[0]["terminal_workflow"] is True
    restored = store.get_by_scope(scope)
    assert restored["pending_yang_json"] is None
    assert restored["status"] == "ACTIVE"
    assert json.loads(restored["yang_json"])["network_policy"]["action"]["qos_params"]["bandwidth"] == 10


def test_exact_qos_modify_is_low_risk_and_never_forces_manual_review():
    plan = FlowTablePlan(
        plan_id="PLAN-MODIFY",
        intent_id="INT-MODIFY",
        policy_operation="MODIFY_QOS_RESOURCE",
    )
    assert classify_risk(
        yang_policy=qos_policy(30),
        flow_table_plan=plan,
        network_snapshot=NetworkSnapshot(),
    ) == "LOW"


@pytest.mark.asyncio
async def test_empty_p4_plan_is_valid_for_existing_resource_modify():
    plan = FlowTablePlan(
        plan_id="PLAN-TC",
        intent_id="INT-TC",
        policy_operation="MODIFY_QOS_RESOURCE",
    )
    report = await P4RuleVerifierAgent().verify(
        plan,
        {"tables": [], "actions": [], "meters": [], "counters": []},
        approval_token="approved",
        require_approval_token=True,
    )
    assert report.is_valid is True
    assert report.checked_items == 0


@pytest.mark.asyncio
async def test_qos_adapter_requires_actual_config_readback(monkeypatch):
    class FakeProbe:
        async def post(self, path, json_data=None):
            assert path == "/policy/qos"
            assert json_data["bandwidth_mbps"] == 30
            assert json_data["destination_port"] == 5004
            return {
                "status": "ok",
                "config_applied": True,
                "mechanism": "linux_tc_htb_u32",
                "resources": [{"class_id": "1:123", "actual_bandwidth_mbps": 30}],
            }

    async def fake_session():
        return FakeProbe()

    import api._probe_session
    from api.qos import apply_qos_policy

    monkeypatch.setattr(api._probe_session, "get_probe_session", fake_session)
    result = await apply_qos_policy(qos_policy(30))
    assert result["config_applied"] is True
    assert result["enforcement_path"] == "onos_p4_classifier+switch_egress_htb"


@pytest.mark.asyncio
async def test_config_applied_without_traffic_is_not_kpi_verified(monkeypatch):
    async def fake_verify(_policy, _execution):
        return {"status": "ok", "config_applied": True}

    async def fake_telemetry(self, intent_id, sample_count):
        return (
            TelemetryData(intent_id=intent_id, sample_count=sample_count, kpi=KPIMetrics()),
            "unit-test",
        )

    async def no_policy_traffic(self, policy, **kwargs):
        return None

    import api.qos

    monkeypatch.setattr(api.qos, "verify_execution_plane", fake_verify)
    monkeypatch.setattr(StatusVerifierAgent, "_collect_live_telemetry", fake_telemetry)
    monkeypatch.setattr(StatusVerifierAgent, "_collect_policy_throughput", no_policy_traffic)
    exact_policy = qos_policy(10, source_ip="192.168.10.10/32")
    exact_policy.network_policy.match.destination_ip = "10.0.0.10/32"
    exact_policy.service_id = "research_video"
    exact_policy.validation_profile_id = "research_video"
    result = await StatusVerifierAgent().verify(
        "INT-NO-TRAFFIC",
        exact_policy,
        {"status": "SUCCESS"},
        wait_secs=0,
    )
    assert result.passed is False
    assert result.verification_state == "NO_SAMPLE"
    assert result.intent_achieved is False


def test_new_policy_create_commits_active_bandwidth_10(temp_store):
    create_intent(temp_store, "INT-CREATE-10")
    store = PolicyStore(temp_store)
    created = store.save(qos_policy(10, policy_id="POL-CREATE-10"), "INT-CREATE-10")

    assert created["status"] == "ACTIVE"
    assert created["version"] == 1
    assert store.count() == 1
    assert json.loads(created["yang_json"])["network_policy"]["action"]["qos_params"]["bandwidth"] == 10


def test_continuous_updates_leave_one_active_policy_and_complete_audit(temp_store):
    for intent_id in ("INT-CONT-10A", "INT-CONT-30", "INT-CONT-20", "INT-CONT-10B"):
        create_intent(temp_store, intent_id)
    store = PolicyStore(temp_store)
    initial = qos_policy(10, policy_id="POL-CONTINUOUS")
    store.save(initial, "INT-CONT-10A")
    scope = traffic_scope_key(initial).key

    for intent_id, bandwidth in (("INT-CONT-30", 30), ("INT-CONT-20", 20), ("INT-CONT-10B", 10)):
        store.stage(qos_policy(bandwidth), intent_id, {"execution_mode": "MODIFY_QOS_RESOURCE"})
        store.commit_staged(scope, intent_id=intent_id, runtime={"actual_bandwidth_mbps": bandwidth})

    active = store.get_by_scope(scope)
    history = store.history(scope)
    assert store.count() == 1
    assert active["policy_id"] == "POL-CONTINUOUS"
    assert active["version"] == 4
    assert json.loads(active["yang_json"])["network_policy"]["action"]["qos_params"]["bandwidth"] == 10
    assert [item["event_type"] for item in history].count("UPDATE_STARTED") == 3
    assert [item["event_type"] for item in history].count("CONFIG_VERIFIED_COMMIT") == 4


def test_preference_withdrawal_restores_predecessor_policy_owner_without_new_resource(temp_store):
    """A preference update may not leave its withdrawn parent as a live owner."""
    for intent_id in ("INT-BASELINE", "INT-PREFERENCE"):
        create_intent(temp_store, intent_id)
    store = PolicyStore(temp_store)
    original = qos_policy(10, policy_id="POL-PREFERENCE-OWNER")
    store.save(original, "INT-BASELINE")
    scope = traffic_scope_key(original).key
    store.stage(qos_policy(10, policy_id="POL-DRAFT"), "INT-PREFERENCE", {
        "execution_mode": "MODIFY_QOS_RESOURCE",
    })
    store.commit_staged(scope, intent_id="INT-PREFERENCE", runtime={})

    candidates = store.preference_owner_candidates("INT-PREFERENCE")

    assert candidates == [{
        "policy_id": "POL-PREFERENCE-OWNER", "traffic_scope_key": scope,
        "current_owner_intent_id": "INT-PREFERENCE", "current_version": 2,
        "predecessor_intent_id": "INT-BASELINE", "predecessor_version": 1,
        "predecessor_history_id": candidates[0]["predecessor_history_id"],
    }]
    restored = store.restore_preference_ownership(
        "INT-PREFERENCE", expected_owners={"POL-PREFERENCE-OWNER": "INT-BASELINE"},
    )

    assert restored[0]["from_intent_id"] == "INT-PREFERENCE"
    assert restored[0]["to_intent_id"] == "INT-BASELINE"
    assert store.get_by_scope(scope)["intent_id"] == "INT-BASELINE"
    assert store.history(scope)[-1]["event_type"] == "PREFERENCE_OWNER_RESTORED"


def test_preference_only_update_cannot_clear_committed_bandwidth_target(temp_store):
    """A missing preference bandwidth is not an instruction to rewrite TC to an unknown target."""
    for intent_id in ("INT-BASELINE", "INT-PREFERENCE"):
        create_intent(temp_store, intent_id)
    store = PolicyStore(temp_store)
    original = qos_policy(10, policy_id="POL-PREFERENCE-QOS")
    store.save(original, "INT-BASELINE")
    scope = traffic_scope_key(original).key
    preference_only = qos_policy(10, policy_id="POL-PREFERENCE-DRAFT")
    preference_only.network_policy.action.qos_params.bandwidth = None
    preference_only.network_policy.action.qos_params.burst_size = None
    store.stage(preference_only, "INT-PREFERENCE", {"execution_mode": "MODIFY_QOS_RESOURCE"})
    committed = store.commit_staged(scope, intent_id="INT-PREFERENCE", runtime={})

    committed_qos = json.loads(committed["yang_json"])["network_policy"]["action"]["qos_params"]
    assert committed_qos["bandwidth"] == 10
    assert committed_qos["burst_size"] == 128


@pytest.mark.asyncio
async def test_idempotent_reuse_does_not_bypass_service_evidence(monkeypatch):
    async def fake_verify(_policy, _execution):
        return {"status": "ok", "config_applied": True}

    async def fake_telemetry(self, intent_id, sample_count):
        return (
            TelemetryData(
                intent_id=intent_id,
                sample_count=sample_count,
                kpi=KPIMetrics(throughput_mbps=6.3),
            ),
            "unit-test",
        )

    async def varied_probe(self, policy, **kwargs):
        return {"status": "ok", "throughput_mbps": 6.3, "traffic_generated": True}

    import api.qos

    monkeypatch.setattr(api.qos, "verify_execution_plane", fake_verify)
    monkeypatch.setattr(StatusVerifierAgent, "_collect_live_telemetry", fake_telemetry)
    monkeypatch.setattr(StatusVerifierAgent, "_collect_policy_throughput", varied_probe)
    result = await StatusVerifierAgent().verify(
        "INT-REUSE",
        qos_policy(10),
        {"status": "SUCCESS", "operation": "VERIFY_EXISTING"},
        wait_secs=0,
    )
    assert result.passed is False
    assert result.intent_achieved is False


@pytest.mark.asyncio
async def test_repeated_noop_still_enters_configuration_and_service_assurance(monkeypatch):
    async def fake_verify(_policy, _execution):
        return {"status": "ok", "config_applied": True}

    async def fake_telemetry(self, intent_id, sample_count):
        return (
            TelemetryData(
                intent_id=intent_id,
                sample_count=sample_count,
                kpi=KPIMetrics(throughput_mbps=6.3),
            ),
            "unit-test",
        )

    async def varied_probe(self, policy, **kwargs):
        return {"status": "ok", "throughput_mbps": 6.3, "traffic_generated": True}

    import api.qos

    monkeypatch.setattr(api.qos, "verify_execution_plane", fake_verify)
    monkeypatch.setattr(StatusVerifierAgent, "_collect_live_telemetry", fake_telemetry)
    monkeypatch.setattr(StatusVerifierAgent, "_collect_policy_throughput", varied_probe)

    result = await StatusVerifierAgent().verify(
        "INT-NOOP-ASSURANCE",
        qos_policy(10),
        {
            "status": "SKIPPED",
            "executor_status": "SKIPPED_NOOP",
            "reason_code": "NO_OP_PLAN",
            "no_change": True,
        },
        wait_secs=0,
    )

    assert result.write_success is True
    assert result.verification_state != "CONFIG_FAILED"

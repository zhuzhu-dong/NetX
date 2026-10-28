"""PolicyCompiler and P4RuleVerifier unit tests — flow table compilation,
P4Info alignment, RATE_LIMIT correction, react retry, and rule verification.

All tests use rule-fallback LLM or decorated LLM mocks.
"""

from __future__ import annotations

import json

import pytest

from agents.p4_rule_verifier import P4RuleVerifierAgent
from agents.policy_compiler import PolicyCompilerAgent
from agents.policy_modeling import PolicyModelingAgent
from agents.status_verifier import StatusVerifierAgent
from models.agent_state import ConflictReport, GlobalAgentState
from models.intent import UserIntent
from models.intent_plan import IntentPlan, SubIntent
from models.network import NetworkSnapshot
from orchestrator.nodes import ExecuteFlowNode, VerifyRulePlanNode


def _two_policy_intent_plan(raw_intent: str = "two policies") -> IntentPlan:
    return IntentPlan(
        raw_intent=raw_intent, confidence_score=0.95,
        sub_intents=[
            SubIntent(sequence=1, objective="Video QoS site A", intent_type="SET_PRIORITY",
                      match={"source_ip": "192.168.10.0/24", "destination_ip": "10.0.0.0/8",
                             "destination_port": 5004, "protocol": "UDP"},
                      action={"action_type": "SET_PRIORITY",
                              "qos_params": {"dscp_value": 46, "bandwidth": 100, "burst_size": 1024}},
                      constraints={"policy_name": "site-a-video-qos"}, priority=100),
            SubIntent(sequence=2, objective="Video QoS site B", intent_type="SET_PRIORITY",
                      match={"source_ip": "192.168.20.0/24", "destination_ip": "10.0.0.0/8",
                             "destination_port": 5004, "protocol": "UDP"},
                      action={"action_type": "SET_PRIORITY",
                              "qos_params": {"dscp_value": 46, "bandwidth": 100, "burst_size": 1024}},
                      constraints={"policy_name": "site-b-video-qos"}, priority=100),
        ],
    )


# ── Compilation ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compile_basic_forward(rule_fallback_llm):
    """Standard intent → valid FlowTablePlan via rule engine."""
    intent_plan = IntentPlan(
        raw_intent="Forward traffic from 192.168.10.0/24 to 10.0.0.0/8.",
        sub_intents=[SubIntent(
            sequence=1, objective="Forward traffic", intent_type="FORWARD",
            match={"source_ip": "192.168.10.0/24", "destination_ip": "10.0.0.0/8", "protocol": "ANY"},
            action={"action_type": "FORWARD"}, constraints={"policy_name": "forward-policy"}, priority=100,
        )],
    )
    yang_policy = await PolicyModelingAgent().model(intent_plan)
    flow_plan = await PolicyCompilerAgent(llm=rule_fallback_llm).compile(
        yang_policy, network_snapshot=NetworkSnapshot())

    assert flow_plan.total_entries >= 1
    assert flow_plan.write_requests[0].table_entries[0].table_name == "MyIngress.ipv4_routing"


@pytest.mark.asyncio
async def test_compile_preserves_explicit_zero_dscp(rule_fallback_llm):
    """DSCP 0 is an explicit service-profile value, not an absent default."""
    intent_plan = IntentPlan(
        raw_intent="Guarantee shared video.",
        sub_intents=[SubIntent(
            sequence=1, objective="Shared UDP video", intent_type="SET_PRIORITY",
            match={"source_ip": "192.168.10.20/32", "destination_ip": "10.0.0.10/32",
                   "destination_port": 5004, "protocol": "UDP"},
            action={"action_type": "SET_PRIORITY",
                    "qos_params": {"dscp_value": 0, "bandwidth": 10, "burst_size": 1024}},
            constraints={"policy_name": "shared-video-qos"}, priority=100,
        )],
    )
    policy = await PolicyModelingAgent().model(intent_plan)
    plan = await PolicyCompilerAgent(llm=rule_fallback_llm).compile(
        policy, network_snapshot=NetworkSnapshot(),
    )
    assert plan.write_requests[0].table_entries[0].action_params[0].value == 0


@pytest.mark.asyncio
async def test_react_shadowed_rule_increments_priority(rule_fallback_llm):
    """SHADOWED_RULE violation → retry with priority+1."""
    intent_plan = IntentPlan(
        raw_intent="Guarantee video traffic.",
        sub_intents=[SubIntent(
            sequence=1, objective="Video QoS", intent_type="SET_PRIORITY",
            match={"source_ip": "192.168.10.0/24", "destination_ip": "10.0.0.0/8",
                   "destination_port": 5004, "protocol": "UDP"},
            action={"action_type": "SET_PRIORITY",
                    "qos_params": {"dscp_value": 46, "bandwidth": 100, "burst_size": 1024}},
            constraints={"policy_name": "video-qos"}, priority=100,
        )],
    )
    yang_policy = await PolicyModelingAgent().model(intent_plan)
    plan = await PolicyCompilerAgent(llm=rule_fallback_llm).compile(
        yang_policy, network_snapshot=NetworkSnapshot(),
        react_context={
            "retry_count": 1, "last_failure_stage": "p4_rule_verifier",
            "rule_verification_report": {
                "violation_items": [{"violation_type": "SHADOWED_RULE", "existing_priority": 200}],
            },
        },
    )
    entry = plan.write_requests[0].table_entries[0]
    assert entry.priority == 201
    assert plan.react_adjustments


@pytest.mark.asyncio
async def test_llm_json_extraction(decorated_flow_plan_llm):
    """Decorated LLM output with `<think>` tags → valid FlowTablePlan."""
    intent_plan = IntentPlan(
        raw_intent="Guarantee video traffic.",
        sub_intents=[SubIntent(
            sequence=1, objective="Video QoS", intent_type="SET_PRIORITY",
            match={"source_ip": "192.168.10.0/24", "destination_ip": "10.0.0.0/8", "protocol": "UDP", "destination_port": 5004},
            action={"action_type": "SET_PRIORITY", "qos_params": {"bandwidth": 10, "dscp_value": 46}}, constraints={"policy_name": "video"}, priority=100,
        )],
    )
    yang_policy = await PolicyModelingAgent().model(intent_plan)
    plan = await PolicyCompilerAgent(llm=decorated_flow_plan_llm).compile(
        yang_policy, network_snapshot=NetworkSnapshot())
    entry = plan.write_requests[0].table_entries[0]
    assert entry.priority == 101
    assert entry.action_params[0].value == 46


@pytest.mark.asyncio
async def test_lpm_to_ternary_p4info_alignment(decorated_flow_plan_llm):
    """P4Info declares TERNARY → match type aligned with mask."""
    intent_plan = IntentPlan(
        raw_intent="Guarantee video traffic.",
        sub_intents=[SubIntent(
            sequence=1, objective="Video QoS", intent_type="SET_PRIORITY",
            match={"source_ip": "192.168.10.0/24", "destination_ip": "10.0.0.0/8", "protocol": "UDP", "destination_port": 5004},
            action={"action_type": "SET_PRIORITY", "qos_params": {"bandwidth": 10, "dscp_value": 46}}, constraints={"policy_name": "video"}, priority=100,
        )],
    )
    yang_policy = await PolicyModelingAgent().model(intent_plan)
    p4info = {
        "tables": [{
            "name": "MyIngress.sla_enforcement",
            "match_fields": [
                {"name": "hdr.ipv4.srcAddr", "match_type": "TERNARY", "bitwidth": 32},
                {"name": "hdr.ipv4.dstAddr", "match_type": "TERNARY", "bitwidth": 32},
                {"name": "hdr.ipv4.protocol", "match_type": "TERNARY", "bitwidth": 8},
                {"name": "hdr.udp.dstPort", "match_type": "TERNARY", "bitwidth": 16},
            ],
            "action_refs": ["MyIngress.set_dscp"], "size": 1024,
        }],
        "actions": [{"name": "MyIngress.set_dscp", "params": [
            {"name": "dscp_value", "bitwidth": 6}]}],
    }
    plan = await PolicyCompilerAgent(llm=decorated_flow_plan_llm).compile(
        yang_policy, NetworkSnapshot(), p4info=p4info)
    entry = plan.write_requests[0].table_entries[0]
    fields = {f.field_name: f for f in entry.match_fields}
    assert fields["hdr.ipv4.dstAddr"].match_type == "ternary"
    assert fields["hdr.ipv4.dstAddr"].mask == 0xFF000000


@pytest.mark.asyncio
async def test_rate_limit_corrects_dscp_to_sla_enforcement(rule_fallback_llm):
    """RATE_LIMIT with DSCP-only plan → redirected to sla_enforcement."""
    intent_plan = IntentPlan(
        raw_intent="Rate-limit traffic.",
        sub_intents=[SubIntent(
            sequence=1, objective="Rate limit", intent_type="RATE_LIMIT",
            match={"source_ip": "192.168.10.0/24", "destination_ip": "10.0.0.0/8",
                   "protocol": "ANY"},
            action={"action_type": "RATE_LIMIT",
                    "qos_params": {"bandwidth": 20, "burst_size": 1024}},
            constraints={"policy_name": "rate-limit-20mbps"}, priority=100,
        )],
    )
    yang_policy = await PolicyModelingAgent().model(intent_plan)
    compiler = PolicyCompilerAgent(llm=rule_fallback_llm)
    plan = await compiler.compile(yang_policy, network_snapshot=NetworkSnapshot())
    entry = plan.write_requests[0].table_entries[0]
    assert entry.table_name == "MyIngress.sla_enforcement"
    assert entry.action_name == "MyIngress.set_dscp"
    assert {field.field_name for field in entry.match_fields} == {
        "hdr.ipv4.srcAddr",
        "hdr.ipv4.dstAddr",
        "hdr.ipv4.protocol",
        "hdr.udp.dstPort",
        "hdr.tcp.dstPort",
    }


@pytest.mark.asyncio
async def test_mock_llm_unknown_selector_falls_back_to_p4info_fields(monkeypatch):
    """An LLM-only semantic field must never reach P4 verification/deploy."""
    monkeypatch.setenv("IDN_MOCK_MODE", "true")
    intent_plan = IntentPlan(
        raw_intent="Guarantee research video UDP/5004 at 10 Mbps.",
        sub_intents=[SubIntent(
            sequence=1,
            objective="Research video bandwidth guarantee",
            intent_type="SET_PRIORITY",
            match={
                "source_ip": "192.168.10.10/32",
                "destination_ip": "10.0.0.10/32",
                "protocol": "UDP",
                "destination_port": 5004,
            },
            action={
                "action_type": "SET_PRIORITY",
                "qos_params": {
                    "qos_mode": "MIN_BANDWIDTH",
                    "bandwidth": 10,
                    "dscp_value": 46,
                },
            },
            constraints={"policy_name": "research-video-min-bandwidth"},
            priority=100,
        )],
    )
    yang_policy = await PolicyModelingAgent().model(intent_plan)
    compiler = PolicyCompilerAgent()
    plan = await compiler.compile(
        yang_policy,
        network_snapshot=NetworkSnapshot(),
        p4info=compiler._demo_p4info(),
    )

    entry = plan.write_requests[0].table_entries[0]
    assert plan.ai_degraded is True
    assert "hdr.intent.intent_type" not in {
        field.field_name for field in entry.match_fields
    }
    assert entry.table_name == "MyIngress.sla_enforcement"


@pytest.mark.asyncio
async def test_tcp_qos_uses_tcp_port_and_wildcards_udp(rule_fallback_llm):
    intent_plan = IntentPlan(
        raw_intent="Guarantee AI TCP data.",
        sub_intents=[SubIntent(
            sequence=1, objective="AI QoS", intent_type="SET_PRIORITY",
            match={"source_ip": "192.168.20.10/32", "destination_ip": "10.0.0.10/32",
                   "destination_port": 5202, "protocol": "TCP"},
            action={"action_type": "SET_PRIORITY",
                    "qos_params": {"dscp_value": 18, "bandwidth": 20, "burst_size": 1024}},
            constraints={"policy_name": "ai-data-qos"}, priority=100,
            service_id="ai_data",
        )],
    )
    yang_policy = await PolicyModelingAgent().model(intent_plan)
    plan = await PolicyCompilerAgent(llm=rule_fallback_llm).compile(
        yang_policy, network_snapshot=NetworkSnapshot())
    fields = {
        item.field_name: item for item in plan.write_requests[0].table_entries[0].match_fields
    }
    assert fields["hdr.tcp.dstPort"].value == "1452"
    assert fields["hdr.tcp.dstPort"].mask == "ffff"
    assert fields["hdr.udp.dstPort"].mask == "0000"


# ── P4RuleVerifier ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verifier_rejects_invalid_meter(rule_fallback_llm):
    """Meter ID exceeding p4info capacity → at least one violation."""
    intent_plan = IntentPlan(
        raw_intent="Rate-limit traffic.",
        sub_intents=[SubIntent(
            sequence=1, objective="Rate limit", intent_type="RATE_LIMIT",
            match={"source_ip": "192.168.10.0/24", "destination_ip": "10.0.0.0/8",
                   "protocol": "ANY"},
            action={"action_type": "RATE_LIMIT",
                    "qos_params": {"bandwidth": 20, "burst_size": 1024}},
            constraints={"policy_name": "rate-limit-test"}, priority=100,
        )],
    )
    yang_policy = await PolicyModelingAgent().model(intent_plan)
    compiler = PolicyCompilerAgent(llm=rule_fallback_llm)
    plan = await compiler.compile(yang_policy, network_snapshot=NetworkSnapshot(),
                                   p4info=compiler._demo_p4info())
    # Force an invalid meter_id beyond p4info meter capacity
    plan.write_requests[0].table_entries[0].meter_id = 5000
    p4info = compiler._demo_p4info()
    p4info["meters"] = [{"name": "MyIngress.host_meter", "size": 4096, "unit": "PACKETS"}]

    report = await P4RuleVerifierAgent().verify(plan, p4info, approval_token="approved")
    assert report.is_valid is False


# ── ExecuteFlowNode ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_flow_node_mock_delivery(rule_fallback_llm, monkeypatch):
    """ExecuteFlowNode writes via mock delivery and returns SUCCESS."""
    class FakeONOSClient:
        async def post_flows(self, device_id, flow_rules):
            return {
                "flows_posted": len(flow_rules),
                "total": len(flow_rules),
                "flow_ids": [f"mock-{index}" for index, _ in enumerate(flow_rules, 1)],
                "errors": None,
            }

        async def delete_flows(self, device_id, flow_ids):
            return {"flows_deleted": len(flow_ids), "errors": None}

    class FakeDelivery:
        onos = FakeONOSClient()

    from mcp_servers.flow_delivery import FlowDeliveryFactory
    monkeypatch.setattr(FlowDeliveryFactory, "create", staticmethod(lambda: FakeDelivery()))

    intent_plan = IntentPlan(
        raw_intent="Forward traffic.",
        sub_intents=[SubIntent(
            sequence=1, objective="Forward", intent_type="FORWARD",
            match={"source_ip": "192.168.10.0/24", "destination_ip": "10.0.0.0/8", "protocol": "ANY"},
            action={"action_type": "FORWARD"}, constraints={"policy_name": "fwd"}, priority=100,
        )],
    )
    yang_policy = await PolicyModelingAgent().model(intent_plan)
    flow_plan = await PolicyCompilerAgent(llm=rule_fallback_llm).compile(
        yang_policy, network_snapshot=NetworkSnapshot())
    state = GlobalAgentState(
        intent_id="INT-EXECUTE",
        user_intent=UserIntent(text="Forward traffic."),
        yang_policies=[yang_policy], yang_draft=yang_policy,
        verified_flow_table_plan=flow_plan.model_dump(mode="json"),
        policy_results=[{}],
    )
    state = await ExecuteFlowNode().run(state)
    assert state.execution_result["status"] == "SUCCESS"
    assert state.execution_result["total_entries_written"] == flow_plan.total_entries


@pytest.mark.asyncio
async def test_verify_existing_repairs_missing_tc_through_fenced_executor(rule_fallback_llm, monkeypatch):
    """A surviving classifier cannot make a missing egress scheduler reusable."""
    class FakePolicyStore:
        def stage(self, *_args, **_kwargs):
            return {"traffic_scope_key": "scope-video", "version": 0}

    async def no_pre_telemetry(self, _state):
        return {}

    calls = []

    async def qos_readback_then_repair(self, _state, *, operation):
        calls.append(operation)
        return {
            "status": "ok",
            "config_applied": operation == "APPLY",
            "qos_binding": {"interface": "s_agg_r-eth2"},
        }

    import persistence.policy_store

    monkeypatch.setattr(persistence.policy_store, "PolicyStore", FakePolicyStore)
    monkeypatch.setattr(ExecuteFlowNode, "_collect_pre_execution_telemetry", no_pre_telemetry)
    monkeypatch.setattr(ExecuteFlowNode, "_apply_qos_with_execution_lease", qos_readback_then_repair)

    plan = IntentPlan(
        raw_intent="guarantee a video service",
        sub_intents=[SubIntent(
            sequence=1, objective="Video QoS", intent_type="SET_PRIORITY",
            match={"source_ip": "192.168.10.10/32", "destination_ip": "10.0.0.10/32",
                   "destination_port": 5004, "protocol": "UDP"},
            action={"action_type": "SET_PRIORITY",
                    "qos_params": {"dscp_value": 46, "bandwidth": 30, "burst_size": 1024}},
            constraints={"policy_name": "video-qos"}, priority=100,
        )],
    )
    policy = await PolicyModelingAgent().model(plan)
    state = GlobalAgentState(
        intent_id="INT-REPAIR-TC",
        user_intent=UserIntent(text="repair missing video scheduler"),
        yang_policies=[policy], yang_draft=policy,
        verified_flow_table_plan={
            "policy_operation": "VERIFY_EXISTING",
            "traffic_scope_key": "scope-video",
            "write_requests": [],
        },
        policy_results=[{}],
    )

    state = await ExecuteFlowNode().run(state)

    assert calls == ["VERIFY", "APPLY"]
    assert state.execution_result["executor_status"] == "RECONCILED"
    assert state.execution_result["tc_operation"] == "REPAIRED"
    assert state.execution_result["data_plane_changed"] is True
    assert state.execution_result["no_change"] is False


@pytest.mark.asyncio
async def test_verify_existing_reuses_live_classifier_when_runtime_history_is_empty(rule_fallback_llm, monkeypatch):
    """Live exact P4 readback is authoritative over an incomplete old runtime record."""
    plan = IntentPlan(
        raw_intent="cap an existing backup flow",
        sub_intents=[SubIntent(
            sequence=1, objective="Backup cap", intent_type="RATE_LIMIT",
            match={"source_ip": "10.0.0.20/32", "destination_ip": "192.168.10.10/32",
                   "destination_port": 5203, "protocol": "TCP"},
            action={"action_type": "RATE_LIMIT",
                    "qos_params": {"dscp_value": 8, "bandwidth": 20, "burst_size": 1024}},
            constraints={"policy_name": "backup-cap"}, priority=100,
        )],
    )
    policy = await PolicyModelingAgent().model(plan)

    class FakePolicyStore:
        def stage(self, *_args, **_kwargs):
            return {
                "traffic_scope_key": "scope-backup", "version": 4,
                "yang_json": json.dumps(policy.model_dump(mode="json")),
                # Regression premise: old code incorrectly required this to
                # contain an installed flow before it could reuse live P4.
                "runtime_json": "{}",
            }

    async def no_pre_telemetry(self, _state):
        return {}

    async def live_exact_classifier(_policy, _runtime, **_kwargs):
        return {"matching_entry_count": 1, "flow_id": "live-20"}

    async def verified_tc(self, _state, *, operation):
        assert operation == "VERIFY"
        return {"status": "ok", "config_applied": True,
                "qos_binding": {"interface": "s_agg_r-eth2"}}

    class NeverDeliver:
        def __init__(self):
            raise AssertionError("VERIFY_EXISTING must not construct a P4 delivery for a live exact classifier")

    import persistence.policy_store
    from mcp_servers.flow_delivery import FlowDeliveryFactory

    monkeypatch.setattr(persistence.policy_store, "PolicyStore", FakePolicyStore)
    monkeypatch.setattr("api.qos.verify_p4_classifier", live_exact_classifier)
    monkeypatch.setattr(ExecuteFlowNode, "_collect_pre_execution_telemetry", no_pre_telemetry)
    monkeypatch.setattr(ExecuteFlowNode, "_apply_qos_with_execution_lease", verified_tc)
    monkeypatch.setattr(FlowDeliveryFactory, "create", staticmethod(NeverDeliver))

    state = GlobalAgentState(
        intent_id="INT-REUSE-LIVE", user_intent=UserIntent(text="reuse live backup cap"),
        yang_policies=[policy], yang_draft=policy,
        verified_flow_table_plan={
            "policy_operation": "VERIFY_EXISTING", "traffic_scope_key": "scope-backup",
            "existing_policy_id": "POL-CURRENT", "write_requests": [{"device_id": "device:bmv2:6"}],
        }, policy_results=[{}],
    )
    state = await ExecuteFlowNode().run(state)

    assert state.execution_result["executor_status"] == "REUSED"
    assert state.execution_result["classifier_reused"] is True
    assert state.execution_result["data_plane_changed"] is False
    assert state.execution_result["southbound_mutations"] == {
        "onos_post": 0, "onos_delete": 0, "p4_insert": 0, "p4_modify": 0, "p4_delete": 0,
        "tc_qdisc_apply": 0, "tc_class_apply": 0, "tc_filter_apply": 0,
        "tc_qdisc_delete": 0, "tc_class_delete": 0, "tc_filter_delete": 0,
        "ingress_apply": 0, "resource_allocation_mutation": 0,
        "classification": "NO_NETWORK_CONFIGURATION_MUTATION",
    }


@pytest.mark.asyncio
async def test_execution_failpoint_after_qos_apply_rolls_back_staged_update(rule_fallback_llm, monkeypatch):
    """A default-off post-actuator fault must use staged-policy compensation."""
    plan = IntentPlan(
        raw_intent="lower an existing backup cap",
        sub_intents=[SubIntent(
            sequence=1, objective="Backup cap", intent_type="RATE_LIMIT",
            match={"source_ip": "10.0.0.20/32", "destination_ip": "192.168.10.10/32",
                   "destination_port": 5203, "protocol": "TCP"},
            action={"action_type": "RATE_LIMIT",
                    "qos_params": {"dscp_value": 8, "bandwidth": 10, "burst_size": 1024}},
            constraints={"policy_name": "backup-cap"}, priority=100,
        )],
    )
    policy = await PolicyModelingAgent().model(plan)
    staged_rollbacks = []

    class FakePolicyStore:
        def stage(self, *_args, **_kwargs):
            return {
                "traffic_scope_key": "scope-backup", "version": 6,
                "yang_json": json.dumps(policy.model_dump(mode="json")),
                "runtime_json": "{}",
            }

        def rollback_staged(self, scope, *, error):
            staged_rollbacks.append((scope, error))

    async def no_pre_telemetry(self, _state):
        return {}

    async def applied_qos(self, _state, *, operation):
        assert operation == "APPLY"
        return {"status": "ok", "config_applied": True}

    qos_rollbacks = []

    async def record_qos_rollback(old_policy, new_policy):
        qos_rollbacks.append((old_policy, new_policy))
        return {"status": "ROLLED_BACK"}

    import persistence.policy_store
    import api.qos

    monkeypatch.setattr(persistence.policy_store, "PolicyStore", FakePolicyStore)
    monkeypatch.setattr(ExecuteFlowNode, "_collect_pre_execution_telemetry", no_pre_telemetry)
    monkeypatch.setattr(ExecuteFlowNode, "_apply_qos_with_execution_lease", applied_qos)
    monkeypatch.setattr(api.qos, "rollback_qos_policy", record_qos_rollback)
    monkeypatch.setenv("IDN_EXECUTION_FAILPOINT", "after_qos_apply")

    state = GlobalAgentState(
        intent_id="INT-FAILPOINT", user_intent=UserIntent(text="lower backup cap"),
        yang_policies=[policy], yang_draft=policy,
        verified_flow_table_plan={
            "policy_operation": "MODIFY_QOS_RESOURCE", "traffic_scope_key": "scope-backup",
            "write_requests": [],
        }, policy_results=[{}],
    )
    state = await ExecuteFlowNode().run(state)

    assert state.execution_result["status"] == "FAILED"
    assert state.execution_result["error"] == "EXECUTION_FAILPOINT:after_qos_apply"
    assert qos_rollbacks and qos_rollbacks[0][0] is not None
    assert staged_rollbacks == [("scope-backup", "EXECUTION_FAILPOINT:after_qos_apply")]

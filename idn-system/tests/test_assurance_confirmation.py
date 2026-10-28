"""Regression coverage for bounded Assurance confirmation semantics."""
from __future__ import annotations

import pytest

from agents.status_verifier import VerificationResult
from models.telemetry import KPIMetrics, TelemetryData
from orchestrator.nodes.verify_status_node import VerifyStatusNode
from orchestrator.nodes.collect_diagnostic_evidence_node import CollectDiagnosticEvidenceNode
from orchestrator.graph import route_after_diagnostics, route_after_verification
from agents.status_verifier import StatusVerifierAgent
from models.agent_state import GlobalAgentState, ConsensusStatus, StageStatus
from models.intent import IntentStatus, UserIntent
from models.yang_policy import ActionRules, ActionType, MatchCriteria, NetworkPolicy, QoSParams, YANGPolicy


def _result(throughput: float | None, *, passed: bool, state: str, violations=None):
    return VerificationResult(
        passed=passed,
        telemetry=TelemetryData(
            intent_id="INT-ASSURANCE",
            sample_count=1,
            kpi=KPIMetrics(throughput_mbps=throughput),
            raw_int_data={
                "policy_traffic_probe": {
                    "offered_rate_mbps": 90,
                    "minimum_acceptable_mbps": 28.5,
                }
            },
        ),
        violations=list(violations or []),
        verification_state=state,
    )


def test_first_low_sample_is_confirmation_not_unsatisfiable():
    sample = VerifyStatusNode._assurance_sample(
        _result(26.1, passed=False, state="CONFIRMING_VIOLATION"), sample_id=1
    )
    assert sample["valid"] is True
    assert sample["received_mbps"] == 26.1
    assert sample["threshold_mbps"] == 28.5
    assert VerifyStatusNode._confirmation_decision([
        _result(26.1, passed=False, state="CONFIRMING_VIOLATION")
    ])["verification_state"] == "MEASUREMENT_INCONCLUSIVE"


def test_transient_low_sample_recovers_after_two_valid_passes():
    decision = VerifyStatusNode._confirmation_decision([
        _result(26.1, passed=False, state="CONFIRMING_VIOLATION"),
        _result(29.2, passed=True, state="MIN_BANDWIDTH_KPI_VERIFIED"),
        _result(29.7, passed=True, state="MIN_BANDWIDTH_KPI_VERIFIED"),
    ])
    assert decision == {
        "verification_state": "SATISFIED_WITH_TRANSIENT_WARNING",
        "persistence_confirmed": False,
    }


def test_two_valid_low_samples_confirm_violation():
    decision = VerifyStatusNode._confirmation_decision([
        _result(26.1, passed=False, state="CONFIRMING_VIOLATION"),
        _result(27.8, passed=False, state="CONFIRMING_VIOLATION"),
        _result(29.2, passed=True, state="MIN_BANDWIDTH_KPI_VERIFIED"),
    ])
    assert decision["verification_state"] == "VIOLATION_CONFIRMED"
    assert decision["persistence_confirmed"] is True


def test_invalid_transport_failure_is_not_converted_to_zero_throughput():
    sample = VerifyStatusNode._assurance_sample(
        _result(
            None,
            passed=False,
            state="NO_SAMPLE",
            violations=["No route to host"],
        ),
        sample_id=2,
    )
    assert sample["valid"] is False
    assert sample["received_mbps"] is None
    assert sample["quality_reason"].startswith("MEASUREMENT_INVALID")


def test_rate_limit_contract_has_only_an_upper_throughput_bound():
    policy = YANGPolicy(
        raw_intent="cap backup traffic at 20 Mbps",
        network_policy=NetworkPolicy(
            policy_id="POL-CAP", policy_name="cap", match=MatchCriteria(
                source_ip="10.0.0.20/32", destination_ip="192.168.10.10/32",
                protocol="TCP", destination_port=5203,
            ),
            action=ActionRules(action_type=ActionType.RATE_LIMIT, qos_params=QoSParams(
                qos_mode="RATE_LIMIT", bandwidth=20, dscp_value=8,
            )),
        ),
    )
    constraints = StatusVerifierAgent()._extract_constraints(policy)
    assert constraints.max_bandwidth_mbps == 22
    assert constraints.min_bandwidth_mbps is None


def test_low_confidence_root_cause_stays_unconfirmed():
    assert VerifyStatusNode._root_cause_confirmed({
        "confidence": 0.15,
        "evidence": [{"source": "tc_qdisc", "strength": "strong"}],
    }) is False


def test_root_cause_requires_confidence_and_deterministic_evidence():
    assert VerifyStatusNode._root_cause_confirmed({
        "confidence": 0.75,
        "evidence": [{"source": "tc_qdisc", "strength": "strong"}],
    }) is True
    assert VerifyStatusNode._root_cause_confirmed({
        "confidence": 0.75,
        "evidence": [{"source": "kpi", "strength": "medium"}],
    }) is False


def _pending_state() -> GlobalAgentState:
    state = GlobalAgentState(
        intent_id="INT-PENDING",
        user_intent=UserIntent(intent_id="INT-PENDING", text="test"),
        current_status=IntentStatus.DEGRADED,
        verification_passed=False,
        verification_report={
            "verification_state": "VIOLATION_CONFIRMED_ROOT_CAUSE_PENDING",
            "root_cause_report": {
                "status": "UNCONFIRMED",
                "confidence": 0.15,
                "missing_evidence": ["tc_overlimit_delta"],
                "evidence": [],
            },
        },
    )
    state.execution_generation = 1
    state.execution_readback_generation = 1
    state.execution_readback_status = "EXECUTION_READBACK_VERIFIED"
    state.assurance_generation = 1
    state.assurance_basis_execution_generation = 1
    state.assurance_id = "ASR-PENDING"
    state.measurement_status = "COMPLETED"
    state.valid_assurance_sample_count = 3
    state.assurance_samples = [
        {"valid": True, "received_mbps": 20, "threshold_mbps": 28.5, "environment_epoch": "epoch-1"},
        {"valid": True, "received_mbps": 21, "threshold_mbps": 28.5, "environment_epoch": "epoch-1"},
        {"valid": True, "received_mbps": 22, "threshold_mbps": 28.5, "environment_epoch": "epoch-1"},
    ]
    state.violation_confirmed = True
    state.violation_id = "VIO-PENDING"
    state.violation_basis_assurance_generation = 1
    state.root_cause_status = "PENDING"
    state.root_cause_analysis_id = "RCA-PENDING"
    state.root_cause_basis_violation_id = "VIO-PENDING"
    state.diagnostic_basis_root_cause_id = "RCA-PENDING"
    state.diagnostic_state = {"diagnostic_round": 0, "max_diagnostic_rounds": 3}
    state.diagnostics_required = True
    return state


def test_root_cause_pending_routes_to_diagnostics_not_generate_or_execute():
    state = _pending_state()
    assert route_after_verification(state) == "collect_diagnostics"
    consensus = VerifyStatusNode()._build_consensus_decision(
        state, StageStatus.SUCCESS, "pending"
    )
    assert consensus.status == ConsensusStatus.NEEDS_DIAGNOSTICS
    assert consensus.next_action == "COLLECT_DIAGNOSTIC_EVIDENCE"
    assert consensus.healing_ready is False
    assert consensus.diagnostics_required is True


def test_evidence_hash_ignores_timestamp_but_tracks_material_change():
    baseline = {
        "environment_epoch": "epoch-1", "context_hash": "ctx-1",
        "current_path": ["a", "b"], "candidate_paths": [["a", "c", "b"]],
        "link_utilization": {"a-b": 80}, "tc": {"overlimits": 1},
        "p4": {"packets": 12}, "onos": {"packets": 12}, "faults": [],
        "workloads": [], "resource_ledger": [], "assurance_samples": ["S1"],
        "missing_evidence": ["candidate_path_overlap"], "timestamp": "first",
    }
    same_content_new_time = {**baseline, "timestamp": "later"}
    changed = {**baseline, "link_utilization": {"a-b": 95}}
    assert CollectDiagnosticEvidenceNode.evidence_hash(baseline) == CollectDiagnosticEvidenceNode.evidence_hash(same_content_new_time)
    assert CollectDiagnosticEvidenceNode.evidence_hash(baseline) != CollectDiagnosticEvidenceNode.evidence_hash(changed)


def test_diagnostic_rounds_are_bounded_and_end_degraded_not_unsatisfiable():
    state = _pending_state()
    state.diagnostic_state = {
        "diagnostic_round": 3,
        "max_diagnostic_rounds": 3,
        "new_evidence": False,
    }
    state.verification_report["verification_state"] = "DEGRADED_ROOT_CAUSE_UNCONFIRMED"
    assert route_after_diagnostics(state) == "__end__"
    assert state.current_status != IntentStatus.UNSATISFIABLE


def test_identical_diagnostic_step_has_stable_loop_guard_key():
    diagnostic = {
        "environment_epoch": "epoch-1", "context_hash": "ctx-1",
        "root_cause_status": "PENDING", "evidence_hash": "evidence-1",
    }
    first = CollectDiagnosticEvidenceNode.loop_guard_key(
        intent_id="INT-PENDING", diagnostic=diagnostic, route_decision="DEGRADED_AWAITING_DIAGNOSTICS"
    )
    second = CollectDiagnosticEvidenceNode.loop_guard_key(
        intent_id="INT-PENDING", diagnostic={**diagnostic, "timestamp": "ignored"}, route_decision="DEGRADED_AWAITING_DIAGNOSTICS"
    )
    assert first == second


def test_http_409_readback_mismatch_is_structured_and_routes_to_reconciliation():
    failure = StatusVerifierAgent._readback_failure("QoS VERIFY readback failed: HTTP 409 status=mismatch", {})
    assert failure["execution_readback_status"] == "EXECUTION_READBACK_MISMATCH"
    assert failure["http_status"] == 409
    state = _pending_state()
    state.verification_report = {"verification_state": "EXECUTION_READBACK_MISMATCH"}
    state.execution_readback_status = "EXECUTION_READBACK_MISMATCH"
    assert route_after_verification(state) == "reconcile_execution"


@pytest.mark.asyncio
async def test_deployed_create_commits_owner_before_reconciling_readback_mismatch(monkeypatch):
    """A repairable CREATE mismatch must leave a managed Reconciler owner."""
    import agents.status_verifier as verifier_module

    policy = YANGPolicy(
        network_policy=NetworkPolicy(
            policy_id="POL-REPAIRABLE-CREATE", policy_name="repairable-create",
            match=MatchCriteria(
                source_ip="192.168.10.20/32", destination_ip="10.0.0.10/32",
                protocol="UDP", destination_port=5005,
            ),
            action=ActionRules(
                action_type=ActionType.RATE_LIMIT,
                qos_params=QoSParams(qos_mode="RATE_LIMIT", bandwidth=5, dscp_value=0),
            ),
        ),
    )
    state = GlobalAgentState(
        intent_id="INT-REPAIRABLE-CREATE",
        user_intent=UserIntent(intent_id="INT-REPAIRABLE-CREATE", text="limit test traffic"),
        yang_draft=policy,
        execution_result={"status": "SUCCESS", "traffic_scope_key": "TSK-repairable"},
    )
    committed = []

    async def mismatch(*_args, **_kwargs):
        return _result(None, passed=False, state="EXECUTION_READBACK_MISMATCH")

    node = VerifyStatusNode()
    monkeypatch.setattr(verifier_module.StatusVerifierAgent, "verify", mismatch)
    monkeypatch.setattr(node, "_start_log", lambda _state: 0)
    monkeypatch.setattr(node, "_end_log", lambda value, *_args, **_kwargs: value)
    monkeypatch.setattr(node, "_commit_policy", lambda value: committed.append(value.intent_id))

    result = await node.run(state)

    assert result.execution_readback_status == "EXECUTION_READBACK_MISMATCH"
    assert result.reconciliation_required is True
    assert committed == ["INT-REPAIRABLE-CREATE"]
    assert result.verification_report["desired_ownership"] == "CURRENT_MANAGED_DEPLOYED_BUT_UNASSURED"


def test_diagnostic_cap_precedes_new_evidence_and_never_routes_round_four():
    state = _pending_state()
    state.diagnostic_state = {"diagnostic_round": 3, "max_diagnostic_rounds": 3, "new_material_evidence": True, "final_reanalysis_pending": False}
    assert route_after_diagnostics(state) == "__end__"


def test_final_root_cause_reanalysis_ends_degraded_not_failed_or_approval():
    state = _pending_state()
    state.diagnostic_state = {
        "diagnostic_round": 3,
        "max_diagnostic_rounds": 3,
        "final_reanalysis_pending": True,
    }
    # The verifier refreshes this field during reanalysis; routing must use
    # the current violation and diagnostic basis rather than fall through to
    # generic retry/approval handling.
    state.verification_report = {"verification_state": "FAILED"}
    assert route_after_verification(state) == "__end__"
    assert state.current_status == IntentStatus.DEGRADED
    assert state.verification_report["verification_state"] == "DEGRADED_ROOT_CAUSE_UNCONFIRMED"
    assert state.verification_report["terminal_reason"] == "ROOT_CAUSE_UNCONFIRMED_AFTER_MAX_DIAGNOSTICS"


def test_material_hash_ignores_trace_round_timestamp_and_absolute_counters():
    baseline = {"environment_epoch": "epoch", "current_path": ["a", "b"], "p4": {"absolute_packets": 10, "delta_packets": 3}, "tc": {"drops_delta": 0}, "request_id": "one", "diagnostic_round": 1, "collected_at": "first"}
    changed_only_volatile = {**baseline, "p4": {"absolute_packets": 9999, "delta_packets": 3}, "request_id": "two", "diagnostic_round": 2, "collected_at": "later"}
    assert CollectDiagnosticEvidenceNode.evidence_hash(baseline) == CollectDiagnosticEvidenceNode.evidence_hash(changed_only_volatile)

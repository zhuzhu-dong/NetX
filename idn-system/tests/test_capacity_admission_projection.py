from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.decision_report import build_decision_artifacts
from models.agent_state import ConsensusReasonCode, ConflictReport


def _capacity_state() -> dict:
    return {
        "intent_id": "INT-capacity-rejected",
        "current_status": "unsatisfiable",
        "user_intent": {"text": "保障科研视频最低带宽20Mbps"},
        "intent_plan": {"plan_id": "PLAN-candidate", "sub_intents": [{"service_id": "research_video"}]},
        "yang_policies": [{"network_policy": {"policy_id": "POL-candidate", "action": {"qos_params": {"bandwidth": 20}}}}],
        "conflict_report": {
            "has_conflict": False,
            "conflict_detected": False,
            "blocking": True,
            "stop_category": "CAPACITY_ADMISSION_REJECTED",
            "conflict_type": "INSUFFICIENT_CAPACITY",
            "initial_admission_reason": "RUNTIME_CAPACITY_UNVERIFIED",
            "recovery_action": "LIVE_RECEIVER_IPERF_CANDIDATE_CALIBRATION",
            "calibration_result": "REJECTED",
            "final_reason_code": "CALIBRATION_DOES_NOT_SUPPORT_CANDIDATE",
            "final_decision": "UNSATISFIABLE",
            "southbound_execution": "NOT_EXECUTED",
            "traffic_scope_key": "scope-candidate",
            "admission_decision": {"requested_mbps": 20, "available_mbps": 100},
            "conflict_items": [{"admission_decision": {"requested_mbps": 20, "reason_code": "CALIBRATION_DOES_NOT_SUPPORT_CANDIDATE"}}],
            "explanation": "Guaranteed bandwidth admission rejected.",
            "rejection_reason": "CALIBRATION_DOES_NOT_SUPPORT_CANDIDATE",
        },
        "policy_results": [],
        "execution_result": {},
        "verification_report": {},
    }


def test_capacity_rejection_is_not_true_policy_conflict():
    _timeline, report = build_decision_artifacts(_capacity_state())
    assert report["stop_category"] == "CAPACITY_ADMISSION_REJECTED"
    assert report["policy_conflict"]["conflict_detected"] is False
    assert report["policy_conflict"]["has_conflict"] is False
    assert report["policy_conflict"]["status"] == "capacity_rejected"


def test_true_policy_conflict_requires_existing_conflicting_policy():
    from agents.policy_conflict_checker import PolicyConflictCheckerAgent

    checker = PolicyConflictCheckerAgent()
    without_existing = checker._blocking_stop_category(
        [{"conflict_type": "ACTION_CONFLICT", "severity": "HIGH"}],
        {"conflict_type": "ACTION_CONFLICT"},
    )
    with_existing = checker._blocking_stop_category(
        [{"conflict_type": "ACTION_CONFLICT", "severity": "HIGH", "existing_policy_id": "POL-existing"}],
        {"conflict_type": "ACTION_CONFLICT"},
    )
    assert without_existing != ConsensusReasonCode.TRUE_POLICY_CONFLICT
    assert with_existing == ConsensusReasonCode.TRUE_POLICY_CONFLICT


def test_pre_execution_rejection_evidence_is_not_applicable():
    _timeline, report = build_decision_artifacts(_capacity_state())
    plane = report["execution_plane"]
    assert plane["execution_state"] == "NOT_EXECUTED"
    assert plane["classifier"]["verification_state"] == "NOT_APPLICABLE"
    assert plane["p4_counter"]["verification_state"] == "NOT_APPLICABLE"
    assert plane["scheduler"]["verification_state"] == "NOT_APPLICABLE"
    assert plane["competition_kpi"]["verification_state"] == "NOT_APPLICABLE"
    assert report["verification_result"]["verification_state"] == "NOT_APPLICABLE"


def test_status_verifier_cannot_report_kpi_verified_without_execution():
    timeline, report = build_decision_artifacts(_capacity_state())
    verifier = next(item for item in timeline if item["target_agent"] == "status_verifier")
    vote = next(item for item in report["agent_votes"] if item["agent_id"] == "status_verifier")
    assert verifier["status"] == "skipped"
    assert "NOT_APPLICABLE" in verifier["output_summary"]
    assert "KPI" in vote["vote"] and "未执行" in vote["vote"]
    assert report["verification_result"]["passed"] is False


def test_planned_action_not_projected_as_executed():
    _timeline, report = build_decision_artifacts(_capacity_state())
    assert report["planned_actions"] == ["install_compiled_policy"]
    assert report["actual_actions"] == []
    assert report["final_action"]["execution_state"] == "NOT_EXECUTED"


def test_final_reason_supersedes_initial_runtime_unverified():
    _timeline, report = build_decision_artifacts(_capacity_state())
    assert report["admission"]["initial_admission_reason"] == "RUNTIME_CAPACITY_UNVERIFIED"
    assert report["admission"]["final_reason_code"] == "CALIBRATION_DOES_NOT_SUPPORT_CANDIDATE"
    assert report["final_conclusion"].endswith("CALIBRATION_DOES_NOT_SUPPORT_CANDIDATE")


def test_conversation_uses_terminal_calibration_reason_not_initial_admission():
    from services.conversation_results import build_assistant_intent_message

    message = build_assistant_intent_message({
        "intent_id": "INT-terminal-reason", "status": "unsatisfiable", "text": "guarantee 20 Mbps",
        "result": {"policy_conflict_report": {
            "final_reason_code": "CALIBRATION_DOES_NOT_SUPPORT_CANDIDATE",
            "admission_decision": {"reason_code": "RUNTIME_CAPACITY_UNVERIFIED"},
        }},
    })
    assert message["reason_code"] == "CALIBRATION_DOES_NOT_SUPPORT_CANDIDATE"


async def _run_candidate_calibration(monkeypatch):
    import infra.capacity_calibration as calibration
    from infra.capacity_calibration import ActiveIngressCapacityCalibrator, CalibrationCandidate

    monkeypatch.setenv("IDN_CAPACITY_CALIBRATION_WARMUP_SECONDS", "1")
    monkeypatch.setenv("IDN_CAPACITY_CALIBRATION_SAMPLE_INTERVAL_SECONDS", "0.25")
    sleeps: list[float] = []

    async def fake_sleep(value):
        sleeps.append(value)

    monkeypatch.setattr(calibration.asyncio, "sleep", fake_sleep)

    class Store:
        events = []
        def add_audit_event(self, **event): self.events.append(event)
    class Registry:
        def snapshot(self):
            return SimpleNamespace(topology_name="topology", links=[SimpleNamespace(resource_id="link:a--b", capacity_mbps=100)])
    class CapacityRepo:
        def __init__(self, _store): pass
        def put(self, record): return record
    class Fence:
        released = []
        def __init__(self, _store): pass
        def acquire(self, **_kwargs):
            return {"lease_id": "lease", "owner_type": "PROVISIONAL_CAPACITY_CALIBRATION", "owner_id": "INT", "fencing_token": 1, "resource_scope": "scope"}
        def authorize_network_configuration_write(self, **_kwargs): return {"authorized": True}
        def record_execution(self, *_args, **_kwargs): pass
        def release(self, lease_id): self.released.append(lease_id); return True
    class Probe:
        iperf_calls = []
        async def get(self, path, query):
            if path == "/probe/iperf":
                self.iperf_calls.append(query)
                return {"status": "ok", "received_mbps": 40.0, "sent_mbps": 42.0, "loss_percent": 0, "jitter_ms": 0.1, "sent_packets": 100, "lost_packets": 0, "iperf_exit_status": 0, "iperf_server_exit_status": 0, "effective_arguments": ["iperf3", "-J"], "processes_after": {"process_cleanup_verified": True}}
            return {"status": "ok", "process_cleanup_verified": True}

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr(calibration, "StableCapacityRepository", CapacityRepo)
    monkeypatch.setattr(calibration, "NetworkWriteFencing", Fence)
    monkeypatch.setattr(calibration, "resolve_service_profile_for_policy", lambda _policy: SimpleNamespace(src_host="h_research", dst_host="h_dc", protocol=SimpleNamespace(value="UDP"), dst_port=5004, dscp=46, packet_size_bytes=1200))
    candidate = CalibrationCandidate("INT", "scope", object(), "research_video", "link:a--b", 20, "topology", {}, "TRACE", "now")

    async def p4_reader(_profile): return {"status": "NO_MATCHING_ENTRY"}

    result = await ActiveIngressCapacityCalibrator(
        Store(), probe=Probe(), p4_reader=p4_reader,
    ).calibrate_candidate(candidate, samples=3, duration_seconds=1)
    return result, Probe.iperf_calls, sleeps, Fence.released


@pytest.mark.asyncio
async def test_calibration_records_sample_diagnostics(monkeypatch):
    result, _calls, _sleeps, _released = await _run_candidate_calibration(monkeypatch)
    sample = result["samples"][0]
    assert sample["diagnostics_before"]["processes"]["process_cleanup_verified"] is True
    assert sample["datagrams_received"] == 100
    assert sample["iperf_exit_status"] == 0
    assert sample["effective_probe_arguments"] == ["iperf3", "-J"]


@pytest.mark.asyncio
async def test_calibration_warmup_not_counted_as_sample(monkeypatch):
    result, calls, _sleeps, _released = await _run_candidate_calibration(monkeypatch)
    assert len(result["samples"]) == 3
    assert len(calls) == 4  # one uncounted warm-up plus three formal samples
    assert result["samples"][0]["warmup"]["counted_in_capacity_estimate"] is False


@pytest.mark.asyncio
async def test_calibration_sample_interval(monkeypatch):
    _result, _calls, sleeps, _released = await _run_candidate_calibration(monkeypatch)
    assert sleeps == [0.25, 0.25]


@pytest.mark.asyncio
async def test_calibration_cleanup_releases_all_probe_resources(monkeypatch):
    _result, _calls, _sleeps, released = await _run_candidate_calibration(monkeypatch)
    assert released == ["lease"]

from __future__ import annotations

from types import SimpleNamespace

import pytest

from models.agent_state import ConflictReport
from models.yang_policy import (
    ActionRules,
    ActionType,
    MatchCriteria,
    NetworkPolicy,
    Protocol,
    QoSMode,
    QoSParams,
    YANGPolicy,
)


def _draft() -> YANGPolicy:
    return YANGPolicy(
        service_id="research_video",
        network_policy=NetworkPolicy(
            policy_id="POL-FRESH",
            policy_name="fresh-research-video",
            match=MatchCriteria(
                source_ip="192.168.10.10/32",
                destination_ip="10.0.0.10/32",
                protocol=Protocol.UDP,
                destination_port=5004,
            ),
            action=ActionRules(
                action_type=ActionType.SET_PRIORITY,
                qos_params=QoSParams(
                    qos_mode=QoSMode.MIN_BANDWIDTH,
                    bandwidth=30,
                    dscp_value=46,
                ),
            ),
        ),
    )


def test_check_timeout_covers_a_real_candidate_calibration(monkeypatch):
    """Cold-start measurement must not be cancelled by the old 10s budget."""
    from orchestrator.nodes.check_conflict_node import CheckConflictNode

    monkeypatch.delenv("IDN_NODE_TIMEOUT_CHECK", raising=False)
    assert CheckConflictNode()._get_timeout() >= 90


@pytest.mark.asyncio
async def test_fresh_install_first_min_bandwidth_auto_calibrates(monkeypatch):
    """One fresh NEW rejection is converted to evidence then re-admitted.

    The test drives the workflow boundary rather than calling the calibrator
    directly: the initial report represents zero stable capacity, no policy,
    and no binding.  Only the provisional authority may run before the
    checker is called a second time.
    """
    from orchestrator.nodes.check_conflict_node import CheckConflictNode
    import infra.capacity_calibration as calibration

    calls = []

    class Registry:
        def snapshot(self):
            return SimpleNamespace(topology_name="fresh-topology")

    class Calibrator:
        async def calibrate_candidate(self, candidate):
            calls.append(candidate)
            return {
                "status": "CALIBRATED",
                "authority_source": "PROVISIONAL_ADMISSION_CANDIDATE",
                "capacity_record": {"capacity_record_id": "CAP-fresh"},
                "schedulable_capacity_mbps": 35.0,
            }

    class Checker:
        async def check(self, **_kwargs):
            calls.append("recheck")
            return ConflictReport(
                has_conflict=False,
                approval_token="approved-after-live-calibration",
                execution_mode="CREATE",
            )

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr(calibration, "ActiveIngressCapacityCalibrator", Calibrator)

    initial = ConflictReport(
        has_conflict=True,
        blocking=True,
        conflict_type="INSUFFICIENT_CAPACITY",
        conflict_items=[{
            "link_id": "link:s_agg_r--s_core1",
            "admission_decision": {
                "reason_code": "RUNTIME_CAPACITY_UNVERIFIED",
                "purpose": "NEW_ADMISSION",
                "requested_mbps": 30,
                "affected_resources": ["link:s_agg_r--s_core1"],
            },
        }],
    )
    state = SimpleNamespace(
        intent_id="INT-fresh-min-bandwidth",
        yang_draft=_draft(),
        intent_plan=None,
        network_snapshot=SimpleNamespace(),
    )

    result = await CheckConflictNode()._calibrate_fresh_candidate_if_required(
        state=state, checker=Checker(), report=initial,
    )

    assert result.has_conflict is False
    assert calls[-1] == "recheck"
    candidate = calls[0]
    assert candidate.intent_id == "INT-fresh-min-bandwidth"
    assert candidate.requested_schedulable_mbps == 30
    assert candidate.bottleneck_egress == "link:s_agg_r--s_core1"
    assert candidate.staged_policy is state.yang_draft


@pytest.mark.asyncio
async def test_preview_never_auto_calibrates_or_persists_capacity(monkeypatch):
    """Preview stays an in-memory, no-write operation on an empty system."""
    from orchestrator.nodes.check_conflict_node import CheckConflictNode
    import infra.capacity_calibration as calibration

    class Calibrator:
        async def calibrate_candidate(self, _candidate):
            raise AssertionError("preview must never create candidate capacity evidence")

    monkeypatch.setattr(calibration, "ActiveIngressCapacityCalibrator", Calibrator)
    report = ConflictReport(has_conflict=True, conflict_items=[{
        "admission_decision": {"reason_code": "RUNTIME_CAPACITY_UNVERIFIED", "purpose": "NEW_ADMISSION"},
    }])
    state = SimpleNamespace(intent_id="PREVIEW-fresh", yang_draft=_draft())

    result = await CheckConflictNode()._calibrate_fresh_candidate_if_required(
        state=state, checker=object(), report=report,
    )

    assert result is report

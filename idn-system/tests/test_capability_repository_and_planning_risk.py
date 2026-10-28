from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from agents.intent_planning import CandidateGenerator
from api.store import IntentStore
from models.capability import CapabilityEvidenceStatus
from models.intent_domain import (
    AssessmentStatus,
    ContextEvidenceQuality,
    EvidenceQuality,
    IntentAssuranceResult,
    IntentLifecycle,
    IntentObjective,
    IntentSpec,
    NetworkContext,
    ObjectiveAssessment,
    ObjectiveType,
    PlanningRiskStatus,
)
from persistence.capability_repository import CapabilityRepository


def _context() -> NetworkContext:
    return NetworkContext(
        context_id="CTX-LIVE",
        epoch="EPOCH-1",
        evidence_quality=ContextEvidenceQuality.TRUSTED,
        quality=EvidenceQuality.TRUSTED,
        topology={
            "aliases": {},
            "nodes": [{"id": "h1"}, {"id": "h2"}],
            "links": [],
        },
        inventory={"endpoints": [{"host_name": "h1"}, {"host_name": "h2"}]},
    )


def test_repository_learns_only_trusted_same_context_assurance(tmp_path):
    store = IntentStore(tmp_path / "capability.db")
    repository = CapabilityRepository(store, boot_id="BOOT-A")
    context = _context()
    assessment = ObjectiveAssessment(
        objective_id="OBJ-1",
        objective_type=ObjectiveType.MIN_BANDWIDTH,
        status=AssessmentStatus.PASS,
        observed=25,
        unit="Mbps",
        evidence_ids=["EVD-1"],
        reason="active validation passed",
    )
    assurance = IntentAssuranceResult(
        intent_id="INT-1",
        context_id=context.context_id,
        lifecycle=IntentLifecycle.SATISFIED,
        evidence_quality=EvidenceQuality.TRUSTED,
        assessments=[assessment],
        satisfied=True,
        summary="pass",
    )

    learned = repository.record_assurance(assurance, context, validation_run_id="VR-1")

    assert len(learned) == 1
    assert learned[0].evidence_refs == ["EVD-1"]
    assert (
        repository.find(metric="MIN_BANDWIDTH", epoch="EPOCH-1")[0].status
        == CapabilityEvidenceStatus.PROVEN
    )

    untrusted = assurance.model_copy(
        update={"evidence_quality": EvidenceQuality.DEGRADED}
    )
    wrong_context = assurance.model_copy(update={"context_id": "CTX-OTHER"})
    assert repository.record_assurance(untrusted, context) == []
    assert repository.record_assurance(wrong_context, context) == []


def test_capabilities_are_stale_after_repository_restart(tmp_path):
    store = IntentStore(tmp_path / "restart.db")
    first = CapabilityRepository(store, boot_id="BOOT-OLD")
    context = _context()
    assurance = IntentAssuranceResult(
        intent_id="INT-1",
        context_id=context.context_id,
        lifecycle=IntentLifecycle.SATISFIED,
        evidence_quality=EvidenceQuality.TRUSTED,
        assessments=[
            ObjectiveAssessment(
                objective_id="OBJ-1",
                objective_type=ObjectiveType.MIN_BANDWIDTH,
                status=AssessmentStatus.PASS,
                observed=30,
                reason="pass",
            )
        ],
        satisfied=True,
        summary="pass",
        observed_at=datetime.now(timezone.utc),
    )
    first.record_assurance(assurance, context)

    restarted = CapabilityRepository(store, boot_id="BOOT-NEW")

    assert (
        restarted.find(metric="MIN_BANDWIDTH", epoch="EPOCH-1")[0].status
        == CapabilityEvidenceStatus.STALE
    )


class RejectingPlanner:
    def compute(self, topology, source, destination, constraints):
        raise ValueError("capacity 8Mbps is below requested 20Mbps")


def test_rejected_candidate_and_reason_are_preserved():
    spec = IntentSpec(
        intent_id="INT-REJECT",
        original_text="h1 to h2 needs 20 Mbps",
        normalized_text="h1 to h2 needs 20 Mbps",
        source={"ip": "h1"},
        destination={"ip": "h2"},
        objectives=[IntentObjective(type=ObjectiveType.MIN_BANDWIDTH, value=20)],
    )

    candidates = CandidateGenerator(planner=RejectingPlanner()).generate(
        spec, _context()
    )

    assert len(candidates) == 1
    assert candidates[0].feasible is False
    assert candidates[0].risk_status == PlanningRiskStatus.INFEASIBLE
    assert candidates[0].rejection_reasons == [
        "capacity 8Mbps is below requested 20Mbps"
    ]


class RecordingPlanner:
    def __init__(self):
        self.endpoints = None

    def compute(self, topology, source, destination, constraints):
        self.endpoints = (source, destination)
        return SimpleNamespace(
            nodes=[source, destination],
            estimated_latency_ms=1.0,
            bottleneck_available_mbps=100.0,
            total_cost=1.0,
        )


def test_candidate_generation_accepts_live_mininet_endpoint_schema():
    context = _context().model_copy(
        update={
            "inventory": {
                "origin": "LIVE",
                "endpoints": [
                    {"name": "h1", "ip": "192.0.2.1", "ip_cidr": "192.0.2.1/24"},
                    {"name": "h2", "ip": "198.51.100.2", "ip_cidr": "198.51.100.2/24"},
                ],
            }
        }
    )
    spec = IntentSpec(
        intent_id="INT-LIVE-INVENTORY",
        original_text="h1 to h2 needs 20 Mbps",
        normalized_text="h1 to h2 needs 20 Mbps",
        source={"ip": "192.0.2.1/32"},
        destination={"ip": "198.51.100.2/32"},
        selector={
            "source_ip": "192.0.2.1/32",
            "destination_ip": "198.51.100.2/32",
        },
        objectives=[IntentObjective(type=ObjectiveType.MIN_BANDWIDTH, value=20)],
    )
    planner = RecordingPlanner()

    candidates = CandidateGenerator(planner=planner).generate(spec, context)

    assert planner.endpoints == ("h1", "h2")
    assert len(candidates) == 1
    assert candidates[0].path == ["h1", "h2"]

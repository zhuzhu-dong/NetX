import json

from services.formal_assurance_evidence import (
    FormalAssuranceEvidenceService,
    RawArtifactManifestItem,
)


def _raw(p4_status="PENDING_BACKEND_READ"):
    return {
        "validation_run_id": "VR-test", "status": "MEASURED",
        "selector": {"source_ip": "192.168.10.10/32", "destination_ip": "10.0.0.10/32", "protocol": "UDP", "destination_port": 5004, "dscp": 46},
        "offered_load": {"target_mbps": 40, "background_mbps": 95},
        "received_load": {"target_mbps": 35, "total_mbps": 41},
        "protocol_metrics": {"loss_percent": 1, "raw": {"start": {"test_start": {"blksize": 1200, "omit": 5, "duration": 35}}}},
        "path_evidence": {"same_bottleneck": True},
        "process_cleanup": {"process_cleanup_verified": True},
        "tc_evidence": {"status": "MEASURED"},
        "p4_counter_evidence": {"status": p4_status},
        "start_time": "2026-08-05T00:00:00Z", "end_time": "2026-08-05T00:00:35Z",
    }


def test_pending_p4_counter_is_invalid_not_zero(tmp_path):
    service = FormalAssuranceEvidenceService(allowed_root=tmp_path)
    artifact = RawArtifactManifestItem(artifact_id="ART-test", artifact_type="IPERF_VALIDATION_RUN_JSON", relative_location="run.json", sha256="a" * 64, byte_size=1)
    lineage = {"intent_id": "INT-test", "assurance_generation": "1:policy", "environment_epoch": "epoch", "policy_generation": "policy"}
    sample = service._normalize(_raw(), artifact, "AEB-test", lineage, 1)
    assert sample.measurement_quality == "INVALID"
    assert sample.target_received_mbps == 35
    assert sample.passed is False
    assert "P4_CLASSIFIER_UNVERIFIED" in sample.validation_errors


def test_verified_p4_counter_allows_a_valid_sample(tmp_path):
    service = FormalAssuranceEvidenceService(allowed_root=tmp_path)
    artifact = RawArtifactManifestItem(artifact_id="ART-test", artifact_type="IPERF_VALIDATION_RUN_JSON", relative_location="run.json", sha256="a" * 64, byte_size=1)
    lineage = {"intent_id": "INT-test", "assurance_generation": "1:policy", "environment_epoch": "epoch", "policy_generation": "policy"}
    sample = service._normalize(_raw("VERIFIED"), artifact, "AEB-test", lineage, 1)
    assert sample.measurement_quality == "VALID"
    assert sample.passed is True


def test_not_applicable_tc_readback_is_invalid(tmp_path):
    service = FormalAssuranceEvidenceService(allowed_root=tmp_path)
    artifact = RawArtifactManifestItem(artifact_id="ART-test", artifact_type="IPERF_VALIDATION_RUN_JSON", relative_location="run.json", sha256="a" * 64, byte_size=1)
    raw = _raw("VERIFIED"); raw["tc_evidence"] = {"status": "NOT_APPLICABLE"}
    lineage = {"intent_id": "INT-test", "assurance_generation": "1:policy", "environment_epoch": "epoch", "policy_generation": "policy"}
    sample = service._normalize(raw, artifact, "AEB-test", lineage, 1)
    assert sample.measurement_quality == "INVALID"
    assert "TC_READBACK_UNVERIFIED" in sample.validation_errors


def test_consumption_rejection_is_append_only_audit_state(tmp_path):
    class Domain:
        def __init__(self):
            self.events = []

        def record_event(self, *args, **kwargs):
            self.events.append((args, kwargs))

        def history(self, _intent_id):
            return [
                {
                    "event_type": "ASSURANCE_EVIDENCE_CONSUMPTION_REJECTED",
                    "reason": "TC_READBACK_UNVERIFIED",
                    "data": {
                        "evidence_batch_id": "AEB-invalid",
                        "reason_code": "TC_READBACK_UNVERIFIED",
                    },
                },
            ]

    domain = Domain()
    service = object.__new__(FormalAssuranceEvidenceService)
    service.domain = domain
    assert service._consumption_rejections("INT-test") == {
        "AEB-invalid": "TC_READBACK_UNVERIFIED",
    }

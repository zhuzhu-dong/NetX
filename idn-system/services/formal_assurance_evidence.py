"""Trusted ingestion and deterministic assessment of formal assurance evidence.

This module deliberately has no controller, planner, LLM, or dataplane calls.
It turns *existing*, immutable validation-run artifacts into an append-only
evidence batch, then lets the control plane make the normal lifecycle
transition from the resulting decision.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from models.intent_domain import EvidenceItem, EvidenceQuality
from persistence.intent_domain_store import IntentDomainStore, get_intent_domain_store

SCENARIO = "REAL_SHARED_BOTTLENECK_CONTENTION"
TRUSTED_SOURCES = {"INTERNAL_VALIDATION_RUNNER", "INTERNAL_SERVICE_VALIDATION", "AUTHORIZED_BACKFILL_IMPORT"}
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class TestContract(BaseModel):
    target_source_ip: str = "192.168.10.10/32"
    target_destination_ip: str = "10.0.0.10/32"
    protocol: str = "UDP"
    destination_port: int = 5004
    dscp: int = 46
    target_offered_mbps: float = 40.0
    background_offered_mbps: float = 95.0
    packet_size_bytes: int = 1200
    warmup_seconds: int = 5
    measurement_seconds: int = 30
    threshold_mbps: float = 28.5
    required_sample_count: int = 5
    required_pass_count: int = 5
    require_background_nonzero: bool = True
    require_same_bottleneck: bool = True
    require_cleanup_verified: bool = True
    require_execution_readback: bool = True
    require_ingress_admission_readback: bool = True
    require_egress_qos_readback: bool = True
    require_p4_classifier_evidence: bool = True


class RawArtifactManifestItem(BaseModel):
    artifact_id: str
    artifact_type: str
    relative_location: str
    media_type: str = "application/json"
    sha256: str
    byte_size: int
    generated_at: str | None = None
    producer: str = "mininet_probe_server"
    validation_run_id: str | None = None
    scenario: str | None = None
    sample_index: int | None = None


class AssuranceSample(BaseModel):
    sample_id: str
    evidence_batch_id: str
    validation_run_id: str
    intent_id: str
    assurance_generation: str
    scenario: str
    sample_index: int
    target_offered_mbps: float
    target_received_mbps: float
    target_loss_percent: float
    background_offered_mbps: float
    background_received_mbps: float
    packet_size_bytes: int
    warmup_seconds: int
    measurement_seconds: int
    threshold_mbps: float
    passed: bool
    measurement_quality: str
    same_bottleneck: bool
    background_nonzero: bool
    process_cleanup_verified: bool
    ingress_readback_verified: bool
    egress_readback_verified: bool
    p4_classifier_verified: bool
    environment_epoch: str
    policy_generation: str
    observed_at_start: str
    observed_at_end: str
    source_artifact_ids: list[str]
    validation_errors: list[str] = Field(default_factory=list)
    sample_hash: str


class AssuranceEvidenceBatch(BaseModel):
    evidence_batch_id: str
    intent_id: str
    revision: int
    run_id: str
    policy_id: str | None
    policy_generation: str
    desired_config_generation: str
    execution_generation: str
    readback_generation: str
    assurance_generation: str
    environment_epoch: str
    controller_epoch: str
    topology_id: str
    path_signature: str
    capacity_record_id: str
    ingress_admission_plan_id: str
    ingress_allocation_epoch: int
    evidence_producer: str
    producer_version: str
    evidence_source_type: str
    test_contract: TestContract
    scenario: str
    samples: list[AssuranceSample]
    readback_evidence: dict[str, Any]
    cleanup_evidence: dict[str, Any]
    raw_artifact_manifest: list[RawArtifactManifestItem]
    raw_artifact_hashes: dict[str, str]
    batch_hash: str
    created_at: datetime = Field(default_factory=_utcnow)
    observed_at_start: str
    observed_at_end: str
    valid_until: datetime
    trust_status: str
    validation_errors: list[str] = Field(default_factory=list)
    ingestion_status: str = "REGISTERED"
    importer_actor: str | None = None
    trace_id: str | None = None


class AssuranceDecision(BaseModel):
    decision_id: str
    evidence_batch_id: str
    intent_id: str
    status: str
    reason_code: str
    valid_sample_count: int
    required_sample_count: int
    passed_sample_count: int
    minimum_received_mbps: float | None
    mean_received_mbps: float | None
    background_nonzero_all_runs: bool
    lineage_verified: bool
    freshness: str
    created_at: datetime = Field(default_factory=_utcnow)


class FormalAssuranceEvidenceRepository:
    """Append-only evidence and decision persistence; never owns lifecycle."""
    def __init__(self, domain: IntentDomainStore | None = None):
        self.domain = domain or get_intent_domain_store()
        self.store = self.domain.base_store
        with self.store._lock, self.store._connect() as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS formal_assurance_batches_v1 (
                evidence_batch_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL,
                batch_hash TEXT NOT NULL, batch_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(evidence_batch_id, batch_hash));
            CREATE TABLE IF NOT EXISTS formal_assurance_decisions_v1 (
                decision_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL,
                evidence_batch_id TEXT NOT NULL, decision_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(evidence_batch_id));
            CREATE INDEX IF NOT EXISTS idx_formal_assurance_batches_intent ON formal_assurance_batches_v1(intent_id, created_at DESC);
            """)

    def register(self, batch: AssuranceEvidenceBatch) -> tuple[str, AssuranceEvidenceBatch]:
        with self.store._lock, self.store._connect() as connection:
            row = connection.execute("SELECT batch_hash,batch_json FROM formal_assurance_batches_v1 WHERE evidence_batch_id=?", (batch.evidence_batch_id,)).fetchone()
            if row:
                if row["batch_hash"] != batch.batch_hash:
                    raise ValueError("EVIDENCE_BATCH_CONFLICT")
                return "ALREADY_REGISTERED", AssuranceEvidenceBatch.model_validate(json.loads(row["batch_json"]))
            connection.execute("INSERT INTO formal_assurance_batches_v1 VALUES (?, ?, ?, ?, ?)", (batch.evidence_batch_id, batch.intent_id, batch.batch_hash, _canonical(batch.model_dump(mode="json")), batch.created_at.isoformat()))
        return "REGISTERED", batch

    def batches(self, intent_id: str) -> list[AssuranceEvidenceBatch]:
        with self.store._connect() as connection:
            rows = connection.execute("SELECT batch_json FROM formal_assurance_batches_v1 WHERE intent_id=? ORDER BY created_at DESC", (intent_id,)).fetchall()
        return [AssuranceEvidenceBatch.model_validate(json.loads(row["batch_json"])) for row in rows]

    def decision(self, batch_id: str) -> AssuranceDecision | None:
        with self.store._connect() as connection:
            row = connection.execute("SELECT decision_json FROM formal_assurance_decisions_v1 WHERE evidence_batch_id=?", (batch_id,)).fetchone()
        return AssuranceDecision.model_validate(json.loads(row["decision_json"])) if row else None

    def save_decision(self, decision: AssuranceDecision) -> AssuranceDecision:
        existing = self.decision(decision.evidence_batch_id)
        if existing:
            return existing
        with self.store._lock, self.store._connect() as connection:
            connection.execute("INSERT INTO formal_assurance_decisions_v1 VALUES (?, ?, ?, ?, ?)", (decision.decision_id, decision.intent_id, decision.evidence_batch_id, _canonical(decision.model_dump(mode="json")), decision.created_at.isoformat()))
        return decision


class FormalAssuranceEvidenceService:
    """Constrained backfill and automatic-run registration implementation."""
    def __init__(self, domain: IntentDomainStore | None = None, *, allowed_root: Path | None = None):
        self.domain = domain or get_intent_domain_store()
        self.repository = FormalAssuranceEvidenceRepository(self.domain)
        # Backend source is mounted at /app while the project workspace is
        # mounted read-only-equivalent at /workspace in deployment.  Tests can
        # still provide an explicit temporary root.
        configured_root = os.getenv("IDN_FORMAL_EVIDENCE_ROOT", "/workspace/deliverables")
        self.allowed_root = (allowed_root or Path(configured_root)).resolve()

    @staticmethod
    def contract() -> TestContract:
        return TestContract()

    def _safe_root(self, relative_root: str) -> Path:
        if not relative_root or ".." in Path(relative_root).parts:
            raise ValueError("EVIDENCE_PATH_REJECTED")
        root = (self.allowed_root / relative_root).resolve(strict=True)
        try: root.relative_to(self.allowed_root)
        except ValueError as exc: raise ValueError("EVIDENCE_PATH_REJECTED") from exc
        if not root.is_dir(): raise ValueError("EVIDENCE_PATH_REJECTED")
        return root

    def _lineage(self, intent_id: str) -> dict[str, Any]:
        spec = self.domain.get_spec(intent_id)
        runtime = self.domain.get_runtime(intent_id)
        if not spec or not runtime: raise ValueError("EVIDENCE_LINEAGE_MISMATCH")
        policy_id = (runtime.active_policy_ids or [None])[0]
        execution = (self.domain.executions(intent_id) or [{}])[0]
        path = list(runtime.current_path or execution.get("selected_path") or [])
        desired_policy = ((runtime.desired_policy or {}).get("network_policy") or {}).get("policy_id")
        canonical = {"intent_id": intent_id, "revision": runtime.current_revision, "policy_id": policy_id, "desired_policy_id": desired_policy, "path": path, "active_plan": runtime.active_plan_id, "execution": runtime.active_execution_id}
        epoch = hashlib.sha256(_canonical(canonical).encode()).hexdigest()[:20]
        return {"intent_id": intent_id, "revision": runtime.current_revision, "run_id": runtime.active_execution_id or runtime.active_plan_id or "legacy-run", "policy_id": policy_id, "policy_generation": policy_id or "policy-unavailable", "desired_config_generation": desired_policy or str(runtime.current_revision), "execution_generation": runtime.active_execution_id or "execution-unavailable", "readback_generation": runtime.active_execution_id or "readback-unavailable", "assurance_generation": f"{runtime.current_revision}:{policy_id or 'none'}:{desired_policy or 'none'}", "environment_epoch": epoch, "controller_epoch": "onos-current", "topology_id": "idn-mininet-current", "path_signature": hashlib.sha256(_canonical(path).encode()).hexdigest()[:20], "capacity_record_id": "stable-schedulable-budget-49mbps", "ingress_admission_plan_id": "INGRESS-WC-20260805-03", "ingress_allocation_epoch": 3}

    def _artifact(self, root: Path, file: Path, *, sample_index: int | None = None, raw: dict[str, Any] | None = None) -> RawArtifactManifestItem:
        if file.is_symlink() or file.stat().st_size > MAX_ARTIFACT_BYTES: raise ValueError("EVIDENCE_ARTIFACT_REJECTED")
        content = file.read_bytes(); payload = raw or json.loads(content)
        return RawArtifactManifestItem(artifact_id="ART-" + hashlib.sha256(str(file.relative_to(root)).encode()).hexdigest()[:16], artifact_type="IPERF_VALIDATION_RUN_JSON" if sample_index else "STRUCTURED_READBACK_JSON", relative_location=str(file.relative_to(root)).replace("\\", "/"), sha256=hashlib.sha256(content).hexdigest(), byte_size=len(content), generated_at=payload.get("end_time"), validation_run_id=payload.get("validation_run_id"), scenario=SCENARIO if sample_index else None, sample_index=sample_index)

    def _normalize(self, raw: dict[str, Any], artifact: RawArtifactManifestItem, batch_id: str, lineage: dict[str, Any], index: int) -> AssuranceSample:
        contract = self.contract(); errors: list[str] = []
        selector = raw.get("selector") or {}; metrics = raw.get("protocol_metrics") or {}; offered = raw.get("offered_load") or {}; background = raw.get("background_metrics") or {}
        # Raw iPerf's test duration includes omit; its usable window is duration-omit.
        test_start = ((metrics.get("raw") or {}).get("start") or {}).get("test_start") or {}
        bg_received = float(background.get("received_mbps") or (raw.get("received_load") or {}).get("total_mbps", 0) - float((raw.get("received_load") or {}).get("target_mbps") or 0))
        p4_status = str((raw.get("p4_counter_evidence") or {}).get("status") or "")
        tc_status = str((raw.get("tc_evidence") or {}).get("status") or "")
        checks = [(raw.get("status") == "MEASURED", "IPERF_NOT_MEASURED"), (float(offered.get("target_mbps") or 0) == contract.target_offered_mbps, "TARGET_OFFERED_MISMATCH"), (float(offered.get("background_mbps") or 0) == contract.background_offered_mbps, "BACKGROUND_OFFERED_MISMATCH"), (int(test_start.get("blksize") or 0) == contract.packet_size_bytes, "PACKET_SIZE_MISMATCH"), (int(test_start.get("omit") or 0) == contract.warmup_seconds, "WARMUP_MISMATCH"), (int(test_start.get("duration") or 0) - int(test_start.get("omit") or 0) == contract.measurement_seconds, "MEASUREMENT_WINDOW_MISMATCH"), (str(selector.get("source_ip")) == contract.target_source_ip and str(selector.get("destination_ip")) == contract.target_destination_ip and str(selector.get("protocol")).upper() == contract.protocol and int(selector.get("destination_port") or 0) == contract.destination_port and int(selector.get("dscp") or -1) == contract.dscp, "SELECTOR_MISMATCH"), ((raw.get("path_evidence") or {}).get("same_bottleneck") is True, "NOT_SHARED_BOTTLENECK"), (bg_received > 0, "BACKGROUND_ZERO"), ((raw.get("process_cleanup") or {}).get("process_cleanup_verified") is True, "CLEANUP_UNVERIFIED"), (tc_status == "MEASURED", "TC_READBACK_UNVERIFIED"), (p4_status == "VERIFIED", "P4_CLASSIFIER_UNVERIFIED")]
        for ok, error in checks:
            if not ok: errors.append(error)
        received = float((raw.get("received_load") or {}).get("target_mbps") or 0); passed = not errors and received >= contract.threshold_mbps
        payload = {"validation_run_id": raw.get("validation_run_id"), "index": index, "received": received, "errors": errors, "artifact": artifact.sha256, "lineage": lineage["assurance_generation"]}
        return AssuranceSample(sample_id="SMP-" + hashlib.sha256(_canonical(payload).encode()).hexdigest()[:16], evidence_batch_id=batch_id, validation_run_id=str(raw.get("validation_run_id") or ""), intent_id=lineage["intent_id"], assurance_generation=lineage["assurance_generation"], scenario=SCENARIO, sample_index=index, target_offered_mbps=float(offered.get("target_mbps") or 0), target_received_mbps=received, target_loss_percent=float(metrics.get("loss_percent") or 0), background_offered_mbps=float(offered.get("background_mbps") or 0), background_received_mbps=bg_received, packet_size_bytes=int(test_start.get("blksize") or 0), warmup_seconds=int(test_start.get("omit") or 0), measurement_seconds=int(test_start.get("duration") or 0) - int(test_start.get("omit") or 0), threshold_mbps=contract.threshold_mbps, passed=passed, measurement_quality="VALID" if not errors else "INVALID", same_bottleneck=(raw.get("path_evidence") or {}).get("same_bottleneck") is True, background_nonzero=bg_received > 0, process_cleanup_verified=(raw.get("process_cleanup") or {}).get("process_cleanup_verified") is True, ingress_readback_verified=tc_status == "MEASURED", egress_readback_verified=tc_status == "MEASURED", p4_classifier_verified=p4_status == "VERIFIED", environment_epoch=lineage["environment_epoch"], policy_generation=lineage["policy_generation"], observed_at_start=str(raw.get("start_time") or ""), observed_at_end=str(raw.get("end_time") or ""), source_artifact_ids=[artifact.artifact_id], validation_errors=errors, sample_hash=hashlib.sha256(_canonical(payload).encode()).hexdigest())

    def import_backfill(self, *, intent_id: str, evidence_root: str, evidence_batch_id: str, actor: str, trace_id: str) -> tuple[str, AssuranceEvidenceBatch]:
        root = self._safe_root(evidence_root); lineage = self._lineage(intent_id)
        paths = sorted(root.glob("contention/iperf-raw/run*/iperf-contention-run*.json"))
        if len(paths) != 5: raise ValueError("INSUFFICIENT_VALID_SAMPLES")
        artifacts: list[RawArtifactManifestItem] = []; samples: list[AssuranceSample] = []; seen: set[str] = set()
        for index, path in enumerate(paths, 1):
            raw = json.loads(path.read_text(encoding="utf-8")); artifact = self._artifact(root, path, sample_index=index, raw=raw)
            if not raw.get("validation_run_id") or raw["validation_run_id"] in seen: raise ValueError("DUPLICATE_OR_MISSING_VALIDATION_RUN")
            seen.add(raw["validation_run_id"]); artifacts.append(artifact); samples.append(self._normalize(raw, artifact, evidence_batch_id, lineage, index))
        for name in ("actuator-readback.json", "plan-diff.json", "resource-ledger-after.json"):
            path = root / name
            if path.exists(): artifacts.append(self._artifact(root, path))
        raw_hashes = {item.relative_location: item.sha256 for item in artifacts}
        batch_body = {"id": evidence_batch_id, "lineage": lineage, "contract": self.contract().model_dump(), "hashes": raw_hashes, "samples": [sample.model_dump(mode="json") for sample in samples]}
        batch = AssuranceEvidenceBatch(evidence_batch_id=evidence_batch_id, **lineage, evidence_producer="mininet_probe_server", producer_version="service-validation-v1", evidence_source_type="AUTHORIZED_BACKFILL_IMPORT", test_contract=self.contract(), scenario=SCENARIO, samples=samples, readback_evidence={"ingress_admission": "actuator-readback.json", "egress_qos": "embedded tc_evidence"}, cleanup_evidence={"all_samples_verified": all(item.process_cleanup_verified for item in samples)}, raw_artifact_manifest=artifacts, raw_artifact_hashes=raw_hashes, batch_hash=hashlib.sha256(_canonical(batch_body).encode()).hexdigest(), observed_at_start=min(item.observed_at_start for item in samples), observed_at_end=max(item.observed_at_end for item in samples), valid_until=_utcnow() + timedelta(hours=24), trust_status="TRUSTED", validation_errors=[error for item in samples for error in item.validation_errors], ingestion_status="VALIDATED" if all(item.measurement_quality == "VALID" for item in samples) else "REJECTED", importer_actor=actor, trace_id=trace_id)
        status, stored = self.repository.register(batch)
        if status == "REGISTERED":
            self.domain.save_evidence(intent_id, EvidenceItem(evidence_id=batch.evidence_batch_id, intent_id=intent_id, source="formal_assurance_evidence_repository", metric="assurance_evidence_batch", value=batch.batch_hash, quality=EvidenceQuality.TRUSTED, sampled_at=batch.created_at, expires_at=batch.valid_until, correlation={"trace_id": trace_id, "assurance_generation": batch.assurance_generation}, raw={"trust_status": batch.trust_status, "ingestion_status": batch.ingestion_status, "valid_samples": sum(item.measurement_quality == "VALID" for item in samples)}))
            self.domain.record_event(intent_id, "ASSURANCE_EVIDENCE_BATCH_REGISTERED", actor=actor, reason="authorized backfill registered immutable raw evidence manifest", data={"evidence_batch_id": batch.evidence_batch_id, "batch_hash": batch.batch_hash, "trace_id": trace_id})
            self.domain.record_event(intent_id, "ASSURANCE_EVIDENCE_BATCH_VALIDATED" if batch.ingestion_status == "VALIDATED" else "ASSURANCE_EVIDENCE_BATCH_REJECTED", actor="formal_assurance_validator", reason="manifest hashes, schema, contract, and sample integrity evaluated", data={"evidence_batch_id": batch.evidence_batch_id, "validation_errors": batch.validation_errors, "trace_id": trace_id})
            self.domain.record_event(intent_id, "ASSURANCE_SAMPLES_NORMALIZED", actor="formal_assurance_normalizer", reason="raw validation runs normalized without deriving throughput from reports", data={"evidence_batch_id": batch.evidence_batch_id, "sample_count": len(samples), "valid_sample_count": sum(item.measurement_quality == "VALID" for item in samples), "trace_id": trace_id})
        return status, stored

    def register_internal_runs(self, *, intent_id: str, runs: list[dict[str, Any]], actor: str = "internal_validation_runner", trace_id: str) -> tuple[str, AssuranceEvidenceBatch]:
        """Register one complete runner-produced batch without a file-upload hop."""
        if len(runs) != 5:
            raise ValueError("INSUFFICIENT_VALID_SAMPLES")
        lineage = self._lineage(intent_id)
        generation = "AG-" + hashlib.sha256(f"{intent_id}:{_utcnow().isoformat()}".encode()).hexdigest()[:16]
        batch_id = "AEB-" + hashlib.sha256(f"{generation}:{intent_id}".encode()).hexdigest()[:16]
        lineage["assurance_generation"] = generation
        artifacts: list[RawArtifactManifestItem] = []; samples: list[AssuranceSample] = []; seen: set[str] = set()
        for index, raw in enumerate(runs, 1):
            content = _canonical(raw).encode(); run_id = str(raw.get("validation_run_id") or "")
            if not run_id or run_id in seen: raise ValueError("DUPLICATE_OR_MISSING_VALIDATION_RUN")
            seen.add(run_id)
            artifact = RawArtifactManifestItem(artifact_id="ART-" + hashlib.sha256(content).hexdigest()[:16], artifact_type="INTERNAL_VALIDATION_RUN_JSON", relative_location=f"internal/{generation}/run-{index}.json", sha256=hashlib.sha256(content).hexdigest(), byte_size=len(content), generated_at=raw.get("end_time"), producer="formal_validation_runner", validation_run_id=run_id, scenario=SCENARIO, sample_index=index)
            artifacts.append(artifact); samples.append(self._normalize(raw, artifact, batch_id, lineage, index))
        hashes = {item.relative_location: item.sha256 for item in artifacts}
        body = {"id": batch_id, "lineage": lineage, "hashes": hashes, "samples": [item.sample_hash for item in samples]}
        batch = AssuranceEvidenceBatch(evidence_batch_id=batch_id, **lineage, evidence_producer="formal_validation_runner", producer_version="formal-assurance-v2", evidence_source_type="INTERNAL_VALIDATION_RUNNER", test_contract=self.contract(), scenario=SCENARIO, samples=samples, readback_evidence={"per_sample": "embedded"}, cleanup_evidence={"all_samples_verified": all(x.process_cleanup_verified for x in samples)}, raw_artifact_manifest=artifacts, raw_artifact_hashes=hashes, batch_hash=hashlib.sha256(_canonical(body).encode()).hexdigest(), observed_at_start=min(x.observed_at_start for x in samples), observed_at_end=max(x.observed_at_end for x in samples), valid_until=_utcnow() + timedelta(hours=24), trust_status="TRUSTED", validation_errors=[e for x in samples for e in x.validation_errors], ingestion_status="VALIDATED" if all(x.measurement_quality == "VALID" for x in samples) else "REJECTED", importer_actor=actor, trace_id=trace_id)
        status, stored = self.repository.register(batch)
        if status == "REGISTERED":
            self.domain.save_evidence(intent_id, EvidenceItem(evidence_id=batch.evidence_batch_id, intent_id=intent_id, source="formal_validation_runner", metric="assurance_evidence_batch", value=batch.batch_hash, quality=EvidenceQuality.TRUSTED, sampled_at=batch.created_at, expires_at=batch.valid_until, correlation={"trace_id":trace_id,"assurance_generation":generation}, raw={"trust_status":batch.trust_status,"ingestion_status":batch.ingestion_status}))
            for event, reason, data in (("ASSURANCE_GENERATION_STARTED", "new immutable formal validation generation started", {"assurance_generation":generation}), ("ASSURANCE_EVIDENCE_BATCH_REGISTERED", "internal runner registered complete raw batch", {"evidence_batch_id":batch_id}), ("ASSURANCE_EVIDENCE_BATCH_VALIDATED" if batch.ingestion_status == "VALIDATED" else "ASSURANCE_EVIDENCE_BATCH_REJECTED", "contract and P4 evidence validated", {"evidence_batch_id":batch_id,"errors":batch.validation_errors})):
                self.domain.record_event(intent_id,event,actor=actor,reason=reason,data={**data,"trace_id":trace_id})
        return status, stored

    def reject_batch_consumption(
        self,
        *,
        intent_id: str,
        evidence_batch_id: str,
        reason_code: str,
        actor: str = "formal_assurance_auditor",
        trace_id: str,
    ) -> None:
        """Append an audit finding without mutating an immutable batch.

        Ingestion status describes whether the originally stored object passed
        the validator available at that time.  A later validator defect must
        never be repaired with an SQL update to that object.  Instead, an
        append-only consumption-rejection event prevents the batch being used
        for any subsequent decision while retaining the original bytes and its
        historical ingestion result.
        """
        self.domain.record_event(
            intent_id,
            "ASSURANCE_EVIDENCE_CONSUMPTION_REJECTED",
            actor=actor,
            reason=reason_code,
            data={
                "evidence_batch_id": evidence_batch_id,
                "reason_code": reason_code,
                "trace_id": trace_id,
                "scope": "append_only_post_registration_audit",
            },
        )

    def _consumption_rejections(self, intent_id: str) -> dict[str, str]:
        """Return later audit exclusions, without changing stored batches."""
        rejected: dict[str, str] = {}
        # ``IntentDomainStore.history`` deliberately pages the oldest 200
        # timeline events for UI rendering.  An audit exclusion is safety
        # critical and can be appended after that page, so assessment must
        # scan the append-only history rather than silently ignoring it.
        base_store = getattr(self.domain, "base_store", None)
        if base_store is None:  # lightweight unit-test domain adapter
            events = self.domain.history(intent_id)
        else:
            with base_store._connect() as connection:
                rows = connection.execute(
                    "SELECT event_json FROM intent_history_v1 WHERE intent_id=? ORDER BY sequence ASC",
                    (intent_id,),
                ).fetchall()
            events = [json.loads(row["event_json"]) for row in rows]
        for event in events:
            if event.get("event_type") != "ASSURANCE_EVIDENCE_CONSUMPTION_REJECTED":
                continue
            data = event.get("data") or {}
            batch_id = str(data.get("evidence_batch_id") or "")
            if batch_id:
                rejected[batch_id] = str(data.get("reason_code") or event.get("reason") or "EVIDENCE_CONSUMPTION_REJECTED")
        return rejected

    def assess_current(self, intent_id: str, *, trace_id: str) -> tuple[AssuranceDecision, AssuranceEvidenceBatch | None]:
        lineage = self._lineage(intent_id); batches = self.repository.batches(intent_id)
        rejected = self._consumption_rejections(intent_id)
        batch = next((item for item in batches if item.ingestion_status == "VALIDATED" and item.trust_status == "TRUSTED" and item.evidence_batch_id not in rejected), None)
        if not batch:
            reason = next(iter(rejected.values()), "INSUFFICIENT_VALID_SAMPLES")
            empty_seed = _canonical({"intent_id": intent_id, "reason": reason, "rejected": rejected})
            return AssuranceDecision(decision_id="DEC-EMPTY-" + hashlib.sha256(empty_seed.encode()).hexdigest()[:16], evidence_batch_id="", intent_id=intent_id, status="UNKNOWN", reason_code=reason, valid_sample_count=0, required_sample_count=5, passed_sample_count=0, minimum_received_mbps=None, mean_received_mbps=None, background_nonzero_all_runs=False, lineage_verified=False, freshness="UNKNOWN"), None
        # assurance_generation is intentionally a fresh validation-cycle ID;
        # configuration lineage must match, while a new cycle must not be
        # rejected merely for having a new assurance-generation identifier.
        lineage_ok = all(getattr(batch, key) == lineage[key] for key in ("revision", "policy_generation", "desired_config_generation", "execution_generation", "environment_epoch", "capacity_record_id", "ingress_admission_plan_id", "ingress_allocation_epoch", "path_signature"))
        freshness = "FRESH" if batch.valid_until > _utcnow() else "STALE"
        valid = [item for item in batch.samples if item.measurement_quality == "VALID"]
        passed = [item for item in valid if item.passed]
        values = [item.target_received_mbps for item in valid]
        if not lineage_ok: status, reason = "UNKNOWN", "EVIDENCE_LINEAGE_MISMATCH"
        elif freshness != "FRESH": status, reason = "UNKNOWN", "EVIDENCE_STALE"
        elif len(valid) < batch.test_contract.required_sample_count: status, reason = "UNKNOWN", "INSUFFICIENT_VALID_SAMPLES"
        elif len(passed) < batch.test_contract.required_pass_count: status, reason = "VIOLATED", "SLA_VIOLATED"
        else: status, reason = "SATISFIED", "WORK_CONSERVING_ADMISSION_GUARANTEE_VERIFIED"
        decision_seed = f"{batch.evidence_batch_id}:{batch.batch_hash}:{status}:{reason}"
        decision = AssuranceDecision(decision_id="DEC-" + hashlib.sha256(decision_seed.encode()).hexdigest()[:16], evidence_batch_id=batch.evidence_batch_id, intent_id=intent_id, status=status, reason_code=reason, valid_sample_count=len(valid), required_sample_count=batch.test_contract.required_sample_count, passed_sample_count=len(passed), minimum_received_mbps=min(values) if values else None, mean_received_mbps=sum(values)/len(values) if values else None, background_nonzero_all_runs=bool(valid) and all(item.background_nonzero for item in valid), lineage_verified=lineage_ok, freshness=freshness)
        return self.repository.save_decision(decision), batch

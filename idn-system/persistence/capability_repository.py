"""Durable repository for evidence-backed, epoch-scoped capabilities."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from api.store import IntentStore, get_store
from models.capability import CapabilityEvidenceStatus, EmpiricalCapability
from models.intent_domain import EvidenceQuality, IntentAssuranceResult, NetworkContext


class CapabilityRepository:
    """Never turns configured intent or an old process cache into proof."""

    def __init__(self, store: IntentStore | None = None, *, boot_id: str | None = None):
        self.store = store or get_store()
        self.boot_id = boot_id or f"BOOT-{uuid.uuid4().hex}"
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS empirical_capabilities_v1 (
                    capability_id TEXT PRIMARY KEY,
                    resource_id TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    epoch TEXT NOT NULL,
                    capability_json TEXT NOT NULL,
                    boot_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_capability_lookup
                   ON empirical_capabilities_v1(metric, epoch, observed_at DESC)"""
            )

    def record(self, capability: EmpiricalCapability) -> EmpiricalCapability:
        if capability.status != CapabilityEvidenceStatus.PROVEN:
            raise ValueError(
                "only proven observations may enter the capability repository"
            )
        if capability.boot_id != self.boot_id:
            raise ValueError("capability boot_id does not match the active process")
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO empirical_capabilities_v1(
                    capability_id, resource_id, metric, epoch, capability_json, boot_id, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    capability.capability_id,
                    capability.resource_id,
                    capability.metric,
                    capability.epoch,
                    capability.model_dump_json(),
                    capability.boot_id,
                    capability.observed_at.isoformat(),
                ),
            )
        return capability

    def record_assurance(
        self,
        assurance: IntentAssuranceResult,
        context: NetworkContext,
        *,
        resource_id: str = "intent-scope",
        validation_run_id: str | None = None,
    ) -> list[EmpiricalCapability]:
        if (
            assurance.evidence_quality != EvidenceQuality.TRUSTED
            or assurance.satisfied is not True
        ):
            return []
        if not context.epoch or assurance.context_id != context.context_id:
            return []
        result: list[EmpiricalCapability] = []
        for assessment in assurance.assessments:
            if assessment.status.value != "PASS" or not isinstance(
                assessment.observed, (int, float)
            ):
                continue
            result.append(
                self.record(
                    EmpiricalCapability(
                        resource_id=resource_id,
                        metric=assessment.objective_type.value,
                        value=float(assessment.observed),
                        unit=assessment.unit,
                        epoch=context.epoch,
                        evidence_refs=list(assessment.evidence_ids),
                        validation_run_id=validation_run_id,
                        assurance_id=assurance.assurance_id,
                        observed_at=assurance.observed_at,
                        boot_id=self.boot_id,
                    )
                )
            )
        return result

    def find(
        self,
        *,
        metric: str,
        epoch: str | None,
        resource_id: str | None = None,
    ) -> list[EmpiricalCapability]:
        if not epoch:
            return []
        clauses = ["metric=?", "epoch=?"]
        params: list[str] = [metric, epoch]
        if resource_id is not None:
            clauses.append("resource_id=?")
            params.append(resource_id)
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT capability_json, boot_id FROM empirical_capabilities_v1 WHERE "
                + " AND ".join(clauses)
                + " ORDER BY observed_at DESC",
                params,
            ).fetchall()
        now = datetime.now(timezone.utc)
        result = []
        for row in rows:
            capability = EmpiricalCapability.model_validate(
                json.loads(row["capability_json"])
            )
            if row["boot_id"] != self.boot_id or (
                capability.expires_at is not None and capability.expires_at <= now
            ):
                capability = capability.model_copy(
                    update={"status": CapabilityEvidenceStatus.STALE}
                )
            result.append(capability)
        return result

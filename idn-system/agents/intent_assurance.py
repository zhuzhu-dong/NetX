"""Evidence aggregation and deterministic per-objective assurance."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from models.intent_domain import (
    AssessmentStatus,
    ComparisonOperator,
    EvidenceItem,
    EvidenceQuality,
    IntentAssuranceResult,
    IntentLifecycle,
    IntentObjective,
    IntentSpec,
    ObjectiveAssessment,
    ObjectiveType,
)
from persistence.telemetry_store import TelemetryStore


class EvidenceAggregator:
    def __init__(self, telemetry: TelemetryStore | None = None):
        self.telemetry = telemetry or TelemetryStore()
        self.stale_seconds = float(os.getenv("IDN_INTENT_EVIDENCE_STALE_SECONDS", "120"))

    def collect(self, spec: IntentSpec) -> list[EvidenceItem]:
        service_ids = list(spec.service.get("service_ids") or [])
        evidence: list[EvidenceItem] = []
        for service_id in service_ids:
            history = self.telemetry.service_history(str(service_id), limit=20)
            if not history:
                continue
            row = history[-1]
            payload = dict(row.get("payload") or {})
            sampled_at = self._datetime(row.get("sampled_at") or payload.get("end_time"))
            quality = self._quality(str(row.get("quality_status") or payload.get("measurement_status") or ""), sampled_at)
            metrics = dict(payload.get("protocol_metrics") or {})
            throughput = self._first(metrics, "received_mbps", "receiver_mbps")
            latency = self._first(metrics, "mean_rtt_ms", "rtt_avg_ms")
            loss = self._first(metrics, "loss_percent")
            jitter = self._first(metrics, "jitter_ms", "rtt_mdev_ms")
            p4 = dict(payload.get("p4_counter_evidence") or {})
            int_observation = dict(
                payload.get("int_analysis")
                or payload.get("int")
                or payload.get("int_probe")
                or {}
            )
            tc_observation = dict(payload.get("tc_evidence") or {})
            bottleneck_link = (
                payload.get("bottleneck_link")
                or int_observation.get("bottleneck_link")
                or tc_observation.get("bottleneck_link")
                or (payload.get("cross_layer_evidence") or {}).get(
                    "bottleneck_link"
                )
            )
            connectivity = self._connectivity(payload, p4)
            values = {
                "throughput_mbps": (throughput, "Mbps"),
                "latency_ms": (latency, "ms"),
                "packet_loss_pct": (loss, "%"),
                "jitter_ms": (jitter, "ms"),
                "connectivity": (connectivity, None),
                "availability_pct": (
                    round(
                        sum(
                            1 for sample in history
                            if str(sample.get("quality_status") or "").upper()
                            in {"MEASURED", "VERIFIED", "TRUSTED"}
                        ) / len(history) * 100.0,
                        3,
                    ),
                    "%",
                ),
            }
            for metric, (value, unit) in values.items():
                item_quality = quality if value is not None else EvidenceQuality.MISSING
                evidence.append(
                    EvidenceItem(
                        intent_id=spec.intent_id,
                        source=f"service_validation:{service_id}",
                        metric=metric,
                        value=value,
                        unit=unit,
                        quality=item_quality,
                        sampled_at=sampled_at,
                        correlation={
                            "service_id": service_id,
                            "validation_run_id": payload.get("validation_run_id"),
                            "policy_id": payload.get("policy_id"),
                            "workload_id": row.get("workload_id"),
                        },
                        raw={
                            "measurement_status": payload.get("measurement_status"),
                            "evidence_status": payload.get("evidence_status"),
                            "p4_status": p4.get("status"),
                            "tc_status": (payload.get("tc_evidence") or {}).get("status"),
                            "bottleneck_link": bottleneck_link,
                            "int_path": (
                                int_observation.get("switch_path")
                                or int_observation.get("path")
                                or payload.get("selected_path")
                                or []
                            ),
                            "queue_state": tc_observation.get("queue_state") or {},
                        },
                    )
                )
        self._collect_policy_and_legacy_evidence(spec, evidence)
        return evidence

    def _collect_policy_and_legacy_evidence(
        self, spec: IntentSpec, evidence: list[EvidenceItem]
    ) -> None:
        store = self.telemetry.store
        with store._connect() as connection:
            rows = connection.execute(
                "SELECT policy_id, yang_json FROM policies WHERE intent_id=? ORDER BY updated_at DESC",
                (spec.intent_id,),
            ).fetchall()
        import json

        for row in rows[:1]:
            try:
                yang = json.loads(row["yang_json"])
            except (TypeError, ValueError):
                yang = {}
            policy = yang.get("network_policy") or {}
            action = policy.get("action") or {}
            qos = action.get("qos_params") or {}
            observed_priority = (
                (policy.get("metadata") or {}).get("priority")
                or qos.get("priority")
                or qos.get("dscp_value")
            )
            if observed_priority is not None:
                evidence.append(EvidenceItem(
                    intent_id=spec.intent_id,
                    source=f"network_policy:{row['policy_id']}",
                    metric="priority",
                    value=observed_priority,
                    quality=EvidenceQuality.TRUSTED,
                    sampled_at=datetime.now(timezone.utc),
                    correlation={"policy_id": row["policy_id"]},
                ))
        record = store.get(spec.intent_id)
        report = ((record or {}).get("result") or {}).get("status_verification_report") or {}
        passed = report.get("passed")
        if passed is not None:
            now = datetime.now(timezone.utc)
            evidence.append(EvidenceItem(
                intent_id=spec.intent_id,
                source="status_verifier",
                metric="isolation",
                value=bool(passed),
                quality=EvidenceQuality.TRUSTED,
                sampled_at=now,
            ))
            evidence.append(EvidenceItem(
                intent_id=spec.intent_id,
                source="status_verifier",
                metric="connectivity",
                value=bool(passed),
                quality=EvidenceQuality.TRUSTED,
                sampled_at=now,
            ))

    def _quality(self, status: str, sampled_at: datetime | None) -> EvidenceQuality:
        if sampled_at is None:
            return EvidenceQuality.MISSING
        age = (datetime.now(timezone.utc) - sampled_at).total_seconds()
        if age > self.stale_seconds:
            return EvidenceQuality.STALE
        status = status.upper()
        if status in {"MEASURED", "VERIFIED", "TRUSTED"}:
            return EvidenceQuality.TRUSTED
        if status in {"DEGRADED", "PARTIAL", "NO_PREVIOUS_SAMPLE"}:
            return EvidenceQuality.DEGRADED
        if status in {"NO_SAMPLE", "TELEMETRY_UNAVAILABLE", "UNAVAILABLE", ""}:
            return EvidenceQuality.MISSING
        return EvidenceQuality.UNTRUSTED

    @staticmethod
    def _datetime(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _first(values: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = values.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _connectivity(payload: dict[str, Any], p4: dict[str, Any]) -> bool | None:
        status = str(payload.get("measurement_status") or payload.get("status") or "").upper()
        if status == "MEASURED":
            return True
        if int(p4.get("delta_packets") or 0) > 0 or int(p4.get("after_packets") or 0) > 0:
            return True
        if status in {"FAILED", "UNREACHABLE"}:
            return False
        return None


class IntentAssuranceAgent:
    agent_id = "intent_assurance"

    def __init__(self, aggregator: EvidenceAggregator | None = None):
        self.aggregator = aggregator or EvidenceAggregator()
        self.min_bandwidth_tolerance_ratio = float(
            os.getenv("IDN_QOS_KPI_TOLERANCE_RATIO", "0.95")
        )

    def assess(
        self,
        spec: IntentSpec,
        *,
        plan_id: str | None = None,
        context_id: str | None = None,
    ) -> tuple[IntentAssuranceResult, list[EvidenceItem]]:
        evidence = self.aggregator.collect(spec)
        assessments = [self._assess_objective(item, evidence) for item in spec.objectives]
        if assessments and all(item.status == AssessmentStatus.PASS for item in assessments):
            satisfied: bool | None = True
            lifecycle = IntentLifecycle.SATISFIED
        elif any(item.status == AssessmentStatus.FAIL for item in assessments):
            satisfied = False
            lifecycle = IntentLifecycle.ASSURANCE_FAILED
        else:
            satisfied = None
            lifecycle = IntentLifecycle.DEGRADED
        used_ids = {
            evidence_id
            for assessment in assessments
            for evidence_id in assessment.evidence_ids
        }
        quality = self._rollup_quality(
            [item for item in evidence if item.evidence_id in used_ids]
        )
        summary = (
            "all objectives are supported by trusted evidence"
            if satisfied is True
            else "one or more objectives are violated"
            if satisfied is False
            else "objective state is unknown because required evidence is missing, stale, or untrusted"
        )
        return (
            IntentAssuranceResult(
                intent_id=spec.intent_id,
                plan_id=plan_id,
                context_id=context_id,
                lifecycle=lifecycle,
                evidence_quality=quality,
                assessments=assessments,
                satisfied=satisfied,
                summary=summary,
                failure_reason="SLA_VIOLATED" if satisfied is False else None,
                failure_stage="ASSURANCE" if satisfied is False else None,
                recoverable=True if satisfied is False else None,
            ),
            evidence,
        )

    def _assess_objective(
        self, objective: IntentObjective, evidence: list[EvidenceItem]
    ) -> ObjectiveAssessment:
        metric = {
            ObjectiveType.CONNECTIVITY: "connectivity",
            ObjectiveType.MIN_BANDWIDTH: "throughput_mbps",
            ObjectiveType.MAX_BANDWIDTH: "throughput_mbps",
            ObjectiveType.MAX_LATENCY: "latency_ms",
            ObjectiveType.MAX_PACKET_LOSS: "packet_loss_pct",
            ObjectiveType.MAX_JITTER: "jitter_ms",
            ObjectiveType.AVAILABILITY: "availability_pct",
            ObjectiveType.PRIORITY: "priority",
            ObjectiveType.ISOLATION: "isolation",
        }.get(objective.type)
        candidates = [
            item for item in evidence
            if item.metric == metric and item.quality in {EvidenceQuality.TRUSTED, EvidenceQuality.DEGRADED}
        ]
        if not candidates:
            return ObjectiveAssessment(
                objective_id=objective.objective_id,
                objective_type=objective.type,
                status=AssessmentStatus.UNKNOWN,
                expected=objective.value,
                unit=objective.unit,
                reason=f"required evidence {metric} is missing, stale, or untrusted",
            )
        observed_values = [item.value for item in candidates if item.value is not None]
        if not observed_values:
            status = AssessmentStatus.UNKNOWN
            observed = None
            reason = f"{metric} contains no observed value"
        else:
            observed = min(observed_values) if objective.operator == ComparisonOperator.GTE else max(observed_values)
            effective_expected = objective.value
            if (
                objective.type == ObjectiveType.MIN_BANDWIDTH
                and objective.operator == ComparisonOperator.GTE
            ):
                effective_expected = (
                    float(objective.value) * self.min_bandwidth_tolerance_ratio
                )
            passed = self._compare(observed, objective.operator, effective_expected)
            status = AssessmentStatus.PASS if passed else AssessmentStatus.FAIL
            reason = (
                f"observed {observed} {objective.unit or ''} against "
                f"{objective.operator.value} {objective.value}"
            )
            if effective_expected != objective.value:
                reason += (
                    f" (effective acceptance threshold {effective_expected:g}, "
                    f"tolerance_ratio={self.min_bandwidth_tolerance_ratio:g})"
                )
        return ObjectiveAssessment(
            objective_id=objective.objective_id,
            objective_type=objective.type,
            status=status,
            expected=objective.value,
            observed=observed,
            unit=objective.unit,
            evidence_ids=[item.evidence_id for item in candidates],
            reason=reason,
        )

    @staticmethod
    def _compare(observed: Any, operator: ComparisonOperator, expected: Any) -> bool:
        if operator == ComparisonOperator.GTE:
            return float(observed) >= float(expected)
        if operator == ComparisonOperator.LTE:
            return float(observed) <= float(expected)
        if operator == ComparisonOperator.IN:
            return observed in expected
        if operator == ComparisonOperator.NOT_IN:
            return observed not in expected
        return observed == expected

    @staticmethod
    def _rollup_quality(evidence: list[EvidenceItem]) -> EvidenceQuality:
        if not evidence:
            return EvidenceQuality.MISSING
        qualities = {item.quality for item in evidence}
        for value in (
            EvidenceQuality.MISSING,
            EvidenceQuality.STALE,
            EvidenceQuality.UNTRUSTED,
            EvidenceQuality.DEGRADED,
        ):
            if value in qualities:
                return value
        return EvidenceQuality.TRUSTED

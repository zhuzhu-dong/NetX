"""Read-only evidence collection for an unconfirmed root cause."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from models.agent_state import ExecutionStage, GlobalAgentState, StageStatus
from models.intent import IntentStatus
from .base_node import BaseNode
from ..state_generations import audit, diagnostics_allowed, mark_degraded, violation_is_current


class CollectDiagnosticEvidenceNode(BaseNode):
    agent_id = "diagnostic_evidence_collector"
    stage = ExecutionStage.VERIFY

    @staticmethod
    def _bucket(value: Any, *, kind: str) -> str | None:
        try: number = float(value)
        except (TypeError, ValueError): return None
        if kind in {"utilization", "cpu"}:
            return "0-50" if number < 50 else "50-70" if number < 70 else "70-85" if number < 85 else "85-95" if number < 95 else "95-100"
        return "ZERO" if number <= 0 else "LOW" if number < 10 else "MATERIAL" if number < 100 else "SEVERE"

    @classmethod
    def material_evidence(cls, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Canonical root-cause facts; raw timing/counters are audit-only."""
        ignored: list[str] = []
        volatile = {"collected_at", "observed_at", "generated_at", "timestamp", "request_id", "trace_id", "analysis_id", "provider_request_id", "diagnostic_round", "latency_ms"}
        def normalize(value: Any, key: str = "") -> Any:
            lowered = key.lower()
            if lowered in volatile or lowered.endswith("_at"):
                ignored.append(key); return None
            if isinstance(value, dict):
                return {str(k): normalized for k, v in sorted(value.items()) if (normalized := normalize(v, str(k))) is not None}
            if isinstance(value, (list, tuple, set)):
                normalized = [normalize(item, key) for item in value]
                return sorted((item for item in normalized if item is not None), key=lambda item: json.dumps(item, sort_keys=True, default=str))
            if isinstance(value, (int, float)):
                if any(token in lowered for token in ("packet", "byte", "counter", "absolute")) and "delta" not in lowered:
                    ignored.append(key); return None
                if "util" in lowered: return cls._bucket(value, kind="utilization")
                if "cpu" in lowered: return cls._bucket(value, kind="cpu")
                if any(token in lowered for token in ("drop", "overlimit", "delta")): return cls._bucket(value, kind="delta")
            return value
        material = normalize(payload) or {}
        return material, sorted(set(ignored))

    @staticmethod
    def evidence_hash(payload: dict[str, Any]) -> str:
        """Hash material evidence only; timestamps never manufacture progress."""
        material, _ = CollectDiagnosticEvidenceNode.material_evidence(payload)
        encoded = json.dumps(material, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def loop_guard_key(*, intent_id: str, diagnostic: dict[str, Any], route_decision: str) -> str:
        """Identity of a no-progress orchestration step, not a time-based retry."""
        material = {
            "intent_id": intent_id,
            "environment_epoch": diagnostic.get("environment_epoch"),
            "context_hash": diagnostic.get("context_hash"),
            "root_cause_status": diagnostic.get("root_cause_status"),
            "evidence_hash": diagnostic.get("evidence_hash"),
            "current_node": "collect_diagnostics",
            "route_decision": route_decision,
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        report = state.verification_report or {}
        root = report.get("root_cause_report") or {}
        prior = dict(state.diagnostic_state or {})
        max_rounds = int(os.getenv("IDN_MAX_DIAGNOSTIC_ROUNDS", "3"))
        completed_rounds = int(prior.get("diagnostic_round") or 0)
        if not violation_is_current(state):
            report.update({
                "verification_state": "ASSURANCE_REQUIRED",
                "route_decision": "STALE_VIOLATION_BASIS",
                "diagnostics_required": False,
            })
            state.verification_report = report
            state.diagnostics_required = False
            return self._end_log(state, start, StageStatus.SUCCESS, "STALE_VIOLATION_BASIS: diagnostic collection refused")
        if not diagnostics_allowed(state):
            report.update({
                "verification_state": "ASSURANCE_REQUIRED",
                "route_decision": "STALE_ROOT_CAUSE_BASIS",
                "diagnostics_required": False,
            })
            state.verification_report = report
            state.diagnostics_required = False
            return self._end_log(state, start, StageStatus.SUCCESS, "STALE_ROOT_CAUSE_BASIS: diagnostic collection refused")
        # This gate precedes collection and every evidence branch.  It is the
        # final defence against an accidental round 4/3 trace.
        if completed_rounds >= max_rounds:
            report.update({"verification_state": "DEGRADED_ROOT_CAUSE_UNCONFIRMED", "orchestration_phase": "DEGRADED_AWAITING_NEW_MATERIAL_EVIDENCE", "route_decision": "END_DEGRADED", "terminal_reason": "ROOT_CAUSE_UNCONFIRMED_AFTER_MAX_DIAGNOSTICS", "healing_ready": False, "diagnostics_required": False, "terminal": True})
            state.root_cause_status = "UNCONFIRMED"
            state.diagnostics_required = False
            mark_degraded(state, reason="ROOT_CAUSE_UNCONFIRMED_AFTER_MAX_DIAGNOSTICS", next_action="WAIT_FOR_NEW_MATERIAL_EVIDENCE")
            state.verification_report = report
            return self._end_log(state, start, StageStatus.SUCCESS, f"DIAGNOSTIC_LIMIT_REACHED: {completed_rounds}/{max_rounds}")
        from api.telemetry import collect_current_telemetry
        from infra.network_context_builder import NetworkContextBuilder

        telemetry = await collect_current_telemetry(include_probe=False, include_iperf=False)
        context = await NetworkContextBuilder().build_async()
        int_analysis = report.get("int_analysis") or {}
        payload = {
            "environment_epoch": (telemetry.get("raw") or {}).get("environment_epoch"),
            "context_hash": context.material_state_hash,
            "current_path": int_analysis.get("switch_path") or int_analysis.get("device_path") or [],
            "candidate_paths": context.candidate_paths,
            "link_utilization": context.utilization,
            "tc": context.tc_state,
            "p4": context.p4_state,
            "onos": (telemetry.get("raw") or {}).get("onos") or {},
            "faults": context.congestion,
            "workloads": context.active_intents,
            "resource_ledger": context.reservations,
            "assurance_samples": report.get("samples") or [],
            "missing_evidence": root.get("missing_evidence") or [],
        }
        material, ignored_changes = self.material_evidence(payload)
        current_hash = self.evidence_hash(payload)
        previous_hash = prior.get("material_evidence_hash") or prior.get("evidence_hash")
        new_evidence = bool(previous_hash is None or current_hash != previous_hash)
        round_no = completed_rounds + 1
        assert round_no <= max_rounds
        state.diagnostic_state = {
            "diagnostic_round": round_no,
            "max_diagnostic_rounds": max_rounds,
            "previous_material_evidence_hash": previous_hash,
            "material_evidence_hash": current_hash,
            "raw_evidence_hash": hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest(),
            "evidence_hash": current_hash, "new_evidence": new_evidence,
            "new_material_evidence": new_evidence,
            "raw_evidence": payload, "material_evidence": material,
            "material_changes": ["material_evidence_hash"] if new_evidence and previous_hash else [],
            "ignored_changes": ignored_changes,
            "environment_epoch": payload["environment_epoch"],
            "context_hash": payload["context_hash"],
            "missing_evidence": payload["missing_evidence"],
            "diagnostic_status": "COLLECTED" if new_evidence else "NO_NEW_EVIDENCE",
            "root_cause_status": "PENDING",
            "final_reanalysis_pending": bool(round_no == max_rounds and new_evidence),
            "analysis_id": root.get("analysis_id") or root.get("report_id"),
            "diagnostic_basis_root_cause_id": state.root_cause_analysis_id,
        }
        next_route = "ROOT_CAUSE_REANALYSIS" if new_evidence else "COLLECT_NEXT_DIAGNOSTIC_EVIDENCE"
        guard_key = self.loop_guard_key(
            intent_id=state.intent_id, diagnostic=state.diagnostic_state, route_decision=next_route
        )
        loop_guard_triggered = bool(
            not new_evidence and prior.get("loop_guard_key") == guard_key
        )
        state.diagnostic_state["loop_guard_key"] = guard_key
        state.diagnostic_state["loop_guard_triggered"] = loop_guard_triggered
        report.update({
            "orchestration_phase": "DIAGNOSTIC_EVIDENCE_COLLECTION",
            "route_decision": next_route,
            "diagnostic_round": round_no,
            "max_diagnostic_rounds": max_rounds,
            "material_evidence_hash": current_hash, "new_material_evidence": new_evidence,
            "ignored_changes": ignored_changes,
            "missing_evidence": payload["missing_evidence"],
            "diagnostics_required": True,
            "healing_ready": False,
            "healing_executed": False,
            "terminal": False,
            "loop_guard_triggered": loop_guard_triggered,
        })
        if loop_guard_triggered or (round_no >= max_rounds and not new_evidence):
            report.update({
                "verification_state": "DEGRADED_ROOT_CAUSE_UNCONFIRMED",
                "orchestration_phase": "DEGRADED_AWAITING_DIAGNOSTICS",
                "route_decision": "END_DEGRADED",
                "no_progress_reason": "ORCHESTRATION_LOOP_GUARD_TRIGGERED" if loop_guard_triggered else "DIAGNOSTIC_ROUND_LIMIT_REACHED",
            })
            state.diagnostics_required = False
            mark_degraded(state, reason="DIAGNOSTIC_ROUND_LIMIT_REACHED", next_action="OPERATOR_REVIEW")
        state.verification_report = report
        audit(state, "DIAGNOSTIC_STATE_RESET" if round_no == 1 else "DIAGNOSTIC_COLLECTION", "read-only diagnostic evidence", diagnostic_round=round_no)
        return self._end_log(
            state, start, StageStatus.SUCCESS,
            f"DIAGNOSTIC_COLLECTION round={round_no}/{max_rounds}; new_evidence={new_evidence}",
        )

"""
orchestrator/nodes/verify_status_node.py
=========================================

效果验证节点 (Verify Status Node) —— STAV 闭环的 VERIFY 阶段。

# 系统角色
执行策略下发后的效果验证闭环：
1. 等待流量稳定 + 通过 MCP 采集 INT/计数器遥测数据
2. KPI 比对（时延、丢包率、带宽、抖动）
3. 验证失败时尝试自主拥塞自愈（切换备用路径）
4. 自愈后重新验证
5. 记录经验记忆和根因分析追踪

# 关键决策路径
- 验证通过 + 更多策略 → _prepare_next_policy() → 返回 SUCCESS
- 验证通过 + 所有策略完成 → state.current_status = SUCCESS
- 验证失败 + 自愈成功 → 以自愈后的 KPI 为准
- 验证失败 + 自愈失败/不适用 → 递增 retry_count + 触发 ReAct 回溯

# 输入/输出
- 输入：state.yang_draft + state.execution_result + state.intent_plan
- 输出：state.verification_passed, state.verification_report, state.telemetry_data
- 必填字段校验：yang_draft, execution_result
"""
from __future__ import annotations

import logging
import time
import os
import hashlib
import json
from datetime import datetime
from typing import Any

from models.agent_state import (
    ExecutionLogEntry,
    ExecutionStage,
    GlobalAgentState,
    StageStatus,
)
from models.intent import IntentStatus

from .base_node import BaseNode
from ..state_generations import (
    audit,
    begin_assurance,
    can_enter_assurance,
    can_confirm_violation,
    ensure_execution_generation,
    mark_degraded,
    mark_readback_verified,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 基础节点类
# ──────────────────────────────────────────────


class VerifyStatusNode(BaseNode):
    """
    效果验证节点 (Status Verifier)
    职责: 等待流量稳定 → 通过MCP采集遥测数据 → KPI比对 → 记录结果
          验证失败时触发ReAct回溯（由graph.py的条件边处理）

    输入: state.yang_draft + state.execution_result
    输出: state.verification_passed + state.verification_report
    """
    agent_id = "status_verifier"
    stage = ExecutionStage.VERIFY
    _required_fields = ("yang_draft", "execution_result")
    _INVALID_MEASUREMENT_MARKERS = (
        "no route to host",
        "connection refused",
        "infrastructure unavailable",
        "measurement process aborted",
        "invalid counter window",
    )

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        state.current_status = IntentStatus.VERIFYING
        try:
            from agents.status_verifier import StatusVerifierAgent

            # A new execution effect starts a clean derived-state generation
            # before any verifier or root-cause branch can inspect it.
            ensure_execution_generation(state, reason="execution entering readback")

            result = await StatusVerifierAgent().verify(
                intent_id=state.intent_id,
                yang_draft=state.yang_draft,
                execution_result=state.execution_result or {},
                intent_plan=state.intent_plan,
                analyze_root_cause=False,
            )
            # Assurance is forbidden until the execution plane has a concrete
            # ONOS/P4Runtime/TC readback proof.  A 409 mismatch is desired vs
            # observed drift, not a throughput sample and not a root cause.
            readback_state = str(result.verification_state or "").upper()
            if readback_state in {
                "EXECUTION_READBACK_MISMATCH", "EXECUTION_READBACK_UNAVAILABLE",
            }:
                # Desired ownership is established by a successful deployment,
                # not by later business assurance.  Keeping a version-0 CREATE
                # staged after the actuator has created P4/TC leaves the formal
                # reconciler without an owner for a repairable readback drift.
                # Commit it as CURRENT/MANAGED while retaining the truthful
                # degraded Intent and unverified outcome; the reconciler can
                # then observe, repair, or compensate it.
                if (
                    readback_state in {
                        "EXECUTION_READBACK_MISMATCH",
                        "EXECUTION_READBACK_UNAVAILABLE",
                    }
                    and str((state.execution_result or {}).get("status") or "").upper() == "SUCCESS"
                ):
                    self._commit_policy(state)
                state.execution_readback_status = readback_state
                state.execution_mismatch = dict(result.config_verification or {})
                state.reconciliation_required = readback_state == "EXECUTION_READBACK_MISMATCH"
                state.measurement_status = "WAITING_FOR_EXECUTION_VERIFICATION"
                state.violation_confirmed = False
                state.violation_id = None
                state.root_cause_status = "NOT_STARTED"
                state.root_cause_analysis_id = None
                state.diagnostics_required = False
                state.healing_ready = False
                state.verification_passed = False
                state.verification_report = result.to_dict()
                state.verification_report.update({
                    "phase": "execution_readback",
                    "reconciliation_required": state.reconciliation_required,
                    "next_action": (
                        "RECONCILE_OBSERVED_CONFIG" if state.reconciliation_required
                        else "WAIT_FOR_READBACK_AVAILABILITY"
                    ),
                    "violation_confirmed": False,
                    "diagnostics_required": False,
                    "healing_ready": False,
                    "desired_ownership": (
                        "CURRENT_MANAGED_DEPLOYED_BUT_UNASSURED"
                        if (
                            readback_state in {
                                "EXECUTION_READBACK_MISMATCH",
                                "EXECUTION_READBACK_UNAVAILABLE",
                            }
                            and str((state.execution_result or {}).get("status") or "").upper() == "SUCCESS"
                        )
                        else "NOT_COMMITTED"
                    ),
                })
                self._current_policy_result(state)["status_verification_report"] = state.verification_report
                mark_degraded(
                    state,
                    reason=readback_state,
                    next_action=state.verification_report["next_action"],
                )
                return self._end_log(
                    state, start, StageStatus.SUCCESS,
                    f"{readback_state}: SLA assurance was not started",
                )
            mark_readback_verified(state)
            if not can_enter_assurance(state):
                raise RuntimeError("current execution readback is required before assurance")
            begin_assurance(state, reason="verified execution readback")
            state.telemetry_data = result.telemetry
            state.verification_passed = result.passed
            state.verification_report = result.to_dict()
            self._attach_pre_execution_telemetry(state.verification_report, state)
            self._current_policy_result(state)["status_verification_report"] = state.verification_report

            if result.passed:
                kpi = result.telemetry.kpi
                # A previous compile/delivery retry may have populated an
                # error message. Once final KPI verification succeeds that
                # diagnostic is historical evidence, not the terminal error
                # shown to users.
                state.error_message = None
                state.react_state.last_failure_reason = None
                state.react_state.last_failure_stage = None
                await self._retire_superseded_classifiers(state, state.verification_report)
                self._record_experience_memory(state, state.verification_report)
                self._commit_policy(state)
                completed_index = state.current_policy_index
                if self._prepare_next_policy(state):
                    state.current_status = IntentStatus.ACTING
                    state.next_action = "PROCESS_NEXT_POLICY"
                    return self._end_log(
                        state, start, StageStatus.SUCCESS,
                        f"KPI验证通过: policy={completed_index + 1}/{len(state.yang_policies)}, "
                        f"continue_next_policy={state.current_policy_index + 1}",
                    )

                state.current_status = IntentStatus.SUCCESS
                packet_loss = (
                    "-" if kpi.packet_loss_rate is None
                    else f"{kpi.packet_loss_rate:.4f}"
                )
                return self._end_log(
                    state, start, StageStatus.SUCCESS,
                    f"KPI验证通过: 时延={kpi.latency_ms}ms, "
                    f"吞吐量={kpi.throughput_mbps}Mbps, "
                        f"丢包率={packet_loss}",
                    )
            else:
                # Bounded measurement confirmation is deliberately read-only:
                # no planner, executor, policy or TC write is called here.
                # One low sample is not a persistent SLA violation.
                samples: list[dict[str, Any]] = []
                valid: list[Any] = []
                first_sample = self._assurance_sample(result, sample_id=1)
                samples.append(first_sample)
                if first_sample["valid"]:
                    valid.append(result)
                for sample_id in range(2, 4):
                    confirmation = await StatusVerifierAgent().verify(
                        intent_id=state.intent_id,
                        yang_draft=state.yang_draft,
                        execution_result=state.execution_result or {},
                        intent_plan=state.intent_plan,
                        analyze_root_cause=False,
                    )
                    sample = self._assurance_sample(confirmation, sample_id=sample_id)
                    samples.append(sample)
                    if sample["valid"]:
                        valid.append(confirmation)
                passed_samples = [item for item in valid if item.passed]
                failed_samples = [item for item in valid if not item.passed]
                decision = self._confirmation_decision(valid)
                confirmation_report = result.to_dict()
                confirmation_report.update({
                    "phase": "CONFIRMING_VIOLATION",
                    "attempt_index": len(samples), "max_attempts": 3,
                    "samples": samples, "valid_sample_count": len(valid),
                    "persistence_confirmed": decision["persistence_confirmed"],
                    "threshold_mbps": self._threshold_mbps(result),
                    "measurement_quality": {
                        "valid": len(valid) == 3,
                        "invalid_sample_count": len(samples) - len(valid),
                    },
                })
                state.valid_assurance_sample_count = len(valid)
                state.assurance_samples = samples
                for sample in samples:
                    sample.update({
                        "intent_id": state.intent_id,
                        "revision": state.revision,
                        "run_id": state.run_id,
                        "assurance_id": state.assurance_id,
                        "assurance_generation": state.assurance_generation,
                        "basis_execution_generation": state.execution_generation,
                    })
                    audit(
                        state,
                        "ASSURANCE_SAMPLE_RECORDED" if sample["valid"] else "ASSURANCE_SAMPLE_REJECTED",
                        sample.get("quality_reason") or "assurance sample",
                        sample_id=sample["sample_id"], assurance_id=state.assurance_id,
                        assurance_generation=state.assurance_generation,
                        received_mbps=sample.get("received_mbps"),
                        threshold_mbps=sample.get("threshold_mbps"), valid=sample["valid"],
                    )
                if decision["verification_state"] == "MEASUREMENT_INCONCLUSIVE":
                    confirmation_report["verification_state"] = decision["verification_state"]
                    confirmation_report["terminal_reason"] = "INSUFFICIENT_VALID_SAMPLES"
                    state.verification_report = confirmation_report
                    state.violation_confirmed = False
                    state.measurement_status = "INCONCLUSIVE"
                    mark_degraded(
                        state,
                        reason="INSUFFICIENT_VALID_SAMPLES",
                        next_action="MEASUREMENT_CONFIRMATION",
                    )
                    return self._end_log(state, start, StageStatus.SUCCESS, "MEASUREMENT_INCONCLUSIVE: insufficient valid assurance samples")
                if decision["verification_state"] == "SATISFIED_WITH_TRANSIENT_WARNING":
                    confirmation_report.update({"verification_state": decision["verification_state"], "transient_warning": True})
                    state.verification_passed = True
                    state.verification_report = confirmation_report
                    state.current_status = IntentStatus.SUCCESS
                    self._commit_policy(state)
                    return self._end_log(state, start, StageStatus.SUCCESS, "SATISFIED_WITH_TRANSIENT_WARNING: later independent samples recovered")
                # Only now is persistent violation established; root cause and
                # healing are eligible.  Low-confidence hypotheses stay pending.
                result = failed_samples[-1]
                state.measurement_status = "COMPLETED"
                if not can_confirm_violation(state):
                    state.violation_confirmed = False
                    state.violation_id = None
                    mark_degraded(
                        state,
                        reason="ASSURANCE_VIOLATION_BASIS_INVALID",
                        next_action="MEASUREMENT_CONFIRMATION",
                    )
                    confirmation_report.update({
                        "verification_state": "MEASUREMENT_INCONCLUSIVE",
                        "terminal_reason": "ASSURANCE_VIOLATION_BASIS_INVALID",
                    })
                    state.verification_report = confirmation_report
                    return self._end_log(state, start, StageStatus.SUCCESS, "MEASUREMENT_INCONCLUSIVE: violation basis is not current")
                state.violation_confirmed = True
                state.violation_id = hashlib.sha256(
                    f"{state.intent_id}:{state.assurance_id}:{state.assurance_generation}".encode()
                ).hexdigest()[:24]
                state.violation_basis_assurance_generation = state.assurance_generation
                audit(state, "VIOLATION_CONFIRMED", "persistent valid SLA samples", violation_id=state.violation_id)
                diagnostic = state.diagnostic_state or {}
                root_key = hashlib.sha256(json.dumps({
                    "intent_id": state.intent_id,
                    "revision": state.current_policy_index,
                    "environment_epoch": diagnostic.get("environment_epoch"),
                    "context_hash": diagnostic.get("context_hash"),
                    "material_evidence_hash": diagnostic.get("material_evidence_hash"),
                    "prompt_version": "root-cause-v1",
                }, sort_keys=True, default=str).encode("utf-8")).hexdigest()
                cached = diagnostic.get("root_cause_report") if diagnostic.get("root_cause_llm_call_key") == root_key else None
                if cached:
                    root, suggestion, rca = cached.get("root_cause"), cached.get("react_suggestion"), dict(cached)
                else:
                    root, suggestion, rca = await StatusVerifierAgent()._analyze_root_cause(
                        result.telemetry, result.violations, state.yang_draft, result.telemetry_source
                    )
                rca = rca or {}
                rca["status"] = (
                    "CONFIRMED" if self._root_cause_confirmed(rca) else "UNCONFIRMED"
                )
                rca.setdefault("analysis_id", rca.get("report_id"))
                rca.setdefault("analysis_id", hashlib.sha256(
                    f"{state.violation_id}:{state.assurance_generation}".encode()
                ).hexdigest()[:24])
                rca.setdefault("missing_evidence", [])
                rca.setdefault("required_evidence_actions", [])
                rca["environment_epoch"] = (state.diagnostic_state or {}).get("environment_epoch")
                rca["context_hash"] = (state.diagnostic_state or {}).get("context_hash")
                state.diagnostic_state["root_cause_llm_call_key"] = root_key
                state.diagnostic_state["root_cause_report"] = {
                    **rca, "root_cause": root, "react_suggestion": suggestion,
                }
                result.root_cause, result.react_suggestion, result.root_cause_report = root, suggestion, rca
                state.root_cause_analysis_id = str(rca["analysis_id"])
                state.root_cause_basis_violation_id = state.violation_id
                state.root_cause_status = "CONFIRMED" if rca["status"] == "CONFIRMED" else "PENDING"
                state.diagnostic_basis_root_cause_id = state.root_cause_analysis_id
                confirmation_report.update({"phase": "VIOLATION_CONFIRMED", "root_cause_status": rca["status"], "root_cause_report": rca})
                state.verification_report = confirmation_report
                if rca["status"] != "CONFIRMED":
                    confirmation_report["verification_state"] = "VIOLATION_CONFIRMED_ROOT_CAUSE_PENDING"
                    confirmation_report["orchestration_phase"] = "DIAGNOSTICS_REQUIRED"
                    confirmation_report["route_decision"] = "COLLECT_DIAGNOSTIC_EVIDENCE"
                    confirmation_report["diagnostics_required"] = True
                    confirmation_report["healing_ready"] = False
                    confirmation_report["healing_executed"] = False
                    state.diagnostics_required = True
                    state.healing_ready = False
                    state.current_status = IntentStatus.DEGRADED
                    return self._end_log(state, start, StageStatus.SUCCESS, "VIOLATION_CONFIRMED_ROOT_CAUSE_PENDING: evidence is insufficient for healing")
                # A retry may have recompiled a plan with a new ID but the
                # exact same desired P4/ONOS/TC effect.  Treat the resulting
                # failed KPI as a terminal feasibility decision, never as a
                # reason to compile and execute that same effect again.
                execution = state.execution_result or {}
                if (
                    state.react_state.retry_count > 0
                    and execution.get("executor_status") == "SKIPPED_NOOP"
                ):
                    confirmation_report["healing_candidate_skipped"] = {
                        "reason_code": "NO_DATAPLANE_CHANGE",
                        "plan_diff": "NO_OP_PLAN",
                        "search_status": "CONTINUE",
                    }
                    state.verification_report = confirmation_report
                await self._rollback_failed_update(state)
                self._record_root_cause_trace(state, result)
                healing_result = await self._attempt_auto_heal(state, result)
                if (
                    healing_result
                    and healing_result.get("status") == "unsatisfiable"
                    and healing_result.get("healing_search_status") == "EXHAUSTED"
                ):
                    terminal_report = confirmation_report
                    terminal_report["auto_healing"] = healing_result
                    terminal_report["verification_state"] = "UNSATISFIABLE"
                    terminal_report["terminal_reason"] = (
                        healing_result.get("reason")
                        or "NO_FEASIBLE_HEALING_ACTION"
                    )
                    state.verification_report = terminal_report
                    state.verification_passed = False
                    state.current_status = IntentStatus.UNSATISFIABLE
                    state.error_message = terminal_report["terminal_reason"]
                    state.react_state.last_failure_reason = terminal_report["terminal_reason"]
                    state.react_state.last_failure_stage = "verify"
                    self._current_policy_result(state)["status_verification_report"] = terminal_report
                    self._current_policy_result(state)["auto_healing"] = healing_result
                    self._record_experience_memory(state, terminal_report)
                    return self._end_log(
                        state,
                        start,
                        StageStatus.SUCCESS,
                        "UNSATISFIABLE: deterministic healing evaluation found no safe action with expected effect",
                        artifacts={"auto_healing": healing_result},
                    )
                if healing_result and healing_result.get("status") == "ok":
                    pre_heal_report = result.to_dict()
                    self._attach_pre_execution_telemetry(pre_heal_report, state)
                    repaired = await StatusVerifierAgent().verify(
                        intent_id=state.intent_id,
                        yang_draft=state.yang_draft,
                        execution_result=state.execution_result or {},
                        intent_plan=state.intent_plan,
                    )
                    repaired_report = repaired.to_dict()
                    self._attach_pre_execution_telemetry(repaired_report, state)
                    from api.int_analysis import build_healing_comparison

                    healing_comparison = build_healing_comparison(
                        pre_heal_report,
                        repaired_report,
                        healing_result,
                    )
                    healing_result["comparison"] = healing_comparison
                    repaired_report["auto_healing"] = healing_result
                    repaired_report["pre_heal_verification"] = pre_heal_report
                    repaired_report["healing_comparison"] = healing_comparison
                    state.telemetry_data = repaired.telemetry
                    state.verification_passed = repaired.passed
                    state.verification_report = repaired_report
                    self._current_policy_result(state)["status_verification_report"] = repaired_report
                    self._current_policy_result(state)["auto_healing"] = healing_result

                    if repaired.passed:
                        kpi = repaired.telemetry.kpi
                        state.error_message = None
                        state.react_state.last_failure_reason = None
                        state.react_state.last_failure_stage = None
                        await self._retire_superseded_classifiers(state, repaired_report)
                        self._record_experience_memory(state, repaired_report)
                        self._commit_policy(state)
                        completed_index = state.current_policy_index
                        if self._prepare_next_policy(state):
                            state.current_status = IntentStatus.ACTING
                            state.next_action = "PROCESS_NEXT_POLICY"
                            return self._end_log(
                                state, start, StageStatus.SUCCESS,
                                f"拥塞自愈后KPI验证通过: policy={completed_index + 1}/{len(state.yang_policies)}, "
                                f"continue_next_policy={state.current_policy_index + 1}",
                                artifacts={"auto_healing": healing_result},
                            )

                        state.current_status = IntentStatus.SUCCESS
                        return self._end_log(
                            state, start, StageStatus.SUCCESS,
                            f"自动自愈成功: 验证通过 → 时延={repaired.telemetry.kpi.latency_ms}ms",
                        )

                state.current_status = IntentStatus.FAILED
                self._record_experience_memory(state, state.verification_report or result.to_dict())
                state.react_state.retry_count += 1
                state.react_state.last_failure_reason = "; ".join(result.violations)
                state.react_state.last_failure_stage = "verify"
                if not state.react_state.can_retry:
                    # Retry exhaustion by itself does not prove that every
                    # safe candidate was evaluated.  Keep the failure
                    # inspectable instead of falsely claiming UNSATISFIABLE.
                    state.current_status = IntentStatus.FAILED
                    state.error_message = "HEALING_RETRY_LIMIT_REACHED"
                    terminal_report = state.verification_report or result.to_dict()
                    terminal_report["verification_state"] = "HEALING_EVALUATING"
                    terminal_report["terminal_reason"] = "HEALING_RETRY_LIMIT_REACHED"
                    state.verification_report = terminal_report

                try:
                    from api.a2a_trace import record_react_trigger

                    record_react_trigger(
                        state,
                        source_agent="status_verifier",
                        target_agent="policy_compiler",
                        stage=self.stage,
                        failure_reason=state.react_state.last_failure_reason,
                        observation_data={
                            "violations": list(result.violations),
                            "root_cause": result.root_cause,
                            "root_cause_report": getattr(result, "root_cause_report", None),
                            "telemetry_source": result.telemetry_source,
                        },
                        retry_suggestion=(
                            result.react_suggestion
                            or "将验证失败上下文交给 PolicyCompiler 重新生成策略。"
                        ),
                    )
                except Exception:
                    pass

                logger.warning(
                    f"[{self.agent_id}] KPI验证失败 (第{state.react_state.retry_count}次): "
                    f"{result.violations}"
                )
                return self._end_log(
                    state, start, StageStatus.SUCCESS,
                    f"KPI验证失败 (第{state.react_state.retry_count}次): "
                    f"{len(result.violations)}项不达标",
                )

        except Exception as e:
            await self._rollback_failed_update(state)
            state.error_message = f"遥测验证异常: {str(e)}"
            logger.error(f"[{self.agent_id}] {state.error_message}")
            state.verification_passed = False
            state.react_state.retry_count += 1
            state.react_state.last_failure_reason = str(e)
            self._current_policy_result(state)["status_verification_error"] = str(e)

            return self._end_log(
                state, start, StageStatus.FAILED,
                "遥测验证异常",
                error=str(e),
            )

    @classmethod
    def _assurance_sample(cls, result: Any, *, sample_id: int) -> dict[str, Any]:
        """Serialize one independent, read-only assurance window and gate it.

        The service validator owns the actual iperf duration and counters.  We
        preserve its raw report here, while classifying transport/infrastructure
        errors as invalid evidence rather than manufacturing a 0 Mbps sample.
        """
        report = result.to_dict()
        text = " ".join(str(item) for item in (report.get("violations") or [])).lower()
        state = str(report.get("verification_state") or "").upper()
        invalid_reason = next((marker for marker in cls._INVALID_MEASUREMENT_MARKERS if marker in text), None)
        if state in {"NO_SAMPLE", "CONFIG_FAILED"}:
            invalid_reason = invalid_reason or state
        kpi = report.get("kpi") or {}
        throughput = kpi.get("throughput_mbps")
        if throughput is None:
            invalid_reason = invalid_reason or "missing throughput sample"
        valid = not invalid_reason and state in {
            "CONFIRMING_VIOLATION", "MIN_BANDWIDTH_KPI_VERIFIED", "RATE_LIMIT_KPI_VERIFIED"
        }
        raw = getattr(getattr(result, "telemetry", None), "raw_int_data", {}) or {}
        probe = raw.get("policy_traffic_probe") if isinstance(raw, dict) else {}
        return {
            **report,
            "sample_id": sample_id,
            "started_at": report.get("timestamp"),
            "ended_at": datetime.utcnow().isoformat(),
            "offered_mbps": (probe or {}).get("offered_rate_mbps"),
            "received_mbps": throughput,
            "threshold_mbps": cls._threshold_mbps(result),
            "loss": kpi.get("packet_loss_rate"),
            "jitter": kpi.get("jitter_ms"),
            "p4_counter_delta": (probe or {}).get("p4_counter_delta"),
            "onos_counter_delta": (probe or {}).get("onos_counter_delta"),
            "tc_counter_delta": (probe or {}).get("tc_counter_delta"),
            "veth_counter_delta": (probe or {}).get("veth_counter_delta"),
            "environment_epoch": (raw.get("environment_epoch") if isinstance(raw, dict) else None),
            "valid": valid,
            "quality_reason": "OK" if valid else f"MEASUREMENT_INVALID: {invalid_reason}",
        }

    @staticmethod
    def _threshold_mbps(result: Any) -> float | None:
        raw = getattr(getattr(result, "telemetry", None), "raw_int_data", {}) or {}
        probe = raw.get("policy_traffic_probe") if isinstance(raw, dict) else {}
        value = (probe or {}).get("minimum_acceptable_mbps")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _root_cause_confirmed(report: dict[str, Any]) -> bool:
        """Require confidence plus deterministic telemetry before healing."""
        try:
            confidence = float(report.get("confidence") or 0)
        except (TypeError, ValueError):
            return False
        deterministic_sources = {"p4_int", "p4_direct_counter", "onos_flow_counter", "tc_qdisc", "resource_ledger"}
        evidence = report.get("evidence") or []
        has_deterministic_evidence = any(
            str(item.get("source") or "").lower() in deterministic_sources
            and str(item.get("strength") or "").lower() not in {"none", "weak"}
            for item in evidence
            if isinstance(item, dict)
        )
        return confidence >= 0.60 and has_deterministic_evidence

    @staticmethod
    def _confirmation_decision(valid: list[Any]) -> dict[str, Any]:
        """Classify the bounded, independent assurance windows.

        This function deliberately has no device-side dependencies so the
        safety semantics are directly regression-tested.
        """
        if len(valid) < 3:
            return {
                "verification_state": "MEASUREMENT_INCONCLUSIVE",
                "persistence_confirmed": False,
            }
        failures = sum(1 for item in valid if not item.passed)
        passes = len(valid) - failures
        if failures >= 2:
            return {
                "verification_state": "VIOLATION_CONFIRMED",
                "persistence_confirmed": True,
            }
        if passes >= 2:
            return {
                "verification_state": "SATISFIED_WITH_TRANSIENT_WARNING",
                "persistence_confirmed": False,
            }
        return {
            "verification_state": "MEASUREMENT_INCONCLUSIVE",
            "persistence_confirmed": False,
        }

    def _attach_pre_execution_telemetry(
        self,
        report: dict[str, Any],
        state: GlobalAgentState,
    ) -> None:
        pre = (
            state.pre_execution_telemetry
            or self._current_policy_result(state).get("pre_execution_telemetry")
            or {}
        )
        if not isinstance(pre, dict):
            return
        kpi = pre.get("kpi") or {}
        if not isinstance(kpi, dict) or not kpi:
            return
        report["pre_execution_verification"] = {
            "status": pre.get("status"),
            "source": pre.get("source"),
            "timestamp": pre.get("timestamp"),
            "kpi": kpi,
            "qdisc": pre.get("qdisc") or {},
            "int": pre.get("int") or {},
        }
        report.setdefault("before_kpi", kpi)
        report.setdefault("after_kpi", report.get("kpi") or {})

    @staticmethod
    def _commit_policy(state: GlobalAgentState) -> None:
        """Commit staged desired state only after concrete config verification."""
        if state.yang_draft is None:
            return
        try:
            from persistence.policy_store import PolicyStore
            from models.policy_update import traffic_scope_key

            scope_key = (
                (state.execution_result or {}).get("traffic_scope_key")
                or traffic_scope_key(state.yang_draft).key
            )
            committed = PolicyStore().commit_staged(
                scope_key,
                intent_id=state.intent_id,
                runtime=state.execution_result or {},
            )
            if state.verification_report is not None:
                state.verification_report["policy_commit"] = {
                    "status": (committed or {}).get("status"),
                    "version": (committed or {}).get("version"),
                    "policy_id": (committed or {}).get("policy_id"),
                    "traffic_scope_key": scope_key,
                }
        except Exception as exc:
            raise RuntimeError(f"Policy store commit failed: {exc}") from exc

    async def _rollback_failed_update(self, state: GlobalAgentState) -> None:
        execution = state.execution_result or {}
        operation = execution.get("operation")
        repaired_existing = (
            operation == "VERIFY_EXISTING"
            and execution.get("executor_status") == "RECONCILED"
        )
        if operation != "MODIFY_QOS_RESOURCE" and not repaired_existing:
            return
        context = execution.get("rollback_context") or {}
        old_policy = context.get("old_policy")
        old_runtime = context.get("old_runtime") or {}
        try:
            from api.qos import clear_qos_policies, rollback_qos_policy
            from persistence.policy_store import PolicyStore

            old_switch_binding = (
                (old_runtime.get("qos_enforcement") or {}).get("qos_binding")
                or old_runtime.get("qos_binding")
            )
            if repaired_existing and not old_switch_binding:
                # A legacy exact policy had no switch-egress resource before
                # reconciliation.  If its classifier repair cannot be
                # verified, release the newly introduced TC resource as well.
                await clear_qos_policies(yang_policy=state.yang_draft)
                restored = "legacy_runtime_without_switch_class"
            else:
                await rollback_qos_policy(old_policy, state.yang_draft)
                restored = "previous_active_policy"
            PolicyStore().rollback_staged(
                execution.get("traffic_scope_key") or "",
                error="status verification failed",
            )
            execution["rollback"] = {"status": "SUCCESS", "restored": restored}
        except Exception as exc:
            execution["rollback"] = {"status": "FAILED", "error": str(exc)}
            logger.error("[%s] failed to rollback QoS update: %s", self.agent_id, exc)

    async def _retire_superseded_classifiers(
        self,
        state: GlobalAgentState,
        verification_report: dict[str, Any],
    ) -> None:
        """Remove prior/duplicate classifier IDs only after the new one is ADDED.

        Repairing a legacy runtime can move classification from a destination
        switch to the source-attached switch.  Delivery deliberately keeps the
        old IDs during validation; this method implements the second half of
        install-new -> verify-ADDED -> remove-old.
        """
        execution = state.execution_result or {}
        classifier = (verification_report.get("config_verification") or {}).get("p4_classifier") or {}
        keep_flow_id = str(classifier.get("flow_id") or "")
        if classifier.get("flow_state") != "ADDED" or not keep_flow_id:
            return

        installed = list(execution.get("installed_flows") or [])
        # Only IDs explicitly designated by a staged classifier migration can
        # be retired.  ``installed_flows`` also contains route entries; treating
        # every non-current ID as a classifier both missed historical ownership
        # and could delete live forwarding rules.
        superseded: dict[str, list[str]] = {}
        for item in execution.get("superseded_classifier_flows") or []:
            device_id = str(item.get("device_id") or "")
            flow_id = str(item.get("flow_id") or "")
            if device_id and flow_id and flow_id != keep_flow_id:
                superseded.setdefault(device_id, []).append(flow_id)
        if not superseded:
            return

        from mcp_servers.onos_server import get_onos_client

        client = get_onos_client()
        results: dict[str, Any] = {}
        try:
            for device_id, flow_ids in superseded.items():
                unique_ids = list(dict.fromkeys(flow_ids))
                visible = await client.get_flows(device_id)
                visible_ids = {
                    str(item.get("id") or "") for item in (visible.get("flows") or [])
                }
                to_delete = [flow_id for flow_id in unique_ids if flow_id in visible_ids]
                already_absent = [flow_id for flow_id in unique_ids if flow_id not in visible_ids]
                result = (
                    await client.delete_flows(device_id, to_delete)
                    if to_delete
                    else {"flows_deleted": 0, "total": 0, "errors": None}
                )
                result["already_absent"] = already_absent
                results[device_id] = result
                if result.get("errors") or int(result.get("flows_deleted") or 0) != len(to_delete):
                    raise RuntimeError(
                        f"superseded classifier cleanup failed on {device_id}: {result}"
                    )
        finally:
            # Process-wide ONOS session is closed by FastAPI lifespan.
            pass

        execution["installed_flows"] = [
            item for item in installed
            if str(item.get("flow_id") or "") not in {
                flow_id for flow_ids in superseded.values() for flow_id in flow_ids
            }
        ]
        execution["superseded_classifier_cleanup"] = {
            "status": "SUCCESS",
            "kept_flow_id": keep_flow_id,
            "removed": superseded,
            "results": results,
            "sequence": "INSTALL_NEW_VERIFY_ADDED_REMOVE_OLD",
        }

    def _record_experience_memory(
        self,
        state: GlobalAgentState,
        verification_report: dict[str, Any],
    ) -> None:
        try:
            if state.yang_draft is None:
                return
            from agents.experience_memory import ExperienceMemoryAgent

            recorded = ExperienceMemoryAgent().record_from_verification(
                intent_id=state.intent_id,
                yang_policy=state.yang_draft,
                verification_report=verification_report,
            )
            if recorded:
                self._current_policy_result(state)["experience_case"] = recorded
                try:
                    from api.a2a_trace import record_response

                    record_response(
                        state,
                        source_agent="experience_memory",
                        target_agent="status_verifier",
                        stage=self.stage,
                        status=StageStatus.SUCCESS,
                        output_summary=(
                            f"记录经验案例 {recorded.get('case_id')}，"
                            f"success={recorded.get('success')}。"
                        ),
                        payload_schema="ExperienceMemory.Case.v1",
                        payload=recorded,
                        evidence_refs=["verification_report", "root_cause_report"],
                    )
                except Exception:
                    pass
        except Exception:
            return

    def _record_root_cause_trace(self, state: GlobalAgentState, result: Any) -> None:
        report = getattr(result, "root_cause_report", None)
        if not isinstance(report, dict) or not report:
            return
        try:
            from api.a2a_trace import record_delegation, record_response

            evidence_refs = [
                f"root_cause_report.evidence[{index}].{item.get('source')}"
                for index, item in enumerate(report.get("evidence") or [])
                if item.get("source")
            ][:6]
            task_id = record_delegation(
                state,
                source_agent="status_verifier",
                target_agent="root_cause_analyzer",
                stage=self.stage,
                input_summary="KPI 未达标，委派 RootCauseAnalyzer 对候选根因进行证据评分。",
                payload_schema="RootCauseAnalyzer.Input.v1",
                payload={
                    "violations": list(getattr(result, "violations", []) or []),
                    "telemetry_source": getattr(result, "telemetry_source", None),
                },
                evidence_refs=["verification_report.kpi", "verification_report.int_analysis"],
            )
            risk_map = {"low": 25, "medium": 55, "high": 85}
            record_response(
                state,
                source_agent="root_cause_analyzer",
                target_agent="status_verifier",
                stage=self.stage,
                status=StageStatus.SUCCESS,
                task_id=task_id,
                output_summary=report.get("summary") or "完成结构化根因分析。",
                payload_schema="RootCauseAnalyzer.Report.v1",
                payload=report,
                evidence_refs=evidence_refs,
                confidence=report.get("confidence"),
                risk_score=risk_map.get(str(report.get("risk")), 50),
            )
        except Exception:
            return

    async def _attempt_auto_heal(self, state: GlobalAgentState, result: Any) -> dict[str, Any] | None:
        """Apply one deterministic congestion-healing attempt before graph ReAct retry.

        This is the bridge between the agent pipeline and the demo-layer
        self-healing module (api/healing.py).  Only triggered when the
        verification result indicates a healable congestion fault.
        """
        if (state.execution_result or {}).get("operation") in {
            "MODIFY_QOS_RESOURCE",
            "VERIFY_EXISTING",
        }:
            # Exact-scope QoS changes do not authorize path mutation.  A KPI
            # failure here is handled by the policy rollback/ReAct path.
            return None
        try:
            from api.healing import apply_congestion_healing, should_attempt_congestion_heal

            if not should_attempt_congestion_heal(result):
                return None

            from api.a2a_trace import record_react_trigger

            failure_reason = "; ".join(getattr(result, "violations", []) or [])
            record_react_trigger(
                state,
                source_agent="status_verifier",
                target_agent="auto_healing_controller",
                stage=self.stage,
                failure_reason=(
                    failure_reason
                    or getattr(result, "root_cause", None)
                    or "KPI 验证失败，疑似主路径拥塞。"
                ),
                observation_data={
                    "root_cause": getattr(result, "root_cause", None),
                    "root_cause_report": getattr(result, "root_cause_report", None),
                    "violations": list(getattr(result, "violations", []) or []),
                    "telemetry_source": getattr(result, "telemetry_source", None),
                },
                retry_suggestion="切换备用路径并限制低优先级流量后重新验证。",
            )
            healing_result = await apply_congestion_healing(
                state.yang_draft,
                result,
                intent_id=state.intent_id,
                previous_attempts=list(state.react_state.adjustment_history),
            )
            if healing_result.get("action_fingerprint"):
                state.react_state.adjustment_history.append(
                    {
                        "action_fingerprint": healing_result.get("action_fingerprint"),
                        "context_signature": healing_result.get("context_signature"),
                        "status": (
                            "EXECUTED_PENDING_ASSURANCE"
                            if healing_result.get("status") == "ok"
                            else "FAILED"
                        ),
                        "selected_action": healing_result.get("selected_action"),
                        "selected_evaluation": healing_result.get("selected_evaluation"),
                    }
                )
            logger.warning(
                "[%s] 自动拥塞自愈已执行: %s",
                self.agent_id,
                healing_result,
            )
            return healing_result
        except Exception as exc:
            logger.warning("[%s] 自动拥塞自愈失败: %s", self.agent_id, exc)
            return {"status": "error", "error": str(exc)}


# ──────────────────────────────────────────────
# 人工审批挂起节点
# ──────────────────────────────────────────────

"""
agents/status_verifier.py
状态验证专家Agent (Status Verifier)

职责:
  • 遥测采集: 通过MCP调用INT Collector与P4计数器获取端到端性能指标
  • KPI比对: 将实际观测值与意图约束进行数值比对
  • 根因分析: 验证失败时分析瓶颈位置
  • 回溯触发: 生成ReAct_Request驱动PolicyCompiler重新编译策略
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from agents.base_agent import BaseAgent
from agents.root_cause_analyzer import RootCauseAnalyzerAgent
from api.int_analysis import analyze_telemetry
from models.policy_update import effective_qos_mode
from models.telemetry import IntentConstraints, INTHopRecord, KPIMetrics, TelemetryData
from models.yang_policy import ActionType, YANGPolicy

logger = logging.getLogger(__name__)

# 验证等待时间 (秒): 下发完成后等待流量稳定
# 可通过环境变量 IDN_VERIFY_WAIT_SECS 覆盖，默认 2s。
# Mininet/BMv2 软件环境流量几乎瞬时稳定，5s 硬等待无必要。
VERIFICATION_WAIT_SECS = int(os.getenv("IDN_VERIFY_WAIT_SECS", "2"))
# 遥测采样次数
SAMPLE_COUNT = 10


class VerificationResult:
    """验证结果容器"""

    def __init__(
        self,
        passed: bool,
        telemetry: TelemetryData,
        violations: List[str],
        root_cause: Optional[str] = None,
        react_suggestion: Optional[str] = None,
        write_success: bool = True,
        intent_achieved: Optional[bool] = None,
        telemetry_source: str = "mcp",
        root_cause_report: Optional[Dict[str, Any]] = None,
        verification_state: str = "KPI_VERIFIED",
        config_verification: Optional[Dict[str, Any]] = None,
    ):
        self.passed = passed
        self.telemetry = telemetry
        self.violations = violations
        self.root_cause = root_cause
        self.react_suggestion = react_suggestion
        self.write_success = write_success
        self.intent_achieved = passed if intent_achieved is None else intent_achieved
        self.telemetry_source = telemetry_source
        self.root_cause_report = root_cause_report
        self.verification_state = verification_state
        self.config_verification = config_verification or {}

    def to_dict(self) -> Dict[str, Any]:
        int_analysis = analyze_telemetry(self.telemetry)
        return {
            "passed": self.passed,
            "verification_state": self.verification_state,
            "config_verification": self.config_verification,
            "write_success": self.write_success,
            "intent_achieved": self.intent_achieved,
            "telemetry_source": self.telemetry_source,
            "kpi": self.telemetry.kpi.model_dump(),
            "device_counters": self.telemetry.device_counters,
            "policy_traffic_probe": (
                (self.telemetry.raw_int_data or {}).get("policy_traffic_probe")
                if isinstance(self.telemetry.raw_int_data, dict)
                else None
            ),
            "int_analysis": int_analysis,
            "int_quality": {
                "quality_status": self.telemetry.int_quality_status,
                "quality_reason": self.telemetry.int_quality_reason,
                "used_for_decision": self.telemetry.int_used_for_decision,
                "validation_checks": self.telemetry.int_validation_checks,
            },
            "trusted_policy_evidence": [
                "ONOS Flow readback",
                "P4 Direct Counter",
                "TC configuration readback",
                "Mininet ping/iperf",
                "background competition traffic",
                "database policy state",
            ],
            "int_path_records": [
                item.model_dump() for item in self.telemetry.int_path_records
            ],
            "violations": self.violations,
            "root_cause": self.root_cause,
            "root_cause_report": self.root_cause_report,
            "react_suggestion": self.react_suggestion,
            "timestamp": datetime.utcnow().isoformat(),
        }


class StatusVerifierAgent(BaseAgent):
    """
    状态验证专家Agent

    在流表下发完成后，采集Telemetry数据验证业务KPI是否达标。
    如果未达标，执行根因分析并生成ReAct调整建议。
    """

    agent_id = "status_verifier"

    @staticmethod
    def _execution_readback_timeout_seconds() -> float:
        """Return one end-to-end budget for controller and TC readback.

        A classifier can legitimately remain ``PENDING_ADD`` until ONOS has
        completed its device polling cycle.  The P4 reader therefore has a
        40-second production convergence budget.  The former outer verifier
        deadline was only 35 seconds, which cancelled that valid reader before
        TC could be observed and incorrectly produced an unavailable result.

        The outer deadline must never be shorter than the controller budget
        plus a small TC/readback allowance. Operators may increase it, but a
        stale low environment value cannot recreate that contradiction.
        """
        controller_budget = max(
            0.1,
            float(os.getenv("ONOS_FLOW_VERIFY_TIMEOUT_SECONDS", "40")),
        )
        configured = max(
            0.0,
            float(os.getenv("IDN_VERIFY_EXECUTION_READBACK_TIMEOUT_SECONDS", "0")),
        )
        return max(60.0, controller_budget + 10.0, configured)
    description = "负责采集Telemetry数据并验证意图KPI是否达标，驱动ReAct回溯"

    async def _bounded_verification_step(self, name: str, operation, *, default_timeout: float) -> Any:
        """Bound one external read/measurement inside the verifier node.

        The outer graph timeout protects the workflow as a whole, but shared
        ONOS/telemetry tasks can be shielded from cancellation.  Without a
        local deadline a single stalled read leaves an otherwise auditable
        Intent in RECEIVED/VERIFYING until the node's retry budget expires.
        Each step therefore returns control promptly so the existing truthful
        unavailable/no-sample paths can persist an outcome and drive retry.
        """
        timeout = max(
            0.1,
            float(os.getenv(f"IDN_VERIFY_{name.upper()}_TIMEOUT_SECONDS", str(default_timeout))),
        )
        try:
            return await asyncio.wait_for(operation, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"VERIFICATION_STEP_TIMEOUT:{name}:{timeout:g}s") from exc

    async def verify(
        self,
        intent_id: str,
        yang_draft: YANGPolicy,
        execution_result: Dict[str, Any],
        wait_secs: int = VERIFICATION_WAIT_SECS,
        intent_plan: Optional[Any] = None,
        analyze_root_cause: bool = True,
    ) -> VerificationResult:
        """
        执行验证流程: 等待稳定 → 采集遥测 → KPI比对 → 根因分析

        Args:
            intent_id: 意图ID
            yang_draft: 原始YANG策略 (含意图约束条件)
            execution_result: P4RuntimeExecutorAgent 的下发结果
            wait_secs: 等待流量稳定的秒数

        Returns:
            VerificationResult: 验证结果 (含KPI数据和ReAct建议)
        """
        logger.info(
            f"[{self.agent_id}] 开始验证 intent_id={intent_id}, 等待{wait_secs}s流量稳定..."
        )
        # 下发成功只代表 P4Runtime 写入成功；后面的 KPI 比对才代表意图达成。
        execution = execution_result or {}
        # A repeated NO_OP plan deliberately performs no duplicate write.  It
        # is still required to perform independent configuration readback and
        # live KPI assurance; treating it as a southbound failure skips that
        # evidence and can incorrectly conclude UNSATISFIABLE.
        write_success = (
            execution.get("status") == "SUCCESS"
            or (
                execution.get("executor_status") == "SKIPPED_NOOP"
                and execution.get("reason_code") == "NO_OP_PLAN"
                and execution.get("no_change") is True
            )
        )
        if not write_success:
            status = (execution_result or {}).get("status", "UNKNOWN")
            telemetry = TelemetryData(
                intent_id=intent_id,
                sample_count=0,
                kpi=KPIMetrics(),
            )
            error_text = str((execution_result or {}).get("error") or status)
            if "mismatch" in error_text.lower() or "409" in error_text:
                readback = self._readback_failure(error_text, execution_result)
                return VerificationResult(
                    passed=False, telemetry=telemetry,
                    violations=["EXECUTION_READBACK_MISMATCH: execution readback did not match desired state"],
                    root_cause="执行读回未验证，尚不能进行 SLA 测量。",
                    react_suggestion="先对账 Desired–Observed 配置。",
                    write_success=False, intent_achieved=False,
                    telemetry_source="config_readback",
                    verification_state=readback["execution_readback_status"],
                    config_verification=readback,
                )
            return VerificationResult(
                passed=False,
                telemetry=telemetry,
                violations=[
                    f"P4Runtime 写入未成功，状态为 {status}，跳过业务 KPI 验证。"
                ],
                root_cause="P4Runtime 写入失败或未执行。",
                react_suggestion="请先修复流表下发问题，再进行遥测验证。",
                write_success=False,
                intent_achieved=False,
                telemetry_source="not_collected",
                verification_state="CONFIG_FAILED",
            )

        # A business KPI is meaningful only when one immutable ServiceProfile
        # owns both the compiled selector and the active probe selector.
        service_profile = None
        try:
            from api.service_profiles import (
                attach_service_profile,
                resolve_service_profile_for_policy,
            )

            service_profile = resolve_service_profile_for_policy(yang_draft)
            yang_draft = attach_service_profile(yang_draft, service_profile.service_id)
        except Exception as exc:
            qos = yang_draft.network_policy.action.qos_params
            raw_intent = str(yang_draft.raw_intent or "").lower()
            requires_profile = bool(
                yang_draft.service_id
                or yang_draft.validation_profile_id
                or (qos and qos.bandwidth)
                or any(
                    token in raw_intent
                    for token in (
                        "latency",
                        "low_latency",
                        "jitter",
                        "loss",
                        "时延",
                        "低时延",
                        "抖动",
                        "丢包",
                    )
                )
            )
            if not requires_profile:
                logger.info(
                    "[%s] generic policy has no business KPI binding; "
                    "continuing with controller/network evidence only",
                    self.agent_id,
                )
            else:
                telemetry = TelemetryData(
                    intent_id=intent_id, sample_count=0, kpi=KPIMetrics()
                )
                return VerificationResult(
                    passed=False,
                    telemetry=telemetry,
                    violations=[f"VALIDATION_PROFILE_MISMATCH: {exc}"],
                    root_cause="策略选择器与主动验证业务画像不一致。",
                    react_suggestion="修正 ServiceProfile 或策略选择器后重新验证。",
                    write_success=True,
                    intent_achieved=False,
                    telemetry_source="service_profile_registry",
                    verification_state="VALIDATION_PROFILE_MISMATCH",
                    config_verification={
                        "status": "error",
                        "code": "VALIDATION_PROFILE_MISMATCH",
                        "error": str(exc),
                    },
                )

        # A controller/DB success flag is insufficient.  Read the concrete
        # QoS class/filter and compare it with the desired bandwidth before
        # evaluating traffic KPIs.
        config_verification: Dict[str, Any] = {}
        if yang_draft.network_policy.action.action_type in {
            ActionType.SET_PRIORITY,
            ActionType.RATE_LIMIT,
        }:
            try:
                from api.qos import verify_execution_plane

                config_verification = await self._bounded_verification_step(
                    "execution_readback",
                    verify_execution_plane(yang_draft, execution_result),
                    default_timeout=self._execution_readback_timeout_seconds(),
                )
            except Exception as exc:
                telemetry = TelemetryData(
                    intent_id=intent_id, sample_count=0, kpi=KPIMetrics()
                )
                readback = self._readback_failure(str(exc), execution_result)
                return VerificationResult(
                    passed=False,
                    telemetry=telemetry,
                    violations=[f"QoS 实际配置读回失败: {exc}"],
                    root_cause="期望配置未在数据平面生效。",
                    react_suggestion="先对账 Desired–Observed 配置，再进行 SLA 测量。",
                    write_success=True,
                    intent_achieved=False,
                    telemetry_source="config_readback",
                    verification_state=readback["execution_readback_status"],
                    config_verification=readback,
                )
            if not self._readback_verified(config_verification):
                telemetry = TelemetryData(
                    intent_id=intent_id, sample_count=0, kpi=KPIMetrics()
                )
                readback = self._readback_failure(
                    "readback response did not prove desired configuration",
                    execution_result,
                    observed=config_verification,
                )
                return VerificationResult(
                    passed=False, telemetry=telemetry,
                    violations=["EXECUTION_READBACK_MISMATCH: desired and observed QoS configuration differ"],
                    root_cause="执行配置读回未验证，尚不能进行 SLA 测量。",
                    react_suggestion="先对账 Desired–Observed 配置，再进行 SLA 测量。",
                    write_success=True, intent_achieved=False,
                    telemetry_source="config_readback",
                    verification_state=readback["execution_readback_status"],
                    config_verification=readback,
                )
        config_verification["service_profile"] = (
            {
                "service_id": service_profile.service_id,
                "validation_profile_id": yang_draft.validation_profile_id,
                "selector": service_profile.selector(),
                "selector_consistent": True,
            }
            if service_profile is not None
            else {
                "status": "NOT_REQUIRED",
                "reason": "generic policy has no business KPI objective",
            }
        )

        # Step 1: 提取意图约束
        constraints = self._extract_constraints(yang_draft)

        # Step 2: 等待流量稳定 — 期间并行预热 ONOS 连接
        # 在等待期间发起一次预热遥测采集（结果丢弃），使 ONOS HTTP 连接池
        # 在正式采集时已处于 warm 状态，省去 TCP/TLS 握手延迟。
        warmup_task = asyncio.ensure_future(self._collect_live_telemetry(intent_id, 1))
        await asyncio.sleep(wait_secs)
        try:
            await self._bounded_verification_step(
                "telemetry_warmup", warmup_task, default_timeout=20,
            )  # Ensure the warmup request releases its connection.
        except Exception:
            pass  # 预热失败不影响正式采集

        # Step 3: 采集真实遥测数据 — 失败即报告不可用，不回退到模拟数据
        try:
            telemetry, telemetry_source = await self._bounded_verification_step(
                "telemetry",
                self._collect_live_telemetry(intent_id, SAMPLE_COUNT),
                default_timeout=25,
            )
        except Exception as exc:
            logger.error("[%s] telemetry collection failed: %s", self.agent_id, exc)
            return VerificationResult(
                passed=False,
                telemetry=TelemetryData(
                    intent_id=intent_id, sample_count=0, kpi=KPIMetrics()
                ),
                violations=[f"Telemetry unavailable: {exc}"],
                root_cause="Telemetry collection infrastructure failure",
                react_suggestion="Restore telemetry connectivity and retry verification.",
                write_success=write_success,
                intent_achieved=False,
                telemetry_source="unavailable",
                verification_state="TELEMETRY_UNAVAILABLE",
                config_verification=config_verification,
            )
        try:
            policy_probe = await self._bounded_verification_step(
                "service_probe",
                self._collect_policy_throughput(
                    yang_draft,
                    intent_id=intent_id,
                    execution_result=execution_result,
                ),
                default_timeout=max(40, float(os.getenv("IDN_QOS_KPI_DURATION_SECS", "15")) + 25),
            )
        except Exception as exc:
            policy_probe = {
                "status": "UNAVAILABLE",
                "measurement_status": "NO_SAMPLE",
                "error": str(exc),
            }
        # Preserve an unsuccessful active-probe response.  Without this, an
        # absent business measurement left the unrelated infrastructure value
        # (often 0 Mbps) in place and was falsely treated as an SLA failure.
        if not isinstance(telemetry.raw_int_data, dict):
            telemetry.raw_int_data = {}
        telemetry.raw_int_data["policy_traffic_probe"] = policy_probe
        bandwidth_expected = bool(
            yang_draft.network_policy.action.qos_params
            and yang_draft.network_policy.action.qos_params.bandwidth
        )
        if bandwidth_expected:
            probe_state = str(
                (policy_probe or {}).get("status") or "UNAVAILABLE"
            ).upper()
            if probe_state != "MEASURED":
                return VerificationResult(
                    passed=False,
                    telemetry=telemetry,
                    violations=[
                        "Business traffic measurement is unavailable; SLA throughput was not evaluated"
                    ],
                    root_cause="The selector-matched service probe did not produce a valid measurement.",
                    react_suggestion="Restore the service traffic probe and run a fresh measurement.",
                    write_success=write_success,
                    intent_achieved=False,
                    telemetry_source=f"{telemetry_source}+service_probe_unavailable",
                    # Preserve the public no-sample contract while retaining
                    # the raw failed probe above. Routing treats NO_SAMPLE as
                    # a measurement-needed outcome, never a 0-Mbps KPI.
                    verification_state="NO_SAMPLE",
                    config_verification=config_verification,
                )
        if (
            policy_probe
            and policy_probe.get("status") == "MEASURED"
            and policy_probe.get("throughput_mbps") is not None
        ):
            telemetry.kpi.throughput_mbps = float(policy_probe["throughput_mbps"])
            protocol_metrics = policy_probe.get("protocol_metrics") or {}
            if policy_probe.get("protocol") == "UDP":
                telemetry.kpi.packet_loss_rate = (
                    float(protocol_metrics["loss_percent"]) / 100.0
                    if protocol_metrics.get("loss_percent") is not None
                    else None
                )
                telemetry.kpi.jitter_ms = protocol_metrics.get("jitter_ms")
            elif policy_probe.get("protocol") == "TCP":
                telemetry.kpi.packet_loss_rate = None
                telemetry.kpi.jitter_ms = None
                if protocol_metrics.get("mean_rtt_ms") is not None:
                    telemetry.kpi.latency_ms = protocol_metrics.get("mean_rtt_ms")
            profile_id = (
                service_profile.service_id if service_profile is not None else "unbound"
            )
            telemetry_source += f"+service_probe:{profile_id}"
            classifier_pair = policy_probe.get("p4_classifier_evidence") or {}
            config_verification["p4_classifier_after_traffic"] = (
                classifier_pair.get("after") or {}
            )
            config_verification["p4_counter"] = (
                policy_probe.get("p4_counter_evidence") or {}
            )
            config_verification["switch_egress_qos_after_traffic"] = (
                policy_probe.get("tc_evidence") or {}
            )
        logger.info(
            f"[{self.agent_id}] 遥测采集完成: "
            f"latency={telemetry.kpi.latency_ms}ms, "
            f"throughput={telemetry.kpi.throughput_mbps}Mbps, "
            f"loss={telemetry.kpi.packet_loss_rate}"
        )

        # Step 4: KPI比对
        passed, violations = constraints.is_satisfied_by(telemetry.kpi)
        if (
            policy_probe
            and policy_probe.get("test_profile") == "RATE_LIMIT_NO_BACKGROUND"
        ):
            bandwidth = float(
                yang_draft.network_policy.action.qos_params.bandwidth or 0
            )
            throughput = float(policy_probe.get("throughput_mbps") or 0)
            if policy_probe.get("background_started") is not False:
                passed = False
                violations.append(
                    "RATE_LIMIT no-background validation unexpectedly started a background flow"
                )
            # A RATE_LIMIT is an upper-bound contract.  Under-delivery is not
            # an SLA violation by itself: TCP can legitimately settle below
            # the cap because of congestion control, loss, RTT, or offered
            # load.  The service validator already proves that traffic was
            # generated and evaluates the exact ``<= cap`` objective; P4 and
            # TC counter evidence below proves that the intended classifier
            # and class carried that traffic.  A synthetic lower throughput
            # floor would silently turn a MAX_BANDWIDTH policy into a
            # MIN_BANDWIDTH policy and breaks semantic idempotency.
        if policy_probe and policy_probe.get("traffic_generated"):
            counter_evidence = config_verification.get("p4_counter") or {}
            if counter_evidence.get("status") != "VERIFIED":
                passed = False
                violations.append(
                    "P4 direct counter did not increase after exact policy-matched traffic"
                )
            if bandwidth_expected:
                tc_evidence = policy_probe.get("tc_evidence") or {}
                tc_delta = tc_evidence.get("delta") or {}
                if (
                    tc_evidence.get("status") != "MEASURED"
                    or int(tc_delta.get("sent_bytes") or 0) <= 0
                ):
                    passed = False
                    violations.append(
                        "target TC interface/class counter did not increase"
                    )
            if not (policy_probe.get("validation_result") or {}).get("passed"):
                passed = False
                violations.append(
                    "service-specific validation failed: "
                    + str(
                        (policy_probe.get("validation_result") or {}).get(
                            "verification_state"
                        )
                    )
                )
        if bandwidth_expected and telemetry.kpi.throughput_mbps is None:
            return VerificationResult(
                passed=False,
                telemetry=telemetry,
                violations=[
                    "NO_SAMPLE: service-specific traffic produced no throughput sample"
                ],
                write_success=write_success,
                intent_achieved=False,
                telemetry_source=telemetry_source,
                verification_state="NO_SAMPLE",
                config_verification=config_verification,
            )

        if passed:
            logger.info(f"[{self.agent_id}] ✓ KPI验证通过")
            return VerificationResult(
                passed=True,
                telemetry=telemetry,
                violations=[],
                write_success=write_success,
                intent_achieved=True,
                telemetry_source=telemetry_source,
                verification_state=(
                    "RATE_LIMIT_KPI_VERIFIED"
                    if bandwidth_expected
                    and effective_qos_mode(yang_draft) == "RATE_LIMIT"
                    else (
                        "MIN_BANDWIDTH_KPI_VERIFIED"
                        if bandwidth_expected
                        else "DEPLOYMENT_AND_NETWORK_EVIDENCE_VERIFIED"
                    )
                ),
                config_verification=config_verification,
            )

        # A single valid KPI miss is evidence for confirmation, not evidence
        # for a causal conclusion.  The orchestrator requests deferred root
        # cause analysis while it collects its bounded independent samples.
        if not analyze_root_cause:
            return VerificationResult(
                passed=False,
                telemetry=telemetry,
                violations=violations,
                write_success=write_success,
                intent_achieved=False,
                telemetry_source=telemetry_source,
                verification_state="CONFIRMING_VIOLATION",
                config_verification=config_verification,
            )

        # Step 5: 根因分析
        root_cause, react_suggestion, root_cause_report = (
            await self._analyze_root_cause(
                telemetry, violations, yang_draft, telemetry_source
            )
        )
        logger.warning(
            f"[{self.agent_id}] ✗ KPI验证失败: {violations}\n根因: {root_cause}"
        )
        return VerificationResult(
            passed=False,
            telemetry=telemetry,
            violations=violations,
            root_cause=root_cause,
            react_suggestion=react_suggestion,
            root_cause_report=root_cause_report,
            write_success=write_success,
            intent_achieved=False,
            telemetry_source=telemetry_source,
            verification_state="KPI_FAILED",
            config_verification=config_verification,
        )

    @staticmethod
    def _readback_verified(result: dict[str, Any]) -> bool:
        return bool(
            str(result.get("status") or "").lower() in {"ok", "verified"}
            and result.get("config_applied") is True
        )

    @staticmethod
    def _readback_failure(
        error: str, execution: dict[str, Any], *, observed: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Turn an opaque QoS readback failure into an auditable gate result."""
        text = error.lower()
        unavailable = any(marker in text for marker in ("timeout", "unavailable", "connection", "refused"))
        status = "EXECUTION_READBACK_UNAVAILABLE" if unavailable else "EXECUTION_READBACK_MISMATCH"
        mismatch = "UNKNOWN_READBACK_MISMATCH"
        for marker, code in (
            ("selector", "SELECTOR_MISMATCH"), ("action", "ACTION_MISMATCH"),
            ("rate", "RATE_MISMATCH"), ("ceil", "CEIL_MISMATCH"),
            ("class", "MISSING_TC_CLASS"), ("filter", "MISSING_TC_FILTER"),
            ("p4", "MISSING_IN_P4RUNTIME"), ("onos", "MISSING_IN_ONOS"),
            ("owner", "OWNER_MISMATCH"), ("generation", "STALE_GENERATION"),
        ):
            if marker in text:
                mismatch = code
                break
        return {
            "status": "unavailable" if unavailable else "mismatch",
            "http_status": 409 if "409" in text or "mismatch" in text else None,
            "execution_readback_status": status,
            "mismatch_codes": [] if unavailable else [mismatch],
            "mismatch_fields": [] if unavailable else [mismatch],
            "expected": {"execution": "desired QoS classifier and HTB binding"},
            "observed": observed or {"error": error},
            "policy_id": execution.get("existing_policy_id") or execution.get("policy_id"),
            "generation": execution.get("generation"),
            "environment_epoch": execution.get("environment_epoch"),
            "device_id": execution.get("device_id"),
            "interface": execution.get("interface"),
            "table": execution.get("table"),
            "selector": execution.get("selector"),
            "action": execution.get("action"),
            "tc_class": execution.get("tc_class"),
            "tc_filter": execution.get("tc_filter"),
            "evidence_refs": ["QoSExecutionPlaneReadback"],
        }

    async def _collect_live_telemetry(
        self, intent_id: str, sample_count: int
    ) -> tuple[TelemetryData, str]:
        """Collect real ONOS counters plus Mininet active-probe KPIs."""
        from api.telemetry import collect_current_telemetry

        # This call collects only network health/infrastructure.  The business
        # flow is generated separately from the bound ServiceProfile.
        payload = await collect_current_telemetry(
            include_probe=True, include_iperf=False
        )
        if payload.get("status") != "ok":
            raise RuntimeError(
                payload.get("error") or "live telemetry status is not ok"
            )

        kpi_payload = payload.get("kpi", {}) or {}
        qdisc_payload = (payload.get("raw") or {}).get("qdisc_probe") or {}
        int_payload = (payload.get("raw") or {}).get("int_probe") or {}
        p4_counter_payload = (payload.get("raw") or {}).get("p4_direct_counter") or {}
        int_used_for_decision = bool(int_payload.get("used_for_decision"))
        qdisc_counters = {
            key: int(value)
            for key, value in {
                "queue_depth": kpi_payload.get("queue_depth"),
                "queue_depth_bytes": kpi_payload.get("queue_depth_bytes"),
                "dropped_packets": kpi_payload.get("queue_drops"),
                "overlimits": kpi_payload.get("queue_overlimits"),
                "congested_interfaces": len(
                    qdisc_payload.get("congested_interfaces") or []
                ),
            }.items()
            if isinstance(value, (int, float)) and float(value).is_integer()
        }
        int_records = []
        if int_payload.get("status") == "ok" and int_used_for_decision:
            for item in int_payload.get("hop_records") or []:
                device_id = item.get("device_id") or (
                    f"device:bmv2:{item.get('switch_id_raw')}"
                    if item.get("switch_id_raw")
                    else "device:bmv2:unknown"
                )
                int_records.append(
                    INTHopRecord(
                        device_id=device_id,
                        ingress_port=str(item.get("ingress_port", "")),
                        egress_port=str(item.get("egress_port", "")),
                        ingress_timestamp_ns=int(item.get("ingress_timestamp_ns") or 0),
                        egress_timestamp_ns=int(item.get("egress_timestamp_ns") or 0),
                        queue_id=0,
                        queue_occupancy=int(item.get("queue_occupancy") or 0),
                    )
                )
        telemetry = TelemetryData(
            intent_id=intent_id,
            sample_count=sample_count,
            int_path_records=int_records,
            int_quality_status=str(int_payload.get("quality_status") or "UNAVAILABLE"),
            int_quality_reason=str(
                int_payload.get("quality_reason") or "没有可用的INT记录"
            ),
            int_used_for_decision=int_used_for_decision,
            int_validation_checks=list(int_payload.get("validation_checks") or []),
            kpi=KPIMetrics(
                latency_ms=kpi_payload.get("latency_ms"),
                packet_loss_rate=kpi_payload.get("packet_loss_rate"),
                throughput_mbps=kpi_payload.get("throughput_mbps"),
                jitter_ms=kpi_payload.get("jitter_ms"),
                queue_depth=kpi_payload.get("queue_depth"),
                int_hop_count=kpi_payload.get("int_hop_count"),
                int_path_latency_ms=kpi_payload.get("int_path_latency_ms"),
                int_max_queue_occupancy=kpi_payload.get("int_max_queue_occupancy"),
            ),
            device_counters={
                "onos_flows": {
                    key: int(value)
                    for key, value in (payload.get("flows", {}) or {}).items()
                    if isinstance(value, (int, float)) and float(value).is_integer()
                },
                "onos_devices": payload.get("devices", {}),
                "tc_qdisc": qdisc_counters,
                "p4_int": {
                    key: int(value)
                    for key, value in {
                        "hop_count": kpi_payload.get("int_hop_count"),
                        "max_queue_occupancy": kpi_payload.get(
                            "int_max_queue_occupancy"
                        ),
                    }.items()
                    if int_used_for_decision
                    and isinstance(value, (int, float))
                    and float(value).is_integer()
                },
                "p4_direct_counter": {
                    key: int(value)
                    for key, value in {
                        "packets": p4_counter_payload.get("packets"),
                        "bytes": p4_counter_payload.get("bytes"),
                        "direct_entries": p4_counter_payload.get("direct_entries"),
                    }.items()
                    if isinstance(value, (int, float)) and float(value).is_integer()
                },
            },
            raw_int_data=payload,
        )
        return telemetry, str(payload.get("source") or "onos_rest")

    async def _collect_policy_throughput(
        self,
        yang_draft: YANGPolicy,
        *,
        intent_id: str,
        execution_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Generate traffic matching the policy protocol/port before KPI use.

        A dashboard or network-health probe must not be used as evidence for
        a different service selector.  This profile-bound probe is the
        positive evidence that the configured TC filter was exercised.
        """
        qos = yang_draft.network_policy.action.qos_params
        raw_intent = str(yang_draft.raw_intent or "").lower()
        bandwidth = int(qos.bandwidth) if qos and qos.bandwidth else None
        needs_service_probe = bool(
            bandwidth
            or any(
                token in raw_intent
                for token in (
                    "latency",
                    "low_latency",
                    "jitter",
                    "loss",
                    "时延",
                    "低时延",
                    "抖动",
                    "丢包",
                )
            )
        )
        if not needs_service_probe:
            return None
        match = yang_draft.network_policy.match
        if not match.destination_ip:
            return None
        protocol = getattr(match.protocol, "value", str(match.protocol)).upper()
        if protocol == "ANY":
            protocol = "TCP"
        mode = effective_qos_mode(yang_draft)
        from api.service_profiles import resolve_service_profile_for_policy
        from api.service_validation import run_service_validation

        profile = resolve_service_profile_for_policy(yang_draft)
        offered_rate = (
            # A bounded over-offer must still leave the declared residual
            # capacity available to the concurrently active best-effort
            # scope.  Three times the requested floor collapsed the
            # admission allocation into a target-only 49 Mbps ceiling,
            # making the supposed competition test neither representative
            # nor reproducible.  This is a probe input, not an SLA change:
            # the policy's requested floor and verification threshold remain
            # exactly as declared.
            max(int((float(bandwidth) * 4 + 2) // 3), 30)
            if bandwidth
            else float(profile.default_offered_rate_mbps or 1)
        )
        request = {
            "service_id": profile.service_id,
            "intent_id": intent_id,
            "policy_id": yang_draft.network_policy.policy_id,
            "duration_seconds": int(os.getenv("IDN_QOS_KPI_DURATION_SECS", "15")),
            "offered_rate_mbps": offered_rate,
        }
        # Background traffic is expressed through ServiceProfiles. The
        # validator executes exactly this request and never selects a
        # business-specific traffic scenario by service name.
        if bandwidth and mode == "MIN_BANDWIDTH":
            request["background_profiles"] = list(profile.background_profile_ids)
        result = await run_service_validation(
            profile.service_id,
            request,
            scenario_id=None,
            yang_policy=yang_draft,
            execution_result=execution_result,
        )
        if isinstance(result, dict):
            if bandwidth and mode == "RATE_LIMIT":
                tolerance = float(
                    os.getenv("IDN_QOS_RATE_LIMIT_TOLERANCE_RATIO", "1.10")
                )
                result["maximum_acceptable_mbps"] = bandwidth * tolerance
            elif bandwidth:
                tolerance = float(os.getenv("IDN_QOS_KPI_TOLERANCE_RATIO", "0.95"))
                result["minimum_acceptable_mbps"] = bandwidth * tolerance
            else:
                tolerance = None
            if tolerance is not None:
                result["tolerance_ratio"] = tolerance
            result["throughput_mbps"] = (
                (result.get("protocol_metrics") or {}).get("received_mbps")
                if result.get("protocol") == "UDP"
                else (result.get("protocol_metrics") or {}).get("receiver_mbps")
            )
        return result if isinstance(result, dict) else None

    def _extract_constraints(self, yang_draft: YANGPolicy) -> IntentConstraints:
        """
        从YANG策略中提取意图约束条件
        将QoS参数映射为可量化的KPI约束
        """
        qos = yang_draft.network_policy.action.qos_params
        constraints = IntentConstraints()

        if qos:
            if qos.bandwidth:
                if effective_qos_mode(yang_draft) == "RATE_LIMIT":
                    # 限速策略要求实际吞吐量不超过阈值。
                    constraints.max_bandwidth_mbps = qos.bandwidth * float(
                        os.getenv("IDN_QOS_RATE_LIMIT_TOLERANCE_RATIO", "1.10")
                    )
                else:
                    # 带宽保障要求实际吞吐量达到预留带宽的 95%。
                    constraints.min_bandwidth_mbps = qos.bandwidth * float(
                        os.getenv("IDN_QOS_KPI_TOLERANCE_RATIO", "0.95")
                    )

        # 从原始意图文本中提取时延/丢包率约束 (简化版)
        raw_intent = yang_draft.raw_intent or ""
        if "时延" in raw_intent or "latency" in raw_intent.lower():
            # 提取数字 (简化: 默认10ms)
            constraints.max_latency_ms = 10.0
        if "丢包" in raw_intent or "loss" in raw_intent.lower():
            constraints.max_packet_loss_rate = 0.01  # 1%

        return constraints

    async def _analyze_root_cause(
        self,
        telemetry: TelemetryData,
        violations: List[str],
        yang_draft: YANGPolicy,
        telemetry_source: str = "",
    ) -> Tuple[str, str, Dict[str, Any]]:
        """
        根因分析: 生成结构化候选根因评分、证据链和 ReAct 建议

        Returns:
            (root_cause_description, react_suggestion, root_cause_report)
        """
        report = await RootCauseAnalyzerAgent(llm=self._llm).analyze_with_ai(
            telemetry=telemetry,
            violations=violations,
            yang_draft=yang_draft,
            telemetry_source=telemetry_source,
        )
        root_cause = report.get("summary") or f"KPI违规: {'; '.join(violations)}"
        react_suggestion = (
            report.get("react_suggestion") or "建议重新检查流表项配置和带宽预留参数"
        )

        if os.getenv("IDN_ROOT_CAUSE_LLM_EXPLANATION", "false").lower() == "true":
            try:
                report["llm_explanation"] = await self._llm_enhance_root_cause(
                    root_cause, violations, yang_draft
                )
            except Exception as exc:
                report["llm_explanation_error"] = str(exc)
        return root_cause, react_suggestion, report

    def _analyze_int_path(self, telemetry: TelemetryData) -> str | None:
        """Return an INT hop-by-hop summary when P4 INT records exist."""
        analysis = analyze_telemetry(telemetry)
        if analysis.get("status") != "ok":
            return None

        worst_latency = analysis.get("worst_latency_hop") or {}
        worst_queue = analysis.get("worst_queue_hop") or {}
        path = " -> ".join(
            analysis.get("switch_path") or analysis.get("device_path") or []
        )
        return (
            "P4 INT hop-by-hop path="
            f"{path}, bottleneck_link={analysis.get('bottleneck_link')}, "
            f"worst_latency={worst_latency.get('switch_name') or worst_latency.get('device_id')}/"
            f"{float(worst_latency.get('hop_latency_ms') or 0.0):.3f}ms, "
            f"worst_queue={worst_queue.get('switch_name') or worst_queue.get('device_id')}/"
            f"{worst_queue.get('queue_occupancy')}, "
            f"primary_path={analysis.get('primary_path')}, "
            f"secondary_path={analysis.get('secondary_path')}, "
            f"congestion_on_primary={analysis.get('congestion_on_primary')}"
        )
        return (
            "P4 INT逐跳遥测路径="
            f"{path}, 最大单跳时延={worst_latency.device_id}/"
            f"{worst_latency.hop_latency_ns / 1_000_000:.3f}ms, "
            f"最大队列={worst_queue.device_id}/{worst_queue.queue_occupancy}"
        )

    def _analyze_qdisc_congestion(self, telemetry: TelemetryData) -> str | None:
        """Return a TC qdisc based congestion summary when real qdisc data exists."""
        raw = telemetry.raw_int_data or {}
        qdisc = (raw.get("raw") or {}).get("qdisc_probe") or {}
        if qdisc.get("status") != "ok":
            return None

        queue_depth = qdisc.get("queue_depth") or 0
        queue_bytes = qdisc.get("queue_depth_bytes") or 0
        drops = qdisc.get("dropped_packets") or 0
        overlimits = qdisc.get("overlimits") or 0
        congested = qdisc.get("congested_interfaces") or []
        active_congested = [
            item
            for item in congested
            if (item.get("backlog_packets") or 0) > 0
            or (item.get("backlog_bytes") or 0) > 0
            or item.get("source") == "active_fault"
            or item.get("fault_id")
        ]
        if not (
            queue_depth
            or queue_bytes
            or active_congested
            or qdisc.get("congestion_detected")
        ):
            return None

        first = active_congested[0] if active_congested else {}
        location = ""
        if first:
            location = f" 位置={first.get('node')}/{first.get('interface')}"
        return (
            "TC qdisc 检测到真实拥塞"
            f"{location}, backlog={queue_depth}pkt/{queue_bytes}B, "
            f"drops={drops}, overlimits={overlimits}"
        )

    async def _llm_enhance_root_cause(
        self,
        preliminary_cause: str,
        violations: List[str],
        yang_draft: YANGPolicy,
    ) -> str:
        """使用LLM对规则分析的根因进行深度解读"""
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [
            SystemMessage(
                content=(
                    "你是网络故障分析专家。根据KPI违规信息和初步根因，"
                    "提供简洁的技术根因说明（不超过100字）。"
                )
            ),
            HumanMessage(
                content=(
                    f"KPI违规: {violations}\n"
                    f"初步根因: {preliminary_cause}\n"
                    f"策略描述: {yang_draft.network_policy.policy_name}\n"
                    "请提供技术根因说明:"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)
        return response.content.strip()

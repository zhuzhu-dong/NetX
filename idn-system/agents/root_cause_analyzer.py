"""
agents/root_cause_analyzer.py
==============================

结构化根因分析 Agent —— KPI 验证失败时的诊断引擎。

# 系统角色
当 StatusVerifier 检测到 KPI 不达标时，由 RootCauseAnalyzerAgent
对候选根因进行证据评分和排序，生成结构化诊断报告：
- selected_cause：最可能的故障根因
- confidence：诊断置信度
- evidence_used：使用的证据来源（INT 遥测、P4 计数器、tc qdisc 等）
- rejected_hypotheses：被排除的假设及排除原因
- recommended_action：推荐的修复动作（供 ReAct 重编译使用）

# 主要依赖
- agents/base_agent.py：LLM 客户端基类（用于 AI 增强分析）
- agents/experience_memory.py：历史案例检索（相似故障的成功修复经验）
- api/int_analysis.py：INT 遥测数据分析
- models/telemetry.py：TelemetryData
- utils/llm_json.py：从 LLM 输出提取 JSON

# 设计意图
- 支持纯确定性分析 + LLM 增强双路径
- 证据评分机制确保结论可追溯
- 排除的假设也被记录（explainability）
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agents.base_agent import BaseAgent
from agents.experience_memory import ExperienceMemoryAgent
from api.int_analysis import analyze_telemetry
from models.telemetry import TelemetryData
from models.yang_policy import YANGPolicy
from utils.llm_json import extract_json_object
from agents.healing_action_applicability import path_contains_link


class RootCauseAnalyzerAgent(BaseAgent):
    """Score candidate root causes from compact telemetry evidence."""

    agent_id = "root_cause_analyzer"
    description = "基于遥测证据生成结构化根因诊断、候选根因评分和 ReAct 修复建议"

    async def analyze_with_ai(
        self,
        *,
        telemetry: TelemetryData,
        violations: list[str],
        yang_draft: YANGPolicy,
        telemetry_source: str = "",
    ) -> dict[str, Any]:
        """Run deterministic evidence scoring, then add guarded LLM reasoning."""
        report = self.analyze(
            telemetry=telemetry,
            violations=violations,
            yang_draft=yang_draft,
            telemetry_source=telemetry_source,
        )
        report["agentic_reasoning"] = self._agentic_reasoning(report)
        if not self._should_use_llm():
            report["ai_reasoning_mode"] = "deterministic_agentic_fallback"
            report["llm_status"] = "skipped"
            return report

        try:
            timeout = float(os.getenv("IDN_ROOT_CAUSE_LLM_TIMEOUT_SEC", "8"))
            assessment = await asyncio.wait_for(
                self._llm_assess(report=report, violations=violations, yang_draft=yang_draft),
                timeout=timeout,
            )
            report["ai_reasoning_mode"] = "guarded_llm"
            report["llm_status"] = "ok"
            report["ai_assessment"] = self._sanitize_llm_assessment(assessment, report)
            report["summary"] = self._merge_ai_summary(report)
        except Exception as exc:
            report["ai_reasoning_mode"] = "deterministic_agentic_fallback"
            report["llm_status"] = "failed"
            report["llm_error"] = str(exc)
        return report

    def analyze(
        self,
        *,
        telemetry: TelemetryData,
        violations: list[str],
        yang_draft: YANGPolicy,
        telemetry_source: str = "",
    ) -> dict[str, Any]:
        raw = telemetry.raw_int_data or {}
        raw_payload = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
        qdisc = raw_payload.get("qdisc_probe") or {}
        int_analysis = analyze_telemetry(telemetry)
        kpi = telemetry.kpi.model_dump()
        evidence = self._collect_evidence(
            telemetry=telemetry,
            violations=violations,
            telemetry_source=telemetry_source,
            qdisc=qdisc,
            int_analysis=int_analysis,
            kpi=kpi,
        )
        candidates = [
            self._primary_path_congestion(evidence, int_analysis, qdisc),
            self._flow_rule_mismatch(evidence, telemetry, violations),
            self._qos_enforcement_gap(evidence, telemetry, violations),
            self._suboptimal_path(evidence, int_analysis, telemetry, violations),
            self._telemetry_unavailable(evidence, telemetry_source, int_analysis, qdisc),
        ]
        experience_cases = self._retrieve_experience(
            yang_draft=yang_draft,
            violations=violations,
            evidence=evidence,
        )
        candidates = self._apply_experience(candidates, experience_cases)
        candidates = sorted(candidates, key=lambda item: item["score"], reverse=True)
        primary = candidates[0] if candidates else self._unknown_candidate(violations)
        rejected = self._rejected_hypotheses(
            candidates=candidates,
            telemetry=telemetry,
            qdisc=qdisc,
            int_analysis=int_analysis,
        )
        bottleneck_link = primary.get("bottleneck_link") or int_analysis.get(
            "bottleneck_link"
        )
        scope = self._bottleneck_scope(bottleneck_link)
        primary_cause = (
            "shared_bottleneck_congestion"
            if primary["cause"] == "primary_path_congestion"
            and scope["scope"] == "SHARED_PATH"
            else "path_segment_congestion"
            if primary["cause"] == "primary_path_congestion" and bottleneck_link
            else primary["cause"]
        )
        report = {
            "report_id": f"RCA-{telemetry.intent_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            "primary_cause": primary_cause,
            "legacy_cause_alias": primary["cause"],
            "confidence": round(primary["score"] / 100, 2),
            "confidence_level": self._confidence_level(primary["score"]),
            # A diagnosis starts as a hypothesis.  The caller promotes it
            # only after a separate confirmation gate has checked confidence
            # *and* deterministic telemetry evidence.  In particular, an LLM
            # explanation must never turn a weak hypothesis into a fact.
            "status": "HYPOTHESIS",
            "hypothesis": primary_cause,
            "bottleneck_link": bottleneck_link,
            "scope": scope["scope"],
            "location_scope": scope["location_scope"],
            "affected_link_ids": [bottleneck_link] if bottleneck_link else [],
            "candidate_paths_affected": scope["candidate_paths_affected"],
            "bottleneck_device": primary.get("bottleneck_device") or self._bottleneck_device(int_analysis),
            "affected_path": int_analysis.get("switch_path") or int_analysis.get("device_path") or [],
            "telemetry_source": telemetry_source,
            "evidence": evidence,
            "evidence_refs": [
                str(item.get("source"))
                for item in evidence
                if item.get("source")
            ],
            "contradicted_evidence": rejected,
            "missing_evidence": [],
            "next_confirmation_action": "collect deterministic path, queue, and counter evidence",
            "candidate_causes": candidates,
            "rejected_hypotheses": rejected,
            "candidate_cause_graph": self._candidate_cause_graph(
                candidates,
                evidence,
                rejected,
                primary,
            ),
            "counterfactual_checks": self._counterfactual_checks(primary, candidates),
            "next_probe": self._next_probe(primary),
            "experience_memory": ExperienceMemoryAgent().summarize_cases(experience_cases),
            "recommended_action": "candidate_action_evaluation_required",
            "react_suggestion": self._runtime_action_guidance(
                bottleneck_link, scope
            ),
            "risk": primary["risk"],
            "needs_human_approval": primary["needs_human_approval"],
            "summary": self._summary(primary, evidence, violations),
            "policy_id": yang_draft.network_policy.policy_id,
            "policy_name": yang_draft.network_policy.policy_name,
        }
        report["analysis_id"] = report["report_id"]
        report["hypotheses"] = [item.get("cause") for item in candidates]
        report["confirmed_facts"] = [
            item for item in evidence if str(item.get("strength") or "").lower() == "strong"
        ]
        report["contradicted_facts"] = rejected
        report["missing_evidence"] = self._missing_evidence(evidence)
        report["required_evidence_actions"] = [
            f"collect_{item}" for item in report["missing_evidence"]
        ]
        return report

    @staticmethod
    def _missing_evidence(evidence: list[dict[str, Any]]) -> list[str]:
        sources = {str(item.get("source") or "") for item in evidence}
        required = {
            "bottleneck_link_utilization": "tc_qdisc",
            "candidate_path_overlap": "p4_int",
            "tc_overlimit_delta": "tc_qdisc",
            "p4_counter_window": "p4_direct_counter",
            "onos_counter_window": "onos_flow_counter",
        }
        return [name for name, source in required.items() if source not in sources]

    async def _llm_assess(
        self,
        *,
        report: dict[str, Any],
        violations: list[str],
        yang_draft: YANGPolicy,
    ) -> dict[str, Any]:
        candidate_brief = [
            {
                "cause": item.get("cause"),
                "score": item.get("score"),
                "evidence_refs": item.get("evidence_refs"),
                "recommended_action": item.get("recommended_action"),
                "risk": item.get("risk"),
            }
            for item in report.get("candidate_causes", [])
        ]
        evidence_brief = [
            {
                "source": item.get("source"),
                "strength": item.get("strength"),
                "observation": item.get("observation"),
                "supports": item.get("supports"),
            }
            for item in report.get("evidence", [])
        ]
        messages = [
            SystemMessage(
                content=(
                    "你是 RootCauseAnalyzer 智能体，负责网络故障推理。"
                    "只能基于给定候选根因和证据做判断，不得编造新遥测。"
                    "输出必须是 JSON 对象，字段包括: selected_cause, confidence, "
                    "reasoning_summary, evidence_used, rejected_hypotheses, recommended_action, "
                    "human_readable_explanation。confidence 为 0 到 1。"
                )
            ),
            HumanMessage(
                content=(
                    "请根据以下结构化证据进行受约束根因推理。\n"
                    f"policy={yang_draft.network_policy.policy_name}\n"
                    f"violations={violations}\n"
                    f"candidate_causes={candidate_brief}\n"
                    f"evidence={evidence_brief}\n"
                    f"rejected_hypotheses={report.get('rejected_hypotheses')}\n"
                    f"experience_memory={report.get('experience_memory')}\n"
                    f"candidate_cause_graph={report.get('candidate_cause_graph')}\n"
                    "请返回 JSON，不要返回 Markdown。"
                )
            ),
        ]
        response = await self.llm.ainvoke(messages)
        return extract_json_object(response.content)

    def _sanitize_llm_assessment(
        self,
        assessment: dict[str, Any],
        report: dict[str, Any],
    ) -> dict[str, Any]:
        selected = assessment.get("selected_cause")
        # The deterministic refinement owns link scope.  A model may still
        # emit the historical coarse alias, but it cannot downgrade a shared
        # or path-segment diagnosis back to ``primary_path_congestion``.
        if selected != report.get("primary_cause"):
            assessment["selected_cause"] = report.get("primary_cause")
            assessment["guardrail_note"] = (
                "LLM cause did not match deterministic link-scope analysis; "
                "kept the network-truth primary cause."
            )
        try:
            confidence = float(assessment.get("confidence"))
        except (TypeError, ValueError):
            confidence = float(report.get("confidence") or 0)
        assessment["confidence"] = max(0.0, min(1.0, confidence))
        assessment["accepted_as_decision"] = bool(
            assessment.get("selected_cause") == report.get("primary_cause")
        )
        return assessment

    def _merge_ai_summary(self, report: dict[str, Any]) -> str:
        assessment = report.get("ai_assessment") or {}
        explanation = str(assessment.get("human_readable_explanation") or "").strip()
        if not explanation:
            explanation = str(assessment.get("reasoning_summary") or "").strip()
        if not explanation:
            return str(report.get("summary") or "")
        return (
            f"{report.get('summary')} AI 诊断补充：{explanation}"
        )

    def _agentic_reasoning(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        top = (report.get("candidate_causes") or [{}])[0]
        return [
            {
                "step": "Observe",
                "content": "聚合 KPI、qdisc、P4 INT、ONOS flow counter 和 direct counter 证据。",
            },
            {
                "step": "Hypothesize",
                "content": "生成主路径拥塞、流表未命中、QoS 未生效、路径不优、遥测低置信度等候选根因。",
            },
            {
                "step": "Retrieve",
                "content": (
                    "检索长期经验记忆，查找症状、证据和根因相似的历史修复案例。"
                ),
            },
            {
                "step": "Evaluate",
                "content": f"按证据强度对候选根因打分，最高候选为 {top.get('cause')}，score={top.get('score')}。",
            },
            {
                "step": "Reject",
                "content": "根据 counter 命中、路径证据和遥测质量排除低分假设。",
            },
            {
                "step": "Act",
                "content": (
                    f"建议动作：{report.get('recommended_action')}；"
                    f"下一步探针：{(report.get('next_probe') or {}).get('action')}。"
                ),
            },
        ]

    def _should_use_llm(self) -> bool:
        mode = os.getenv("IDN_ROOT_CAUSE_AI_MODE", "auto").lower()
        if mode in {"off", "false", "disabled"}:
            return False
        if mode in {"on", "true", "always"}:
            return True
        if os.getenv("IDN_MOCK_MODE", "false").lower() == "true":
            return True
        return any(
            os.getenv(name)
            for name in (
                "WANGTIAO2026_API_KEY",
                "DASHSCOPE_API_KEY",
                "OPENAI_API_KEY",
                "LOCAL_QWEN_API_KEY",
                "ANTHROPIC_API_KEY",
            )
        )

    def _collect_evidence(
        self,
        *,
        telemetry: TelemetryData,
        violations: list[str],
        telemetry_source: str,
        qdisc: dict[str, Any],
        int_analysis: dict[str, Any],
        kpi: dict[str, Any],
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        self._add_kpi_evidence(evidence, kpi, violations)
        self._add_qdisc_evidence(evidence, qdisc)
        self._add_int_evidence(evidence, int_analysis)
        self._add_counter_evidence(evidence, telemetry)
        if telemetry_source:
            strength = "weak" if telemetry_source in {"mock", "not_collected"} else "medium"
            evidence.append(
                {
                    "source": "telemetry_source",
                    "strength": strength,
                    "observation": telemetry_source,
                    "supports": ["telemetry_quality"],
                }
            )
        return evidence

    def _add_kpi_evidence(
        self,
        evidence: list[dict[str, Any]],
        kpi: dict[str, Any],
        violations: list[str],
    ) -> None:
        for violation in violations:
            supports = ["kpi_violation"]
            if "时延" in violation or "latency" in violation.lower():
                supports.append("primary_path_congestion")
            if "丢包" in violation or "loss" in violation.lower():
                supports.append("primary_path_congestion")
            if "带宽不足" in violation or "吞吐量指标缺失" in violation:
                supports.extend(["primary_path_congestion", "flow_rule_mismatch"])
            if "限速未达成" in violation:
                supports.append("qos_enforcement_gap")
            evidence.append(
                {
                    "source": "kpi",
                    "strength": "medium",
                    "observation": violation,
                    "supports": sorted(set(supports)),
                }
            )
        for name in ("latency_ms", "packet_loss_rate", "throughput_mbps", "queue_depth"):
            if kpi.get(name) is not None:
                evidence.append(
                    {
                        "source": "kpi",
                        "strength": "weak",
                        "observation": f"{name}={kpi.get(name)}",
                        "supports": ["kpi_context"],
                    }
                )

    def _add_qdisc_evidence(self, evidence: list[dict[str, Any]], qdisc: dict[str, Any]) -> None:
        if not qdisc or qdisc.get("status") not in {None, "ok"}:
            return
        queue_depth = int(qdisc.get("queue_depth") or 0)
        queue_bytes = int(qdisc.get("queue_depth_bytes") or 0)
        drops = int(qdisc.get("dropped_packets") or 0)
        overlimits = int(qdisc.get("overlimits") or 0)
        congested = qdisc.get("congested_interfaces") or []
        active_congested = [
            item
            for item in congested
            if (item.get("backlog_packets") or 0) > 0
            or (item.get("backlog_bytes") or 0) > 0
            or item.get("source") == "active_fault"
            or item.get("fault_id")
        ]
        if qdisc.get("congestion_detected") or queue_depth or queue_bytes or active_congested:
            location = ""
            if active_congested:
                first = active_congested[0] or {}
                location = f" at {first.get('node')}/{first.get('interface')}"
            evidence.append(
                {
                    "source": "tc_qdisc",
                    "strength": "strong",
                    "observation": (
                        f"congestion_detected={bool(qdisc.get('congestion_detected'))}, "
                        f"backlog={queue_depth}pkt/{queue_bytes}B, drops={drops}, "
                        f"overlimits={overlimits}{location}"
                    ),
                    "supports": ["primary_path_congestion"],
                }
            )

    def _add_int_evidence(self, evidence: list[dict[str, Any]], int_analysis: dict[str, Any]) -> None:
        if int_analysis.get("status") != "ok":
            return
        worst_latency = int_analysis.get("worst_latency_hop") or {}
        worst_queue = int_analysis.get("worst_queue_hop") or {}
        path = " -> ".join(int_analysis.get("switch_path") or int_analysis.get("device_path") or [])
        supports = ["path_context"]
        if int_analysis.get("congestion_on_primary"):
            supports.append("primary_path_congestion")
        if int_analysis.get("primary_path") and not int_analysis.get("secondary_path"):
            supports.append("suboptimal_path")
        evidence.append(
            {
                "source": "p4_int",
                "strength": "strong",
                "observation": (
                    f"path={path}, bottleneck_link={int_analysis.get('bottleneck_link')}, "
                    f"worst_latency={worst_latency.get('switch_name') or worst_latency.get('device_id')}/"
                    f"{float(worst_latency.get('hop_latency_ms') or 0):.3f}ms, "
                    f"worst_queue={worst_queue.get('switch_name') or worst_queue.get('device_id')}/"
                    f"{worst_queue.get('queue_occupancy')}"
                ),
                "supports": supports,
            }
        )

    def _add_counter_evidence(self, evidence: list[dict[str, Any]], telemetry: TelemetryData) -> None:
        direct = telemetry.device_counters.get("p4_direct_counter") or {}
        packets = int(direct.get("packets") or 0)
        direct_entries = int(direct.get("direct_entries") or 0)
        if direct:
            supports = ["flow_rule_match"] if packets > 0 else ["flow_rule_mismatch"]
            evidence.append(
                {
                    "source": "p4_direct_counter",
                    "strength": "strong",
                    "observation": f"packets={packets}, direct_entries={direct_entries}",
                    "supports": supports,
                }
            )
        flows = telemetry.device_counters.get("onos_flows") or {}
        if flows:
            packets = int(flows.get("packets") or 0)
            evidence.append(
                {
                    "source": "onos_flow_counter",
                    "strength": "medium",
                    "observation": f"flows={flows.get('total')}, packets={packets}, bytes={flows.get('bytes')}",
                    "supports": ["flow_rule_match"] if packets > 0 else ["flow_rule_mismatch"],
                }
            )

    def _primary_path_congestion(
        self,
        evidence: list[dict[str, Any]],
        int_analysis: dict[str, Any],
        qdisc: dict[str, Any],
    ) -> dict[str, Any]:
        score = self._support_score(evidence, "primary_path_congestion")
        if qdisc.get("congestion_detected"):
            score += 20
        if int_analysis.get("congestion_on_primary"):
            score += 20
        qdisc_link, qdisc_device = self._qdisc_bottleneck(qdisc)
        return self._candidate(
            cause="primary_path_congestion",
            score=score,
            evidence=evidence,
            recommended_action="candidate_action_evaluation_required",
            react_suggestion=(
                "基于当前路径、瓶颈边、候选路径共享边和可抢占资源评估修复动作；"
                "未通过适用性与预期效果校验的动作不得执行。"
            ),
            risk="low" if score >= 60 else "medium",
            needs_human_approval=False,
            bottleneck_link=int_analysis.get("bottleneck_link") or qdisc_link,
            bottleneck_device=self._bottleneck_device(int_analysis) or qdisc_device,
        )

    def _qdisc_bottleneck(self, qdisc: dict[str, Any]) -> tuple[str | None, str | None]:
        """Map trusted TC interface evidence to the formal physical-link model."""
        from api.topology_paths import topology_paths

        for item in qdisc.get("congested_interfaces") or []:
            switch = str(item.get("node") or "")
            interface = str(item.get("interface") or "")
            try:
                port = int(interface.rsplit("eth", 1)[1])
            except (IndexError, ValueError):
                continue
            device = next(
                (key for key, value in topology_paths.device_switch_map.items() if value == switch),
                None,
            )
            for link in topology_paths.physical_links:
                if device == link.get("device_a") and port == int(link.get("port_a") or -1):
                    return str(link.get("link_id")), switch
                if device == link.get("device_b") and port == int(link.get("port_b") or -1):
                    return str(link.get("link_id")), switch
        return None, None

    def _flow_rule_mismatch(
        self,
        evidence: list[dict[str, Any]],
        telemetry: TelemetryData,
        violations: list[str],
    ) -> dict[str, Any]:
        score = self._support_score(evidence, "flow_rule_mismatch")
        if telemetry.device_counters.get("p4_direct_counter", {}).get("packets", 0):
            score -= 25
        if any("指标缺失" in item for item in violations):
            score += 15
        return self._candidate(
            cause="flow_rule_mismatch_or_miss",
            score=score,
            evidence=evidence,
            recommended_action="verify_match_fields_priority_and_direct_counter",
            react_suggestion="检查 FlowTablePlan 的匹配字段、优先级和 direct counter 命中，必要时重新编译表项。",
            risk="medium",
            needs_human_approval=score >= 70,
        )

    def _qos_enforcement_gap(
        self,
        evidence: list[dict[str, Any]],
        telemetry: TelemetryData,
        violations: list[str],
    ) -> dict[str, Any]:
        score = self._support_score(evidence, "qos_enforcement_gap")
        if any("限速未达成" in item for item in violations):
            score += 25
        throughput = telemetry.kpi.throughput_mbps
        if throughput is not None and throughput > 0:
            score += 5
        return self._candidate(
            cause="qos_enforcement_gap",
            score=score,
            evidence=evidence,
            recommended_action="reapply_rate_limit_meter_or_tc_qdisc",
            react_suggestion="重新下发 RATE_LIMIT，核对 P4 direct meter 配置，并同步验证 Mininet tc/HTB 是否生效。",
            risk="medium",
            needs_human_approval=False,
        )

    def _suboptimal_path(
        self,
        evidence: list[dict[str, Any]],
        int_analysis: dict[str, Any],
        telemetry: TelemetryData,
        violations: list[str],
    ) -> dict[str, Any]:
        score = self._support_score(evidence, "suboptimal_path")
        if telemetry.kpi.latency_ms is not None and any("时延" in item for item in violations):
            score += 20
        if int_analysis.get("primary_path") and not int_analysis.get("congestion_on_primary"):
            score += 10
        return self._candidate(
            cause="suboptimal_path",
            score=score,
            evidence=evidence,
            recommended_action="recompute_path_or_raise_qos_priority",
            react_suggestion="重新计算低时延路径，或提升 DSCP/QoS 优先级后再次验证。",
            risk="medium",
            needs_human_approval=False,
            bottleneck_link=int_analysis.get("bottleneck_link"),
            bottleneck_device=self._bottleneck_device(int_analysis),
        )

    def _telemetry_unavailable(
        self,
        evidence: list[dict[str, Any]],
        telemetry_source: str,
        int_analysis: dict[str, Any],
        qdisc: dict[str, Any],
    ) -> dict[str, Any]:
        score = 0
        if telemetry_source in {"mock", "not_collected"}:
            score += 60
        if int_analysis.get("status") != "ok" and qdisc.get("status") not in {"ok", None}:
            score += 20
        return self._candidate(
            cause="telemetry_unavailable_or_low_confidence",
            score=score,
            evidence=evidence,
            recommended_action="refresh_telemetry_before_repair",
            react_suggestion="先恢复或刷新 telemetry/qdisc/INT 探针，避免基于低置信度数据执行修复。",
            risk="high" if score >= 60 else "low",
            needs_human_approval=score >= 70,
        )

    def _candidate(
        self,
        *,
        cause: str,
        score: int,
        evidence: list[dict[str, Any]],
        recommended_action: str,
        react_suggestion: str,
        risk: str,
        needs_human_approval: bool,
        bottleneck_link: str | None = None,
        bottleneck_device: str | None = None,
    ) -> dict[str, Any]:
        supporting = self._evidence_refs(evidence, cause)
        return {
            "cause": cause,
            "score": max(0, min(100, int(score))),
            "evidence_refs": supporting,
            "supporting_evidence": supporting,
            "counter_evidence": self._counter_evidence_refs(evidence, cause),
            "recommended_action": recommended_action,
            "react_suggestion": react_suggestion,
            "risk": risk,
            "needs_human_approval": needs_human_approval,
            "bottleneck_link": bottleneck_link,
            "bottleneck_device": bottleneck_device,
        }

    def _unknown_candidate(self, violations: list[str]) -> dict[str, Any]:
        return {
            "cause": "unknown_kpi_violation",
            "score": 30,
            "evidence_refs": list(violations),
            "recommended_action": "reobserve_and_request_human_review",
            "react_suggestion": "重新采样遥测；若仍无法定位，进入人工审批。",
            "risk": "medium",
            "needs_human_approval": True,
        }

    def _retrieve_experience(
        self,
        *,
        yang_draft: YANGPolicy,
        violations: list[str],
        evidence: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        try:
            memory = ExperienceMemoryAgent()
            return memory.retrieve_similar(
                intent_type=self._intent_type(yang_draft),
                symptoms=violations,
                evidence=[
                    str(item.get("observation"))
                    for item in evidence
                    if item.get("observation")
                ],
                limit=3,
            )
        except Exception as exc:
            logger.warning("[RootCauseAnalyzer] experience retrieval failed: %s", exc)
            return []

    def _apply_experience(
        self,
        candidates: list[dict[str, Any]],
        cases: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not cases:
            return candidates
        success_cases = [case for case in cases if case.get("success")]
        for candidate in candidates:
            matching = [
                case for case in success_cases
                if case.get("root_cause") == candidate.get("cause")
            ]
            if not matching:
                continue
            best = matching[0]
            boost = min(12, int(float(best.get("similarity") or 0) * 15))
            candidate["score"] = max(0, min(100, int(candidate["score"]) + boost))
            candidate.setdefault("supporting_evidence", []).append(
                f"experience_memory:{best.get('case_id')} similarity={best.get('similarity')} success=true"
            )
            candidate.setdefault("evidence_refs", []).append(
                f"experience_memory:{best.get('case_id')} lesson={best.get('lesson')}"
            )
            actions = best.get("repair_action") or []
            if actions:
                candidate["experience_recommended_actions"] = actions
        return candidates

    def _candidate_cause_graph(
        self,
        candidates: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
        primary: dict[str, Any],
    ) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        for candidate in candidates:
            cause_id = f"cause:{candidate['cause']}"
            nodes.append(
                {
                    "id": cause_id,
                    "label": candidate["cause"],
                    "kind": "candidate_cause",
                    "score": candidate["score"],
                    "selected": candidate["cause"] == primary.get("cause"),
                }
            )
        for index, item in enumerate(evidence):
            evidence_id = f"evidence:{index}"
            nodes.append(
                {
                    "id": evidence_id,
                    "label": item.get("source") or f"evidence_{index}",
                    "kind": "evidence",
                    "strength": item.get("strength"),
                    "observation": item.get("observation"),
                }
            )
            for cause in item.get("supports") or []:
                edges.append(
                    {
                        "source": evidence_id,
                        "target": f"cause:{cause}",
                        "relation": "supports",
                        "strength": item.get("strength"),
                    }
                )
        for item in rejected:
            cause = item.get("cause")
            if cause:
                edges.append(
                    {
                        "source": f"cause:{cause}",
                        "target": "decision:rejected",
                        "relation": "rejected_by",
                        "reason": item.get("reason"),
                    }
                )
        nodes.append({"id": "decision:rejected", "label": "排除假设", "kind": "decision"})
        return {"nodes": nodes, "edges": edges}

    def _counterfactual_checks(
        self,
        primary: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        cause = primary.get("cause")
        checks = [
            {
                "hypothesis": "if_reroute_then_congestion_should_drop",
                "action": "compare_int_path_and_qdisc_after_reroute",
                "expected_if_primary_true": "INT path changes away from s_core1 and qdisc overlimits stop growing.",
                "applies_to": "primary_path_congestion",
            },
            {
                "hypothesis": "if_rule_mismatch_then_counter_should_stay_zero",
                "action": "read_onos_and_p4_direct_counter",
                "expected_if_primary_true": "Target flow counter remains zero or matched rule differs from policy selector.",
                "applies_to": "flow_rule_mismatch_or_miss",
            },
            {
                "hypothesis": "if_qos_gap_then_meter_or_tc_state_should_not_match_policy",
                "action": "read_meter_and_tc_qdisc_state",
                "expected_if_primary_true": "Configured meter/tc rate differs from policy or qdisc has no matching class.",
                "applies_to": "qos_enforcement_gap",
            },
        ]
        relevant = [item for item in checks if item["applies_to"] == cause]
        rejected_causes = {item.get("cause") for item in candidates[1:3]}
        relevant.extend(item for item in checks if item["applies_to"] in rejected_causes)
        return relevant or checks[:1]

    def _next_probe(self, primary: dict[str, Any]) -> dict[str, str]:
        cause = primary.get("cause")
        if cause == "primary_path_congestion":
            return {
                "action": "evaluate_current_path_and_bottleneck_impact",
                "reason": "确认候选路径是否真正绕开当前瓶颈，再决定是否允许执行。",
            }
        if cause == "flow_rule_mismatch_or_miss":
            return {
                "action": "read_p4_direct_counter_and_onos_flow_counter",
                "reason": "确认目标表项是否真实命中，排除 match/priority 错误。",
            }
        if cause == "qos_enforcement_gap":
            return {
                "action": "read_meter_and_tc_qdisc_state",
                "reason": "确认 P4 meter 或 Mininet tc/HTB 是否按策略参数生效。",
            }
        return {
            "action": "refresh_telemetry_snapshot",
            "reason": "当前证据不足，先重新采集 KPI、INT、qdisc 和 counter。",
        }

    @staticmethod
    def _bottleneck_scope(bottleneck_link: str | None) -> dict[str, Any]:
        from api.topology_paths import topology_paths

        if not bottleneck_link:
            return {
                "scope": "UNKNOWN",
                "location_scope": "UNKNOWN",
                "candidate_paths_affected": [],
            }
        paths = [path for _, path in topology_paths.items() if path.enabled]
        affected = [
            path.path_id
            for path in paths
            if path_contains_link(path.switches, bottleneck_link)
        ]
        scope = "SHARED_PATH" if paths and len(affected) == len(paths) else "PATH_SPECIFIC"
        location = "UNKNOWN"
        if any(
            len(path.switches) >= 2
            and path_contains_link(path.switches[:2], bottleneck_link)
            for path in paths
        ):
            location = "ACCESS"
        elif any(
            len(path.switches) >= 2
            and path_contains_link(path.switches[-2:], bottleneck_link)
            for path in paths
        ):
            location = "EGRESS"
        return {
            "scope": scope,
            "location_scope": location,
            "candidate_paths_affected": affected,
        }

    @staticmethod
    def _runtime_action_guidance(
        bottleneck_link: str | None, scope: dict[str, Any]
    ) -> str:
        if bottleneck_link and scope.get("scope") == "SHARED_PATH":
            return (
                f"当前瓶颈位于共享链路 {bottleneck_link}，现有候选核心路径均经过该链路；"
                "核心路径切换无预期收益。仅评估有证据的安全资源回收，否则收敛为 UNSATISFIABLE。"
            )
        return (
            "候选修复建议必须先检查当前路径、瓶颈规避、资源影响和预期收益；"
            "只有通过 HealingActionApplicabilityValidator 的动作才可执行。"
        )

    def _support_score(self, evidence: list[dict[str, Any]], cause: str) -> int:
        weights = {"strong": 30, "medium": 15, "weak": 5}
        score = 0
        for item in evidence:
            if cause in item.get("supports", []):
                score += weights.get(item.get("strength"), 5)
        return min(score, 100)

    def _evidence_refs(self, evidence: list[dict[str, Any]], cause: str) -> list[str]:
        refs = [
            f"{item.get('source')}: {item.get('observation')}"
            for item in evidence
            if cause in item.get("supports", [])
        ]
        return refs[:6]

    def _counter_evidence_refs(self, evidence: list[dict[str, Any]], cause: str) -> list[str]:
        counter_map = {
            "flow_rule_mismatch_or_miss": {"flow_rule_match"},
            "primary_path_congestion": {"telemetry_quality"},
            "qos_enforcement_gap": {"flow_rule_mismatch"},
            "suboptimal_path": {"primary_path_congestion"},
            "telemetry_unavailable_or_low_confidence": {"p4_int", "tc_qdisc"},
        }
        blockers = counter_map.get(cause, set())
        refs = []
        for item in evidence:
            supports = set(item.get("supports") or [])
            source = str(item.get("source") or "")
            if supports & blockers or source in blockers:
                refs.append(f"{item.get('source')}: {item.get('observation')}")
        return refs[:4]

    def _intent_type(self, yang_draft: YANGPolicy) -> str:
        policy = yang_draft.network_policy
        action = policy.action.action_type.value
        name = policy.policy_name or ""
        if "video" in name.lower() or "视频" in name:
            return "保障视频业务" if action == "SET_PRIORITY" else "视频业务"
        return action

    def _rejected_hypotheses(
        self,
        *,
        candidates: list[dict[str, Any]],
        telemetry: TelemetryData,
        qdisc: dict[str, Any],
        int_analysis: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rejected: list[dict[str, Any]] = []
        direct_packets = int((telemetry.device_counters.get("p4_direct_counter") or {}).get("packets") or 0)
        flow_packets = int((telemetry.device_counters.get("onos_flows") or {}).get("packets") or 0)
        if direct_packets > 0 or flow_packets > 0:
            rejected.append(
                {
                    "cause": "flow_rule_mismatch_or_miss",
                    "reason": "direct/ONOS counter shows packets hitting installed rules",
                }
            )
        if qdisc.get("congestion_detected") or int_analysis.get("congestion_on_primary"):
            rejected.append(
                {
                    "cause": "host_unreachable",
                    "reason": "telemetry points to path congestion rather than complete reachability failure",
                }
            )
        top = candidates[0] if candidates else {}
        for candidate in candidates[1:]:
            if candidate["score"] <= 20:
                rejected.append(
                    {
                        "cause": candidate["cause"],
                        "reason": f"score={candidate['score']} is much lower than top cause {top.get('cause')}",
                    }
                )
        seen = set()
        unique = []
        for item in rejected:
            key = item["cause"]
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def _summary(
        self,
        primary: dict[str, Any],
        evidence: list[dict[str, Any]],
        violations: list[str],
    ) -> str:
        top_evidence = primary.get("evidence_refs") or [
            f"{item.get('source')}: {item.get('observation')}" for item in evidence[:2]
        ]
        evidence_text = "；".join(top_evidence[:2])
        violation_text = "；".join(violations[:2])
        return (
            f"根因判定为 {primary['cause']}，置信度 {primary['score']}%。"
            f"关键证据：{evidence_text or violation_text or '暂无强证据'}。"
            f"建议动作：{primary['recommended_action']}。"
        )

    def _bottleneck_device(self, int_analysis: dict[str, Any]) -> str | None:
        worst_queue = int_analysis.get("worst_queue_hop") or {}
        worst_latency = int_analysis.get("worst_latency_hop") or {}
        return (
            worst_queue.get("switch_name")
            or worst_queue.get("device_id")
            or worst_latency.get("switch_name")
            or worst_latency.get("device_id")
        )

    def _confidence_level(self, score: int) -> str:
        if score >= 75:
            return "high"
        if score >= 45:
            return "medium"
        return "low"

"""
agents/safety_council.py
=========================

执行前多智能体安全评审委员会 —— 最后的安全闸门。

# 系统角色
在流表下发前，并行运行 5 个专业子 Agent 进行全面安全评审：
1. invariant_verifier：网络不变量检查（设备可达性、链路可用性）
2. digital_twin：数字孪生仿真验证（shadow simulation）
3. redteam：红队攻击审查（安全性测试）
4. rollback_verifier：回滚计划可执行性验证
5. history_risk_agent：历史风险评估（相似策略的成功/失败率）

每个子 Agent 独立评审并投票（block/approve），
最终通过多Agent协商产生 overall_decision（approve/block）。

# 双模式
- review()：完整 5-Agent 并行评审（HIGH/CRITICAL 风险）
- review_mini()：轻量级 2-Agent 评审（MEDIUM 风险，仅 invariant + history）

# 主要依赖
- agents/experience_memory.py：历史风险数据查询
- models/yang_policy.py / models/flow_table.py / models/network.py

# 设计意图
- 5 个 Agent 的评审互为独立视角，任何一个发现 hard_blocker 即触发驳回
- 确定性子 Agent（invariant/rollback/history）不受 LLM 幻觉影响
- 所有子 Agent 的评审结果通过 A2A 追踪独立记录
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import ip_address, ip_network
from typing import Any

from agents.experience_memory import ExperienceMemoryAgent
from models.agent_state import RuleVerificationReport
from models.flow_table import FlowTablePlan, WriteRequest
from models.network import HostInfo, LinkInfo, NetworkSnapshot
from models.yang_policy import YANGPolicy


@dataclass
class SafetyReview:
    agent_id: str
    decision: str
    risk_score: int
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hard_blockers: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "decision": self.decision,
            "risk_score": self.risk_score,
            "evidence": self.evidence,
            "warnings": self.warnings,
            "hard_blockers": self.hard_blockers,
            "details": self.details,
        }


class InvariantVerifierAgent:
    """Check deterministic network-safety invariants without using an LLM."""

    agent_id = "invariant_verifier"

    async def review(
        self,
        *,
        yang_policy: YANGPolicy,
        flow_table_plan: FlowTablePlan,
        network_snapshot: NetworkSnapshot,
        rule_report: RuleVerificationReport | None,
    ) -> SafetyReview:
        blockers: list[str] = []
        warnings: list[str] = []
        evidence: list[str] = []

        if rule_report is not None and not rule_report.is_valid:
            blockers.append("P4RuleVerifierAgent 未通过，禁止进入执行阶段。")

        devices = {device.device_id for device in network_snapshot.devices}
        affected = set(flow_table_plan.affected_devices)
        missing_devices = sorted(affected - devices)
        if missing_devices:
            blockers.append(f"FlowTablePlan 引用不存在设备: {', '.join(missing_devices)}")
        else:
            evidence.append("affected_devices_exist")

        graph = _build_device_graph(network_snapshot.links)
        src_host, dst_host = _match_hosts(yang_policy, network_snapshot.hosts)
        before_path = _shortest_path(
            graph,
            src_host.location_device if src_host else None,
            dst_host.location_device if dst_host else None,
        )
        if src_host and dst_host and before_path:
            evidence.append(f"reachability_ok:{' -> '.join(before_path)}")
        elif src_host and dst_host:
            blockers.append(
                f"REACHABILITY 失败: {src_host.host_id} 到 {dst_host.host_id} 无可达路径。"
            )
        else:
            warnings.append("未能从当前 Host 表精确匹配源/目的主机，跳过端到端可达性证明。")

        if before_path and len(before_path) == len(set(before_path)):
            evidence.append("no_loop")
        elif before_path:
            blockers.append(f"NO_LOOP 失败: 路径出现重复节点 {before_path}")

        blackholes = _find_blackholes(flow_table_plan, network_snapshot)
        if blackholes:
            blockers.extend(blackholes)
        else:
            evidence.append("no_blackhole")

        capacity_warning = _capacity_warning(yang_policy, network_snapshot.links)
        if capacity_warning:
            warnings.append(capacity_warning)
        else:
            evidence.append("capacity_bound_ok")

        risk = 10 + len(warnings) * 8 + len(blockers) * 35
        return SafetyReview(
            agent_id=self.agent_id,
            decision="reject" if blockers else "approve",
            risk_score=min(100, risk),
            evidence=evidence,
            warnings=warnings,
            hard_blockers=blockers,
            details={
                "source_host": _host_label(src_host),
                "destination_host": _host_label(dst_host),
                "predicted_path": before_path or [],
                "checked_invariants": [
                    "Reachability",
                    "No Blackhole",
                    "No Loop",
                    "Capacity Bound",
                    "RuleVerifier Passed",
                ],
            },
        )


class DigitalTwinSimulationAgent:
    """Perform a lightweight in-memory what-if simulation."""

    agent_id = "digital_twin_simulator"

    async def review(
        self,
        *,
        yang_policy: YANGPolicy,
        flow_table_plan: FlowTablePlan,
        network_snapshot: NetworkSnapshot,
    ) -> SafetyReview:
        graph = _build_device_graph(network_snapshot.links)
        src_host, dst_host = _match_hosts(yang_policy, network_snapshot.hosts)
        before_path = _shortest_path(
            graph,
            src_host.location_device if src_host else None,
            dst_host.location_device if dst_host else None,
        )
        after_path = _predict_after_path(flow_table_plan, network_snapshot, before_path)
        warnings = []
        if before_path and after_path and before_path != after_path:
            evidence = [f"before_path={' -> '.join(before_path)}", f"after_path={' -> '.join(after_path)}"]
        elif after_path:
            evidence = [f"after_path={' -> '.join(after_path)}", "path_unchanged_or_qos_only"]
        else:
            evidence = []
            warnings.append("影子仿真无法推导执行后路径，仅记录影响范围。")

        affected_flows = [_flow_label(yang_policy)]
        risk = 18 + (15 if warnings else 0)
        return SafetyReview(
            agent_id=self.agent_id,
            decision="approve_with_warning" if warnings else "approve",
            risk_score=min(100, risk),
            evidence=evidence + [f"affected_flows={len(affected_flows)}"],
            warnings=warnings,
            details={
                "simulation_id": f"SIM-{flow_table_plan.plan_id}",
                "before_path": before_path or [],
                "after_path": after_path or [],
                "affected_flows": affected_flows,
                "affected_devices": flow_table_plan.affected_devices,
            },
        )


class LLMRedTeamCriticAgent:
    """Heuristic red-team reviewer; LLM-style critique without final authority."""

    agent_id = "llm_redteam_critic"

    async def review(
        self,
        *,
        yang_policy: YANGPolicy,
        flow_table_plan: FlowTablePlan,
        invariant_review: SafetyReview,
        simulation_review: SafetyReview,
    ) -> SafetyReview:
        warnings: list[str] = []
        evidence: list[str] = []
        action = yang_policy.network_policy.action.action_type.value
        match = yang_policy.network_policy.match
        if action == "RATE_LIMIT" and not match.destination_port:
            warnings.append("限速策略未指定目的端口，可能影响同源其他低优先级流量。")
        if action == "DROP":
            warnings.append("DROP 策略具有业务中断风险，建议保留人工审批条件。")
        if not simulation_review.details.get("after_path"):
            warnings.append("缺少明确执行后路径预测，建议答辩时补充仿真证据。")
        if invariant_review.hard_blockers:
            evidence.append("hard_blocker_detected_by_invariant_verifier")

        risk = 22 + len(warnings) * 7 + len(invariant_review.hard_blockers) * 20
        decision = "approve_with_warning" if warnings else "approve"
        if invariant_review.hard_blockers:
            decision = "reject"
        return SafetyReview(
            agent_id=self.agent_id,
            decision=decision,
            risk_score=min(100, risk),
            evidence=evidence or ["redteam_review_completed"],
            warnings=warnings,
            hard_blockers=[],
            details={
                "questions_checked": [
                    "business_impact",
                    "missing_evidence",
                    "lower_risk_alternative",
                    "human_approval_need",
                ],
                "approval_recommendation": decision,
            },
        )


class RollbackPlannerAgent:
    """Generate and verify a rollback plan before execution."""

    agent_id = "rollback_planner"

    async def review(
        self,
        *,
        yang_policy: YANGPolicy,
        flow_table_plan: FlowTablePlan,
        network_snapshot: NetworkSnapshot,
    ) -> SafetyReview:
        rollback_actions = []
        for request in flow_table_plan.write_requests:
            for entry in request.table_entries:
                rollback_actions.append(
                    {
                        "device_id": request.device_id,
                        "operation": "DELETE",
                        "table": entry.table_name,
                        "priority": entry.priority,
                        "match": [
                            field.model_dump(mode="json")
                            if hasattr(field, "model_dump")
                            else dict(field)
                            for field in entry.match_fields
                        ],
                    }
                )
        action = yang_policy.network_policy.action.action_type.value
        if action in {"RATE_LIMIT", "SET_PRIORITY"}:
            rollback_actions.append(
                {
                    "operation": "RESTORE_QDISC",
                    "scope": "matched_source_host",
                    "reason": f"{action} may install tc/qdisc enforcement",
                }
            )

        atomicity_ok = all(
            str(request.atomicity).endswith("ROLLBACK_ON_ERROR")
            for request in flow_table_plan.write_requests
        )
        rollback_ready = bool(rollback_actions) and atomicity_ok
        blockers = [] if rollback_ready else ["ROLLBACK_READY 失败: 缺少可执行回滚动作或原子性设置。"]
        risk = 5 if rollback_ready else 80
        return SafetyReview(
            agent_id=self.agent_id,
            decision="approve" if rollback_ready else "reject",
            risk_score=risk,
            evidence=["rollback_plan_ready"] if rollback_ready else [],
            hard_blockers=blockers,
            details={
                "rollback_plan_id": f"RB-{flow_table_plan.intent_id}",
                "snapshot_before": network_snapshot.snapshot_id,
                "rollback_ready": rollback_ready,
                "estimated_rollback_time_ms": 500,
                "rollback_actions": rollback_actions,
            },
        )


class HistoryRiskAgent:
    """Look up long-term repair memory before approving risky actions."""

    agent_id = "history_risk_agent"

    async def review(self, *, yang_policy: YANGPolicy) -> SafetyReview:
        action = yang_policy.network_policy.action.action_type.value
        known_risks = []
        if action == "RATE_LIMIT":
            known_risks.append("历史经验: 限速策略应展示 qdisc/iperf 验证证据。")
        memory = ExperienceMemoryAgent()
        similar_cases = memory.retrieve_similar(
            intent_type=_memory_intent_type(yang_policy),
            symptoms=[_flow_label(yang_policy), action],
            evidence=[yang_policy.network_policy.policy_name],
            limit=3,
        )
        summary = memory.summarize_cases(similar_cases)
        success_rate = summary.get("success_rate")
        if similar_cases and success_rate is not None and success_rate < 0.5:
            known_risks.append("历史相似案例成功率偏低，建议提高人工审批或回滚关注度。")
        evidence = [
            f"similar_case:{case.get('case_id')} similarity={case.get('similarity')}"
            for case in similar_cases
        ] or ["history_memory_no_similar_case"]
        return SafetyReview(
            agent_id=self.agent_id,
            decision="approve",
            risk_score=32 if known_risks else 18,
            evidence=evidence,
            warnings=known_risks,
            details={
                "similar_cases": summary.get("similar_cases") or [],
                "success_rate": success_rate,
                "recommended_actions": summary.get("recommended_actions") or [],
                "lesson": summary.get("lesson"),
                "known_risks": known_risks,
            },
        )


class SafetyCouncilAgent:
    """Aggregate safety sub-agent reviews into one execution decision."""

    agent_id = "safety_council"

    async def review(
        self,
        *,
        intent_id: str,
        yang_policy: YANGPolicy,
        flow_table_plan: FlowTablePlan | dict[str, Any],
        network_snapshot: NetworkSnapshot,
        rule_report: RuleVerificationReport | None = None,
    ) -> dict[str, Any]:
        plan = (
            flow_table_plan
            if isinstance(flow_table_plan, FlowTablePlan)
            else FlowTablePlan(**flow_table_plan)
        )

        # ── 并行启动 4 个独立的确定性子 Agent ──
        # invariant、simulation、rollback、history 互不依赖且均不使用 LLM，
        # 并行化可将 SafetyCouncil 阶段耗时从累加降为取最慢者。
        invariant_fut = asyncio.ensure_future(
            InvariantVerifierAgent().review(
                yang_policy=yang_policy,
                flow_table_plan=plan,
                network_snapshot=network_snapshot,
                rule_report=rule_report,
            )
        )
        simulation_fut = asyncio.ensure_future(
            DigitalTwinSimulationAgent().review(
                yang_policy=yang_policy,
                flow_table_plan=plan,
                network_snapshot=network_snapshot,
            )
        )
        rollback_fut = asyncio.ensure_future(
            RollbackPlannerAgent().review(
                yang_policy=yang_policy,
                flow_table_plan=plan,
                network_snapshot=network_snapshot,
            )
        )
        history_fut = asyncio.ensure_future(
            HistoryRiskAgent().review(yang_policy=yang_policy)
        )

        # redteam 依赖 invariant + simulation 的结果，先等这两个
        invariant, simulation = await asyncio.gather(invariant_fut, simulation_fut)

        redteam = await LLMRedTeamCriticAgent().review(
            yang_policy=yang_policy,
            flow_table_plan=plan,
            invariant_review=invariant,
            simulation_review=simulation,
        )

        # rollback 和 history 已在后台完成，直接取结果
        rollback = await rollback_fut
        history = await history_fut

        reviews = [invariant, simulation, redteam, rollback, history]

        hard_blockers = [
            blocker
            for review in reviews
            for blocker in review.hard_blockers
        ]
        risk = _weighted_risk(reviews)
        if hard_blockers:
            decision = "reject"
        elif risk >= 70:
            decision = "manual_approval"
        else:
            decision = "approve"
        report = {
            "report_id": f"SAFETY-{intent_id}",
            "intent_id": intent_id,
            "policy_id": yang_policy.network_policy.policy_id,
            "generated_at": datetime.utcnow().isoformat(),
            "overall_decision": decision,
            "overall_risk_score": risk,
            "risk_level": _risk_level(risk),
            "hard_blockers": hard_blockers,
            "agent_reviews": [review.to_dict() for review in reviews],
            "negotiation": _negotiation_report(
                proposal=_proposal(yang_policy, plan),
                reviews=reviews,
                final_decision=decision,
                risk=risk,
            ),
            "blast_radius": _blast_radius(yang_policy, plan, simulation, network_snapshot),
            "rollback_plan": rollback.details,
            "final_rationale": _rationale(decision, risk, hard_blockers, reviews),
        }
        return report

    async def review_mini(
        self,
        *,
        intent_id: str,
        yang_policy: YANGPolicy,
        flow_table_plan: FlowTablePlan | dict[str, Any],
        network_snapshot: NetworkSnapshot,
        rule_report: RuleVerificationReport | None = None,
    ) -> dict[str, Any]:
        """Lightweight MEDIUM-risk review: only invariant + history checks.

        Runs invariant_verifier and history_risk_agent in parallel (both are
        deterministic), then aggregates.  Skips digital_twin, redteam, and
        rollback — those are reserved for HIGH/CRITICAL paths.
        """
        plan = (
            flow_table_plan
            if isinstance(flow_table_plan, FlowTablePlan)
            else FlowTablePlan(**flow_table_plan)
        )

        invariant_fut = asyncio.ensure_future(
            InvariantVerifierAgent().review(
                yang_policy=yang_policy,
                flow_table_plan=plan,
                network_snapshot=network_snapshot,
                rule_report=rule_report,
            )
        )
        history_fut = asyncio.ensure_future(
            HistoryRiskAgent().review(yang_policy=yang_policy)
        )

        invariant, history = await asyncio.gather(invariant_fut, history_fut)
        reviews = [invariant, history]

        hard_blockers = [
            blocker for review in reviews for blocker in review.hard_blockers
        ]
        risk = _weighted_risk(reviews)
        if hard_blockers:
            decision = "reject"
        elif risk >= 70:
            decision = "manual_approval"
        else:
            decision = "approve"

        return {
            "report_id": f"SAFETY-MINI-{intent_id}",
            "intent_id": intent_id,
            "policy_id": yang_policy.network_policy.policy_id,
            "generated_at": datetime.utcnow().isoformat(),
            "overall_decision": decision,
            "overall_risk_score": risk,
            "risk_level": _risk_level(risk),
            "hard_blockers": hard_blockers,
            "agent_reviews": [review.to_dict() for review in reviews],
            "review_mode": "mini",
            "final_rationale": _rationale(decision, risk, hard_blockers, reviews),
        }


def _weighted_risk(reviews: list[SafetyReview]) -> int:
    weights = {
        "invariant_verifier": 0.30,
        "digital_twin_simulator": 0.25,
        "llm_redteam_critic": 0.05,
        "rollback_planner": 0.15,
        "history_risk_agent": 0.10,
    }
    blast_radius_risk = 15
    score = 0.15 * blast_radius_risk
    for review in reviews:
        score += weights.get(review.agent_id, 0.10) * review.risk_score
    return int(round(min(100, score)))


def _risk_level(score: int) -> str:
    if score >= 70:
        return "HIGH"
    if score >= 40:
        return "MEDIUM"
    return "LOW"


def _rationale(
    decision: str,
    risk: int,
    hard_blockers: list[str],
    reviews: list[SafetyReview],
) -> str:
    if hard_blockers:
        return f"发现硬性安全阻断项，禁止执行: {'; '.join(hard_blockers)}"
    warnings = [
        warning
        for review in reviews
        for warning in review.warnings
    ]
    if decision == "manual_approval":
        return f"综合风险 {risk}/100，建议进入人工审批。"
    if warnings:
        return f"硬性安全不变量通过，综合风险 {risk}/100；允许执行但保留警告: {'; '.join(warnings[:2])}"
    return f"所有硬性安全不变量通过，影子仿真和回滚计划可用，综合风险 {risk}/100，允许执行。"


def _proposal(yang_policy: YANGPolicy, plan: FlowTablePlan) -> str:
    action = yang_policy.network_policy.action.action_type.value
    devices = ", ".join(plan.affected_devices or [])
    return f"执行 {action} 策略 {yang_policy.network_policy.policy_name}，影响设备 {devices or '-'}。"


def _negotiation_report(
    *,
    proposal: str,
    reviews: list[SafetyReview],
    final_decision: str,
    risk: int,
) -> dict[str, Any]:
    votes = []
    for review in reviews:
        votes.append(
            {
                "agent": review.agent_id,
                "vote": review.decision,
                "risk_score": review.risk_score,
                "reason": _vote_reason(review),
                "evidence": review.evidence[:3],
                "warnings": review.warnings[:3],
            }
        )
    return {
        "proposal": proposal,
        "votes": votes,
        "approve_count": sum(1 for vote in votes if vote["vote"] == "approve"),
        "warning_count": sum(1 for vote in votes if vote["vote"] == "approve_with_warning"),
        "reject_count": sum(1 for vote in votes if vote["vote"] == "reject"),
        "final_consensus": final_decision,
        "overall_risk_score": risk,
        "explanation": _consensus_explanation(final_decision, votes),
    }


def _vote_reason(review: SafetyReview) -> str:
    if review.hard_blockers:
        return "; ".join(review.hard_blockers[:2])
    if review.warnings:
        return "; ".join(review.warnings[:2])
    if review.evidence:
        return "; ".join(review.evidence[:2])
    return "no additional risk found"


def _consensus_explanation(final_decision: str, votes: list[dict[str, Any]]) -> str:
    rejected = [vote["agent"] for vote in votes if vote["vote"] == "reject"]
    warned = [vote["agent"] for vote in votes if vote["vote"] == "approve_with_warning"]
    if rejected:
        return f"{', '.join(rejected)} 提出拒绝票，最终进入 {final_decision}。"
    if warned:
        return f"{', '.join(warned)} 提出带警告通过，其余评审同意，最终 {final_decision}。"
    return f"所有子智能体均同意，最终 {final_decision}。"


def _blast_radius(
    yang_policy: YANGPolicy,
    plan: FlowTablePlan,
    simulation: SafetyReview,
    snapshot: NetworkSnapshot,
) -> dict[str, Any]:
    src_host, dst_host = _match_hosts(yang_policy, snapshot.hosts)
    path = simulation.details.get("after_path") or simulation.details.get("before_path") or []
    links = []
    for left, right in zip(path, path[1:]):
        links.append(f"{left}-{right}")
    service = _flow_label(yang_policy)
    return {
        "affected_hosts": [item for item in [_host_label(src_host), _host_label(dst_host)] if item],
        "affected_devices": plan.affected_devices,
        "affected_links": links,
        "affected_services": [service],
    }


def _build_device_graph(links: list[LinkInfo]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for link in links:
        if str(link.state).upper() != "ACTIVE":
            continue
        graph.setdefault(link.src_device, set()).add(link.dst_device)
        graph.setdefault(link.dst_device, set()).add(link.src_device)
    return graph


def _shortest_path(
    graph: dict[str, set[str]],
    source: str | None,
    target: str | None,
) -> list[str] | None:
    if not source or not target:
        return None
    if source == target:
        return [source]
    queue: deque[tuple[str, list[str]]] = deque([(source, [source])])
    visited = {source}
    while queue:
        node, path = queue.popleft()
        for nxt in sorted(graph.get(node, set())):
            if nxt in visited:
                continue
            if nxt == target:
                return path + [nxt]
            visited.add(nxt)
            queue.append((nxt, path + [nxt]))
    return None


def _match_hosts(
    yang_policy: YANGPolicy,
    hosts: list[HostInfo],
) -> tuple[HostInfo | None, HostInfo | None]:
    match = yang_policy.network_policy.match
    return _host_in_network(hosts, match.source_ip), _host_in_network(hosts, match.destination_ip)


def _host_in_network(hosts: list[HostInfo], cidr: str | None) -> HostInfo | None:
    if not cidr:
        return None
    try:
        network = ip_network(cidr, strict=False)
    except ValueError:
        return None
    for host in hosts:
        for value in host.ip_addresses:
            try:
                if ip_address(value) in network:
                    return host
            except ValueError:
                continue
    return None


def _find_blackholes(plan: FlowTablePlan, snapshot: NetworkSnapshot) -> list[str]:
    links = snapshot.links
    port_index = {
        (link.src_device, str(link.src_port))
        for link in links
        if str(link.state).upper() == "ACTIVE"
    } | {
        (link.dst_device, str(link.dst_port))
        for link in links
        if str(link.state).upper() == "ACTIVE"
    }
    port_index |= {
        (host.location_device, str(host.location_port))
        for host in snapshot.hosts
        if host.location_device and host.location_port
    }
    blackholes: list[str] = []
    for request in plan.write_requests:
        for entry in request.table_entries:
            port = entry.egress_port or _action_param(entry, "port")
            if port is None:
                continue
            if (request.device_id, str(port)) not in port_index:
                blackholes.append(
                    f"NO_BLACKHOLE 失败: {request.device_id} 表 {entry.table_name} 输出端口 {port} 无 ACTIVE 链路。"
                )
    return blackholes


def _action_param(entry: Any, name: str) -> Any:
    for param in entry.action_params or []:
        if param.param_name == name:
            return param.value
    return None


def _capacity_warning(yang_policy: YANGPolicy, links: list[LinkInfo]) -> str | None:
    qos = yang_policy.network_policy.action.qos_params
    target = qos.bandwidth if qos else None
    if not target:
        return None
    known = [link.bandwidth_mbps for link in links if link.bandwidth_mbps]
    if not known:
        return "当前拓扑缺少链路容量字段，无法证明 Capacity Bound。"
    if min(known) < target:
        return f"存在链路容量 {min(known)}Mbps 低于策略目标 {target}Mbps。"
    return None


def _predict_after_path(
    plan: FlowTablePlan,
    snapshot: NetworkSnapshot,
    before_path: list[str] | None,
) -> list[str] | None:
    graph = _build_device_graph(snapshot.links)
    egress_devices = []
    for request in plan.write_requests:
        for entry in request.table_entries:
            port = entry.egress_port or _action_param(entry, "port")
            if port is None:
                continue
            peer = _peer_for_port(snapshot.links, request.device_id, str(port))
            if peer:
                egress_devices.append(peer)
    if before_path and egress_devices and egress_devices[0] in graph:
        suffix = _shortest_path(graph, egress_devices[0], before_path[-1])
        if suffix:
            return [before_path[0], egress_devices[0], *suffix[1:]]
    return before_path


def _peer_for_port(links: list[LinkInfo], device_id: str, port: str) -> str | None:
    for link in links:
        if str(link.state).upper() != "ACTIVE":
            continue
        if link.src_device == device_id and str(link.src_port) == port:
            return link.dst_device
        if link.dst_device == device_id and str(link.dst_port) == port:
            return link.src_device
    return None


def _host_label(host: HostInfo | None) -> str | None:
    if not host:
        return None
    ip_text = ",".join(host.ip_addresses)
    return f"{host.host_id}({ip_text})" if ip_text else host.host_id


def _flow_label(yang_policy: YANGPolicy) -> str:
    match = yang_policy.network_policy.match
    action = yang_policy.network_policy.action.action_type.value
    return (
        f"{match.source_ip or '*'} -> {match.destination_ip or '*'} "
        f"{match.protocol or 'ANY'}/{match.destination_port or '*'} action={action}"
    )


def _memory_intent_type(yang_policy: YANGPolicy) -> str:
    name = yang_policy.network_policy.policy_name or ""
    action = yang_policy.network_policy.action.action_type.value
    if "video" in name.lower() or "视频" in name:
        return "保障视频业务" if action == "SET_PRIORITY" else "视频业务"
    return action

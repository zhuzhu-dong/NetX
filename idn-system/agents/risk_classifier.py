"""
agents/risk_classifier.py
==========================

确定性风险分级器 —— v2.0 分级安全评审入口的决策引擎。

# 系统角色
在 P4 规则验证通过后、安全评审执行前，根据意图属性确定风险等级：
LOW / MEDIUM / HIGH / CRITICAL，决定后续安全评审的深度。

# 分级规则（纯确定性，无 LLM 参与）
- CRITICAL（强制人工审批）：跨子网重路由、>=4 设备、高优先级 DROP
- HIGH（完整 5-Agent SafetyCouncil）：跨子网限速、任意重路由、>=2 设备
- MEDIUM（MiniCouncil：不变量+历史）：SET_PRIORITY/RATE_LIMIT、单设备 DROP
- LOW（跳过 SafetyCouncil）：不匹配上述规则的默认等级

# 主要依赖
- models/yang_policy.py：ActionType 枚举
- models/flow_table.py：FlowTablePlan（用于统计设备数）
- models/network.py：NetworkSnapshot（用于判断跨子网）

# 设计意图
- 风险等级的区分节约了 SafetyCouncil 的评审成本
  （LOW 跳过 5 个 Agent 并行评审，MEDIUM 仅运行 2 个确定性子 Agent）
- 规则按顺序匹配（CRITICAL → HIGH → MEDIUM → LOW），首个命中即返回
- _is_cross_subnet 在不明确时默认返回 True（保守策略，宁可高估风险）
"""

from __future__ import annotations

import logging
from typing import Any

from models.flow_table import FlowTablePlan
from models.network import NetworkSnapshot
from models.yang_policy import ActionType, YANGPolicy

logger = logging.getLogger(__name__)


def classify_risk(
    *,
    yang_policy: YANGPolicy,
    flow_table_plan: FlowTablePlan | dict[str, Any],
    network_snapshot: NetworkSnapshot | None = None,
) -> str:
    """Return one of: ``LOW``, ``MEDIUM``, ``HIGH``, ``CRITICAL``.

    Rules are ordered — the first match wins.
    When the YANG model is partially constructed, defaults to MEDIUM
    (conservative but avoids the full 5-agent SafetyCouncil).
    """
    operation = (
        flow_table_plan.policy_operation
        if isinstance(flow_table_plan, FlowTablePlan)
        else str((flow_table_plan or {}).get("policy_operation") or "CREATE")
    )
    if operation in {"VERIFY_EXISTING", "MODIFY_QOS_RESOURCE"}:
        # Exact-scope reuse/update has already passed the action-conflict and
        # capacity gates.  It must not be converted into a manual-review path.
        return "LOW"

    # ── 安全访问：YANG 模型可能部分构建 ──
    np = getattr(yang_policy, "network_policy", None)
    if np is None:
        return "MEDIUM"

    action = getattr(getattr(np, "action", None), "action_type", ActionType.FORWARD)
    action_name = getattr(action, "value", str(action)).upper()
    priority = getattr(getattr(np, "metadata", None), "priority", 1000)
    devices = _affected_device_count(flow_table_plan)
    is_cross = _is_cross_subnet_safe(yang_policy, network_snapshot)

    # ── CRITICAL: mandatory human approval ──────────────────────────
    if action_name == "REROUTE" and is_cross:
        return "CRITICAL"
    if devices >= 4:
        return "CRITICAL"
    if action == ActionType.DROP and priority > 100:
        return "CRITICAL"

    # ── HIGH: full 5-agent SafetyCouncil ────────────────────────────
    if action == ActionType.RATE_LIMIT and is_cross:
        return "HIGH"
    if action_name == "REROUTE":
        return "HIGH"
    if devices >= 2:
        return "HIGH"

    # ── MEDIUM: MiniCouncil (invariant + history only) ──────────────
    if action in (ActionType.SET_PRIORITY, ActionType.RATE_LIMIT):
        return "MEDIUM"
    if action == ActionType.DROP and devices == 1:
        return "MEDIUM"

    # ── LOW: skip SafetyCouncil ─────────────────────────────────────
    return "LOW"


def _affected_device_count(plan: FlowTablePlan | dict[str, Any]) -> int:
    """Return the number of distinct devices touched by this plan."""
    if isinstance(plan, FlowTablePlan):
        return len(set(plan.affected_devices))
    if isinstance(plan, dict):
        return len(set(plan.get("affected_devices") or []))
    return 1


def _is_cross_subnet_safe(
    yang_policy: YANGPolicy,
    network_snapshot: NetworkSnapshot | None,
) -> bool:
    """安全版跨子网检测——模型不完整时保守返回 True（假设跨子网）。"""
    np = getattr(yang_policy, "network_policy", None)
    if np is None:
        return True
    match = getattr(np, "match", None)
    if match is None:
        return True
    src = getattr(match, "source_ip", None)
    dst = getattr(match, "destination_ip", None)
    if not src or not dst:
        return True
    if network_snapshot is None:
        return True
    src_subnet = _subnet_for_ip(src, network_snapshot)
    dst_subnet = _subnet_for_ip(dst, network_snapshot)
    if src_subnet is None or dst_subnet is None:
        return True
    return src_subnet != dst_subnet


def _is_cross_subnet(
    yang_policy: YANGPolicy,
    network_snapshot: NetworkSnapshot | None,
) -> bool:
    """Return True when source and destination IPs belong to different subnets."""
    match = yang_policy.network_policy.match
    src = match.source_ip
    dst = match.destination_ip
    if not src or not dst:
        # Cannot determine — assume cross-subnet to be conservative
        return True
    if network_snapshot is None:
        return True

    src_subnet = _subnet_for_ip(src, network_snapshot)
    dst_subnet = _subnet_for_ip(dst, network_snapshot)
    if src_subnet is None or dst_subnet is None:
        return True  # conservative: unknown = cross-subnet
    return src_subnet != dst_subnet


def _subnet_for_ip(ip_cidr: str, snapshot: NetworkSnapshot) -> str | None:
    """Heuristic: return the subnet of the host whose IP matches the CIDR."""
    from ipaddress import ip_address, ip_network

    try:
        addr = ip_address(ip_cidr.split("/")[0])
    except ValueError:
        return None

    for host in snapshot.hosts or []:
        for host_ip in host.ip_addresses or []:
            try:
                net = ip_network(host_ip, strict=False)
                if addr in net:
                    return str(net)
            except ValueError:
                continue
    return None

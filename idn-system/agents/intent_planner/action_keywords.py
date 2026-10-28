"""
agents/intent_planner/action_keywords.py
Action type and priority inference from keyword matching.

Extracted from ``IntentPlannerAgent`` for independent testability.
"""
from __future__ import annotations

import re
from typing import Optional

from models.yang_policy import ActionType, Protocol

ACTION_KEYWORDS = {
    ActionType.RATE_LIMIT: (
        "上限", "封顶", "最多",
        "限制", "限速", "不超过", "小于",
        "limit", "rate limit", "cap", "throttle",
    ),
    ActionType.DROP: ("阻断", "禁止", "拒绝", "丢弃", "block", "deny", "drop", "forbid"),
    ActionType.MIRROR: ("镜像", "复制", "mirror"),
    ActionType.FORWARD: ("转发", "重路由", "切换", "绕行", "forward", "reroute"),
    ActionType.SET_PRIORITY: (
        "保障", "保证", "优先", "最高优先级", "优化",
        "guarantee", "ensure", "prioritize", "priority", "optimize",
    ),
    ActionType.ACL_BLOCK: (
        "acl阻断", "acl拦截", "访问控制", "acl",
        "access control", "安全阻断",
    ),
}


def infer_action_type(lowered: str) -> tuple[ActionType, bool]:
    """Guess the desired action from keywords in *lowered* (lowercase) text."""
    cleaned = re.sub(
        r"(?:不要|无需|不需要|不是|并非)\s*(?:阻断|禁止|拒绝|丢弃|限速|限制|保障|保证|block|deny|drop|limit)",
        "",
        lowered,
    )
    for action_type, keywords in ACTION_KEYWORDS.items():
        if any(keyword in cleaned for keyword in keywords):
            return action_type, True
    return ActionType.FORWARD, False


def infer_priority(text: str, action_type: ActionType) -> int:
    """Return an appropriate P4Runtime priority for *action_type*."""
    lowered = text.lower()
    if any(value in lowered for value in ("critical", "最高", "核心", "紧急")):
        return 50
    if action_type == ActionType.DROP:
        return 50
    if action_type == ActionType.SET_PRIORITY:
        return 100
    return 1000


def infer_traffic(
    text: str, lowered: str, explicit_port: Optional[int] = None,
) -> tuple[Protocol, Optional[int], Optional[int], Optional[str]]:
    """Infer protocol, port, DSCP, and rationale from keyword heuristics."""
    from api.service_profiles import application_default

    dscp_value = extract_explicit_dscp(text)

    # Business-service defaults are resolved centrally by
    # ServiceSemanticResolver after endpoint applicability is known.
    for application in ("ssh", "https", "http"):
        if application in lowered:
            default = application_default(application) or {}
            return (
                Protocol(str(default.get("protocol") or "TCP")),
                explicit_port or default.get("destination_port"),
                dscp_value,
                f"应用参数来自 ServiceProfile registry application_defaults.{application}",
            )
    if "tcp" in lowered:
        return Protocol.TCP, explicit_port, dscp_value, None
    if "udp" in lowered:
        return Protocol.UDP, explicit_port, dscp_value, None
    return Protocol.ANY, explicit_port, dscp_value, None


def extract_explicit_dscp(text: str) -> Optional[int]:
    """Return a DSCP only when the user actually expressed a traffic class.

    This deliberately does not infer a DSCP from a service name.  The latter
    belongs to ServiceProfile attachment, while DSCP/EF/AF/CS wording is a
    user-owned selector constraint.
    """
    value = str(text or "")
    numeric = re.search(
        r"(?:\bdscp(?=\s|:|=|\d)|traffic[ _-]*class|优先级码点)\s*[:=]?\s*(\d{1,2})\b",
        value,
        re.IGNORECASE,
    )
    if numeric:
        parsed = int(numeric.group(1))
        return parsed if 0 <= parsed <= 63 else None
    if re.search(r"\bEF\b", value, re.IGNORECASE):
        return 46
    af = re.search(r"\bAF([1-4])([1-3])\b", value, re.IGNORECASE)
    if af:
        return 8 * int(af.group(1)) + 2 * int(af.group(2))
    cs = re.search(r"\bCS([0-7])\b", value, re.IGNORECASE)
    if cs:
        return 8 * int(cs.group(1))
    return None


def has_explicit_dscp(text: str) -> bool:
    """Whether a DSCP expression, including a valid numeric zero, was present."""
    return extract_explicit_dscp(text) is not None


def has_named_application(lowered: str) -> bool:
    """True when *lowered* mentions a known application name."""
    from api.service_profiles import service_registry

    application_names = service_registry()["application_defaults"].keys()
    semantic_values = []
    for profile in service_registry()["profiles"]:
        semantic_values.extend(
            [profile.display_name, profile.service_name, *profile.aliases, *profile.effective_semantic_tags]
        )
    return any(
        str(value).casefold() in lowered.casefold()
        for value in [*application_names, *semantic_values]
        if str(value).strip()
    )


def mentions_video_service(lowered: str) -> bool:
    """Compatibility alias: true when registry semantics mention video."""
    return "视频" in lowered or "video" in lowered

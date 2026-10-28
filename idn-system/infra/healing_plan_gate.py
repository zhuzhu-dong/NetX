"""Desired-state gate shared by the legacy A2A healing execution chain.

The transactional control plane already compares IntentPlanIR generations.  The
worker's LangGraph path uses serialized FlowTablePlan dictionaries instead, so
it needs the same identity-independent comparison before it can write ONOS/P4
or TC again.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


_NON_EFFECT_KEYS = {
    "plan_id", "intent_id", "created_at", "updated_at", "timestamp",
    "context_id", "evidence_refs", "trace_id", "correlation_id",
}


def _canonical(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _NON_EFFECT_KEYS
        }
    if isinstance(value, (list, tuple, set)):
        items = [_canonical(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return value


def desired_effect_signature(flow_plan: dict[str, Any], yang_draft: Any | None) -> str:
    """Hash only concrete forwarding/QoS semantics, never generation metadata."""

    action = {}
    if yang_draft is not None:
        network_policy = getattr(yang_draft, "network_policy", None)
        action_model = getattr(network_policy, "action", None)
        action = _canonical(action_model) if action_model is not None else {}
    payload = {
        "policy_operation": flow_plan.get("policy_operation"),
        "traffic_scope_key": flow_plan.get("traffic_scope_key"),
        "policy_change": _canonical(flow_plan.get("policy_change") or {}),
        "action_profile_requests": _canonical(flow_plan.get("action_profile_requests") or []),
        "write_requests": _canonical(flow_plan.get("write_requests") or []),
        "yang_action": action,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_repeated_noop(
    *,
    flow_plan: dict[str, Any],
    yang_draft: Any | None,
    previous_execution: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Return whether a healing retry has exactly the prior desired effect."""

    signature = desired_effect_signature(flow_plan, yang_draft)
    previous = dict(previous_execution or {})
    same = bool(
        previous.get("desired_effect_signature")
        and previous.get("desired_effect_signature") == signature
        and previous.get("data_plane_changed") is True
    )
    return same, signature

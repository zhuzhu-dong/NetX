"""Recovery of lost desired ownership from durable Intent artifacts only."""
from __future__ import annotations

from typing import Any

from api.store import IntentStore, get_store
from models.yang_policy import YANGPolicy
from persistence.policy_store import PolicyStore


def _artifact_candidates(record: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    result = dict(record.get("result") or {})
    plan = dict(result.get("intent_plan") or {})
    policies = list(plan.get("yang_policies") or [])
    policy_results = list(result.get("policy_results") or [])
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index, item in enumerate(policies):
        candidates.append((dict(item or {}), dict(
            (policy_results[index] if index < len(policy_results) else {}) or {}
        )))
    for item in policy_results:
        payload = dict(item or {})
        policy = dict(payload.get("yang_policy") or {})
        if policy:
            candidates.append((policy, payload))
    return candidates


def recover_managed_desired_from_intent_artifact(
    *, intent_id: str, policy_id: str, store: IntentStore | None = None,
) -> dict[str, Any]:
    """Formally recover one lost policy owner without inferring desired state.

    The immutable input is the original persisted Intent result: YANG determines
    the selector/action and the recorded execution result supplies only lineage
    required by the later reconciler.  Observed ONOS/P4/TC state is never used
    to construct desired policy semantics.
    """
    state_store = store or get_store()
    record = state_store.get(intent_id)
    if not record:
        raise RuntimeError("DURABLE_DESIRED_RECOVERY_INTENT_NOT_FOUND")
    for raw_policy, policy_result in _artifact_candidates(record):
        network = dict(raw_policy.get("network_policy") or raw_policy)
        if str(network.get("policy_id") or "") != str(policy_id):
            continue
        policy = YANGPolicy(**raw_policy)
        committed = PolicyStore(state_store).recover_managed_desired_from_artifact(
            policy,
            intent_id=intent_id,
            execution_runtime=dict(policy_result.get("execution_result") or {}),
        )
        state_store.add_audit_event(
            event_type="DURABLE_DESIRED_OWNERSHIP_RECOVERED",
            resource_type="policy",
            resource_id=policy_id,
            correlation={"intent_id": intent_id, "traffic_scope_key": committed["traffic_scope_key"]},
            expected={"desired_source": "DURABLE_INTENT_ARTIFACT", "policy_id": policy_id},
            observed={"intent_artifact_present": True, "observed_dataplane_used_for_desired": False},
            action={"operation": "STATE_RECOVERY_COMMIT_MANAGED_DESIRED", "write_executed": True},
            result={"status": "ACTIVE_MANAGED", "version": committed.get("version")},
        )
        return committed
    raise RuntimeError("DURABLE_DESIRED_RECOVERY_POLICY_ARTIFACT_NOT_FOUND")

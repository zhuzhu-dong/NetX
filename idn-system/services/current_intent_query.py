"""Read-only, ownership-based projection for the dashboard current-intent panel.

The dashboard is deliberately not a list of every non-terminal runtime row.  A
row is current only while it owns live desired state/resources, or has a live
worker workflow.  This keeps historical audit records visible through the
normal history API without misrepresenting them as deployed services.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from typing import Any

from api.store import logical_waiting_key
from api.intent_lifecycle_projection import project_intent_lifecycle
from models.intent_domain import IntentLifecycle
from models.policy_update import TrafficScopeKey
from persistence.intent_domain_store import IntentDomainStore, get_intent_domain_store

PROCESSING = {"RECEIVED", "UNDERSTANDING", "PARSED", "SENSING", "PLANNING", "PLAN_READY", "CHECKING", "NEEDS_APPROVAL", "DEPLOYING", "VERIFYING", "REPLANNING", "REDEPLOYING", "DIAGNOSING"}
ATTENTION_LIFECYCLES = {"DEGRADED", "VIOLATED", "ASSURANCE_FAILED", "EXECUTION_FAILED", "FAILED", "WITHDRAW_REQUESTED", "WITHDRAW_PENDING_CLEANUP", "WITHDRAW_FAILED", "WAITING_FOR_INFRASTRUCTURE", "CONFIRMING_VIOLATION", "COLLECTING_DIAGNOSTICS", "VIOLATION_CONFIRMED_ROOT_CAUSE_PENDING", "DEGRADED_ROOT_CAUSE_UNCONFIRMED", "DEGRADED_AWAITING_DIAGNOSTICS", "HEALING", "RECONCILING", "DRIFTED"}
TERMINAL_CLEAN = {"WITHDRAWN", "CANCELLED", "REJECTED", "EXPIRED", "UNSATISFIABLE"}
ACTIVE_JOB_STATES = {"queued", "running", "claimed", "processing", "pending"}
CONVERSATIONAL_WAITING_LIFECYCLES = {
    "NEEDS_CLARIFICATION", "WAITING_FOR_USER", "PENDING_CLARIFICATION",
}
# A terminal result is not necessarily historical.  These states have no
# desired policy/binding, but their latest logical owner still needs an
# operator decision (retry, withdraw, or replace).  They must consequently
# remain in Current Network Intents until a formal lifecycle action resolves
# them.  Do not add WITHDRAWN/CANCELLED/SUPERSEDED here.
CURRENT_ACTIONABLE_FAILURE_LIFECYCLES = {
    "UNSATISFIABLE", "REJECTED", "EXECUTION_FAILED", "FAILED",
    "ROLLBACK_FAILED", "RECONCILIATION_FAILED", "BLOCKED", "DEGRADED",
    "VIOLATED", "ASSURANCE_FAILED",
}
RESOLVED_OR_DISMISSED_LIFECYCLES = {
    "WITHDRAWN", "CANCELLED", "EXPIRED", "SUPERSEDED", "DISMISSED",
}
DISPLAY_BUCKETS = {
    "ABNORMAL_WAITING": {"group": "attention", "tone": "warning"},
    "EXECUTING": {"group": "processing", "tone": "progress"},
    "ACTIVE": {"group": "enforcing", "tone": "success"},
}


def _as_dict(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value or {})


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _age(value: Any) -> float | None:
    parsed = _time(value)
    return max(0, (datetime.now(timezone.utc) - parsed).total_seconds()) if parsed else None


def _endpoint(value: dict[str, Any]) -> str | None:
    """Prefer resolved topology labels; selector/IP is an intentional fallback."""
    return value.get("host") or value.get("name") or value.get("label") or value.get("id") or value.get("ip") or value.get("selector_prefix")


def _objective(value: Any) -> dict[str, Any]:
    item = _as_dict(value)
    return {"type": item.get("objective_type") or item.get("type") or "UNKNOWN", "requested": item.get("value", item.get("requested")), "effective_threshold": item.get("effective_threshold") or item.get("threshold"), "unit": item.get("unit")}


def _service_id(service: dict[str, Any]) -> str:
    """Read both the canonical single service field and LLM service lists."""
    values = service.get("service_ids") or []
    return str(service.get("service_id") or service.get("service_type") or (values[0] if values else ""))


class CurrentIntentCurrentnessResolver:
    """Pure, deterministic policy/workflow currentness resolver.

    It never calls ONOS, P4Runtime, TC, assurance, or an LLM.  The caller gives
    it persisted snapshots only.  Scope replacement uses the formal traffic
    selector plus service identity, never display text or an IP-only heuristic.
    """
    workflow_freshness_seconds = 300

    def scope_identity(self, spec: dict[str, Any], aggregate: dict[str, Any]) -> tuple[str, str]:
        result = aggregate.get("result") or {}
        execution = result.get("execution_result") or {}
        binding = (execution.get("qos_enforcement") or {}).get("qos_binding") or {}
        scope = binding.get("traffic_scope_key") or execution.get("traffic_scope_key")
        service = _service_id(spec.get("service") or {})
        if not scope:
            selector = spec.get("selector") or {}
            scope = TrafficScopeKey.from_match(selector, action_family="QOS", service_id=service or None).key
        # The project TrafficScopeKey deliberately makes service audit metadata
        # non-hashed.  Add it to the replacement identity so distinct profiles
        # sharing five tuples are not incorrectly merged in this view.
        return str(scope), f"{scope}|service={service}"

    def workflow_activity(self, lifecycle: str, runtime_updated_at: Any, jobs: list[dict[str, Any]], has_owner: bool) -> tuple[str, str, str | None]:
        if lifecycle not in PROCESSING:
            return "COMPLETED", "workflow is terminal or committed", None
        active = next((job for job in jobs if str(job.get("status", "")).lower() in ACTIVE_JOB_STATES), None)
        heartbeat = active.get("heartbeat_at") if active else None
        fresh = _age(heartbeat or (active or {}).get("started_at") or runtime_updated_at)
        if active and (fresh is None or fresh <= self.workflow_freshness_seconds):
            return "ACTIVE", "active persisted worker task has a valid heartbeat", heartbeat
        if has_owner:
            return "STALE_WORKFLOW", "workflow heartbeat/task is stale but committed network ownership remains", heartbeat
        return "HISTORICAL", "nonterminal projection has no live task, heartbeat, or network ownership", heartbeat

    def currentness(self, *, lifecycle: str, has_owner: bool, activity: str, superseded: bool, withdrawn: bool) -> tuple[bool, str]:
        if superseded:
            return False, "superseded by the current owner of the formal traffic scope"
        if withdrawn or lifecycle in RESOLVED_OR_DISMISSED_LIFECYCLES:
            return False, "terminal/withdrawn record has no maintained network resources"
        if has_owner:
            return True, "owns committed policy, desired configuration, or resource binding"
        if lifecycle in CONVERSATIONAL_WAITING_LIFECYCLES:
            return True, "active conversational intent is waiting for required user input"
        if lifecycle in CURRENT_ACTIONABLE_FAILURE_LIFECYCLES:
            return True, "latest logical intent ended without a policy and still needs an operator decision"
        if activity == "ACTIVE":
            return True, "active workflow has a valid persisted task/heartbeat"
        return False, "historical or zombie workflow has no current ownership"


class CurrentIntentQueryService:
    """Build the current-intent DTO from one persisted batch snapshot."""
    freshness_seconds = 300

    def __init__(self, domain: IntentDomainStore | None = None):
        self.domain = domain or get_intent_domain_store()
        self.currentness = CurrentIntentCurrentnessResolver()

    def list_current(self, *, group: str | None = None, limit: int = 50, include_evidence: bool = False, include_projection_details: bool = False) -> dict[str, Any]:
        records = self._records(min(max(limit, 1), 200))
        authoritative = self._managed_effective_owners()
        authoritative_available = self._supports_effective_authority()
        record_by_intent = {
            str(record["runtime"].intent_id): record
            for record in records
            if record.get("runtime") is not None
        }
        # A later formal outcome (including a formal withdrawal) is an
        # ownership tombstone for earlier no-owner failures of the same
        # resolved scope.  Without this, withdrawing the latest failed
        # attempt would make an older, already-replaced failure reappear in
        # Current Network Intents.
        latest_failure_attempt_by_scope: dict[str, dict[str, Any]] = {}
        for record in records:
            candidate = self._candidate(record)
            if not candidate or candidate["spec"].get("semantic_scope_complete") is False:
                continue
            if candidate["lifecycle"] not in (CURRENT_ACTIONABLE_FAILURE_LIFECYCLES | RESOLVED_OR_DISMISSED_LIFECYCLES):
                continue
            logical_scope = candidate.get("scope_identity")
            if not logical_scope:
                continue
            prior = latest_failure_attempt_by_scope.get(logical_scope)
            if prior is None or str(candidate["owner_sort"]) > str(prior["owner_sort"]):
                latest_failure_attempt_by_scope[logical_scope] = candidate

        # A card is an effective network scope, not an Intent lifecycle row.
        # Build it from PolicyStore's committed desired owner and its unique
        # active ResourceBinding, then use the source Intent only for audit and
        # runtime presentation fields.  In particular, a compound parent may
        # own multiple policies and must never become an extra card itself.
        selected: list[dict[str, Any]] = []
        owner_intent_ids: set[str] = set()
        for owner in authoritative.values():
            source_intent_id = str(owner["policy"]["intent_id"])
            owner_intent_ids.add(source_intent_id)
            record = record_by_intent.get(source_intent_id) or self._record_for_owner(owner)
            candidate = self._candidate(record, authority=owner)
            # A resource binding cannot resurrect a terminal historical
            # runtime.  Ownership reconciliation repairs the durable source;
            # this read-side guard ensures a malformed legacy row remains
            # history-only while that formal repair is pending.
            if candidate and candidate["lifecycle"] not in RESOLVED_OR_DISMISSED_LIFECYCLES:
                candidate["is_current"] = True
                candidate["currentness_reason"] = "owns the committed policy revision and unique active resource binding"
                candidate["superseded"] = False
                candidate["superseded_by"] = None
                selected.append(candidate)

        # A fresh workflow with no effective owner can still be shown while it
        # is genuinely executing.  It must not duplicate an already-managed
        # scope, and terminal/audit-only rows never enter this panel.
        for record in records:
            intent_id = str(record["runtime"].intent_id)
            if intent_id in owner_intent_ids:
                continue
            candidate = self._candidate(record)
            if not candidate or (authoritative_available and candidate["scope_key"] in authoritative):
                continue
            if candidate["lifecycle"] in CURRENT_ACTIONABLE_FAILURE_LIFECYCLES:
                latest = latest_failure_attempt_by_scope.get(candidate.get("scope_identity"))
                if latest and str(latest["intent_id"]) != str(candidate["intent_id"]):
                    continue
            current, reason = self.currentness.currentness(
                lifecycle=candidate["lifecycle"],
                has_owner=False if authoritative_available else candidate["has_owner"],
                activity=candidate["workflow_activity"], superseded=False,
                withdrawn=candidate["lifecycle"] == "WITHDRAWN",
            )
            # A no-owner failure only becomes a current actionable card once
            # its formal traffic scope was actually resolved.  This prevents
            # legacy/incomplete audit rows from competing with a resident
            # policy merely because they share generic prose or an empty
            # selector.
            if (
                candidate["lifecycle"] in CURRENT_ACTIONABLE_FAILURE_LIFECYCLES
                and authoritative_available
                and not candidate["current_actionable_failure"]
            ):
                current = False
            if current:
                candidate["is_current"], candidate["currentness_reason"] = current, reason
                candidate["superseded"] = False
                candidate["superseded_by"] = None
                selected.append(candidate)

        # A session is conversation provenance, not the logical owner.  Old
        # data can contain equivalent waiting rows from several sessions, so
        # the read model also defends the current panel with the same global
        # logical key used by creation/reconciliation.
        waiting_by_logical_key: dict[str, dict[str, Any]] = {}
        actionable_failure_by_scope: dict[str, dict[str, Any]] = {}
        non_waiting: list[dict[str, Any]] = []
        for candidate in selected:
            if candidate["lifecycle"] in CONVERSATIONAL_WAITING_LIFECYCLES and not candidate["has_owner"]:
                logical_key = candidate.get("logical_waiting_key") or candidate["intent_id"]
                prior = waiting_by_logical_key.get(logical_key)
                if prior is None or str(candidate["owner_sort"]) > str(prior["owner_sort"]):
                    waiting_by_logical_key[logical_key] = candidate
            elif candidate["lifecycle"] in CURRENT_ACTIONABLE_FAILURE_LIFECYCLES and not candidate["has_owner"]:
                # A failed attempt without resources is a current logical
                # owner, not an extra effective policy card.  Keep only the
                # newest unresolved attempt for its fully-resolved scope.
                logical_key = candidate.get("scope_identity") or f"failure:{candidate['intent_id']}"
                prior = actionable_failure_by_scope.get(logical_key)
                if prior is None or str(candidate["owner_sort"]) > str(prior["owner_sort"]):
                    actionable_failure_by_scope[logical_key] = candidate
            else:
                non_waiting.append(candidate)
        selected = [*non_waiting, *waiting_by_logical_key.values(), *actionable_failure_by_scope.values()]
        items = [
            self._dto(item, include_evidence, include_projection_details)
            for item in sorted(selected, key=lambda item: (str(item["scope_key"] or ""), item["owner_sort"]))
        ]
        groups = {"processing": [], "enforcing": [], "attention": []}
        for item in items:
            groups[item["display_group"]].append(item)
        for values in groups.values(): values.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        if group in groups: groups = {group: groups[group]}
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "environment_epoch": None, "summary": {"total": sum(map(len, groups.values())), **{key: len(value) for key, value in groups.items()}}, "groups": groups, "items": [item for values in groups.values() for item in values], "read_only": True}

    def _supports_effective_authority(self) -> bool:
        store = getattr(self.domain, "base_store", None)
        return bool(store and hasattr(store, "_lock") and hasattr(store, "_connect"))

    def _managed_effective_owners(self) -> dict[str, dict[str, Any]]:
        """Return the one committed policy + active binding owner per scope.

        This is intentionally the same authority used by resident
        reconciliation.  Historical runtimes may retain a stable policy ID
        after an in-place update, so they are never an ownership authority.
        """
        try:
            from infra.qos_resource_allocator import QoSResourceAllocator
            from persistence.policy_store import PolicyStore

            store = self.domain.base_store
            policies = PolicyStore(store, read_only=True).resolve_managed_desired_policies()
            allocator = QoSResourceAllocator(store)
        except (AttributeError, TypeError):
            # Lightweight test doubles that do not expose the control-plane
            # store retain the isolated runtime-projection behaviour.
            return {}

        owners: dict[str, dict[str, Any]] = {}
        for scope, policy in policies.items():
            try:
                binding = allocator.get_active_for_scope(scope)
            except RuntimeError:
                # Ambiguous bindings are not an effective owner; displaying
                # either one as current would hide a control-plane integrity
                # fault.
                continue
            if binding is None or str(binding.policy_id) != str(policy.get("policy_id")):
                continue
            try:
                payload = json.loads(policy["yang_json"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            network_policy = payload.get("network_policy") or payload
            owners[str(scope)] = {
                "scope_key": str(scope),
                "policy": policy,
                "binding": binding,
                "network_policy": network_policy,
                "yang": payload,
            }
        return owners

    def _record_for_owner(self, owner: dict[str, Any]) -> dict[str, Any]:
        """Construct a minimal audit record when its runtime is outside page size."""
        policy = owner["policy"]
        intent_id = str(policy["intent_id"])
        runtime = self.domain.get_runtime(intent_id) if hasattr(self.domain, "get_runtime") else None
        updated_at = policy.get("updated_at")
        if runtime is None:
            runtime = SimpleNamespace(
                intent_id=intent_id, lifecycle=IntentLifecycle.SATISFIED,
                current_revision=1, active_policy_ids=[policy["policy_id"]],
                active_plan_id=None, current_path=[], observed_metrics={},
                created_at=updated_at, updated_at=updated_at, failure_reason=None,
            )
        spec = self.domain.get_spec(intent_id, getattr(runtime, "current_revision", None)) if hasattr(self.domain, "get_spec") else None
        aggregate = self.domain.base_store.get(intent_id) if hasattr(self.domain.base_store, "get") else {}
        return {
            "runtime": runtime, "spec": spec, "aggregate": aggregate or {},
            "execution": {}, "assurance": {}, "evidence": {}, "jobs": [],
            "revisions": [], "policies": [],
        }

    def _records(self, limit: int) -> list[dict[str, Any]]:
        batch = getattr(self.domain, "current_intent_records", None)
        if batch:
            return batch(limit=limit)
        # Compatibility for isolated unit-test fakes. Production uses the
        # batch method above, so the GET handler does not perform N+1 reads.
        records = []
        for runtime in self.domain.current_runtimes(limit=limit):
            records.append({"runtime": runtime, "spec": self.domain.get_spec(runtime.intent_id, runtime.current_revision) or self.domain.get_spec(runtime.intent_id), "aggregate": self.domain.base_store.get(runtime.intent_id) or {}, "execution": (self.domain.executions(runtime.intent_id) or [{}])[0], "assurance": (self.domain.assurance(runtime.intent_id) or [{}])[0], "evidence": (self.domain.evidence(runtime.intent_id) or [{}])[0], "jobs": [], "revisions": [], "policies": []})
        return records

    def _candidate(self, record: dict[str, Any], *, authority: dict[str, Any] | None = None) -> dict[str, Any] | None:
        runtime = record["runtime"]; spec_obj = record.get("spec")
        if not spec_obj and authority is None: return None
        spec = self._canonical_spec(authority) if authority else _as_dict(spec_obj)
        aggregate = record.get("aggregate") or {}; result = aggregate.get("result") or {}
        lifecycle = runtime.lifecycle.value if isinstance(runtime.lifecycle, IntentLifecycle) else str(runtime.lifecycle)
        execution = record.get("execution") or {}; assurance = record.get("assurance") or {}; evidence = record.get("evidence") or {}
        legacy_execution = result.get("execution_result") or {}; verification = result.get("status_verification_report") or {}
        aggregate_satisfied = bool(legacy_execution.get("config_applied")) and (verification.get("passed") is True or (aggregate.get("snapshot") or {}).get("verification_passed") is True or str(aggregate.get("status", "")).lower() in {"success", "satisfied"})
        active_policy_ids = list(getattr(runtime, "active_policy_ids", []) or [])
        binding = (legacy_execution.get("qos_enforcement") or {}).get("qos_binding") or {}
        resource_active = binding.get("active") is not False and bool(binding or legacy_execution.get("resource_binding"))
        desired = bool(getattr(runtime, "desired_policy", {}) or {})
        has_owner = (
            True if authority is not None else
            bool(active_policy_ids or (aggregate_satisfied and resource_active) or desired)
        )
        conversational_waiting = lifecycle in CONVERSATIONAL_WAITING_LIFECYCLES and authority is None and not has_owner
        selector = spec.get("selector") or {}
        formal_scope_resolved = bool(
            binding.get("traffic_scope_key")
            or execution.get("traffic_scope_key")
            or (selector.get("source_ip") and selector.get("destination_ip"))
        )
        current_actionable_failure = (
            lifecycle in CURRENT_ACTIONABLE_FAILURE_LIFECYCLES
            and authority is None
            and not has_owner
            and formal_scope_resolved
            # A modern incomplete scope is retained as a conversational
            # waiting card.  Old records which incorrectly reached FAILED
            # before resolving their semantic scope are historical audit only
            # and must not inflate the current exception queue.
            and spec.get("semantic_scope_complete") is not False
        )
        if conversational_waiting:
            # No selector is authoritative while semantic clarification is
            # open.  Never synthesize TrafficScopeKey for display because that
            # key is also the identity used by binding/update resolution.
            scope_key, scope_identity = None, f"conversation:{runtime.intent_id}"
        else:
            scope_key, scope_identity = self.currentness.scope_identity(spec, aggregate)
        if authority:
            scope_key = authority["scope_key"]
            service = (spec.get("service") or {}).get("service_id") or ""
            scope_identity = f"{scope_key}|service={service}"
        workflow_activity, activity_reason, heartbeat = self.currentness.workflow_activity(lifecycle, getattr(runtime, "updated_at", None), record.get("jobs") or [], has_owner)
        evidence_state, freshness = self._evidence_state(evidence, assurance, has_owner)
        assurance_state = self._assurance_state(lifecycle, assurance, aggregate_satisfied)
        enforcement = self._enforcement_state(lifecycle, execution, has_owner, assurance_state)
        recovery = {}
        try:
            # Independent, durable recovery projection.  It deliberately does
            # not change the historical assurance decision during a restart.
            from infra.production_restart_recovery import RestartRecoveryRepository
            recovery = RestartRecoveryRepository(self.domain.base_store).state_for(runtime.intent_id) or {}
            if recovery:
                assurance_state = recovery["assurance_state"]
                enforcement = recovery["enforcement_state"]
        except Exception:
            recovery = {}
        active_revision = self._active_revision(runtime, record.get("revisions") or [])
        policy_id = authority["policy"]["policy_id"] if authority else (active_policy_ids[0] if active_policy_ids else binding.get("policy_id") or legacy_execution.get("policy_id"))
        owner_sort = (
            authority["policy"].get("updated_at") if authority else
            (_iso(getattr(runtime, "updated_at", None)) or aggregate.get("updated_at") or "")
        )
        pending_operation = bool(authority and any(
            authority["policy"].get(field)
            for field in ("pending_yang_json", "pending_intent_id", "pending_operation")
        ))
        user_id = str(aggregate.get("user_id") or "anonymous")
        original_text = str(aggregate.get("text") or spec.get("original_text") or "")
        snapshot = aggregate.get("snapshot") or {}
        details = snapshot.get("clarification_details") or []
        unresolved_slots = [item.get("field") for item in details if isinstance(item, dict)] or snapshot.get("unresolved_fields") or []
        waiting_key = None
        if conversational_waiting and original_text:
            waiting_key = logical_waiting_key(
                user_id=user_id, text=original_text,
                unresolved_slots=unresolved_slots,
                context=aggregate.get("context") or {},
            )
        return {"intent_id": runtime.intent_id, "source_intent_id": authority["policy"]["intent_id"] if authority else runtime.intent_id, "revision": runtime.current_revision, "active_revision": active_revision, "runtime": runtime, "spec": spec, "aggregate": aggregate, "execution": execution, "assurance": assurance, "evidence": evidence, "lifecycle": lifecycle, "has_owner": has_owner, "policy_id": policy_id, "scope_key": scope_key, "scope_identity": scope_identity, "session_id": record.get("session_id") or getattr(runtime, "session_id", None), "logical_waiting_key": waiting_key, "current_actionable_failure": current_actionable_failure, "workflow_activity": workflow_activity, "workflow_activity_reason": activity_reason, "worker_heartbeat_at": heartbeat, "evidence_state": evidence_state, "evidence_freshness": freshness, "assurance_state": assurance_state, "enforcement_state": enforcement, "pending_operation": pending_operation, "recovery": recovery, "owner_sort": owner_sort, "projection_state": lifecycle, "projection_consistent": not (assurance_state == "SATISFIED" and lifecycle == "NEEDS_APPROVAL"), "authority": authority}

    @staticmethod
    def _canonical_spec(authority: dict[str, Any]) -> dict[str, Any]:
        """Make card content from committed YANG, never an Intent's prose/spec."""
        policy = authority["network_policy"]
        match = dict(policy.get("match") or {})
        qos = dict((policy.get("action") or {}).get("qos_params") or {})
        mode = str(qos.get("qos_mode") or "")
        objective = "MAX_BANDWIDTH" if mode == "RATE_LIMIT" else "MIN_BANDWIDTH"
        service_id = authority["binding"].service_id or authority["yang"].get("service_id")
        return {
            "original_text": authority["yang"].get("raw_intent") or "",
            "source": {"selector_prefix": match.get("source_ip")},
            "destination": {"selector_prefix": match.get("destination_ip")},
            "service": {"service_id": service_id},
            "selector": {**match, "dscp": qos.get("dscp_value")},
            "objectives": [{"type": objective, "value": qos.get("bandwidth"), "unit": "Mbps"}],
        }

    @staticmethod
    def _active_revision(runtime: Any, revisions: list[dict[str, Any]]) -> int:
        active = next((int(row.get("revision")) for row in revisions if str(row.get("status")) == "ACTIVE"), None)
        return active or int(getattr(runtime, "current_revision", 1))

    def _evidence_state(self, evidence: dict[str, Any], assurance: dict[str, Any], committed: bool) -> tuple[str, dict[str, Any]]:
        observed = evidence.get("sampled_at") or evidence.get("observed_at") or assurance.get("observed_at")
        age = _age(observed); state = "STALE" if age is not None and age > self.freshness_seconds else ("FRESH" if observed else "UNKNOWN")
        return ("STALE" if state == "STALE" else ("VERIFIED" if committed and state == "FRESH" else "UNKNOWN"), {"observed_at": observed, "age_seconds": round(age, 3) if age is not None else None, "freshness": state})

    @staticmethod
    def _assurance_state(lifecycle: str, assurance: dict[str, Any], aggregate_satisfied: bool) -> str:
        if assurance.get("satisfied") is True or aggregate_satisfied: return "SATISFIED"
        if lifecycle in {"VIOLATED", "ASSURANCE_FAILED"}: return "VIOLATED"
        if lifecycle == "UNSATISFIABLE": return "UNSATISFIABLE"
        if lifecycle in {"VERIFYING", "CONFIRMING_VIOLATION"}: return "CONFIRMING"
        if lifecycle in {"DIAGNOSING", "COLLECTING_DIAGNOSTICS"}: return "ROOT_CAUSE_PENDING"
        return "NOT_EVALUATED"

    @staticmethod
    def _enforcement_state(lifecycle: str, execution: dict[str, Any], has_owner: bool, assurance: str) -> str:
        if lifecycle == "WITHDRAWN": return "WITHDRAWN"
        if lifecycle in {"WITHDRAW_REQUESTED", "WITHDRAW_PENDING_CLEANUP"}: return "WITHDRAWING"
        if lifecycle == "WAITING_FOR_INFRASTRUCTURE": return "WAITING_FOR_INFRASTRUCTURE"
        if lifecycle == "RECONCILING": return "RECONCILING"
        if lifecycle in {"DEGRADED", "DRIFTED", "VIOLATED", "ASSURANCE_FAILED"} or assurance == "VIOLATED": return "DEGRADED"
        if str(execution.get("status", "")).upper() in {"PREPARING", "INSTALLING", "READING_BACK", "VERIFYING_CONFIG", "COMMITTING"}: return "INSTALLING"
        # Stale evidence is a freshness statement, not a data-plane failure.
        return "ACTIVE" if has_owner else "NOT_INSTALLED"

    def _dto(self, item: dict[str, Any], include_evidence: bool, include_projection_details: bool) -> dict[str, Any]:
        spec = item["spec"]; runtime = item["runtime"]; objectives = [_objective(value) for value in spec.get("objectives") or []]
        source, destination = _endpoint(spec.get("source") or {}), _endpoint(spec.get("destination") or {})
        service_data = spec.get("service") or {}; service = _service_id(service_data) or _endpoint(service_data)
        authority = item.get("authority")
        policy = (authority or {}).get("policy") or {}
        binding = (authority or {}).get("binding")
        canonical_policy = (authority or {}).get("network_policy") or {}
        canonical_qos = dict((canonical_policy.get("action") or {}).get("qos_params") or {})
        canonical_selector = dict(canonical_policy.get("match") or spec.get("selector") or {})
        if canonical_qos.get("dscp_value") is not None:
            canonical_selector["dscp"] = canonical_qos["dscp_value"]
        effective_qos = self._effective_qos(binding, canonical_qos)
        if effective_qos["target_mbps"] is not None and objectives:
            # A current active binding is the durable QoS owner when a legacy
            # committed policy document omits its bandwidth field.
            objectives[0] = {
                **objectives[0],
                "requested": effective_qos["target_mbps"],
                "effective_threshold": effective_qos["target_mbps"],
                "unit": objectives[0].get("unit") or "Mbps",
            }
        readback_state = (
            "OWNER_IN_SYNC" if authority else
            ("PENDING" if item["workflow_activity"] == "ACTIVE" else "NOT_APPLICABLE")
        )
        attention = item["workflow_activity_reason"] if item["workflow_activity"] == "STALE_WORKFLOW" else (getattr(runtime, "failure_reason", None) or item["assurance"].get("failure_reason"))
        if item["enforcement_state"] in {"DEGRADED", "WAITING_FOR_INFRASTRUCTURE", "WITHDRAWING"} or item["assurance_state"] in {"VIOLATED", "UNSATISFIABLE", "ROOT_CAUSE_PENDING"} or item["workflow_activity"] == "STALE_WORKFLOW": group = "attention"
        elif item["workflow_activity"] == "ACTIVE" or item["enforcement_state"] in {"INSTALLING", "RECONCILING"}: group = "processing"
        else: group = "enforcing"
        if item["enforcement_state"] == "DEGRADED": display_status = "已降级·SLA未满足"
        elif item["enforcement_state"] == "ACTIVE" and item["assurance_state"] == "SATISFIED" and item["evidence_state"] == "STALE": display_status = "生效中·上次SLA已满足"
        elif item["enforcement_state"] == "ACTIVE" and item["assurance_state"] == "SATISFIED": display_status = "生效中·SLA已满足"
        elif item["workflow_activity"] == "ACTIVE": display_status = "执行中·待验证"
        else: display_status = "异常与等待"
        execution = item["execution"]
        lifecycle_projection = project_intent_lifecycle(
            item["aggregate"], runtime=runtime, jobs=item.get("jobs"), execution=execution,
        )
        # ``display_bucket`` is the sole status authority consumed by the
        # dashboard.  ``display_group`` remains a backward-compatible alias.
        display = self._display_projection(item)
        group = DISPLAY_BUCKETS[display["bucket"]]["group"]
        display_status = display["status"]
        summary = spec.get("original_text", "")[:160]
        if source and destination and objectives:
            target = objectives[0]
            summary = f"保障 {source} 到 {destination} 的 {service or '网络服务'} {target.get('type')} {target.get('requested')} {target.get('unit') or ''}".strip()
        withdrawable = bool(authority)
        waiting_card = item["lifecycle"] in CONVERSATIONAL_WAITING_LIFECYCLES and not authority and not item["has_owner"]
        if waiting_card:
            # This is intentionally a different contract from an effective
            # policy card.  Policy/binding/execution/southbound facts are
            # absent, not merely pending or unverified.
            dto = {
                "intent_id": item["intent_id"], "source_intent_id": item["source_intent_id"],
                "revision": item["revision"], "active_revision": item["active_revision"],
                "card_subtype": "CONVERSATIONAL_WAITING", "is_current": True,
                "effective_state": "CONVERSATIONAL_WAITING", "current_policy_owner": None,
                "current_resource_owner": None, "policy_id": None, "policy_version": None,
                "binding_id": None, "effective_scope_id": None, "traffic_scope_key": None,
                "selector": None, "committed_qos": {"mode": None, "bandwidth_mbps": None, "dscp": None},
                "effective_qos": {"mode": None, "target_mbps": None, "target_source": "NOT_APPLICABLE"},
                "policy_lineage": [], "requested_objectives": objectives, "effective_thresholds": [],
                "user_summary": summary, "original_text": spec.get("original_text", "")[:500],
                "workflow_state": item["lifecycle"], "runtime_lifecycle": item["lifecycle"],
                "workflow_activity": item["workflow_activity"], "workflow_activity_reason": item["workflow_activity_reason"],
                "enforcement_state": "NOT_INSTALLED", "assurance_state": "NOT_EVALUATED",
                "readback_state": "NOT_APPLICABLE", "evidence_state": "NOT_EVALUATED",
                "last_verified_state": "NOT_EVALUATED", "last_verified_at": None,
                "display_bucket": "ABNORMAL_WAITING", "display_group": "attention", "group": "attention",
                "display_tone": "warning", "display_status": "需要补充信息", "attention_reason": None,
                "service_id": None, "service": None, "source_id": None, "source_endpoint": None,
                "source_label": None, "destination_id": None, "destination_endpoint": None,
                "destination_label": None, "latest_measurements": None, "current_path": [],
                "selected_devices": [], "plan_id": None, "latest_execution_id": None,
                "latest_assurance_id": None, "created_at": _iso(getattr(runtime, "created_at", None)),
                "updated_at": _iso(getattr(runtime, "updated_at", None)), "last_updated_at": _iso(getattr(runtime, "updated_at", None)),
                "next_action": "等待你补充必要信息", "withdrawable": False,
                "withdraw_reason_if_false": "NO_POLICY_OR_BINDING", "can_withdraw": False,
                "can_open_details": True, "can_locate_topology": False,
                "currentness_reason": item["currentness_reason"], "superseded": item["superseded"],
                "superseded_by": item["superseded_by"], "projection_state": item["projection_state"],
                "projection_consistent": True, "projection_lag_seconds": 0,
            }
            if include_evidence:
                dto["evidence"] = None
            dto.update(lifecycle_projection)
            return dto
        failure_card = item["current_actionable_failure"]
        if failure_card:
            # A capacity/admission rejection is a first-class current state.
            # It intentionally exposes the requested business scope while
            # making the absence of all data-plane ownership explicit.
            failure_reason = self._actionable_failure_reason(item)
            dto = {
                "intent_id": item["intent_id"], "source_intent_id": item["source_intent_id"],
                "revision": item["revision"], "active_revision": item["active_revision"],
                "card_subtype": "CURRENT_ACTIONABLE_FAILURE", "is_current": True,
                "effective_state": "NOT_EFFECTIVE", "current_policy_owner": None,
                "current_resource_owner": None, "policy_id": None, "policy_version": None,
                "binding_id": None, "effective_scope_id": item["scope_key"], "traffic_scope_key": item["scope_key"],
                "selector": canonical_selector, "committed_qos": {"mode": None, "bandwidth_mbps": None, "dscp": None},
                "effective_qos": {"mode": None, "target_mbps": None, "target_source": "NOT_COMMITTED"},
                "policy_lineage": [], "requested_objectives": objectives, "effective_thresholds": [],
                "user_summary": summary, "original_text": spec.get("original_text", "")[:500],
                "workflow_state": item["lifecycle"], "runtime_lifecycle": item["lifecycle"],
                "workflow_activity": item["workflow_activity"], "workflow_activity_reason": item["workflow_activity_reason"],
                "enforcement_state": "NOT_INSTALLED", "assurance_state": item["assurance_state"],
                "readback_state": "NOT_APPLICABLE", "evidence_state": "NOT_EVALUATED",
                "last_verified_state": "NOT_EVALUATED", "last_verified_at": None,
                "display_bucket": "ABNORMAL_WAITING", "display_group": "attention", "group": "attention",
                "display_tone": "warning", "display_status": "当前意图未能生效", "attention_reason": failure_reason,
                "network_configuration_state": "NOT_EXECUTED", "southbound_writes": {"onos": 0, "p4": 0, "tc": 0},
                "service_id": service_data.get("service_id"), "service": service,
                "source_id": (spec.get("source") or {}).get("id"), "source_endpoint": source,
                "source_label": source, "destination_id": (spec.get("destination") or {}).get("id"),
                "destination_endpoint": destination, "destination_label": destination,
                "latest_measurements": None, "current_path": [], "selected_devices": [],
                "plan_id": getattr(runtime, "active_plan_id", None), "latest_execution_id": None,
                "latest_assurance_id": None, "created_at": _iso(getattr(runtime, "created_at", None)),
                "updated_at": _iso(getattr(runtime, "updated_at", None)), "last_updated_at": _iso(getattr(runtime, "updated_at", None)),
                "next_action": "可查看详细证据、重新尝试或撤销当前意图", "withdrawable": True,
                "withdraw_kind": "INTENT", "withdraw_reason_if_false": None, "can_withdraw": True,
                "can_open_details": True, "can_locate_topology": False,
                "currentness_reason": item["currentness_reason"], "superseded": item["superseded"],
                "superseded_by": item["superseded_by"], "projection_state": item["projection_state"],
                "projection_consistent": True, "projection_lag_seconds": 0,
            }
            if include_evidence:
                dto["evidence"] = item["evidence"]
            dto.update(lifecycle_projection)
            return dto
        dto = {"intent_id": item["intent_id"], "source_intent_id": item["source_intent_id"], "revision": item["revision"], "active_revision": item["active_revision"], "current_policy_owner": item["policy_id"], "current_resource_owner": binding.binding_id if binding else None, "policy_id": item["policy_id"], "policy_version": policy.get("version"), "binding_id": binding.binding_id if binding else None, "effective_scope_id": item["scope_key"], "is_current": True, "effective_state": "ACTIVE" if authority else "PENDING", "withdrawable": withdrawable, "withdraw_reason_if_false": None if withdrawable else "NOT_CURRENT_EFFECTIVE_OWNER", "currentness_reason": item["currentness_reason"], "superseded": item["superseded"], "superseded_by": item["superseded_by"], "user_summary": summary, "original_text": spec.get("original_text", "")[:500], "workflow_state": "COMPLETED" if item["assurance_state"] == "SATISFIED" else item["lifecycle"], "runtime_lifecycle": item["lifecycle"], "workflow_activity": item["workflow_activity"], "workflow_activity_reason": item["workflow_activity_reason"], "worker_heartbeat_at": item["worker_heartbeat_at"], "workflow_freshness": "FRESH" if item["workflow_activity"] == "ACTIVE" else item["workflow_activity"], "enforcement_state": item["enforcement_state"], "assurance_state": item["assurance_state"], "readback_state": readback_state, "evidence_state": item["evidence_state"], "last_verified_state": item["assurance_state"], "last_verified_at": item["evidence_freshness"].get("observed_at"), "evidence_freshness": item["evidence_freshness"], "projection_state": item["projection_state"], "projection_consistent": item["projection_consistent"], "projection_lag_seconds": _age(getattr(runtime, "updated_at", None)) if not item["projection_consistent"] else 0, "display_group": group, "group": group, "display_status": display_status, "attention_reason": attention, "service_id": service_data.get("service_id"), "service": service, "source_id": (spec.get("source") or {}).get("id"), "source_endpoint": source, "source_label": source, "destination_id": (spec.get("destination") or {}).get("id"), "destination_endpoint": destination, "destination_label": destination, "traffic_scope_key": item["scope_key"], "selector": canonical_selector, "committed_qos": {"mode": canonical_qos.get("qos_mode"), "bandwidth_mbps": canonical_qos.get("bandwidth"), "dscp": canonical_qos.get("dscp_value")}, "policy_lineage": [item["policy_id"]] if item["policy_id"] else [], "requested_objectives": objectives, "effective_thresholds": objectives, "latest_measurements": next((row for row in item["assurance"].get("assessments", []) if row.get("observed") is not None), None) or getattr(runtime, "observed_metrics", {}), "current_path": getattr(runtime, "current_path", []) or execution.get("selected_path", []), "selected_devices": [row.get("device_id") for row in execution.get("device_receipts", []) if row.get("device_id")], "plan_id": getattr(runtime, "active_plan_id", None), "latest_execution_id": execution.get("execution_id"), "latest_assurance_id": item["assurance"].get("assurance_id"), "created_at": _iso(getattr(runtime, "created_at", None)), "updated_at": _iso(getattr(runtime, "updated_at", None)), "last_updated_at": policy.get("updated_at") or _iso(getattr(runtime, "updated_at", None)), "next_action": "证据已过期，等待刷新" if item["evidence_state"] == "STALE" and item["enforcement_state"] == "ACTIVE" else ("工作流已中断，但网络策略仍存在" if item["workflow_activity"] == "STALE_WORKFLOW" else "已持续生效" if item["enforcement_state"] == "ACTIVE" else "正在处理"), "can_withdraw": withdrawable, "can_open_details": True, "can_locate_topology": bool(getattr(runtime, "current_path", []) or execution.get("selected_path"))}
        dto.update({
            "display_bucket": display["bucket"],
            "display_group": group,
            "group": group,
            "display_tone": DISPLAY_BUCKETS[display["bucket"]]["tone"],
            "display_status": display_status,
            "verification_status": self._verification_status(item),
            "committed_qos": {
                "mode": effective_qos["mode"],
                "bandwidth_mbps": effective_qos["target_mbps"],
                "dscp": canonical_qos.get("dscp_value"),
            },
            "effective_qos": effective_qos,
        })
        recovery = item.get("recovery") or {}
        dto.update({
            "recovery_state": recovery.get("recovery_state", "IDLE"),
            "runtime_instance_epoch": recovery.get("runtime_instance_epoch"),
            "environment_epoch": recovery.get("environment_epoch"),
            "environment_fingerprint_status": recovery.get("environment_fingerprint_status"),
            "last_recovery_id": recovery.get("last_recovery_id"),
            "last_recovery_started_at": recovery.get("last_recovery_started_at"),
            "last_recovery_completed_at": recovery.get("last_recovery_completed_at"),
            "recovery_result": recovery.get("recovery_result"),
            "capacity_freshness": recovery.get("capacity_freshness", "UNKNOWN"),
            "revalidation_required": bool(recovery.get("revalidation_required", False)),
        })
        if include_evidence: dto["evidence"] = item["evidence"]
        if include_projection_details: dto["projection_details"] = {"authoritative_state": dto["effective_state"], "projection_state": item["projection_state"], "authoritative_revision": policy.get("version") or item["active_revision"], "projection_revision": item["revision"], "projection_lag": not item["projection_consistent"], "authority": "COMMITTED_POLICY_PLUS_ACTIVE_BINDING" if authority else "LIVE_WORKFLOW"}
        dto.update(lifecycle_projection)
        return dto

    @staticmethod
    def _effective_qos(binding: Any, committed_qos: dict[str, Any]) -> dict[str, Any]:
        """Return the display target from the active QoS ownership contract.

        ``None`` is preserved as unknown.  In particular, a missing policy
        document value must never become a zero-Mbps display value.
        """
        binding_target = getattr(binding, "rate_mbps", None) if binding else None
        target = binding_target if binding_target is not None else committed_qos.get("bandwidth")
        binding_kind = getattr(binding, "reservation_kind", None) if binding else None
        mode = committed_qos.get("qos_mode") or (
            "RATE_LIMIT" if binding_kind == "RATE_LIMIT"
            else "MIN_BANDWIDTH" if binding_kind == "GUARANTEED"
            else None
        )
        return {
            "mode": mode,
            "target_mbps": target,
            "target_source": "ACTIVE_RESOURCE_BINDING" if binding_target is not None else ("COMMITTED_POLICY_QOS" if target is not None else "UNKNOWN"),
            "known": target is not None,
        }

    @staticmethod
    def _verification_status(item: dict[str, Any]) -> str:
        """Evidence detail only; this value never chooses a display bucket."""
        if item["assurance_state"] == "VIOLATED" and item["evidence_state"] == "VERIFIED":
            return "VIOLATED"
        if item["evidence_state"] == "STALE":
            return "STALE"
        if item["assurance_state"] == "SATISFIED" and item["evidence_state"] == "VERIFIED":
            return "SATISFIED"
        return "UNVERIFIED"

    @staticmethod
    def _actionable_failure_reason(item: dict[str, Any]) -> str:
        """Return a small operator-facing reason without exposing audit codes."""
        aggregate = item.get("aggregate") or {}
        result = aggregate.get("result") or {}
        payload = " ".join(str(value) for value in (
            getattr(item.get("runtime"), "failure_reason", None),
            aggregate.get("error"), aggregate.get("status"), result,
        ) if value is not None).upper()
        if "RUNTIME_CAPACITY_UNVERIFIED" in payload:
            return "运行时容量尚未验证"
        if "CAPACITY" in payload:
            return "当前可安全调度容量不足"
        if item.get("lifecycle") == "UNSATISFIABLE":
            return "当前条件不足以安全满足该意图"
        if item.get("lifecycle") in {"EXECUTION_FAILED", "FAILED", "ROLLBACK_FAILED"}:
            return "当前意图未完成，网络配置未保持为生效状态"
        return "当前意图需要处理后才能生效"

    @staticmethod
    def _display_projection(item: dict[str, Any]) -> dict[str, str]:
        """The single policy-effectiveness authority for dashboard status.

        Evidence freshness is intentionally excluded from the bucket decision.
        A resident ACTIVE policy is still effective when its last SLA proof is
        stale or unavailable; those facts remain available as evidence-detail
        metadata.  The only evidence-derived exception is a fresh, authoritative
        measured violation.
        """
        active_work = item["workflow_activity"] == "ACTIVE" or item["enforcement_state"] in {"INSTALLING", "RECONCILING", "WITHDRAWING"}
        if active_work:
            return {"bucket": "EXECUTING", "status": "正在执行"}

        fresh_violation = item["assurance_state"] == "VIOLATED" and item["evidence_state"] == "VERIFIED"
        if item["lifecycle"] in CONVERSATIONAL_WAITING_LIFECYCLES:
            return {"bucket": "ABNORMAL_WAITING", "status": "需要补充信息"}
        failure_lifecycle = item["lifecycle"] in {
            "NEEDS_CLARIFICATION", "NEEDS_APPROVAL", "UNSATISFIABLE",
            "EXECUTION_FAILED", "FAILED", "WITHDRAW_FAILED",
            "ROLLBACK_FAILED", "RECONCILIATION_FAILED", "BLOCKED", "CONFLICT",
        }
        aggregate_status = str((item.get("aggregate") or {}).get("status") or "").upper()
        authoritative_block = aggregate_status in {"CONFLICT", "BLOCKED", "UNSATISFIABLE", "FAILED"}
        if fresh_violation:
            return {"bucket": "ABNORMAL_WAITING", "status": "已降级 · SLA未满足"}
        if item["lifecycle"] == "UNSATISFIABLE":
            return {"bucket": "ABNORMAL_WAITING", "status": "当前意图无法满足"}
        if item["lifecycle"] in {"EXECUTION_FAILED", "FAILED", "ROLLBACK_FAILED", "RECONCILIATION_FAILED"}:
            return {"bucket": "ABNORMAL_WAITING", "status": "策略执行失败"}
        if failure_lifecycle or authoritative_block:
            return {"bucket": "ABNORMAL_WAITING", "status": "当前意图需要处理"}
        if item.get("pending_operation"):
            return {"bucket": "ABNORMAL_WAITING", "status": "策略更新等待恢复"}
        if item["has_owner"]:
            return {"bucket": "ACTIVE", "status": "策略已生效"}
        return {"bucket": "ABNORMAL_WAITING", "status": "异常与等待 · 待确认"}

    @staticmethod
    def _group(workflow: str, enforcement: str, assurance: str) -> str:
        """Compatibility helper; the DTO path adds workflow-activity context."""
        if enforcement in {"DEGRADED", "DRIFTED", "WAITING_FOR_INFRASTRUCTURE", "WITHDRAWING"} or assurance in {"ROOT_CAUSE_PENDING", "VIOLATED", "UNSATISFIABLE"} or workflow in ATTENTION_LIFECYCLES:
            return "attention"
        if enforcement == "ACTIVE" and workflow == "COMPLETED":
            return "enforcing"
        return "processing"

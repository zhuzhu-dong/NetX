from datetime import datetime, timezone
from types import SimpleNamespace

from models.intent_domain import IntentLifecycle, IntentObjective, IntentSpec, ObjectiveType
from services.current_intent_query import CurrentIntentQueryService


class FakeDomain:
    def __init__(self, runtime, *, lifecycle=IntentLifecycle.SATISFIED, execution=None, assurance=None, evidence=None):
        self.runtime = runtime
        self.spec = IntentSpec(
            intent_id=runtime.intent_id, original_text="protect a service", normalized_text="protect a service",
            source={"host": "h_research"}, destination={"host": "h_dc"}, service={"service_id": "video"},
            objectives=[IntentObjective(type=ObjectiveType.MIN_BANDWIDTH, value=30, unit="Mbps")],
        )
        self.execution_value = execution or {}
        self.assurance_value = assurance or {}
        self.evidence_value = evidence or {}
        self.base_store = SimpleNamespace(get=lambda _id: {})

    def current_runtimes(self, **_): return [self.runtime]
    def get_spec(self, *_): return self.spec
    def executions(self, *_): return [self.execution_value] if self.execution_value else []
    def assurance(self, *_): return [self.assurance_value] if self.assurance_value else []
    def evidence(self, *_): return [self.evidence_value] if self.evidence_value else []


def runtime(lifecycle=IntentLifecycle.SATISFIED, policies=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(intent_id="INT-CURRENT", lifecycle=lifecycle, revision=1, current_revision=1,
        active_policy_ids=["POL-1"] if policies is None else policies, active_plan_id="PLAN-1", current_path=["s1", "s2"],
        observed_metrics={}, created_at=now, updated_at=now, failure_reason=None)


def test_completed_active_satisfied_is_enforcing_with_verified_evidence():
    domain = FakeDomain(runtime(), execution={"status": "COMPLETED", "flows_installed": 1,
        "device_receipts": [{"device_id": "device:bmv2:1"}], "config_verified": True},
        assurance={"satisfied": True, "observed_at": datetime.now(timezone.utc).isoformat()},
        evidence={"observed_at": datetime.now(timezone.utc).isoformat()})
    item = CurrentIntentQueryService(domain).list_current()["groups"]["enforcing"][0]
    assert item["workflow_state"] == "COMPLETED"
    assert item["enforcement_state"] == "ACTIVE"
    assert item["assurance_state"] == "SATISFIED"
    assert item["evidence_state"] == "VERIFIED"
    assert item["display_bucket"] == "ACTIVE"
    assert item["display_group"] == "enforcing"


def test_policy_without_readback_is_current_but_not_verified():
    item = CurrentIntentQueryService(FakeDomain(runtime(), execution={"status": "COMPLETED"})).list_current()["groups"]["enforcing"][0]
    assert item["evidence_state"] != "VERIFIED"
    assert item["enforcement_state"] == "ACTIVE"
    assert item["display_bucket"] == "ACTIVE"
    assert item["verification_status"] == "UNVERIFIED"


def test_active_satisfied_stale_evidence_remains_active_not_a_violation():
    domain = FakeDomain(runtime(), execution={"status": "COMPLETED"},
        assurance={"satisfied": True, "observed_at": "2020-01-01T00:00:00+00:00"},
        evidence={"observed_at": "2020-01-01T00:00:00+00:00"})
    item = CurrentIntentQueryService(domain).list_current()["groups"]["enforcing"][0]
    assert item["evidence_state"] == "STALE"
    assert item["enforcement_state"] == "ACTIVE"
    assert item["display_bucket"] == "ACTIVE"
    assert item["display_status"] == "策略已生效"
    assert item["verification_status"] == "STALE"


def test_waiting_and_root_cause_pending_are_attention():
    waiting = CurrentIntentQueryService(FakeDomain(runtime(IntentLifecycle.RECEIVED, [])))._group("WAITING_FOR_INFRASTRUCTURE", "WAITING_FOR_INFRASTRUCTURE", "NOT_EVALUATED")
    diagnosing = CurrentIntentQueryService(FakeDomain(runtime(IntentLifecycle.DIAGNOSING)))._group("COLLECTING_DIAGNOSTICS", "DEGRADED", "ROOT_CAUSE_PENDING")
    assert waiting == diagnosing == "attention"


def test_failed_projection_with_owned_resource_remains_visible_for_operator_attention():
    item = CurrentIntentQueryService(FakeDomain(runtime(IntentLifecycle.FAILED, ["POL-OWNED"]))).list_current()["items"][0]
    assert item["current_policy_owner"] == "POL-OWNED"
    assert item["enforcement_state"] == "ACTIVE"
    assert item["is_current"] is True


def test_projection_needs_approval_cannot_override_satisfied_aggregate():
    state = runtime(IntentLifecycle.NEEDS_APPROVAL, [])
    domain = FakeDomain(state)
    domain.base_store = SimpleNamespace(get=lambda _id: {"status": "success", "updated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": {"verification_passed": True}, "result": {"execution_result": {"config_applied": True,
        "installed_flows": [{"device_id": "device:bmv2:1"}], "qos_enforcement": {"tc_readback": {}, "qos_binding": {"current_path": ["s1"]}}},
        "status_verification_report": {"passed": True}}})
    item = CurrentIntentQueryService(domain).list_current(include_projection_details=True)["items"][0]
    assert item["workflow_state"] == "COMPLETED"
    assert item["assurance_state"] == "SATISFIED"
    assert item["projection_consistent"] is False
    assert item["projection_details"]["projection_state"] == "NEEDS_APPROVAL"


def _record(intent_id, *, lifecycle=IntentLifecycle.SATISFIED, bandwidth=30, service="video"):
    state = runtime(lifecycle, ["POL-STALE"])
    state.intent_id = intent_id
    return {
        "runtime": state,
        "spec": IntentSpec(
            intent_id=intent_id, original_text=f"audit {bandwidth}", normalized_text=f"audit {bandwidth}",
            source={"host": "old-source"}, destination={"host": "old-destination"},
            service={"service_id": service},
            objectives=[IntentObjective(type=ObjectiveType.MIN_BANDWIDTH, value=bandwidth, unit="Mbps")],
        ),
        "aggregate": {}, "execution": {}, "assurance": {}, "evidence": {},
        "jobs": [], "revisions": [], "policies": [],
    }


def _owner(scope, *, intent_id, policy_id, service, source, destination, protocol, port, dscp, mode, bandwidth, version=1):
    return {
        "scope_key": scope,
        "policy": {"intent_id": intent_id, "policy_id": policy_id, "version": version, "updated_at": "2026-08-09T04:00:00+00:00"},
        "binding": SimpleNamespace(
            binding_id=f"RB-{scope}", policy_id=policy_id, service_id=service,
            rate_mbps=bandwidth,
            reservation_kind="RATE_LIMIT" if mode == "RATE_LIMIT" else "GUARANTEED",
        ),
        "network_policy": {
            "match": {"source_ip": source, "destination_ip": destination, "protocol": protocol, "destination_port": port},
            "action": {"qos_params": {"qos_mode": mode, "dscp_value": dscp, "bandwidth": bandwidth}},
        },
        "yang": {"service_id": service, "raw_intent": f"canonical {service}"},
    }


class AuthorityQuery(CurrentIntentQueryService):
    def __init__(self, records, owners):
        self.records = records
        self.owners = owners
        self.domain = SimpleNamespace(base_store=SimpleNamespace())
        self.currentness = __import__(
            "services.current_intent_query", fromlist=["CurrentIntentCurrentnessResolver"]
        ).CurrentIntentCurrentnessResolver()

    def _records(self, _limit):
        return self.records

    def _managed_effective_owners(self):
        return self.owners

    def _supports_effective_authority(self):
        return True


def _effective(query):
    return query.list_current(include_projection_details=True)["items"]


def test_effective_projection_replaces_successful_history_with_committed_owner():
    records = [_record("INT-30", bandwidth=30), _record("INT-20", bandwidth=20), _record("INT-10", bandwidth=10)]
    owners = {"scope-research": _owner("scope-research", intent_id="INT-10", policy_id="POL-R", service="research",
        source="192.168.10.10/32", destination="10.0.0.10/32", protocol="UDP", port=5004, dscp=46,
        mode="MIN_BANDWIDTH", bandwidth=10, version=14)}
    items = _effective(AuthorityQuery(records, owners))
    assert len(items) == 1
    assert items[0]["intent_id"] == "INT-10"
    assert items[0]["committed_qos"] == {"mode": "MIN_BANDWIDTH", "bandwidth_mbps": 10, "dscp": 46}
    assert items[0]["effective_state"] == "ACTIVE"
    assert items[0]["policy_version"] == 14


def test_failed_update_never_replaces_committed_effective_owner():
    records = [_record("INT-10", bandwidth=10), _record("INT-60", lifecycle=IntentLifecycle.FAILED, bandwidth=60)]
    owners = {"scope-research": _owner("scope-research", intent_id="INT-10", policy_id="POL-R", service="research",
        source="192.168.10.10/32", destination="10.0.0.10/32", protocol="UDP", port=5004, dscp=46,
        mode="MIN_BANDWIDTH", bandwidth=10)}
    items = _effective(AuthorityQuery(records, owners))
    assert [item["intent_id"] for item in items] == ["INT-10"]
    assert items[0]["requested_objectives"][0]["requested"] == 10


def test_historical_records_remain_untouched_while_effective_view_selects_one():
    records = [_record("INT-30", bandwidth=30), _record("INT-10", bandwidth=10)]
    owners = {"scope-research": _owner("scope-research", intent_id="INT-10", policy_id="POL-R", service="research",
        source="192.168.10.10/32", destination="10.0.0.10/32", protocol="UDP", port=5004, dscp=46,
        mode="MIN_BANDWIDTH", bandwidth=10)}
    _effective(AuthorityQuery(records, owners))
    assert [record["spec"].intent_id for record in records] == ["INT-30", "INT-10"]


def test_withdrawn_scope_is_removed_from_effective_view():
    items = _effective(AuthorityQuery([_record("INT-W", lifecycle=IntentLifecycle.WITHDRAWN)], {}))
    assert items == []


def test_withdrawn_runtime_cannot_be_resurrected_by_a_historical_active_binding():
    """The current panel must wait for formal ownership reconciliation, not infer ownership backward."""
    records = [_record("INT-W", lifecycle=IntentLifecycle.WITHDRAWN)]
    owners = {"scope-research": _owner("scope-research", intent_id="INT-W", policy_id="POL-R", service="research",
        source="192.168.10.10/32", destination="10.0.0.10/32", protocol="UDP", port=5004, dscp=46,
        mode="MIN_BANDWIDTH", bandwidth=10)}
    assert _effective(AuthorityQuery(records, owners)) == []


def test_live_owner_is_current_after_withdrawn_preference_is_reconciled():
    records = [_record("INT-W", lifecycle=IntentLifecycle.WITHDRAWN), _record("INT-BASELINE")]
    owners = {"scope-research": _owner("scope-research", intent_id="INT-BASELINE", policy_id="POL-R", service="research",
        source="192.168.10.10/32", destination="10.0.0.10/32", protocol="UDP", port=5004, dscp=46,
        mode="MIN_BANDWIDTH", bandwidth=10)}
    items = _effective(AuthorityQuery(records, owners))
    assert [item["intent_id"] for item in items] == ["INT-BASELINE"]


def test_compound_parent_projects_two_authoritative_child_scopes_not_parent_plus_children():
    records = [_record("INT-COMPOUND", bandwidth=30, service="parent")]
    owners = {
        "scope-research": _owner("scope-research", intent_id="INT-COMPOUND", policy_id="POL-R", service="research",
            source="192.168.10.10/32", destination="10.0.0.10/32", protocol="UDP", port=5004, dscp=46,
            mode="MIN_BANDWIDTH", bandwidth=10),
        "scope-backup": _owner("scope-backup", intent_id="INT-COMPOUND", policy_id="POL-B", service="backup",
            source="10.0.0.20/32", destination="192.168.10.10/32", protocol="TCP", port=5203, dscp=8,
            mode="RATE_LIMIT", bandwidth=20),
    }
    items = _effective(AuthorityQuery(records, owners))
    assert len(items) == 2
    assert {(item["service"], item["committed_qos"]["mode"]) for item in items} == {
        ("research", "MIN_BANDWIDTH"), ("backup", "RATE_LIMIT"),
    }
    assert {item["traffic_scope_key"] for item in items} == {"scope-research", "scope-backup"}


def test_different_effective_scopes_are_independently_displayed_with_unique_binding_owner():
    records = [_record("INT-R", bandwidth=10), _record("INT-S", bandwidth=10)]
    owners = {
        "scope-r": _owner("scope-r", intent_id="INT-R", policy_id="POL-R", service="research",
            source="192.168.10.10/32", destination="10.0.0.10/32", protocol="UDP", port=5004, dscp=46,
            mode="MIN_BANDWIDTH", bandwidth=10),
        "scope-s": _owner("scope-s", intent_id="INT-S", policy_id="POL-S", service="shared",
            source="192.168.10.20/32", destination="10.0.0.10/32", protocol="UDP", port=5004, dscp=0,
            mode="MIN_BANDWIDTH", bandwidth=10),
    }
    items = _effective(AuthorityQuery(records, owners))
    assert {item["binding_id"] for item in items} == {"RB-scope-r", "RB-scope-s"}
    assert {item["current_resource_owner"] for item in items} == {"RB-scope-r", "RB-scope-s"}


def test_fresh_violation_is_abnormal_with_violation_status():
    domain = FakeDomain(runtime(IntentLifecycle.VIOLATED), execution={"status": "COMPLETED"},
        evidence={"observed_at": datetime.now(timezone.utc).isoformat()})
    item = CurrentIntentQueryService(domain).list_current()["groups"]["attention"][0]
    assert item["display_bucket"] == "ABNORMAL_WAITING"
    assert item["display_status"] == "已降级 · SLA未满足"


def test_active_policy_with_expired_evidence_stays_in_active_section():
    domain = FakeDomain(runtime(), execution={"status": "COMPLETED"},
        evidence={"observed_at": "2020-01-01T00:00:00+00:00"})
    item = CurrentIntentQueryService(domain).list_current()["groups"]["enforcing"][0]
    assert item["display_bucket"] == "ACTIVE"
    assert item["verification_status"] == "STALE"


def test_needs_clarification_and_unsatisfiable_are_abnormal_waiting():
    clarification = CurrentIntentQueryService(FakeDomain(runtime(IntentLifecycle.NEEDS_CLARIFICATION))).list_current()["items"][0]
    unsatisfiable = CurrentIntentQueryService(FakeDomain(runtime(IntentLifecycle.UNSATISFIABLE))).list_current()["items"][0]
    assert clarification["display_bucket"] == "ABNORMAL_WAITING"
    assert unsatisfiable["display_bucket"] == "ABNORMAL_WAITING"


def _unowned_failure_record(intent_id, lifecycle, *, updated_at, result=None):
    row = _record(intent_id, lifecycle=lifecycle)
    row["runtime"].active_policy_ids = []
    row["runtime"].active_plan_id = None
    row["runtime"].updated_at = updated_at
    row["spec"] = row["spec"].model_copy(update={
        "selector": {"source_ip": "192.168.20.10/32", "destination_ip": "10.0.0.10/32", "protocol": "UDP", "destination_port": 5004},
        "semantic_scope_complete": True,
    })
    row["aggregate"] = {"status": str(lifecycle).lower(), "result": result or {}}
    return row


def test_unowned_unsatisfiable_is_current_actionable_failure_with_no_dataplane_owner():
    now = datetime.now(timezone.utc)
    row = _unowned_failure_record(
        "INT-CAPACITY", IntentLifecycle.UNSATISFIABLE, updated_at=now,
        result={"capacity_admission": {"reason_code": "RUNTIME_CAPACITY_UNVERIFIED"}},
    )
    item = _effective(AuthorityQuery([row], {}))[0]
    assert item["card_subtype"] == "CURRENT_ACTIONABLE_FAILURE"
    assert item["display_bucket"] == "ABNORMAL_WAITING"
    assert item["display_status"] == "当前意图未能生效"
    assert item["policy_id"] is None and item["binding_id"] is None
    assert item["network_configuration_state"] == "NOT_EXECUTED"
    assert item["southbound_writes"] == {"onos": 0, "p4": 0, "tc": 0}
    assert item["attention_reason"] == "运行时容量尚未验证"
    assert item["withdraw_kind"] == "INTENT"


def test_rejected_and_execution_failed_without_policy_remain_current_attention():
    now = datetime.now(timezone.utc)
    rejected = _unowned_failure_record("INT-REJECTED", "REJECTED", updated_at=now)
    failed = _unowned_failure_record("INT-EXEC", IntentLifecycle.EXECUTION_FAILED, updated_at=now)
    failed["spec"] = failed["spec"].model_copy(update={"service": {"service_id": "backup"}})
    items = _effective(AuthorityQuery([rejected, failed], {}))
    assert {item["intent_id"] for item in items} == {"INT-REJECTED", "INT-EXEC"}
    assert {item["display_bucket"] for item in items} == {"ABNORMAL_WAITING"}


def test_only_the_latest_unresolved_failure_for_one_scope_is_current():
    older = _unowned_failure_record("INT-OLD", IntentLifecycle.UNSATISFIABLE, updated_at=datetime(2026, 8, 9, tzinfo=timezone.utc))
    newer = _unowned_failure_record("INT-NEW", IntentLifecycle.UNSATISFIABLE, updated_at=datetime(2026, 8, 10, tzinfo=timezone.utc))
    items = _effective(AuthorityQuery([older, newer], {}))
    assert [item["intent_id"] for item in items] == ["INT-NEW"]


def test_withdrawn_or_superseded_failure_does_not_reenter_current_attention():
    now = datetime.now(timezone.utc)
    withdrawn = _unowned_failure_record("INT-WITHDRAWN", IntentLifecycle.WITHDRAWN, updated_at=now)
    superseded = _unowned_failure_record("INT-SUPERSEDED", "SUPERSEDED", updated_at=now)
    assert _effective(AuthorityQuery([withdrawn, superseded], {})) == []


def test_historical_incomplete_failure_does_not_inflate_current_attention_queue():
    row = _unowned_failure_record("INT-OLD-INCOMPLETE", IntentLifecycle.FAILED, updated_at=datetime.now(timezone.utc))
    row["spec"] = row["spec"].model_copy(update={"semantic_scope_complete": False})
    assert _effective(AuthorityQuery([row], {})) == []


def test_withdrawing_newest_failure_does_not_resurrect_an_older_replaced_failure():
    older = _unowned_failure_record("INT-OLD", IntentLifecycle.UNSATISFIABLE, updated_at=datetime(2026, 8, 9, tzinfo=timezone.utc))
    withdrawn_newer = _unowned_failure_record("INT-NEW", IntentLifecycle.WITHDRAWN, updated_at=datetime(2026, 8, 10, tzinfo=timezone.utc))
    assert _effective(AuthorityQuery([older, withdrawn_newer], {})) == []


def test_needs_clarification_without_policy_or_binding_remains_current_waiting():
    item = CurrentIntentQueryService(
        FakeDomain(runtime(IntentLifecycle.NEEDS_CLARIFICATION, []))
    ).list_current()["groups"]["attention"][0]
    assert item["current_policy_owner"] is None
    assert item["current_resource_owner"] is None
    assert item["display_bucket"] == "ABNORMAL_WAITING"
    assert item["display_status"] == "需要补充信息"
    assert item["effective_qos"]["target_mbps"] is None
    assert item["is_current"] is True


def test_clarification_projection_is_a_scope_less_conversational_waiting_card():
    item = CurrentIntentQueryService(
        FakeDomain(runtime(IntentLifecycle.NEEDS_CLARIFICATION, []))
    ).list_current()["groups"]["attention"][0]
    assert item["card_subtype"] == "CONVERSATIONAL_WAITING"
    assert item["traffic_scope_key"] is None
    assert item["policy_id"] is None
    assert item["binding_id"] is None
    assert item["latest_execution_id"] is None
    assert item["effective_qos"]["target_mbps"] is None
    assert item["evidence_state"] == "NOT_EVALUATED"


def test_pending_execution_is_executing_even_when_a_scope_owner_exists():
    domain = FakeDomain(runtime(IntentLifecycle.DEPLOYING), execution={"status": "INSTALLING"})
    item = CurrentIntentQueryService(domain).list_current()["groups"]["processing"][0]
    assert item["display_bucket"] == "EXECUTING"
    assert item["display_status"] == "正在执行"


def test_effective_qos_uses_binding_when_committed_policy_omits_bandwidth():
    records = [_record("INT-R", bandwidth=30), _record("INT-S", bandwidth=30)]
    owners = {
        "scope-r": _owner("scope-r", intent_id="INT-R", policy_id="POL-R", service="research",
            source="192.168.10.10/32", destination="10.0.0.10/32", protocol="UDP", port=5004, dscp=46,
            mode="MIN_BANDWIDTH", bandwidth=10),
        "scope-s": _owner("scope-s", intent_id="INT-S", policy_id="POL-S", service="shared",
            source="192.168.10.20/32", destination="10.0.0.10/32", protocol="UDP", port=5004, dscp=0,
            mode="MIN_BANDWIDTH", bandwidth=10),
    }
    for owner in owners.values():
        owner["network_policy"]["action"]["qos_params"]["bandwidth"] = None
    items = _effective(AuthorityQuery(records, owners))
    assert {item["service"]: item["effective_qos"]["target_mbps"] for item in items} == {"research": 10, "shared": 10}
    assert {item["effective_qos"]["target_source"] for item in items} == {"ACTIVE_RESOURCE_BINDING"}


def test_unknown_effective_qos_is_never_coerced_to_zero():
    owner = _owner("scope-r", intent_id="INT-R", policy_id="POL-R", service="research",
        source="192.168.10.10/32", destination="10.0.0.10/32", protocol="UDP", port=5004, dscp=46,
        mode="MIN_BANDWIDTH", bandwidth=10)
    owner["binding"] = SimpleNamespace(binding_id="RB-scope-r", policy_id="POL-R", service_id="research")
    owner["network_policy"]["action"]["qos_params"]["bandwidth"] = None
    item = _effective(AuthorityQuery([_record("INT-R", bandwidth=30)], {"scope-r": owner}))[0]
    assert item["effective_qos"]["target_mbps"] is None
    assert item["committed_qos"]["bandwidth_mbps"] is None

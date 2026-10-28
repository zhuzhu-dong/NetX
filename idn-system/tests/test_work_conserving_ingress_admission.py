from infra.ingress_admission import (
    build_activity_snapshot,
    calculate_work_conserving_ingress_budget,
)


def _protected(demand: float = 40.0):
    return {
        "scope_key": "protected", "requested_min_bandwidth_mbps": 30,
        "implementation_guaranteed_floor_mbps": 30,
        "observed_demand_mbps": demand,
    }


def _be(key: str, demand: float = 95.0, **extra):
    return {"scope_key": key, "observed_demand_mbps": demand, **extra}


def test_floor_is_not_admission_ceiling_when_demand_can_borrow():
    result = calculate_work_conserving_ingress_budget(
        schedulable_capacity_mbps=49, active_protected_scopes=[_protected(40)],
        active_best_effort_scopes=[_be("be-a")], capacity_record_id="CAP", environment_epoch="ENV",
    )
    protected = result["protected_scopes"][0]
    assert protected["implementation_guaranteed_floor_mbps"] == 30
    assert protected["ingress_admission_ceiling_mbps"] == 40
    assert protected["borrowed_capacity_mbps"] == 10
    assert result["active_be_aggregate_budget_mbps"] == 9
    assert result["expected_aggregate_ceiling_mbps"] == 49


def test_lower_protected_demand_releases_capacity_to_active_be():
    result = calculate_work_conserving_ingress_budget(
        schedulable_capacity_mbps=49, active_protected_scopes=[_protected(30)],
        active_best_effort_scopes=[_be("be-a")], capacity_record_id="CAP", environment_epoch="ENV",
    )
    assert result["protected_scopes"][0]["ingress_admission_ceiling_mbps"] == 30
    assert result["active_be_aggregate_budget_mbps"] == 19


def test_only_active_be_scopes_share_runtime_budget():
    result = calculate_work_conserving_ingress_budget(
        schedulable_capacity_mbps=49, active_protected_scopes=[_protected(40)],
        active_best_effort_scopes=[_be("be-a", weight=1), _be("be-b", weight=2)],
        capacity_record_id="CAP", environment_epoch="ENV",
    )
    allocations = {item["scope_key"]: item["runtime_budget_mbps"] for item in result["active_be_allocations"]}
    assert allocations == {"be-a": 3, "be-b": 6}
    assert sum(allocations.values()) == 9


def test_activity_requires_fresh_counter_or_workload_evidence_and_debounces():
    snapshot = build_activity_snapshot([
        {"scope_key": "active", "counter_delta_bytes": 10, "observation_age_seconds": 1},
        {"scope_key": "idle", "counter_delta_bytes": 0, "observation_age_seconds": 1,
         "previous_activity_state": "ACTIVE", "idle_windows": 2},
        {"scope_key": "stale", "counter_delta_bytes": 10, "observation_age_seconds": 99},
    ], observation_window_seconds=10, freshness_seconds=20)
    states = {row["scope_key"]: row["activity_state"] for row in snapshot["scopes"]}
    assert states == {"active": "ACTIVE", "idle": "IDLE", "stale": "STALE"}

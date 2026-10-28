"""Work-conserving pre-dataplane admission budgeting.

This module deliberately separates a durable MIN_BANDWIDTH reservation from a
short-lived admission ceiling.  A guaranteed floor is consumed by the resource
ledger; borrowed capacity is a runtime decision and is never persisted as a
second reservation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


ACTIVITY_STATES = {"ACTIVE", "IDLE", "UNKNOWN", "STALE"}


def _number(scope: dict[str, Any], name: str, default: float = 0.0) -> float:
    return max(0.0, float(scope.get(name, default) or default))


def build_activity_snapshot(
    scopes: list[dict[str, Any]], *, observation_window_seconds: float,
    freshness_seconds: float, generated_at: str | None = None,
) -> dict[str, Any]:
    """Classify scopes from bounded counter deltas or a workload declaration.

    A database record alone is never activity evidence.  Callers can supply a
    measured ``counter_delta_bytes`` / ``observed_demand_mbps`` or a current
    workload declaration.  ``UNKNOWN`` and stale observations do not receive
    runtime budget.  The previous state may be supplied to implement a
    debounce threshold without coupling this pure function to a database.
    """
    now = generated_at or datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    for raw in scopes:
        item = dict(raw)
        declared = bool(item.get("workload_active") or item.get("active_declaration"))
        delta = _number(item, "counter_delta_bytes")
        demand = _number(item, "observed_demand_mbps")
        age = _number(item, "observation_age_seconds", freshness_seconds + 1)
        previous = str(item.get("previous_activity_state") or "UNKNOWN").upper()
        fresh = age <= freshness_seconds
        signal = declared or delta > 0 or demand > 0
        if not fresh:
            state = "STALE"
        elif signal:
            state = "ACTIVE"
        elif previous == "ACTIVE" and _number(item, "idle_windows", 0) < 2:
            # One empty window is not enough to make a live source flap.
            state = "ACTIVE"
        elif previous in ACTIVITY_STATES:
            state = "IDLE"
        else:
            state = "UNKNOWN"
        rows.append({
            "scope_key": str(item.get("scope_key") or item.get("scope_id") or ""),
            "activity_state": state,
            "observed_demand_mbps": demand,
            "counter_delta_bytes": delta,
            "observation_age_seconds": age,
            "fresh": fresh,
            "reason": "WORKLOAD_OR_COUNTER_DELTA" if state == "ACTIVE" else (
                "OBSERVATION_STALE" if state == "STALE" else "NO_RECENT_ACTIVITY"
            ),
        })
    return {
        "activity_snapshot_id": f"ACT-{now.replace(':', '').replace('+', '').replace('-', '')}",
        "generated_at": now,
        "observation_window_seconds": observation_window_seconds,
        "freshness_seconds": freshness_seconds,
        "scopes": rows,
    }


def calculate_work_conserving_ingress_budget(
    *, schedulable_capacity_mbps: float, active_protected_scopes: list[dict[str, Any]],
    active_best_effort_scopes: list[dict[str, Any]], capacity_record_id: str,
    environment_epoch: str, minimum_be_liveness_mbps: float = 0.0,
    allocation_epoch: int = 1,
) -> dict[str, Any]:
    """Return a conservative, work-conserving admission allocation.

    Protected flows first retain their requested floors, then may borrow up to
    their *observed* demand.  Only the currently ACTIVE BE set shares the
    remaining capacity.  This makes a 30 Mbps MIN_BANDWIDTH guarantee distinct
    from a 40 Mbps admission ceiling when demand and capacity permit it.
    """
    schedulable = _number({"value": schedulable_capacity_mbps}, "value")
    if schedulable <= 0:
        raise ValueError("INGRESS_SCHEDULABLE_CAPACITY_REQUIRED")
    protected = [dict(item) for item in active_protected_scopes]
    active_be = [dict(item) for item in active_best_effort_scopes]
    floors = sum(_number(item, "implementation_guaranteed_floor_mbps", _number(item, "guaranteed_floor_mbps")) for item in protected)
    if floors > schedulable + 1e-6:
        raise ValueError("INGRESS_GUARANTEED_FLOORS_EXCEED_SCHEDULABLE")

    remaining = schedulable - floors
    protected_rows: list[dict[str, Any]] = []
    # Deterministic priority ordering means budget calculations are repeatable.
    for scope in sorted(protected, key=lambda item: (int(item.get("priority", 100)), str(item.get("scope_key") or item.get("scope_id") or ""))):
        floor = _number(scope, "implementation_guaranteed_floor_mbps", _number(scope, "guaranteed_floor_mbps"))
        demand = max(floor, _number(scope, "observed_demand_mbps", floor))
        borrowed = min(max(0.0, demand - floor), remaining)
        ceiling = floor + borrowed
        remaining -= borrowed
        protected_rows.append({
            **scope,
            "requested_min_bandwidth_mbps": _number(scope, "requested_min_bandwidth_mbps", floor),
            "implementation_guaranteed_floor_mbps": floor,
            "observed_active_demand_mbps": demand,
            "ingress_admission_ceiling_mbps": ceiling,
            "borrowed_capacity_mbps": borrowed,
        })

    liveness_total = min(remaining, max(0.0, minimum_be_liveness_mbps) * len(active_be))
    distributable = remaining
    total_weight = sum(max(_number(item, "weight", 1.0), 1e-6) for item in active_be)
    be_rows: list[dict[str, Any]] = []
    for scope in active_be:
        share = distributable * max(_number(scope, "weight", 1.0), 1e-6) / total_weight if total_weight else 0.0
        be_rows.append({**scope, "runtime_budget_mbps": share, "activity_state": "ACTIVE"})
    inactive = []
    for scope in [*protected, *active_be]:
        # Included only for a uniform output shape; callers append inactive
        # discovered scopes separately below.
        _ = scope
    expected = sum(item["ingress_admission_ceiling_mbps"] for item in protected_rows) + sum(item["runtime_budget_mbps"] for item in be_rows)
    if expected > schedulable + 1e-6:
        raise AssertionError("INGRESS_AGGREGATE_BUDGET_INVARIANT")
    return {
        "capacity_record_id": capacity_record_id,
        "environment_epoch": environment_epoch,
        "allocation_epoch": allocation_epoch,
        "schedulable_capacity_mbps": schedulable,
        "protected_floors_mbps": floors,
        "protected_scopes": protected_rows,
        "active_be_aggregate_budget_mbps": sum(item["runtime_budget_mbps"] for item in be_rows),
        "active_be_allocations": be_rows,
        "inactive_scopes": inactive,
        "unused_capacity_mbps": max(0.0, schedulable - expected),
        "expected_aggregate_ceiling_mbps": expected,
        "minimum_be_liveness_reserved_mbps": liveness_total,
        "allocation_reason": "WORK_CONSERVING_ACTIVE_SCOPE_ALLOCATION",
    }


def calculate_ingress_admission_budget(**kwargs: Any) -> dict[str, Any]:
    """Compatibility wrapper for callers using the earlier static helper."""
    schedulable = float(kwargs["schedulable_capacity_mbps"])
    reservations = list(kwargs.get("implementation_reservations_mbps") or [])
    protected = [
        {"scope_key": f"protected-{index}", "implementation_guaranteed_floor_mbps": float(value),
         "observed_demand_mbps": float(value)}
        for index, value in enumerate(reservations)
    ]
    active_be = list(kwargs.get("best_effort_scopes") or [])
    return calculate_work_conserving_ingress_budget(
        schedulable_capacity_mbps=schedulable, active_protected_scopes=protected,
        active_best_effort_scopes=active_be, capacity_record_id="UNSPECIFIED",
        environment_epoch="UNSPECIFIED",
    )

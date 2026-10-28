"""Desired/observed policy state and drift contracts."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PolicyLifecycle(str, Enum):
    PLANNED = "PLANNED"
    CHECKED = "CHECKED"
    APPROVED = "APPROVED"
    INSTALLING = "INSTALLING"
    ACTIVE = "ACTIVE"
    UPDATING = "UPDATING"
    DEGRADED = "DEGRADED"
    RECONCILING = "RECONCILING"
    ROLLING_BACK = "ROLLING_BACK"
    FAILED = "FAILED"
    REMOVED = "REMOVED"


class DriftType(str, Enum):
    IN_SYNC = "IN_SYNC"
    MISSING = "MISSING"
    STALE = "STALE"
    DRIFTED = "DRIFTED"
    EXTRA = "EXTRA"
    UNKNOWN = "UNKNOWN"


class DesiredPolicyState(BaseModel):
    policy_id: str
    traffic_scope_key: str
    desired_state: PolicyLifecycle
    version: int
    expected: dict[str, Any]
    updated_at: str


class ObservedPolicyState(BaseModel):
    policy_id: str
    traffic_scope_key: str
    observed_state: PolicyLifecycle
    drift_type: DriftType
    observed: dict[str, Any]
    checked_at: str
    last_repair_at: str | None = None
    repair_attempts: int = 0


class ReconciliationSummary(BaseModel):
    run_id: str
    status: str
    checked: int = 0
    in_sync: int = 0
    missing: int = 0
    drifted: int = 0
    extra: int = 0
    repaired: list[str] = Field(default_factory=list)
    repair_failed: list[dict[str, Any]] = Field(default_factory=list)
    workload_result: dict[str, Any] = Field(default_factory=dict)
    infrastructure: dict[str, Any] = Field(default_factory=dict)
    started_at: str
    finished_at: str

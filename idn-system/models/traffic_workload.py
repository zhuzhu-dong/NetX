"""Generic, persistent traffic workload contracts.

ServiceProfile describes *what* an application is.  TrafficWorkload describes
one concrete runtime instance, independently from intents and validation runs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TrafficRole(str, Enum):
    PRODUCTION = "PRODUCTION"
    BACKGROUND = "BACKGROUND"
    VALIDATION = "VALIDATION"
    HEALTH_PROBE = "HEALTH_PROBE"
    DIAGNOSTIC = "DIAGNOSTIC"


class WorkloadPattern(str, Enum):
    CONSTANT_BIT_RATE = "CONSTANT_BIT_RATE"
    BURSTY = "BURSTY"
    BULK_TRANSFER = "BULK_TRANSFER"
    PERIODIC = "PERIODIC"
    INTERACTIVE = "INTERACTIVE"
    BEST_EFFORT = "BEST_EFFORT"


class WorkloadStartMode(str, Enum):
    IMMEDIATE = "IMMEDIATE"
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"


class WorkloadLifecycle(str, Enum):
    CREATED = "CREATED"
    SCHEDULED = "SCHEDULED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class RecoveryPolicy(str, Enum):
    AUTO_RESTART = "AUTO_RESTART"
    MANUAL = "MANUAL"
    DO_NOT_RESTART = "DO_NOT_RESTART"


class WorkloadSelector(BaseModel):
    source_ip: str
    destination_ip: str
    protocol: str
    source_port: int | None = Field(default=None, ge=1, le=65535)
    destination_port: int | None = Field(default=None, ge=1, le=65535)
    dscp: int | None = Field(default=None, ge=0, le=63)
    vlan_id: int | None = Field(default=None, ge=1, le=4094)

    @field_validator("protocol")
    @classmethod
    def normalize_protocol(cls, value: str) -> str:
        normalized = str(value).upper()
        if normalized not in {"UDP", "TCP", "ICMP"}:
            raise ValueError("protocol must be UDP, TCP or ICMP")
        return normalized

    @model_validator(mode="after")
    def validate_ports(self) -> "WorkloadSelector":
        if self.protocol in {"UDP", "TCP"} and self.destination_port is None:
            raise ValueError(f"{self.protocol} workload requires destination_port")
        if self.protocol == "ICMP" and (
            self.source_port is not None or self.destination_port is not None
        ):
            raise ValueError("ICMP workload cannot define transport ports")
        return self


class TrafficWorkloadCreate(BaseModel):
    workload_id: str = Field(default_factory=lambda: f"WL-{uuid4().hex[:16]}")
    service_id: str
    src_endpoint: str
    dst_endpoint: str
    selector: WorkloadSelector
    traffic_pattern: WorkloadPattern = WorkloadPattern.BEST_EFFORT
    traffic_role: TrafficRole = TrafficRole.PRODUCTION
    offered_rate_mbps: float | None = Field(default=None, gt=0)
    packet_size_bytes: int = Field(default=1200, ge=64, le=9000)
    burst_size_bytes: int | None = Field(default=None, ge=1)
    burst_interval_seconds: float | None = Field(default=None, gt=0)
    parallel_flows: int = Field(default=1, ge=1, le=128)
    start_mode: WorkloadStartMode = WorkloadStartMode.MANUAL
    start_time: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=1)
    recurrence: dict[str, Any] | None = None
    priority: int = Field(default=100, ge=0, le=65535)
    tenant_id: str = "default"
    application_id: str | None = None
    recovery_policy: RecoveryPolicy = RecoveryPolicy.AUTO_RESTART
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_schedule_and_rate(self) -> "TrafficWorkloadCreate":
        if self.start_mode == WorkloadStartMode.SCHEDULED and self.start_time is None:
            raise ValueError("SCHEDULED workload requires start_time")
        if self.selector.protocol == "UDP" and self.offered_rate_mbps is None:
            raise ValueError("UDP workload requires offered_rate_mbps")
        if self.traffic_pattern == WorkloadPattern.BURSTY:
            if self.burst_size_bytes is None or self.burst_interval_seconds is None:
                raise ValueError("BURSTY workload requires burst_size_bytes and burst_interval_seconds")
        return self


class TrafficWorkloadPatch(BaseModel):
    offered_rate_mbps: float | None = Field(default=None, gt=0)
    packet_size_bytes: int | None = Field(default=None, ge=64, le=9000)
    burst_size_bytes: int | None = Field(default=None, ge=1)
    burst_interval_seconds: float | None = Field(default=None, gt=0)
    parallel_flows: int | None = Field(default=None, ge=1, le=128)
    start_time: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=1)
    recurrence: dict[str, Any] | None = None
    priority: int | None = Field(default=None, ge=0, le=65535)
    recovery_policy: RecoveryPolicy | None = None
    labels: dict[str, str] | None = None


class TrafficWorkload(BaseModel):
    workload_id: str
    service_id: str
    src_endpoint: str
    dst_endpoint: str
    selector: WorkloadSelector
    traffic_pattern: WorkloadPattern
    traffic_role: TrafficRole
    offered_rate_mbps: float | None = None
    packet_size_bytes: int
    burst_size_bytes: int | None = None
    burst_interval_seconds: float | None = None
    parallel_flows: int
    start_mode: WorkloadStartMode
    start_time: datetime | None = None
    duration_seconds: int | None = None
    recurrence: dict[str, Any] | None = None
    priority: int
    tenant_id: str
    application_id: str | None = None
    recovery_policy: RecoveryPolicy
    lifecycle_state: WorkloadLifecycle
    desired_state: WorkloadLifecycle
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    process_refs: list[dict[str, Any]] = Field(default_factory=list)
    statistics: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    last_error: str | None = None
    version: int = 1

    @classmethod
    def from_create(cls, request: TrafficWorkloadCreate) -> "TrafficWorkload":
        now = utc_now()
        scheduled = request.start_mode == WorkloadStartMode.SCHEDULED
        initial = WorkloadLifecycle.SCHEDULED if scheduled else WorkloadLifecycle.CREATED
        return cls(
            **request.model_dump(), lifecycle_state=initial,
            desired_state=WorkloadLifecycle.RUNNING if request.start_mode == WorkloadStartMode.IMMEDIATE else initial,
            created_at=now, updated_at=now,
        )


ALLOWED_WORKLOAD_TRANSITIONS: dict[WorkloadLifecycle, set[WorkloadLifecycle]] = {
    WorkloadLifecycle.CREATED: {WorkloadLifecycle.SCHEDULED, WorkloadLifecycle.STARTING, WorkloadLifecycle.STOPPED, WorkloadLifecycle.FAILED},
    WorkloadLifecycle.SCHEDULED: {WorkloadLifecycle.STARTING, WorkloadLifecycle.STOPPED, WorkloadLifecycle.FAILED},
    WorkloadLifecycle.STARTING: {WorkloadLifecycle.RUNNING, WorkloadLifecycle.FAILED, WorkloadLifecycle.STOPPING},
    WorkloadLifecycle.RUNNING: {WorkloadLifecycle.DEGRADED, WorkloadLifecycle.STOPPING, WorkloadLifecycle.STOPPED, WorkloadLifecycle.FAILED},
    WorkloadLifecycle.DEGRADED: {WorkloadLifecycle.RUNNING, WorkloadLifecycle.STOPPING, WorkloadLifecycle.FAILED},
    WorkloadLifecycle.STOPPING: {WorkloadLifecycle.STOPPED, WorkloadLifecycle.FAILED},
    WorkloadLifecycle.STOPPED: {WorkloadLifecycle.SCHEDULED, WorkloadLifecycle.STARTING},
    WorkloadLifecycle.FAILED: {WorkloadLifecycle.STARTING, WorkloadLifecycle.STOPPED},
}


def ensure_workload_transition(current: WorkloadLifecycle, target: WorkloadLifecycle) -> None:
    if current == target:
        return
    if target not in ALLOWED_WORKLOAD_TRANSITIONS[current]:
        raise ValueError(f"invalid workload transition {current.value} -> {target.value}")

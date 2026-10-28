"""Persistent fault definitions and lifecycle contracts."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class FaultType(str, Enum):
    LINK_DOWN = "LINK_DOWN"
    LINK_DELAY = "LINK_DELAY"
    LINK_LOSS = "LINK_LOSS"
    LINK_RATE_LIMIT = "LINK_RATE_LIMIT"
    DEVICE_DOWN = "DEVICE_DOWN"
    SERVICE_OVERLOAD = "SERVICE_OVERLOAD"


class FaultLifecycle(str, Enum):
    PLANNED = "PLANNED"
    APPLYING = "APPLYING"
    ACTIVE = "ACTIVE"
    REMOVING = "REMOVING"
    REMOVED = "REMOVED"
    FAILED = "FAILED"
    RESIDUAL = "RESIDUAL"


class PathReversionPolicy(str, Enum):
    PREEMPTIVE = "PREEMPTIVE"
    NON_PREEMPTIVE = "NON_PREEMPTIVE"
    MANUAL = "MANUAL"


class FaultDefinition(BaseModel):
    fault_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    fault_type: FaultType
    target_resource_id: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    reversion_policy: PathReversionPolicy = PathReversionPolicy.NON_PREEMPTIVE
    actor: str = "operator"
    reason: str | None = None
    duration_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_parameters(self):
        required = {
            FaultType.LINK_DELAY: "delay_ms",
            FaultType.LINK_LOSS: "loss_pct",
            FaultType.LINK_RATE_LIMIT: "rate_mbps",
        }
        key = required.get(self.fault_type)
        if key and float(self.parameters.get(key, 0)) <= 0:
            raise ValueError(f"{self.fault_type.value} requires positive {key}")
        if self.fault_type == FaultType.LINK_LOSS and float(self.parameters["loss_pct"]) > 100:
            raise ValueError("loss_pct cannot exceed 100")
        return self


class FaultRecord(FaultDefinition):
    lifecycle_state: FaultLifecycle
    desired_state: FaultLifecycle
    observed: dict[str, Any] = Field(default_factory=dict)
    applied_at: str | None = None
    removed_at: str | None = None
    created_at: str
    updated_at: str
    last_error: str | None = None


class PathConstraints(BaseModel):
    min_bandwidth_mbps: float | None = Field(default=None, gt=0)
    max_latency_ms: float | None = Field(default=None, gt=0)
    avoid_devices: set[str] = Field(default_factory=set)
    avoid_links: set[str] = Field(default_factory=set)
    required_devices: list[str] = Field(default_factory=list)
    tenant_id: str | None = None
    service_affinity: list[str] = Field(default_factory=list)


class ConstrainedPath(BaseModel):
    source: str
    destination: str
    nodes: list[str]
    link_ids: list[str]
    total_cost: float
    estimated_latency_ms: float
    bottleneck_available_mbps: float | None = None
    score_components: dict[str, float] = Field(default_factory=dict)

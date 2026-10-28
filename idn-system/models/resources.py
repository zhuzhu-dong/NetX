"""Canonical network resource and capacity-ledger contracts."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class OperationalState(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


class EndpointResource(BaseModel):
    resource_id: str
    host_name: str
    ip_address: str
    ip_prefix_length: int
    mac_address: str
    subnet_name: str
    gateway_ip: str
    gateway_mac: str
    namespace: str
    attached_device: str
    attached_port: int


class DeviceResource(BaseModel):
    resource_id: str
    logical_device_id: str
    switch_name: str
    p4runtime_endpoint: str
    # Every switch runs as a separate Stratum process. The logical ONOS ID
    # varies (device:bmv2:N), while the node inside each gRPC endpoint is 1.
    p4runtime_node_id: int = 1
    node_id: int
    role: str
    pipeline: str
    ports: list[str] = Field(default_factory=list)


class InterfaceResource(BaseModel):
    resource_id: str
    device_id: str
    peer_resource_id: str
    linux_interface: str
    onos_port: int
    p4_port: int
    capacity_mbps: float
    admin_state: OperationalState = OperationalState.UP
    operational_state: OperationalState = OperationalState.UNKNOWN


class LinkResource(BaseModel):
    resource_id: str
    physical_link_id: str
    src_interface_id: str
    dst_interface_id: str
    directed_link_ids: list[str]
    capacity_mbps: float
    delay_ms: float = 0.0
    loss_pct: float = 0.0
    cost: float = 1.0
    role: str = "default"


class ResourceRegistrySnapshot(BaseModel):
    schema_version: str
    topology_name: str
    endpoints: list[EndpointResource]
    devices: list[DeviceResource]
    interfaces: list[InterfaceResource]
    links: list[LinkResource]
    aliases: dict[str, str]


class ResourceBinding(BaseModel):
    binding_id: str
    policy_id: str
    service_id: str | None = None
    traffic_scope_key: str
    resource_key: str
    device_id: str | None = None
    interface_id: str | None = None
    class_id: str
    filter_id: int
    handle: int
    dscp: int | None = None
    rate_mbps: float
    ceil_mbps: float
    reservation_kind: str = "GUARANTEED"
    tenant_id: str = "default"
    created_at: str
    active: bool = True
    runtime: dict[str, Any] = Field(default_factory=dict)


class ReservationKind(str, Enum):
    GUARANTEED = "GUARANTEED"
    RATE_LIMIT = "RATE_LIMIT"
    BEST_EFFORT = "BEST_EFFORT"


class LinkLedgerEntry(BaseModel):
    resource_id: str
    physical_capacity_mbps: float
    measured_load_mbps: float | None = None
    guaranteed_reservation_mbps: float
    rate_limit_commitment_mbps: float
    best_effort_load_mbps: float | None = None
    safety_margin_mbps: float
    available_guaranteed_mbps: float
    utilization_pct: float | None = None
    per_tenant_reservation_mbps: dict[str, float] = Field(default_factory=dict)
    policy_ids: list[str] = Field(default_factory=list)
    quality_status: str = "NO_SAMPLE"
    # The physical topology remains authoritative for link visualization.  The
    # fields below describe a separately calibrated software dataplane limit.
    stable_capacity_mbps: float | None = None
    schedulable_capacity_mbps: float | None = None
    # A durable ingress admission plan can impose a stricter whole-Mbit
    # scheduler ceiling than the fractional calibration evidence.
    authorized_schedulable_capacity_mbps: float | None = None
    committed_guaranteed_capacity_mbps: float = 0.0
    remaining_guaranteed_capacity_mbps: float | None = None
    remaining_best_effort_capacity_mbps: float | None = None
    capacity_record_id: str | None = None
    capacity_confidence: str | None = None
    capacity_freshness: str = "UNKNOWN"


class StableDataplaneCapacityRecord(BaseModel):
    """A conservative, epoch-scoped software dataplane capacity observation."""
    capacity_record_id: str
    topology_id: str
    environment_epoch: str
    controller_epoch: str
    path_signature: str
    bottleneck_egress: str
    packet_profile: str
    protocol: str
    packet_size_bytes: int = Field(gt=0)
    observation_mode: str
    physical_capacity_mbps: float = Field(gt=0)
    stable_capacity_mbps: float = Field(gt=0)
    capacity_floor_mbps: float = Field(gt=0)
    p05_capacity_mbps: float = Field(gt=0)
    sample_count: int = Field(gt=0)
    confidence: str
    measured_at: str
    valid_until: str
    source_experiment: str
    status: str = "ACTIVE"
    invalidation_reason: str | None = None
    safety_margin_mbps: float = Field(default=0.0, ge=0)


class IngressAdmissionPlan(BaseModel):
    admission_plan_id: str
    intent_id: str
    revision: int
    policy_id: str
    policy_generation: int
    topology_id: str
    environment_epoch: str
    capacity_record_id: str
    path_signature: str
    access_device_id: str
    protected_scopes: list[dict[str, Any]] = Field(default_factory=list)
    best_effort_scopes: list[dict[str, Any]] = Field(default_factory=list)
    protected_attachments: list[dict[str, Any]] = Field(default_factory=list)
    best_effort_attachments: list[dict[str, Any]] = Field(default_factory=list)
    schedulable_capacity_mbps: float
    implementation_reserved_mbps: float
    best_effort_residual_mbps: float
    safety_margin_mbps: float
    activation_reason: str
    mode: str
    created_at: str
    expires_at: str
    status: str
    requested_min_bandwidth_mbps: float = 0.0
    implementation_guaranteed_floor_mbps: float = 0.0
    ingress_admission_ceiling_mbps: float = 0.0
    observed_active_demand_mbps: float = 0.0
    borrowable_capacity_mbps: float = 0.0
    borrowed_capacity_mbps: float = 0.0
    active_be_aggregate_budget_mbps: float = 0.0
    active_be_allocations: list[dict[str, Any]] = Field(default_factory=list)
    inactive_scopes: list[dict[str, Any]] = Field(default_factory=list)
    expected_aggregate_ceiling_mbps: float = 0.0
    allocation_epoch: int = 0
    activity_snapshot_id: str | None = None
    activity_snapshot_freshness: str = "UNKNOWN"


class AdmissionDecision(str, Enum):
    ACCEPT = "ACCEPT"
    ACCEPT_WITH_ADJUSTMENT = "ACCEPT_WITH_ADJUSTMENT"
    REJECT = "REJECT"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"


class AdmissionResult(BaseModel):
    decision: AdmissionDecision
    requested_mbps: float
    available_mbps: float
    resource_id: str
    reason: str
    alternative_resource_ids: list[str] = Field(default_factory=list)

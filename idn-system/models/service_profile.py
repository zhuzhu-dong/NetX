"""Typed service-validation contracts shared by intent, probe and API layers."""
from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class ServiceProtocol(str, Enum):
    ICMP = "ICMP"
    TCP = "TCP"
    UDP = "UDP"


class TrafficPattern(str, Enum):
    CONSTANT_BIT_RATE = "CONSTANT_BIT_RATE"
    BURST = "BURST"
    BULK_TRANSFER = "BULK_TRANSFER"
    PERIODIC = "PERIODIC"
    BEST_EFFORT = "BEST_EFFORT"


class MeasurementStatus(str, Enum):
    MEASURED = "MEASURED"
    NO_TRAFFIC = "NO_TRAFFIC"
    NO_SAMPLE = "NO_SAMPLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PROBE_FAILED = "PROBE_FAILED"
    TELEMETRY_UNAVAILABLE = "TELEMETRY_UNAVAILABLE"
    PROFILE_MISMATCH = "PROFILE_MISMATCH"


class EvidenceStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class KpiStatus(str, Enum):
    VERIFIED = "VERIFIED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    NOT_EVALUATED = "NOT_EVALUATED"


class ServiceProfile(BaseModel):
    service_id: str
    service_name: str
    display_name: str | None = None
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    semantic_tags: list[str] = Field(default_factory=list)
    src_applicability: dict[str, Any] = Field(default_factory=dict)
    dst_applicability: dict[str, Any] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    src_host: str
    dst_host: str
    src_ip: str
    dst_ip: str
    protocol: ServiceProtocol
    src_port: int | None = Field(default=None, ge=1, le=65535)
    dst_port: int | None = Field(default=None, ge=1, le=65535)
    src_port_range: str | None = None
    dst_port_range: str | None = None
    dscp: int | None = Field(default=None, ge=0, le=63)
    vlan_id: int | None = Field(default=None, ge=1, le=4094)
    tenant_id: str = "default"
    direction: str = "ingress"
    traffic_pattern: TrafficPattern
    default_offered_rate_mbps: float | None = Field(default=None, gt=0)
    default_duration_seconds: int = Field(default=5, ge=1, le=120)
    packet_size_bytes: int = Field(default=1200, ge=64, le=9000)
    parallel_streams: int = Field(default=1, ge=1, le=16)
    validation_metrics: list[str] = Field(default_factory=list)
    validation_thresholds: dict[str, Any] = Field(default_factory=dict)
    background_profile_ids: list[str] = Field(default_factory=list)
    p4_counter_binding: dict[str, Any] = Field(default_factory=dict)
    qos_binding: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @property
    def effective_semantic_tags(self) -> list[str]:
        return list(dict.fromkeys([*self.semantic_tags, *self.tags]))

    @model_validator(mode="after")
    def validate_protocol_fields(self) -> "ServiceProfile":
        if not self.display_name:
            self.display_name = self.service_name
        if not self.src_applicability:
            self.src_applicability = {"hosts": [self.src_host], "prefixes": [self.src_ip]}
        if not self.dst_applicability:
            self.dst_applicability = {"hosts": [self.dst_host], "prefixes": [self.dst_ip]}
        for direction, applicability in (
            ("src", self.src_applicability),
            ("dst", self.dst_applicability),
        ):
            mode = str(applicability.get("mode") or "ALLOW_LIST").upper()
            if mode not in {"ALLOW_LIST", "ANY_REGISTERED_HOST", "PREFIX_SET"}:
                raise ValueError(
                    f"{direction}_applicability.mode must be ALLOW_LIST, "
                    "ANY_REGISTERED_HOST, or PREFIX_SET"
                )
            applicability["mode"] = mode
        if not self.defaults:
            self.defaults = {
                "protocol": self.protocol.value,
                "destination_port": self.dst_port,
                "dscp": self.dscp,
            }
        if self.protocol == ServiceProtocol.ICMP and any(
            value is not None
            for value in (self.src_port, self.dst_port, self.src_port_range, self.dst_port_range)
        ):
            raise ValueError("ICMP service profiles cannot define transport ports")
        if self.protocol in {ServiceProtocol.TCP, ServiceProtocol.UDP} and self.dst_port is None:
            raise ValueError(f"{self.protocol.value} service profile requires dst_port")
        return self

    @staticmethod
    def _prefix_allowed(value: str | None, prefixes: list[Any]) -> bool:
        if not value:
            return False
        try:
            candidate = ipaddress.ip_network(str(value), strict=False)
            return any(
                candidate.subnet_of(ipaddress.ip_network(str(prefix), strict=False))
                for prefix in prefixes
            )
        except ValueError:
            return False

    @staticmethod
    def _applicability_allows(
        applicability: dict[str, Any],
        *,
        endpoint_id: str | None,
        selector_prefix: str | None,
    ) -> bool:
        mode = str(applicability.get("mode") or "ALLOW_LIST").upper()
        if mode == "ANY_REGISTERED_HOST":
            # The caller must obtain this identity from EndpointResolver.  A
            # raw IP string is not evidence that a user supplied a registered
            # endpoint, and must never make a selector executable by itself.
            return bool(str(endpoint_id or "").strip())
        hosts = {str(item).casefold() for item in applicability.get("hosts") or []}
        if endpoint_id and str(endpoint_id).casefold() in hosts:
            return True
        prefixes = list(applicability.get("prefixes") or [])
        return ServiceProfile._prefix_allowed(selector_prefix, prefixes)

    def allows_source(
        self, *, endpoint_id: str | None, selector_prefix: str | None
    ) -> bool:
        return self._applicability_allows(
            self.src_applicability,
            endpoint_id=endpoint_id,
            selector_prefix=selector_prefix,
        )

    def allows_destination(
        self, *, endpoint_id: str | None, selector_prefix: str | None
    ) -> bool:
        return self._applicability_allows(
            self.dst_applicability,
            endpoint_id=endpoint_id,
            selector_prefix=selector_prefix,
        )

    def selector(self) -> dict[str, Any]:
        return {
            "src_host": self.src_host,
            "dst_host": self.dst_host,
            "source_ip": self.src_ip,
            "destination_ip": self.dst_ip,
            "protocol": self.protocol.value,
            "source_port": self.src_port,
            "destination_port": self.dst_port,
            "source_port_range": self.src_port_range,
            "destination_port_range": self.dst_port_range,
            "dscp": self.dscp,
            "vlan_id": self.vlan_id,
            "tenant_id": self.tenant_id,
            "direction": self.direction,
        }


class BackgroundFlow(BaseModel):
    service_profile_id: str
    src_host: str | None = None
    dst_host: str | None = None
    protocol: ServiceProtocol | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    offered_rate_mbps: float | None = Field(default=None, gt=0)
    start_offset_seconds: float = Field(default=0.0, ge=0, le=30)
    duration_seconds: int | None = Field(default=None, ge=1, le=120)
    priority: int = Field(default=1000, ge=1, le=65535)


class TrafficScenario(BaseModel):
    scenario_id: str
    name: str
    target_service_id: str
    background_flows: list[BackgroundFlow] = Field(default_factory=list)
    bottleneck_link: str | None = None
    duration_seconds: int = Field(default=10, ge=1, le=120)
    cleanup_policy: Literal["ALWAYS", "ON_SUCCESS", "MANUAL"] = "ALWAYS"
    validation_rules: dict[str, Any] = Field(default_factory=dict)


class ServiceValidationRequest(BaseModel):
    service_id: str
    src_host: str | None = None
    dst_host: str | None = None
    src_ip: str | None = None
    dst_ip: str | None = None
    protocol: ServiceProtocol | None = None
    src_port: int | None = Field(default=None, ge=1, le=65535)
    dst_port: int | None = Field(default=None, ge=1, le=65535)
    dscp: int | None = Field(default=None, ge=0, le=63)
    offered_rate_mbps: float | None = Field(default=None, gt=0)
    duration_seconds: int | None = Field(default=None, ge=1, le=120)
    packet_size_bytes: int | None = Field(default=None, ge=64, le=9000)
    parallel_streams: int | None = Field(default=None, ge=1, le=16)
    background_profiles: list[str] = Field(default_factory=list)
    intent_id: str | None = None
    policy_id: str | None = None
    traffic_scope_key: str | None = None
    qos_mode: str | None = None
    validation_profile_id: str | None = None


class ServiceProbeResult(BaseModel):
    probe_id: str = Field(default_factory=lambda: f"SP-{uuid4().hex[:12]}")
    service_id: str
    intent_id: str | None = None
    policy_id: str | None = None
    protocol: ServiceProtocol
    selector: dict[str, Any]
    start_time: str
    end_time: str
    duration_seconds: float
    status: MeasurementStatus
    measurement_source: str
    offered_load: dict[str, Any] = Field(default_factory=dict)
    received_load: dict[str, Any] = Field(default_factory=dict)
    protocol_metrics: dict[str, Any] = Field(default_factory=dict)
    background_results: list[dict[str, Any]] = Field(default_factory=list)
    p4_counter_evidence: dict[str, Any] = Field(default_factory=dict)
    tc_evidence: dict[str, Any] = Field(default_factory=dict)
    path_evidence: dict[str, Any] = Field(default_factory=dict)
    validation_result: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any] | str] = Field(default_factory=list)

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def normalize_time(cls, value: Any) -> str:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        return str(value)

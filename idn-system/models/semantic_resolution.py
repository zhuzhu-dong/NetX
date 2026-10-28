"""Typed contracts for deterministic endpoint and service resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


class FieldProvenance(str, Enum):
    USER_EXPLICIT = "USER_EXPLICIT"
    USER_EXPLICIT_UI = "USER_EXPLICIT_UI"
    # An LLM can propose a value, but that proposal is not equivalent to a
    # user-supplied selector constraint.  Keeping these two cases distinct is
    # essential when a ServiceProfile owns the canonical traffic class.
    LLM_INFERRED = "LLM_INFERRED"
    LLM_DEFAULT = "LLM_DEFAULT"
    SEMANTIC_RESOLUTION = "SEMANTIC_RESOLUTION"
    SERVICE_PROFILE = "SERVICE_PROFILE"
    ENDPOINT_REGISTRY = "ENDPOINT_REGISTRY"
    SYSTEM_DEFAULT = "SYSTEM_DEFAULT"


class ClarificationOption(BaseModel):
    value: str
    label: str
    description: str = ""


class ClarificationQuestion(BaseModel):
    question_id: str = Field(default_factory=lambda: f"CQ-{uuid4().hex[:12]}")
    intent_id: str | None = None
    field: str
    reason: str
    question: str
    options: list[ClarificationOption] = Field(default_factory=list)
    required: bool = True
    candidate_values: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class EndpointResolution(BaseModel):
    status: ResolutionStatus
    endpoint_id: str | None = None
    hostname: str | None = None
    host_ip: str | None = None
    interface_network: str | None = None
    selector_prefix: str | None = None
    mac: str | None = None
    attached_device: str | None = None
    attached_port: int | None = None
    matched_reference: str | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    provenance: dict[str, str] = Field(default_factory=dict)


class EndpointPairResolution(BaseModel):
    source: EndpointResolution
    destination: EndpointResolution
    unresolved_fields: list[str] = Field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return not self.unresolved_fields


class ServiceCandidate(BaseModel):
    service_id: str
    display_name: str
    confidence: float = Field(ge=0, le=1)
    match_reason: list[str] = Field(default_factory=list)
    conflicting_fields: list[str] = Field(default_factory=list)


class ServiceResolution(BaseModel):
    status: ResolutionStatus
    selected_service_id: str | None = None
    candidate_services: list[ServiceCandidate] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    matched_semantics: list[str] = Field(default_factory=list)
    explicit_fields: dict[str, Any] = Field(default_factory=dict)
    inferred_fields: dict[str, Any] = Field(default_factory=dict)
    field_provenance: dict[str, str] = Field(default_factory=dict)
    ambiguity_reason: str | None = None
    clarification_options: list[ClarificationOption] = Field(default_factory=list)

    def clarification_question(self, *, intent_id: str | None = None) -> ClarificationQuestion:
        candidates = [item.service_id for item in self.candidate_services]
        if self.status == ResolutionStatus.AMBIGUOUS:
            labels = "、".join(option.label for option in self.clarification_options)
            question = f"检测到多个符合该业务描述的服务，请选择要保障的业务：{labels}。"
            reason = "MULTIPLE_SERVICE_CANDIDATES"
        elif any(item.conflicting_fields for item in self.candidate_services):
            candidate = (
                self.candidate_services[0]
                if len(self.candidate_services) == 1
                else None
            )
            service_label = candidate.display_name if candidate else "该业务"
            question = (
                f"已识别为{service_label}，但当前源/目的端点不在该业务配置的适用范围内。"
                "请确认端点，或选择其他业务类型。"
            )
            reason = "SERVICE_SELECTOR_MISMATCH"
        else:
            question = "请说明希望保障的业务类型，例如科研视频、AI 数据或备份业务。"
            reason = "SERVICE_NOT_IDENTIFIED"
        return ClarificationQuestion(
            intent_id=intent_id,
            field="service",
            reason=reason,
            question=question,
            options=self.clarification_options,
            candidate_values=candidates,
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)

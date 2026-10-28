"""Empirical capability evidence learned only from trusted observations."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from models.intent_domain import new_id
from pydantic import BaseModel, Field


class CapabilityEvidenceStatus(str, Enum):
    PROVEN = "PROVEN"
    STALE = "STALE"
    UNTRUSTED = "UNTRUSTED"


class EmpiricalCapability(BaseModel):
    capability_id: str = Field(default_factory=lambda: new_id("CAP"))
    resource_id: str
    metric: str
    value: float
    unit: str | None = None
    epoch: str
    status: CapabilityEvidenceStatus = CapabilityEvidenceStatus.PROVEN
    evidence_refs: list[str] = Field(default_factory=list)
    validation_run_id: str | None = None
    assurance_id: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    boot_id: str

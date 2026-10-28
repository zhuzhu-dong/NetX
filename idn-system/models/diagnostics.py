"""Packet-accounting and runtime performance diagnostic contracts."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AccountingPoint(BaseModel):
    point_id: str
    node: str | None = None
    interface: str | None = None
    direction: str | None = None
    counter_before: int | None = None
    counter_after: int | None = None
    delta: int | None = None
    quality_status: str
    source: str


class PacketLossSegment(BaseModel):
    segment_id: str
    source_point: str
    destination_point: str
    packets_before: int | None = None
    packets_after: int | None = None
    packet_difference: int | None = None
    loss_pct: float | None = None
    status: str
    reason: str | None = None


class PacketAccountingResult(BaseModel):
    accounting_id: str
    workload_id: str
    service_id: str
    measurement_window_seconds: float
    started_at: str
    finished_at: str
    points: list[AccountingPoint] = Field(default_factory=list)
    segments: list[PacketLossSegment] = Field(default_factory=list)
    environment: dict[str, Any] = Field(default_factory=dict)

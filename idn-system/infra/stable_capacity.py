"""Epoch-scoped, conservative software dataplane capacity records.

The model deliberately keeps a link's physical capacity separate from the
capacity that a software forwarding path has demonstrated it can schedule
reliably under the recorded packet profile.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

from api.store import IntentStore, get_store
from models.resources import StableDataplaneCapacityRecord


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def conservative_capacity_estimate(samples: list[float]) -> dict[str, Any]:
    """Return a non-optimistic lower bound; never use mean or best sample."""
    valid = sorted(float(value) for value in samples if float(value) > 0)
    if not valid:
        raise ValueError("at least one positive delivered-capacity sample is required")
    # Linear P05 is reported for diagnostics, while the minimum is the actual
    # floor for a small calibration set.  This prevents a sparse sample set
    # from claiming a stronger guarantee than it has observed.
    position = 0.05 * (len(valid) - 1)
    lower, upper = math.floor(position), math.ceil(position)
    p05 = valid[lower] + (valid[upper] - valid[lower]) * (position - lower)
    confidence = "CALIBRATED_MEDIUM" if len(valid) >= 20 else "CALIBRATED_LOW"
    return {
        "estimate_mbps": min(valid), "lower_bound_mbps": min(valid),
        "p05_capacity_mbps": p05, "sample_count": len(valid),
        "confidence": confidence,
    }


def stable_environment_epoch(*, topology_id: str, runtime_fingerprint: dict[str, Any]) -> str:
    """Stable hash includes the runtime factors that invalidate calibration."""
    material = json.dumps(
        {"topology_id": topology_id, "runtime": runtime_fingerprint},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return "ENV-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class StableCapacityRepository:
    def __init__(self, store: IntentStore | None = None):
        self.store = store or get_store()

    def put(self, record: StableDataplaneCapacityRecord) -> StableDataplaneCapacityRecord:
        if record.stable_capacity_mbps > record.physical_capacity_mbps:
            raise ValueError("stable capacity cannot exceed physical capacity")
        if record.capacity_floor_mbps > record.stable_capacity_mbps:
            raise ValueError("capacity floor cannot exceed stable capacity")
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                """INSERT INTO stable_dataplane_capacity_records(
                    capacity_record_id, bottleneck_egress, topology_id, environment_epoch,
                    controller_epoch, path_signature, record_json, status, valid_until,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(capacity_record_id) DO UPDATE SET
                    record_json=excluded.record_json, status=excluded.status,
                    valid_until=excluded.valid_until, updated_at=excluded.updated_at""",
                (record.capacity_record_id, record.bottleneck_egress, record.topology_id,
                 record.environment_epoch, record.controller_epoch, record.path_signature,
                 record.model_dump_json(), record.status, record.valid_until,
                 datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
            )
        return record

    def list(self, bottleneck_egress: str | None = None) -> list[StableDataplaneCapacityRecord]:
        query = "SELECT record_json FROM stable_dataplane_capacity_records"
        params: tuple[Any, ...] = ()
        if bottleneck_egress:
            query += " WHERE bottleneck_egress=?"
            params = (bottleneck_egress,)
        query += " ORDER BY updated_at DESC"
        with self.store._lock, self.store._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [StableDataplaneCapacityRecord.model_validate_json(row["record_json"]) for row in rows]

    def active_for(self, *, bottleneck_egress: str, topology_id: str) -> StableDataplaneCapacityRecord | None:
        now = datetime.now(timezone.utc)
        for record in self.list(bottleneck_egress):
            if record.status != "ACTIVE" or record.topology_id != topology_id:
                continue
            try:
                fresh = _parse_time(record.valid_until) > now
            except ValueError:
                fresh = False
            if fresh:
                return record
        return None

    @staticmethod
    def schedulable_capacity(record: StableDataplaneCapacityRecord) -> float:
        return max(0.0, min(record.physical_capacity_mbps, record.stable_capacity_mbps) - record.safety_margin_mbps)

    @staticmethod
    def htb_schedulable_capacity(record: StableDataplaneCapacityRecord) -> int:
        """Conservative HTB capacity in the actuator's whole-Mbit unit.

        Calibration remains a fractional measurement.  The switch egress
        actuator emits ``tc ... mbit`` values and deliberately floors that
        measurement, so desired/readback comparisons must use the same
        operational unit rather than treating a 0.088 Mbps representation
        difference as data-plane drift.
        """
        return max(1, int(math.floor(StableCapacityRepository.schedulable_capacity(record))))

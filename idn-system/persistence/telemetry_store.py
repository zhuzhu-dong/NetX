"""SQLite time-series storage with counter-delta correctness and retention."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from api.store import IntentStore, get_store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TelemetryStore:
    def __init__(self, store: IntentStore | None = None):
        self.store = store or get_store()
        self.max_samples_per_resource = max(
            int(os.getenv("IDN_TELEMETRY_RETENTION_SAMPLES", "5000")), 10
        )

    @staticmethod
    def _loads(value: str | None, default):
        return json.loads(value) if value else default

    def record_interface_batch(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        results = []
        with self.store._lock, self.store._connect() as connection:
            for raw in payload.get("samples") or []:
                resource_id = str(raw["resource_id"])
                sampled_at = str(raw.get("timestamp") or payload.get("timestamp") or _now())
                after = {key: int(value or 0) for key, value in (raw.get("counters") or {}).items()}
                previous = connection.execute(
                    """SELECT sampled_at, counter_after_json FROM interface_metrics
                    WHERE resource_id=? ORDER BY sampled_at DESC LIMIT 1""",
                    (resource_id,),
                ).fetchone()
                before = self._loads(previous["counter_after_json"], {}) if previous else None
                interval = None
                delta = None
                rate = None
                utilization = None
                quality = "NO_PREVIOUS_SAMPLE"
                if previous:
                    start = datetime.fromisoformat(previous["sampled_at"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(sampled_at.replace("Z", "+00:00"))
                    interval = (end - start).total_seconds()
                    if interval <= 0:
                        quality = "INVALID_INTERVAL"
                    else:
                        delta = {key: after.get(key, 0) - int(before.get(key, 0)) for key in after}
                        if any(value < 0 for value in delta.values()):
                            quality = "COUNTER_RESET"
                            delta = None
                        else:
                            rate = float(delta.get("tx_bytes", 0)) * 8.0 / interval / 1_000_000.0
                            capacity = raw.get("capacity_mbps")
                            utilization = rate / float(capacity) * 100.0 if capacity else None
                            quality = "MEASURED"
                sample_id = f"IFM-{uuid.uuid4().hex[:16]}"
                queue = dict(raw.get("queue") or {})
                connection.execute(
                    """INSERT INTO interface_metrics(
                        sample_id, resource_id, sampled_at, interval_seconds,
                        counter_before_json, counter_after_json, delta_json,
                        calculated_rate_mbps, utilization_pct, queue_json, quality_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (sample_id, resource_id, sampled_at, interval,
                     json.dumps(before) if before is not None else None, json.dumps(after),
                     json.dumps(delta) if delta is not None else None, rate, utilization,
                     json.dumps(queue), quality),
                )
                item = {
                    **raw, "sample_id": sample_id, "interval_seconds": interval,
                    "counter_before": before, "counter_after": after, "delta": delta,
                    "calculated_rate_mbps": rate, "utilization_pct": utilization,
                    "quality_status": quality,
                }
                results.append(item)
            for resource_id in {item["resource_id"] for item in results}:
                connection.execute(
                    """DELETE FROM interface_metrics WHERE sample_id IN (
                        SELECT sample_id FROM interface_metrics WHERE resource_id=?
                        ORDER BY sampled_at DESC LIMIT -1 OFFSET ?
                    )""",
                    (resource_id, self.max_samples_per_resource),
                )
        return results

    def interface_history(self, resource_id: str, limit: int = 300) -> list[dict[str, Any]]:
        with self.store._lock, self.store._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM interface_metrics WHERE resource_id=?
                ORDER BY sampled_at DESC LIMIT ?""",
                (resource_id, max(1, min(int(limit), 5000))),
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            for column in ("counter_before_json", "counter_after_json", "delta_json", "queue_json"):
                item[column.removesuffix("_json")] = self._loads(item.pop(column), None)
            result.append(item)
        return result

    def record_service_metric(
        self, payload: dict[str, Any], *, traffic_role: str = "VALIDATION",
        workload_id: str | None = None,
    ) -> dict[str, Any]:
        service_id = str(payload["service_id"])
        sampled_at = str(payload.get("end_time") or payload.get("timestamp") or _now())
        sample_id = f"SVM-{uuid.uuid4().hex[:16]}"
        validation_run_id = payload.get("validation_run_id")
        quality = str(payload.get("measurement_status") or payload.get("status") or "NO_SAMPLE")
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                """INSERT INTO service_metrics(
                    sample_id, service_id, traffic_role, workload_id, validation_run_id,
                    payload_json, quality_status, sampled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (sample_id, service_id, traffic_role, workload_id, validation_run_id,
                 json.dumps(payload, default=str), quality, sampled_at),
            )
        return {"sample_id": sample_id, "service_id": service_id, "sampled_at": sampled_at,
                "quality_status": quality}

    def service_history(
        self, service_id: str, *, traffic_role: str | None = None, limit: int = 300,
    ) -> list[dict[str, Any]]:
        clause, params = "", [service_id]
        if traffic_role:
            clause = " AND traffic_role=?"
            params.append(traffic_role)
        params.append(max(1, min(int(limit), 5000)))
        with self.store._lock, self.store._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM service_metrics WHERE service_id=?{clause}
                ORDER BY sampled_at DESC LIMIT ?""", params,
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["payload"] = self._loads(item.pop("payload_json"), {})
            result.append(item)
        return result

    def record_workload_metric(self, workload: dict[str, Any]) -> None:
        sampled_at = _now()
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                """INSERT INTO workload_metrics(
                    sample_id, workload_id, service_id, traffic_role, payload_json,
                    quality_status, sampled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (f"WLM-{uuid.uuid4().hex[:16]}", workload["workload_id"],
                 workload["service_id"], workload["traffic_role"],
                 json.dumps(workload, default=str),
                 "MEASURED" if workload.get("lifecycle_state") == "RUNNING" else "NO_TRAFFIC",
                 sampled_at),
            )

    def workload_history(self, workload_id: str, limit: int = 300) -> list[dict[str, Any]]:
        with self.store._lock, self.store._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM workload_metrics WHERE workload_id=?
                ORDER BY sampled_at DESC LIMIT ?""",
                (workload_id, max(1, min(int(limit), 5000))),
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["payload"] = self._loads(item.pop("payload_json"), {})
            result.append(item)
        return result

    def generic_history(
        self, *, sample_type: str | None = None, limit: int = 300,
    ) -> list[dict[str, Any]]:
        where, params = "", []
        if sample_type:
            where = " WHERE sample_type=?"
            params.append(sample_type)
        params.append(max(1, min(int(limit), 5000)))
        with self.store._lock, self.store._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM telemetry_samples{where} ORDER BY sampled_at DESC LIMIT ?", params,
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["payload"] = self._loads(item.pop("payload_json"), {})
            result.append(item)
        return result

    def record_generic(self, sample_type: str, payload: dict[str, Any], quality: str) -> None:
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                """INSERT INTO telemetry_samples(
                    sample_id, sample_type, payload_json, quality_status, sampled_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (f"TEL-{uuid.uuid4().hex[:16]}", sample_type,
                 json.dumps(payload, default=str), quality,
                 str(payload.get("timestamp") or _now())),
            )

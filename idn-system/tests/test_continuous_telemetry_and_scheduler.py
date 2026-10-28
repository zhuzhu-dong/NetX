from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.store import IntentStore
from infra.probe_scheduler import ProbeScheduler
from persistence.telemetry_store import TelemetryStore


def interface_payload(timestamp: str, tx_bytes: int, tx_packets: int = 10) -> dict:
    return {
        "status": "ok", "timestamp": timestamp,
        "samples": [{
            "resource_id": "interface:s_access_r:3",
            "timestamp": timestamp,
            "capacity_mbps": 100,
            "counters": {
                "tx_bytes": tx_bytes, "rx_bytes": 0,
                "tx_packets": tx_packets, "rx_packets": 0,
                "tx_dropped": 0, "rx_dropped": 0,
                "tx_errors": 0, "rx_errors": 0,
            },
            "queue": {"backlog_bytes": 0, "backlog_packets": 0},
        }],
    }


def test_interface_counter_delta_and_rate_are_persisted(tmp_path):
    store = TelemetryStore(IntentStore(tmp_path / "telemetry.db"))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = store.record_interface_batch(interface_payload(start.isoformat(), 1000))[0]
    second = store.record_interface_batch(
        interface_payload((start + timedelta(seconds=2)).isoformat(), 2_501_000, 2010)
    )[0]

    assert first["quality_status"] == "NO_PREVIOUS_SAMPLE"
    assert first["calculated_rate_mbps"] is None
    assert second["delta"]["tx_bytes"] == 2_500_000
    assert second["interval_seconds"] == 2
    assert second["calculated_rate_mbps"] == 10
    assert second["utilization_pct"] == 10
    history = store.interface_history("interface:s_access_r:3")
    assert [item["quality_status"] for item in history] == ["NO_PREVIOUS_SAMPLE", "MEASURED"]


def test_counter_reset_is_not_reported_as_negative_rate(tmp_path):
    store = TelemetryStore(IntentStore(tmp_path / "reset.db"))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.record_interface_batch(interface_payload(start.isoformat(), 9000))
    reset = store.record_interface_batch(
        interface_payload((start + timedelta(seconds=2)).isoformat(), 100)
    )[0]
    assert reset["quality_status"] == "COUNTER_RESET"
    assert reset["delta"] is None
    assert reset["calculated_rate_mbps"] is None


def test_no_sample_does_not_construct_history(tmp_path):
    store = TelemetryStore(IntentStore(tmp_path / "empty.db"))
    assert store.interface_history("interface:missing:1") == []
    assert store.service_history("missing-service") == []
    assert store.generic_history() == []


def test_service_roles_are_filterable_in_persistent_history(tmp_path):
    store = TelemetryStore(IntentStore(tmp_path / "roles.db"))
    store.record_service_metric(
        {"service_id": "svc", "status": "MEASURED", "received_mbps": 20},
        traffic_role="PRODUCTION", workload_id="WL-1",
    )
    store.record_service_metric(
        {"service_id": "svc", "status": "MEASURED", "received_mbps": 5},
        traffic_role="VALIDATION",
    )
    production = store.service_history("svc", traffic_role="PRODUCTION")
    validation = store.service_history("svc", traffic_role="VALIDATION")
    assert production[0]["workload_id"] == "WL-1"
    assert production[0]["payload"]["received_mbps"] == 20
    assert validation[0]["payload"]["received_mbps"] == 5


@pytest.mark.asyncio
async def test_probe_budget_defers_excess_work_without_running_heavy_probe(tmp_path):
    calls = []

    async def light(service_id):
        calls.append(("light", service_id))
        return {"status": "MEASURED"}

    async def heavy(service_id):
        calls.append(("heavy", service_id))
        return {"status": "MEASURED"}

    scheduler = ProbeScheduler(
        store=IntentStore(tmp_path / "budget.db"), light_probe=light,
        heavy_probe=heavy, budget_per_minute=2,
    )
    scheduler.light_cost = 1
    scheduler.heavy_cost = 10
    scheduler._due_normal_light_probes = lambda: None
    for reason in ("a", "b", "c"):
        scheduler.notify("ai_data", reason, heavy=False)
    scheduler.notify("backup_data", "degradation", heavy=True)

    result = await scheduler.run_once()
    assert len(result["executed"]) == 2
    assert len(result["deferred"]) == 2
    assert calls == [("light", "ai_data"), ("light", "ai_data")]
    assert scheduler.status()["pending"]


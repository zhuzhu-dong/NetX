from __future__ import annotations

import inspect

import pytest

from api import telemetry
from api.service_profiles import get_service_profile, infer_service_id


def test_service_resolution_requires_business_semantics_not_demo_endpoints():
    assert infer_service_id("video") is None
    expected = get_service_profile("research_video")
    # Multiple video profiles can legitimately apply to the same registered
    # endpoint pair.  A generic service word must not recover the old
    # research-host demo mapping.
    assert infer_service_id(
        "video service",
        source_ip=expected.src_ip,
        destination_ip=expected.dst_ip,
    ) is None
    assert infer_service_id(
        "research video service",
        source_ip=expected.src_ip,
        destination_ip=expected.dst_ip,
    ) == expected.service_id
    assert infer_service_id("please protect ai_data") == "ai_data"


def test_live_telemetry_has_no_demo_mode_success_branch():
    source = inspect.getsource(telemetry.collect_current_telemetry)
    assert "IDN_DEMO_MODE" not in source
    assert "_collect_telemetry" not in source


@pytest.mark.asyncio
async def test_health_probe_endpoints_come_from_service_profile(monkeypatch):
    calls = []

    class Client:
        async def get_devices(self):
            return {"devices": []}

        async def get_flows(self):
            return {"flows": []}

    async def probe(path, params=None):
        calls.append((path, params or {}))
        if path == "/probe/ping":
            return {"status": "ok", "transmitted": 3, "received": 3}
        return {"status": "ok", "samples": []}

    monkeypatch.setattr(telemetry, "_probe_get", probe)
    monkeypatch.setattr(telemetry, "_collect_p4_direct_counters", lambda _devices: telemetry._noop_probe())
    await telemetry.collect_current_telemetry(Client(), include_probe=True)

    health = next(
        profile for profile in telemetry.list_service_profiles()
        if profile.protocol.value == "ICMP" and "network-health" in profile.tags
    )
    ping = next(params for path, params in calls if path == "/probe/ping")
    assert ping["src"] == health.src_host
    assert ping["dst"] == health.dst_host

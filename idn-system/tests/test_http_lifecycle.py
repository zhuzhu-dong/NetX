from __future__ import annotations

import asyncio

import pytest
import sqlite3
import aiohttp

from api.store import IntentStore

from api import telemetry
from api._probe_session import ProbeSession
from mcp_servers.onos_server import ONOSClient


@pytest.mark.asyncio
async def test_onos_shared_session_closes_connector_on_shutdown():
    client = ONOSClient()
    session = await client._get_session()
    connector = client._connector
    assert session.closed is False
    assert connector is not None and connector.closed is False
    await client.close()
    assert session.closed is True
    assert connector.closed is True
    assert client._session is None
    assert client._connector is None


@pytest.mark.asyncio
async def test_probe_shared_session_closes_connector_on_shutdown(monkeypatch):
    monkeypatch.setenv("MININET_PROBE_URL", "http://127.0.0.1:9")
    probe = ProbeSession()
    session = await probe._get_session()
    connector = probe._connector
    await probe.close()
    assert session.closed is True
    assert connector is not None and connector.closed is True


@pytest.mark.asyncio
async def test_probe_get_reconnects_once_after_runtime_transport_reset(monkeypatch):
    """A restarted Mininet runtime must not leave recovery on a stale GET socket."""
    monkeypatch.setenv("MININET_PROBE_URL", "http://mininet:9001")
    probe = ProbeSession()
    calls = []

    class Response:
        status = 200

        async def json(self, **_kwargs):
            return {"status": "ok"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Session:
        def __init__(self, fail=False):
            self.fail = fail

        def get(self, *_args, **_kwargs):
            calls.append("get")
            if self.fail:
                raise aiohttp.ClientConnectionError("runtime restarted")
            return Response()

    sessions = iter([Session(fail=True), Session()])

    async def get_session():
        return next(sessions)

    async def close():
        calls.append("close")

    monkeypatch.setattr(probe, "_get_session", get_session)
    monkeypatch.setattr(probe, "close", close)

    assert await probe.get("/runtime/identity") == {"status": "ok"}
    assert calls == ["get", "close", "get"]


def test_store_connection_context_closes_sqlite_descriptor(tmp_path):
    store = IntentStore(tmp_path / "connection-lifecycle.db")
    with store._connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


class SlowONOS:
    def __init__(self):
        self.device_calls = 0
        self.flow_calls = 0

    async def get_devices(self):
        self.device_calls += 1
        await asyncio.sleep(0.03)
        return {"devices": [{"id": "of:1", "available": True}]}

    async def get_flows(self):
        self.flow_calls += 1
        await asyncio.sleep(0.03)
        return {"flows": []}


@pytest.mark.asyncio
async def test_concurrent_telemetry_polling_is_single_flight(monkeypatch):
    fake = SlowONOS()
    monkeypatch.setattr("mcp_servers.onos_server.get_onos_client", lambda: fake)
    telemetry._telemetry_cache.clear()
    telemetry._telemetry_inflight.clear()
    results = await asyncio.gather(*[
        telemetry.collect_current_telemetry(include_probe=False, include_iperf=False)
        for _ in range(8)
    ])
    assert all(item["status"] == "ok" for item in results)
    assert fake.device_calls == 1
    assert fake.flow_calls == 1
    await telemetry.close_telemetry_tasks()

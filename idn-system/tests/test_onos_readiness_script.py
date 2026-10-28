"""Unit coverage for the standalone, read-only ONOS readiness command."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_CANDIDATES = (Path("/workspace"), Path(__file__).resolve().parents[1])
_ROOT = next(
    (
        candidate
        for candidate in _CANDIDATES
        if (candidate / "validation" / "check_onos_readiness.py").is_file()
    ),
    None,
)
if _ROOT is None:
    pytest.skip(
        "validation/check_onos_readiness.py is not part of this repository",
        allow_module_level=True,
    )
_SCRIPT = _ROOT / "validation" / "check_onos_readiness.py"
_SPEC = importlib.util.spec_from_file_location("onos_readiness_for_tests", _SCRIPT)
assert _SPEC and _SPEC.loader
readiness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(readiness)


def _ready_payload(path: str) -> dict:
    if path == "/onos/v1/applications":
        names = sorted(readiness.P4_APPS)
        return {"status": 200, "json": {"applications": [
            {"name": name, "state": "ACTIVE"} for name in names
        ]}}
    if path == "/onos/v1/devices":
        return {"status": 200, "json": {"devices": [
            {
                "id": device_id,
                "available": True,
                "driver": f"stratum-bmv2:{readiness.REQUIRED_PIPECONF_ID}",
            }
            for device_id in sorted(readiness.REQUIRED_DEVICE_IDS)
        ]}}
    return {"status": 200, "json": {}}


def _docker(*args: str) -> tuple[int, str]:
    if args[:2] == ("ps", "-a"):
        return 0, "idn-onos idn-onos:latest\n"
    if args and args[0] == "top":
        return 0, "PID ARGS\n71 org.apache.karaf.main.Main\n"
    if args and args[0] == "logs":
        return 0, "ONOS ready\n"
    raise AssertionError(args)


def test_readiness_passes_only_with_core_services_devices_and_pipeconf(monkeypatch, capsys):
    monkeypatch.setattr(readiness, "get_json", _ready_payload)
    monkeypatch.setattr(readiness, "docker", _docker)

    assert readiness.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is True
    assert report["checks"]["pipeconf_active"] is True
    assert report["checks"]["six_devices_available"] is True


def test_devices_503_fails_readiness_instead_of_becoming_empty_inventory(monkeypatch, capsys):
    def device_service_unavailable(path: str) -> dict:
        if path == "/onos/v1/devices":
            return {"status": 503, "json": None}
        return _ready_payload(path)

    monkeypatch.setattr(readiness, "get_json", device_service_unavailable)
    monkeypatch.setattr(readiness, "docker", _docker)

    assert readiness.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["device_service"] is False
    assert report["devices"]["present"] == []

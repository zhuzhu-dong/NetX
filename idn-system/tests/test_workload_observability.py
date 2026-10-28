from __future__ import annotations

import importlib.util
import json
import signal
import subprocess
import time
from pathlib import Path

import pytest


_DEFAULT_ROOT = Path(__file__).resolve().parents[2]
ROOT = next(
    (
        candidate for candidate in (_DEFAULT_ROOT, Path("/workspace"), Path("/app"))
        if (candidate / "mininet" / "workload_runner.py").exists()
    ),
    _DEFAULT_ROOT,
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


runner = _load("idn_workload_runner_tests", ROOT / "mininet" / "workload_runner.py")
probe = _load("idn_workload_probe_tests", ROOT / "mininet" / "probe_server.py")


def test_tcp_workload_segment_exposes_real_receiver_metrics():
    metrics, counters = runner._segment_metrics("TCP", json.dumps({
        "end": {
            "sum_sent": {"bits_per_second": 40_000_000, "bytes": 10_000_000,
                         "retransmits": 2},
            "sum_received": {"bits_per_second": 39_000_000, "bytes": 9_750_000},
            "streams": [{"sender": {"mean_rtt": 2200, "max_snd_cwnd": 65536,
                                      "retransmits": 2}}],
        }
    }))
    assert metrics["receiver_mbps"] == 39
    assert metrics["mean_rtt_ms"] == 2.2
    assert counters["bytes_received"] == 9_750_000
    assert counters["retransmits"] == 2


def test_udp_workload_segment_prefers_receiver_server_json():
    metrics, counters = runner._segment_metrics("UDP", json.dumps({
        "end": {"sum": {"bits_per_second": 21_000_000, "bytes": 5_250_000,
                          "packets": 5000}},
        "server_output_json": {"end": {"sum": {
            "bits_per_second": 19_500_000, "bytes": 4_875_000, "packets": 5000,
            "lost_packets": 25, "lost_percent": 0.5, "jitter_ms": 0.4,
        }}},
    }))
    assert metrics["received_mbps"] == 19.5
    assert metrics["received_packets"] == 4975
    assert counters["bytes_received"] == 4_875_000


def test_exact_running_workload_is_reused_and_stats_are_visible(tmp_path):
    class RunningProcess:
        pid = 123
        returncode = None

        @staticmethod
        def poll():
            return None

    stats_file = tmp_path / "stats.json"
    stats_file.write_text(json.dumps({
        "status": "MEASURED", "sequence": 3, "sampled_at": "2026-01-01T00:00:00Z",
        "protocol_metrics": {"receiver_mbps": 39.0},
        "cumulative": {"bytes_received": 10_000_000},
    }), encoding="utf-8")
    record = {
        "spec": {
            "workload_id": "WL-ai", "service_id": "ai_data",
            "src_endpoint": "h_ai", "dst_endpoint": "h_dc",
            "selector": {
                "source_ip": "192.168.20.10/32", "destination_ip": "10.0.0.10/32",
                "protocol": "TCP", "source_port": None, "destination_port": 5202,
            },
            "actual_dst_ip": "10.0.0.10", "stats_file": str(stats_file),
        },
        "server_process": RunningProcess(), "client_process": RunningProcess(),
        "lifecycle_state": "RUNNING", "started_at": "2026-01-01T00:00:00Z",
        "stopped_at": None, "monotonic_started": time.monotonic(),
        "restart_count": 0, "statistics": {}, "last_error": None,
    }
    probe._workload_processes["WL-ai"] = record
    try:
        workload_id = probe._matching_runtime_workload({
            "service_id": "ai_data", "src_host": "h_ai", "dst_host": "h_dc",
            "src_ip": "192.168.20.10/32", "dst_ip": "10.0.0.10/32",
            "protocol": "TCP", "src_port": None, "dst_port": 5202,
        })
        snapshot = probe._workload_snapshot("WL-ai")
    finally:
        probe._workload_processes.clear()
    assert workload_id == "WL-ai"
    assert snapshot["statistics"]["protocol_metrics"]["receiver_mbps"] == 39


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="requires POSIX process-group signals")
def test_runtime_process_cleanup_is_graceful_before_forced_kill(monkeypatch):
    class Process:
        pid = 4321

        def __init__(self):
            self.wait_timeouts = []

        @staticmethod
        def poll():
            return None

        def wait(self, timeout):
            self.wait_timeouts.append(timeout)
            if timeout == 3:
                raise subprocess.TimeoutExpired(cmd="workload", timeout=timeout)
            return -signal.SIGKILL

        @staticmethod
        def terminate():
            pytest.fail("process-group signaling should be used when available")

        @staticmethod
        def kill():
            pytest.fail("process-group signaling should be used when available")

    signals = []
    monkeypatch.setattr(probe.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    process = Process()

    probe._kill_process(process)

    assert signals == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]
    assert process.wait_timeouts == [3, 1]

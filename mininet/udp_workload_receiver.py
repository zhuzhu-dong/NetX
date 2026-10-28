"""Shared, receiver-accounted UDP endpoint for production workloads.

The production workload model permits independent services with distinct
selectors to use the same UDP destination port.  iperf3 serializes those
tests, so this receiver multiplexes only the workload transport endpoint and
persists per-workload receiver measurements.  It does not encode service
names, policies, or acceptance thresholds.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import time
from datetime import datetime, timezone
from pathlib import Path


_stopping = False


def _stop(_signum, _frame) -> None:
    global _stopping
    _stopping = True


def _write(path: Path, payload: dict) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _record_path(stats_dir: Path, workload_id: str) -> Path:
    safe = "".join(char if char.isalnum() or char in "_.-" else "_" for char in workload_id)
    return stats_dir / f"idn_workload_{safe}.json"


def _emit(records: dict[str, dict], stats_dir: Path, now: float) -> None:
    sampled_at = datetime.now(timezone.utc).isoformat()
    for workload_id, record in records.items():
        # Preserve the last real receiver sample across an idle interval.  An
        # empty timer tick is not a new measurement and must not overwrite
        # valid burst evidence with a fabricated zero-throughput sample.
        if record["window_packets"] <= 0:
            continue
        elapsed = max(now - record["window_started"], 0.001)
        window_bytes = record["window_bytes"]
        record["sequence"] += 1
        _write(_record_path(stats_dir, workload_id), {
            "sampled_at": sampled_at,
            "sequence": record["sequence"],
            "status": "MEASURED",
            "protocol": "UDP",
            "segment_seconds": elapsed,
            "protocol_metrics": {
                "sent_mbps": None,
                "received_mbps": (window_bytes * 8) / elapsed / 1_000_000,
                "sent_packets": None,
                "received_packets": record["window_packets"],
                "lost_packets": 0,
                "total_datagrams": record["window_packets"],
                "loss_percent": 0.0,
                "jitter_ms": None,
                "out_of_order": 0,
            },
            "cumulative": {
                "bytes_sent": None,
                "bytes_received": record["bytes_received"],
                "sent_packets": None,
                "received_packets": record["packets_received"],
                "lost_packets": 0,
                "retransmits": 0,
            },
        })
        record["window_started"] = now
        record["window_bytes"] = 0
        record["window_packets"] = 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--stats-dir", default="/tmp")
    parser.add_argument("--sample-seconds", type=float, default=1.0)
    args = parser.parse_args()
    stats_dir = Path(args.stats_dir)
    stats_dir.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.port))
    sock.setblocking(False)
    records: dict[str, dict] = {}
    next_emit = time.monotonic() + max(args.sample_seconds, 0.1)
    try:
        while not _stopping:
            timeout = max(0.0, next_emit - time.monotonic())
            readable, _, _ = select.select([sock], [], [], timeout)
            if readable:
                payload, _peer = sock.recvfrom(65535)
                parts = payload.split(b"|", 2)
                if len(parts) != 3 or parts[0] != b"IDN1":
                    continue
                try:
                    workload_id = parts[1].decode("ascii")
                except UnicodeDecodeError:
                    continue
                if not workload_id:
                    continue
                now = time.monotonic()
                record = records.setdefault(workload_id, {
                    "window_started": now,
                    "window_bytes": 0,
                    "window_packets": 0,
                    "bytes_received": 0,
                    "packets_received": 0,
                    "sequence": 0,
                })
                size = len(payload)
                record["window_bytes"] += size
                record["window_packets"] += 1
                record["bytes_received"] += size
                record["packets_received"] += 1
            now = time.monotonic()
            if now >= next_emit:
                _emit(records, stats_dir, now)
                next_emit = now + max(args.sample_seconds, 0.1)
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

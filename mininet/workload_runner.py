"""Protocol-aware workload client executed inside a Mininet host namespace.

This process is intentionally generic: all behaviour comes from command-line
workload metadata.  It never knows service names or demonstration scenarios.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone


_stop = False
_child: subprocess.Popen | None = None


def _signal_stop(_signum, _frame) -> None:
    global _stop
    _stop = True
    if _child and _child.poll() is None:
        _child.terminate()


def _iperf_args(args: argparse.Namespace, *, segment_seconds: int | None = None) -> list[str]:
    command = [
        "iperf3", "-c", args.destination_ip, "-p", str(args.destination_port),
        "-P", str(args.parallel_flows), "-S", f"0x{args.dscp << 2:02x}",
        "-J", "--get-server-output",
    ]
    if args.source_port:
        command.extend(["--cport", str(args.source_port)])
    if args.protocol == "UDP":
        command.extend([
            "-u", "-b", f"{args.offered_rate_mbps:g}M",
            "-l", str(args.packet_size_bytes),
            "--fq-rate", f"{args.offered_rate_mbps:g}M",
        ])
    elif args.offered_rate_mbps:
        command.extend(["--fq-rate", f"{args.offered_rate_mbps:g}M"])
    if args.traffic_pattern == "BURSTY" and args.burst_size_bytes:
        if args.protocol == "UDP":
            command.extend(["-k", str(max(1, math.ceil(args.burst_size_bytes / args.packet_size_bytes)))])
        else:
            command.extend(["-n", str(args.burst_size_bytes)])
    else:
        command.extend(["-t", str(max(1, segment_seconds or args.duration_seconds or 86400))])
    return command


def _run(command: list[str]) -> int:
    global _child
    _child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while _child.poll() is None and not _stop:
        time.sleep(0.2)
    if _stop and _child.poll() is None:
        _child.terminate()
    return _child.wait()


def _run_capture(command: list[str]) -> tuple[int, str, str]:
    """Run one bounded traffic segment and retain iperf's measured JSON."""
    global _child
    _child = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    while _child.poll() is None and not _stop:
        time.sleep(0.2)
    if _stop and _child.poll() is None:
        _child.terminate()
    stdout, stderr = _child.communicate()
    return _child.returncode, stdout, stderr


def _segment_metrics(protocol: str, output: str) -> tuple[dict, dict]:
    """Extract protocol metrics and additive counters from one iperf JSON result."""
    payload = json.loads(output)
    end = payload.get("end") or {}
    if protocol == "UDP":
        server = payload.get("server_output_json") or {}
        receiver = (server.get("end") or {}).get("sum") or end.get("sum") or {}
        sender = end.get("sum") or {}
        metrics = {
            "sent_mbps": float(sender.get("bits_per_second") or 0) / 1_000_000,
            "received_mbps": float(receiver.get("bits_per_second") or 0) / 1_000_000,
            "sent_packets": int(sender.get("packets") or 0),
            "received_packets": max(
                int(receiver.get("packets") or 0) - int(receiver.get("lost_packets") or 0), 0
            ),
            "lost_packets": int(receiver.get("lost_packets") or 0),
            "total_datagrams": int(receiver.get("packets") or 0),
            "loss_percent": float(receiver.get("lost_percent") or 0),
            "jitter_ms": float(receiver.get("jitter_ms") or 0),
            "out_of_order": int(receiver.get("out_of_order") or 0),
        }
        counters = {
            "bytes_sent": int(sender.get("bytes") or 0),
            "bytes_received": int(receiver.get("bytes") or 0),
            "sent_packets": metrics["sent_packets"],
            "received_packets": metrics["received_packets"],
            "lost_packets": metrics["lost_packets"],
            "retransmits": 0,
        }
        return metrics, counters

    sent = end.get("sum_sent") or {}
    received = end.get("sum_received") or {}
    streams = end.get("streams") or []
    sender_streams = [item.get("sender") or {} for item in streams]
    rtts = [float(item.get("mean_rtt") or 0) / 1000 for item in sender_streams]
    metrics = {
        "sender_mbps": float(sent.get("bits_per_second") or 0) / 1_000_000,
        "receiver_mbps": float(received.get("bits_per_second") or 0) / 1_000_000,
        "retransmits": int(sent.get("retransmits") or sum(
            int(item.get("retransmits") or 0) for item in sender_streams
        )),
        "mean_rtt_ms": sum(rtts) / len(rtts) if rtts else None,
        "max_cwnd": max((int(item.get("max_snd_cwnd") or 0) for item in sender_streams), default=0),
        "bytes_sent": int(sent.get("bytes") or 0),
        "bytes_received": int(received.get("bytes") or 0),
    }
    counters = {
        "bytes_sent": metrics["bytes_sent"],
        "bytes_received": metrics["bytes_received"],
        "sent_packets": 0,
        "received_packets": 0,
        "lost_packets": 0,
        "retransmits": metrics["retransmits"],
    }
    return metrics, counters


def _write_stats(path: str | None, payload: dict) -> None:
    if not path:
        return
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _run_multiplexed_udp(args: argparse.Namespace) -> int:
    """Send a DSCP-marked UDP workload to the shared receiver.

    iperf3 deliberately accepts one test at a time on a listener.  That is a
    poor fit for the production workload API: two distinct services may
    legitimately share a UDP destination port while being separated by their
    /32 selectors and DSCP.  The companion receiver records per-workload
    receiver counters, so this remains real paced traffic rather than a
    synthetic KPI.
    """
    if not args.workload_id:
        raise ValueError("--workload-id is required for multiplexed UDP")
    if not args.destination_port or not args.offered_rate_mbps:
        raise ValueError("multiplexed UDP requires destination port and offered rate")
    header = f"IDN1|{args.workload_id}|".encode("ascii")
    packet_size = max(int(args.packet_size_bytes), len(header) + 1)
    payload = header + (b"x" * (packet_size - len(header)))
    interval = (packet_size * 8) / (float(args.offered_rate_mbps) * 1_000_000)
    interval = max(interval, 0.00001)
    deadline = None if args.duration_seconds is None else time.monotonic() + args.duration_seconds
    next_send = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, int(args.dscp) << 2)
        while not _stop and (deadline is None or time.monotonic() < deadline):
            now = time.monotonic()
            if now < next_send:
                time.sleep(min(next_send - now, 0.01))
                continue
            sock.sendto(payload, (args.destination_ip, int(args.destination_port)))
            next_send += interval
            # If the process was descheduled, reset the pacing deadline rather
            # than emitting a catch-up burst that invalidates the offered rate.
            if next_send < now - interval:
                next_send = now + interval
    finally:
        sock.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", choices=("UDP", "TCP", "ICMP"), required=True)
    parser.add_argument("--traffic-pattern", required=True)
    parser.add_argument("--destination-ip", required=True)
    parser.add_argument("--destination-port", type=int)
    parser.add_argument("--source-port", type=int)
    parser.add_argument("--offered-rate-mbps", type=float)
    parser.add_argument("--packet-size-bytes", type=int, default=1200)
    parser.add_argument("--parallel-flows", type=int, default=1)
    parser.add_argument("--dscp", type=int, default=0)
    parser.add_argument("--duration-seconds", type=int)
    parser.add_argument("--burst-size-bytes", type=int)
    parser.add_argument("--burst-interval-seconds", type=float)
    parser.add_argument("--stats-file")
    parser.add_argument("--sample-seconds", type=int, default=2)
    parser.add_argument("--udp-multiplexed", action="store_true")
    parser.add_argument("--workload-id")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _signal_stop)
    signal.signal(signal.SIGINT, _signal_stop)
    started = time.monotonic()

    if args.protocol == "ICMP":
        interval = max(args.burst_interval_seconds or 1.0, 0.01)
        command = [
            "ping", args.destination_ip, "-i", f"{interval:g}",
            "-s", str(max(0, args.packet_size_bytes - 28)),
            "-Q", str(args.dscp << 2),
        ]
        if args.duration_seconds:
            command.extend(["-w", str(args.duration_seconds)])
        return _run(command)

    if args.udp_multiplexed:
        if args.protocol != "UDP":
            raise ValueError("--udp-multiplexed is only valid for UDP")
        return _run_multiplexed_udp(args)

    # Long-running workloads are executed as adjacent bounded segments.  This
    # preserves real production traffic while making receiver-side iperf JSON
    # continuously observable without starting a competing validation server.
    recurring = args.traffic_pattern in {"BURSTY", "PERIODIC", "INTERACTIVE"}
    interval = max(args.burst_interval_seconds or 1.0, 0.05)
    sequence = 0
    cumulative = {
        "bytes_sent": 0, "bytes_received": 0, "sent_packets": 0,
        "received_packets": 0, "lost_packets": 0, "retransmits": 0,
    }
    while not _stop:
        elapsed = time.monotonic() - started
        if args.duration_seconds is not None and elapsed >= args.duration_seconds:
            break
        remaining = None if args.duration_seconds is None else max(1, int(args.duration_seconds - elapsed))
        sample_seconds = max(1, min(int(args.sample_seconds), 30))
        segment = min(sample_seconds, remaining) if remaining is not None else sample_seconds
        returncode, stdout, stderr = _run_capture(_iperf_args(args, segment_seconds=segment))
        if returncode != 0 and not _stop:
            _write_stats(args.stats_file, {
                "sampled_at": datetime.now(timezone.utc).isoformat(),
                "sequence": sequence, "status": "ERROR",
                "error": stderr.strip() or stdout.strip() or f"iperf3 exit {returncode}",
                "cumulative": cumulative,
            })
            return returncode
        if _stop:
            break
        try:
            metrics, delta = _segment_metrics(args.protocol, stdout)
        except Exception as exc:
            _write_stats(args.stats_file, {
                "sampled_at": datetime.now(timezone.utc).isoformat(),
                "sequence": sequence, "status": "ERROR",
                "error": f"invalid iperf3 JSON: {exc}", "cumulative": cumulative,
            })
            return 2
        sequence += 1
        for key in cumulative:
            cumulative[key] += int(delta.get(key) or 0)
        _write_stats(args.stats_file, {
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "sequence": sequence, "status": "MEASURED", "protocol": args.protocol,
            "segment_seconds": segment, "protocol_metrics": metrics,
            "cumulative": cumulative,
        })
        if recurring:
            time.sleep(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

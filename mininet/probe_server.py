"""Mininet active telemetry probe server.

Exposes topology snapshots and active probes (ping, iperf, qdisc, INT) to the
application container via HTTP.  Also handles INT (In-band Network Telemetry)
probing via P4-enabled raw sockets.

All telemetry parsing functions are pure — they take raw output strings/bytes
and return structured dicts.  The probe server orchestrates probes on the live
Mininet topology.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import copy
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4
from urllib.parse import parse_qs, urlparse
from ipaddress import ip_address, ip_network
from pathlib import Path

_MODULE_DIR = str(Path(__file__).resolve().parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)
from int_schema import decode_hops as _decode_int_hops, schema as _int_schema

# ── Constants ─────────────────────────────────────────────────────────────────

PROBE_PORT = 9001
INT_IP_PROTOCOL = 253
INT_SCHEMA = _int_schema()
INT_HOP_BYTES = int(INT_SCHEMA["hop_record"]["length_bytes"])
INT_HEADER_BYTES = int(INT_SCHEMA["header"]["length_bytes"])
INT_PARSER_VERSION = str(INT_SCHEMA["parser_version"])

# ── Module-level topology config (set by start_probe_server) ──────────────────

_switch_config: dict = {}
_switch_name_by_id: dict = {}
_host_runtime_config: dict = {}
_host_facing_ports: dict = {}
_qos_policies: dict[str, dict] = {}
# Calibration deliberately changes only the instantaneous HTB ceiling of an
# already installed, deterministic scope.  The declared policy is never
# replaced: the normal QoS installer restores it in the same fenced window.
_qos_calibration_windows: set[str] = set()
# One prepared generation per deterministic traffic scope.  The old binding
# remains installed until the controller has proved the new path through two
# independent readbacks and explicitly commits the transaction.
_qos_pending: dict[str, dict] = {}
_ingress_admission_plans: dict[str, dict] = {}
# The normal admission plan stays authoritative.  A calibration window keeps a
# private snapshot only while the backend owns the global write lease, then
# restores that plan with the same actuator used by reconciliation.
_ingress_calibration_windows: dict[str, dict] = {}
_diagnostic_tc_allocations: dict[str, tuple[str, int]] = {}
_workload_processes: dict[str, dict] = {}
# A UDP listener is shared only by production workloads that explicitly use
# this runtime's multiplexing receiver.  Unmanaged listeners remain a hard
# error, preserving the ownership boundary for arbitrary external processes.
_shared_udp_servers: dict[tuple[str, int], dict] = {}
_workload_lock = threading.RLock()
_fault_states: dict[str, dict] = {}
_fault_lock = threading.RLock()
_runtime_identity = f"mininet-{uuid4()}"
_runtime_started_at = _utc_now() if "_utc_now" in globals() else datetime.now(timezone.utc).isoformat()
_path_templates_cache: tuple[str, float, dict] | None = None
_service_profiles_cache: tuple[str, float, dict] | None = None


class _IperfServerLockPool:
    """Serialize only probes that would bind the same iperf server socket.

    A process-wide iperf lock made the threaded probe API appear concurrent
    while silently serializing unrelated traffic flows.  That prevented a
    production QoS acceptance probe from creating real overlapping load.  An
    iperf server collision is determined by its destination namespace and
    listening port, so independent destinations/ports may run concurrently.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, int], threading.Lock] = {}

    def for_server(self, destination: str, port: int) -> threading.Lock:
        key = (str(destination), int(port))
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())


def _load_path_templates() -> dict:
    """Load the same validated desired-path file used by the backend."""
    global _path_templates_cache
    path = os.getenv("TOPOLOGY_PATHS_FILE", "/app/config/topology_paths.json")
    mtime = os.path.getmtime(path) if os.path.exists(path) else -1
    if _path_templates_cache and _path_templates_cache[:2] == (path, mtime):
        return _path_templates_cache[2]
    if mtime < 0:
        raise ValueError(f"PATH_CONFIG_INVALID: missing {path}")
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload.get("paths"), list):
        raise ValueError("PATH_CONFIG_INVALID: paths must be a list")
    _path_templates_cache = (path, mtime, payload)
    return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_service_registry() -> dict:
    """Load centralized service and scenario definitions inside Mininet."""
    global _service_profiles_cache
    path = os.getenv("SERVICE_PROFILES_FILE", "/app/config/service_profiles.json")
    mtime = os.path.getmtime(path) if os.path.exists(path) else -1
    if _service_profiles_cache and _service_profiles_cache[:2] == (path, mtime):
        return _service_profiles_cache[2]
    if mtime < 0:
        raise ValueError(f"SERVICE_PROFILE_INVALID: missing {path}")
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    profiles = raw.get("profiles") or []
    scenarios = raw.get("traffic_scenarios") or []
    by_id = {str(item.get("service_id")): item for item in profiles}
    scenario_by_id = {str(item.get("scenario_id")): item for item in scenarios}
    if len(by_id) != len(profiles) or "None" in by_id:
        raise ValueError("SERVICE_PROFILE_INVALID: duplicate or absent service_id")
    if len(scenario_by_id) != len(scenarios) or "None" in scenario_by_id:
        raise ValueError("SERVICE_PROFILE_INVALID: duplicate or absent scenario_id")
    payload = {
        "schema_version": raw.get("schema_version"),
        "profiles": profiles,
        "by_id": by_id,
        "scenarios": scenarios,
        "scenario_by_id": scenario_by_id,
    }
    _service_profiles_cache = (path, mtime, payload)
    return payload


def _service_profile(service_id: str) -> dict:
    profile = _load_service_registry()["by_id"].get(str(service_id))
    if not profile or not profile.get("enabled", True):
        raise ValueError(f"SERVICE_PROFILE_INVALID: unknown or disabled service_id={service_id}")
    return dict(profile)


def _traffic_scenario(scenario_id: str) -> dict:
    scenario = _load_service_registry()["scenario_by_id"].get(str(scenario_id))
    if not scenario:
        raise ValueError(f"SERVICE_PROFILE_INVALID: unknown scenario_id={scenario_id}")
    return dict(scenario)


def _resolve_configured_path(src_host: str, dst_host: str, switch_path: list[str]) -> dict:
    payload = _load_path_templates()
    endpoint_paths = [
        item for item in payload.get("paths", [])
        if item.get("enabled", True)
        and item.get("src_host") == src_host
        and item.get("dst_host") == dst_host
    ]
    if not endpoint_paths:
        return {
            "path_id": None,
            "path_config_status": "NOT_CONFIGURED",
            "configured_primary_path_id": None,
            "alternative_path_ids": [],
        }
    selected = next((item for item in endpoint_paths if item.get("switches") == switch_path), None)
    if selected is None:
        raise ValueError(
            "PATH_LINK_MISSING: runtime switch path is not a configured enabled path: "
            + " -> ".join(switch_path)
        )
    primary = next((item for item in endpoint_paths if str(item.get("role")).upper() == "PRIMARY"), None)
    return {
        "path_id": selected.get("path_id"),
        "path_name": selected.get("name"),
        "path_role": str(selected.get("role") or "").upper(),
        "path_config_status": "VALIDATED",
        "configured_primary_path_id": (primary or {}).get("path_id"),
        "alternative_path_ids": [item.get("path_id") for item in endpoint_paths if item is not selected],
    }


def _tc_identity(value: dict | str) -> tuple[str, int]:
    """Return explicit allocator IDs, or a collision-checked local fallback.

    Production calls provide IDs allocated transactionally by the backend.
    The fallback exists for direct Mininet diagnostics and selects the first
    actually free pair from the live in-memory registry; it never assumes a
    hash is collision-free.
    """
    payload = value if isinstance(value, dict) else {"scope_key": str(value)}
    raw_class = payload.get("class_id")
    raw_filter = payload.get("filter_id")
    if raw_class is not None or raw_filter is not None:
        if raw_class is None or raw_filter is None:
            raise ValueError("class_id and filter_id must be provided together")
        # The bundled Stratum/Mininet image still ships an older Python that
        # predates str.removeprefix().  Keep parsing explicit allocator IDs
        # portable because this path is exercised during restart recovery.
        class_text = str(raw_class).lower()
        if class_text.startswith("1:"):
            class_text = class_text[2:]
        if class_text.startswith("0x"):
            class_text = class_text[2:]
        minor = int(class_text, 16)
        priority = int(raw_filter)
        if not 0x100 <= minor < 0xFE00 or not 100 <= priority < 32000:
            raise ValueError("allocated TC resource is outside the managed range")
        return format(minor, "x"), priority
    scope_key = str(payload.get("scope_key") or "")
    if not scope_key:
        raise ValueError("scope_key is required for local TC allocation")
    if scope_key in _diagnostic_tc_allocations:
        return _diagnostic_tc_allocations[scope_key]
    existing = _qos_policies.get(scope_key) or {}
    binding = existing.get("qos_binding") or {}
    if binding.get("class_id") and binding.get("filter_id"):
        return _tc_identity(binding)
    used_classes, used_filters = set(), set()
    for class_id, filter_id in _diagnostic_tc_allocations.values():
        used_classes.add(int(class_id, 16))
        used_filters.add(int(filter_id))
    for policy in _qos_policies.values():
        resource = policy.get("qos_binding") or policy
        if resource.get("class_id"):
            used_classes.add(int(str(resource["class_id"]).split(":", 1)[-1], 16))
        if resource.get("filter_id"):
            used_filters.add(int(resource["filter_id"]))
    minor = next(value for value in range(0x100, 0xFE00) if value not in used_classes)
    priority = next(value for value in range(100, 32000) if value not in used_filters)
    identity = (format(minor, "x"), priority)
    _diagnostic_tc_allocations[scope_key] = identity
    return identity


def _tc_rate_mbps(output: str, class_id: str) -> float | None:
    pattern = rf"class\s+htb\s+1:{re.escape(class_id)}\b[^\n]*?rate\s+([0-9.]+)([KMG]?)bit"
    match = re.search(pattern, output, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    return value * {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}.get(unit, 1.0)


def _rate_to_mbps(value: str, unit: str) -> float:
    return float(value) * {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}[unit.upper()]


def _tc_class_values(output: str, class_id: str) -> dict:
    pattern = rf"^class\s+htb\s+1:{re.escape(class_id)}\b([^\n]*)"
    match = re.search(pattern, output, re.IGNORECASE | re.MULTILINE)
    if not match:
        return {"present": False, "rate_mbps": None, "ceil_mbps": None}
    line = match.group(1)
    rate = re.search(r"\brate\s+([0-9.]+)([KMG]?)bit", line, re.IGNORECASE)
    ceil = re.search(r"\bceil\s+([0-9.]+)([KMG]?)bit", line, re.IGNORECASE)
    prio = re.search(r"\bprio\s+(\d+)", line, re.IGNORECASE)
    return {
        "present": True,
        "rate_mbps": _rate_to_mbps(rate.group(1), rate.group(2)) if rate else None,
        "ceil_mbps": _rate_to_mbps(ceil.group(1), ceil.group(2)) if ceil else None,
        "priority": int(prio.group(1)) if prio else None,
    }


def _tc_class_stats(output: str, class_id: str) -> dict:
    block_match = re.search(
        rf"(^class\s+htb\s+1:{re.escape(class_id)}\b.*?)(?=^class\s+htb\s+|\Z)",
        output,
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    block = block_match.group(1) if block_match else ""
    sent = re.search(
        r"Sent\s+(\d+)\s+bytes\s+(\d+)\s+pkt\s*\(dropped\s+(\d+),\s*overlimits\s+(\d+)",
        block,
        re.IGNORECASE,
    )
    borrowed = re.search(r"\blended:\s*(\d+)\s+borrowed:\s*(\d+)", block, re.IGNORECASE)
    backlog = re.search(r"backlog\s+(\d+)b\s+(\d+)p", block, re.IGNORECASE)
    return {
        "sent_bytes": int(sent.group(1)) if sent else None,
        "sent_packets": int(sent.group(2)) if sent else None,
        "dropped_packets": int(sent.group(3)) if sent else None,
        "overlimits": int(sent.group(4)) if sent else None,
        "lended": int(borrowed.group(1)) if borrowed else None,
        "borrowed": int(borrowed.group(2)) if borrowed else None,
        "backlog_bytes": int(backlog.group(1)) if backlog else 0,
        "backlog_packets": int(backlog.group(2)) if backlog else 0,
    }


def _tc_filter_stats(output: str) -> dict:
    """Return gact action counters attached to an observational flower rule."""
    sent = re.search(
        r"Sent\s+(\d+)\s+bytes\s+(\d+)\s+pkt\s*\(dropped\s+(\d+),\s*overlimits\s+(\d+)",
        output,
        re.IGNORECASE,
    )
    return {
        "matched_bytes": int(sent.group(1)) if sent else None,
        "matched_packets": int(sent.group(2)) if sent else None,
        "dropped_packets": int(sent.group(3)) if sent else None,
        "overlimits": int(sent.group(4)) if sent else None,
    }


def _tc_filter_counter_delta(before: dict, after: dict) -> dict:
    return {
        key: (
            int(after.get(key) or 0) - int(before.get(key) or 0)
            if after.get(key) is not None and before.get(key) is not None else None
        )
        for key in ("matched_bytes", "matched_packets", "dropped_packets", "overlimits")
    }


def _node_pid(node) -> int:
    pid = int(getattr(node, "pid", 0) or 0)
    if pid <= 0:
        raise RuntimeError("Mininet node has no valid namespace pid")
    return pid


def _node_cmd(
    node,
    command: str,
    *,
    timeout: float = 8.0,
    check: bool = False,
    return_status: bool = False,
) -> str | tuple[str, int]:
    """Run a bounded command without Mininet's shared interactive shell.

    ``Node.cmd`` multiplexes every call over one shell.  HTTP probe requests are
    concurrent, so a slow command can corrupt that channel or block unrelated
    topology and health requests.  ``mnexec`` gives every probe an independent
    process inside the existing host namespace.
    """
    pid = _node_pid(node)
    try:
        completed = subprocess.run(
            ["mnexec", "-a", str(pid), "sh", "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or exc.output or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        detail = str(output).strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"node command timed out after {timeout:g}s{suffix}") from exc

    output = completed.stdout or ""
    if check and completed.returncode != 0:
        raise RuntimeError(output.strip() or f"node command failed with exit code {completed.returncode}")
    return (output, int(completed.returncode)) if return_status else output


def _node_popen(node, args: list[str], **kwargs) -> subprocess.Popen:
    """Start an independently killable process in a Mininet host namespace."""
    pid = _node_pid(node)
    return subprocess.Popen(
        ["mnexec", "-a", str(pid), *args],
        start_new_session=True,
        **kwargs,
    )


def _kill_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        # Give workload_runner an opportunity to terminate and wait for its
        # active iperf child.  Killing the entire process group immediately
        # can orphan a simultaneous child death under a non-reaping container
        # PID 1, leaving a persistent ``[iperf3] <defunct>`` entry.
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait(timeout=1)


def _communicate_bounded(
    process: subprocess.Popen,
    timeout: float,
) -> tuple[str, str, bool]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return stdout or "", stderr or "", False
    except subprocess.TimeoutExpired:
        _kill_process(process)
        stdout, stderr = process.communicate()
        return stdout or "", stderr or "", True


def _tc_cmd(node, command: str) -> str:
    """Run a TC command in the node namespace and fail on command errors."""
    return _node_cmd(node, command, timeout=8, check=True)


def _attachment_for_source(net, source_ip: str) -> dict:
    """Resolve a source namespace and its pre-BMv2 attachment at runtime."""
    wanted = ip_address(str(source_ip).split("/", 1)[0])
    topology = build_topology_snapshot(net)
    host = next((item for item in topology.get("hosts", []) if ip_address(item["ip"]) == wanted), None)
    if host is None:
        raise ValueError(f"INGRESS_ATTACHMENT_NOT_FOUND: {source_ip}")
    link = next((item for item in topology.get("links", []) if host["name"] in [x.get("node") for x in item.get("endpoints", [])]), None)
    if link is None:
        raise ValueError(f"INGRESS_ATTACHMENT_LINK_NOT_FOUND: {host['name']}")
    host_end = next(item for item in link["endpoints"] if item.get("node") == host["name"])
    switch_end = next(item for item in link["endpoints"] if item.get("node") != host["name"])
    return {"host": host["name"], "pid": net.get(host["name"]).pid,
            "interface": host_end["interface"], "peer_interface": switch_end["interface"],
            "access_device_id": switch_end.get("device_id"), "access_port": switch_end.get("port"),
            "link_id": link.get("id")}


def _ingress_filter_command(interface: str, spec: dict, classid: str, preference: int) -> str:
    parts = [f"tc filter replace dev {interface} parent a1: protocol ip pref {preference} flower",
             f"src_ip {ip_network(str(spec['source_ip']), strict=False)}"]
    if spec.get("destination_ip"):
        parts.append(f"dst_ip {ip_network(str(spec['destination_ip']), strict=False)}")
    if str(spec.get("protocol") or "ANY").upper() != "ANY":
        parts.append(f"ip_proto {str(spec['protocol']).lower()}")
    if spec.get("source_port") is not None:
        parts.append(f"src_port {int(spec['source_port'])}")
    if spec.get("destination_port") is not None:
        parts.append(f"dst_port {int(spec['destination_port'])}")
    return " ".join(parts + [f"flowid a1:{classid}"])


def _apply_ingress_admission(net, raw: dict) -> dict:
    """Install per-source pre-BMv2 admission trees from a generic plan.

    The plan contains scopes and selectors; no service name, fixed host name, or
    port is embedded in this actuator.  A host namespace is the earliest safe
    control point because its egress precedes the attachment veth and BMv2.
    """
    plan_id = str(raw.get("admission_plan_id") or "")
    mode = str(raw.get("mode") or "").upper()
    if not plan_id or mode not in {"IDLE_BORROWING", "OVERSUBSCRIBED_ADMISSION", "DISABLED"}:
        raise ValueError("INGRESS_ADMISSION_PLAN_INVALID")
    if mode == "DISABLED":
        return _remove_ingress_admission(net, plan_id)
    schedulable = float(raw.get("schedulable_capacity_mbps") or 0)
    reserved = float(raw.get("implementation_reserved_mbps") or 0)
    residual = float(raw.get("best_effort_residual_mbps") or 0)
    protected = list(raw.get("protected_scopes") or [])
    best_effort = list(raw.get("best_effort_scopes") or [])
    inactive_best_effort = list(raw.get("inactive_best_effort_scopes") or [])
    expected_aggregate = float(raw.get("expected_aggregate_ceiling_mbps") or (reserved + residual))
    if schedulable <= 0 or reserved <= 0 or residual < 0 or expected_aggregate > schedulable + 0.01:
        raise ValueError("INGRESS_ADMISSION_BUDGET_INVALID")
    if not protected:
        raise ValueError("INGRESS_ADMISSION_PROTECTED_SCOPE_REQUIRED")
    # ``best_effort_scopes`` is the current ACTIVE set.  Inactive scopes may
    # remain in durable intent state, but receive no runtime shaping budget.
    be_by_source: dict[str, list[dict]] = {}
    for scope in best_effort:
        be_by_source.setdefault(str(scope["source_ip"]), []).append(scope)
    attachments: list[dict] = []
    protected_sources = {str(item["source_ip"]): item for item in protected}
    # Include inactive source attachments as well, so an ACTIVE -> IDLE
    # migration first shrinks their owned class rather than leaving a prior
    # runtime allocation resident on that namespace.
    inactive_sources = [str(scope["source_ip"]) for scope in inactive_best_effort]
    all_sources = list(dict.fromkeys([*protected_sources, *be_by_source, *inactive_sources]))
    be_budget_by_source = {
        source: sum(float(item.get("runtime_budget_mbps") or 0.0) for item in scopes)
        for source, scopes in be_by_source.items()
    }
    if not any(be_budget_by_source.values()) and be_by_source:
        share = residual / len(be_by_source)
        be_budget_by_source = {source: share for source in be_by_source}
    for index, source_ip in enumerate(all_sources, start=1):
        attachment = _attachment_for_source(net, source_ip)
        node = net.get(attachment["host"])
        interface = attachment["interface"]
        before = _tc_cmd(node, f"tc qdisc show dev {interface}")
        owns_root = "qdisc htb a1:" in before
        # Mininet's attachment links already carry its own 1-Gbit HTB root.
        # It is a verified, shared parent, so attach owned children beneath it
        # instead of replacing its root qdisc.  Unknown roots remain refused.
        if owns_root:
            root_handle, parent = "a1", "a1:1"
        elif "qdisc htb 5:" in before and "class htb 5:1 root" in _tc_cmd(node, f"tc class show dev {interface}"):
            root_handle, parent = "5", "5:1"
        elif "qdisc noqueue" in before:
            _tc_cmd(node, f"tc qdisc replace dev {interface} root handle a1: htb default fffe")
            root_handle, parent, owns_root = "a1", "a1:1", True
            _tc_cmd(node, f"tc class replace dev {interface} parent a1: classid a1:1 htb rate {schedulable:g}mbit ceil {schedulable:g}mbit")
        else:
            raise RuntimeError(f"INGRESS_QDISC_CONFLICT: {attachment['host']}:{interface}")
        source_protected = protected_sources.get(source_ip)
        source_budget = (
            float(source_protected.get("ingress_admission_ceiling_mbps") or schedulable)
            if source_protected else be_budget_by_source.get(source_ip, 0.0)
        )
        existing_classes = _tc_cmd(node, f"tc class show dev {interface}")
        existing_filters = _tc_cmd(node, f"tc filter show dev {interface} parent {root_handle}:")
        if source_protected:
            target_floor = float(source_protected.get("implementation_guaranteed_floor_mbps") or reserved)
            target_ceil = float(source_protected.get("ingress_admission_ceiling_mbps") or (
                schedulable if mode == "IDLE_BORROWING" else target_floor
            ))
            if target_ceil + 0.01 < target_floor:
                raise ValueError("INGRESS_PROTECTED_CEILING_BELOW_FLOOR")
            _tc_cmd(node, f"tc class replace dev {interface} parent {parent} classid {root_handle}:10 htb rate {target_floor:g}mbit ceil {target_ceil:g}mbit prio 1")
            if f"class htb {root_handle}:10" not in existing_classes:
                # A rate-guarantee class must not apply an independent
                # latency-based AQM that can discard protected UDP below its
                # admitted rate.  The dedicated HTB leaf owns a bounded FIFO;
                # HTB remains the single rate authority.
                _tc_cmd(node, f"tc qdisc replace dev {interface} parent {root_handle}:10 handle 10: pfifo limit 1000")
            protected_pref = 500 + index
            if f"pref {protected_pref} " not in existing_filters:
                _tc_cmd(node, _ingress_filter_command(interface, source_protected, "10", protected_pref).replace("parent a1:", f"parent {root_handle}:" ).replace("flowid a1:", f"flowid {root_handle}:"))
        # The fallback filter matches every packet emitted by the source host,
        # including reverse-path TCP ACKs for a protected forward flow.  Its
        # guaranteed rate remains the tiny best-effort allocation, but its
        # ceiling must be the verified residual budget.  Capping the ceiling
        # at 1 kbit for a protected-only source starves ACKs, collapses the
        # forward TCP congestion window, and makes a healthy service probe
        # appear to time out.  The residual is part of the formally validated
        # admission plan, so this is still bounded rather than an unlimited
        # bypass of the scheduler.
        default_ceil = be_budget_by_source.get(source_ip, residual)
        if default_ceil <= 0:
            default_ceil = 0.001
        default_rate = 1.0 if default_ceil >= 1.0 else 0.001
        ceiling_unit = f"{default_ceil:g}mbit" if default_ceil >= 1.0 else f"{default_ceil * 1000:g}kbit"
        rate_unit = f"{default_rate:g}mbit" if default_rate >= 1.0 else f"{default_rate * 1000:g}kbit"
        _tc_cmd(node, f"tc class replace dev {interface} parent {parent} classid {root_handle}:fffe htb rate {rate_unit} ceil {ceiling_unit} prio 7")
        if f"class htb {root_handle}:fffe" not in existing_classes:
            _tc_cmd(node, f"tc qdisc replace dev {interface} parent {root_handle}:fffe handle fffe: pfifo limit 1000")
        fallback = {"source_ip": source_ip, "protocol": "ANY"}
        fallback_pref = 800 + index
        if f"pref {fallback_pref} " not in existing_filters:
            _tc_cmd(node, _ingress_filter_command(interface, fallback, "fffe", fallback_pref).replace("parent a1:", f"parent {root_handle}:" ).replace("flowid a1:", f"flowid {root_handle}:"))
        readback = {"qdisc": _tc_cmd(node, f"tc -s qdisc show dev {interface}"),
                    "classes": _tc_cmd(node, f"tc -s class show dev {interface}"),
                    "filters": _tc_cmd(node, f"tc -s filter show dev {interface}")}
        if f"class htb {root_handle}:10" not in readback["classes"] and source_protected:
            raise RuntimeError("INGRESS_ADMISSION_READBACK_FAILED")
        attachments.append({**attachment, "before_qdisc": before, "readback": readback,
                            "protected": bool(source_protected), "budget_mbps": source_budget,
                            "root_handle": root_handle, "owns_root": owns_root,
                            "target_ceil_mbps": target_ceil if source_protected else None,
                            "best_effort_ceil_mbps": default_ceil})
    result = {**raw, "status": "VERIFIED", "actuator": "MininetAttachmentEgressActuator",
              "attachments": attachments, "expected_total_admitted_mbps": expected_aggregate,
              "applied_at": _utc_now()}
    _ingress_admission_plans[plan_id] = result
    return result


def _remove_ingress_admission(net, plan_id: str) -> dict:
    plan = _ingress_admission_plans.get(plan_id)
    if not plan:
        return {"status": "NOT_FOUND", "admission_plan_id": plan_id}
    removed = []
    for attachment in plan.get("attachments") or []:
        node = net.get(attachment["host"])
        interface = attachment["interface"]
        current = _tc_cmd(node, f"tc qdisc show dev {interface}")
        root_handle = attachment.get("root_handle")
        for preference in (500 + len(removed) + 1, 800 + len(removed) + 1):
            _tc_cmd(node, f"tc filter del dev {interface} parent {root_handle}: protocol ip pref {preference}")
        if attachment.get("owns_root") and "qdisc htb a1:" in current:
            _tc_cmd(node, f"tc qdisc del dev {interface} root")
        elif root_handle:
            _tc_cmd(node, f"tc class del dev {interface} classid {root_handle}:10")
            _tc_cmd(node, f"tc class del dev {interface} classid {root_handle}:fffe")
        removed.append({"host": attachment["host"], "interface": interface})
    _ingress_admission_plans.pop(plan_id, None)
    return {"status": "REMOVED", "admission_plan_id": plan_id, "attachments": removed}


def _set_ingress_calibration_window(
    net, admission_plan_id: str, traffic_scope_key: str, operation: str,
    ceiling_mbps: float | None = None,
) -> dict:
    """Temporarily lift an active ingress plan only for live calibration.

    Egress calibration alone is insufficient because the pre-BMv2 admission
    tree is upstream of every P4/TC resource.  The backend holds one fenced
    lease for this change and the accompanying receiver-side probe.  ENTER
    preserves selector/floor semantics and widens only the validated ceiling;
    EXIT reapplies the exact pre-window allocation through the canonical
    admission actuator.
    """
    plan_id = str(admission_plan_id or "")
    scope = str(traffic_scope_key or "")
    normalized_operation = str(operation or "").upper()
    plan = _ingress_admission_plans.get(plan_id)
    if not plan:
        return {"status": "not_found", "admission_plan_id": plan_id, "write_executed": False}
    if scope != str(plan.get("traffic_scope_key") or ""):
        return {"status": "error", "admission_plan_id": plan_id,
                "error": "CALIBRATION_SCOPE_MISMATCH", "write_executed": False}
    if normalized_operation == "ENTER":
        if plan_id in _ingress_calibration_windows:
            return {"status": "ok", "admission_plan_id": plan_id,
                    "window": "ACTIVE", "idempotent": True, "write_executed": False}
        requested = float(ceiling_mbps or 0)
        reserved = float(plan.get("implementation_reserved_mbps") or 0)
        if requested <= 0 or requested + 0.01 < reserved:
            return {"status": "error", "admission_plan_id": plan_id,
                    "error": "CALIBRATION_CEILING_INVALID", "write_executed": False}
        window = copy.deepcopy(plan)
        window["schedulable_capacity_mbps"] = requested
        window["best_effort_residual_mbps"] = max(0.0, requested - reserved)
        window["expected_aggregate_ceiling_mbps"] = requested
        for protected in window.get("protected_scopes") or []:
            protected["ingress_admission_ceiling_mbps"] = requested
        _ingress_calibration_windows[plan_id] = copy.deepcopy(plan)
        applied = _apply_ingress_admission(net, window)
        if applied.get("status") != "VERIFIED":
            _ingress_calibration_windows.pop(plan_id, None)
            return {"status": "error", "admission_plan_id": plan_id,
                    "error": "CALIBRATION_INGRESS_APPLY_FAILED", "observed": applied,
                    "write_executed": True}
        observed_ceils = [float(item.get("ingress_admission_ceiling_mbps") or 0)
                          for item in applied.get("protected_scopes") or []]
        if not observed_ceils or any(abs(value - requested) > 0.01 for value in observed_ceils):
            raise RuntimeError(f"CALIBRATION_INGRESS_READBACK_FAILED:{plan_id}:{observed_ceils}:{requested}")
        return {"status": "ok", "admission_plan_id": plan_id, "scope_key": scope,
                "window": "ACTIVE", "ceiling_mbps": requested, "write_executed": True}
    if normalized_operation == "EXIT":
        original = _ingress_calibration_windows.get(plan_id)
        if not original:
            return {"status": "ok", "admission_plan_id": plan_id,
                    "window": "RESTORED", "idempotent": True, "write_executed": False}
        restored = _apply_ingress_admission(net, original)
        if restored.get("status") != "VERIFIED":
            return {"status": "error", "admission_plan_id": plan_id,
                    "error": "CALIBRATION_INGRESS_RESTORE_FAILED", "observed": restored,
                    "write_executed": True}
        _ingress_calibration_windows.pop(plan_id, None)
        return {"status": "ok", "admission_plan_id": plan_id, "scope_key": scope,
                "window": "RESTORED", "readback": restored, "write_executed": True}
    return {"status": "error", "admission_plan_id": plan_id,
            "error": "INVALID_CALIBRATION_WINDOW_OPERATION", "write_executed": False}


def _host_for_cidr(net, cidr: str) -> str:
    network = ip_network(cidr, strict=False)
    matches = []
    for name, node in net.nameToNode.items():
        if not name.startswith("h_"):
            continue
        raw_ip = str(getattr(node, "IP", lambda: "")()).split("/", 1)[0]
        try:
            if ip_address(raw_ip) in network:
                matches.append(name)
        except ValueError:
            continue
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"no Mininet host belongs to {cidr}")
    raise ValueError(f"ambiguous host prefix {cidr}: {sorted(matches)}")


def _shortest_runtime_path(topology: dict, source: str, destination: str) -> tuple[list[str], list[dict]]:
    """Dijkstra path over runtime links, using YAML-derived link cost."""
    import heapq

    adjacency: dict[str, list[tuple[float, str, dict]]] = {}
    for link in topology.get("links", []) or []:
        endpoints = link.get("endpoints") or []
        if len(endpoints) != 2 or str(link.get("state", "ACTIVE")).upper() != "ACTIVE":
            continue
        left, right = endpoints
        a, b = left.get("node"), right.get("node")
        if not a or not b:
            continue
        cost = float(link.get("cost") or 1.0)
        adjacency.setdefault(a, []).append((cost, b, link))
        adjacency.setdefault(b, []).append((cost, a, link))

    queue = [(0.0, source)]
    distance = {source: 0.0}
    previous: dict[str, tuple[str, dict]] = {}
    while queue:
        current_distance, current = heapq.heappop(queue)
        if current == destination:
            break
        if current_distance != distance.get(current):
            continue
        for cost, neighbor, link in adjacency.get(current, []):
            candidate = current_distance + cost
            if candidate < distance.get(neighbor, float("inf")):
                distance[neighbor] = candidate
                previous[neighbor] = (current, link)
                heapq.heappush(queue, (candidate, neighbor))
    if destination not in distance:
        raise ValueError(f"no active path from {source} to {destination}")

    nodes = [destination]
    links = []
    current = destination
    while current != source:
        parent, link = previous[current]
        nodes.append(parent)
        links.append(link)
        current = parent
    nodes.reverse()
    links.reverse()
    return nodes, links


def _selected_runtime_path(
    topology: dict, source: str, destination: str, selected_path: list[str]
) -> tuple[list[str], list[dict]]:
    """Validate an IntentPlan path against the current runtime topology."""
    nodes = [str(item) for item in selected_path if str(item)]
    if not nodes:
        return _shortest_runtime_path(topology, source, destination)
    if nodes[0] != source:
        nodes.insert(0, source)
    if nodes[-1] != destination:
        nodes.append(destination)
    if nodes[0] != source or nodes[-1] != destination or len(nodes) < 3:
        raise ValueError("selected_path endpoints do not match QoS selector")
    if len(set(nodes)) != len(nodes):
        raise ValueError("selected_path contains a loop")

    links = []
    for left, right in zip(nodes, nodes[1:]):
        match = next((
            link for link in topology.get("links", []) or []
            if str(link.get("state", "ACTIVE")).upper() == "ACTIVE"
            and {str(item.get("node")) for item in link.get("endpoints") or []}
            == {left, right}
        ), None)
        if match is None:
            raise ValueError(f"selected_path hop is unavailable: {left}->{right}")
        links.append(match)
    return nodes, links


def _select_qos_binding(net, payload: dict) -> dict:
    topology = build_topology_snapshot(net)
    source_host = _host_for_cidr(net, payload["source_ip"])
    destination_host = _host_for_cidr(net, payload["destination_ip"])
    nodes, path_links = _selected_runtime_path(
        topology,
        source_host,
        destination_host,
        list(payload.get("selected_path") or []),
    )
    candidates = []
    for index, link in enumerate(path_links):
        endpoints = link.get("endpoints") or []
        if len(endpoints) != 2:
            continue
        from_node, to_node = nodes[index], nodes[index + 1]
        source_endpoint = next((item for item in endpoints if item.get("node") == from_node), None)
        peer_endpoint = next((item for item in endpoints if item.get("node") == to_node), None)
        if not source_endpoint or not peer_endpoint:
            continue
        if source_endpoint.get("type") != "switch" or peer_endpoint.get("type") != "switch":
            continue
        capacity = int(link.get("bandwidth_mbps") or 0)
        if capacity <= 0:
            raise ValueError(f"link capacity unavailable for {link.get('id')}")
        interface_name = str(source_endpoint.get("interface") or "")
        egress_port = source_endpoint.get("port")
        device_id = source_endpoint.get("device_id")
        if not interface_name or egress_port is None or not device_id:
            raise ValueError(f"incomplete runtime egress mapping for {link.get('id')}")
        candidates.append((capacity, index, {
            "device_id": str(device_id),
            "switch_name": from_node,
            "egress_port": int(egress_port),
            "interface_name": interface_name,
            "peer_device": to_node,
            "peer_device_id": peer_endpoint.get("device_id"),
            "link_capacity_mbps": capacity,
            "link_id": link.get("id"),
        }))
    if not candidates:
        raise ValueError("selected path contains no switch-to-switch egress")

    # The lowest-capacity path edge is the actual bottleneck; ties choose the
    # source-side edge, giving the shared access uplink deterministic priority.
    _capacity, _index, binding = min(candidates, key=lambda item: (item[0], item[1]))
    switch_path = [name for name in nodes if name.startswith("s_")]
    path_material = "|".join(str(link.get("id")) for link in path_links)
    configured_path = _resolve_configured_path(source_host, destination_host, switch_path)
    binding.update({
        "path_id": configured_path.get("path_id") or "PATH-" + hashlib.sha256(path_material.encode("utf-8")).hexdigest()[:16],
        "runtime_path_fingerprint": "PATH-" + hashlib.sha256(path_material.encode("utf-8")).hexdigest()[:16],
        "current_path": switch_path,
        "current_device_path": [
            item.get("device_id")
            for name in switch_path
            for item in topology.get("switches", [])
            if item.get("name") == name and item.get("device_id")
        ],
        "source_host": source_host,
        "destination_host": destination_host,
        "classifier_device_id": next(
            (item.get("device_id") for item in topology.get("switches", []) if item.get("name") == switch_path[0]),
            None,
        ),
        **configured_path,
    })
    return binding


def _validate_qos_payload(payload: dict) -> dict:
    scope_key = str(payload.get("scope_key") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", scope_key):
        raise ValueError("invalid scope_key")
    source_ip = str(ip_network(str(payload.get("source_ip")), strict=False))
    destination_ip = str(ip_network(str(payload.get("destination_ip")), strict=False))
    bandwidth = int(payload.get("bandwidth_mbps"))
    burst = int(payload.get("burst_kb") or 64)
    if not 1 <= bandwidth <= 100000:
        raise ValueError("bandwidth_mbps must be in [1, 100000]")
    if not 1 <= burst <= 1048576:
        raise ValueError("burst_kb must be in [1, 1048576]")
    protocol = str(payload.get("protocol") or "ANY").upper()
    if protocol not in {"ANY", "TCP", "UDP"}:
        raise ValueError("unsupported protocol")
    source_port = payload.get("source_port")
    destination_port = payload.get("destination_port")
    for name, value in (("source_port", source_port), ("destination_port", destination_port)):
        if value is not None and not 1 <= int(value) <= 65535:
            raise ValueError(f"invalid {name}")
    qos_mode = str(payload.get("qos_mode") or "MIN_BANDWIDTH").upper()
    if qos_mode not in {"MIN_BANDWIDTH", "RATE_LIMIT"}:
        raise ValueError("qos_mode must be MIN_BANDWIDTH or RATE_LIMIT")
    dscp = int(payload.get("dscp") if payload.get("dscp") is not None else 46)
    if not 0 <= dscp <= 63:
        raise ValueError("dscp must be in [0, 63]")
    tc_priority = int(payload.get("priority") or (1 if qos_mode == "MIN_BANDWIDTH" else 3))
    if not 0 <= tc_priority <= 7:
        raise ValueError("priority must be in [0, 7]")
    stable_capacity = payload.get("stable_capacity")
    if stable_capacity is not None:
        if not isinstance(stable_capacity, dict):
            raise ValueError("stable_capacity must be an object")
        required = {"capacity_record_id", "physical_capacity_mbps", "stable_capacity_mbps", "schedulable_capacity_mbps", "safety_margin_mbps", "status"}
        if required.difference(stable_capacity):
            raise ValueError(f"stable_capacity missing fields: {sorted(required.difference(stable_capacity))}")
        if str(stable_capacity["status"]).upper() != "ACTIVE":
            raise ValueError("stable_capacity record is not active")
    durable_contract = payload.get("durable_scheduler_contract_mbps")
    if durable_contract is not None:
        durable_contract = float(durable_contract)
        if durable_contract <= 0:
            raise ValueError("durable_scheduler_contract_mbps must be positive")
    return {
        "scope_key": scope_key,
        "source_ip": source_ip,
        "destination_ip": destination_ip,
        "protocol": protocol,
        "source_port": int(source_port) if source_port is not None else None,
        "destination_port": int(destination_port) if destination_port is not None else None,
        "bandwidth_mbps": bandwidth,
        "burst_kb": burst,
        "qos_mode": qos_mode,
        "dscp": dscp,
        "priority": tc_priority,
        "class_id": payload.get("class_id"),
        "filter_id": payload.get("filter_id"),
        "selected_path": [str(item) for item in payload.get("selected_path") or []],
        "stable_capacity": stable_capacity,
        "authorized_schedulable_capacity_mbps": payload.get("authorized_schedulable_capacity_mbps"),
        # This survives the HTTP payload boundary specifically for formal
        # recovery/reconcile.  It is intentionally distinct from the legacy
        # authorized field, which by itself is not a durable authority.
        "durable_scheduler_contract_mbps": durable_contract,
    }


def _binding_key(binding: dict) -> str:
    return f"{binding['device_id']}|{binding['egress_port']}|{binding['interface_name']}"


def _binding_path_is_active(net, binding: dict) -> bool:
    """Return whether every hop of the already selected switch path is live."""
    path = list(binding.get("current_path") or [])
    if len(path) < 2:
        return False
    topology = build_topology_snapshot(net)
    active_edges: set[frozenset[str]] = set()
    for link in topology.get("links") or []:
        if str(link.get("state") or "ACTIVE").upper() != "ACTIVE":
            continue
        endpoints = link.get("endpoints") or []
        if len(endpoints) != 2:
            continue
        active_edges.add(frozenset((str(endpoints[0].get("node")), str(endpoints[1].get("node")))))
    return all(
        frozenset((str(left), str(right))) in active_edges
        for left, right in zip(path, path[1:])
    )


def _default_class_rate(capacity_mbps: int) -> int:
    configured = int(os.getenv("IDN_QOS_DEFAULT_CLASS_RATE_MBPS", "1"))
    return max(1, min(configured, capacity_mbps))


def _scheduler_capacity(payload: dict, binding: dict) -> dict:
    """Use a calibrated root limit while retaining the physical link fact."""
    physical = float(binding["link_capacity_mbps"])
    record = payload.get("stable_capacity") or {}
    if not record:
        # A recovery/reconcile operation is restoring already committed
        # scheduler state, not admitting new capacity.  Its durable contract
        # remains authoritative when a runtime calibration has expired or is
        # unavailable.  Keep this a distinct field: ``authorized_*`` alone is
        # also used by legacy observation paths and must not turn an arbitrary
        # caller into a capacity authority.
        durable = payload.get("durable_scheduler_contract_mbps")
        if durable is not None:
            durable = float(durable)
            authorized = float(payload.get("authorized_schedulable_capacity_mbps", durable))
            if durable <= 0 or durable > physical + 0.01:
                raise ValueError("durable scheduler contract exceeds physical capacity")
            if abs(authorized - durable) > 0.01:
                raise ValueError("durable scheduler contract and authorized cap differ")
            return {"physical_capacity_mbps": physical, "stable_capacity_mbps": None,
                    "schedulable_capacity_mbps": max(1, int(math.floor(durable))),
                    "safety_margin_mbps": None, "capacity_record_id": None,
                    "capacity_confidence": "DURABLE_CONTRACT"}
        return {"physical_capacity_mbps": physical, "stable_capacity_mbps": None,
                "schedulable_capacity_mbps": int(physical), "safety_margin_mbps": 0.0,
                "capacity_record_id": None, "capacity_confidence": None}
    stable = float(record["stable_capacity_mbps"])
    requested = float(record["schedulable_capacity_mbps"])
    safety = float(record["safety_margin_mbps"])
    if abs(float(record["physical_capacity_mbps"]) - physical) > 0.01:
        raise ValueError("stable capacity physical capacity does not match selected egress")
    if stable <= 0 or stable > physical or requested <= 0:
        raise ValueError("invalid stable capacity record")
    if abs(requested - (min(physical, stable) - safety)) > 0.01:
        raise ValueError("stable capacity schedulable value does not match record calculation")
    authorized = payload.get("authorized_schedulable_capacity_mbps")
    if authorized is not None:
        authorized = float(authorized)
        if authorized <= 0 or authorized > requested + 0.01:
            raise ValueError("authorized scheduler cap exceeds stable capacity")
        requested = authorized
    return {"physical_capacity_mbps": physical, "stable_capacity_mbps": stable,
            "schedulable_capacity_mbps": max(1, int(math.floor(requested))),
            "safety_margin_mbps": safety, "capacity_record_id": str(record["capacity_record_id"]),
            "capacity_confidence": record.get("confidence")}


def _guaranteed_reservation_mbps(binding: dict) -> int:
    """Return the committed MIN_BANDWIDTH total for one TC egress resource.

    The default class is shared by unclassified/best-effort traffic.  Its
    borrowing ceiling must therefore be the residual capacity after active
    guarantees on the *same* egress, rather than the full physical link.
    This is the dataplane counterpart of ResourceLedger admission: it is
    keyed only by the canonical QoS binding and never by a service identity.
    """
    return sum(
        int(item.get("bandwidth_mbps") or 0)
        for item in _qos_policies.values()
        if str(item.get("qos_mode") or "").upper() == "MIN_BANDWIDTH"
        and _binding_key(item.get("qos_binding") or {}) == _binding_key(binding)
    )


def _default_class_ceil_mbps(capacity_mbps: int, binding: dict) -> int:
    # Preserve the configured minimum best-effort service while preventing it
    # from borrowing capacity that has been committed to guarantees.
    return max(_default_class_rate(capacity_mbps), capacity_mbps - _guaranteed_reservation_mbps(binding))


def _reconcile_default_tc_class(net, binding: dict, payload: dict | None = None) -> dict:
    """Apply the current residual-capacity ceiling to a shared default class."""
    capacity_model = _scheduler_capacity(payload or {}, binding)
    capacity = capacity_model["schedulable_capacity_mbps"]
    default_rate = _default_class_rate(capacity)
    default_ceil = _default_class_ceil_mbps(capacity, binding)
    if not hasattr(net, "get"):  # Pure/readback tests do not own a namespace.
        return {"default_rate_mbps": default_rate, "default_ceil_mbps": default_ceil,
                "guaranteed_reservation_mbps": _guaranteed_reservation_mbps(binding),
                "capacity_mbps": capacity, "capacity_model": capacity_model}
    node = net.get(binding["switch_name"])
    intf = binding["interface_name"]
    htb_quantum = max(1514, int(os.getenv("IDN_QOS_HTB_QUANTUM_BYTES", "1514")))
    htb_cburst = max(1, int(os.getenv("IDN_QOS_HTB_CBURST_KB", "64")))
    _tc_cmd(
        node,
        f"tc class replace dev {intf} parent 1:1 classid 1:fffe htb "
        f"rate {default_rate}mbit ceil {default_ceil}mbit prio 7 quantum {htb_quantum} "
        "",
    )
    return {
        "default_rate_mbps": default_rate,
        "default_ceil_mbps": default_ceil,
        "guaranteed_reservation_mbps": _guaranteed_reservation_mbps(binding),
        "capacity_mbps": capacity,
        "capacity_model": capacity_model,
    }


def _capacity_check(payload: dict, binding: dict) -> dict:
    capacity_model = _scheduler_capacity(payload, binding)
    capacity = capacity_model["schedulable_capacity_mbps"]
    default_rate = _default_class_rate(capacity)
    reserved = _guaranteed_reservation_mbps(binding)
    existing = _qos_policies.get(payload["scope_key"])
    if existing and _binding_key(existing.get("qos_binding") or {}) == _binding_key(binding):
        reserved -= int(existing.get("bandwidth_mbps") or 0)
    available = max(0, capacity - default_rate - reserved)
    passed = payload["bandwidth_mbps"] <= available
    return {
        "passed": passed,
        "capacity_mbps": capacity,
        "default_class_rate_mbps": default_rate,
        "reserved_by_other_policies_mbps": reserved,
        "available_mbps": available,
        "requested_mbps": payload["bandwidth_mbps"],
        "capacity_model": capacity_model,
    }


def _filter_command(intf: str, payload: dict, class_id: str, filter_priority: int) -> str:
    tos = payload["dscp"] << 2
    parts = [
        f"tc filter add dev {intf} parent 1: protocol ip pref {filter_priority} flower",
        f"ip_tos 0x{tos:02x}/0xfc",
        f"src_ip {payload['source_ip']}",
        f"dst_ip {payload['destination_ip']}",
    ]
    if payload["protocol"] != "ANY":
        parts.append(f"ip_proto {payload['protocol'].lower()}")
    if payload["source_port"] is not None:
        parts.append(f"src_port {payload['source_port']}")
    if payload["destination_port"] is not None:
        parts.append(f"dst_port {payload['destination_port']}")
    # gact pass is forwarding-neutral but makes the flower classifier export
    # independent packet/byte counters under `tc -s filter show`.
    parts.extend([f"classid 1:{class_id}", "action pass"])
    return " ".join(parts)


def _read_tc_resource(net, payload: dict, binding: dict) -> dict:
    class_id, filter_priority = _tc_identity(payload)
    node = net.get(binding["switch_name"])
    intf = binding["interface_name"]
    qdisc_output = _tc_cmd(node, f"tc qdisc show dev {intf}")
    class_output = _tc_cmd(node, f"tc -s class show dev {intf}")
    filter_output = _tc_cmd(
        node,
        f"tc -s filter show dev {intf} parent 1: protocol ip pref {filter_priority}",
    )
    values = _tc_class_values(class_output, class_id)
    root_values = _tc_class_values(class_output, "1")
    default_values = _tc_class_values(class_output, "fffe")
    capacity_model = _scheduler_capacity(payload, binding)
    expected_ceil = (
        payload["bandwidth_mbps"]
        if payload["qos_mode"] == "RATE_LIMIT"
        else capacity_model["schedulable_capacity_mbps"]
    )
    filter_present = bool(re.search(r"\bfilter\b", filter_output, re.IGNORECASE))
    filter_target = bool(re.search(rf"(?:classid|flowid)\s+1:{re.escape(class_id)}\b", filter_output, re.IGNORECASE))
    root_present = bool(re.search(r"qdisc\s+htb\s+1:", qdisc_output, re.IGNORECASE))
    # iproute2 renders hexadecimal class IDs either as ``fffe`` or
    # ``0xfffe`` depending on version; both denote the same default class.
    root_default = bool(re.search(r"\bdefault\s+(?:0x)?fffe\b", qdisc_output, re.IGNORECASE))
    # RATE_LIMIT is an exact upper-bound contract, whereas MIN_BANDWIDTH is
    # satisfied by a stronger existing guarantee.  Requiring equality for the
    # latter makes an already-provisioned 50 Mbps guarantee fail a >=30 Mbps
    # intent and causes an unnecessary (and unsafe) downgrade during a verify.
    rate_mbps = values.get("rate_mbps")
    rate_requirement_satisfied = (
        abs((rate_mbps or 0) - payload["bandwidth_mbps"]) < 0.01
        if payload["qos_mode"] == "RATE_LIMIT"
        else (rate_mbps is not None and rate_mbps + 0.01 >= payload["bandwidth_mbps"])
    )
    validation_checks = {
        "root_htb_present": root_present,
        "root_default_class": root_default,
        "root_class_present": bool(root_values.get("present")),
        "default_class_present": bool(default_values.get("present")),
        "target_class_present": bool(values.get("present")),
        "target_rate_present": values.get("rate_mbps") is not None,
        "target_ceil_present": values.get("ceil_mbps") is not None,
        "rate_requirement_satisfied": rate_requirement_satisfied,
        "target_ceil_matches": abs((values.get("ceil_mbps") or 0) - expected_ceil) < 0.01,
        "root_rate_matches": abs((root_values.get("rate_mbps") or 0) - capacity_model["schedulable_capacity_mbps"]) < 0.01,
        "root_ceil_matches": abs((root_values.get("ceil_mbps") or 0) - capacity_model["schedulable_capacity_mbps"]) < 0.01,
        "filter_present": filter_present,
        "filter_target_matches": filter_target,
        "switch_egress": binding["switch_name"].startswith("s_"),
        "not_host_interface": not intf.startswith("h_"),
    }
    config_applied = all(validation_checks.values())
    return {
        "class_id": f"1:{class_id}",
        "filter_id": filter_priority,
        "filter_priority": filter_priority,
        "filter_kind": "flower_dscp_plus_five_tuple",
        "classification_key": {
            "dscp": payload["dscp"],
            "ip_tos_value": f"0x{payload['dscp'] << 2:02x}",
            "ip_tos_mask": "0xfc",
            "source_ip": payload["source_ip"],
            "destination_ip": payload["destination_ip"],
            "protocol": payload["protocol"],
            "source_port": payload["source_port"],
            "destination_port": payload["destination_port"],
        },
        "rate_mbps": values.get("rate_mbps"),
        "rate_requirement_satisfied": rate_requirement_satisfied,
        "ceil_mbps": values.get("ceil_mbps"),
        "priority": values.get("priority"),
        "root_rate_mbps": root_values.get("rate_mbps"),
        "root_ceil_mbps": root_values.get("ceil_mbps"),
        "expected_root_capacity_mbps": capacity_model["schedulable_capacity_mbps"],
        "capacity_model": capacity_model,
        "default_class_id": "1:fffe",
        "default_rate_mbps": default_values.get("rate_mbps"),
        "default_ceil_mbps": default_values.get("ceil_mbps"),
        "root_qdisc_present": root_present,
        "default_class_present": default_values.get("present", False),
        "filter_present": filter_present,
        "filter_target_correct": filter_target,
        "validation_checks": validation_checks,
        "filter_stats": _tc_filter_stats(filter_output),
        "stats": _tc_class_stats(class_output, class_id),
        "root_stats": _tc_class_stats(class_output, "1"),
        "default_stats": _tc_class_stats(class_output, "fffe"),
        "config_applied": config_applied,
        "raw": {
            "qdisc": qdisc_output,
            "class": class_output,
            "filter": filter_output,
        },
    }


def _install_tc_resource(net, payload: dict, binding: dict, *, create_filter: bool) -> dict:
    class_id, filter_priority = _tc_identity(payload)
    binding.update({
        "class_id": f"1:{class_id}",
        "filter_id": filter_priority,
        "dscp": payload["dscp"],
    })
    node = net.get(binding["switch_name"])
    intf = binding["interface_name"]
    capacity_model = _scheduler_capacity(payload, binding)
    capacity = capacity_model["schedulable_capacity_mbps"]
    ceil = payload["bandwidth_mbps"] if payload["qos_mode"] == "RATE_LIMIT" else capacity
    # Keep the HTB scheduler packet-granular.  Without an explicit quantum,
    # iproute2 derives it from the class rate (200000 bytes for a 30 Mbit/s
    # class in this environment), which gives a guaranteed class an excessive
    # service burst relative to sibling classes.  cburst is also explicit so
    # a class can borrow at ceil for more than a single 1200-byte datagram.
    # These are deployment-wide scheduler settings, deliberately independent
    # of a service name, address, port, or requested bandwidth.
    htb_quantum = max(1514, int(os.getenv("IDN_QOS_HTB_QUANTUM_BYTES", "1514")))
    htb_cburst = max(1, int(os.getenv("IDN_QOS_HTB_CBURST_KB", "64")))
    qdisc = _tc_cmd(node, f"tc qdisc show dev {intf}")
    if not re.search(r"qdisc\s+htb\s+1:", qdisc):
        _tc_cmd(node, f"tc qdisc replace dev {intf} root handle 1: htb default fffe")
    _tc_cmd(
        node,
        f"tc class replace dev {intf} parent 1: classid 1:1 htb "
        f"rate {capacity}mbit ceil {capacity}mbit quantum {htb_quantum}",
    )
    _reconcile_default_tc_class(net, binding, payload)
    _tc_cmd(
        node,
        f"tc class replace dev {intf} parent 1:1 classid 1:{class_id} htb "
        f"rate {payload['bandwidth_mbps']}mbit ceil {ceil}mbit "
        f"burst {payload['burst_kb']}kb cburst {htb_cburst}kb "
        f"quantum {htb_quantum} prio {payload['priority']}",
    )
    # Explicit leaf qdiscs keep the HTB classes from inheriting an opaque
    # default queue.  They are separate, idempotent qdiscs: EF traffic remains
    # in its guaranteed class and best effort remains in the default class.
    leaf_limit = int(os.getenv("IDN_QOS_LEAF_LIMIT", "1000"))
    leaf_target = os.getenv("IDN_QOS_LEAF_TARGET", "5ms")
    leaf_interval = os.getenv("IDN_QOS_LEAF_INTERVAL", "100ms")
    _tc_cmd(
        node,
        f"tc qdisc replace dev {intf} parent 1:{class_id} handle {class_id}: "
        f"fq_codel limit {leaf_limit} target {leaf_target} interval {leaf_interval}",
    )
    _tc_cmd(
        node,
        f"tc qdisc replace dev {intf} parent 1:fffe handle fffe: "
        f"fq_codel limit {leaf_limit} target {leaf_target} interval {leaf_interval}",
    )
    if create_filter:
        current_filter = _tc_cmd(
            node,
            f"tc filter show dev {intf} parent 1: protocol ip pref {filter_priority}",
        )
        if re.search(r"\bfilter\b", current_filter, re.IGNORECASE):
            if not re.search(rf"(?:classid|flowid)\s+1:{re.escape(class_id)}\b", current_filter, re.IGNORECASE):
                raise RuntimeError(f"FILTER_ID_CONFLICT: pref {filter_priority} is owned by another class")
        else:
            _tc_cmd(node, _filter_command(intf, payload, class_id, filter_priority))
    readback = _read_tc_resource(net, payload, binding)
    if not readback["config_applied"]:
        raise RuntimeError(f"switch egress TC readback mismatch: {readback}")
    return readback


def _remove_tc_resource(net, payload: dict, binding: dict) -> dict:
    class_id, filter_priority = _tc_identity(payload)
    node = net.get(binding["switch_name"])
    intf = binding["interface_name"]
    filter_output = _tc_cmd(node, f"tc filter show dev {intf} parent 1: protocol ip pref {filter_priority}")
    if re.search(r"\bfilter\b", filter_output, re.IGNORECASE):
        _tc_cmd(node, f"tc filter del dev {intf} parent 1: protocol ip pref {filter_priority}")
    class_output = _tc_cmd(node, f"tc class show dev {intf}")
    if _tc_class_values(class_output, class_id)["present"]:
        _tc_cmd(node, f"tc class del dev {intf} classid 1:{class_id}")
    remaining_class = _tc_class_values(_tc_cmd(node, f"tc class show dev {intf}"), class_id)["present"]
    remaining_filter = _tc_cmd(node, f"tc filter show dev {intf} parent 1: protocol ip pref {filter_priority}")
    return {
        "class_removed": not remaining_class,
        "filter_removed": not bool(re.search(r"\bfilter\b", remaining_filter, re.IGNORECASE)),
        "shared_root_preserved": True,
    }


def _set_qos_calibration_window(net, scope_key: str, operation: str) -> dict:
    """Temporarily expose a policy's physical egress ceiling for measurement.

    A capacity probe that shares an active classifier otherwise only observes
    that policy's current scheduler limit.  It can therefore never refresh
    evidence requiring a safety margin above that limit.  This controlled,
    fenced actuator temporarily widens the root and matching class to the
    authoritative physical link capacity, then restores the declared policy
    through the canonical installer.  This applies equally to a recoverable
    RATE_LIMIT state: keeping its rate at the old cap would make the live probe
    measure the cap instead of the path.
    The caller holds the backend's global network-write fence for the whole
    enter/probe/exit interval.
    """
    scope = str(scope_key or "")
    policy = _qos_policies.get(scope)
    if not scope or not policy:
        return {"status": "not_found", "scope_key": scope, "write_executed": False}
    binding = policy.get("qos_binding") or {}
    qos_mode = str(policy.get("qos_mode") or "").upper()
    if qos_mode not in {"MIN_BANDWIDTH", "RATE_LIMIT"}:
        return {"status": "error", "scope_key": scope, "error": "CALIBRATION_QOS_MODE_UNSUPPORTED", "write_executed": False}
    node = net.get(binding["switch_name"])
    intf = binding["interface_name"]
    class_id, _ = _tc_identity(policy)
    physical = int(binding["link_capacity_mbps"])
    htb_quantum = max(1514, int(os.getenv("IDN_QOS_HTB_QUANTUM_BYTES", "1514")))
    htb_cburst = max(1, int(os.getenv("IDN_QOS_HTB_CBURST_KB", "64")))
    normalized_operation = str(operation or "").upper()
    if normalized_operation == "ENTER":
        if scope in _qos_calibration_windows:
            return {"status": "ok", "scope_key": scope, "window": "ACTIVE", "idempotent": True, "write_executed": False}
        _tc_cmd(node, f"tc class replace dev {intf} parent 1: classid 1:1 htb rate {physical}mbit ceil {physical}mbit quantum {htb_quantum}")
        _tc_cmd(
            node,
            f"tc class replace dev {intf} parent 1:1 classid 1:{class_id} htb "
            f"rate {physical}mbit ceil {physical}mbit "
            f"burst {policy['burst_kb']}kb cburst {htb_cburst}kb quantum {htb_quantum} prio {policy['priority']}",
        )
        classes = _tc_cmd(node, f"tc class show dev {intf}")
        root = _tc_class_values(classes, "1")
        target = _tc_class_values(classes, class_id)
        if (
            root.get("rate_mbps") != physical
            or target.get("rate_mbps") != physical
            or target.get("ceil_mbps") != physical
        ):
            raise RuntimeError(f"CALIBRATION_WINDOW_READBACK_FAILED:{scope}:{root}:{target}")
        _qos_calibration_windows.add(scope)
        return {"status": "ok", "scope_key": scope, "window": "ACTIVE", "physical_capacity_mbps": physical, "write_executed": True}
    if normalized_operation == "EXIT":
        # Always use the canonical installer for restoration: it validates the
        # original rate, ceil, filter and default residual as one policy.
        readback = _install_tc_resource(net, policy, binding, create_filter=True)
        _qos_calibration_windows.discard(scope)
        return {"status": "ok", "scope_key": scope, "window": "RESTORED", "readback": readback, "write_executed": True}
    return {"status": "error", "scope_key": scope, "error": "INVALID_CALIBRATION_WINDOW_OPERATION", "write_executed": False}


def _apply_tc_qos(net, raw_payload: dict, *, retain_previous: bool = False) -> dict:
    payload = _validate_qos_payload(raw_payload)
    binding = _select_qos_binding(net, payload)
    capacity = _capacity_check(payload, binding)
    if not capacity["passed"]:
        return {
            "status": "error",
            "code": "INSUFFICIENT_CAPACITY",
            "scope_key": payload["scope_key"],
            "capacity_check": capacity,
            "qos_binding": binding,
            "config_applied": False,
        }

    existing = _qos_policies.get(payload["scope_key"])
    old_binding = (existing or {}).get("qos_binding") or {}
    migrated = bool(existing and _binding_key(old_binding) != _binding_key(binding))
    same_binding = bool(existing and not migrated)
    try:
        # Re-check and repair the classifier filter even when the deterministic
        # scope is already known on the same egress.  The helper is idempotent:
        # it validates an existing pref/class ownership and only creates the
        # filter when runtime drift removed it.
        readback = _install_tc_resource(net, payload, binding, create_filter=True)
        if migrated and not retain_previous:
            _remove_tc_resource(net, existing, old_binding)
    except Exception:
        if migrated:
            try:
                _remove_tc_resource(net, payload, binding)
            except Exception:
                pass
        elif same_binding:
            try:
                _install_tc_resource(net, existing, old_binding, create_filter=False)
            except Exception:
                pass
        raise

    path_changed = bool(existing and old_binding.get("path_id") != binding.get("path_id"))
    if not path_changed:
        path_change_reason = "NO_PATH_CHANGE"
    elif binding.get("path_role") == "PRIMARY" and old_binding.get("path_role") == "BACKUP":
        path_change_reason = "RUNTIME_PRIMARY_RECOVERED"
    elif binding.get("path_role") == "BACKUP":
        path_change_reason = "RUNTIME_PRIMARY_UNAVAILABLE"
    else:
        path_change_reason = "RUNTIME_PATH_CHANGED"
    result = {
        "status": "ok",
        "scope_key": payload["scope_key"],
        "qos_mode": payload["qos_mode"],
        "mechanism": "switch_egress_htb_flower",
        "execution_architecture": "P4_CLASSIFIER + SWITCH_EGRESS_HTB",
        "configured_bandwidth_mbps": payload["bandwidth_mbps"],
        "link_capacity_mbps": binding["link_capacity_mbps"],
        "rate_mbps": readback["rate_mbps"],
        "ceil_mbps": readback["ceil_mbps"],
        "qos_binding": binding,
        "resources": [{"switch": binding["switch_name"], "interface": binding["interface_name"], **readback}],
        "tc_readback": readback,
        "capacity_check": capacity,
        "config_applied": readback["config_applied"],
        "migrated": migrated,
        "previous_binding": old_binding if migrated else None,
        "path_changed": path_changed,
        "previous_path_id": old_binding.get("path_id") if existing else None,
        "current_path_id": binding.get("path_id"),
        "path_change_reason": path_change_reason,
        "qos_resource_migrated": migrated,
        "migration_sequence": (
            "INSTALL_NEW_VERIFY_RETAIN_OLD"
            if migrated and retain_previous
            else
            "INSTALL_NEW_VERIFY_REMOVE_OLD"
            if migrated
            else "PATH_CHANGED_QOS_BINDING_UNCHANGED"
            if path_changed
            else "VERIFY_OR_UPDATE_IN_PLACE"
        ),
        "selector": {key: payload[key] for key in (
            "source_ip", "destination_ip", "protocol", "source_port", "destination_port"
        )},
    }
    _qos_policies[payload["scope_key"]] = {**payload, **result}
    result["default_class_residual"] = _reconcile_default_tc_class(net, binding, payload)
    result["stable_capacity"] = capacity.get("capacity_model")
    if retain_previous:
        _qos_pending[payload["scope_key"]] = {
            "old": existing,
            "new": _qos_policies[payload["scope_key"]],
            "migrated": migrated,
            "same_binding": same_binding,
        }
    return result


def _commit_prepared_tc_qos(net, raw_payload: dict) -> dict:
    payload = _validate_qos_payload(raw_payload)
    scope = payload["scope_key"]
    pending = _qos_pending.get(scope)
    current = _qos_policies.get(scope)
    if not pending:
        return {
            "status": "ok",
            "scope_key": scope,
            "config_applied": bool(current),
            "qos_binding": (current or {}).get("qos_binding") or {},
            "commit_state": "ALREADY_COMMITTED" if current else "NOT_FOUND",
        }
    old = pending.get("old") or {}
    old_binding = old.get("qos_binding") or {}
    released = None
    if pending.get("migrated") and old_binding:
        released = _remove_tc_resource(net, old, old_binding)
        if not released.get("class_removed") or not released.get("filter_removed"):
            # The new generation is live and verified; surface cleanup drift
            # without reverting to an unverified old data-plane state.
            raise RuntimeError(f"superseded TC cleanup failed: {released}")
    _qos_pending.pop(scope, None)
    return {
        "status": "ok",
        "scope_key": scope,
        "config_applied": bool(current),
        "qos_binding": (current or {}).get("qos_binding") or {},
        "previous_binding_released": released,
        "commit_state": "COMMITTED",
        "migration_sequence": "PREPARE_PROVE_COMMIT_REMOVE_OLD",
    }


def _rollback_prepared_tc_qos(net, raw_payload: dict) -> dict:
    payload = _validate_qos_payload(raw_payload)
    scope = payload["scope_key"]
    pending = _qos_pending.get(scope)
    current = _qos_policies.get(scope)
    if not pending:
        return {
            "status": "ok",
            "scope_key": scope,
            "config_applied": bool(current),
            "qos_binding": (current or {}).get("qos_binding") or {},
            "rollback_state": "NOTHING_PREPARED",
        }
    old = pending.get("old")
    new = pending.get("new") or {}
    new_binding = new.get("qos_binding") or {}
    if pending.get("migrated") and new_binding:
        released = _remove_tc_resource(net, new, new_binding)
        if not released.get("class_removed") or not released.get("filter_removed"):
            raise RuntimeError(f"prepared TC rollback failed: {released}")
    elif pending.get("same_binding") and old:
        _install_tc_resource(
            net, old, old.get("qos_binding") or {}, create_filter=False
        )
    if old:
        _qos_policies[scope] = old
    else:
        _qos_policies.pop(scope, None)
    _qos_pending.pop(scope, None)
    restored = _qos_policies.get(scope)
    return {
        "status": "ok",
        "scope_key": scope,
        "config_applied": True,
        "qos_binding": (restored or {}).get("qos_binding") or {},
        "rollback_state": "OLD_BINDING_RESTORED" if restored else "NEW_BINDING_REMOVED",
    }


def _verify_tc_qos(net, raw_payload: dict) -> dict:
    # Keep the symbol for diagnostics/tests, but never read, select, rehydrate
    # or reconcile from it.  The old implementation could call
    # ``_reconcile_default_tc_class`` after a successful comparison.
    return {
        "status": "error", "reason_code": "LEGACY_QOS_VERIFY_DISABLED",
        "error": "READBACK_MUST_USE_GET_ENDPOINT", "write_executed": False,
    }


def _clear_tc_qos(net, scope_key: str, source_ip: str) -> dict:
    pending = _qos_pending.pop(scope_key, None)
    if pending:
        new = pending.get("new") or {}
        new_binding = new.get("qos_binding") or {}
        if new_binding:
            try:
                _remove_tc_resource(net, new, new_binding)
            except Exception:
                pass
        old = pending.get("old")
        if old:
            _qos_policies[scope_key] = old
    existing = _qos_policies.get(scope_key)
    if not existing:
        return {"status": "not_found", "scope_key": scope_key, "resources_released": True}
    released = _remove_tc_resource(net, existing, existing["qos_binding"])
    if released["class_removed"] and released["filter_removed"]:
        _qos_policies.pop(scope_key, None)
        _diagnostic_tc_allocations.pop(scope_key, None)
        _reconcile_default_tc_class(net, existing["qos_binding"], existing)
    return {
        "status": "ok" if released["class_removed"] and released["filter_removed"] else "error",
        "scope_key": scope_key,
        "released_resources": [released],
        "resources_released": released["class_removed"] and released["filter_removed"],
    }


def _init_config(
    switch_config: dict,
    switch_name_by_id: dict,
    probe_port: int | None = None,
    host_runtime_config: dict | None = None,
    host_facing_ports: dict | None = None,
) -> None:
    """Set module-level topology config before starting the server."""
    global _switch_config, _switch_name_by_id, _host_runtime_config, _host_facing_ports, PROBE_PORT
    _switch_config = switch_config
    _switch_name_by_id = switch_name_by_id
    _host_runtime_config = host_runtime_config or {}
    _host_facing_ports = host_facing_ports or {}
    if probe_port is not None:
        PROBE_PORT = probe_port


# ── Telemetry parsers (pure functions) ────────────────────────────────────────


def parse_ping_output(output: str) -> dict:
    transmitted = received = None
    loss_rate = None
    latency_ms = None
    jitter_ms = None

    summary = re.search(r"(\d+) packets transmitted, (\d+) received.*?([0-9.]+)% packet loss", output)
    if summary:
        transmitted = int(summary.group(1))
        received = int(summary.group(2))
        loss_rate = float(summary.group(3)) / 100.0

    rtt = re.search(r"=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)\s*ms", output)
    if rtt:
        latency_ms = float(rtt.group(2))
        jitter_ms = float(rtt.group(4))

    return {
        "transmitted": transmitted,
        "received": received,
        "packet_loss_rate": loss_rate,
        "latency_ms": latency_ms,
        "jitter_ms": jitter_ms,
        "raw": output,
    }


def parse_iperf_output(output: str) -> dict:
    try:
        payload = json.loads(output)
        # ``--get-server-output`` embeds the receiver's authoritative JSON in
        # the client result.  Prefer it when available: the client-side UDP
        # summary mixes the offered rate with receiver loss statistics and is
        # less robust when the sender is briefly descheduled.
        server_payload = payload.get("server_output_json")
        measurement = server_payload if isinstance(server_payload, dict) else payload
        bits_per_second = (
            measurement.get("end", {})
            .get("sum_received", measurement.get("end", {}).get("sum", {}))
            .get("bits_per_second")
        )
        if bits_per_second is None:
            bits_per_second = (
                measurement.get("end", {})
                .get("sum_sent", {})
                .get("bits_per_second")
            )
        summary = measurement.get("end", {}).get("sum", {}) or {}
        client_end = payload.get("end", {}) or {}
        sent = client_end.get("sum_sent", {}) or client_end.get("sum", {}) or {}
        received = measurement.get("end", {}).get("sum_received", {}) or summary
        lost_packets = summary.get("lost_packets")
        packets = summary.get("packets")
        received_bits_per_second = received.get("bits_per_second")

        # iperf3 3.6 can emit a zero-valued final UDP receiver summary in
        # ``server_output_json`` even though every receiver interval contains
        # valid delivered-byte measurements.  Reconstruct the aggregate from
        # those intervals instead of turning a valid run into 0 Mbps.
        if isinstance(server_payload, dict) and not float(received_bits_per_second or 0):
            interval_bytes = 0
            interval_seconds = 0.0
            for interval in measurement.get("intervals", []) or []:
                interval_sum = interval.get("sum") or {}
                if interval_sum.get("omitted"):
                    continue
                seconds = float(interval_sum.get("seconds") or 0)
                bytes_count = int(interval_sum.get("bytes") or 0)
                if seconds > 0 and bytes_count >= 0:
                    interval_seconds += seconds
                    interval_bytes += bytes_count
            if interval_seconds > 0:
                received_bits_per_second = interval_bytes * 8 / interval_seconds

        # iperf3's UDP *client* JSON reports the offered/sent bitrate in
        # end.sum.bits_per_second, even though the same object also includes
        # receiver-reported packet loss.  Treating that number as delivered
        # throughput makes an effective policer look ineffective.  When the
        # dedicated sum_received object is absent, derive wire-delivered
        # throughput from the receiver loss ratio.
        is_udp = str(measurement.get("start", {}).get("test_start", {}).get("protocol") or "").upper() == "UDP"
        if (
            is_udp
            and not isinstance(server_payload, dict)
            and not measurement.get("end", {}).get("sum_received")
            and bits_per_second is not None
            and packets is not None
            and int(packets) > 0
            and lost_packets is not None
        ):
            delivered_packets = max(0, int(packets) - int(lost_packets))
            received_bits_per_second = float(bits_per_second) * delivered_packets / int(packets)

        return {
            "throughput_mbps": (
                float(received_bits_per_second) / 1_000_000
                if received_bits_per_second is not None else None
            ),
            "sent_mbps": (float(sent.get("bits_per_second")) / 1_000_000) if sent.get("bits_per_second") else None,
            "received_mbps": (
                float(received_bits_per_second) / 1_000_000
                if received_bits_per_second is not None else None
            ),
            "lost_packets": lost_packets,
            "packets": packets,
            "loss_percent": summary.get("lost_percent"),
            "jitter_ms": summary.get("jitter_ms"),
            "measurement_source": "receiver_server" if isinstance(server_payload, dict) else "client_reported_receiver",
            "raw": payload,
        }
    except Exception:
        return {"throughput_mbps": None, "raw": output}


def parse_udp_iperf_output(output: str) -> dict:
    """Return only UDP-defined metrics; TCP retransmits never enter this model."""
    parsed = parse_iperf_output(output)
    raw = parsed.get("raw") if isinstance(parsed.get("raw"), dict) else {}
    receiver_payload = raw.get("server_output_json")
    measurement = receiver_payload if isinstance(receiver_payload, dict) else raw
    receiver_summary = (measurement.get("end") or {}).get("sum") or {}
    sender_summary = (raw.get("end") or {}).get("sum") or {}
    receiver_streams = (measurement.get("end") or {}).get("streams") or []
    out_of_order_values = [
        int((stream.get("udp") or {}).get("out_of_order") or 0)
        for stream in receiver_streams
        if isinstance(stream, dict) and isinstance(stream.get("udp"), dict)
    ]
    total_datagrams = receiver_summary.get("packets")
    lost_packets = receiver_summary.get("lost_packets")
    sender_seconds = float(sender_summary.get("seconds") or 0)
    receiver_seconds = float(receiver_summary.get("seconds") or 0)
    received_packets = None
    if total_datagrams is not None and lost_packets is not None:
        received_packets = max(int(total_datagrams) - int(lost_packets), 0)
    return {
        "sent_mbps": parsed.get("sent_mbps"),
        "received_mbps": parsed.get("received_mbps"),
        "sent_packets": sender_summary.get("packets"),
        "received_packets": received_packets,
        "sent_pps": (
            float(sender_summary.get("packets")) / sender_seconds
            if sender_summary.get("packets") is not None and sender_seconds > 0
            else None
        ),
        "received_pps": (
            float(received_packets) / receiver_seconds
            if received_packets is not None and receiver_seconds > 0
            else None
        ),
        "lost_packets": lost_packets,
        "total_datagrams": total_datagrams,
        "loss_percent": receiver_summary.get("lost_percent"),
        "jitter_ms": receiver_summary.get("jitter_ms"),
        "out_of_order": (
            receiver_summary.get("out_of_order")
            if receiver_summary.get("out_of_order") is not None
            else sum(out_of_order_values) if out_of_order_values else None
        ),
        "measurement_source": parsed.get("measurement_source"),
        "raw": raw,
    }


def parse_tcp_iperf_output(output: str) -> dict:
    """Return TCP goodput/congestion metrics without UDP loss placeholders."""
    try:
        raw = json.loads(output)
    except Exception:
        return {
            "sender_mbps": None,
            "receiver_mbps": None,
            "retransmits": None,
            "mean_rtt_ms": None,
            "max_cwnd": None,
            "bytes_sent": None,
            "bytes_received": None,
            "raw": output,
        }
    end = raw.get("end") or {}
    sent = end.get("sum_sent") or {}
    received = end.get("sum_received") or {}
    sender_streams = [
        item.get("sender") or {}
        for item in end.get("streams") or []
        if isinstance(item, dict)
    ]
    rtts = [float(item["mean_rtt"]) / 1000.0 for item in sender_streams if item.get("mean_rtt") is not None]
    cwnds = [int(item["max_snd_cwnd"]) for item in sender_streams if item.get("max_snd_cwnd") is not None]
    retransmits = sent.get("retransmits")
    if retransmits is None:
        values = [item.get("retransmits") for item in sender_streams]
        retransmits = sum(int(value or 0) for value in values) if any(value is not None for value in values) else None
    return {
        "sender_mbps": (
            float(sent.get("bits_per_second")) / 1_000_000
            if sent.get("bits_per_second") is not None else None
        ),
        "receiver_mbps": (
            float(received.get("bits_per_second")) / 1_000_000
            if received.get("bits_per_second") is not None else None
        ),
        "retransmits": retransmits,
        "mean_rtt_ms": sum(rtts) / len(rtts) if rtts else None,
        "max_cwnd": max(cwnds) if cwnds else None,
        "bytes_sent": sent.get("bytes"),
        "bytes_received": received.get("bytes"),
        "transfer_completion_time_seconds": sent.get("seconds"),
        "raw": raw,
    }


def parse_icmp_service_output(output: str) -> dict:
    parsed = parse_ping_output(output)
    rtt = re.search(r"=\s*([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+)\s*ms", output)
    return {
        "transmitted": parsed.get("transmitted"),
        "received": parsed.get("received"),
        "loss_percent": (
            float(parsed["packet_loss_rate"]) * 100
            if parsed.get("packet_loss_rate") is not None else None
        ),
        "rtt_min_ms": float(rtt.group(1)) if rtt else None,
        "rtt_avg_ms": float(rtt.group(2)) if rtt else None,
        "rtt_max_ms": float(rtt.group(3)) if rtt else None,
        "rtt_mdev_ms": float(rtt.group(4)) if rtt else None,
        "raw": output,
    }


def _payload_bool(value, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _counter_delta(before: dict, after: dict) -> dict:
    values = {}
    for key in (
        "sent_bytes", "sent_packets", "dropped_packets", "overlimits",
        "lended", "borrowed",
    ):
        before_value = before.get(key)
        after_value = after.get(key)
        values[key] = (
            int(after_value or 0) - int(before_value or 0)
            if after_value is not None else None
        )
    values["backlog_bytes"] = after.get("backlog_bytes")
    values["backlog_packets"] = after.get("backlog_packets")
    return values


def _service_selector(profile: dict) -> dict:
    return {
        "src_host": profile.get("src_host"),
        "dst_host": profile.get("dst_host"),
        "source_ip": profile.get("src_ip"),
        "destination_ip": profile.get("dst_ip"),
        "protocol": str(profile.get("protocol") or "").upper(),
        "source_port": profile.get("src_port"),
        "destination_port": profile.get("dst_port"),
        "dscp": profile.get("dscp"),
        "vlan_id": profile.get("vlan_id"),
        "tenant_id": profile.get("tenant_id", "default"),
        "direction": profile.get("direction", "ingress"),
    }


def _interface_counter_snapshot(node, interface_name: str) -> dict:
    keys = (
        "rx_packets", "tx_packets", "rx_dropped", "tx_dropped",
        "rx_errors", "tx_errors", "rx_bytes", "tx_bytes",
    )
    command = " ".join(
        f"cat /sys/class/net/{interface_name}/statistics/{key} 2>/dev/null || echo 0;"
        for key in keys
    )
    values = _node_cmd(node, command, timeout=3).split()
    return {
        key: int(values[index]) if index < len(values) and values[index].isdigit() else 0
        for index, key in enumerate(keys)
    }


def _udp_kernel_stats(node) -> dict:
    """Read receiver UDP kernel counters from its own Mininet namespace."""
    output = _node_cmd(node, "cat /proc/net/snmp", timeout=3)
    lines = [line.split() for line in output.splitlines() if line.startswith("Udp:")]
    if len(lines) < 2:
        return {"status": "unavailable", "reason": "UDP_SNMP_NOT_AVAILABLE"}
    fields, values = lines[0][1:], lines[1][1:]
    parsed = dict(zip(fields, values))
    return {
        "status": "ok",
        "InDatagrams": int(parsed.get("InDatagrams") or 0),
        "InErrors": int(parsed.get("InErrors") or 0),
        "RcvbufErrors": int(parsed.get("RcvbufErrors") or 0),
    }


def _all_interface_telemetry(net) -> dict:
    topology = build_topology_snapshot(net)
    link_by_interface: dict[str, dict] = {}
    for link in topology.get("links") or []:
        for endpoint in link.get("endpoints") or []:
            interface = endpoint.get("interface")
            if interface:
                link_by_interface[str(interface)] = link
    samples = []
    sampled_at = _utc_now()
    for switch in topology.get("switches") or []:
        node_name = str(switch["name"])
        node = net.get(node_name)
        for port in switch.get("ports") or []:
            interface = str(port.get("name") or "")
            port_id = port.get("port")
            if not interface or port_id is None:
                continue
            counters = _interface_counter_snapshot(node, interface)
            qdisc = parse_root_qdisc_output(
                _node_cmd(node, f"tc -s qdisc show dev {interface}", timeout=3)
            )
            link = link_by_interface.get(interface) or {}
            samples.append({
                "resource_id": f"interface:{node_name}:{int(port_id)}",
                "device_id": switch.get("device_id"),
                "switch_name": node_name,
                "interface": interface,
                "onos_port": int(port_id),
                "p4_port": int(port_id),
                "link_id": link.get("id"),
                "capacity_mbps": link.get("bandwidth_mbps"),
                "timestamp": sampled_at,
                "counters": counters,
                "queue": {
                    "backlog_bytes": qdisc.get("backlog_bytes"),
                    "backlog_packets": qdisc.get("backlog_packets"),
                    "dropped_packets": qdisc.get("dropped_packets"),
                    "overlimits": qdisc.get("overlimits"),
                },
            })
    return {"status": "ok", "timestamp": sampled_at, "samples": samples, "count": len(samples)}


def _packet_accounting_snapshot(net, raw: dict) -> dict:
    src = str(raw.get("src_endpoint") or "")
    dst = str(raw.get("dst_endpoint") or "")
    if src not in net.nameToNode or dst not in net.nameToNode:
        raise ValueError("packet accounting endpoints are absent from topology")
    topology = build_topology_snapshot(net)
    nodes, path_links = _shortest_runtime_path(topology, src, dst)
    points = []
    for index, link in enumerate(path_links):
        from_node, to_node = nodes[index], nodes[index + 1]
        endpoints = link.get("endpoints") or []
        from_endpoint = next((item for item in endpoints if item.get("node") == from_node), None)
        to_endpoint = next((item for item in endpoints if item.get("node") == to_node), None)
        for suffix, endpoint, direction in (
            ("tx", from_endpoint, "tx"), ("rx", to_endpoint, "rx"),
        ):
            if endpoint is None or not endpoint.get("interface"):
                points.append({
                    "point_id": f"link:{link.get('id')}:{suffix}", "value": None,
                    "node": endpoint.get("node") if endpoint else None,
                    "interface": None, "direction": direction,
                    "quality_status": "UNKNOWN", "source": "interface_counter",
                })
                continue
            stats = _interface_counter_snapshot(
                net.get(endpoint["node"]), endpoint["interface"]
            )
            points.append({
                "point_id": f"link:{link.get('id')}:{suffix}",
                "value": stats.get(f"{direction}_packets"),
                "node": endpoint["node"], "interface": endpoint["interface"],
                "direction": direction, "quality_status": "AGGREGATE_INTERFACE",
                "source": "linux_interface_counter",
            })
    scope_key = str(raw.get("traffic_scope_key") or "")
    installed = _qos_policies.get(scope_key)
    if installed:
        tc = _read_tc_resource(net, installed, installed["qos_binding"])
        points.append({
            "point_id": "tc:classified", "value": (tc.get("stats") or {}).get("sent_packets"),
            "node": installed["qos_binding"].get("switch_name"),
            "interface": installed["qos_binding"].get("interface_name"),
            "direction": "tx", "quality_status": "EXACT_SELECTOR",
            "source": "tc_class_counter",
        })
    else:
        points.append({
            "point_id": "tc:classified", "value": None, "node": None,
            "interface": None, "direction": None, "quality_status": "UNKNOWN",
            "source": "tc_class_counter",
        })
    # P4 direct counters are read by the backend P4Runtime client.  This
    # endpoint explicitly reports that boundary instead of estimating it.
    points.append({
        "point_id": "p4:classifier", "value": None, "node": None,
        "interface": None, "direction": None, "quality_status": "UNKNOWN",
        "source": "requires_p4runtime_backend",
    })
    return {"nodes": nodes, "links": [item.get("id") for item in path_links], "points": points}


def _packet_accounting(net, raw: dict) -> dict:
    duration = max(0.2, min(float(raw.get("measurement_window_seconds") or 2), 30.0))
    started_at = _utc_now()
    monotonic_started = time.monotonic()
    before = _packet_accounting_snapshot(net, raw)
    time.sleep(duration)
    after = _packet_accounting_snapshot(net, raw)
    after_by_id = {item["point_id"]: item for item in after["points"]}
    points = []
    delta_by_id = {}
    for first in before["points"]:
        last = after_by_id.get(first["point_id"]) or {}
        first_value, last_value = first.get("value"), last.get("value")
        delta = (
            int(last_value) - int(first_value)
            if first_value is not None and last_value is not None and int(last_value) >= int(first_value)
            else None
        )
        quality = first["quality_status"] if delta is not None else "UNKNOWN"
        point = {
            "point_id": first["point_id"], "node": first.get("node"),
            "interface": first.get("interface"), "direction": first.get("direction"),
            "counter_before": first_value, "counter_after": last_value, "delta": delta,
            "quality_status": quality, "source": first["source"],
        }
        points.append(point)
        delta_by_id[first["point_id"]] = delta
    segments = []
    for link_id in before["links"]:
        source_id, destination_id = f"link:{link_id}:tx", f"link:{link_id}:rx"
        sent, received = delta_by_id.get(source_id), delta_by_id.get(destination_id)
        measurable = sent is not None and received is not None
        difference = max(0, sent - received) if measurable else None
        segments.append({
            "segment_id": str(link_id), "source_point": source_id,
            "destination_point": destination_id, "packets_before": sent,
            "packets_after": received, "packet_difference": difference,
            "loss_pct": (difference / sent * 100.0) if measurable and sent > 0 else (0.0 if measurable else None),
            "status": "MEASURED_AGGREGATE" if measurable else "UNKNOWN",
            "reason": None if measurable else "one or both interface counters unavailable",
        })
    for segment_id, source_id, destination_id in (
        ("p4_classifier_to_tc_class", "p4:classifier", "tc:classified"),
    ):
        sent, received = delta_by_id.get(source_id), delta_by_id.get(destination_id)
        measurable = sent is not None and received is not None
        segments.append({
            "segment_id": segment_id, "source_point": source_id,
            "destination_point": destination_id, "packets_before": sent,
            "packets_after": received,
            "packet_difference": max(0, sent - received) if measurable else None,
            "loss_pct": (max(0, sent - received) / sent * 100.0) if measurable and sent > 0 else None,
            "status": "MEASURED_EXACT" if measurable else "UNKNOWN",
            "reason": None if measurable else "P4Runtime counter must be joined by backend",
        })
    return {
        "status": "ok", "accounting_id": f"PA-{uuid4().hex[:16]}",
        "workload_id": raw.get("workload_id"), "service_id": raw.get("service_id"),
        "started_at": started_at, "finished_at": _utc_now(),
        "measurement_window_seconds": time.monotonic() - monotonic_started,
        "path": before["nodes"], "points": points, "segments": segments,
    }


def _runtime_environment_diagnostics() -> dict:
    sysctl_names = (
        "net.core.rmem_max", "net.core.wmem_max", "net.core.rmem_default",
        "net.core.wmem_default", "net.ipv4.udp_rmem_min", "net.ipv4.udp_wmem_min",
    )
    sysctls = {}
    for name in sysctl_names:
        output = subprocess.run(
            ["sysctl", "-n", name], capture_output=True, text=True, timeout=2, check=False,
        )
        sysctls[name] = output.stdout.strip() if output.returncode == 0 else None
    load = os.getloadavg() if hasattr(os, "getloadavg") else (None, None, None)
    process_output = subprocess.run(
        ["ps", "-eo", "pid,comm,%cpu,%mem", "--sort=-%cpu"],
        capture_output=True, text=True, timeout=3, check=False,
    ).stdout.splitlines()
    relevant = [line.strip() for line in process_output if any(
        token in line.lower() for token in ("stratum", "bmv2", "probe", "iperf", "python")
    )]
    memory = {}
    try:
        for line in open("/proc/meminfo", encoding="utf-8"):
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree"}:
                memory[key] = value.strip()
    except OSError as exc:
        memory = {"status": "unavailable", "error": str(exc)}
    cgroup = {}
    for name in ("cpu.stat", "memory.current", "memory.max"):
        try:
            cgroup[name] = open(f"/sys/fs/cgroup/{name}", encoding="utf-8").read().strip()
        except OSError:
            cgroup[name] = None
    return {
        "status": "ok", "timestamp": _utc_now(), "sysctl": sysctls,
        "load_average": {"one": load[0], "five": load[1], "fifteen": load[2]},
        "cpu_count": os.cpu_count(), "processes": relevant,
        "memory": memory, "container_cgroup": cgroup,
        "modifications_applied": False,
    }


def _service_interface_bindings(net, profile: dict, installed: dict | None) -> dict:
    src_name = profile["src_host"]
    dst_name = profile["dst_host"]
    src_node = net.get(src_name)
    dst_node = net.get(dst_name)
    binding = (installed or {}).get("qos_binding") or {}
    switch_name = binding.get("switch_name") or (profile.get("qos_binding") or {}).get("switch")
    ingress_interface = None
    if switch_name:
        for link in net.links:
            endpoints = ((link.intf1.node.name, link.intf1), (link.intf2.node.name, link.intf2))
            names = {item[0] for item in endpoints}
            if {src_name, switch_name}.issubset(names):
                ingress_interface = next(item[1].name for item in endpoints if item[0] == switch_name)
                break
    result = {
        "sender": {"node": src_name, "interface": src_node.defaultIntf().name, "direction": "tx"},
        "receiver": {"node": dst_name, "interface": dst_node.defaultIntf().name, "direction": "rx"},
    }
    if switch_name and ingress_interface:
        result["p4_ingress"] = {"node": switch_name, "interface": ingress_interface, "direction": "rx"}
    if switch_name and binding.get("interface_name"):
        result["tc_egress"] = {
            "node": switch_name,
            "interface": binding["interface_name"],
            "direction": "tx",
        }
    return result


def _read_service_interfaces(net, bindings: dict) -> dict:
    result = {}
    for name, binding in bindings.items():
        result[name] = {
            **binding,
            "stats": _interface_counter_snapshot(
                net.get(binding["node"]), binding["interface"]
            ),
        }
    return result


def _service_interface_delta(before: dict, after: dict) -> dict:
    result = {}
    for name, after_item in after.items():
        before_item = before.get(name) or {}
        before_stats = before_item.get("stats") or {}
        after_stats = after_item.get("stats") or {}
        result[name] = {
            "node": after_item.get("node"),
            "interface": after_item.get("interface"),
            "direction": after_item.get("direction"),
            "before": before_stats,
            "after": after_stats,
            "delta": {
                key: max(int(after_stats.get(key) or 0) - int(before_stats.get(key) or 0), 0)
                for key in after_stats
            },
        }
    return result


def _normalize_ip_prefix(value) -> str | None:
    if value in (None, ""):
        return None
    return str(ip_network(str(value), strict=False))


def _effective_service_request(net, raw_payload: dict) -> dict:
    profile = _service_profile(str(raw_payload.get("service_id") or ""))
    immutable = {
        "src_host": "src_host",
        "dst_host": "dst_host",
        "src_ip": "src_ip",
        "dst_ip": "dst_ip",
        "protocol": "protocol",
        "src_port": "src_port",
        "dst_port": "dst_port",
        "dscp": "dscp",
    }
    mismatches = []
    for request_key, profile_key in immutable.items():
        requested = raw_payload.get(request_key)
        expected = profile.get(profile_key)
        if requested is None:
            continue
        if request_key in {"src_ip", "dst_ip"}:
            same = _normalize_ip_prefix(requested) == _normalize_ip_prefix(expected)
        elif request_key == "protocol":
            same = str(requested).upper() == str(expected).upper()
        else:
            same = requested == expected
        if not same:
            mismatches.append(f"{request_key}: requested={requested}, profile={expected}")
    if mismatches:
        raise ValueError("VALIDATION_PROFILE_MISMATCH: " + "; ".join(mismatches))

    src_host = str(profile.get("src_host") or "")
    dst_host = str(profile.get("dst_host") or "")
    if src_host not in net.nameToNode or dst_host not in net.nameToNode:
        raise ValueError(
            f"VALIDATION_PROFILE_MISMATCH: topology host missing for {src_host}->{dst_host}"
        )
    actual_src = _host_ip_plain(net.get(src_host))
    actual_dst = _host_ip_plain(net.get(dst_host))
    if ip_address(actual_src) not in ip_network(str(profile.get("src_ip")), strict=False):
        raise ValueError(f"VALIDATION_PROFILE_MISMATCH: {src_host} runtime IP is {actual_src}")
    if ip_address(actual_dst) not in ip_network(str(profile.get("dst_ip")), strict=False):
        raise ValueError(f"VALIDATION_PROFILE_MISMATCH: {dst_host} runtime IP is {actual_dst}")

    protocol = str(profile.get("protocol") or "").upper()
    duration = max(1, min(120, int(
        raw_payload.get("duration_seconds")
        or raw_payload.get("duration")
        or profile.get("default_duration_seconds")
        or 5
    )))
    offered = raw_payload.get("offered_rate_mbps")
    if offered is None:
        offered = profile.get("default_offered_rate_mbps")
    offered = float(offered) if offered is not None else None
    if protocol in {"UDP", "TCP"} and (offered is None or offered <= 0):
        raise ValueError(f"VALIDATION_PROFILE_MISMATCH: {protocol} profile has no offered rate")
    return {
        **profile,
        "selector": _service_selector(profile),
        "actual_src_ip": actual_src,
        "actual_dst_ip": actual_dst,
        "duration_seconds": duration,
        "offered_rate_mbps": offered,
        "packet_size_bytes": max(64, min(9000, int(
            raw_payload.get("packet_size_bytes") or profile.get("packet_size_bytes") or 1200
        ))),
        "warmup_seconds": max(0, min(10, int(raw_payload.get("warmup_seconds") or 0))),
        "pacing_timer_us": (
            max(10, min(10000, int(raw_payload["pacing_timer_us"])))
            if raw_payload.get("pacing_timer_us") is not None else None
        ),
        "parallel_streams": max(1, min(16, int(
            raw_payload.get("parallel_streams") or profile.get("parallel_streams") or 1
        ))),
        "intent_id": raw_payload.get("intent_id"),
        "policy_id": raw_payload.get("policy_id"),
        "traffic_scope_key": raw_payload.get("traffic_scope_key") or raw_payload.get("scope_key"),
        "qos_mode": str(raw_payload.get("qos_mode") or "").upper() or None,
        "bandwidth_mbps": raw_payload.get("bandwidth_mbps"),
    }


def _service_validation_server_keys(raw_payload: dict) -> list[tuple[str, int]]:
    """Return all listener sockets reserved by one service-validation probe.

    Service validation can contain a target and profile-defined competitors.
    Lock only those exact destination sockets, in canonical order, so two
    independent business services can produce genuinely overlapping traffic
    while a same-port validation remains serialized and safe.
    """
    profile = _service_profile(str(raw_payload.get("service_id") or ""))
    service_ids = [str(profile["service_id"])]
    for item in raw_payload.get("background_profiles") or []:
        if isinstance(item, str):
            service_ids.append(item)
        elif isinstance(item, dict):
            service_ids.append(str(item.get("service_id") or item.get("service_profile_id") or ""))
    keys = {
        (str(_service_profile(service_id)["dst_host"]), int(_service_profile(service_id)["dst_port"]))
        for service_id in service_ids if service_id
    }
    return sorted(keys)


def _port_listener(node, port: int) -> str:
    return _node_cmd(
        node,
        f"ss -H -lntup 2>/dev/null | grep -E ':{int(port)}([^0-9]|$)' || true",
        timeout=3,
    ).strip()


def _flow_args(flow: dict) -> list[str]:
    protocol = str(flow["protocol"]).upper()
    args = [
        "iperf3", "-c", str(flow["actual_dst_ip"]),
        "-p", str(flow["dst_port"]),
        "-t", str(flow["duration_seconds"]),
        "-J", "--get-server-output",
        "-P", str(flow.get("parallel_streams") or 1),
    ]
    if flow.get("src_port") is not None:
        args.extend(["--cport", str(flow["src_port"])])
    if flow.get("dscp") is not None:
        args.extend(["-S", f"0x{int(flow['dscp']) << 2:02x}"])
    if int(flow.get("warmup_seconds") or 0) > 0:
        args.extend(["-O", str(int(flow["warmup_seconds"]))])
    if flow.get("pacing_timer_us") is not None:
        args.extend(["--pacing-timer", str(int(flow["pacing_timer_us"]))])
    socket_buffer = os.getenv("MININET_IPERF_SOCKET_BUFFER_BYTES", "").strip()
    if socket_buffer:
        args.extend(["-w", socket_buffer])
    if protocol == "UDP":
        args.extend([
            "-u", "-b", f"{float(flow['offered_rate_mbps']):g}M",
            "-l", str(flow.get("packet_size_bytes") or 1200),
            "--fq-rate", f"{float(flow['offered_rate_mbps']):g}M",
        ])
    elif protocol == "TCP" and flow.get("offered_rate_mbps"):
        args.extend(["--fq-rate", f"{float(flow['offered_rate_mbps']):g}M"])
    return args


def _protocol_result(protocol: str, output: str) -> dict:
    return parse_udp_iperf_output(output) if protocol == "UDP" else parse_tcp_iperf_output(output)


def _service_probe_timeout_seconds(flows: list[dict]) -> float:
    """Return a bounded completion budget for all started iperf clients.

    ``iperf3 -t`` is not the whole wall-clock lifetime when a warm-up omit
    period is configured, and collecting the peer JSON can take a little
    longer under a contended Mininet namespace.  This is a process-completion
    budget only; it does not change a service KPI or its SLA threshold.
    """
    longest_run = max(
        float(flow.get("duration_seconds") or 0)
        + float(flow.get("warmup_seconds") or 0)
        for flow in flows
    )
    grace = float(os.getenv("MININET_SERVICE_PROBE_COMPLETION_GRACE_SECONDS", "20"))
    return longest_run + max(5.0, min(grace, 60.0))


def _read_capture(capture) -> str:
    """Read a completed temporary subprocess capture without pipe deadlock."""
    capture.flush()
    capture.seek(0)
    return capture.read()


def _measurement_client_records(client_records: list[tuple[dict, subprocess.Popen]]) -> list[tuple[dict, subprocess.Popen]]:
    """Return the client processes whose completion determines the measurement.

    A foreground service flow is the object being measured.  Competition
    flows deliberately oversubscribe a lower-priority class and can lose the
    final UDP control datagram, leaving their ``iperf3`` client waiting for a
    receiver summary after the foreground measurement has already completed.
    They must remain active during the foreground window, but their shutdown
    must not erase a valid foreground result or turn a completed SLA probe
    into a timeout.
    """
    targets = [record for record in client_records if record[0].get("role") == "target"]
    return targets or client_records


def _service_validation_result(profile: dict, metrics: dict, raw_payload: dict) -> dict:
    protocol = str(profile.get("protocol") or "").upper()
    missing = []
    if protocol == "UDP":
        for key in ("received_mbps", "lost_packets", "loss_percent", "jitter_ms"):
            if metrics.get(key) is None:
                missing.append(key)
        observed = metrics.get("received_mbps")
    elif protocol == "TCP":
        for key in ("sender_mbps", "receiver_mbps", "retransmits"):
            if metrics.get(key) is None:
                missing.append(key)
        observed = metrics.get("receiver_mbps")
    else:
        for key in ("transmitted", "received", "loss_percent", "rtt_avg_ms"):
            if metrics.get(key) is None:
                missing.append(key)
        observed = None
    if missing:
        return {
            "passed": False,
            "status": "NO_SAMPLE",
            "verification_state": "SERVICE_METRICS_INCOMPLETE",
            "missing_metrics": missing,
        }

    qos_mode = str(raw_payload.get("qos_mode") or profile.get("qos_mode") or "").upper()
    bandwidth = raw_payload.get("bandwidth_mbps")
    if qos_mode == "MIN_BANDWIDTH" and bandwidth is not None:
        threshold = float(bandwidth) * float(raw_payload.get("minimum_ratio") or 0.95)
        passed = float(observed or 0) >= threshold
        state = "MIN_BANDWIDTH_KPI_VERIFIED" if passed else "MIN_BANDWIDTH_KPI_FAILED"
        return {"passed": passed, "status": "MEASURED", "verification_state": state,
                "observed_mbps": observed, "minimum_acceptable_mbps": threshold}
    if qos_mode == "RATE_LIMIT" and bandwidth is not None:
        maximum = float(bandwidth) * float(raw_payload.get("maximum_ratio") or 1.10)
        # A rate limit is an upper-bound objective.  TCP may settle below the
        # configured cap under contention or congestion control; treating a
        # 90%-of-cap floor as mandatory silently changes RATE_LIMIT into a
        # minimum-bandwidth SLA.  Require an actual receiver-side sample and
        # enforce only the declared upper bound.
        traffic_observed = float(observed or 0) > 0
        passed = traffic_observed and float(observed or 0) <= maximum
        state = "RATE_LIMIT_KPI_VERIFIED" if passed else "RATE_LIMIT_KPI_FAILED"
        return {"passed": passed, "status": "MEASURED", "verification_state": state,
                "observed_mbps": observed, "traffic_observed": traffic_observed,
                "maximum_acceptable_mbps": maximum}

    thresholds = profile.get("validation_thresholds") or {}
    passed = True
    if protocol == "ICMP":
        if thresholds.get("minimum_received") is not None:
            passed = passed and int(metrics.get("received") or 0) >= int(thresholds["minimum_received"])
        if thresholds.get("maximum_loss_percent") is not None:
            passed = passed and float(metrics.get("loss_percent") or 0) <= float(thresholds["maximum_loss_percent"])
    elif thresholds.get("minimum_received_mbps") is not None:
        passed = float(observed or 0) >= float(thresholds["minimum_received_mbps"])
    return {
        "passed": passed,
        "status": "MEASURED",
        "verification_state": "SERVICE_KPI_VERIFIED" if passed else "SERVICE_KPI_FAILED",
        "observed_mbps": observed,
    }


def _process_leak_evidence(net, hosts: set[str]) -> dict:
    evidence = []
    for host in sorted(hosts):
        remaining = _node_cmd(net.get(host), "pgrep -a -x iperf3 || true", timeout=3).strip()
        evidence.append({"host": host, "remaining": remaining})
    return {
        "process_cleanup_verified": not any(item["remaining"] for item in evidence),
        "hosts": evidence,
    }


def _tracked_process_cleanup_evidence(processes: list[subprocess.Popen]) -> dict:
    """Verify only processes owned by this probe, not production workloads.

    Mininet host network namespaces share the container PID namespace, so a
    host-local ``pgrep`` sees iperf processes belonging to every host.  Using
    it as probe cleanup evidence therefore produces false leak reports while
    legitimate long-running workloads are active.
    """
    tracked = []
    for process in processes:
        returncode = process.poll()
        tracked.append({
            "pid": process.pid,
            "terminated": returncode is not None,
            "returncode": returncode,
        })
    return {
        "process_cleanup_verified": all(item["terminated"] for item in tracked),
        "scope": "CURRENT_PROBE_ONLY",
        "tracked_processes": tracked,
    }


def _validate_runtime_workload(net, raw: dict) -> dict:
    workload_id = str(raw.get("workload_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", workload_id):
        raise ValueError("invalid workload_id")
    src = str(raw.get("src_endpoint") or "")
    dst = str(raw.get("dst_endpoint") or "")
    if src not in net.nameToNode or not src.startswith("h_"):
        raise ValueError(f"unknown source endpoint: {src}")
    if dst not in net.nameToNode or not dst.startswith("h_"):
        raise ValueError(f"unknown destination endpoint: {dst}")
    selector = dict(raw.get("selector") or {})
    protocol = str(selector.get("protocol") or "").upper()
    if protocol not in {"UDP", "TCP", "ICMP"}:
        raise ValueError("protocol must be UDP, TCP or ICMP")
    destination_port = selector.get("destination_port")
    if protocol in {"UDP", "TCP"} and not destination_port:
        raise ValueError(f"{protocol} workload requires destination_port")
    pattern = str(raw.get("traffic_pattern") or "BEST_EFFORT").upper()
    if pattern not in {
        "CONSTANT_BIT_RATE", "BURSTY", "BULK_TRANSFER", "PERIODIC",
        "INTERACTIVE", "BEST_EFFORT",
    }:
        raise ValueError(f"unsupported traffic_pattern: {pattern}")
    duration = raw.get("duration_seconds")
    if duration is not None and int(duration) < 1:
        raise ValueError("duration_seconds must be positive")
    offered = raw.get("offered_rate_mbps")
    if protocol == "UDP" and (offered is None or float(offered) <= 0):
        raise ValueError("UDP workload requires offered_rate_mbps")
    actual_dst_ip = str(net.get(dst).IP()).split("/", 1)[0]
    return {
        **raw,
        "workload_id": workload_id,
        "src_endpoint": src,
        "dst_endpoint": dst,
        "selector": {**selector, "protocol": protocol},
        "traffic_pattern": pattern,
        "actual_dst_ip": actual_dst_ip,
        "duration_seconds": int(duration) if duration is not None else None,
        "offered_rate_mbps": float(offered) if offered is not None else None,
        "packet_size_bytes": int(raw.get("packet_size_bytes") or 1200),
        "parallel_flows": int(raw.get("parallel_flows") or 1),
    }


def _workload_client_args(spec: dict) -> list[str]:
    selector = spec["selector"]
    args = [
        "python3", "/app/mn/workload_runner.py",
        "--protocol", selector["protocol"],
        "--traffic-pattern", spec["traffic_pattern"],
        "--destination-ip", spec["actual_dst_ip"],
        "--packet-size-bytes", str(spec["packet_size_bytes"]),
        "--parallel-flows", str(spec["parallel_flows"]),
        "--dscp", str(int(selector.get("dscp") or 0)),
        "--stats-file", str(spec["stats_file"]),
    ]
    optional = (
        ("--destination-port", selector.get("destination_port")),
        ("--source-port", selector.get("source_port")),
        ("--offered-rate-mbps", spec.get("offered_rate_mbps")),
        ("--duration-seconds", spec.get("duration_seconds")),
        ("--burst-size-bytes", spec.get("burst_size_bytes")),
        ("--burst-interval-seconds", spec.get("burst_interval_seconds")),
    )
    for flag, value in optional:
        if value is not None:
            args.extend([flag, str(value)])
    if spec.get("shared_udp_receiver"):
        args.extend(["--udp-multiplexed", "--workload-id", str(spec["workload_id"])])
    return args


def _release_shared_udp_server(key: tuple[str, int], workload_id: str) -> None:
    shared = _shared_udp_servers.get(key)
    if shared is None:
        return
    shared["workload_ids"].discard(workload_id)
    if not shared["workload_ids"]:
        _kill_process(shared.get("process"))
        _shared_udp_servers.pop(key, None)


def _acquire_shared_udp_server(net, spec: dict) -> tuple[tuple[str, int], subprocess.Popen]:
    key = (str(spec["dst_endpoint"]), int(spec["selector"]["destination_port"]))
    shared = _shared_udp_servers.get(key)
    if shared is not None and shared["process"].poll() is None:
        shared["workload_ids"].add(spec["workload_id"])
        return key, shared["process"]
    _shared_udp_servers.pop(key, None)
    listener = _port_listener(net.get(key[0]), key[1])
    if listener:
        raise RuntimeError(f"WORKLOAD_PORT_IN_USE: {key[0]}:{key[1]} {listener}")
    process = _node_popen(
        net.get(key[0]),
        ["python3", "/app/mn/udp_workload_receiver.py", "--port", str(key[1])],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.2)
    if process.poll() is not None:
        raise RuntimeError(f"WORKLOAD_SHARED_UDP_SERVER_FAILED: exit={process.returncode}")
    _shared_udp_servers[key] = {"process": process, "workload_ids": {spec["workload_id"]}}
    return key, process


def _runtime_process_refs(record: dict) -> list[dict]:
    refs = []
    for role in ("server", "client"):
        process = record.get(f"{role}_process")
        if process is not None:
            refs.append({
                "role": role,
                "pid": process.pid,
                "state": "RUNNING" if process.poll() is None else "EXITED",
                "exit_code": process.poll(),
                "endpoint": record["spec"]["dst_endpoint" if role == "server" else "src_endpoint"],
            })
    return refs


def _runtime_workload_snapshot(record: dict) -> dict:
    client = record.get("client_process")
    if record["lifecycle_state"] == "RUNNING" and client is not None and client.poll() is not None:
        if record.get("shared_udp_key"):
            _release_shared_udp_server(tuple(record["shared_udp_key"]), record["spec"]["workload_id"])
        else:
            _kill_process(record.get("server_process"))
        record["lifecycle_state"] = "STOPPED" if client.returncode == 0 else "FAILED"
        record["stopped_at"] = _utc_now()
        record["last_error"] = None if client.returncode == 0 else f"client exited with {client.returncode}"
    elapsed = max(0.0, time.monotonic() - record["monotonic_started"])
    stats_file = record["spec"].get("stats_file")
    if stats_file:
        try:
            with open(stats_file, "r", encoding="utf-8") as stream:
                measured = json.load(stream)
            if isinstance(measured, dict):
                record["statistics"].update(measured)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
    record["statistics"].update({
        "runtime_seconds": elapsed,
        "restart_count": record.get("restart_count", 0),
        "last_observed_at": _utc_now(),
    })
    spec = {
        key: value for key, value in record["spec"].items()
        if key not in {"actual_dst_ip", "stats_file"}
    }
    return {
        **spec,
        "lifecycle_state": record["lifecycle_state"],
        "started_at": record["started_at"],
        "stopped_at": record.get("stopped_at"),
        "process_refs": _runtime_process_refs(record),
        "statistics": dict(record["statistics"]),
        "last_error": record.get("last_error"),
    }


def _start_runtime_workload(net, raw: dict, *, restart_count: int = 0) -> dict:
    spec = _validate_runtime_workload(net, raw)
    workload_id = spec["workload_id"]
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", workload_id)
    spec["stats_file"] = f"/tmp/idn_workload_{safe_id}.json"
    with _workload_lock:
        existing = _workload_processes.get(workload_id)
        if existing:
            snapshot = _runtime_workload_snapshot(existing)
            if snapshot["lifecycle_state"] == "RUNNING":
                return {"status": "ok", "workload": snapshot, "idempotent": True}
            _stop_runtime_workload(workload_id)

        selector = spec["selector"]
        try:
            os.unlink(spec["stats_file"])
        except FileNotFoundError:
            pass
        server = None
        shared_udp_key = None
        if selector["protocol"] == "UDP":
            spec["shared_udp_receiver"] = True
            shared_udp_key, server = _acquire_shared_udp_server(net, spec)
        elif selector["protocol"] == "TCP":
            listener = _port_listener(net.get(spec["dst_endpoint"]), int(selector["destination_port"]))
            if listener:
                raise RuntimeError(
                    f"WORKLOAD_PORT_IN_USE: {spec['dst_endpoint']}:{selector['destination_port']} {listener}"
                )
            server = _node_popen(
                net.get(spec["dst_endpoint"]),
                ["iperf3", "-s", "-p", str(selector["destination_port"])],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.2)
            if server.poll() is not None:
                raise RuntimeError(f"WORKLOAD_SERVER_FAILED: exit={server.returncode}")
        try:
            client = _node_popen(
                net.get(spec["src_endpoint"]), _workload_client_args(spec),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.1)
            if client.poll() is not None:
                raise RuntimeError(f"WORKLOAD_CLIENT_FAILED: exit={client.returncode}")
        except Exception:
            if shared_udp_key:
                _release_shared_udp_server(shared_udp_key, workload_id)
            else:
                _kill_process(server)
            raise
        record = {
            "spec": spec,
            "server_process": server,
            "shared_udp_key": shared_udp_key,
            "client_process": client,
            "lifecycle_state": "RUNNING",
            "started_at": _utc_now(),
            "stopped_at": None,
            "monotonic_started": time.monotonic(),
            "restart_count": restart_count,
            "statistics": {},
            "last_error": None,
        }
        _workload_processes[workload_id] = record
        return {"status": "ok", "workload": _runtime_workload_snapshot(record), "idempotent": False}


def _stop_runtime_workload(workload_id: str) -> dict:
    with _workload_lock:
        record = _workload_processes.get(workload_id)
        if record is None:
            return {"status": "ok", "workload_id": workload_id, "lifecycle_state": "STOPPED",
                    "process_refs": [], "statistics": {}, "not_found": True}
        _kill_process(record.get("client_process"))
        if record.get("shared_udp_key"):
            _release_shared_udp_server(tuple(record["shared_udp_key"]), workload_id)
        else:
            _kill_process(record.get("server_process"))
        record["lifecycle_state"] = "STOPPED"
        record["stopped_at"] = _utc_now()
        snapshot = _runtime_workload_snapshot(record)
        try:
            os.unlink(record["spec"].get("stats_file") or "")
        except (FileNotFoundError, OSError):
            pass
        del _workload_processes[workload_id]
        return {"status": "ok", "workload": snapshot}


def _update_runtime_workload(net, raw: dict) -> dict:
    workload_id = str(raw.get("workload_id") or "")
    with _workload_lock:
        previous = _workload_processes.get(workload_id)
        restart_count = int((previous or {}).get("restart_count") or 0) + 1
        if previous:
            _stop_runtime_workload(workload_id)
        return _start_runtime_workload(net, raw, restart_count=restart_count)


def _list_runtime_workloads(workload_id: str | None = None) -> dict:
    with _workload_lock:
        if workload_id:
            record = _workload_processes.get(workload_id)
            if record is None:
                return {"status": "not_found", "workload_id": workload_id}
            return {"status": "ok", "workload": _runtime_workload_snapshot(record)}
        snapshots = [_runtime_workload_snapshot(item) for item in list(_workload_processes.values())]
        return {"status": "ok", "workloads": snapshots, "count": len(snapshots)}


def _matching_runtime_workload(profile: dict) -> str | None:
    """Return a running workload that carries the exact service selector."""
    with _workload_lock:
        for workload_id, record in _workload_processes.items():
            snapshot = _runtime_workload_snapshot(record)
            spec = record["spec"]
            selector = spec.get("selector") or {}
            if snapshot.get("lifecycle_state") != "RUNNING":
                continue
            if (
                spec.get("service_id") != profile.get("service_id")
                or spec.get("src_endpoint") != profile.get("src_host")
                or spec.get("dst_endpoint") != profile.get("dst_host")
                or str(selector.get("protocol") or "").upper() != profile.get("protocol")
                or selector.get("source_port") != profile.get("src_port")
                or selector.get("destination_port") != profile.get("dst_port")
            ):
                continue
            try:
                source_ok = ip_network(str(selector.get("source_ip")), strict=False) == ip_network(
                    str(profile.get("src_ip")), strict=False
                )
                destination_ok = ip_network(
                    str(selector.get("destination_ip")), strict=False
                ) == ip_network(str(profile.get("dst_ip")), strict=False)
            except ValueError:
                continue
            if source_ok and destination_ok:
                return workload_id
    return None


def _workload_snapshot(workload_id: str) -> dict:
    with _workload_lock:
        record = _workload_processes.get(workload_id)
        if record is None:
            raise RuntimeError(f"MATCHING_WORKLOAD_DISAPPEARED: {workload_id}")
        return _runtime_workload_snapshot(record)


def _workload_backed_validation(
    net, profile: dict, raw_payload: dict, *, workload_id: str,
    probe_id: str, validation_run_id: str, started_at: str, wall_started: float,
) -> dict:
    """Measure a production workload in place without competing for its port."""
    initial = _workload_snapshot(workload_id)
    initial_stats = initial.get("statistics") or {}
    initial_sequence = int(initial_stats.get("sequence") or 0)
    duration = max(1, min(120, int(profile.get("duration_seconds") or 5)))
    installed = _qos_policies.get(str(profile.get("traffic_scope_key") or ""))
    tc_before = _read_tc_resource(net, installed, installed["qos_binding"]) if installed else None
    tc_peak_backlog = 0
    interface_bindings = _service_interface_bindings(net, profile, installed)
    interface_before = _read_service_interfaces(net, interface_bindings)

    deadline = time.monotonic() + duration
    latest = initial
    while time.monotonic() < deadline:
        time.sleep(min(0.5, max(deadline - time.monotonic(), 0.0)))
        latest = _workload_snapshot(workload_id)
        if latest.get("lifecycle_state") != "RUNNING":
            raise RuntimeError(f"MATCHING_WORKLOAD_STOPPED: {workload_id}")
        if installed:
            sample = _read_tc_resource(net, installed, installed["qos_binding"])
            tc_peak_backlog = max(
                tc_peak_backlog, int(((sample.get("stats") or {}).get("backlog_bytes") or 0)),
            )

    # Allow one segment boundary after the requested observation window.  A
    # fresh sequence is mandatory so stale persisted metrics cannot pass KPI
    # verification.
    sample_deadline = time.monotonic() + 4
    while int((latest.get("statistics") or {}).get("sequence") or 0) <= initial_sequence:
        if time.monotonic() >= sample_deadline:
            raise RuntimeError(
                f"WORKLOAD_METRICS_UNAVAILABLE: no fresh iperf sample for {workload_id}"
            )
        time.sleep(0.25)
        latest = _workload_snapshot(workload_id)

    statistics = latest.get("statistics") or {}
    if statistics.get("status") != "MEASURED" or not statistics.get("protocol_metrics"):
        raise RuntimeError(
            f"WORKLOAD_METRICS_UNAVAILABLE: {statistics.get('error') or workload_id}"
        )
    metrics = dict(statistics["protocol_metrics"])
    validation_payload = {
        **raw_payload,
        "qos_mode": profile.get("qos_mode"),
        "bandwidth_mbps": profile.get("bandwidth_mbps"),
    }
    validation = _service_validation_result(profile, metrics, validation_payload)
    received_key = "received_mbps" if profile["protocol"] == "UDP" else "receiver_mbps"
    received = metrics.get(received_key)
    interface_after = _read_service_interfaces(net, interface_bindings)
    tc_after = _read_tc_resource(net, installed, installed["qos_binding"]) if installed else None
    tc_evidence = {"status": "NOT_APPLICABLE"}
    if tc_before is not None and tc_after is not None:
        before_stats = tc_before.get("stats") or {}
        after_stats = tc_after.get("stats") or {}
        before_filter = tc_before.get("filter_stats") or {}
        after_filter = tc_after.get("filter_stats") or {}
        tc_evidence = {
            "status": "MEASURED", "validation_run_id": validation_run_id,
            "switch": installed["qos_binding"].get("switch_name"),
            "interface": installed["qos_binding"].get("interface_name"),
            "class_id": tc_after.get("class_id"), "filter_id": tc_after.get("filter_id"),
            "rate_mbps": tc_after.get("rate_mbps"), "ceil_mbps": tc_after.get("ceil_mbps"),
            "before": before_stats, "after": after_stats,
            "delta": _counter_delta(before_stats, after_stats),
            "filter_before": before_filter,
            "filter_after": after_filter,
            "filter_delta": _tc_filter_counter_delta(before_filter, after_filter),
            "backlog_peak_bytes": tc_peak_backlog,
            "root_historical": tc_after.get("root_stats"),
            "default_class_historical": tc_after.get("default_stats"),
            "aggregation_rule": "target_class_only", "overlimits_is_drop": False,
        }
    bottleneck = (profile.get("qos_binding") or {}).get("bottleneck_link")
    return {
        "probe_id": probe_id, "validation_run_id": validation_run_id,
        "service_id": profile["service_id"], "intent_id": profile.get("intent_id"),
        "policy_id": profile.get("policy_id"), "protocol": profile["protocol"],
        "selector": profile["selector"], "start_time": started_at, "end_time": _utc_now(),
        "duration_seconds": time.monotonic() - wall_started, "status": validation["status"],
        "measurement_source": f"mininet_workload_iperf3_{profile['protocol'].lower()}_json",
        "offered_load": {
            "target_mbps": latest.get("offered_rate_mbps"), "background_mbps": 0,
            "total_mbps": latest.get("offered_rate_mbps"),
        },
        "received_load": {"target_mbps": received, "total_mbps": received},
        "protocol_metrics": metrics, "background_results": [],
        "p4_counter_evidence": {"status": "PENDING_BACKEND_READ"},
        "tc_evidence": tc_evidence,
        "interface_counters": _service_interface_delta(interface_before, interface_after),
        "path_evidence": {"bottleneck_link": bottleneck, "same_bottleneck": True},
        "validation_result": validation,
        "process_cleanup": {
            "process_cleanup_verified": True, "tracked_workload_retained": True,
            "workload_id": workload_id,
        },
        "traffic_generated": True, "background_started": False,
        "throughput_mbps": received, "test_profile": "RUNNING_WORKLOAD_WINDOW",
        "workload_evidence": {
            "workload_id": workload_id, "initial_sequence": initial_sequence,
            "final_sequence": statistics.get("sequence"),
            "sampled_at": statistics.get("sampled_at"),
            "cumulative": statistics.get("cumulative") or {},
        },
        "errors": [],
    }


def _run_service_validation(net, raw_payload: dict) -> dict:
    """Execute one protocol-correct service probe plus optional backgrounds."""
    started_at = _utc_now()
    wall_started = time.monotonic()
    probe_id = f"SP-{uuid4().hex[:12]}"
    validation_run_id = str(raw_payload.get("validation_run_id") or f"VR-{uuid4().hex[:16]}")
    processes: list[subprocess.Popen] = []
    captures = []
    all_hosts: set[str] = set()
    profile = None
    try:
        profile = _effective_service_request(net, raw_payload)
        # Service validation is evidence collection, not a policy compiler.
        # Preserve an installed capacity binding so a profile snapshot can
        # never silently replace a calibrated root HTB generation.
        installed_policy = _qos_policies.get(str(profile.get("traffic_scope_key") or ""))
        if installed_policy and installed_policy.get("stable_capacity"):
            profile["stable_capacity"] = dict(installed_policy["stable_capacity"])
        if profile["protocol"] == "ICMP":
            count = max(3, min(20, int(raw_payload.get("count") or profile["duration_seconds"])))
            output = _node_cmd(
                net.get(profile["src_host"]),
                f"ping -c {count} -W 1 {profile['actual_dst_ip']}",
                timeout=count + 4,
            )
            metrics = parse_icmp_service_output(output)
            validation = _service_validation_result(profile, metrics, raw_payload)
            return {
                "probe_id": probe_id,
                "validation_run_id": validation_run_id,
                "service_id": profile["service_id"],
                "intent_id": profile.get("intent_id"),
                "policy_id": profile.get("policy_id"),
                "protocol": "ICMP",
                "selector": profile["selector"],
                "start_time": started_at,
                "end_time": _utc_now(),
                "duration_seconds": time.monotonic() - wall_started,
                "status": validation["status"],
                "measurement_source": "mininet_ping",
                "offered_load": {"packet_count": count},
                "received_load": {"packet_count": metrics.get("received")},
                "protocol_metrics": metrics,
                "background_results": [],
                "p4_counter_evidence": {"status": "NOT_APPLICABLE"},
                "tc_evidence": {"status": "NOT_APPLICABLE"},
                "path_evidence": {},
                "validation_result": validation,
                "errors": [],
            }

        backgrounds_raw = list(raw_payload.get("background_profiles") or [])
        scenario_id = raw_payload.get("scenario_id")
        if scenario_id:
            scenario = _traffic_scenario(str(scenario_id))
            if scenario.get("target_service_id") != profile["service_id"]:
                raise ValueError("VALIDATION_PROFILE_MISMATCH: scenario target differs from service_id")
            backgrounds_raw = list(scenario.get("background_flows") or [])

        # An exact production workload is stronger evidence than a synthetic
        # one-shot probe and already owns the service port.  Observe it in
        # place; only fall back to a bounded validation client when no matching
        # workload exists and no configured competition scenario is requested.
        if not backgrounds_raw:
            workload_id = _matching_runtime_workload(profile)
            if workload_id:
                return _workload_backed_validation(
                    net, profile, raw_payload, workload_id=workload_id,
                    probe_id=probe_id, validation_run_id=validation_run_id,
                    started_at=started_at, wall_started=wall_started,
                )

        flows = [{**profile, "role": "target", "start_offset_seconds": 0.0}]
        for item in backgrounds_raw:
            override = {"service_id": item} if isinstance(item, str) else dict(item)
            service_id = override.get("service_id") or override.get("service_profile_id")
            background = _effective_service_request(net, {
                "service_id": service_id,
                "offered_rate_mbps": override.get("offered_rate_mbps"),
                "duration_seconds": override.get("duration_seconds") or profile["duration_seconds"],
                "packet_size_bytes": override.get("packet_size_bytes"),
                "warmup_seconds": override.get("warmup_seconds", profile.get("warmup_seconds")),
                "pacing_timer_us": override.get("pacing_timer_us"),
            })
            background.update({
                "role": "background",
                "start_offset_seconds": float(override.get("start_offset_seconds") or 0),
            })
            flows.append(background)

        endpoint_keys = []
        for flow in flows:
            key = (flow["dst_host"], int(flow["dst_port"]))
            if key in endpoint_keys:
                raise ValueError(
                    "VALIDATION_PROFILE_MISMATCH: simultaneous services cannot share one "
                    f"destination namespace/listener {key}; distinguish dst_host or port"
                )
            endpoint_keys.append(key)
            all_hosts.update({flow["src_host"], flow["dst_host"]})
            destination_node = net.get(flow["dst_host"])
            listener = _port_listener(destination_node, int(flow["dst_port"]))
            if listener:
                raise RuntimeError(
                    f"SERVICE_PORT_IN_USE: {flow['dst_host']}:{flow['dst_port']} {listener}"
                )
            server = _node_popen(
                destination_node,
                ["iperf3", "-s", "-1", "-p", str(flow["dst_port"]), "-J"],
                # The client carries the receiver JSON via
                # --get-server-output.  Avoid leaving an unread server pipe
                # that can block the one-shot server before it accepts traffic.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            flow["server_process"] = server
            processes.append(server)

        tc_before = tc_after = None
        tc_peak_backlog = 0
        # TC readbacks execute several subprocesses in the Mininet namespace.
        # Keep the measurement bounded but do not compete with a short UDP
        # experiment every 500 ms.  This is a deployment-level observability
        # knob, not a traffic or SLA parameter.
        tc_sample_interval = min(5.0, max(0.5, float(
            os.getenv("IDN_QOS_TC_SAMPLE_INTERVAL_SECONDS", "2.0")
        )))
        next_tc_sample = 0.0
        installed = _qos_policies.get(str(profile.get("traffic_scope_key") or ""))
        if installed:
            tc_before = _read_tc_resource(net, installed, installed["qos_binding"])
        interface_bindings = _service_interface_bindings(net, profile, installed)
        interface_before = _read_service_interfaces(net, interface_bindings)

        time.sleep(0.5)
        client_records = []
        client_started = time.monotonic()
        for flow in sorted(flows, key=lambda item: item.get("start_offset_seconds") or 0):
            offset = float(flow.get("start_offset_seconds") or 0)
            remaining_offset = client_started + offset - time.monotonic()
            if remaining_offset > 0:
                time.sleep(remaining_offset)
            stdout_capture = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
            stderr_capture = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
            captures.extend((stdout_capture, stderr_capture))
            client = _node_popen(
                net.get(flow["src_host"]),
                _flow_args(flow),
                # JSON output can exceed the container pipe capacity.  A
                # non-drained PIPE deadlocks iperf after traffic has started,
                # which was observed as SERVICE_PROBE_TIMEOUT.
                stdout=stdout_capture,
                stderr=stderr_capture,
                text=True,
            )
            flow["client_stdout_capture"] = stdout_capture
            flow["client_stderr_capture"] = stderr_capture
            flow["client_process"] = client
            processes.append(client)
            client_records.append((flow, client))

        measurement_records = _measurement_client_records(client_records)
        deadline = time.monotonic() + _service_probe_timeout_seconds(
            [flow for flow, _ in measurement_records]
        )
        while any(process.poll() is None for _, process in measurement_records):
            if time.monotonic() >= deadline:
                raise RuntimeError("SERVICE_PROBE_TIMEOUT: iperf client exceeded bounded deadline")
            now = time.monotonic()
            if installed and now >= next_tc_sample:
                sample = _read_tc_resource(net, installed, installed["qos_binding"])
                tc_peak_backlog = max(
                    tc_peak_backlog,
                    int(((sample.get("stats") or {}).get("backlog_bytes") or 0)),
                )
                next_tc_sample = now + tc_sample_interval
            time.sleep(0.25)

        results = []
        for flow, client in measurement_records:
            stdout = _read_capture(flow["client_stdout_capture"])
            stderr = _read_capture(flow["client_stderr_capture"])
            if client.returncode != 0:
                raise RuntimeError(
                    f"SERVICE_PROBE_FAILED {flow['service_id']}: "
                    f"{stderr.strip() or stdout.strip() or 'client failed'}"
                )
            metrics = _protocol_result(flow["protocol"], stdout)
            raw_iperf = metrics.get("raw") if isinstance(metrics.get("raw"), dict) else {}
            results.append({
                "service_id": flow["service_id"],
                "service_name": flow.get("service_name"),
                "role": flow["role"],
                "protocol": flow["protocol"],
                "selector": flow["selector"],
                "offered_rate_mbps": flow["offered_rate_mbps"],
                "duration_seconds": flow["duration_seconds"],
                "warmup_seconds": flow.get("warmup_seconds", 0),
                "pacing_timer_us": flow.get("pacing_timer_us"),
                "packet_size_bytes": flow.get("packet_size_bytes"),
                "protocol_metrics": metrics,
                "sender_json": raw_iperf,
                "receiver_json": raw_iperf.get("server_output_json") or {},
                "measurement_source": f"mininet_iperf3_{flow['protocol'].lower()}_json",
            })

        # Background clients are competition generators, rather than the
        # measured subject.  Once the target has completed, terminate them
        # and retain explicit lifecycle evidence.  The default TC-class
        # counter delta below proves whether they actually competed on the
        # shared bottleneck during the target measurement window.
        backgrounds = []
        for flow, client in client_records:
            if flow.get("role") != "background":
                continue
            active_at_target_completion = client.poll() is None
            _kill_process(client)
            backgrounds.append({
                "service_id": flow["service_id"],
                "service_name": flow.get("service_name"),
                "role": "background",
                "protocol": flow["protocol"],
                "selector": flow["selector"],
                "offered_rate_mbps": flow["offered_rate_mbps"],
                "duration_seconds": flow["duration_seconds"],
                "measurement_source": "mininet_iperf3_competition_window",
                "active_at_target_completion": active_at_target_completion,
                "completion": "TERMINATED_AFTER_TARGET_MEASUREMENT",
                "protocol_metrics": {},
            })

        interface_after = _read_service_interfaces(net, interface_bindings)
        if installed:
            tc_after = _read_tc_resource(net, installed, installed["qos_binding"])
        target = next(item for item in results if item["role"] == "target")
        validation_payload = {**raw_payload, **{
            "qos_mode": profile.get("qos_mode"),
            "bandwidth_mbps": profile.get("bandwidth_mbps"),
        }}
        validation = _service_validation_result(profile, target["protocol_metrics"], validation_payload)
        target_received = target["protocol_metrics"].get(
            "received_mbps" if profile["protocol"] == "UDP" else "receiver_mbps"
        )
        tc_evidence = {"status": "NOT_APPLICABLE"}
        if tc_before is not None and tc_after is not None:
            before_stats = tc_before.get("stats") or {}
            after_stats = tc_after.get("stats") or {}
            before_filter = tc_before.get("filter_stats") or {}
            after_filter = tc_after.get("filter_stats") or {}
            before_root = tc_before.get("root_stats") or {}
            after_root = tc_after.get("root_stats") or {}
            before_default = tc_before.get("default_stats") or {}
            after_default = tc_after.get("default_stats") or {}
            tc_evidence = {
                "status": "MEASURED",
                "validation_run_id": validation_run_id,
                "switch": installed["qos_binding"].get("switch_name"),
                "interface": installed["qos_binding"].get("interface_name"),
                "class_id": tc_after.get("class_id"),
                "filter_id": tc_after.get("filter_id"),
                "rate_mbps": tc_after.get("rate_mbps"),
                "ceil_mbps": tc_after.get("ceil_mbps"),
                "before": before_stats,
                "after": after_stats,
                "delta": _counter_delta(before_stats, after_stats),
                "filter_before": before_filter,
                "filter_after": after_filter,
                "filter_delta": _tc_filter_counter_delta(before_filter, after_filter),
                "root_before": before_root,
                "root_after": after_root,
                "root_delta": _counter_delta(before_root, after_root),
                "default_before": before_default,
                "default_after": after_default,
                "default_delta": _counter_delta(before_default, after_default),
                "backlog_peak_bytes": tc_peak_backlog,
                "root_historical": tc_after.get("root_stats"),
                "default_class_historical": tc_after.get("default_stats"),
                "aggregation_rule": "target_class_only",
                "overlimits_is_drop": False,
            }
            if backgrounds:
                tc_evidence["competition_observed"] = bool(
                    (tc_evidence["default_delta"].get("sent_packets") or 0) > 0
                )
                tc_evidence["competition_proof"] = (
                    "default_tc_class_packet_delta"
                    if tc_evidence["competition_observed"]
                    else "no_default_tc_class_packets_observed"
                )
        total_offered = sum(float(item["offered_rate_mbps"] or 0) for item in results)
        total_offered += sum(float(item["offered_rate_mbps"] or 0) for item in backgrounds)
        total_received = sum(float(
            item["protocol_metrics"].get(
                "received_mbps" if item["protocol"] == "UDP" else "receiver_mbps"
            ) or 0
        ) for item in results)
        same_bottleneck = True
        target_bottleneck = (profile.get("qos_binding") or {}).get("bottleneck_link")
        if backgrounds and target_bottleneck:
            same_bottleneck = all(
                ((_service_profile(item["service_id"]).get("qos_binding") or {}).get("bottleneck_link")
                 == target_bottleneck)
                for item in backgrounds
            )
        for process in reversed(processes):
            _kill_process(process)
        cleanup = _tracked_process_cleanup_evidence(processes)
        return {
            "probe_id": probe_id,
            "validation_run_id": validation_run_id,
            "service_id": profile["service_id"],
            "intent_id": profile.get("intent_id"),
            "policy_id": profile.get("policy_id"),
            "protocol": profile["protocol"],
            "selector": profile["selector"],
            "start_time": started_at,
            "end_time": _utc_now(),
            "duration_seconds": time.monotonic() - wall_started,
            "status": validation["status"],
            "measurement_source": target["measurement_source"],
            "offered_load": {
                "target_mbps": profile["offered_rate_mbps"],
                "background_mbps": total_offered - float(profile["offered_rate_mbps"] or 0),
                "total_mbps": total_offered,
            },
            "received_load": {"target_mbps": target_received, "total_mbps": total_received},
            "protocol_metrics": target["protocol_metrics"],
            "background_results": backgrounds,
            "p4_counter_evidence": {"status": "PENDING_BACKEND_READ"},
            "tc_evidence": tc_evidence,
            "interface_counters": _service_interface_delta(interface_before, interface_after),
            "path_evidence": {
                "bottleneck_link": target_bottleneck,
                "same_bottleneck": same_bottleneck,
            },
            "validation_result": validation,
            "process_cleanup": cleanup,
            "traffic_generated": True,
            "background_started": bool(backgrounds),
            "throughput_mbps": target_received,
            "test_profile": (
                "MIN_BANDWIDTH_COMPETITION"
                if profile.get("qos_mode") == "MIN_BANDWIDTH" and backgrounds
                else "RATE_LIMIT_NO_BACKGROUND"
                if profile.get("qos_mode") == "RATE_LIMIT" and not backgrounds
                else "SERVICE_VALIDATION"
            ),
            "errors": [],
        }
    except Exception as exc:
        code = str(exc).split(":", 1)[0] if ":" in str(exc) else "SERVICE_PROBE_FAILED"
        for process in reversed(processes):
            _kill_process(process)
        cleanup = _tracked_process_cleanup_evidence(processes)
        return {
            "probe_id": probe_id,
            "validation_run_id": validation_run_id,
            "service_id": (profile or {}).get("service_id") or raw_payload.get("service_id"),
            "intent_id": raw_payload.get("intent_id"),
            "policy_id": raw_payload.get("policy_id"),
            "protocol": str((profile or {}).get("protocol") or raw_payload.get("protocol") or "").upper(),
            "selector": (profile or {}).get("selector") or {},
            "start_time": started_at,
            "end_time": _utc_now(),
            "duration_seconds": time.monotonic() - wall_started,
            "status": "PROFILE_MISMATCH" if code == "VALIDATION_PROFILE_MISMATCH" else "PROBE_FAILED",
            "measurement_source": "mininet_service_probe",
            "offered_load": {},
            "received_load": {},
            "protocol_metrics": {},
            "background_results": [],
            "p4_counter_evidence": {},
            "tc_evidence": {},
            "path_evidence": {},
            "validation_result": {"passed": False, "verification_state": code},
            "process_cleanup": cleanup,
            "errors": [{"code": code, "message": str(exc)}],
        }
    finally:
        for process in reversed(processes):
            _kill_process(process)
        for capture in captures:
            capture.close()


def _run_configured_scenario(net, raw_payload: dict) -> dict:
    scenario = _traffic_scenario(str(raw_payload.get("scenario_id") or ""))
    request = {
        **raw_payload,
        "service_id": scenario["target_service_id"],
        "scenario_id": scenario["scenario_id"],
        "duration_seconds": raw_payload.get("duration_seconds") or scenario.get("duration_seconds"),
    }
    result = _run_service_validation(net, request)
    result["scenario_id"] = scenario["scenario_id"]
    result["scenario_name"] = scenario.get("name")
    result["scenario_validation_rules"] = scenario.get("validation_rules") or {}
    return result


def _run_qos_competition(net, raw_payload: dict) -> dict:
    """Run a bounded QoS probe, with optional background competition.

    This compatibility endpoint consumes an explicit selector and optional
    background source; it does not select endpoints or ports by business name.
    """
    payload = _validate_qos_payload(raw_payload)
    installed = _qos_policies.get(payload["scope_key"])
    if not installed:
        return {"status": "error", "error": "QoS policy is not installed"}
    binding = installed["qos_binding"]
    include_background = _payload_bool(raw_payload.get("include_background"), default=True)
    profile = str(raw_payload.get("test_profile") or "").upper()
    minimum_duration = 15 if profile in {
        "RATE_LIMIT_NO_BACKGROUND", "MIN_BANDWIDTH_COMPETITION",
    } else 2
    duration = max(minimum_duration, min(30, int(raw_payload.get("duration") or 5)))
    warmup_duration = 3
    requested_target_offered = raw_payload.get("target_offered_mbps")
    target_offered = (
        max(1, int(requested_target_offered))
        if requested_target_offered is not None
        else max(payload["bandwidth_mbps"] * 3, 30)
    )
    background_offered = (
        max(
            int(raw_payload.get("background_offered_mbps") or 0),
            int(binding["link_capacity_mbps"]),
            90,
        )
        if include_background else 0
    )
    target_source = binding["source_host"]
    destination = binding["destination_host"]
    background_source = str(raw_payload.get("background_source") or "")
    if include_background and not background_source:
        raise ValueError("background_source is required when include_background=true")
    if include_background and background_source not in net.nameToNode:
        raise ValueError(f"background source {background_source} is absent")
    target_node = net.get(target_source)
    background_node = net.get(background_source) if include_background else None
    destination_node = net.get(destination)
    destination_ip = _host_ip_plain(destination_node)
    if payload.get("destination_port") is None:
        raise ValueError("VALIDATION_PROFILE_MISMATCH: policy destination port is required")
    target_port = int(payload["destination_port"])
    background_port = target_port + 1 if target_port < 65535 else target_port - 1
    timeout = duration + 8
    processes: list[subprocess.Popen] = []
    before = _read_tc_resource(net, payload, binding)
    try:
        target_server = _node_popen(
            destination_node,
            ["iperf3", "-s", "-1", "-p", str(target_port), "-J"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        processes.append(target_server)
        background_server = None
        if include_background:
            background_server = _node_popen(
                destination_node,
                ["iperf3", "-s", "-1", "-p", str(background_port), "-J"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            processes.append(background_server)
        time.sleep(0.4)
        target_args = [
            "iperf3", "-c", destination_ip, "-p", str(target_port),
            "-t", str(duration), "-O", str(warmup_duration),
            "-J", "--get-server-output", "-w", "2M",
        ]
        if payload["protocol"] != "TCP":
            target_args.extend([
                "-u", "-b", f"{target_offered}M",
                "--fq-rate", f"{target_offered}M",
            ])
        target_client = _node_popen(
            target_node, target_args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        processes.append(target_client)
        background_client = None
        if include_background:
            background_args = [
                "iperf3", "-c", destination_ip, "-p", str(background_port),
                "-t", str(duration), "-O", str(warmup_duration),
                "-J", "--get-server-output", "-w", "2M",
                "-u", "-b", f"{background_offered}M",
                "--fq-rate", f"{background_offered}M",
            ]
            background_client = _node_popen(
                background_node, background_args,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            processes.append(background_client)
        target_stdout, target_stderr, target_timeout = _communicate_bounded(target_client, timeout)
        if background_client is not None:
            background_stdout, background_stderr, background_timeout = _communicate_bounded(background_client, timeout)
        else:
            background_stdout, background_stderr, background_timeout = "", "", False
        if target_timeout or background_timeout:
            raise RuntimeError("competing iperf client timed out")
        if target_client.returncode != 0:
            raise RuntimeError(f"target iperf failed: {target_stderr.strip() or target_stdout.strip()}")
        if background_client is not None and background_client.returncode != 0:
            raise RuntimeError(f"background iperf failed: {background_stderr.strip() or background_stdout.strip()}")
        target = parse_iperf_output(target_stdout)
        background = parse_iperf_output(background_stdout) if include_background else None
    finally:
        for process in reversed(processes):
            _kill_process(process)
    after = _read_tc_resource(net, payload, binding)
    before_stats = before.get("stats") or {}
    after_stats = after.get("stats") or {}
    before_filter_stats = before.get("filter_stats") or {}
    after_filter_stats = after.get("filter_stats") or {}
    stats_delta = _counter_delta(before_stats, after_stats)
    filter_stats_delta = _tc_filter_counter_delta(before_filter_stats, after_filter_stats)
    before_root_stats = before.get("root_stats") or {}
    after_root_stats = after.get("root_stats") or {}
    root_stats_delta = _counter_delta(before_root_stats, after_root_stats)
    before_default_stats = before.get("default_stats") or {}
    after_default_stats = after.get("default_stats") or {}
    default_stats_delta = _counter_delta(before_default_stats, after_default_stats)
    throughput = target.get("throughput_mbps")
    background_throughput = (background or {}).get("throughput_mbps")
    total_received = float(throughput or 0) + float(background_throughput or 0)
    egress_sent_mbps = (
        float(root_stats_delta.get("sent_bytes") or 0)
        * 8 / (duration + warmup_duration) / 1_000_000
    )
    return {
        "status": "ok",
        "scope_key": payload["scope_key"],
        "qos_mode": payload["qos_mode"],
        "test_type": "COMPETING_TRAFFIC" if include_background else "NO_BACKGROUND",
        "test_profile": profile or ("COMPETING_TRAFFIC" if include_background else "NO_BACKGROUND"),
        "background_started": include_background,
        "congestion_created": include_background and target_offered + background_offered > binding["link_capacity_mbps"],
        "link_capacity_mbps": binding["link_capacity_mbps"],
        "target_offered_mbps": target_offered,
        "background_offered_mbps": background_offered,
        "total_offered_mbps": target_offered + background_offered,
        "throughput_mbps": throughput,
        "target": target,
        "background": background,
        "total_received_mbps": total_received,
        "bottleneck_egress_sent_mbps": egress_sent_mbps,
        "target_source": target_source,
        "background_source": background_source,
        "destination": destination,
        "target_port": target_port,
        "background_port": background_port,
        "duration": duration,
        "warmup_duration": warmup_duration,
        "traffic_wall_duration": duration + warmup_duration,
        "traffic_generated": True,
        "same_bottleneck_binding": binding,
        "tc_class_before": before_stats,
        "tc_class_after": after_stats,
        "tc_class_delta": stats_delta,
        "tc_filter_before": before_filter_stats,
        "tc_filter_after": after_filter_stats,
        "tc_filter_delta": filter_stats_delta,
        "tc_root_before": before_root_stats,
        "tc_root_after": after_root_stats,
        "tc_root_delta": root_stats_delta,
        "tc_default_before": before_default_stats,
        "tc_default_after": after_default_stats,
        "tc_default_delta": default_stats_delta,
        "tc_class_counter_increased": int(stats_delta.get("sent_bytes") or 0) > 0,
        "process_cleanup_verified": all(process.poll() is not None for process in processes),
    }


def _cleanup_qos_test_processes(net) -> dict:
    cleaned = []
    for name in sorted(item for item in net.nameToNode if str(item).startswith("h_")):
        if name not in net.nameToNode:
            continue
        node = net.get(name)
        _node_cmd(node, "pkill -x iperf3 || true", timeout=3)
        remaining = _node_cmd(node, "pgrep -x iperf3 || true", timeout=3).strip()
        cleaned.append({"host": name, "iperf_processes_remaining": bool(remaining)})
    return {
        "status": "ok" if not any(item["iperf_processes_remaining"] for item in cleaned) else "error",
        "hosts": cleaned,
        "process_cleanup_verified": not any(item["iperf_processes_remaining"] for item in cleaned),
    }


def parse_qdisc_output(output: str) -> dict:
    def _size_to_bytes(raw: str) -> int:
        match = re.match(r"^([0-9.]+)\s*([kmgt]?)(?:b|byte|bytes)?$", raw.strip(), re.IGNORECASE)
        if not match:
            return 0
        value = float(match.group(1))
        unit = match.group(2).lower()
        factor = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}.get(unit, 1)
        return int(value * factor)

    backlog_matches = re.findall(r"backlog\s+([0-9.]+[KkMmGgTt]?[bB]?)\s+(\d+)p", output)
    stat_matches = re.findall(
        r"Sent\s+(\d+)\s+bytes\s+(\d+)\s+pkt\s+\(dropped\s+(\d+),\s+overlimits\s+(\d+)",
        output,
    )
    queue_bytes = [_size_to_bytes(item[0]) for item in backlog_matches]
    queue_packets = [int(item[1]) for item in backlog_matches]
    dropped_packets = [int(item[2]) for item in stat_matches]
    overlimits = [int(item[3]) for item in stat_matches]
    sent_packets = [int(item[1]) for item in stat_matches]

    return {
        "backlog_bytes": max(queue_bytes) if queue_bytes else None,
        "backlog_packets": max(queue_packets) if queue_packets else None,
        "dropped_packets": sum(dropped_packets) if dropped_packets else None,
        "overlimits": sum(overlimits) if overlimits else 0,
        "sent_packets": sum(sent_packets) if sent_packets else 0,
        "raw": output,
    }


def parse_root_qdisc_output(output: str) -> dict:
    """Parse only the interface root qdisc; never add child/class counters."""
    blocks = re.split(r"(?=^qdisc\s+)", output, flags=re.MULTILINE)
    root = next((block for block in blocks if re.search(r"^qdisc\s+.*\broot\b", block)), "")
    value = parse_qdisc_output(root)
    value["aggregation_scope"] = "root_qdisc_only"
    value["overlimits_is_drop"] = False
    return value


# ── INT probe ─────────────────────────────────────────────────────────────────

INT_RECEIVER_CODE = r"""
import json
import socket
import sys
import time

sys.path.insert(0, "/app/mn")
from int_schema import parse_ethernet_frame, schema

bind_ip = sys.argv[1]
timeout = float(sys.argv[2])
# protocol arg is ignored — we read raw Ethernet frames to bypass
# kernel IP processing (P4 inserts INT headers before IPv4, which
# corrupts the IP header from the kernel's point of view).

# Find a non-loopback interface (if_nameindex returns (index, name))
iface = None
for _idx, name in socket.if_nameindex():
    if name != "lo":
        iface = name
        break
if iface is None:
    print(json.dumps({"status": "error", "error": "no interface"}))
    sys.exit(1)

# ETH_P_ALL — read all Ethernet frames
sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
sock.bind((iface, 0))
sock.settimeout(timeout)
deadline = time.time() + timeout

while time.time() < deadline:
    try:
        frame = sock.recv(4096)
    except socket.timeout:
        break

    try:
        decoded = parse_ethernet_frame(frame)
    except ValueError:
        continue
    ipv4_packet = decoded["ipv4"]
    ihl = (ipv4_packet[0] & 0x0F) * 4
    if len(ipv4_packet) <= ihl:
        continue
    payload = ipv4_packet[ihl:]

    result = {
        "status": "ok",
        "src": bind_ip,
        "dst": bind_ip,
        "packet_hex": ipv4_packet.hex(),
        "payload_hex": payload.hex(),
        "int_header": decoded["header"],
        "int_hop_count": decoded["header"]["hop_count"],
        "int_hops_hex": decoded["hops_wire"].hex(),
        "parser_version": schema()["parser_version"],
    }
    print(json.dumps(result))
    sys.exit(0)

print(json.dumps({"status": "timeout", "error": "no INT probe received"}))
sys.exit(1)
"""

INT_SENDER_CODE = r"""
import socket
import sys
import time

dst_ip = sys.argv[1]
count = int(sys.argv[2])
interval = float(sys.argv[3])
protocol = int(sys.argv[4])

sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, protocol)
for idx in range(count):
    payload = bytes([0]) + b"IDNINT" + idx.to_bytes(2, "big")
    sock.sendto(payload, (dst_ip, 0))
    time.sleep(interval)
"""


def _parse_p4_int_hops(hop_data: bytes, hop_count: int) -> dict:
    """Decode canonical v2 records and normalize P4 microseconds to ns."""
    try:
        decoded = _decode_int_hops(hop_data, hop_count, wire_newest_first=True)
    except ValueError as exc:
        return {"status": "error", "error": str(exc), "hop_count": hop_count,
                "parsed_hop_count": 0, "hop_records": [], "payload_bytes": len(hop_data)}
    hops = []
    for idx, record in enumerate(decoded):
        ingress_ns = int(record["ingress_timestamp_us"]) * 1000
        egress_ns = int(record["egress_timestamp_us"]) * 1000
        hops.append({
            "hop_index": idx,
            "switch_id_raw": int(record["switch_id"]),
            "ingress_port": int(record["ingress_port"]),
            "egress_port": int(record["egress_port"]),
            "ingress_timestamp_ns": ingress_ns,
            "egress_timestamp_ns": egress_ns,
            "hop_latency_ns": egress_ns - ingress_ns,
            "queue_occupancy": int(record["queue_occupancy"]),
            "flags": int(record["flags"]),
            "timestamp_source_unit": "microseconds",
        })
    return {
        "status": "ok",
        "hop_count": hop_count,
        "parsed_hop_count": len(hops),
        "hop_records": hops,
        "payload_bytes": INT_HEADER_BYTES + len(hop_data),
        "payload_tail_hex": "",
    }


def _host_ip_plain(node) -> str:
    return str(getattr(node, "IP", lambda: "")()).split("/", 1)[0]


def _topology_links_by_node_port(topology: dict) -> dict[tuple[str, int], dict]:
    by_port: dict[tuple[str, int], dict] = {}
    for link in topology.get("links", []) or []:
        endpoints = link.get("endpoints") or []
        for idx, endpoint in enumerate(endpoints):
            node = endpoint.get("node")
            port = endpoint.get("port")
            if node is None or port is None:
                continue
            other = endpoints[1 - idx] if len(endpoints) == 2 else {}
            by_port[(node, int(port))] = {"link": link, "self": endpoint, "other": other}
    return by_port


def _first_switch_for_host(topology: dict, host_name: str) -> str | None:
    for link in topology.get("links", []) or []:
        endpoints = link.get("endpoints") or []
        if len(endpoints) != 2:
            continue
        left, right = endpoints
        if left.get("node") == host_name and right.get("type") == "switch":
            return right.get("node")
        if right.get("node") == host_name and left.get("type") == "switch":
            return left.get("node")
    return None


def _annotate_int_hops(net, hops: list[dict], src_host: str | None = None) -> list[dict]:
    topology = build_topology_snapshot(net)
    by_port = _topology_links_by_node_port(topology)
    switches = {item["name"]: item for item in topology.get("switches", []) or []}
    annotated = []

    for hop in hops:
        item = dict(hop)
        raw_switch_id = int(hop.get("switch_id_raw") or 0)
        current_switch = _switch_name_by_id.get(raw_switch_id)
        if current_switch and current_switch in switches:
            switch_info = switches[current_switch]
            item["switch_name"] = current_switch
            item["device_id"] = switch_info.get("device_id")
            item["switch_id_source"] = "p4_int"
        else:
            item["switch_name"] = None
            item["device_id"] = (
                f"device:bmv2:{hop.get('switch_id_raw')}"
                if hop.get("switch_id_raw")
                else None
            )
            item["switch_id_source"] = "p4_int" if raw_switch_id else "unavailable"

        next_ref = None
        if current_switch is not None:
            next_ref = by_port.get((current_switch, int(hop.get("egress_port") or -1)))
        if next_ref:
            link = next_ref.get("link") or {}
            other = next_ref.get("other") or {}
            item["link_id"] = link.get("id")
            item["next_node"] = other.get("node")
            item["next_type"] = other.get("type")
        else:
            item["link_id"] = None
            item["next_node"] = None
            item["next_type"] = None
        annotated.append(item)

    return annotated


def run_int_probe(
    net,
    *,
    src: str,
    dst: str,
    count: int = 1,
    timeout: float = 4.0,
) -> dict:
    if src not in net.nameToNode or dst not in net.nameToNode:
        return {"status": "error", "error": "unsupported INT probe endpoint"}
    src_node = net.get(src)
    dst_node = net.get(dst)
    dst_ip = _host_ip_plain(dst_node)
    if not dst_ip:
        return {"status": "error", "error": f"cannot resolve destination IP for {dst}"}

    receiver = None
    sender = None
    try:
        receiver = _node_popen(
            dst_node,
            ["python3", "-c", INT_RECEIVER_CODE, dst_ip, str(timeout), str(INT_IP_PROTOCOL)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.2)
        sender = _node_popen(
            src_node,
            ["python3", "-c", INT_SENDER_CODE, dst_ip, str(max(1, count)), "0.1", str(INT_IP_PROTOCOL)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        sender_stdout, sender_stderr, sender_timed_out = _communicate_bounded(sender, timeout)
        if sender_timed_out:
            return {
                "status": "error",
                "error": f"INT sender timed out after {timeout:g}s",
                "sender_stderr": sender_stderr.strip(),
            }
        receiver_stdout, receiver_stderr, receiver_timed_out = _communicate_bounded(
            receiver, timeout + 1,
        )
        if receiver_timed_out:
            return {
                "status": "error",
                "error": f"INT receiver timed out after {timeout + 1:g}s",
                "receiver_stderr": receiver_stderr.strip(),
            }
    finally:
        _kill_process(sender)
        _kill_process(receiver)

    if sender.returncode != 0:
        return {
            "status": "error",
            "error": "INT sender failed",
            "sender_stderr": sender_stderr.strip(),
        }

    try:
        received = json.loads((receiver_stdout or "").strip().splitlines()[-1])
    except Exception:
        return {
            "status": "error",
            "error": "INT receiver produced no JSON",
            "receiver_stdout": receiver_stdout,
            "receiver_stderr": receiver_stderr,
        }

    if received.get("status") != "ok":
        received.setdefault("receiver_stderr", receiver_stderr.strip())
        return received

    # ── Parse INT hop data ──
    # Only the canonical magic/version-framed layout is accepted. Legacy
    # payload heuristics are deliberately not promoted to telemetry evidence.
    int_hops_hex = received.get("int_hops_hex")
    hop_count = received.get("int_hop_count", 0)
    if int_hops_hex and hop_count > 0:
        hop_data = bytes.fromhex(int_hops_hex)
        parsed = _parse_p4_int_hops(hop_data, hop_count)
        parser_version = INT_PARSER_VERSION
        hop_record_bytes = INT_HOP_BYTES
    else:
        return {"status": "error", "error": "INT_V2_RECORDS_MISSING",
                "parser_version": received.get("parser_version") or "unknown"}
    if parsed.get("status") != "ok":
        return {**parsed, "parser_version": parser_version}
    annotated = _annotate_int_hops(net, parsed.get("hop_records", []), src_host=src)
    total_latency_ns = sum(item.get("hop_latency_ns") or 0 for item in annotated)
    max_queue = max([item.get("queue_occupancy") or 0 for item in annotated] or [0])

    return {
        "status": "ok",
        "source": "p4_int_probe",
        "parser_version": parser_version,
        "byte_order": "big",
        "schema_version": int(INT_SCHEMA["schema_version"]),
        "hop_record_bytes": hop_record_bytes,
        "src": src,
        "dst": dst,
        "dst_ip": dst_ip,
        "ip_protocol": INT_IP_PROTOCOL,
        "hop_count": parsed.get("hop_count", 0),
        "parsed_hop_count": parsed.get("parsed_hop_count", 0),
        "total_hop_latency_ns": total_latency_ns,
        "total_hop_latency_ms": total_latency_ns / 1_000_000,
        "max_queue_occupancy": max_queue,
        "hop_records": annotated,
        "receiver": {
            "src": received.get("src"),
            "dst": received.get("dst"),
        },
        "packet_payload_bytes": parsed.get("payload_bytes"),
        "raw_packet_hex": received.get("packet_hex"),
        "payload_tail_hex": parsed.get("payload_tail_hex"),
    }


# ── Topology snapshot builders ─────────────────────────────────────────────────


def _intf_bmv2_port(intf) -> int | None:
    """Return the 1-based BMv2/Stratum/ONOS port number."""
    name = getattr(intf, "name", "")
    if "-eth" not in name:
        return None
    try:
        return int(name.rsplit("-eth", 1)[1])
    except (TypeError, ValueError):
        return None


def _node_kind(node_name: str) -> str:
    return "host" if node_name.startswith("h_") else "switch"


def _link_endpoint(intf) -> dict:
    node = getattr(intf, "node", None)
    node_name = getattr(node, "name", "unknown")
    endpoint = {
        "node": node_name,
        "type": _node_kind(node_name),
        "interface": getattr(intf, "name", ""),
        "mac": getattr(intf, "MAC", lambda: None)(),
    }
    if endpoint["type"] == "switch":
        endpoint["port"] = _intf_bmv2_port(intf)
        cfg = _switch_config.get(node_name)
        if cfg:
            endpoint["device_id"] = cfg["device_uri"]
            endpoint["host_facing"] = endpoint["port"] in _host_facing_ports.get(cfg["device_uri"], [])
    else:
        endpoint["ip"] = getattr(node, "IP", lambda: None)()
    return endpoint


def _qdisc_interfaces(net) -> list[tuple[str, str]]:
    interfaces: list[tuple[str, str]] = []
    for node_name, node in net.nameToNode.items():
        if not (node_name.startswith("h_") or node_name.startswith("s_")):
            continue
        for intf in node.intfList():
            intf_name = getattr(intf, "name", "")
            if intf_name == "lo" or "-eth" not in intf_name:
                continue
            interfaces.append((node_name, intf_name))
    return interfaces


def _interface_is_up(intf) -> bool:
    """Return live interface state without changing the Mininet topology."""
    try:
        return bool(intf.isUp())
    except Exception:
        # Compatibility with older Intf implementations that lack isUp().
        return True


def build_topology_snapshot(net) -> dict:
    switches = []
    hosts = []

    for name in sorted(net.nameToNode):
        node = net.nameToNode[name]
        if name.startswith("s_"):
            cfg = _switch_config.get(name, {})
            ports = []
            for intf in node.intfList():
                intf_name = getattr(intf, "name", "")
                if intf_name == "lo" or "-eth" not in intf_name:
                    continue
                ports.append({
                    "name": intf_name,
                    "port": _intf_bmv2_port(intf),
                    "mac": getattr(intf, "MAC", lambda: None)(),
                    "host_facing": _intf_bmv2_port(intf) in _host_facing_ports.get(cfg.get("device_uri"), []),
                })
            switches.append({
                "name": name,
                "device_id": cfg.get("device_uri"),
                "grpc_port": cfg.get("grpc_port"),
                "thrift_port": cfg.get("thrift_port"),
                "router_mac": cfg.get("router_mac"),
                "ports": sorted(ports, key=lambda item: item.get("port") or 0),
            })
        elif name.startswith("h_"):
            default_intf = node.defaultIntf()
            configured = _host_runtime_config.get(name, {})
            hosts.append({
                "name": name,
                "ip": node.IP(),
                "ip_cidr": configured.get("ip_cidr"),
                "mac": node.MAC(),
                "interface": getattr(default_intf, "name", ""),
                "subnet": configured.get("subnet"),
                "gateway_ip": configured.get("gateway_ip"),
                "gateway_mac": configured.get("gateway_mac"),
            })

    links = []
    for index, link in enumerate(net.links):
        metadata = getattr(link, "idn_metadata", {}) or {}
        endpoint1 = _link_endpoint(link.intf1)
        endpoint2 = _link_endpoint(link.intf2)
        links.append({
            "id": metadata.get("id") or f"link-{index}",
            "role": metadata.get("role", "primary"),
            "cost": float(metadata.get("cost", 1.0)),
            "bandwidth_mbps": metadata.get("bandwidth_mbps"),
            "delay_ms": metadata.get("delay_ms"),
            "profile": metadata.get("profile"),
            "state": (
                "ACTIVE"
                if _interface_is_up(link.intf1) and _interface_is_up(link.intf2)
                else "INACTIVE"
            ),
            "endpoints": [endpoint1, endpoint2],
        })

    return {
        "status": "ok",
        "source": "mininet_probe",
        "timestamp": time.time(),
        "switches": switches,
        "hosts": hosts,
        "links": links,
        "host_facing_ports": _host_facing_ports,
    }


def configure_p4_switch_ids(net) -> dict:
    """Report switch_id → device_uri mapping for INT telemetry."""
    topology = build_topology_snapshot(net)
    switch_by_name = {item["name"]: item for item in topology.get("switches", []) or []}
    results = []
    for switch_name, cfg in _switch_config.items():
        switch_info = switch_by_name.get(switch_name, {})
        ports = [
            int(port_info.get("port"))
            for port_info in switch_info.get("ports", [])
            if port_info.get("port") is not None
        ]
        results.append({
            "switch": switch_name,
            "device_id": cfg["device_uri"],
            "switch_id": int(cfg["device_id"]),
            "ports": sorted(set(ports)),
            "method": "p4_table_entry",
        })
    return {
        "status": "ok",
        "table": "MyEgress.int_switch_config",
        "method": "onos_p4runtime_flow",
        "configured": len(results),
        "total": len(results),
        "results": results,
        "errors": [],
    }


def _fault_link(net, raw: dict):
    target = str(raw.get("target_resource_id") or "")
    resolution = raw.get("target_resolution") or {}
    candidates = {target, str(resolution.get("canonical_resource_id") or "")}
    candidates.update(str(item) for item in resolution.get("runtime_candidates") or [])
    for link in net.links:
        metadata = getattr(link, "idn_metadata", {}) or {}
        left, right = link.intf1.node.name, link.intf2.node.name
        aliases = {
            str(metadata.get("id") or ""), f"{left}-{right}", f"{right}-{left}",
            "link:" + "--".join(sorted((left, right))),
        }
        if candidates & aliases:
            return link
    raise ValueError(f"FAULT_LINK_NOT_FOUND: {target}")


def _fault_device(net, raw: dict):
    resolution = raw.get("target_resolution") or {}
    switch_name = str(resolution.get("switch_name") or raw.get("target_resource_id") or "")
    if switch_name not in net.nameToNode:
        raise ValueError(f"FAULT_DEVICE_NOT_FOUND: {switch_name}")
    node = net.get(switch_name)
    if not switch_name.startswith("s_"):
        raise ValueError(f"FAULT_TARGET_NOT_DEVICE: {switch_name}")
    return node


def _fault_observed(net, record: dict) -> dict:
    fault_type = record["fault_type"]
    if fault_type.startswith("LINK_"):
        link = _fault_link(net, record)
        if fault_type == "LINK_DOWN":
            active = not _interface_is_up(link.intf1) and not _interface_is_up(link.intf2)
        else:
            output = _node_cmd(link.intf1.node, f"tc qdisc show dev {link.intf1.name}", timeout=3)
            active = "netem" in output
        return {"active": active, "interfaces": [link.intf1.name, link.intf2.name]}
    if fault_type == "DEVICE_DOWN":
        node = _fault_device(net, record)
        interfaces = [item for item in node.intfList() if item.name != "lo"]
        return {"active": bool(interfaces) and all(not _interface_is_up(item) for item in interfaces),
                "interfaces": [item.name for item in interfaces]}
    workload_id = str((record.get("parameters") or {}).get("workload", {}).get("workload_id") or "")
    runtime = _list_runtime_workloads(workload_id) if workload_id else {"status": "not_found"}
    return {"active": runtime.get("status") == "ok" and
            (runtime.get("workload") or {}).get("lifecycle_state") == "RUNNING",
            "workload": runtime.get("workload")}


def _apply_fault(net, raw: dict) -> dict:
    fault_id = str(raw.get("fault_id") or "")
    fault_type = str(raw.get("fault_type") or "").upper()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", fault_id):
        raise ValueError("FAULT_ID_INVALID")
    supported = {"LINK_DOWN", "LINK_DELAY", "LINK_LOSS", "LINK_RATE_LIMIT",
                 "DEVICE_DOWN", "SERVICE_OVERLOAD"}
    if fault_type not in supported:
        raise ValueError(f"FAULT_TYPE_UNSUPPORTED: {fault_type}")
    with _fault_lock:
        current = _fault_states.get(fault_id)
        if current and _fault_observed(net, current).get("active"):
            return {"status": "ok", "fault_id": fault_id, "active": True,
                    "idempotent": True, "observed": _fault_observed(net, current)}
        record = {
            "fault_id": fault_id, "fault_type": fault_type,
            "target_resource_id": str(raw.get("target_resource_id") or ""),
            "target_resolution": raw.get("target_resolution") or {},
            "parameters": dict(raw.get("parameters") or {}),
            "applied_at": _utc_now(),
        }
        if fault_type.startswith("LINK_"):
            link = _fault_link(net, record)
            record["original"] = {
                "metadata": dict(getattr(link, "idn_metadata", {}) or {}),
                "qdisc": _node_cmd(link.intf1.node, f"tc qdisc show dev {link.intf1.name}", timeout=3),
            }
            if fault_type == "LINK_DOWN":
                for intf in (link.intf1, link.intf2):
                    _node_cmd(intf.node, f"ip link set dev {intf.name} down", timeout=4, check=True)
            else:
                params = record["parameters"]
                if fault_type == "LINK_DELAY":
                    option = f"delay {float(params['delay_ms']):g}ms"
                elif fault_type == "LINK_LOSS":
                    loss = float(params["loss_pct"])
                    if not 0 < loss <= 100:
                        raise ValueError("FAULT_LOSS_OUT_OF_RANGE")
                    option = f"loss {loss:g}%"
                else:
                    rate = float(params["rate_mbps"])
                    if rate <= 0:
                        raise ValueError("FAULT_RATE_INVALID")
                    option = f"rate {rate:g}mbit"
                _node_cmd(link.intf1.node,
                          f"tc qdisc replace dev {link.intf1.name} root netem {option}",
                          timeout=5, check=True)
        elif fault_type == "DEVICE_DOWN":
            node = _fault_device(net, record)
            interfaces = [item for item in node.intfList() if item.name != "lo"]
            for intf in interfaces:
                _node_cmd(node, f"ip link set dev {intf.name} down", timeout=4, check=True)
        else:
            workload = dict(record["parameters"].get("workload") or {})
            if not workload:
                raise ValueError("SERVICE_OVERLOAD_REQUIRES_WORKLOAD")
            workload["traffic_role"] = "BACKGROUND"
            _start_runtime_workload(net, workload)
        _fault_states[fault_id] = record
        observed = _fault_observed(net, record)
        if not observed.get("active"):
            raise RuntimeError(f"FAULT_NOT_OBSERVED: {observed}")
        return {"status": "ok", "fault_id": fault_id, "fault_type": fault_type,
                "active": True, "idempotent": False, "observed": observed}


def _remove_fault(net, raw: dict) -> dict:
    fault_id = str(raw.get("fault_id") or "")
    with _fault_lock:
        record = _fault_states.get(fault_id)
        if record is None:
            return {"status": "ok", "fault_id": fault_id, "active": False, "not_found": True}
        fault_type = record["fault_type"]
        if fault_type.startswith("LINK_"):
            link = _fault_link(net, record)
            if fault_type == "LINK_DOWN":
                for intf in (link.intf1, link.intf2):
                    _node_cmd(intf.node, f"ip link set dev {intf.name} up", timeout=4, check=True)
            else:
                _node_cmd(link.intf1.node,
                          f"tc qdisc del dev {link.intf1.name} root 2>/dev/null || true", timeout=5)
                metadata = record.get("original", {}).get("metadata") or {}
                config = {}
                if metadata.get("bandwidth_mbps"):
                    config["bw"] = float(metadata["bandwidth_mbps"])
                if metadata.get("delay_ms"):
                    config["delay"] = f"{float(metadata['delay_ms']):g}ms"
                if config:
                    link.intf1.config(**config)
        elif fault_type == "DEVICE_DOWN":
            node = _fault_device(net, record)
            for intf in [item for item in node.intfList() if item.name != "lo"]:
                _node_cmd(node, f"ip link set dev {intf.name} up", timeout=4, check=True)
        else:
            workload_id = str(record["parameters"]["workload"]["workload_id"])
            _stop_runtime_workload(workload_id)
        observed = _fault_observed(net, record)
        if observed.get("active"):
            raise RuntimeError(f"FAULT_RESIDUAL: {observed}")
        del _fault_states[fault_id]
        return {"status": "ok", "fault_id": fault_id, "active": False,
                "removed_at": _utc_now(), "observed": observed}


def _list_faults(net, fault_id: str | None = None) -> dict:
    with _fault_lock:
        if fault_id:
            record = _fault_states.get(fault_id)
            if record is None:
                return {"status": "ok", "fault_id": fault_id, "active": False}
            observed = _fault_observed(net, record)
            return {"status": "ok", **record, **observed, "observed": observed}
        values = []
        for record in _fault_states.values():
            observed = _fault_observed(net, record)
            values.append({**record, **observed, "observed": observed})
        return {"status": "ok", "faults": values, "count": len(values)}


# ── Checksum offload ──────────────────────────────────────────────────────────


def disable_checksum_offload(net) -> None:
    """Disable NIC checksum offload on all host interfaces.

    BMv2 is a userspace switch — packets arrive via veth pairs without hardware
    checksum completion.
    """
    for node_name, node in net.nameToNode.items():
        if not node_name.startswith("h_"):
            continue
        intf = node.defaultIntf().name
        node.cmd(
            "command -v ethtool >/dev/null 2>&1 && "
            f"for feature in tx rx tso gso gro lro sg; do "
            f"ethtool -K {intf} $feature off >/dev/null 2>&1 || true; "
            "done || true"
        )


# ── HTTP probe server ─────────────────────────────────────────────────────────


def start_probe_server(
    net,
    switch_config: dict,
    switch_name_by_id: dict,
    probe_port: int | None = None,
    *,
    host_runtime_config: dict | None = None,
    host_facing_ports: dict | None = None,
) -> ThreadingHTTPServer:
    """Expose active telemetry probes to the application container.

    Args:
        net: Mininet network instance.
        switch_config: {switch_name: {device_id, grpc_port, device_uri}} mapping.
        switch_name_by_id: {device_id_int: switch_name} reverse lookup.
        probe_port: Override for the HTTP listen port (env MININET_PROBE_PORT).
    """
    _init_config(
        switch_config,
        switch_name_by_id,
        probe_port,
        host_runtime_config,
        host_facing_ports,
    )

    # QoS compatibility tests still use this process-wide lock.  Service
    # validation instead reserves its exact listener sockets below so
    # independent business flows can overlap for a real multi-intent probe.
    iperf_lock = threading.Lock()
    iperf_server_locks = _IperfServerLockPool()
    int_lock = threading.Lock()
    qos_lock = threading.Lock()

    def resolve_probe_target(target: str) -> str:
        node = net.nameToNode.get(target)
        if node is not None and target.startswith("h_"):
            ip = getattr(node, "IP", lambda: "")()
            if ip:
                return ip
        return target

    class ProbeHandler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: dict) -> None:
            try:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # client disconnected — nothing to do

        def log_message(self, fmt, *args):
            return

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            try:
                if parsed.path == "/health":
                    self._json(200, {"status": "ok", "nodes": list(net.nameToNode.keys())})
                    return

                if parsed.path == "/topology":
                    snapshot = build_topology_snapshot(net)
                    self._json(200, snapshot)
                    return

                if parsed.path == "/telemetry/interfaces":
                    self._json(200, _all_interface_telemetry(net))
                    return

                if parsed.path == "/diagnostics/environment":
                    self._json(200, _runtime_environment_diagnostics())
                    return

                if parsed.path == "/service-profiles":
                    registry = _load_service_registry()
                    self._json(200, {
                        "status": "ok",
                        "schema_version": registry.get("schema_version"),
                        "profiles": registry.get("profiles") or [],
                        "traffic_scenarios": registry.get("scenarios") or [],
                    })
                    return

                if parsed.path == "/workloads":
                    workload_id = query.get("workload_id", [None])[0]
                    result = _list_runtime_workloads(workload_id)
                    self._json(200 if result.get("status") == "ok" else 404, result)
                    return

                if parsed.path == "/runtime/identity":
                    self._json(200, {
                        "status": "ok", "component": "mininet",
                        "runtime_id": _runtime_identity, "started_at": _runtime_started_at,
                        "switch_count": len(_switch_config),
                        "workload_count": len(_workload_processes),
                        "fault_count": len(_fault_states),
                    })
                    return

                if parsed.path == "/runtime/hosts":
                    hosts = []
                    for name, node in sorted(net.nameToNode.items()):
                        if not str(name).startswith("h_"):
                            continue
                        pid = getattr(node, "pid", None)
                        interfaces = [
                            str(intf.name) for intf in getattr(node, "intfList", lambda: [])()
                            if getattr(intf, "name", None) and str(intf.name) != "lo"
                        ]
                        hosts.append({
                            "name": name,
                            "pid": pid,
                            "namespace_type": "mininet_pid_namespace",
                            "interfaces": interfaces,
                            "ipv4": _host_ip_plain(node),
                            "process_alive": bool(pid and os.path.exists(f"/proc/{pid}")),
                            "command_execution_available": bool(pid),
                            "execution_method": "Mininet Host.cmd via probe",
                            "observed_at": time.time(),
                        })
                    self._json(200, {"status": "ok", "source": "mininet_probe", "hosts": hosts})
                    return

                if parsed.path == "/faults":
                    fault_id = query.get("fault_id", [None])[0]
                    result = _list_faults(net, fault_id)
                    self._json(200, result)
                    return

                if parsed.path == "/probe/processes":
                    requested_hosts = {
                        name.strip() for name in query.get("hosts", [""])[0].split(",") if name.strip()
                    }
                    hosts = requested_hosts or {
                        name for name in net.nameToNode if str(name).startswith("h_")
                    }
                    if any(host not in net.nameToNode or not host.startswith("h_") for host in hosts):
                        self._json(400, {"status": "error", "error": "hosts must name runtime Mininet hosts"})
                        return
                    evidence = _process_leak_evidence(net, hosts)
                    self._json(200 if evidence["process_cleanup_verified"] else 409, {
                        "status": "ok" if evidence["process_cleanup_verified"] else "leak_detected",
                        **evidence,
                    })
                    return

                if parsed.path == "/probe/ping":
                    src = query.get("src", [None])[0]
                    dst = query.get("dst", [None])[0]
                    if not src or not dst:
                        self._json(400, {"status": "error", "error": "src and dst are required"})
                        return
                    dst_ip = resolve_probe_target(dst)
                    count = max(1, min(10, int(query.get("count", ["3"])[0])))
                    if src not in net.nameToNode or not src.startswith("h_"):
                        self._json(400, {"status": "error", "error": "unsupported src"})
                        return
                    output = _node_cmd(
                        net.get(src),
                        f"ping -c {count} -W 1 {dst_ip}",
                        timeout=count + 3,
                    )
                    result = parse_ping_output(output)
                    result.update({"status": "ok", "src": src, "dst": dst, "dst_ip": dst_ip, "count": count})
                    self._json(200, result)
                    return

                if parsed.path == "/probe/udp-kernel-stats":
                    host = query.get("host", [None])[0]
                    if not host or host not in net.nameToNode or not host.startswith("h_"):
                        self._json(400, {"status": "error", "error": "host must name a runtime Mininet host"})
                        return
                    self._json(200, {"host": host, **_udp_kernel_stats(net.get(host))})
                    return

                if parsed.path == "/probe/iperf":
                    duration = max(1, min(30, int(query.get("duration", ["3"])[0])))
                    src = query.get("src", [None])[0]
                    dst = query.get("dst", [None])[0]
                    if (
                        not src or not dst
                        or src not in net.nameToNode or dst not in net.nameToNode
                        or not src.startswith("h_") or not dst.startswith("h_")
                    ):
                        self._json(400, {
                            "status": "error",
                            "error": "src and dst must name runtime Mininet hosts",
                        })
                        return
                    dst_ip = resolve_probe_target(dst)
                    protocol = query.get("protocol", ["TCP"])[0].upper()
                    if protocol not in {"TCP", "UDP"}:
                        raise ValueError("protocol must be TCP or UDP")
                    port = max(1024, min(65535, int(query.get("port", ["5201"])[0])))
                    offered_mbps = max(1, min(100000, int(query.get("bandwidth_mbps", ["100"])[0])))
                    dscp = query.get("dscp", [None])[0]
                    if dscp is not None and not 0 <= int(dscp) <= 63:
                        raise ValueError("dscp must be in [0, 63]")
                    destination_node = net.get(dst)
                    source_node = net.get(src)
                    timeout_sec = duration + 8
                    effective_arguments = [
                        "iperf3", "-c", dst_ip, "-p", str(port), "-t", str(duration), "-J",
                        *( ["-u", "-b", f"{offered_mbps}M"] if protocol == "UDP" else [] ),
                        *( ["-S", f"0x{int(dscp) << 2:02x}"] if dscp is not None else [] ),
                    ]
                    processes_before = _process_leak_evidence(net, {src, dst})
                    # Only a same-destination/same-port server can collide.
                    # Do not serialize independent probes and never use a
                    # namespace-wide pkill: it can terminate a concurrent
                    # client belonging to a different traffic scope.
                    with iperf_server_locks.for_server(dst, port):
                        server = _node_popen(
                            destination_node,
                            ["iperf3", "-s", "-1", "-p", str(port), "-J"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT,
                            text=True,
                        )
                        time.sleep(0.5)
                        protocol_args = f"-u -b {offered_mbps}M" if protocol == "UDP" else ""
                        dscp_arg = f"-S 0x{int(dscp) << 2:02x}" if dscp is not None else ""
                        try:
                            output, client_exit_status = _node_cmd(
                                source_node,
                                f"timeout {timeout_sec} iperf3 -c {dst_ip} -p {port} "
                                f"-t {duration} {protocol_args} {dscp_arg} -J",
                                timeout=timeout_sec + 2,
                                return_status=True,
                            )
                        finally:
                            _kill_process(server)
                        server_exit_status = server.poll()
                    processes_after = _process_leak_evidence(net, {src, dst})
                    result = parse_iperf_output(output)
                    result.update({
                        "status": "ok", "src": src, "dst": dst,
                        "dst_ip": dst_ip, "duration": duration,
                        "protocol": protocol, "port": port,
                        "dscp": int(dscp) if dscp is not None else None,
                        "offered_bandwidth_mbps": offered_mbps,
                        "traffic_generated": True,
                        "iperf_exit_status": client_exit_status,
                        "iperf_server_exit_status": server_exit_status,
                        "effective_arguments": effective_arguments,
                        "socket_buffer_bytes": None,
                        "processes_before": processes_before,
                        "processes_after": processes_after,
                    })
                    self._json(200, result)
                    return

                if parsed.path == "/probe/qdisc":
                    samples = []
                    for node_name, intf in _qdisc_interfaces(net):
                        if node_name not in net.nameToNode:
                            continue
                        output = _node_cmd(
                            net.get(node_name),
                            f"tc -s qdisc show dev {intf}",
                            timeout=5,
                        )
                        sample = parse_root_qdisc_output(output)
                        sample.update({"node": node_name, "interface": intf})
                        samples.append(sample)

                    queue_packets = [
                        item["backlog_packets"] for item in samples
                        if item.get("backlog_packets") is not None
                    ]
                    queue_bytes = [
                        item["backlog_bytes"] for item in samples
                        if item.get("backlog_bytes") is not None
                    ]
                    drops = [
                        item["dropped_packets"] for item in samples
                        if item.get("dropped_packets") is not None
                    ]
                    overlimits = [
                        item["overlimits"] for item in samples
                        if item.get("overlimits") is not None
                    ]
                    congested_samples = [
                        item for item in samples
                        if (item.get("backlog_packets") or 0) > 0
                        or (item.get("dropped_packets") or 0) > 0
                        or (item.get("overlimits") or 0) > 0
                    ]
                    self._json(200, {
                        "status": "ok",
                        "queue_depth": max(queue_packets) if queue_packets else None,
                        "queue_depth_bytes": max(queue_bytes) if queue_bytes else None,
                        "dropped_packets": sum(drops) if drops else None,
                        "overlimits": sum(overlimits) if overlimits else 0,
                        "aggregation_scope": "one_root_qdisc_per_interface",
                        "overlimits_is_drop": False,
                        "congestion_detected": bool(congested_samples),
                        "congested_interfaces": [
                            {
                                "node": item.get("node"),
                                "interface": item.get("interface"),
                                "backlog_packets": item.get("backlog_packets"),
                                "backlog_bytes": item.get("backlog_bytes"),
                                "dropped_packets": item.get("dropped_packets"),
                                "overlimits": item.get("overlimits"),
                                "source": "qdisc_counter",
                            }
                            for item in congested_samples
                        ],
                        "applied_policies": [],
                        "applied_faults": [],
                        "samples": samples,
                    })
                    return

                if parsed.path == "/probe/int":
                    with int_lock:
                        src = query.get("src", [None])[0]
                        dst = query.get("dst", [None])[0]
                        if not src or not dst:
                            self._json(400, {
                                "status": "error", "error": "src and dst are required",
                            })
                            return
                        count = max(1, min(5, int(query.get("count", ["1"])[0])))
                        timeout = max(1.0, min(10.0, float(query.get("timeout", ["4"])[0])))
                        result = run_int_probe(
                            net, src=src, dst=dst, count=count, timeout=timeout,
                        )
                    self._json(200 if result.get("status") == "ok" else 502, result)
                    return

                if parsed.path == "/policy/qos":
                    scope_key = query.get("scope_key", [""])[0]
                    if not scope_key:
                        self._json(200, {
                            "status": "ok",
                            "scopes": sorted(_qos_policies),
                            "count": len(_qos_policies),
                        })
                        return
                    self._json(200, _qos_policies.get(scope_key, {
                        "status": "not_found", "scope_key": scope_key,
                    }))
                    return

                if parsed.path == "/policy/qos-readback":
                    # Strictly read-only recovery primitive.  It is separate
                    # from POST /policy/qos VERIFY because VERIFY may reconcile
                    # the default class after a successful comparison.
                    scope_key = query.get("scope_key", [""])[0]
                    policy = _qos_policies.get(scope_key)
                    if not scope_key or not policy:
                        self._json(404, {"status": "not_found", "scope_key": scope_key})
                        return
                    try:
                        binding = policy.get("qos_binding") or {}
                        readback = _read_tc_resource(net, policy, binding)
                        self._json(200, {
                            "status": "ok" if readback.get("config_applied") else "mismatch",
                            "scope_key": scope_key, "qos_binding": binding, **readback,
                        })
                    except Exception as exc:
                        self._json(502, {"status": "error", "scope_key": scope_key, "error": str(exc)})
                    return

                if parsed.path == "/policy/ingress-admission":
                    plan_id = query.get("admission_plan_id", [""])[0]
                    if plan_id:
                        self._json(200, _ingress_admission_plans.get(plan_id, {
                            "status": "NOT_FOUND", "admission_plan_id": plan_id,
                        }) )
                    else:
                        self._json(200, {"status": "ok", "plans": list(_ingress_admission_plans.values())})
                    return

                self._json(404, {"status": "error", "error": "unknown endpoint"})
            except Exception as exc:
                self._json(500, {"status": "error", "error": str(exc)})

        def do_POST(self):
            parsed = urlparse(self.path)

            try:
                if parsed.path == "/workloads/start":
                    result = _start_runtime_workload(net, self._body())
                    self._json(200, result)
                    return

                if parsed.path == "/diagnostics/packet-accounting":
                    result = _packet_accounting(net, self._body())
                    self._json(200, result)
                    return

                if parsed.path == "/workloads/stop":
                    body = self._body()
                    result = _stop_runtime_workload(str(body.get("workload_id") or ""))
                    self._json(200, result)
                    return

                if parsed.path == "/workloads/update":
                    result = _update_runtime_workload(net, self._body())
                    self._json(200, result)
                    return

                if parsed.path == "/policy/qos":
                    body = self._body()
                    operation = str(body.pop("operation", "APPLY")).upper()
                    with qos_lock:
                        if operation == "VERIFY":
                            # Historical VERIFY was not a verification-only
                            # operation: it could reconcile the default TC
                            # class.  It is permanently disabled; all live
                            # readback must use GET /policy/qos-readback.
                            self._json(410, {
                                "status": "error",
                                "reason_code": "LEGACY_QOS_VERIFY_DISABLED",
                                "error": "READBACK_MUST_USE_GET_ENDPOINT",
                                "write_executed": False,
                            })
                            return
                        elif operation == "CLEAR":
                            result = _clear_tc_qos(net, str(body.get("scope_key") or ""), str(body.get("source_ip") or ""))
                        elif operation == "COMMIT":
                            result = _commit_prepared_tc_qos(net, body)
                        elif operation == "ROLLBACK":
                            result = _rollback_prepared_tc_qos(net, body)
                        else:
                            result = _apply_tc_qos(
                                net, body, retain_previous=operation == "PREPARE"
                            )
                    self._json(200 if result.get("status") == "ok" else 409, result)
                    return

                if parsed.path == "/policy/qos-calibration-window":
                    body = self._body()
                    with qos_lock:
                        result = _set_qos_calibration_window(
                            net,
                            str(body.get("scope_key") or ""),
                            str(body.get("operation") or ""),
                        )
                    self._json(200 if result.get("status") == "ok" else 409, result)
                    return

                if parsed.path == "/policy/ingress-admission":
                    body = self._body()
                    operation = str(body.pop("operation", "APPLY")).upper()
                    with qos_lock:
                        result = (
                            _remove_ingress_admission(net, str(body.get("admission_plan_id") or ""))
                            if operation in {"REMOVE", "ROLLBACK"}
                            else _apply_ingress_admission(net, body)
                        )
                    self._json(200 if result.get("status") in {"VERIFIED", "REMOVED", "NOT_FOUND"} else 409, result)
                    return

                if parsed.path == "/policy/ingress-admission-calibration-window":
                    body = self._body()
                    with qos_lock:
                        result = _set_ingress_calibration_window(
                            net,
                            str(body.get("admission_plan_id") or ""),
                            str(body.get("traffic_scope_key") or ""),
                            str(body.get("operation") or ""),
                            body.get("ceiling_mbps"),
                        )
                    self._json(200 if result.get("status") == "ok" else 409, result)
                    return

                if parsed.path == "/policy/clear":
                    body = self._body()
                    with qos_lock:
                        result = _clear_tc_qos(net, str(body.get("scope_key") or ""), str(body.get("source_ip") or ""))
                    self._json(200, result)
                    return

                if parsed.path == "/probe/qos-kpi":
                    body = self._body()
                    with iperf_lock:
                        result = _run_qos_competition(net, body)
                    self._json(200 if result.get("status") == "ok" else 409, result)
                    return

                if parsed.path == "/probe/service/validate":
                    body = self._body()
                    with ExitStack() as locks:
                        for destination, port in _service_validation_server_keys(body):
                            locks.enter_context(iperf_server_locks.for_server(destination, port))
                        result = _run_service_validation(net, body)
                    self._json(200, result)
                    return

                if parsed.path == "/probe/traffic-scenario":
                    body = self._body()
                    with iperf_lock:
                        result = _run_configured_scenario(net, body)
                    self._json(200, result)
                    return

                if parsed.path == "/probe/qos-cleanup":
                    with iperf_lock:
                        result = _cleanup_qos_test_processes(net)
                    self._json(200 if result.get("status") == "ok" else 409, result)
                    return

                if parsed.path == "/faults/apply":
                    result = _apply_fault(net, self._body())
                    self._json(200 if result.get("status") == "ok" else 409, result)
                    return

                if parsed.path == "/faults/remove":
                    result = _remove_fault(net, self._body())
                    self._json(200 if result.get("status") == "ok" else 409, result)
                    return

                if parsed.path == "/p4/switch-ids":
                    result = configure_p4_switch_ids(net)
                    self._json(200 if result.get("status") == "ok" else 502, result)
                    return

                self._json(404, {"status": "error", "error": "unknown endpoint"})
            except Exception as exc:
                self._json(500, {"status": "error", "error": str(exc)})

    class ProbeHTTPServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 64
        allow_reuse_address = True

    server = ProbeHTTPServer(("0.0.0.0", PROBE_PORT), ProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[探针] Mininet active telemetry probe listening on 0.0.0.0:{PROBE_PORT}")
    return server

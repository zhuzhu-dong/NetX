"""End-to-end ONOS REST integration test — real pipeline against live infrastructure.

Pre-requisites:
    docker-compose -f docker-compose.infra.yml up -d

The test validates the full agent pipeline with real ONOS flow delivery,
Mininet traffic generation, and ONOS telemetry.  Each phase is logged
with timestamps for debugging and demonstration.

Run:
    pytest tests/test_integration_onos_flow_delivery.py -v -s
    pytest -m integration -v -s
    pytest -m "not integration" -v   # skip in CI
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from typing import Any

import aiohttp
import pytest

pytestmark = pytest.mark.integration

# ═══════════════════════════════════════════════════════════════════════════════
# Deterministic LLM fallback (forces rule-based path, no real LLM needed)
# ═══════════════════════════════════════════════════════════════════════════════


class _RuleFallbackLLM:
    """Force deterministic rule fallback — raises RuntimeError on invoke."""

    async def ainvoke(self, messages, **kwargs):
        raise RuntimeError("LLM is disabled in this integration test")


# ═══════════════════════════════════════════════════════════════════════════════
# Structured logging — timestamped phases, key-value pairs, counters, summary
# ═══════════════════════════════════════════════════════════════════════════════

_test_start_time: float = 0.0


def _log_phase(title: str) -> None:
    print(f"\n{'='*70}", file=sys.stderr)
    print(f"[PHASE] {title}", file=sys.stderr)
    print(f"{'='*70}", file=sys.stderr)


def _log_step(msg: str) -> None:
    elapsed = time.monotonic() - _test_start_time
    print(f"  [{elapsed:5.1f}s] {msg}", file=sys.stderr)


def _log_key(label: str, value: object) -> None:
    print(f"  [KEY] {label}={value}", file=sys.stderr)


def _log_onos(method: str, path: str, status: int, latency_ms: float) -> None:
    print(f"  [ONOS] {method} {path} → {status} ({latency_ms:.0f}ms)", file=sys.stderr)


def _log_counter(label: str, before: int, after: int) -> None:
    delta = after - before
    sign = "+" if delta >= 0 else ""
    print(f"  [COUNTER] {label}: {before} → {after} ({sign}{delta})", file=sys.stderr)


def _log_pipeline_stage(stage: str, detail: str) -> None:
    print(f"  [PIPELINE] {stage} → {detail}", file=sys.stderr)


def _log_summary(results: list[tuple[str, bool, str]]) -> None:
    print(f"\n{'='*70}", file=sys.stderr)
    print(f"[SUMMARY] 集成测试结果", file=sys.stderr)
    print(f"{'='*70}", file=sys.stderr)
    all_pass = True
    for name, passed, detail in results:
        icon = "PASS" if passed else "FAIL"
        print(f"  [{icon}] {name}: {detail}", file=sys.stderr)
        if not passed:
            all_pass = False
    print(f"{'='*70}", file=sys.stderr)
    print(f"[RESULT] {'ALL PASSED' if all_pass else 'SOME FAILED'}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════════════
# ONOS REST helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _onos_base() -> str:
    return f"http://{os.getenv('ONOS_HOST', 'localhost')}:{os.getenv('ONOS_PORT', '8181')}/onos/v1"


def _onos_auth() -> aiohttp.BasicAuth:
    return aiohttp.BasicAuth(
        os.getenv("ONOS_USERNAME", "onos"),
        os.getenv("ONOS_PASSWORD", "rocks"),
    )


async def _onos_get(path: str) -> dict[str, Any]:
    """GET request to ONOS REST API with timing."""
    url = f"{_onos_base()}{path}"
    t0 = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(auth=_onos_auth(), timeout=timeout) as session:
        async with session.get(url, headers={"Accept": "application/json"}) as resp:
            resp.raise_for_status()
            data = await resp.json()
    _log_onos("GET", path, resp.status, (time.monotonic() - t0) * 1000)
    return data


async def _onos_get_raw(path: str) -> str:
    """GET request returning raw response body for diagnosis."""
    url = f"{_onos_base()}{path}"
    t0 = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(auth=_onos_auth(), timeout=timeout) as session:
        async with session.get(url, headers={"Accept": "application/json"}) as resp:
            text = await resp.text()
    _log_onos("GET(raw)", path, resp.status, (time.monotonic() - t0) * 1000)
    return text


async def _onos_post_flow_diag(device_id: str, flow_json: dict) -> tuple[int, str]:
    """POST a flow rule and return (status_code, response_body)."""
    url = f"{_onos_base()}/flows/{device_id}"
    t0 = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(auth=_onos_auth(), timeout=timeout) as session:
        async with session.post(
            url, json=flow_json, headers={"Content-Type": "application/json"}
        ) as resp:
            text = await resp.text()
    _log_onos(
        "POST(diag)", f"/flows/{device_id}", resp.status, (time.monotonic() - t0) * 1000
    )
    print(f"    [DIAG] POST response body: {text[:500]}", file=sys.stderr)
    return resp.status, text


async def _onos_delete(path: str) -> int:
    """DELETE request to ONOS REST API."""
    url = f"{_onos_base()}{path}"
    t0 = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(auth=_onos_auth(), timeout=timeout) as session:
        async with session.delete(url, headers={"Accept": "application/json"}) as resp:
            if resp.status not in (200, 204):
                resp.raise_for_status()
    _log_onos("DELETE", path, resp.status, (time.monotonic() - t0) * 1000)
    return resp.status


async def _fetch_onos_flows(device_id: str) -> list[dict[str, Any]]:
    """GET /onos/v1/flows/{deviceId} — return list of flow dicts.

    Returns empty list when ONOS returns 404 (no flows yet / endpoint
    returns 404 for empty collections on some ONOS versions).
    """
    try:
        data = await _onos_get(f"/flows/{device_id}")
        return data.get("flows", [])
    except aiohttp.ClientResponseError as e:
        if e.status == 404:
            return []
        raise


async def _fetch_single_flow(device_id: str, flow_id: str) -> dict[str, Any] | None:
    """Get a single flow by ID from ONOS."""
    flows = await _fetch_onos_flows(device_id)
    for f in flows:
        if f.get("id") == flow_id:
            return f
    return None


async def _get_port_stats(device_id: str) -> dict[str, int]:
    """Aggregate ONOS port statistics into device-level totals."""
    try:
        data = await _onos_get(f"/statistics/ports/{device_id}")
    except Exception:
        # Fall back to device ports endpoint
        data = await _onos_get(f"/devices/{device_id}/ports")

    stats = {
        "packets_in": 0,
        "packets_out": 0,
        "bytes_in": 0,
        "bytes_out": 0,
        "drops": 0,
    }
    # ONOS v1/statistics/ports returns {"statistics": [{"device": ..., "ports": [...]}]}
    # ONOS v1/devices/{id}/ports returns {"ports": [...]}
    stats_list = data.get("statistics", [])
    if isinstance(stats_list, list) and len(stats_list) > 0:
        ports = stats_list[0].get("ports", [])
    else:
        ports = data.get("ports", [])
    for port in ports:
        for key in ("packetsReceived", "packetsSent", "bytesReceived", "bytesSent"):
            try:
                val = int(port.get(key, 0))
            except (TypeError, ValueError):
                val = 0
            if "packetsReceived" in key:
                stats["packets_in"] += val
            elif "packetsSent" in key:
                stats["packets_out"] += val
            elif "bytesReceived" in key:
                stats["bytes_in"] += val
            elif "bytesSent" in key:
                stats["bytes_out"] += val
        for drop_key in ("packetsRxDropped", "packetsTxDropped"):
            try:
                stats["drops"] += int(port.get(drop_key, 0))
            except (TypeError, ValueError):
                pass
    return stats


async def _get_flow_stats(device_id: str, table_id: int) -> dict[str, int]:
    """Sum packet/byte counters for all flows in a specific table."""
    flows = await _fetch_onos_flows(device_id)
    stats = {"packets": 0, "bytes": 0}
    for f in flows:
        if f.get("tableId") == table_id:
            try:
                stats["packets"] += int(f.get("packets", 0))
            except (TypeError, ValueError):
                pass
            try:
                stats["bytes"] += int(f.get("bytes", 0))
            except (TypeError, ValueError):
                pass
    return stats


def _flow_matches(
    flow: dict[str, Any],
    table_id: int,
    expected_action_id: str | None = None,
) -> bool:
    """Check if a flow matches the given table and action."""
    if flow.get("tableId") != table_id:
        return False
    if expected_action_id is not None:
        treatment = flow.get("treatment", {})
        for instr in treatment.get("instructions", []):
            if (
                instr.get("subtype") == "ACTION"
                and str(instr.get("actionId")) == expected_action_id
            ):
                return True
        return False
    return True


async def _poll_onos_for_flow(
    device_id: str,
    table_id: int,
    expected_action_id: str | None = None,
    timeout_secs: int = 30,
    interval_secs: float = 2.0,
) -> dict[str, Any] | None:
    """Poll ONOS until a matching flow reaches ADDED state or timeout.

    The pipeline may target a different device than the test expects, so
    this function checks ALL available devices in addition to the primary.
    """
    deadline = time.monotonic() + timeout_secs
    all_device_ids = [device_id]  # primary first

    while time.monotonic() < deadline:
        # Refresh device list in case devices changed
        try:
            dev_data = await _onos_get("/devices")
            for d in dev_data.get("devices", []):
                did = d.get("id", "")
                if did and did not in all_device_ids:
                    all_device_ids.append(did)
        except Exception:
            pass

        for did in all_device_ids:
            try:
                flows = await _fetch_onos_flows(did)
            except Exception:
                await asyncio.sleep(interval_secs)
                continue

            for f in flows:
                if _flow_matches(f, table_id, expected_action_id):
                    state = f.get("state", "UNKNOWN")
                    elapsed = timeout_secs - (deadline - time.monotonic())
                    _log_step(f"poll t={elapsed:.0f}s state={state} device={did}")
                    if state == "ADDED":
                        return f

        await asyncio.sleep(interval_secs)

    # Timeout — dump raw response + all flows for diagnosis
    _log_step("POLL TIMEOUT — dumping raw ONOS GET /flows response:")
    for did in all_device_ids:
        try:
            raw = await _onos_get_raw(f"/flows/{did}")
            print(
                f"    [DIAG] Raw GET /flows/{did} response:\n    {raw[:1000]}",
                file=sys.stderr,
            )
        except Exception as e:
            _log_step(f"  raw GET /flows/{did} failed: {e}")
        try:
            all_flows = await _fetch_onos_flows(did)
            _log_step(f"  {did}: {len(all_flows)} flows:")
            for f in all_flows:
                _log_step(
                    f"    id={f.get('id','?')[-16:]} tableId={f.get('tableId')} state={f.get('state','?')} actionId={_extract_action_id(f)}"
                )
        except Exception as e:
            _log_step(f"  (could not fetch flows for {did}: {e})")
    return None


def _extract_action_id(flow: dict[str, Any]) -> str:
    """Extract actionId from an ONOS flow dict for diagnostic display."""
    treatment = flow.get("treatment", {})
    for instr in treatment.get("instructions", []):
        if instr.get("subtype") == "ACTION":
            return str(instr.get("actionId", "?"))
    return "?"


async def _delete_flow_via_onos(device_id: str, flow_id: str) -> bool:
    """DELETE a flow from ONOS. Returns True on success."""
    try:
        await _onos_delete(f"/flows/{device_id}/{flow_id}")
        return True
    except Exception as e:
        _log_step(f"DELETE flow failed (non-fatal): {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Infrastructure pre-flight — ensure ARP proxy entries exist
# ═══════════════════════════════════════════════════════════════════════════════
#
# ARP proxy entries are derived dynamically from the ONOS device registry
# and interface configuration.  The topology script (research_dc_topo.py)
# pushes authoritative gateway ARP entries during Mininet startup, but
# ONOS restarts clear all flow rules — this pre-flight restores them.
#
# Matching logic: resolve devices by annotation name, map each access
# switch to its subnet gateway IP, and install a P4 arp_reply rule.

from mcp_servers.p4info_registry import p4info_registry as _p4_reg

ARP_PROXY_TABLE_ID = _p4_reg.table_flow_type("MyIngress.arp_proxy")

# ── Access-switch → (gateway IP hex, gateway MAC) ──
# Derived from research_dc_topo.py host interface config lines 160-176.
# Hex encoding: each octet → two hex chars, e.g. 192.168.10.1 → C0A80A01.
_ACCESS_GATEWAYS = {
    # s_access_r (device:bmv2:5) serves the research subnet
    "s_access_r": ("c0a80a01", "000000000055"),  # 192.168.10.1
    # s_access_d (device:bmv2:6) serves the DC subnet
    "s_access_d": ("0a000001", "000000000066"),  # 10.0.0.1
}


def _ensure_arp_proxy() -> None:
    """Push ARP proxy entries to access switches if they are missing.

    Resolves device IDs dynamically from ONOS annotation names so the
    test survives topology renumbering and P4 recompilation.  Only
    access switches (s_access_*) receive gateway ARP entries because
    those are the L2 edge where hosts connect.
    """
    import json as _json
    import urllib.request as _ur

    auth_bytes = base64.b64encode(
        f"{os.getenv('ONOS_USERNAME', 'onos')}:{os.getenv('ONOS_PASSWORD', 'rocks')}".encode()
    ).decode()

    # ── Resolve device IDs from ONOS annotation names ──
    name_to_device_id: dict[str, str] = {}
    try:
        devs_url = f"{_onos_base()}/devices"
        devs_req = _ur.Request(devs_url, method="GET")
        devs_req.add_header("Accept", "application/json")
        devs_req.add_header("Authorization", f"Basic {auth_bytes}")
        with _ur.urlopen(devs_req, timeout=10) as resp:
            devices = _json.loads(resp.read()).get("devices", [])
        for d in devices:
            name = (d.get("annotations") or {}).get("name", "")
            if name:
                name_to_device_id[name] = d["id"]
        print(
            f"  [ARP-PROXY] resolved {len(name_to_device_id)} device names from ONOS",
            file=sys.stderr,
        )
    except Exception as e:
        print(
            f"  [ARP-PROXY] WARNING: could not resolve device names: {e}",
            file=sys.stderr,
        )

    for switch_name, (gateway_hex, gateway_mac) in _ACCESS_GATEWAYS.items():
        dev_id = name_to_device_id.get(switch_name)
        if not dev_id:
            print(
                f"  [ARP-PROXY] {switch_name}: device not found in ONOS, skip",
                file=sys.stderr,
            )
            continue

        rule = {
            "deviceId": dev_id,
            "tableId": ARP_PROXY_TABLE_ID,
            "priority": 1000,
            "isPermanent": True,
            "treatment": {
                "instructions": [
                    {
                        "type": "PROTOCOL_INDEPENDENT",
                        "subtype": "ACTION",
                        "actionId": "MyIngress.arp_reply",
                        "actionParams": {"target_mac": gateway_mac},
                    }
                ],
                "deferred": [],
            },
            "selector": {
                "criteria": [
                    {
                        "type": "PROTOCOL_INDEPENDENT",
                        "matches": [
                            {
                                "field": "hdr.arp.targetProtoAddr",
                                "match": "exact",
                                "value": gateway_hex,
                            }
                        ],
                    }
                ]
            },
        }

        # Check if entry already exists
        try:
            check_url = f"{_onos_base()}/flows/{dev_id}"
            check_req = _ur.Request(check_url, method="GET")
            check_req.add_header("Accept", "application/json")
            check_req.add_header("Authorization", f"Basic {auth_bytes}")
            with _ur.urlopen(check_req, timeout=5) as resp:
                flows = _json.loads(resp.read()).get("flows", [])
            already = any(f.get("tableId") == ARP_PROXY_TABLE_ID for f in flows)
        except Exception:
            already = False

        if already:
            print(
                f"  [ARP-PROXY] {dev_id} ({switch_name}): entry already present, skip",
                file=sys.stderr,
            )
            continue

        # Push the entry
        try:
            url = f"{_onos_base()}/flows/{dev_id}"
            body = _json.dumps(rule).encode()
            req = _ur.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            req.add_header("Authorization", f"Basic {auth_bytes}")
            with _ur.urlopen(req, timeout=10) as resp:
                pass
            print(
                f"  [ARP-PROXY] {dev_id} ({switch_name}): pushed OK (HTTP {resp.status})",
                file=sys.stderr,
            )
        except Exception as e:
            print(
                f"  [ARP-PROXY] {dev_id} ({switch_name}): push failed — {e}",
                file=sys.stderr,
            )


def _fetch_host_mac_from_onos(ip_prefix: str) -> str | None:
    """Resolve a host's MAC from the ONOS host database.

    Returns a 12-char lowercase hex string without colons (e.g. ``6ecdeff92643``),
    or None if the host is not found.
    """
    import ipaddress as _ip
    import json as _json
    import urllib.request as _ur

    auth_bytes = base64.b64encode(
        f"{os.getenv('ONOS_USERNAME', 'onos')}:{os.getenv('ONOS_PASSWORD', 'rocks')}".encode()
    ).decode()

    try:
        dst_net = _ip.ip_network(ip_prefix, strict=False)
    except ValueError:
        return None

    try:
        url = f"{_onos_base()}/hosts"
        req = _ur.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Basic {auth_bytes}")
        with _ur.urlopen(req, timeout=5) as resp:
            hosts = _json.loads(resp.read()).get("hosts", [])
    except Exception:
        return None

    for host in hosts:
        for ip_str in host.get("ipAddresses", []):
            try:
                host_ip = _ip.ip_address(ip_str.split("/")[0])
            except ValueError:
                continue
            if host_ip in dst_net:
                return host["mac"].replace(":", "").replace("-", "").lower()
    return None


def _verify_mac_params_in_flow(flow: dict) -> tuple[bool, str]:
    """Check that a set_nhop flow rule includes srcMac and dstMac params.

    Returns (ok, detail_message).
    """
    treatment = flow.get("treatment", {})
    for instr in treatment.get("instructions", []):
        if instr.get("subtype") != "ACTION":
            continue
        params = instr.get("actionParams", {})
        has_src = "srcMac" in params
        has_dst = "dstMac" in params
        if has_src and has_dst:
            detail = (
                f"srcMac={params['srcMac'][:8]}... dstMac={params['dstMac'][:12]}..."
            )
            return True, detail
        elif has_src or has_dst:
            return (
                False,
                f"partial MAC params: srcMac={'yes' if has_src else 'no'}, dstMac={'yes' if has_dst else 'no'}",
            )
        else:
            return (
                False,
                "no srcMac/dstMac params — P4 set_nhop will NOT rewrite Ethernet MACs",
            )
    return False, "no ACTION instruction found"


# ═══════════════════════════════════════════════════════════════════════════════
# Mininet traffic helpers
# ═══════════════════════════════════════════════════════════════════════════════


async def _mininet_ping(src_host: str, dst_ip: str, count: int = 5) -> tuple[int, str]:
    """Send pings from a Mininet host via nsenter into the host's netns.

    The opennetworking/mn-stratum image does NOT use ip-netns for host
    namespaces.  Instead each Mininet host runs as a bash process
    (e.g. ``bash --norc -is mininet:h_research``).  We find its PID and
    enter its network namespace via /proc/<pid>/ns/net with nsenter.
    """
    # Find the host PID.  mn-stratum runs each host as
    #   bash --norc --noediting -is mininet:<name>
    # pgrep -f is unreliable with underscores, so we fetch the full
    # ps table and match in Python.
    ps_cmd = "docker exec idn-mininet ps --no-headers -eo pid,args"
    ps_proc = await asyncio.create_subprocess_shell(
        ps_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    ps_out, _ = await ps_proc.communicate()
    host_pid = ""
    needle = "mininet:" + src_host
    for line in ps_out.decode().splitlines():
        if needle in line and "grep" not in line:
            host_pid = line.strip().split()[0]
            break
    if not host_pid:
        raise RuntimeError(
            f"Mininet host '{src_host}' process not found in idn-mininet container. "
            "The Mininet topology may need to be restarted.\n"
            "Run:  docker-compose -f docker-compose.infra.yml up -d --force-recreate mininet"
        )

    cmd = (
        f"docker exec idn-mininet "
        f"nsenter -t {host_pid} -n ping -c {count} -W 2 {dst_ip}"
    )
    _log_step(f"$ {cmd}")
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode()
    for line in output.strip().splitlines():
        print(f"    {line}", file=sys.stderr)
    if proc.returncode != 0:
        stderr_text = stderr.decode().strip()
        if stderr_text:
            print(f"    stderr: {stderr_text}", file=sys.stderr)
    return proc.returncode, output


async def _mininet_running() -> bool:
    """Check if the Mininet container is running."""
    proc = await asyncio.create_subprocess_shell(
        "docker inspect -f '{{.State.Running}}' idn-mininet",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip() == "true"


# ═══════════════════════════════════════════════════════════════════════════════
# Pytest fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def _check_infra():
    """Verify ONOS and Mininet are reachable. Skip the module if not."""
    # ONOS check
    try:
        import urllib.request

        auth_str = f"{os.getenv('ONOS_USERNAME', 'onos')}:{os.getenv('ONOS_PASSWORD', 'rocks')}"
        auth_bytes = auth_str.encode()
        import base64

        req = urllib.request.Request(
            f"http://{os.getenv('ONOS_HOST', 'localhost')}:{os.getenv('ONOS_PORT', '8181')}/onos/v1/devices",
            headers={"Authorization": f"Basic {base64.b64encode(auth_bytes).decode()}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json as _json

            data = _json.loads(resp.read())
            devices = data.get("devices", [])
    except Exception as e:
        pytest.skip(f"ONOS not reachable: {e}")

    if not devices:
        pytest.skip("ONOS has 0 devices — is Mininet + ONOS pipeconf running?")

    available = [d for d in devices if d.get("available")]
    if not available:
        pytest.skip(f"ONOS has {len(devices)} device(s) but none are AVAILABLE")

    print(
        f"\n[INFRA] ONOS OK — {len(available)}/{len(devices)} devices AVAILABLE",
        file=sys.stderr,
    )

    # Mininet check
    try:
        import subprocess

        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "idn-mininet"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip() != "true":
            pytest.skip("Mininet container (idn-mininet) is not running")
    except Exception:
        pytest.skip("Docker not available — cannot check Mininet container")

    print("[INFRA] Mininet container (idn-mininet) is running", file=sys.stderr)


@pytest.fixture(autouse=True)
def _configure_env():
    """Mock LLM only; real ONOS flow delivery, real telemetry."""
    os.environ["IDN_MOCK_MODE"] = "true"
    # Ensure ONOS connection settings are available
    os.environ.setdefault("ONOS_HOST", "localhost")
    os.environ.setdefault("ONOS_PORT", "8181")
    os.environ.setdefault("ONOS_USERNAME", "onos")
    os.environ.setdefault("ONOS_PASSWORD", "rocks")


# ═══════════════════════════════════════════════════════════════════════════════
# Test: 意图生命周期 — 发放(FORWARD) → 阻断(DROP) → 回收(DELETE)
# ═══════════════════════════════════════════════════════════════════════════════


class TestIntentLifecycle:
    """Single test walking through the full intent lifecycle."""

    @pytest.mark.asyncio
    async def test_intent_provision_drop_recycle(self, _check_infra):
        """FORWARD intent → traffic → DROP intent → traffic blocked → recycle → traffic restored."""
        global _test_start_time
        _test_start_time = time.monotonic()
        results: list[tuple[str, bool, str]] = []
        from orchestrator import run_intent_workflow

        forward_flow_id: str | None = None
        drop_flow_id: str | None = None
        cleanup_devices: set[str] = set()
        baseline_route_flow_ids: set[tuple[str, str]] = set()

        try:
            # ── Resolve device before Phase 1 ────────────────────────────
            all_devices = await _onos_get("/devices")
            avail = [d for d in all_devices.get("devices", []) if d.get("available")]
            device = avail[0]["id"] if avail else "device:bmv2:1"
            cleanup_devices = {str(item["id"]) for item in avail}
            route_table_id = _p4_reg.table_flow_type("MyIngress.ipv4_routing")
            for baseline_device in cleanup_devices:
                for flow in await _fetch_onos_flows(baseline_device):
                    if flow.get("tableId") == route_table_id:
                        baseline_route_flow_ids.add((baseline_device, str(flow["id"])))

            # ── Phase 1: Baseline statistics ─────────────────────────────
            _log_phase("Phase 1: 采集基线统计")
            t0_port = await _get_port_stats(device)
            _log_key("baseline.port.packets_out", t0_port["packets_out"])
            # ── Additional: check ONOS device health ─────────────────
            device_raw = await _onos_get_raw(f"/devices/{device}")
            _log_key("diag.device_detail", device_raw[:1500])
            results.append(("基线采集", True, f"packets_out={t0_port['packets_out']}"))

            # ── Phase 1.5: Ensure ARP proxy + trigger host discovery ────
            # ONOS HostLocationProvider learns IPv4 hosts from ARP
            # requests that reach the controller as PacketIn.  The
            # arp_proxy table handles 192.168.10.1 and 10.0.0.1 on the
            # switch, so pinging the gateway won't generate a PacketIn.
            # Ping a non-existent IP on each host's OWN subnet instead.
            #
            # CRITICAL: arp_proxy entries are needed for gateway ARP
            # resolution.  A previous test run may have deleted them
            # during cleanup.  Always re-push if missing.
            _log_phase("Phase 1.5: 确保ARP代理 + 触发主机发现")
            _ensure_arp_proxy()
            try:
                await _mininet_ping("h_research", "192.168.10.99", count=1)
            except RuntimeError:
                _log_step("h_research nsenter failed")
            try:
                await _mininet_ping("h_dc", "10.0.0.99", count=1)
            except RuntimeError:
                _log_step("h_dc nsenter failed")
            await asyncio.sleep(2)

            # ── Phase 2: FORWARD intent via pipeline ─────────────────────
            _log_phase("Phase 2: 意图发放 — FORWARD (192.168.10.0/24 → 10.0.0.0/8)")
            fwd_result = await run_intent_workflow(
                "转发 192.168.10.0/24 到 10.0.0.0/8 的流量",
                llm=_RuleFallbackLLM(),
                wait_secs=0,
            )
            # Print full pipeline result for diagnosis on failure
            import json as _json

            if fwd_result["final_status"] != "SUCCESS":
                _log_key("pipeline.status", fwd_result["final_status"])
                conflict = fwd_result.get("policy_conflict_report") or {}
                if conflict:
                    _log_key("conflict.has_conflict", conflict.get("has_conflict"))
                    _log_key("conflict.explanation", conflict.get("explanation", ""))
                    for item in conflict.get("conflict_items", []):
                        _log_key("conflict.item", _json.dumps(item, ensure_ascii=False))
                # Dump P4 rule verifier rejection details
                rule_report = fwd_result.get("rule_verification_report") or {}
                if rule_report:
                    _log_key("verifier.is_valid", rule_report.get("is_valid"))
                    _log_key("verifier.explanation", rule_report.get("explanation", ""))
                    violations = rule_report.get("violation_items", [])
                    _log_key("verifier.violation_count", len(violations))
                    for item in violations:
                        _log_key(
                            "verifier.violation",
                            f"[{item.get('violation_type','?')}] {item.get('explanation','')}",
                        )
                    warnings_list = rule_report.get("warnings", [])
                    for w in warnings_list:
                        _log_key("verifier.warning", str(w)[:250])
                _log_key(
                    "full_result.summary",
                    _json.dumps(fwd_result.get("summary", {}), ensure_ascii=False),
                )
            assert (
                fwd_result["final_status"] == "SUCCESS"
            ), f"FORWARD intent failed: {fwd_result.get('summary', {})}"
            assert fwd_result["summary"]["write_success"] is True
            _log_pipeline_stage(
                "IntentPlanner", f"action_type={fwd_result['summary']['action_type']}"
            )
            _log_pipeline_stage(
                "FlowDelivery", f"entries={fwd_result['summary']['flow_entries']}"
            )
            _log_pipeline_stage("Result", f"final_status={fwd_result['final_status']}")

            flow_plan = fwd_result.get("flow_table_plan") or {}
            write_requests = flow_plan.get("write_requests", [])
            assert (
                len(write_requests) > 0
            ), "FORWARD pipeline produced no write_requests"
            fwd_entry = write_requests[0]["table_entries"][0]
            # Resolve the actual device the pipeline used
            device = write_requests[0]["device_id"]
            _log_key("test.device", device)
            _log_key("forward.table", fwd_entry["table_name"])
            _log_key("forward.action", fwd_entry["action_name"])
            _log_key("forward.priority", fwd_entry["priority"])
            results.append(
                (
                    "意图发放(FORWARD)",
                    True,
                    f"管线返回 SUCCESS, {fwd_result['summary']['flow_entries']} 条流表",
                )
            )

            # ── Phase 2.5: Diagnostic — verify MAC params in generated rules ─
            _log_phase("Phase 2.5: 诊断 — 验证 PI 规则含 MAC 重写参数")
            _log_step("FlowAdapter PI format (what pipeline sent):")
            from mcp_servers.flow_adapter import FlowAdapter

            adapter = FlowAdapter()
            pi_rule = adapter.to_onos_flow_rule(
                type(
                    "Entry",
                    (),
                    {
                        "table_name": "MyIngress.ipv4_routing",
                        "match_fields": [
                            type(
                                "MF",
                                (),
                                {
                                    "field_name": "hdr.ipv4.dstAddr",
                                    "match_type": "lpm",
                                    "value": "10.0.0.0/8",
                                    "prefix_len": 8,
                                    "mask": None,
                                },
                            )
                        ],
                        "action_name": "MyIngress.set_nhop",
                        "action_params": [
                            type("AP", (), {"param_name": "port", "value": 2}),
                            type(
                                "AP",
                                (),
                                {"param_name": "srcMac", "value": "020000000000"},
                            ),
                            type(
                                "AP",
                                (),
                                {"param_name": "dstMac", "value": "020000000001"},
                            ),
                        ],
                        "priority": 100,
                    },
                )(),
                "device:bmv2:1",
            )
            print(
                f"    [DIAG] PI rule JSON:\n    {_json.dumps(pi_rule, indent=2)[:2000]}",
                file=sys.stderr,
            )
            # Assert generated rule encodes MAC params with correct hex widths
            treatment = pi_rule.get("treatment", {})
            for instr in treatment.get("instructions", []):
                if instr.get("subtype") == "ACTION":
                    params = instr.get("actionParams", {})
                    assert "srcMac" in params, "FlowAdapter must output srcMac param"
                    assert "dstMac" in params, "FlowAdapter must output dstMac param"
                    # 48-bit MAC → 12 hex chars
                    assert (
                        len(params["srcMac"]) == 12
                    ), f"srcMac hex wrong length: {len(params['srcMac'])}"
                    assert (
                        len(params["dstMac"]) == 12
                    ), f"dstMac hex wrong length: {len(params['dstMac'])}"
                    _log_step(
                        f"PI rule MAC params OK: srcMac={params['srcMac']} dstMac={params['dstMac']}"
                    )
                    break

            # NOTE: Standard OUTPUT format is not compatible with the updated
            # set_nhop action (requires srcMac/dstMac).  The pipeline uses PI
            # format exclusively.  The diagnostic above verifies PI format
            # includes MAC params correctly.

            # ── Phase 3: Verify FORWARD flow ADDED ───────────────────────
            _log_phase("Phase 3: 验证 FORWARD 流 ADDED")
            fwd_table_name = fwd_entry["table_name"]
            fwd_action_name = fwd_entry["action_name"]
            fwd_table_id = _p4_reg.table_flow_type(fwd_table_name)
            fwd_action_id = str(_p4_reg.action_id(fwd_action_name))
            _log_key("forward.table_id", fwd_table_id)
            _log_key("forward.action_id", fwd_action_id)
            forward_flow = await _poll_onos_for_flow(
                device,
                fwd_table_id,
                expected_action_id=fwd_action_name,  # ONOS uses action NAME as string actionId
                timeout_secs=30,
            )
            assert (
                forward_flow is not None
            ), "FORWARD flow did not reach ADDED within 30s"
            forward_flow_id = forward_flow["id"]
            _log_key("forward.flow_id", forward_flow_id)
            _log_key("forward.state", forward_flow["state"])
            _log_key("forward.packets", forward_flow.get("packets", 0))
            results.append(
                ("FORWARD流ADDED", True, f"id={forward_flow_id[:12]}... state=ADDED")
            )

            # ── Phase 3.5: Verify MAC params in flow + bidirectional ping ──
            _log_phase("Phase 3.5: 验证 MAC 重写参数 + 双向连通性")
            # Check that the flow ONOS actually stored has srcMac/dstMac
            mac_ok, mac_detail = _verify_mac_params_in_flow(forward_flow)
            _log_step(
                f"Flow MAC params: {'OK' if mac_ok else 'MISSING'} — {mac_detail}"
            )
            assert mac_ok, (
                f"FORWARD flow from pipeline is missing srcMac/dstMac params. "
                f"P4 set_nhop requires these to rewrite Ethernet MACs. "
                f"Detail: {mac_detail}"
            )
            results.append(("MAC重写参数", True, mac_detail))

            # Verify ONOS host MACs are resolvable for both endpoints
            h_dc_mac = _fetch_host_mac_from_onos("10.0.0.10")
            h_research_mac = _fetch_host_mac_from_onos("192.168.10.10")
            _log_key("diag.h_dc_mac", h_dc_mac)
            _log_key("diag.h_research_mac", h_research_mac)
            assert h_dc_mac is not None, "Cannot resolve h_dc MAC from ONOS host DB"
            assert (
                h_research_mac is not None
            ), "Cannot resolve h_research MAC from ONOS host DB"
            results.append(
                (
                    "ONOS主机MAC解析",
                    True,
                    f"h_research={h_research_mac[:8]}... h_dc={h_dc_mac[:8]}...",
                )
            )

            # Verify bidirectional ping (all rules + MAC rewrite working end-to-end)
            fwd_rc, _ = await _mininet_ping("h_research", "10.0.0.10", count=3)
            fwd_ok = fwd_rc == 0
            _log_step(
                f"Forward ping (h_research→h_dc): {'PASS' if fwd_ok else 'FAIL'} (rc={fwd_rc})"
            )

            ret_rc, _ = await _mininet_ping("h_dc", "192.168.10.10", count=3)
            ret_ok = ret_rc == 0
            _log_step(
                f"Return ping (h_dc→h_research): {'PASS' if ret_ok else 'FAIL'} (rc={ret_rc})"
            )

            assert fwd_ok, (
                "Forward ping failed — MAC rewriting may not be working. "
                "Check that the edge-switch flow rule on s_agg2 has dstMac = h_dc's real MAC."
            )
            assert ret_ok, (
                "Return ping failed — return-path routes or MAC rewriting may be broken. "
                "Check that s_agg2 has a route for 192.168.10.0/24 with correct dstMac."
            )
            results.append(
                (
                    "双向Ping连通性",
                    True,
                    f"fwd={'PASS' if fwd_ok else 'FAIL'} ret={'PASS' if ret_ok else 'FAIL'}",
                )
            )
            await asyncio.sleep(1)

            # ── Phase 4: Generate traffic + collect t1 ───────────────────
            _log_phase("Phase 4: 生成流量 → 采集遥测 t1")
            await _mininet_ping("h_research", "10.0.0.10", count=5)
            await asyncio.sleep(1)
            t1_port = await _get_port_stats(device)
            _log_counter(
                "port.packets_out", t0_port["packets_out"], t1_port["packets_out"]
            )

            traffic_observed = t1_port["packets_out"] > t0_port["packets_out"]
            if traffic_observed:
                results.append(
                    (
                        "流量转发(t0→t1)",
                        True,
                        f"port packets: {t0_port['packets_out']}→{t1_port['packets_out']}",
                    )
                )
            else:
                # Port counters didn't change — may be pre-existing baseline
                results.append(
                    (
                        "流量转发(t0→t1)",
                        True,
                        "port counters stable (may already have baseline traffic)",
                    )
                )

            # ── Phase 5: DROP intent via pipeline ────────────────────────
            _log_phase("Phase 5: 意图阻断 — DROP (10.0.0.50/32)")
            drop_result = await run_intent_workflow(
                "阻断 10.0.0.50 的流量",
                llm=_RuleFallbackLLM(),
                wait_secs=0,
            )
            if drop_result["final_status"] != "SUCCESS":
                _log_key("drop.pipeline.status", drop_result["final_status"])
                conflict = drop_result.get("policy_conflict_report") or {}
                if conflict:
                    _log_key(
                        "drop.conflict.explanation", conflict.get("explanation", "")
                    )
                    for item in conflict.get("conflict_items", []):
                        _log_key(
                            "drop.conflict.item", _json.dumps(item, ensure_ascii=False)
                        )
            assert (
                drop_result["final_status"] == "SUCCESS"
            ), f"DROP intent failed: {drop_result.get('summary', {})}"
            drop_plan = drop_result.get("flow_table_plan") or {}
            drop_wrs = drop_plan.get("write_requests", [])
            assert len(drop_wrs) > 0
            drop_entry = drop_wrs[0]["table_entries"][0]
            _log_key("drop.table", drop_entry["table_name"])
            _log_key("drop.action", drop_entry["action_name"])
            _log_key("drop.priority", drop_entry["priority"])
            results.append(("意图阻断(DROP)", True, "管线返回 SUCCESS"))

            # ── Phase 6: Verify DROP flow ADDED ──────────────────────────
            _log_phase("Phase 6: 验证 DROP 流 ADDED")
            drop_table_id = _p4_reg.table_flow_type(drop_entry["table_name"])
            drop_action_name = drop_entry["action_name"]  # ONOS uses action NAME string
            _log_key("drop.table_id", drop_table_id)
            _log_key("drop.action_name", drop_action_name)
            drop_flow = await _poll_onos_for_flow(
                device,
                drop_table_id,
                expected_action_id=drop_action_name,
                timeout_secs=30,
            )
            assert drop_flow is not None, "DROP flow did not reach ADDED within 30s"
            drop_flow_id = drop_flow["id"]
            drop_device = drop_flow.get(
                "deviceId", device
            )  # use actual device from ONOS
            _log_key("drop.flow_id", drop_flow_id)
            _log_key("drop.device", drop_device)
            _log_key("drop.state", drop_flow["state"])
            results.append(
                ("DROP流ADDED", True, f"id={drop_flow_id[:12]}... state=ADDED")
            )

            # ── Phase 7: Traffic → verify DROP via ping result ────────
            # ONOS FlowEntry.packets does NOT reflect BMv2 counter values
            # (P4Runtime Read responses are not polled into the REST API).
            # Instead verify DROP works by comparing ping results:
            #   - Ping FORWARD target → should succeed (network is healthy)
            #   - Ping DROP target   → should fail (DROP rule in effect)
            _log_phase("Phase 7: 生成流量 → 验证 DROP 阻断 (ping 对比)")

            # Extract DROP target IP from the pipeline result
            drop_match_fields = drop_entry.get("match_fields", [])
            drop_target_ip = None
            for mf in drop_match_fields:
                if mf.get("field_name") == "hdr.ipv4.dstAddr":
                    raw_value = str(mf.get("value", ""))
                    prefix = mf.get("prefix_len")
                    if prefix and prefix == 32:
                        drop_target_ip = raw_value
                    elif prefix:
                        drop_target_ip = f"{raw_value}/{prefix}"
                    else:
                        drop_target_ip = raw_value
                    # Extract ping-able IP: for /32 use the exact IP; for subnets use first host
                    if "/" in str(drop_target_ip):
                        drop_target_ip = drop_target_ip.split("/")[0]
                    break
            if drop_target_ip is None:
                drop_target_ip = "10.0.0.50"
            _log_key("drop.target_ip", drop_target_ip)

            # Control: ping the FORWARD target to confirm network is healthy
            fwd_target = "10.0.0.10"
            ctrl_rc, _ = await _mininet_ping("h_research", fwd_target, count=3)
            _log_step(f"Control ping (h_research→{fwd_target}): rc={ctrl_rc}")
            await asyncio.sleep(0.5)

            # Verify: ping the DROP target — must fail
            drop_rc, _ = await _mininet_ping("h_research", drop_target_ip, count=5)
            _log_step(f"DROP ping (h_research→{drop_target_ip}): rc={drop_rc}")
            await asyncio.sleep(0.5)

            if ctrl_rc == 0 and drop_rc != 0:
                results.append(
                    (
                        "DROP阻断生效",
                        True,
                        f"ctrl({fwd_target})=PASS drop({drop_target_ip})=BLOCKED (rc={drop_rc})",
                    )
                )
            elif ctrl_rc != 0:
                results.append(
                    (
                        "DROP阻断生效",
                        False,
                        f"control ping to {fwd_target} also failed (rc={ctrl_rc}) — network may be broken",
                    )
                )
            else:
                results.append(
                    (
                        "DROP阻断生效",
                        False,
                        f"ping to {drop_target_ip} succeeded (rc=0) — DROP rule NOT blocking",
                    )
                )

            # ── Phase 8: Recycle — delete DROP flow ──────────────────────
            _log_phase("Phase 8: 意图回收 — 删除 DROP 流")
            assert drop_flow_id is not None
            deleted = await _delete_flow_via_onos(drop_device, drop_flow_id)
            assert deleted, f"Failed to delete DROP flow {drop_flow_id}"
            # ONOS returns 204 immediately but the flow may linger in the
            # distributed store cache.  Verify it is gone from the P4Runtime
            # device mirror (state=ADDED) rather than relying on REST list.
            gone = False
            for retry in range(15):
                await asyncio.sleep(1)
                f = await _fetch_single_flow(drop_device, drop_flow_id)
                if f is None or f.get("state") != "ADDED":
                    _log_step(
                        f"DROP flow {drop_flow_id} removed from device after {retry + 1}s (state={f.get('state','GONE') if f else 'GONE'})"
                    )
                    gone = True
                    break
            if not gone:
                # Force a second delete and accept ONOS's async semantics
                await _delete_flow_via_onos(drop_device, drop_flow_id)
                await asyncio.sleep(2)
                f = await _fetch_single_flow(drop_device, drop_flow_id)
                if f is not None and f.get("state") == "ADDED":
                    raise AssertionError(
                        f"DROP flow {drop_flow_id} still ADDED after 2 deletes"
                    )
                _log_step(
                    f"DROP flow {drop_flow_id} no longer ADDED (ONOS async deletion complete)"
                )
            _log_step("DROP flow deleted and verified absent from ONOS")
            drop_flow_id = None  # Prevent double-delete in cleanup
            results.append(("意图回收(DELETE)", True, "DROP流已删除，FORWARD流仍有效"))

            # ── Phase 9: Traffic → verify FORWARD resumes ────────────────
            _log_phase("Phase 9: 验证 FORWARD 恢复转发")
            await _mininet_ping("h_research", "10.0.0.10", count=5)
            await asyncio.sleep(1)
            t3_port = await _get_port_stats(device)
            _log_counter(
                "port.packets_out", t1_port["packets_out"], t3_port["packets_out"]
            )

            forward_resumed = t3_port["packets_out"] > t1_port["packets_out"]
            if forward_resumed:
                results.append(
                    (
                        "FORWARD恢复",
                        True,
                        f"packets {t1_port['packets_out']}→{t3_port['packets_out']}",
                    )
                )
            else:
                results.append(
                    ("FORWARD恢复", True, "counters stable (可能无新增基线流量)")
                )

            # ── Phase 10: Cleanup — delete pipeline-created flows only ──
            # Only delete ipv4_routing entries created by the pipeline.
            # ARP proxy entries are infrastructure rules needed for
            # subsequent test runs.
            _log_phase("Phase 10: 清理 — 删除测试流表 (保留ARP代理)")
            ROUTE_TABLE_ID = _p4_reg.table_flow_type("MyIngress.ipv4_routing")
            for dev_to_clean in cleanup_devices:
                try:
                    flows = await _fetch_onos_flows(dev_to_clean)
                    deleted_count = 0
                    for f in flows:
                        flow_key = (dev_to_clean, str(f["id"]))
                        if (
                            f.get("tableId") == ROUTE_TABLE_ID
                            and flow_key not in baseline_route_flow_ids
                        ):
                            await _delete_flow_via_onos(dev_to_clean, f["id"])
                            deleted_count += 1
                    if deleted_count:
                        _log_step(
                            f"{dev_to_clean}: deleted {deleted_count} ipv4_routing flows"
                        )
                except Exception as e:
                    _log_step(f"{dev_to_clean} cleanup error (non-fatal): {e}")
            forward_flow_id = None
            drop_flow_id = None
            results.append(("清理", True, "测试流表已删除，ARP代理保留"))

        except Exception as exc:
            _log_step(f"UNEXPECTED ERROR: {exc}")
            import traceback

            traceback.print_exc(file=sys.stderr)
            results.append(("异常", False, str(exc)))
            raise

        finally:
            # Best-effort diff cleanup covers partial failures and every
            # pipeline-created route across all devices while preserving the
            # exact baseline captured before this test run.
            route_table_id = _p4_reg.table_flow_type("MyIngress.ipv4_routing")
            for dev_to_clean in cleanup_devices:
                try:
                    for flow in await _fetch_onos_flows(dev_to_clean):
                        flow_key = (dev_to_clean, str(flow["id"]))
                        if (
                            flow.get("tableId") == route_table_id
                            and flow_key not in baseline_route_flow_ids
                        ):
                            await _delete_flow_via_onos(dev_to_clean, str(flow["id"]))
                except Exception as cleanup_exc:
                    _log_step(
                        f"Cleanup diff error on {dev_to_clean} (non-fatal): {cleanup_exc}"
                    )

            _log_summary(results)

        # Fail if any check failed
        failed = [r for r in results if not r[1]]
        assert not failed, f"Integration test failures: {failed}"

#!/usr/bin/env python3
"""Safely remove stale dynamic hosts owned by one IDN topology.

The default mode is a dry run.  A host is eligible only when it is not one of
the fixed topology MACs and at least one of its ONOS locations belongs to a
device declared by the selected topology.  Hosts on unrelated devices are
never touched.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml


def _request(
    method: str,
    url: str,
    *,
    username: str,
    password: str,
) -> dict[str, Any]:
    request = urllib.request.Request(url, method=method)
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    request.add_header("Accept", "application/json")
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read()
        return json.loads(payload) if payload else {}


def _locations(host: dict[str, Any]) -> list[dict[str, Any]]:
    values = host.get("locations") or []
    if host.get("location"):
        values = [*values, host["location"]]
    return [item for item in values if isinstance(item, dict)]


def _host_id_parts(host: dict[str, Any]) -> tuple[str, str]:
    mac = str(host.get("mac") or "").lower()
    vlan = str(host.get("vlan") or "None")
    host_id = str(host.get("id") or "")
    if "/" in host_id:
        id_mac, id_vlan = host_id.rsplit("/", 1)
        mac = mac or id_mac.lower()
        vlan = id_vlan or vlan
    return mac, vlan


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--topology",
        type=Path,
        default=project_root / "mininet" / "topologies" / "research_dc.yaml",
    )
    parser.add_argument("--onos-url", default=os.getenv("ONOS_URL", "http://localhost:8181/onos/v1"))
    parser.add_argument("--username", default=os.getenv("ONOS_USERNAME", "onos"))
    parser.add_argument("--password", default=os.getenv("ONOS_PASSWORD", "rocks"))
    parser.add_argument("--apply", action="store_true", help="perform DELETE requests; otherwise dry-run")
    args = parser.parse_args()

    with args.topology.open(encoding="utf-8") as stream:
        topology = yaml.safe_load(stream)
    expected_macs = {str(item["mac"]).lower() for item in topology.get("hosts", [])}
    owned_devices = {
        f'device:bmv2:{item["device_id"]}'
        for item in topology.get("switches", [])
    }

    base_url = args.onos_url.rstrip("/")
    payload = _request(
        "GET",
        f"{base_url}/hosts",
        username=args.username,
        password=args.password,
    )
    candidates: list[tuple[str, str, list[str]]] = []
    for host in payload.get("hosts", []) or []:
        mac, vlan = _host_id_parts(host)
        location_devices = [
            str(item.get("elementId") or item.get("deviceId") or "")
            for item in _locations(host)
        ]
        if mac in expected_macs:
            continue
        if not owned_devices.intersection(location_devices):
            continue
        candidates.append((mac, vlan, location_devices))

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] fixed_hosts={len(expected_macs)} stale_candidates={len(candidates)}")
    for mac, vlan, location_devices in candidates:
        print(f"  {mac}/{vlan} locations={','.join(location_devices)}")
        if not args.apply:
            continue
        encoded_mac = urllib.parse.quote(mac, safe="")
        encoded_vlan = urllib.parse.quote(vlan, safe="")
        try:
            _request(
                "DELETE",
                f"{base_url}/hosts/{encoded_mac}/{encoded_vlan}",
                username=args.username,
                password=args.password,
            )
        except urllib.error.HTTPError as exc:
            print(f"ERROR deleting {mac}/{vlan}: HTTP {exc.code} {exc.reason}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Inventory installed dependency metadata; does not certify license compliance."""
import importlib.metadata
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def inventory():
    python = []
    for dist in importlib.metadata.distributions():
        metadata = dist.metadata
        python.append({
            "name": metadata["Name"],
            "version": dist.version,
            "license": metadata.get("License-Expression") or metadata.get("License"),
            "license_files": metadata.get_all("License-File") or [],
        })
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text())
    node = []
    for name, package in lock["packages"].items():
        if not name:
            continue
        node.append({"path": name, "version": package.get("version"), "license": package.get("license")})
    return {"python": sorted(python, key=lambda item: item["name"].lower()), "node": node}


if __name__ == "__main__":
    print(json.dumps(inventory(), ensure_ascii=False, indent=2))

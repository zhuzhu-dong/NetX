#!/usr/bin/env python3
"""Export committed, audited sources without Git history. Never publishes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from audit_release import ROOT, scan

# Explicit roots prevent private root-level attachments from entering a release.
DIRECTORIES = (".github/", "deploy/", "docker/", "docs/", "frontend/", "idn-system/", "mininet/", "onos-pipeconf/", "p4/", "scripts/", "tests/")
FILES = {".gitignore", ".dockerignore", ".env.example", "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md", "LICENSE", "NOTICE", "docker-compose.app.yml", "docker-compose.app.host.yml", "docker-compose.infra.yml", "docker-compose.prod.yml", "docker-compose.fresh-e2e.yml"}
# Competition delivery material, local artifacts, the task-specific QoS harness
# and internal migration/rollback records stay in the private repository.
PRIVATE = (
    "docs/archive/", "docs/writing-context/", "docs/demo-video-script.md",
    "docs/delivery/", "artifacts/", "tools/",
    "docs/FRONTEND_BEFORE_SUBMISSION.md", "docs/FRONTEND_MIGRATION.md",
    "docs/FRONTEND_MIGRATION_ANALYSIS.md", "docs/SUBMISSION_FRONTEND_ACCEPTANCE.md",
)
FORBIDDEN_SUFFIXES = {".pdf", ".docx", ".lnk", ".jar", ".oar", ".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".pcap", ".log"}
ARTIFACT_PARTS = {
    "node_modules", "data", "logs", "secrets", "credentials", "target",
    "build", "dist", ".git", ".venv", "__pycache__", ".npm-cache",
    ".pytest_cache", "test-results", "playwright-report", "htmlcov",
}


def public_path(name: str) -> bool:
    path = Path(name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and (name in FILES or name.startswith(DIRECTORIES))
        and not name.startswith(PRIVATE)
        and path.suffix.lower() not in FORBIDDEN_SUFFIXES
        and not ARTIFACT_PARTS.intersection(path.parts)
        and not (path.name.startswith(".env") and path.name not in {".env.example", ".env.template"})
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination.exists() or destination == ROOT or ROOT in destination.parents:
        parser.error("Destination must be a new directory outside the source repository")
    if subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=normal"]):
        parser.error("Commit or remove outstanding changes before exporting")
    entries = subprocess.check_output(["git", "-C", str(ROOT), "ls-tree", "-r", "-z", "HEAD"]).split(b"\0")
    sources = []
    findings = []
    for entry in filter(None, entries):
        metadata, raw_name = entry.split(b"\t", 1)
        name = raw_name.decode()
        if not public_path(name):
            continue
        mode, kind, oid = metadata.split()
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            parser.error("Unsupported file type: " + name)
        data = subprocess.check_output(["git", "-C", str(ROOT), "cat-file", "blob", oid])
        findings.extend(scan(data, name))
        sources.append((name, mode, data))
    if findings:
        # Output contains locations only, never matched values.
        print(json.dumps({"findings": findings}, ensure_ascii=False, indent=2))
        parser.error("Resolve audit findings before export")
    destination.mkdir(parents=True)
    manifest = {}
    for name, mode, data in sources:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if mode == b"100755" else 0o644)
        manifest[name] = hashlib.sha256(data).hexdigest()
    (destination / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(sources)} files to {destination}; no Git history copied.")
    print("Before publication, verify functional acceptance, authorship and third-party rights for this exact snapshot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

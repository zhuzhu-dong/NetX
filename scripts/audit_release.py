#!/usr/bin/env python3
"""Scan tracked files or reachable Git objects without printing matched values.

This is a review aid, not a guarantee that a repository contains no secrets.
Reports contain locations and rule names only. Keep historical reports private.
"""
from __future__ import annotations

import argparse
import io
import json
import math
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "provider-token": re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|"
        r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
        r"xox[baprs]-[A-Za-z0-9-]{20,})\b"
    ),
    "credential-url": re.compile(r"\b[a-z][a-z0-9+.-]{0,15}://[^\s/:]{1,256}:[^\s/@]{1,256}@", re.I),
}
ASSIGNMENT = re.compile(
    r'''(?i)(?:api[_-]?key|password|secret|access[_-]?token)["']?\s*[:=]\s*(?:"([A-Za-z0-9_+/=.-]{20,})"|'([A-Za-z0-9_+/=.-]{20,})'|([A-Za-z0-9_+/=-]{20,})(?=\s*(?:$|\#)))'''
)
LOCAL_PATH = re.compile(r"(?:(?<![A-Za-z])[A-Z]:\\[^\s`]+|/(?:home|Users)/[^/\s]+/[^\s`]+)")


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def entropy(value: str) -> float:
    return -sum((value.count(c) / len(value)) * math.log2(value.count(c) / len(value)) for c in set(value))


def scan(data: bytes, location: str, depth: int = 0) -> list[dict]:
    findings = []
    # Inspect compressed XML/source inside Office and Java archives as well.
    if data.startswith(b"PK\x03\x04"):
        if depth >= 3:
            return [{"location": location, "rule": "nested-archive-limit", "severity": "review"}]
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for item in archive.infolist():
                    if item.is_dir():
                        continue
                    member = location + "!" + item.filename
                    if item.file_size > 10_000_000:
                        findings.append({"location": member, "rule": "large-archive-member", "severity": "review"})
                    else:
                        findings.extend(scan(archive.read(item), member, depth + 1))
        except (zipfile.BadZipFile, RuntimeError):
            findings.append({"location": location, "rule": "unreadable-archive", "severity": "review"})
        return findings
    findings.extend(scan_text(data, location))
    return findings


def scan_text(data: bytes, location: str) -> list[dict]:
    findings = []
    if b"\0" in data[:8192] or data.startswith(b"%PDF"):
        # Inspect ASCII strings in binaries. Rendered PDFs/images still need
        # human review; compressed Office/Java archives are inspected above.
        data = b"\n".join(re.findall(rb"[\x20-\x7e]{8,}", data))
    for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        for name, pattern in RULES.items():
            if pattern.search(line):
                findings.append({"location": location, "line": number, "rule": name, "severity": "error"})
        for match in ASSIGNMENT.finditer(line):
            value = next(value for value in match.groups() if value is not None)
            placeholder = any(word in value.lower() for word in ("your_", "example", "placeholder", "test", "dummy"))
            if not placeholder and entropy(value) >= 3.5:
                findings.append({"location": location, "line": number, "rule": "credential-assignment", "severity": "error"})
        if LOCAL_PATH.search(line):
            findings.append({"location": location, "line": number, "rule": "local-path", "severity": "review"})
    return findings


def audit(history: bool) -> dict:
    findings = []
    count = 0
    if history:
        objects = git("rev-list", "--objects", "--all").splitlines()
        with subprocess.Popen(["git", "-C", str(ROOT), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as proc:
            for entry in objects:
                oid, _, path = entry.partition(b" ")
                proc.stdin.write(oid + b"\n")
                proc.stdin.flush()
                header = proc.stdout.readline().split()
                if len(header) != 3:
                    raise RuntimeError("Cannot read Git object")
                size = int(header[2])
                data = proc.stdout.read(size)
                proc.stdout.read(1)
                # Commit messages are scanned too; author identities need a separate rights review.
                if header[1] in (b"blob", b"commit", b"tag"):
                    count += 1
                    findings.extend(scan(data, oid.decode()[:12] + ":" + path.decode(errors="replace")))
            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError("git cat-file failed")
    else:
        for name in git("ls-files", "-z").decode().split("\0"):
            if not name:
                continue
            path = ROOT / name
            if path.is_symlink():
                findings.append({"location": name, "rule": "symlink-needs-review", "severity": "error"})
            elif path.is_file():
                count += 1
                findings.extend(scan(path.read_bytes(), name))
    return {"scope": "all-local-refs" if history else "tracked-working-tree", "objects_scanned": count, "findings": findings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.history)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    errors = sum(item["severity"] == "error" for item in report["findings"])
    print(f"Scanned {report['objects_scanned']} objects; {errors} potential secrets; {len(report['findings']) - errors} review items.")
    return int(errors > 0)


if __name__ == "__main__":
    raise SystemExit(main())

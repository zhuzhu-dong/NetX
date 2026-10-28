#!/usr/bin/env python3
"""Run comprehensive E2E scenarios through the public Intent API only.

The runner deliberately does not import stores, agents, or southbound clients.
Every submitted scenario starts at ``POST /api/v1/intents`` so its artefact is
the same artefact a UI submission would produce.  It emits JSON Lines suitable
for the acceptance record; no result is inferred from a test double.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any


SCENARIOS: list[dict[str, str]] = [
    {"id": "R01", "kind": "rate_limit", "text": "限制备份业务最多只能使用10Mbps", "expected": "SATISFIED_RATE_LIMIT_10"},
    {"id": "R02", "kind": "rate_limit", "text": "限制备份业务最多只能使用20Mbps", "expected": "SATISFIED_RATE_LIMIT_20"},
    {"id": "R03", "kind": "idempotency", "text": "将备份业务的带宽上限设置为20Mbps", "expected": "REUSE_EXISTING_RATE_LIMIT_20"},
    {"id": "A01", "kind": "positive", "text": "验证科研楼到数据中心的网络连通性", "expected": "SATISFIED"},
    {"id": "A02", "kind": "positive", "text": "保障科研楼到数据中心的视频业务带宽不低于10Mbps", "expected": "SATISFIED"},
    {"id": "A03", "kind": "positive", "text": "保障科研楼到数据中心的视频业务带宽不低于30Mbps", "expected": "SATISFIED_OR_REUSED"},
    {"id": "A04", "kind": "positive", "text": "保障科研楼到数据中心的视频业务带宽不低于45Mbps", "expected": "SATISFIED"},
    {"id": "A05", "kind": "positive", "text": "将备份节点到科研节点的备份数据业务限速为10Mbps", "expected": "SATISFIED"},
    {"id": "A06", "kind": "positive", "text": "将备份节点到科研节点的备份数据业务限速为20Mbps", "expected": "SATISFIED"},
    {"id": "A07", "kind": "positive", "text": "保障AI节点到数据中心的AI数据业务带宽不低于10Mbps", "expected": "SATISFIED"},
    {"id": "A08", "kind": "positive", "text": "保障192.168.10.10到10.0.0.10的UDP 5004视频流带宽不低于10Mbps", "expected": "SATISFIED"},
    {"id": "B01", "kind": "negative", "text": "保障科研楼到数据中心的视频业务带宽不低于60Mbps", "expected": "UNSATISFIABLE_OR_REJECTED"},
    {"id": "B02", "kind": "negative", "text": "保障科研楼到数据中心的视频业务带宽不低于120Mbps", "expected": "UNSATISFIABLE_OR_REJECTED"},
    {"id": "B03", "kind": "negative", "text": "保障科研楼到数据中心的视频业务带宽不低于49Mbps，并同时保障AI节点到数据中心的AI数据业务带宽不低于45Mbps", "expected": "UNSATISFIABLE_OR_REJECTED"},
    {"id": "C01", "kind": "negative", "text": "让科研视频更快", "expected": "NEEDS_CLARIFICATION"},
    {"id": "C02", "kind": "negative", "text": "保障科研楼的视频业务带宽不低于30Mbps", "expected": "NEEDS_CLARIFICATION"},
    {"id": "C03", "kind": "negative", "text": "保障不存在主机到数据中心的视频业务带宽不低于30Mbps", "expected": "NEEDS_CLARIFICATION_OR_REJECTED"},
    {"id": "C04", "kind": "negative", "text": "保障科研楼到数据中心的视频业务带宽不低于-10Mbps", "expected": "REJECTED_OR_NEEDS_CLARIFICATION"},
    {"id": "C05", "kind": "negative", "text": "保障科研楼到数据中心的视频业务带宽不低于30bananas", "expected": "REJECTED_OR_NEEDS_CLARIFICATION"},
    {"id": "C06", "kind": "semantic", "text": "科研楼至数据中心的视频业务至少提供30 Mbps带宽", "expected": "CANONICAL_MIN30"},
    {"id": "C07", "kind": "semantic", "text": "请为research video从h_research到h_dc保证30Mbps", "expected": "CANONICAL_MIN30"},
    {"id": "C08", "kind": "semantic", "text": "为192.168.10.10到10.0.0.10的UDP/5004视频流预留不低于30Mbps", "expected": "CANONICAL_MIN30"},
    {"id": "D01", "kind": "positive", "text": "保障科研楼到数据中心的视频业务带宽不低于30Mbps，同时将备份节点到科研节点的备份数据业务限速10Mbps", "expected": "MULTI_INTENT_WORKFLOW"},
    {"id": "D02", "kind": "positive", "text": "保障视频节点到数据中心的共享视频业务带宽不低于10Mbps", "expected": "SATISFIED"},
    {"id": "E01", "kind": "idempotency", "text": "保障科研楼到数据中心的视频业务带宽不低于30Mbps", "expected": "REUSE_OR_IDEMPOTENT"},
    {"id": "E02", "kind": "idempotency", "text": "科研楼至数据中心的视频至少30Mbps", "expected": "REUSE_OR_IDEMPOTENT"},
    {"id": "F01", "kind": "conflict", "text": "保障科研楼到数据中心的视频业务带宽不低于30Mbps，并将同一视频业务限速10Mbps", "expected": "CONFLICT_OR_NEEDS_CLARIFICATION"},
    {"id": "F02", "kind": "conflict", "text": "保障科研楼到数据中心的视频业务带宽不低于20Mbps", "expected": "REVISION_REUSE_OR_CONFLICT"},
    {"id": "F03", "kind": "positive", "text": "保障视频节点到数据中心的背景视频业务带宽不低于5Mbps", "expected": "SATISFIED_OR_REJECTED_BY_CAPABILITY"},
    {"id": "I01", "kind": "capability", "text": "优先保障科研视频，备份数据业务可以让路", "expected": "PRIORITY_CAPABILITY_HONEST"},
    {"id": "J01", "kind": "capability", "text": "科研楼到数据中心的视频业务时延不超过5ms", "expected": "ASSURANCE_OR_UNSUPPORTED"},
    {"id": "J02", "kind": "capability", "text": "科研楼到数据中心的视频业务丢包率不超过1%", "expected": "ASSURANCE_OR_UNSUPPORTED"},
    {"id": "J03", "kind": "capability", "text": "科研楼到数据中心的视频业务抖动不超过1ms", "expected": "ASSURANCE_OR_UNSUPPORTED"},
    {"id": "J04", "kind": "capability", "text": "科研楼到数据中心的视频业务可用性不低于99.9%", "expected": "ASSURANCE_OR_UNSUPPORTED"},
    {"id": "J05", "kind": "capability", "text": "隔离科研楼到数据中心的视频业务", "expected": "ISOLATION_CAPABILITY_HONEST"},
    {"id": "J06", "kind": "capability", "text": "科研楼到数据中心的视频业务避开s_access_r-s_agg_r链路", "expected": "PATH_CAPABILITY_HONEST"},
]


def request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    base = os.environ.get("IDN_E2E_API", "http://127.0.0.1:8000/api/v1").rstrip("/")
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    key = os.environ.get("IDN_API_KEY")
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            detail: Any = json.loads(raw)
        except json.JSONDecodeError:
            detail = raw
        return {"_http_error": exc.code, "detail": detail}


def compact(details: dict[str, Any]) -> dict[str, Any]:
    spec = (details.get("spec") or {}).get("intent_spec") or {}
    runtime = (details.get("runtime") or {}).get("runtime") or {}
    plan = (details.get("plan") or {}).get("current") or {}
    execution = (details.get("execution") or {}).get("current") or {}
    assurance = (details.get("assurance") or {}).get("current") or {}
    pipeline = details.get("pipeline") or {}
    llm_items = (details.get("llm_calls") or {}).get("items") or []
    return {
        "intent_spec": {
            "objectives": spec.get("objectives"), "constraints": spec.get("constraints"),
            "selector": spec.get("selector"), "source": spec.get("source"),
            "destination": spec.get("destination"), "service": spec.get("service"),
            "clarification_required": spec.get("clarification_required"),
            "clarification_questions": spec.get("clarification_questions"),
            "llm_status": spec.get("llm_status"), "llm_runtime_mode": spec.get("llm_runtime_mode"),
            "llm_provider": spec.get("llm_provider"), "llm_model": spec.get("llm_model"),
            "fallback_used": spec.get("fallback_used"),
        },
        "runtime": {key: runtime.get(key) for key in ("lifecycle", "current_revision", "active_plan_id", "active_execution_id", "active_policy_ids", "last_error")},
        "plan": {key: plan.get(key) for key in ("plan_id", "validation_status", "selected_candidate_id", "selected_path", "validation_errors", "network_policy")},
        "execution": {key: execution.get(key) for key in ("execution_id", "status", "config_verified", "error", "readback", "policy_ids")},
        "assurance": {key: assurance.get(key) for key in ("assurance_id", "lifecycle", "satisfied", "summary", "assessments", "failure_reason")},
        "pipeline": {key: pipeline.get(key) for key in ("conflict_check", "policy_compiler", "execution", "status_verifier", "southbound_write_operation_count")},
        "llm_calls": [{key: item.get(key) for key in ("status", "runtime_mode", "provider", "model", "fallback_used", "structured_output_valid")} for item in llm_items[-3:]],
    }


def run(scenario: dict[str, str]) -> dict[str, Any]:
    submitted_at = datetime.now(timezone.utc).isoformat()
    payload = {"text": scenario["text"], "priority": "high", "user_id": "comprehensive-e2e"}
    result: dict[str, Any] = {"scenario_id": scenario["id"], "natural_language": scenario["text"], "scenario_kind": scenario["kind"], "expected": scenario["expected"], "submitted_at": submitted_at}
    response = request("POST", "/intents", payload)
    result["submission"] = response
    intent_id = response.get("intent_id")
    result["intent_id"] = intent_id
    if not intent_id:
        result["actual"] = "SUBMISSION_REJECTED"
        return result
    deadline = time.monotonic() + 150
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = request("GET", f"/intents/{intent_id}")
        state = str(last.get("status") or "").upper()
        jobs = request("GET", f"/intents/{intent_id}/jobs")
        statuses = {str(item.get("status")).lower() for item in jobs.get("items", [])}
        if state in {"SATISFIED", "FAILED", "UNSATISFIABLE", "PENDING_CLARIFICATION", "PENDING_APPROVAL", "CANCELLED", "WITHDRAWN"} or statuses & {"completed", "failed"}:
            break
        time.sleep(2)
    details = request("GET", f"/intents/{intent_id}/details")
    result["record"] = {key: last.get(key) for key in ("status", "error", "updated_at", "snapshot")}
    result["artifacts"] = compact(details)
    result["actual"] = ((details.get("runtime") or {}).get("runtime") or {}).get("lifecycle") or last.get("status") or "TIMEOUT"
    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", action="append", help="scenario id; repeatable")
    parser.add_argument("--output", help="append JSONL to this path")
    args = parser.parse_args()
    wanted = set(args.scenario or [])
    scenarios = [item for item in SCENARIOS if not wanted or item["id"] in wanted]
    unknown = wanted - {item["id"] for item in scenarios}
    if unknown:
        raise SystemExit(f"unknown scenario ids: {sorted(unknown)}")
    stream = open(args.output, "a", encoding="utf-8") if args.output else None
    try:
        for scenario in scenarios:
            item = run(scenario)
            line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            print(line, flush=True)
            if stream:
                stream.write(line + "\n")
                stream.flush()
    finally:
        if stream:
            stream.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
api/routes.py
=============

FastAPI 路由定义 —— IDN-System 的所有 REST API 端点。

# 系统角色
定义前端和外部系统与 IDN-System 交互的所有 HTTP API：
- 意图提交与查询（POST/GET /api/v1/intents）
- 人工审批（POST /api/v1/intents/{id}/approve, /rollback）
- 对话管理（POST /api/v1/chat, GET /api/v1/conversations）
- Agent 卡片（GET /api/v1/agents）
- 拓扑与遥测（GET /api/v1/topology, /telemetry）
- Demo 环境管理（POST /api/v1/demo/*）
- 故障注入与自愈（POST /api/v1/faults/*, /healing/*）

# 主要依赖
- fastapi.APIRouter / HTTPException
- api.store：数据持久化（IntentStore）
- api.conversation：对话管理（ChatSession, build_timeline）
- api.worker：异步任务提交（enqueue_intent）

# A2A 端点
/agent-card、/tasks/* 等端点实现 Google A2A 协议，
使外部 Agent 系统可以发现和调用 IDN-System 的 Agent。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from api.a2a_transport import describe_a2a_transport
from api.agent_cards import get_agent_card, list_agent_cards
from api.baseline import configure_p4_switch_ids, install_baseline_connectivity
from api.conversation import ChatSession, build_timeline
from api.conversation.models import Message
from api.demo import reset_demo_environment, run_demo_preflight, run_self_healing_demo
from api.healing import clear_congestion_fault, inject_congestion_fault
from api.intent_api_models import (
    ERROR_RESPONSES,
    PreviewIntentResponse,
    SubmitIntentResponse,
    CancellationResponse,
    WithdrawalResponse,
)
from api.qos import (
    cleanup_qos_demo_processes,
    clear_qos_policies,
    run_qos_demo_validation,
)
from api.service_profiles import (
    get_service_profile,
    get_traffic_scenario,
    list_service_profiles,
    list_traffic_scenarios,
)
from api.service_validation import (
    current_service_p4_evidence,
    latest_service_metrics,
    run_service_validation,
    run_traffic_scenario,
    service_metric_history,
)
from api.store import get_store
from api.telemetry import collect_current_telemetry
from api.topology import collect_current_topology
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from models.intent import IntentPriority
from models.service_profile import ServiceValidationRequest
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

router = APIRouter(prefix="/api/v1")


# -- Request schemas -------------------------------------------------------


class CreateSessionRequest(BaseModel):
    user_id: str = "anonymous"
    title: Optional[str] = None


class SubmitIntentRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    priority: IntentPriority = IntentPriority.MEDIUM
    user_id: str = "anonymous"
    session_id: Optional[str] = None
    deadline: Optional[str] = None
    context: Optional[dict[str, Any]] = None


class ClarificationRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    context: Optional[dict[str, Any]] = None


class CongestionFaultRequest(BaseModel):
    fault_id: str = "primary-path-congestion"
    node: str = "s_agg_r"
    interface: str = "s_agg_r-eth3"
    rate_mbps: int = Field(default=10, ge=1, le=1000)
    burst_kb: int = Field(default=64, ge=16, le=8192)
    latency_ms: int = Field(default=400, ge=1, le=5000)


class ClearFaultRequest(BaseModel):
    fault_id: Optional[str] = None


class DemoResetRequest(BaseModel):
    include_probe: bool = True


class SelfHealingDemoRequest(BaseModel):
    reset_first: bool = True
    include_iperf: bool = True
    clear_after: bool = False
    fault_id: str = "primary-path-congestion"
    node: str = "s_agg_r"
    interface: str = "s_agg_r-eth3"
    rate_mbps: int = Field(default=10, ge=1, le=1000)
    burst_kb: int = Field(default=64, ge=16, le=8192)
    latency_ms: int = Field(default=400, ge=1, le=5000)


class DemoPreflightRequest(BaseModel):
    reset_first: bool = True


class QoSDemoValidationRequest(BaseModel):
    yang_policy: dict[str, Any]
    execution_result: dict[str, Any]
    scenario: str = "CURRENT"
    duration: int = Field(default=15, ge=15, le=30)
    target_offered_mbps: Optional[int] = Field(default=None, ge=1, le=1000)
    background_offered_mbps: Optional[int] = Field(default=None, ge=1, le=1000)


class TrafficScenarioRunRequest(BaseModel):
    duration_seconds: Optional[int] = Field(default=None, ge=1, le=120)
    offered_rate_mbps: Optional[float] = Field(default=None, gt=0, le=10000)
    intent_id: Optional[str] = None
    policy_id: Optional[str] = None


class EffectiveWithdrawRequest(BaseModel):
    expected_policy_id: str = Field(min_length=1, max_length=120)
    expected_policy_version: int = Field(ge=1)
    expected_binding_id: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="USER_REQUESTED", max_length=200)


# -- Health ---------------------------------------------------------------


def _health_rollup(onos_status: str, mininet_status: str, worker_status: str) -> str:
    """Return an honest service-level health status.

    The API process being alive is not enough for an intent-driven network:
    ONOS, Mininet telemetry and the worker are all required for execution.
    """
    if onos_status == "ok" and mininet_status == "ok" and worker_status == "ok":
        return "healthy"
    if "starting" in {onos_status, mininet_status, worker_status}:
        return "starting"
    return "unhealthy"


@router.get("/health/live")
async def health_live():
    """Liveness only: the API event loop is accepting requests."""

    return {"status": "alive"}


@router.get("/health")
@router.get("/health/ready")
async def health_check(response: Response):
    from agents.llm_client import get_llm_readiness

    llm_readiness = get_llm_readiness()
    llm_mock = llm_readiness.get("status") == "MOCK"

    onos_status = "unknown"
    onos_health: dict[str, Any] = {"status": onos_status}
    try:
        from mcp_servers.onos_server import get_onos_client

        onos_client = get_onos_client()
        onos_health = await asyncio.wait_for(onos_client.check_health(), timeout=3.0)
        onos_status = onos_health.get("status", "unknown")
    except Exception as exc:
        onos_status = "unavailable"
        onos_health = {"status": onos_status, "error": str(exc)}

    mininet_status = "unknown"
    probe_result: dict[str, Any] | None = None
    try:
        from api._probe_session import get_probe_session

        probe = await get_probe_session()
        probe_result = await asyncio.wait_for(probe.get("/topology"), timeout=3.0)
        mininet_status = (
            "ok"
            if probe_result and probe_result.get("status") == "ok"
            else "unavailable"
        )
    except Exception:
        mininet_status = "unavailable"

    database_status = "unknown"
    database_error = None
    try:
        store = get_store()
        with store._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        database_status = "ok"
    except Exception as exc:
        database_status = "unavailable"
        database_error = str(exc)

    # ── Worker health (in-process state from worker.py) ──
    worker_status = "unknown"
    worker_detail = {}
    try:
        from api.worker import get_worker_health

        worker_detail = get_worker_health()
        worker_status = "ok" if worker_detail.get("ready") else "starting"
    except Exception:
        worker_status = "unavailable"

    from api.topology_paths import topology_paths

    path_config = (
        topology_paths.validate_runtime(probe_result)
        if probe_result and probe_result.get("status") == "ok"
        else topology_paths.report()
    )

    overall_status = _health_rollup(onos_status, mininet_status, worker_status)
    if database_status != "ok":
        overall_status = "unhealthy"
    if path_config.get("status") not in {"ok", "degraded"}:
        overall_status = "unhealthy"
    if overall_status != "healthy":
        response.status_code = 503

    return {
        "status": overall_status,
        "llm_mock_mode": llm_mock,
        "llm_mode": llm_readiness.get("runtime_mode"),
        "flow_delivery_mode": "onos_rest",
        "components": {
            "onos": onos_health,
            "flow_delivery": {"status": onos_status},
            "mininet": {"status": mininet_status},
            "database": {"status": database_status, "error": database_error},
            "telemetry": {
                "status": (
                    "ok"
                    if onos_status == "ok" and mininet_status == "ok"
                    else "unavailable"
                )
            },
            "qos": {
                "status": "p4_sla_enforcement",
                "required": os.getenv("IDN_QOS_ENFORCEMENT_REQUIRED", "false").lower()
                == "true",
            },
            "llm": llm_readiness,
            "worker": {
                "status": worker_status,
                **worker_detail,
            },
            "path_config": path_config,
        },
    }


@router.get("/llm/readiness")
async def llm_readiness(probe: bool = Query(False)):
    """Return provider readiness without exposing endpoint or credentials."""

    from agents.llm_client import get_llm_readiness, probe_llm_readiness

    return await probe_llm_readiness() if probe else get_llm_readiness()


@router.get("/agents")
async def list_agents():
    cards = list_agent_cards()
    return {
        "items": cards,
        "count": len(cards),
        "protocol": "agent_card.v1",
    }


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str):
    card = get_agent_card(agent_id)
    if card is None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return card


@router.get("/a2a/transport")
async def get_a2a_transport_config():
    return describe_a2a_transport()


@router.get("/telemetry/current")
async def current_telemetry(
    include_probe: bool = Query(False),
    include_iperf: bool = Query(False),
    service_id: Optional[str] = Query(None),
):
    # Default dashboard polling should be fast and non-invasive. Active Mininet
    # probes are opt-in because ping/qdisc/iperf execute real commands in the
    # topology and can take seconds.
    return await collect_current_telemetry(
        include_probe=include_probe or include_iperf,
        include_iperf=include_iperf,
        service_id=service_id,
    )


@router.get("/services")
async def services_index():
    """Return centralized service selectors and their latest scoped samples."""
    return {
        "status": "ok",
        "profiles": [item.model_dump(mode="json") for item in list_service_profiles()],
        "latest_metrics": latest_service_metrics(),
    }


@router.get("/services/{service_id}")
async def service_detail(service_id: str):
    try:
        profile = get_service_profile(service_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "status": "ok",
        "profile": profile.model_dump(mode="json"),
        "latest_metric": latest_service_metrics(service_id),
        "history": service_metric_history(service_id),
    }


@router.get("/services/{service_id}/p4-evidence")
async def service_p4_evidence(service_id: str):
    try:
        get_service_profile(service_id)
        return await current_service_p4_evidence(service_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/services/{service_id}/validate")
async def validate_service(service_id: str, body: ServiceValidationRequest):
    if body.service_id != service_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VALIDATION_PROFILE_MISMATCH",
                "message": f"path service_id={service_id} differs from body={body.service_id}",
            },
        )
    try:
        return await run_service_validation(
            service_id,
            body.model_dump(mode="json", exclude_none=True),
        )
    except (KeyError, ValueError) as exc:
        code = getattr(exc, "code", "VALIDATION_PROFILE_MISMATCH")
        raise HTTPException(
            status_code=409, detail={"code": code, "message": str(exc)}
        ) from exc


@router.get("/traffic-scenarios")
async def traffic_scenario_index():
    return {
        "status": "ok",
        "scenarios": [
            item.model_dump(mode="json") for item in list_traffic_scenarios()
        ],
    }


@router.get("/traffic-scenarios/{scenario_id}")
async def traffic_scenario_detail(scenario_id: str):
    try:
        scenario = get_traffic_scenario(scenario_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "scenario": scenario.model_dump(mode="json")}


@router.post("/traffic-scenarios/{scenario_id}/run")
async def run_configured_traffic_scenario(
    scenario_id: str,
    body: TrafficScenarioRunRequest,
):
    try:
        return await run_traffic_scenario(
            scenario_id,
            body.model_dump(mode="json", exclude_none=True),
        )
    except (KeyError, ValueError) as exc:
        code = getattr(exc, "code", "VALIDATION_PROFILE_MISMATCH")
        raise HTTPException(
            status_code=409, detail={"code": code, "message": str(exc)}
        ) from exc


@router.get("/service-metrics/current")
async def current_service_metrics():
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": latest_service_metrics(),
    }


@router.get("/service-metrics/{service_id}")
async def current_service_metric(service_id: str):
    try:
        return {
            "status": "ok",
            "service": latest_service_metrics(service_id),
            "history": service_metric_history(service_id),
        }
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/telemetry/int")
async def current_int_telemetry():
    payload = await collect_current_telemetry(include_probe=True, include_iperf=False)
    int_probe = (payload.get("raw") or {}).get("int_probe") or {}
    if int_probe.get("status") != "ok":
        raise HTTPException(
            status_code=502, detail=int_probe or {"status": "unavailable"}
        )
    return {
        "status": "ok",
        "source": "p4_int_probe",
        "timestamp": payload.get("timestamp"),
        "quality_status": int_probe.get("quality_status", "UNAVAILABLE"),
        "quality_reason": int_probe.get("quality_reason", "没有可用的INT记录"),
        "used_for_decision": bool(int_probe.get("used_for_decision")),
        "validation_checks": int_probe.get("validation_checks") or [],
        "kpi": {
            "int_hop_count": payload.get("kpi", {}).get("int_hop_count"),
            "int_path_latency_ms": payload.get("kpi", {}).get("int_path_latency_ms"),
            "int_max_queue_occupancy": payload.get("kpi", {}).get(
                "int_max_queue_occupancy"
            ),
        },
        "hop_records": int_probe.get("hop_records") or [],
        "raw": int_probe,
    }


# ── Watcher internal sync endpoint (single-writer to SQLite) ──────────


class WatcherSyncRequest(BaseModel):
    state: Optional[dict[str, Any]] = None
    incident: Optional[dict[str, Any]] = None
    sample: Optional[dict[str, Any]] = None
    a2a_event: Optional[dict[str, Any]] = None


@router.post("/watcher/_sync", status_code=200)
async def watcher_sync(req: WatcherSyncRequest):
    """Internal endpoint for the Watcher process to persist state.

    All SQLite writes from the Watcher go through this endpoint so that
    the API process is the single writer to the database, avoiding
    cross-process SQLITE_BUSY contention.
    """
    store = get_store()
    results: dict[str, Any] = {}

    if req.state:
        results["state"] = store.upsert_watcher_state(req.state)

    if req.incident:
        incident = req.incident
        results["incident"] = store.upsert_watcher_incident(
            incident_id=incident["incident_id"],
            status=incident.get("status", "open"),
            reason=incident.get("reason"),
            kpi_snapshot=incident.get("kpi_snapshot"),
            triggered_intent_id=incident.get("triggered_intent_id"),
            resolved=incident.get("resolved", False),
        )

    if req.sample:
        sample = req.sample
        results["sample"] = store.record_watcher_sample(
            kpi_snapshot=sample.get("kpi_snapshot", {}),
            verdict=sample.get("verdict", {}),
            incident_id=sample.get("incident_id"),
            sample_id=sample.get("sample_id"),
        )

    if req.a2a_event:
        evt = req.a2a_event
        store.add_event(
            evt.get("intent_id", ""),
            evt.get("event_type", "a2a_message"),
            evt.get("data", evt),
        )
        results["a2a_event"] = "ok"

    return {"status": "ok", "results": results}


@router.get("/watcher/status")
async def watcher_status():
    return get_store().get_watcher_state()


@router.get("/watcher/incidents")
async def watcher_incidents(limit: int = Query(20, ge=1, le=100)):
    return {"items": get_store().list_watcher_incidents(limit=limit)}


@router.get("/experience-memory/cases")
async def experience_memory_cases(limit: int = Query(20, ge=1, le=100)):
    items: list[dict[str, Any]] = []
    for case in get_store().list_experience_cases(limit=limit):
        embedding = case.pop("embedding", []) or []
        case["embedding_dim"] = len(embedding)
        items.append(case)
    return {
        "items": items,
        "retrieval_modes": ["keyword", "vector", "hybrid"],
        "default_mode": "keyword",
    }


@router.get("/topology/current")
async def current_topology():
    return await collect_current_topology()


@router.get("/topology/events")
async def topology_events(fields: str = "frontend"):
    """SSE stream of topology changes.  Polls ONOS/Mininet every 5 s and
    pushes the frontend snapshot to the client only when the data
    has changed.

    Query params:
      - fields: "frontend" (default, ~3-5KB) or "full" (complete data, ~15-50KB)
    """

    import hashlib

    async def event_stream():
        last_hash = None
        while True:
            try:
                topo = await collect_current_topology(fields=fields)
                if fields == "frontend":
                    compare_data = topo.get("frontend", {})
                else:
                    compare_data = topo
                current_hash = hashlib.md5(
                    json.dumps(compare_data, sort_keys=True, default=str).encode()
                ).hexdigest()
                if current_hash != last_hash:
                    if fields == "frontend":
                        payload_data = {
                            "frontend": topo.get("frontend", {}),
                            "timestamp": topo.get("timestamp"),
                        }
                    else:
                        payload_data = topo
                    payload = json.dumps(payload_data, ensure_ascii=False, default=str)
                    yield f"data: {payload}\n\n"
                    last_hash = current_hash
            except Exception:
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(5)  # reduced from 2s — topology changes are slow

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/network/bootstrap")
async def bootstrap_network():
    result = await install_baseline_connectivity()
    if result["status"] != "SUCCESS":
        raise HTTPException(status_code=502, detail=result)
    return result


@router.post("/network/switch-ids")
async def configure_switch_ids():
    result = await configure_p4_switch_ids()
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=result)
    return result


@router.post("/qos/clear")
async def clear_qos(policy_id: Optional[str] = None):
    return await clear_qos_policies(policy_id)


@router.post("/fault/congestion")
async def create_congestion_fault(req: CongestionFaultRequest):
    result = await inject_congestion_fault(
        fault_id=req.fault_id,
        node=req.node,
        interface=req.interface,
        rate_mbps=req.rate_mbps,
        burst_kb=req.burst_kb,
        latency_ms=req.latency_ms,
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=result)
    return result


@router.post("/fault/clear")
async def clear_fault(req: ClearFaultRequest | None = None):
    result = await clear_congestion_fault(req.fault_id if req else None)
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=result)
    return result


@router.post("/demo/reset")
async def reset_demo(_req: DemoResetRequest | None = None):
    result = await reset_demo_environment()
    if result.get("status") not in {"ready", "partial"}:
        raise HTTPException(status_code=502, detail=result)
    return result


@router.post("/demo/self-healing")
async def self_healing_demo(req: SelfHealingDemoRequest | None = None):
    req = req or SelfHealingDemoRequest()
    result = await run_self_healing_demo(
        reset_first=req.reset_first,
        include_iperf=req.include_iperf,
        clear_after=req.clear_after,
        fault={
            "fault_id": req.fault_id,
            "node": req.node,
            "interface": req.interface,
            "rate_mbps": req.rate_mbps,
            "burst_kb": req.burst_kb,
            "latency_ms": req.latency_ms,
        },
    )
    if result.get("status") not in {"success", "partial"}:
        raise HTTPException(status_code=502, detail=result)
    return result


@router.post("/demo/preflight")
async def demo_preflight(req: DemoPreflightRequest | None = None):
    req = req or DemoPreflightRequest()
    result = await run_demo_preflight(reset_first=req.reset_first)
    if result.get("status") != "ready":
        raise HTTPException(status_code=502, detail=result)
    return result


@router.post("/demo/qos/validate")
async def validate_qos_demo(req: QoSDemoValidationRequest):
    """Run a repeatable, non-mutating four-layer QoS evidence check."""
    try:
        result = await run_qos_demo_validation(
            req.yang_policy,
            req.execution_result,
            scenario=req.scenario,
            duration=req.duration,
            target_offered_mbps=req.target_offered_mbps,
            background_offered_mbps=req.background_offered_mbps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result.get("status") != "ok":
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/demo/qos/cleanup")
async def cleanup_qos_demo():
    result = await cleanup_qos_demo_processes()
    if result.get("status") != "ok":
        raise HTTPException(status_code=502, detail=result)
    return result


@router.get("/demo/qos/audit")
async def qos_demo_audit(scope_key: str = Query(..., min_length=1, max_length=80)):
    from persistence.policy_store import PolicyStore

    store = PolicyStore()
    record = store.get_by_scope(scope_key)
    if not record:
        raise HTTPException(status_code=404, detail="QoS policy scope not found")
    return {
        "policy": {
            key: record.get(key)
            for key in (
                "policy_id",
                "intent_id",
                "traffic_scope_key",
                "status",
                "version",
                "pending_intent_id",
                "pending_yang_json",
                "updated_at",
                "error_message",
            )
        },
        "history": store.history(scope_key),
    }


# -- Unified Chat endpoint (streaming, multi-turn context, intent escalation) ---


class ChatRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    session_id: Optional[str] = None
    user_id: str = "anonymous"


# -- New Conversation API schemas -------------------------------------------


class SendMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    user_id: str = "anonymous"


class CreateConversationRequest(BaseModel):
    user_id: str = "anonymous"
    title: Optional[str] = None


class ConversationApproveRequest(BaseModel):
    decision: str = Field(..., description="approve | reject | abort")
    reason: str = Field(default="")


class ConversationClarifyRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


def _langchain_history(messages: list[dict]) -> list:
    """Convert stored messages to LangChain message objects for LLM context."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    converted = []
    for m in messages[-20:]:  # last 20 messages for context window
        role = m.get("role", "")
        content = m.get("content", "")
        if not content:
            continue
        if role == "user":
            converted.append(HumanMessage(content=content))
        elif role == "assistant":
            converted.append(AIMessage(content=content))
        elif role == "system":
            converted.append(SystemMessage(content=f"[系统通知] {content}"))
    return converted


CHAT_SYSTEM_PROMPT = (
    "你是 IDN 智能网络助手。用简洁的中文回答用户的网络相关问题。\n"
    "\n"
    "风格要求：\n"
    "- 回复直接使用中文自然语言，不要输出 JSON、代码块或 Markdown 格式标记。\n"
    "- 对于问候和闲聊，友好地自我介绍并引导用户描述网络需求。\n"
    "- 如果用户描述了一个网络操作需求（带宽保障、流量阻断、路由调整等），"
    "先简短确认理解了这个需求，然后说明正在进行意图编排。\n"
    "- 简洁专业，每次回复控制在 200 字以内。\n"
    "- 对话历史中有之前的上下文时，请结合上下文理解用户的意图。"
)


@router.post("/chat", status_code=200)
async def chat_message(req: ChatRequest):
    """Streaming chat endpoint — delegates to ChatSession for unified dialogue.

    Maintains backward compatibility with the old SSE format while using
    the new ChatSession internally.
    """
    store = get_store()
    session_id = (
        req.session_id or store.create_session(req.user_id, req.text[:40])["session_id"]
    )

    session = ChatSession(session_id, store)

    async def _stream():
        async for event in session.send_message(req.text, req.user_id):
            # ChatSession SSEEvent format is already compatible with the old
            # chat endpoint format (data: {...}\n\n)
            yield event.to_sse()

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/non-stream", status_code=200)
async def chat_message_non_stream(req: ChatRequest):
    """Non-streaming fallback — delegates to ChatSession, returns final JSON.

    Consumes the full SSE stream internally and returns only the final
    metadata (reply, intent_id, confidence, etc.).
    """
    store = get_store()
    session_id = (
        req.session_id or store.create_session(req.user_id, req.text[:40])["session_id"]
    )

    session = ChatSession(session_id, store)

    full_reply = ""
    intent_detected = False
    intent_id = None
    intent_type = ""
    confidence = 0.0

    async for event in session.send_message(req.text, req.user_id):
        if event.event_type == "token":
            full_reply += event.data.get("token", "")
        elif event.event_type == "stream_end":
            extra = event.data
            intent_detected = extra.get("intent_detected", False)
            intent_id = extra.get("intent_id")
            intent_type = extra.get("intent_type", "")
            confidence = extra.get("confidence", 0.0)

    return {
        "session_id": session_id,
        "reply": full_reply,
        "intent_detected": intent_detected,
        "intent_hint": "",
        "intent_type": intent_type,
        "confidence": confidence,
        "intent_id": intent_id,
    }


# -- Conversation endpoints (unified dialogue + execution timeline) --------


@router.post("/conversations", status_code=201)
async def create_conversation(req: CreateConversationRequest):
    """Create a new conversation session."""
    return get_store().create_session(req.user_id, req.title)


@router.get("/conversations")
async def list_conversations(
    user_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List conversations for a user.  Reuses the sessions table."""
    return get_store().list_sessions(user_id=user_id, page=page, page_size=page_size)


@router.get("/conversations/{session_id}")
async def get_conversation(session_id: str):
    """Get conversation metadata."""
    session = get_store().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return session


@router.get("/conversations/{session_id}/timeline")
async def get_conversation_timeline(
    session_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """Return the unified conversation timeline.

    Interleaves chat messages and intent lifecycle milestones into a
    single chronologically-ordered list.  This is the primary data
    source for the conversation UI.
    """
    store = get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return build_timeline(store, session_id, page=page, page_size=page_size)


@router.delete("/conversations/{session_id}", status_code=204)
async def delete_conversation(session_id: str):
    """Delete a conversation and all associated messages/intents.

    Note: this permanently deletes the session and related records.
    """
    store = get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    # Delete all intents in this session
    intents = store.list(session_id=session_id, page_size=1000)
    for item in intents.get("items", []):
        store.delete(item["intent_id"])
    # Delete the session itself
    with store._lock, store._connect() as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    return None


@router.post("/conversations/{session_id}/message", status_code=200)
async def send_conversation_message(session_id: str, req: SendMessageRequest):
    """Send a message in a conversation.  Returns an SSE stream.

    The stream delivers:
    - ``message_created`` for each new Message (user, assistant, system)
    - ``token`` for each streaming LLM token
    - ``message_updated`` when the thinking message becomes final text
    - ``stream_end`` with metadata (intent_id, confidence, etc.)
    """
    store = get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    session = ChatSession(session_id, store)

    async def _stream():
        async for event in session.send_message(req.text, req.user_id):
            yield event.to_sse()

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/conversations/{session_id}/clarify", status_code=202)
async def answer_conversation_clarification(
    session_id: str, req: ConversationClarifyRequest
):
    """Answer a clarification question and resume pipeline execution.

    Returns the intent_id whose clarification was answered.
    """
    store = get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    # Find the pending-clarification intent for this session
    intents = store.list(
        session_id=session_id, status="pending_clarification", page_size=1
    )
    items = intents.get("items", [])
    if not items:
        raise HTTPException(status_code=404, detail="没有等待澄清的意图")

    intent_id = items[0]["intent_id"]

    async def _stream():
        session = ChatSession(session_id, store)
        async for event in session.answer_clarification(intent_id, req.text):
            yield event.to_sse()

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/conversations/{session_id}/approve", status_code=200)
async def submit_conversation_approval(
    session_id: str, req: ConversationApproveRequest
):
    """Submit an approval decision (approve/reject/abort).

    Delegates to the existing approval machinery in approve_intent().
    """
    store = get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    if req.decision not in ("approve", "reject", "abort"):
        raise HTTPException(
            status_code=422,
            detail=f"无效的决策: '{req.decision}'，允许: approve, reject, abort",
        )

    # Find the pending-approval intent for this session
    intents = store.list(session_id=session_id, status="pending_approval", page_size=1)
    items = intents.get("items", [])
    if not items:
        raise HTTPException(status_code=404, detail="没有等待审批的意图")

    intent_id = items[0]["intent_id"]

    # Delegate to existing approve_intent handler (defined below in this file)
    return await approve_intent(
        intent_id,
        ApprovalRequest(decision=req.decision, reason=req.reason),
    )


@router.post("/conversations/{session_id}/cancel", status_code=200)
async def cancel_conversation_execution(session_id: str):
    """Cancel any running execution in this conversation."""
    store = get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    # Use the same cleanup-backed withdrawal workflow as intent-level APIs.
    cancelled = []
    failures = []
    intents = store.list(session_id=session_id, page_size=100)
    from orchestrator.intent_control_plane import get_intent_control_plane

    for item in intents.get("items", []):
        if item["status"] != "cancelled":
            try:
                runtime = await get_intent_control_plane().withdraw(item["intent_id"])
                cancelled.append(
                    {
                        "intent_id": item["intent_id"],
                        "status": runtime.lifecycle.value,
                    }
                )
            except Exception as exc:
                failures.append({"intent_id": item["intent_id"], "error": str(exc)})

    return {"session_id": session_id, "cancelled": cancelled, "failures": failures}


@router.get("/conversations/{session_id}/stream")
async def conversation_stream(session_id: str):
    """Unified SSE stream for a conversation.

    A single EventSource connection delivers all conversation events:
    new messages, stage progress, heartbeats.  Use this instead of
    subscribing to individual intent event streams.
    """
    store = get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    from api.conversation.stream import conversation_event_stream

    return StreamingResponse(
        conversation_event_stream(session_id, store),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# -- Suggestions endpoint (topology-aware intent templates) ----------------


@router.get("/suggestions")
async def get_suggestions():
    """Return topology-aware intent suggestions based on current network state.

    Uses the live topology snapshot to generate relevant intent templates
    with real IPs, subnets, and P4 capabilities.
    """
    try:
        topo = await collect_current_topology(fields="full")
    except Exception:
        topo = {}

    hosts = topo.get("hosts") or []
    devices = topo.get("devices") or []
    links = topo.get("links") or []

    suggestions = _generate_topology_suggestions(hosts, devices, links)
    return {"items": suggestions, "source": "topology_aware"}


def _generate_topology_suggestions(
    hosts: list,
    devices: list,
    links: list,
) -> list[dict]:
    """Generate topology-aware intent templates from live network data.

    Groups hosts by /24 subnet and generates per-pair suggestions with
    realistic bandwidth values derived from link data.
    """
    suggestions: list[dict] = []

    if not hosts:
        return [
            {
                "name": "带宽保障",
                "summary": "保障关键业务带宽",
                "priority": "high",
                "text": "保障关键业务的网络带宽不低于指定速率",
            },
            {
                "name": "流量阻断",
                "summary": "阻断特定IP间流量",
                "priority": "critical",
                "text": "阻断指定源IP到目的IP的所有流量",
            },
            {
                "name": "网络优化",
                "summary": "优化网络质量",
                "priority": "medium",
                "text": "优化网络路径，降低时延和丢包率",
            },
        ]

    # Group hosts by /24 subnet
    subnet_hosts: dict[str, list[dict]] = {}
    for h in hosts:
        ip = (h.get("ipAddresses") or [None])[0]
        if not ip:
            continue
        parts = str(ip).split("/")[0].split(".")
        subnet = ".".join(parts[:3]) + ".0/24" if len(parts) >= 3 else ip
        subnet_hosts.setdefault(subnet, []).append(h)

    # Derive realistic bandwidth from link data
    max_bw_mbps = 10000
    if links:
        bw_values = [
            int(l.get("bandwidth_mbps") or l.get("bandwidth") or 0)
            for l in links
            if l.get("bandwidth_mbps") or l.get("bandwidth")
        ]
        if bw_values:
            max_bw_mbps = max(min(max(bw_values), 10000), 1000)

    # Per-subnet-pair suggestions (cap at 10 to avoid frontend overload)
    subnets = list(subnet_hosts.items())
    for i, (src_sub, src_hosts) in enumerate(subnets):
        for j, (dst_sub, dst_hosts) in enumerate(subnets):
            if i >= j:
                continue
            src = src_hosts[0]
            dst = dst_hosts[0]
            src_ip = (src.get("ipAddresses") or [src_sub])[0]
            dst_ip = (dst.get("ipAddresses") or [dst_sub])[0]
            src_name = src.get("name") or src.get("id") or f"子网{src_sub}"
            dst_name = dst.get("name") or dst.get("id") or f"子网{dst_sub}"
            bw_gbps = max(max_bw_mbps // 1000, 1)

            suggestions.append(
                {
                    "name": "带宽保障",
                    "summary": f"保障 {src_name}→{dst_name} 带宽",
                    "priority": "high",
                    "text": f"保障 {src_ip} 到 {dst_ip} 的业务带宽不低于 {bw_gbps}Gbps",
                }
            )
            suggestions.append(
                {
                    "name": "流量阻断",
                    "summary": f"阻断 {src_name}→{dst_name} 流量",
                    "priority": "critical",
                    "text": f"阻断 {src_ip} 到 {dst_ip} 的所有流量",
                }
            )
            if len(suggestions) >= 10:
                break
        if len(suggestions) >= 10:
            break

    suggestions.append(
        {
            "name": "网络优化",
            "summary": f"优化网络质量（{len(devices)}台设备，{len(links)}条链路）",
            "priority": "medium",
            "text": f"优化当前网络（{len(devices)}台设备，{len(links)}条链路）的转发质量，降低时延和丢包率",
        }
    )
    return suggestions


# -- Session endpoints ----------------------------------------------------


@router.post("/sessions", status_code=201)
async def create_session(req: CreateSessionRequest):
    return get_store().create_session(req.user_id, req.title)


@router.get("/sessions")
async def list_sessions(
    user_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return get_store().list_sessions(user_id=user_id, page=page, page_size=page_size)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    session = get_store().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.get("/sessions/{session_id}/messages")
async def list_session_messages(session_id: str):
    store = get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"session_id": session_id, "items": store.list_messages(session_id)}


@router.get("/sessions/{session_id}/intents")
async def list_session_intents(
    session_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    store = get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return store.list(session_id=session_id, page=page, page_size=page_size)


@router.post(
    "/intents/preview",
    status_code=200,
    response_model=PreviewIntentResponse,
    responses=ERROR_RESPONSES,
)
async def preview_intent(req: SubmitIntentRequest):
    """Preview what WOULD happen without committing flows to the data plane.

    Runs the STAV pipeline through verify_rules (sense → parse_intent →
    check_conflict → generate_policy → verify_rules), then returns the
    compiled plan for human review.

    Does NOT execute flows or persist any state beyond the ephemeral
    LangGraph checkpoint (memory-only).
    """
    # 1. Build a deterministic fallback only for a network-unavailable reply.
    # The normal preview response below is rebuilt from the actual planner
    # result, so it truthfully reports the LLM invocation evidence.
    intent_id = f"PREVIEW-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    from agents.intent_understanding import IntentUnderstandingAgent

    intent_spec = IntentUnderstandingAgent().understand(
        intent_id=intent_id,
        text=req.text,
        legacy_plan=None,
        priority=req.priority.value,
        context=req.context,
    )

    # 2. Fetch network snapshot (reuses NetworkSensingAgent for field mapping).
    from agents.network_sensing import NetworkSensingAgent

    agent = NetworkSensingAgent()
    try:
        try:
            snapshot = await agent.collect_snapshot()
        except Exception as exc:
            return {
                "intent_id": intent_id,
                "status": "preview",
                "user_intent": req.text,
                "intent_spec": intent_spec.model_dump(mode="json"),
                "network_status": {"status": "UNAVAILABLE", "error": str(exc)},
                "planning_status": "NOT_EVALUATED",
                "yang_policies": [],
                "flow_table_plan": {},
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        snapshot.existing_policies = []  # preview is read-only, skip conflict data
    finally:
        await agent.close()

    # 3. Invoke the same real intent-planner boundary used by the workflow.
    # A relation-only request can stop here: it produces an auditable scheduler
    # projection and must not enter policy/P4/TC/ONOS compilation.
    from agents.intent_planner import IntentPlannerAgent

    preview_plan = await IntentPlannerAgent().plan(
        user_input=req.text,
        network_snapshot=snapshot,
        context=req.context,
        intent_id=intent_id,
    )
    parsed_spec = IntentUnderstandingAgent().understand(
        intent_id=intent_id,
        text=req.text,
        legacy_plan=preview_plan,
        priority=req.priority.value,
        context=req.context,
    )
    if parsed_spec.preferences:
        from services.preference_relations import scheduler_projection

        projection = scheduler_projection(parsed_spec.preferences)
        return {
            "intent_id": intent_id,
            "status": "preview",
            "user_intent": req.text,
            "intent_spec": parsed_spec.model_dump(mode="json"),
            "network_status": {"status": "AVAILABLE"},
            "planning_status": "PREFERENCE_CONTROL_PLANE_READY",
            "intent_plan": preview_plan.model_dump(mode="json"),
            "yang_policies": [],
            "flow_table_plan": {
                "execution_capability": projection["execution_capability"],
                "southbound_mutation_required": False,
                "write_requests": [],
                "tc_mutations": [],
                "onos_mutations": [],
                "scheduler_projection": projection,
                "total_entries": 0,
                "affected_devices": [],
            },
            "affected_devices": [],
            "total_entries": 0,
            "capacity_check_passed": True,
            "capacity_warnings": [],
            "conflict_report": None,
            "rule_verification_report": None,
            "parse_warnings": list(preview_plan.parse_warnings),
            "ai_degraded": False,
            "degradation_reason": "",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # 4. Run the normal dry-run graph for intents that require policy planning.
    from models.agent_state import GlobalAgentState
    from models.intent import UserIntent

    state = GlobalAgentState(
        intent_id=intent_id,
        user_intent=UserIntent(
            intent_id=intent_id,
            text=req.text,
            priority=req.priority,
            user_id=req.user_id,
            session_id=req.session_id or "",
            context=req.context,
        ),
        network_snapshot=snapshot,
    )

    from orchestrator.graph import IDNOrchestrator

    orch = IDNOrchestrator(dry_run=True)
    await orch.initialize()
    try:
        final_state = await orch.run(state)
    finally:
        await orch.close()

    # 3. Serialize preview response
    # LangGraph may return a dict (from checkpoint) or GlobalAgentState object
    def _sv(key: str, default: Any = None) -> Any:
        if isinstance(final_state, dict):
            return final_state.get(key, default)
        return getattr(final_state, key, default)

    def _jsonable(value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return value
        return None

    flow_plan = _jsonable(_sv("flow_table_plan")) or {}
    yang_policies = [_jsonable(p) for p in (_sv("yang_policies") or []) if _jsonable(p)]
    conflict_report = _jsonable(_sv("conflict_report"))
    rule_verification = _jsonable(_sv("rule_verification_report"))
    intent_plan = _sv("intent_plan")
    intent_plan_data = _jsonable(intent_plan)

    parse_warnings = []
    if intent_plan:
        parse_warnings = (
            intent_plan.parse_warnings
            if hasattr(intent_plan, "parse_warnings")
            else (
                intent_plan.get("parse_warnings", [])
                if isinstance(intent_plan, dict)
                else []
            )
        )

    return {
        "intent_id": intent_id,
        "status": "preview",
        "user_intent": req.text,
        "intent_spec": IntentUnderstandingAgent().understand(
            intent_id=intent_id,
            text=req.text,
            legacy_plan=intent_plan,
            priority=req.priority.value,
            context=req.context,
        ).model_dump(mode="json"),
        "network_status": {"status": "AVAILABLE"},
        "planning_status": "EVALUATED",
        "intent_plan": intent_plan_data,
        "yang_policies": yang_policies,
        "flow_table_plan": flow_plan,
        "affected_devices": flow_plan.get("affected_devices", []),
        "total_entries": flow_plan.get("total_entries", 0),
        "capacity_check_passed": flow_plan.get("capacity_check_passed", True),
        "capacity_warnings": flow_plan.get("capacity_warnings", []),
        "conflict_report": conflict_report,
        "rule_verification_report": rule_verification,
        "parse_warnings": parse_warnings,
        "ai_degraded": flow_plan.get("ai_degraded", False),
        "degradation_reason": flow_plan.get("degradation_reason", ""),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post(
    "/intents",
    status_code=202,
    response_model=SubmitIntentResponse,
    responses=ERROR_RESPONSES,
)
async def submit_intent(
    req: SubmitIntentRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    store = get_store()
    operation = "POST:/api/v1/intents"
    payload = req.model_dump(mode="json")
    replay = store.begin_idempotent(
        operation=operation, key=idempotency_key, payload=payload
    )
    if replay is not None:
        return replay
    try:
        if req.session_id:
            if store.get_session(req.session_id) is None:
                raise HTTPException(status_code=404, detail="会话不存在")
            session_id = req.session_id
        else:
            session_id = None

        # Repair any old multi-session duplicates before deciding whether this
        # request can reuse the authoritative logical waiting owner.
        from services.waiting_intent_ownership import reconcile_waiting_intent_owners
        await reconcile_waiting_intent_owners(store)

        waiting = store.find_equivalent_waiting_intent(
            user_id=req.user_id, session_id=session_id, text=req.text,
            context=req.context,
        )
        if waiting is not None:
            jobs = store.list_jobs(waiting["intent_id"])
            latest_job = jobs[-1] if jobs else {}
            response = {
                "session_id": waiting["session_id"],
                "intent_id": waiting["intent_id"],
                "job_id": latest_job.get("job_id", ""),
                "job_status": latest_job.get("status", waiting.get("status", "pending_clarification")),
                "status": waiting.get("status", "pending_clarification"),
                "created_at": waiting.get("created_at"),
                "reused": True,
                "reuse_result": "REUSE_WAITING_INTENT",
            }
            store.complete_idempotent(
                operation=operation, key=idempotency_key, response=response,
                resource_id=waiting["intent_id"],
            )
            return response

        if session_id is None:
            session_id = store.create_session(req.user_id, req.text[:40])["session_id"]

        intent_id = f"INT-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
        store.create(
            intent_id=intent_id,
            text=req.text,
            priority=req.priority.value,
            user_id=req.user_id,
            session_id=session_id,
            context=req.context,
        )
        from orchestrator.intent_control_plane import get_intent_control_plane

        get_intent_control_plane().register_received(
            intent_id=intent_id,
            text=req.text,
            priority=req.priority.value,
            context=req.context,
        )
        job = store.enqueue_job(
            intent_id,
            {
                "text": req.text,
                "priority": req.priority.value,
                "user_id": req.user_id,
                "session_id": session_id,
                "context": req.context,
            },
        )
        response = {
            "session_id": session_id,
            "intent_id": intent_id,
            "job_id": job["job_id"],
            "job_status": job["status"],
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reused": False,
            "reuse_result": None,
        }
        store.complete_idempotent(
            operation=operation,
            key=idempotency_key,
            response=response,
            resource_id=intent_id,
        )
        return response
    except Exception:
        store.abort_idempotent(operation=operation, key=idempotency_key)
        raise


@router.post("/intents/{intent_id}/clarifications", status_code=202)
async def answer_clarification(intent_id: str, req: ClarificationRequest):
    store = get_store()
    record = store.get(intent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="意图不存在")
    if record["status"] != "pending_clarification" or not (
        record.get("snapshot") or {}
    ).get("needs_clarification"):
        raise HTTPException(status_code=409, detail="当前意图不在等待澄清状态")

    session_id = record["session_id"]
    store.add_message(
        session_id,
        "user",
        "clarification_answer",
        req.text,
        intent_id=intent_id,
    )
    execution_text = f"{record['execution_text']}\n用户补充说明：{req.text}"
    previous_context = dict(record.get("context") or {})
    incoming_context = dict(req.context or {})
    context = {**previous_context, **incoming_context}
    clarifications: list[dict[str, Any]] = []
    clarifications.extend(previous_context.get("clarifications") or [])
    clarifications.extend(incoming_context.get("clarifications") or [])
    clarifications.append(
        {
            "question": (record.get("snapshot") or {}).get("clarification_question"),
            "question_details": (record.get("snapshot") or {}).get(
                "clarification_questions"
            )
            or [],
            "answer": req.text,
            "answered_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    context["clarifications"] = clarifications
    context["latest_clarification_answer"] = req.text
    resumed_snapshot = dict(record.get("snapshot") or {})
    resumed_snapshot.update(
        {
            "phase": "PLANNING",
            "needs_clarification": False,
            "requires_user_input": False,
            "clarification_prompt": None,
            "clarification_question": None,
            "clarification_questions": [],
            "clarification_details": [],
            "unresolved_fields": [],
            "missing_fields": [],
            "reason_code": None,
        }
    )
    store.update(
        intent_id,
        status="pending",
        execution_text=execution_text,
        context=context,
        snapshot=resumed_snapshot,
    )

    # Advance the formal lifecycle before queuing the continuation.  This is
    # deliberately not a UI-only state update: the current-intent projection
    # must expose the same Intent as executing as soon as its answer is
    # accepted, even before the worker claims the job.
    from orchestrator.intent_control_plane import get_intent_control_plane
    try:
        get_intent_control_plane().resume_after_clarification(intent_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Use the same identifiers so checkpoints and dialogue history stay linked.
    job = store.enqueue_job(
        intent_id,
        {
            "text": execution_text,  # include clarification answer in re-run
            "priority": record["priority"],
            "user_id": record["user_id"],
            "session_id": session_id,
            "context": context,
        },
    )
    return {
        "session_id": session_id,
        "intent_id": intent_id,
        "job_id": job["job_id"],
        "job_status": job["status"],
        "status": "pending",
    }


@router.get("/intents")
async def list_intents(
    status: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    response = get_store().list(
        status=status, user_id=user_id, page=page, page_size=page_size
    )
    from api.intent_lifecycle_projection import project_intent_lifecycle
    from persistence.intent_domain_store import get_intent_domain_store

    domain = get_intent_domain_store()
    for item in response["items"]:
        runtime = domain.get_runtime(item["intent_id"])
        spec = domain.get_spec(item["intent_id"])
        item.update(project_intent_lifecycle(
            item, runtime=runtime, jobs=get_store().list_jobs(item["intent_id"]),
        ))
        item["intent_lifecycle"] = item["lifecycle_status"]
        item["intent_revision"] = spec.revision if spec else None
    return response


@router.get("/intents/current")
async def get_current_intents(
    group: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    include_evidence: bool = False,
    include_projection_details: bool = False,
):
    """Read persisted Intent projections only; never invokes LLM or southbound I/O."""
    from services.current_intent_query import CurrentIntentQueryService

    return CurrentIntentQueryService().list_current(
        group=group,
        limit=limit,
        include_evidence=include_evidence,
        include_projection_details=include_projection_details,
    )


@router.post("/intents/current/reconcile-waiting")
async def reconcile_current_waiting_owners():
    """Formally retire legacy duplicate waiting owners; history is retained."""
    from services.waiting_intent_ownership import reconcile_waiting_intent_owners

    return await reconcile_waiting_intent_owners(get_store())


@router.post("/intents/effective/{effective_scope_id}/withdraw")
async def withdraw_effective_intent(
    effective_scope_id: str,
    request: EffectiveWithdrawRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Formal, scope-level withdrawal for a Dashboard effective-owner card."""
    from orchestrator.intent_control_plane import get_intent_control_plane
    from orchestrator.intent_withdrawal import IntentWithdrawalService

    control = get_intent_control_plane()
    try:
        result = await IntentWithdrawalService(
            control.store, control.domain, control.execution,
        ).withdraw_effective_scope(
            effective_scope_id,
            expected_policy_id=request.expected_policy_id,
            expected_policy_version=request.expected_policy_version,
            expected_binding_id=request.expected_binding_id,
            actor="dashboard",
            reason=request.reason,
        )
    except RuntimeError as exc:
        if str(exc) in {"CURRENT_OWNER_CHANGED", "SCOPE_EXECUTION_LINEAGE_AMBIGUOUS"}:
            raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
        raise
    return {**result, "idempotency_key": idempotency_key, "readback_verified": result.get("status") in {"WITHDRAWN", "ALREADY_WITHDRAWN"}}


@router.post("/intents/projections/repair")
async def repair_intent_projections(limit: int = Query(default=200, ge=1, le=200)):
    """Run the explicit persisted-projection repair; never touches data plane."""
    from services.intent_projection_repair import IntentProjectionRepairService

    return IntentProjectionRepairService().repair(limit=limit)


@router.get("/intents/{intent_id}")
async def get_intent(intent_id: str):
    store = get_store()
    record = store.get(intent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="意图不存在")
    # ── Lazy consistency repair ──
    checkpoint_at = record.get("checkpoint_at")
    updated_at = record.get("updated_at")
    if checkpoint_at and updated_at and checkpoint_at > updated_at:
        logger = __import__("logging").getLogger(__name__)
        logger.warning(
            "Intent %s: checkpoint_at (%s) > updated_at (%s) — "
            "attempting lazy repair from LangGraph checkpoint",
            intent_id,
            checkpoint_at,
            updated_at,
        )
        try:
            from api.worker import _workflow_record
            from orchestrator.checkpointer import get_async_checkpointer

            checkpointer = await get_async_checkpointer("sqlite")
            config = {
                "configurable": {
                    "thread_id": f"{intent_id}::{record.get('session_id', '')}"
                }
            }
            checkpoint = await checkpointer.aget_tuple(config)
            if checkpoint and checkpoint.checkpoint:
                from models.agent_state import GlobalAgentState

                state = GlobalAgentState(
                    **checkpoint.checkpoint.get("channel_values", {})
                )
                final_status, snapshot, milestones, result = _workflow_record(state)
                # Fire-and-forget repair — don't block the GET response
                import asyncio

                asyncio.create_task(
                    _repair_intent_store(
                        intent_id, final_status, snapshot, milestones, result
                    )
                )
                record["status"] = final_status
                record["snapshot"] = snapshot
                record["milestones"] = milestones
                record["result"] = result
        except Exception as exc:
            logger.warning("Lazy repair failed for intent %s: %s", intent_id, exc)
    from persistence.intent_domain_store import get_intent_domain_store

    from api.intent_lifecycle_projection import project_intent_lifecycle

    domain = get_intent_domain_store()
    spec = domain.get_spec(intent_id)
    runtime = domain.get_runtime(intent_id)
    record.update(project_intent_lifecycle(
        record, runtime=runtime, jobs=store.list_jobs(intent_id),
    ))
    record["intent_lifecycle"] = record["lifecycle_status"]
    record["intent_spec"] = spec.model_dump(mode="json") if spec else None
    record["intent_runtime"] = runtime.model_dump(mode="json") if runtime else None
    from services.conversation_results import build_assistant_intent_message
    record["assistant_message"] = build_assistant_intent_message(record)
    return record


@router.get("/intents/{intent_id}/assistant-message")
async def get_intent_assistant_message(intent_id: str):
    """Read-only deterministic conversation result for one intent."""
    record = await get_intent(intent_id)
    return record["assistant_message"]


async def _repair_intent_store(
    intent_id: str,
    status: str,
    snapshot: dict,
    milestones: list,
    result: dict,
) -> None:
    """Fire-and-forget repair of the API store from checkpoint data."""
    try:
        from api.store import get_store
        from api.worker import _utc_now

        get_store().update_with_checkpoint_verification(
            intent_id,
            checkpoint_timestamp=_utc_now(),
            status=status,
            snapshot=snapshot,
            milestones=milestones,
            result=result,
        )
    except Exception:
        pass


@router.post(
    "/intents/{intent_id}/cancel",
    status_code=200,
    response_model=CancellationResponse,
    responses=ERROR_RESPONSES,
)
async def cancel_intent(intent_id: str):
    """Cancel execution only; policy withdrawal remains a separate operation."""
    store = get_store()
    if store.get(intent_id) is None:
        raise HTTPException(status_code=404, detail="意图不存在")
    from api.cancellation import request_cancellation
    try:
        return request_cancellation(store, intent_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete(
    "/intents/{intent_id}",
    status_code=200,
    response_model=WithdrawalResponse,
    responses=ERROR_RESPONSES,
)
async def delete_intent(intent_id: str):
    if get_store().get(intent_id) is None:
        raise HTTPException(status_code=404, detail="意图不存在")
    from orchestrator.intent_control_plane import get_intent_control_plane

    runtime = await get_intent_control_plane().withdraw(intent_id)
    return {
        "intent_id": intent_id,
        "status": runtime.lifecycle.value,
        "runtime": runtime.model_dump(mode="json"),
    }


@router.get("/intents/{intent_id}/decision-report")
async def get_intent_decision_report(intent_id: str):
    record = get_store().get(intent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="意图不存在")
    result = record.get("result") or {}
    report = result.get("decision_report")
    if not report:
        raise HTTPException(status_code=404, detail="决策报告尚未生成")
    return {
        "intent_id": intent_id,
        "decision_report": report,
        "a2a_timeline": result.get("a2a_timeline") or [],
        "a2a_runtime_evidence": result.get("a2a_runtime_evidence") or {},
    }


# ── Human Approval endpoints ────────────────────────────────────────────


class ApprovalRequest(BaseModel):
    decision: str = Field(..., description="approve / reject / abort")
    reason: str = Field(default="", description="审批备注")


@router.post("/intents/{intent_id}/approve", status_code=200)
async def approve_intent(intent_id: str, req: ApprovalRequest):
    """Resume a paused intent with a human approval decision.

    The decision must be one of 'approve' (continue execution),
    'reject' (recompile policy), or 'abort' (stop and rollback).

    Internally calls ``graph.ainvoke(Command(resume={...}), config)``.
    """
    store = get_store()
    record = store.get(intent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="意图不存在")

    if record.get("status") != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail=f"意图状态为 '{record.get('status')}'，当前不可审批（需要 pending_approval 状态）",
        )

    if req.decision not in ("approve", "reject", "abort"):
        raise HTTPException(
            status_code=422,
            detail=f"无效的决策: '{req.decision}'，允许的值: approve, reject, abort",
        )

    # Build thread_id matching the orchestrator's config
    session_id = record.get("session_id", "")
    thread_id = f"{intent_id}::{session_id}"
    config = {"configurable": {"thread_id": thread_id}}

    try:
        from langgraph.types import Command
        from orchestrator.graph import IDNOrchestrator

        orchestrator = IDNOrchestrator()
        await orchestrator.initialize()

        # Record the approval decision
        store.add_message(
            session_id,
            "user",
            "approval",
            (
                f"人工审批: {req.decision} — {req.reason}"
                if req.reason
                else f"人工审批: {req.decision}"
            ),
            intent_id=intent_id,
        )

        # Resume the graph with the decision
        resume_value = {"decision": req.decision, "reason": req.reason}
        result = await orchestrator.graph.ainvoke(
            Command(resume=resume_value),
            config=config,
        )

        await orchestrator.close()

        # Extract and persist the result
        from api.worker import _attach_runtime_a2a_trace, _utc_now, _workflow_record

        final_status, snapshot, milestones, result_data = _workflow_record(result)

        # If abort was chosen, trigger flow compensation
        if req.decision == "abort":
            try:
                from api.worker import _compensate_flows

                await _compensate_flows(intent_id, store)
            except Exception:
                pass

        _attach_runtime_a2a_trace(
            store=store,
            intent_id=intent_id,
            snapshot=snapshot,
            result=result_data,
        )
        store.update_with_checkpoint_verification(
            intent_id,
            checkpoint_timestamp=_utc_now(),
            status=final_status,
            snapshot=snapshot,
            milestones=[],
            result=result_data,
        )

        return {
            "intent_id": intent_id,
            "decision": req.decision,
            "status": final_status,
            "snapshot": snapshot,
        }

    except ImportError as exc:
        logger = __import__("logging").getLogger(__name__)
        logger.warning("Cannot resume graph — LangGraph Command unavailable: %s", exc)
        raise HTTPException(
            status_code=501,
            detail="LangGraph resume (interrupt/Command) is not available in this environment",
        )
    except Exception as exc:
        logger = __import__("logging").getLogger(__name__)
        logger.exception("Resume intent %s failed", intent_id)
        raise HTTPException(status_code=502, detail=f"恢复执行失败: {exc}")


@router.post("/intents/{intent_id}/rollback", status_code=200)
async def rollback_intent(intent_id: str):
    """Rollback a paused intent: abort execution and clean up installed flows.

    Equivalent to ``POST /intents/{id}/approve`` with ``decision=abort``.
    """
    return await approve_intent(
        intent_id,
        ApprovalRequest(decision="abort", reason="用户手动触发回滚"),
    )


@router.get("/intents/{intent_id}/a2a-trace")
async def get_intent_a2a_trace(intent_id: str):
    store = get_store()
    record = store.get(intent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="意图不存在")
    result = record.get("result") or {}
    evidence = result.get("a2a_runtime_evidence")
    from api.a2a_trace import build_runtime_evidence
    from persistence.intent_domain_store import get_intent_domain_store

    if not evidence:
        runtime_events = [
            event
            for event in store.list_events(intent_id)
            if event.get("event_type") == "a2a_message"
        ]
        evidence = build_runtime_evidence(intent_id, runtime_events)
    evidence = dict(evidence or {})
    domain = get_intent_domain_store()
    decisions = domain.decisions(intent_id)
    healing = domain.healing(intent_id)
    runtime = domain.get_runtime(intent_id)
    target_by_type = {
        "OBJECTIVE_ASSESSMENT": "intent_root_cause",
        "ROOT_CAUSE": "intent_healing",
        "HEALING_PROPOSAL": "network_sensing",
        "NETWORK_CONTEXT_REFRESH": "intent_planning",
        "CANDIDATE_SELECTION": "conflict_safety",
        "HEALING_SAFETY_CHECK": "intent_execution",
        "DEVICE_CONFIG_EXECUTION": "status_verifier",
    }
    healing_decision_types = set(target_by_type) | {"OBJECTIVE_ASSESSMENT"}
    latest_healing = healing[0] if healing else {}
    formal_messages = []
    for decision in reversed(decisions):
        decision_type = str(decision.get("decision_type") or "")
        if decision_type not in healing_decision_types:
            continue
        correlation = {
            "intent_id": intent_id,
            "revision": decision.get("intent_revision"),
            "violation_id": latest_healing.get("violation_id"),
            "assurance_id": (
                (decision.get("output") or {}).get("assurance_id")
                or latest_healing.get("after_assurance_id")
                or latest_healing.get("assurance_id")
            ),
            "root_cause_id": latest_healing.get("root_cause_id"),
            "healing_id": latest_healing.get("healing_id"),
            "context_id": decision.get("context_id"),
            "plan_id": decision.get("plan_id"),
            "execution_id": (
                (decision.get("output") or {}).get("execution_id")
                or latest_healing.get("execution_id")
            ),
        }
        formal_messages.append(
            {
                "id": f"A2A-FORMAL-{decision.get('decision_id')}",
                "protocol_version": "a2a.runtime.v1",
                "trace_id": f"TRACE-{intent_id}-HEALING",
                "intent_id": intent_id,
                "intent_revision": decision.get("intent_revision"),
                "plan_id": decision.get("plan_id"),
                "context_id": decision.get("context_id"),
                "jsonrpc": "2.0",
                "method": "a2a.task.delegate",
                "message_type": "Task_Delegation",
                "task_id": decision.get("decision_id"),
                "correlation_id": decision.get("correlation_id"),
                "source_agent": decision.get("agent_id"),
                "target_agent": target_by_type.get(
                    decision_type, "intent_assurance"
                ),
                "target_agents": [
                    target_by_type.get(decision_type, "intent_assurance")
                ],
                "stage": decision_type,
                "status": "success",
                "timestamp": decision.get("created_at"),
                "input_summary": decision.get("rationale_summary") or decision_type,
                "output_summary": "; ".join(decision.get("facts") or [])
                or decision_type,
                "confidence": decision.get("confidence"),
                "evidence_refs": decision.get("evidence_refs") or [],
                "payload_schema": f"Intent.{decision_type}.v1",
                "payload": {
                    "input_refs": decision.get("input_refs") or [],
                    "inputs": decision.get("inputs") or {},
                    "output": decision.get("output") or {},
                    "correlation": correlation,
                    "llm_status": decision.get("llm_status") or "NOT_USED",
                },
                "metadata": {
                    "source": "PERSISTED_FORMAL_DECISION",
                    "correlation": correlation,
                },
            }
        )
    messages = list(evidence.get("messages") or []) + formal_messages
    messages.sort(key=lambda item: str(item.get("timestamp") or ""))
    for index, message in enumerate(messages, 1):
        message["sequence"] = index
    evidence.update(
        {
            "intent_id": intent_id,
            "runtime_trace_ready": bool(messages),
            "message_count": len(messages),
            "messages": messages,
            "agents": sorted(
                {
                    str(value)
                    for item in messages
                    for value in (item.get("source_agent"), item.get("target_agent"))
                    if value
                }
            ),
            "message_types": sorted(
                {str(item.get("message_type")) for item in messages if item.get("message_type")}
            ),
            "formal_healing_trace": bool(formal_messages),
            "formal_healing_message_count": len(formal_messages),
            "healing_correlation": latest_healing,
            "current_runtime": runtime.model_dump(mode="json") if runtime else None,
        }
    )
    if not evidence["runtime_trace_ready"]:
        raise HTTPException(status_code=404, detail="A2A 运行时证据链尚未生成")
    return evidence


@router.get("/intents/{intent_id}/jobs")
async def list_intent_jobs(intent_id: str):
    store = get_store()
    if store.get(intent_id) is None:
        raise HTTPException(status_code=404, detail="意图不存在")
    return {"intent_id": intent_id, "items": store.list_jobs(intent_id)}


@router.get("/intents/{intent_id}/events")
async def intent_events(intent_id: str, request: Request):
    store = get_store()
    if store.get(intent_id) is None:
        raise HTTPException(status_code=404, detail="意图不存在")

    async def event_generator():
        try:
            cursor = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            cursor = 0
        idle_seconds = 0
        # Batch limit prevents flooding the SSE channel when many events
        # are produced in a short burst (e.g. concurrent A2A messages).
        max_events_per_batch = int(os.getenv("IDN_SSE_MAX_BATCH", "50"))
        try:
            while True:
                if await request.is_disconnected():
                    break
                events = store.list_events(intent_id, after_id=cursor)
                if events:
                    batch = events[:max_events_per_batch]
                    for event in batch:
                        cursor = event["event_id"]
                        data = json.dumps(event["data"], ensure_ascii=False)
                        yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {data}\n\n"
                    # If we hit the batch limit there may be more; yield
                    # remaining events in the next cycle without sleeping.
                    idle_seconds = 0
                else:
                    await asyncio.sleep(1)
                    idle_seconds += 1
                    if idle_seconds >= 10:
                        yield ": keepalive\n\n"
                        idle_seconds = 0
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

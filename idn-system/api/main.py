"""
api/main.py
===========

FastAPI 应用程序入口 —— IDN-System 的 HTTP 服务层。

# 系统角色
创建并配置 FastAPI 应用实例，包括：
- 生命周期管理（lifespan）：启动/关闭 Worker 和 Stale Job Recovery
- 中间件：Trace ID 注入、CORS、Demo API Key 认证
- 全局异常处理：Python 异常 → ErrorInfo 标准化 JSON 响应
- 健康检查、路由注册

# 主要依赖
- fastapi.FastAPI / CORSMiddleware
- api.routes：注册所有 REST API 路由
- api.worker：后台任务消费者（intent 工作流执行）
- api.store：数据持久化

# 启动方式
    uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from api.diagnostics import router as diagnostics_router
from api.auth import auth_enabled, router as auth_router, session_from_request
from api.faults import router as faults_router
from api.intent_control import router as intent_control_router
from api.probes import router as probes_router
from api.reconciliation import router as reconciliation_router
from api.recovery import router as recovery_router
from api.restart_readback import router as restart_readback_router
from api.resources import router as resources_router
from api.routes import router
from api.soak import router as soak_router
from api.system_state import router as system_state_router
from api.telemetry_history import router as telemetry_history_router
from api.workloads import router as workloads_router
from fastapi import FastAPI

# Attach a console handler so worker/lifespan messages are visible alongside
# uvicorn logs.  We avoid basicConfig(force=True) because it would overwrite
# uvicorn's carefully-configured formatters.
_console = logging.StreamHandler()
_console.setFormatter(
    logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
)
for _mod in ("api.main", "api.worker"):
    _l = logging.getLogger(_mod)
    _l.setLevel(logging.INFO)
    _l.addHandler(_console)
    _l.propagate = False

logger = logging.getLogger(__name__)

_worker_task: asyncio.Task | None = None
_bootstrap_task: asyncio.Task | None = None
_workload_task: asyncio.Task | None = None
_reconciler_task: asyncio.Task | None = None
_telemetry_collector_task: asyncio.Task | None = None
_probe_scheduler_task: asyncio.Task | None = None
_fault_controller_task: asyncio.Task | None = None
_intent_assurance_task: asyncio.Task | None = None
_llm_readiness_task: asyncio.Task | None = None
_production_restart_recovery_task: asyncio.Task | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start the intent worker as a background task alongside the API server.

    Previously the worker ran as a separate process (``python -m api.worker``),
    which was easy to forget.  Binding it to the app lifespan ensures that
    intent submission always has a consumer.

    An independent stale-recovery loop runs even if the worker task crashes.
    """
    global _worker_task, _bootstrap_task, _workload_task, _reconciler_task, _telemetry_collector_task, _probe_scheduler_task, _fault_controller_task, _intent_assurance_task, _llm_readiness_task, _production_restart_recovery_task

    # ── Shared shutdown event ───────────────────────────────────────────
    _shutdown = asyncio.Event()

    # ── Independent stale-job recovery (survives worker crashes) ────────
    async def _stale_recovery_loop():
        stale_interval = float(os.getenv("IDN_STALE_CHECK_INTERVAL_SECONDS", "60"))
        while not _shutdown.is_set():
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=stale_interval)
                break  # shut down
            except asyncio.TimeoutError:
                pass  # time to check
            try:
                from api.store import get_store

                n = get_store().recover_stale_jobs()
                if n:
                    logger.info("API stale-recovery requeued %d job(s)", n)
            except Exception:
                logger.exception("API stale-recovery check failed")

    _stale_task = asyncio.create_task(_stale_recovery_loop())

    # Reconcile only stale persisted control-plane projections before work is
    # accepted. This is idempotent and never invokes an agent or data plane.
    # Projection replay can change a business lifecycle.  It is therefore an
    # explicit maintenance action, never a side effect of a container restart.
    if os.getenv("IDN_ENABLE_STARTUP_PROJECTION_REPAIR", "false").lower() == "true":
        try:
            from services.intent_projection_repair import IntentProjectionRepairService
            projection_result = IntentProjectionRepairService().repair()
            if projection_result["repaired"]:
                logger.info("Startup projection reconciliation: %s", projection_result["repaired"])
        except Exception:
            logger.exception("Startup projection reconciliation failed")

    # ── Startup ──────────────────────────────────────────────────────
    if os.getenv("IDN_EMBEDDED_WORKER_ENABLED", "true").lower() == "true":
        try:
            from api.worker import run_worker

            _worker_task = asyncio.create_task(run_worker(poll_seconds=1.0))
            logger.info("Intent worker started (background task).")
        except Exception as exc:
            logger.warning("Intent worker failed to start: %s", exc)
    else:
        logger.info(
            "Embedded worker disabled (IDN_EMBEDDED_WORKER_ENABLED=false); "
            "relying on external idn-worker container."
        )

    # Config validation
    try:
        from infra.config import ConfigManager

        for warning in ConfigManager.validate():
            logger.warning("Config: %s", warning)
    except Exception:
        pass

    if os.getenv("IDN_LLM_STARTUP_PROBE_ENABLED", "false").lower() == "true":
        try:
            from agents.llm_client import probe_llm_readiness

            _llm_readiness_task = asyncio.create_task(
                probe_llm_readiness(), name="llm-readiness"
            )
        except Exception:
            logger.warning("LLM readiness probe could not be scheduled")

    # Generation-aware recovery installs baseline only after a data/control
    # plane reset; ordinary backend restarts preserve the live network.
    if os.getenv("IDN_AUTO_BOOTSTRAP_CONNECTIVITY", "false").lower() == "true":
        try:
            from infra.recovery_manager import get_recovery_manager

            _bootstrap_task = asyncio.create_task(
                get_recovery_manager().run_loop(_shutdown),
                name="restart-recovery",
            )
        except Exception as exc:
            logger.warning("Restart recovery failed to start: %s", exc)

    if os.getenv("IDN_ENABLE_STARTUP_TRAFFIC_ORCHESTRATION", "false").lower() == "true":
        try:
            from orchestrator.traffic_orchestrator import get_traffic_orchestrator

            _workload_task = asyncio.create_task(
                get_traffic_orchestrator().run_loop(_shutdown),
                name="traffic-orchestrator",
            )
            logger.info("Traffic orchestrator started.")
        except Exception:
            logger.exception("Traffic orchestrator failed to start")

    if os.getenv("IDN_NETWORK_RECONCILER_ENABLED", "true").lower() == "true":
        try:
            from infra.network_reconciler import get_network_reconciler

            _reconciler_task = asyncio.create_task(
                get_network_reconciler().run_loop(_shutdown),
                name="network-reconciler",
            )
            logger.info("Network reconciler started.")
        except Exception:
            logger.exception("Network reconciler failed to start")

    # This is deliberately separate from Formal Assurance. It reads desired
    # state, reconciles and readbacks only; it never schedules performance work.
    if os.getenv("IDN_PRODUCTION_RESTART_RECOVERY_ENABLED", "true").lower() == "true":
        try:
            from infra.production_restart_recovery import get_production_restart_recovery_worker
            _production_restart_recovery_task = asyncio.create_task(
                get_production_restart_recovery_worker().run_startup_recovery(),
                name="production-restart-recovery",
            )
            logger.info("Production restart recovery scheduled.")
        except Exception:
            logger.exception("Production restart recovery failed to start")

    if os.getenv("IDN_CONTINUOUS_TELEMETRY_ENABLED", "true").lower() == "true":
        try:
            from infra.continuous_telemetry import get_continuous_telemetry_collector

            _telemetry_collector_task = asyncio.create_task(
                get_continuous_telemetry_collector().run_loop(_shutdown),
                name="continuous-telemetry",
            )
            logger.info("Continuous interface telemetry started.")
        except Exception:
            logger.exception("Continuous telemetry failed to start")

    if os.getenv("IDN_PROBE_SCHEDULER_ENABLED", "true").lower() == "true":
        try:
            from infra.probe_scheduler import get_probe_scheduler

            _probe_scheduler_task = asyncio.create_task(
                get_probe_scheduler().run_loop(_shutdown),
                name="probe-scheduler",
            )
            logger.info("Probe scheduler started.")
        except Exception:
            logger.exception("Probe scheduler failed to start")

    if os.getenv("IDN_FAULT_CONTROLLER_ENABLED", "true").lower() == "true":
        try:
            from infra.fault_controller import get_fault_controller

            _fault_controller_task = asyncio.create_task(
                get_fault_controller().run_loop(_shutdown),
                name="fault-controller",
            )
            logger.info("Fault controller started.")
        except Exception:
            logger.exception("Fault controller failed to start")

    if os.getenv("IDN_INTENT_ASSURANCE_ENABLED", "true").lower() == "true":
        try:
            from orchestrator.intent_control_plane import get_intent_control_plane

            _intent_assurance_task = asyncio.create_task(
                get_intent_control_plane().run_loop(_shutdown),
                name="intent-assurance",
            )
            logger.info("Intent assurance loop started.")
        except Exception:
            logger.exception("Intent assurance loop failed to start")

    yield  # ── application runs here ──

    # ── Shutdown ─────────────────────────────────────────────────────
    _shutdown.set()
    if _workload_task is not None:
        _workload_task.cancel()
        try:
            await _workload_task
        except asyncio.CancelledError:
            pass
    if _reconciler_task is not None:
        _reconciler_task.cancel()
        try:
            await _reconciler_task
        except asyncio.CancelledError:
            pass
    if _production_restart_recovery_task is not None:
        _production_restart_recovery_task.cancel()
        try:
            await _production_restart_recovery_task
        except asyncio.CancelledError:
            pass
    if _telemetry_collector_task is not None:
        _telemetry_collector_task.cancel()
        try:
            await _telemetry_collector_task
        except asyncio.CancelledError:
            pass
    if _probe_scheduler_task is not None:
        _probe_scheduler_task.cancel()
        try:
            await _probe_scheduler_task
        except asyncio.CancelledError:
            pass
    if _fault_controller_task is not None:
        _fault_controller_task.cancel()
        try:
            await _fault_controller_task
        except asyncio.CancelledError:
            pass
    if _intent_assurance_task is not None:
        _intent_assurance_task.cancel()
        try:
            await _intent_assurance_task
        except asyncio.CancelledError:
            pass
    if _llm_readiness_task is not None and not _llm_readiness_task.done():
        _llm_readiness_task.cancel()
        try:
            await _llm_readiness_task
        except asyncio.CancelledError:
            pass
    if _bootstrap_task is not None and not _bootstrap_task.done():
        _bootstrap_task.cancel()
        try:
            await _bootstrap_task
        except asyncio.CancelledError:
            pass
    try:
        from api.telemetry import close_telemetry_tasks

        await close_telemetry_tasks()
    except Exception:
        logger.exception("Failed to drain telemetry tasks during shutdown")
    # 关闭全局 Checkpointer 连接（应用级别清理）
    try:
        from orchestrator.checkpointer import close_checkpointer

        await close_checkpointer()
    except Exception:
        pass
    # 关闭 ONOS 持久 HTTP 会话（连接池 + 熔断器）
    try:
        from mcp_servers.onos_server import onos_client

        await onos_client.close()
    except Exception:
        pass
    # 关闭 Mininet probe 持久 HTTP 会话
    try:
        from api._probe_session import close_probe_session

        await close_probe_session()
    except Exception:
        pass
    if _stale_task is not None:
        _stale_task.cancel()
        try:
            await _stale_task
        except asyncio.CancelledError:
            pass
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        logger.info("Intent worker stopped.")


def create_app() -> FastAPI:
    app = FastAPI(title="IDN-System API", version="0.1.0", lifespan=_lifespan)

    # ── Trace ID middleware — inject trace_id into every request ──
    @app.middleware("http")
    async def _trace_middleware(request, call_next):
        import uuid

        trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    # ── CORS — restrict to frontend origins ──
    from fastapi.middleware.cors import CORSMiddleware

    cors_origins = os.getenv(
        "IDN_CORS_ORIGINS", "http://localhost:5173,http://localhost:3000"
    ).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*", "X-API-Key", "X-Trace-Id"],
    )

    # ── Demo Auth middleware — minimal API key check ──
    @app.middleware("http")
    async def _demo_auth_middleware(request, call_next):
        path = request.url.path
        if path.startswith("/api/v1/auth/"):
            return await call_next(request)
        # When operator auth is explicitly enabled, browser/API access must
        # carry the HttpOnly server-issued session.  Readiness/OpenAPI remain
        # public operational probes; no mutating route is exempt.
        if auth_enabled() and path.startswith("/api/v1") and path not in {"/api/v1/health", "/api/v1/health/ready", "/api/v1/topology/current"}:
            if not session_from_request(request):
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=401, content={"error": {"error_type": "AUTH", "message": "Operator session is required", "reason_code": "AUTH_SESSION_REQUIRED", "trace_id": request.headers.get("X-Trace-Id", ""), "retryable": False}})
        api_key = os.getenv("IDN_API_KEY", "demo-key-xxx")
        # Only check destructive operations
        is_destructive = request.method in ("POST", "PUT", "DELETE", "PATCH")
        if is_destructive:
            auth = request.headers.get("Authorization", "")
            api_key_header = request.headers.get("X-API-Key", "")
            if auth.startswith("Bearer "):
                provided_key = auth.removeprefix("Bearer ")
            elif api_key_header:
                provided_key = api_key_header
            else:
                provided_key = ""
            if provided_key != api_key:
                logger.warning(
                    "Destructive %s %s rejected — missing or invalid API key",
                    request.method,
                    request.url.path,
                )
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "error_type": "VALIDATION",
                            "message": "Missing or invalid API key",
                            "reason_code": "API_KEY_INVALID",
                            "trace_id": request.headers.get("X-Trace-Id", ""),
                            "retryable": False,
                        }
                    },
                )
        return await call_next(request)

    # ── Global exception handler — maps Python exceptions to ErrorInfo ──
    from api.idempotency import IdempotencyConflictError
    from fastapi import HTTPException
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from models.agent_state import ErrorInfo
    from models.intent_lifecycle_state_machine import IntentStateTransitionError

    def _contract_error(request, *, error_type: str, message: str, reason_code: str, retryable: bool) -> dict:
        return {
            "error": {
                "error_type": error_type,
                "message": message,
                "reason_code": reason_code,
                "trace_id": request.headers.get("X-Trace-Id", ""),
                "retryable": retryable,
            }
        }

    @app.exception_handler(HTTPException)
    async def _http_error_handler(request, exc):
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        message = str(detail.get("message") or exc.detail or "Request failed")
        error_type = str(detail.get("error_type") or ("NOT_FOUND" if exc.status_code == 404 else "VALIDATION" if exc.status_code == 422 else "CONFLICT" if exc.status_code == 409 else "REQUEST_ERROR"))
        return JSONResponse(
            status_code=exc.status_code,
            content=_contract_error(
                request,
                error_type=error_type,
                message=message,
                reason_code=str(detail.get("reason_code") or detail.get("code") or f"HTTP_{exc.status_code}"),
                retryable=bool(detail.get("retryable", exc.status_code >= 500)),
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error_handler(request, exc):
        return JSONResponse(
            status_code=422,
            content=_contract_error(
                request,
                error_type="VALIDATION",
                message="Request validation failed",
                reason_code="REQUEST_VALIDATION_FAILED",
                retryable=False,
            ),
        )

    @app.exception_handler(IntentStateTransitionError)
    async def _intent_transition_error_handler(request, exc):
        detail = exc.as_dict()
        return JSONResponse(status_code=409, content=_contract_error(request, error_type="CONFLICT", message=str(detail.get("message") or "Intent state transition conflict"), reason_code=str(detail.get("code") or "INTENT_STATE_CONFLICT"), retryable=False))

    @app.exception_handler(IdempotencyConflictError)
    async def _idempotency_conflict_handler(request, exc):
        detail = exc.as_dict()
        return JSONResponse(status_code=409, content=_contract_error(request, error_type="CONFLICT", message=str(detail.get("message") or "Idempotency conflict"), reason_code=str(detail.get("code") or "IDEMPOTENCY_CONFLICT"), retryable=False))

    @app.exception_handler(Exception)
    async def _global_error_handler(request, exc):
        logger.exception(
            "Unhandled exception in request %s %s", request.method, request.url
        )
        error = ErrorInfo.from_exception(exc)
        status_map = {
            "TIMEOUT": 504,
            "NETWORK": 502,
            "LLM": 502,
            "VALIDATION": 422,
            "INTERNAL": 500,
        }
        return JSONResponse(
            status_code=status_map.get(error.error_type.value, 500),
            content=_contract_error(
                request,
                error_type=error.error_type.value,
                message=error.message,
                reason_code=error.error_code,
                retryable=error.retryable,
            ),
        )

    app.include_router(auth_router)
    app.include_router(router)
    app.include_router(workloads_router)
    app.include_router(resources_router)
    app.include_router(reconciliation_router)
    app.include_router(telemetry_history_router)
    app.include_router(probes_router)
    app.include_router(diagnostics_router)
    app.include_router(faults_router)
    app.include_router(recovery_router)
    app.include_router(restart_readback_router)
    app.include_router(soak_router)
    app.include_router(system_state_router)
    app.include_router(intent_control_router)
    return app


app = create_app()

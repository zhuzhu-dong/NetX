"""Budgeted active-probe scheduling driven by service state and events."""
from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from api.service_profiles import get_service_profile
from api.store import IntentStore, get_store
from orchestrator.traffic_orchestrator import get_traffic_orchestrator
from persistence.telemetry_store import TelemetryStore


@dataclass
class ProbeRequest:
    service_id: str
    reason: str
    heavy: bool
    created_monotonic: float


class ProbeBudget:
    def __init__(self, capacity_per_minute: float):
        self.capacity = max(float(capacity_per_minute), 1.0)
        self.events: deque[tuple[float, float]] = deque()

    def _trim(self, now: float) -> None:
        while self.events and now - self.events[0][0] >= 60.0:
            self.events.popleft()

    def available(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self._trim(now)
        return max(0.0, self.capacity - sum(cost for _, cost in self.events))

    def consume(self, cost: float, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.available(now) < cost:
            return False
        self.events.append((now, cost))
        return True


class ProbeScheduler:
    def __init__(
        self, *, store: IntentStore | None = None,
        light_probe: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        heavy_probe: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        budget_per_minute: float | None = None,
    ):
        self.store = store or get_store()
        self.telemetry = TelemetryStore(self.store)
        self.light_probe = light_probe or self._default_light_probe
        self.heavy_probe = heavy_probe or self._default_heavy_probe
        self.budget = ProbeBudget(
            budget_per_minute if budget_per_minute is not None
            else float(os.getenv("IDN_PROBE_BUDGET_UNITS_PER_MINUTE", "30"))
        )
        self.light_cost = float(os.getenv("IDN_PROBE_LIGHT_COST", "1"))
        self.heavy_cost = float(os.getenv("IDN_PROBE_HEAVY_COST", "10"))
        self._pending: deque[ProbeRequest] = deque()
        self._last_light: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def notify(self, service_id: str, reason: str, *, heavy: bool = True) -> None:
        get_service_profile(service_id)
        key = (service_id, reason, heavy)
        if not any((item.service_id, item.reason, item.heavy) == key for item in self._pending):
            self._pending.append(ProbeRequest(service_id, reason, heavy, time.monotonic()))
            self.store.add_audit_event(
                event_type="PROBE_SCHEDULED", resource_type="service", resource_id=service_id,
                correlation={"traffic_role": "VALIDATION" if heavy else "HEALTH_PROBE"},
                reason=reason, result={"status": "queued"},
            )

    async def _default_light_probe(self, service_id: str) -> dict[str, Any]:
        from api._probe_session import get_probe_session

        profile = get_service_profile(service_id)
        probe = await get_probe_session()
        result = await probe.get("/probe/ping", {
            "src": profile.src_host, "dst": profile.dst_host, "count": 1,
        })
        payload = {
            "service_id": service_id, "traffic_role": "HEALTH_PROBE",
            "measurement_source": "scheduled_mininet_ping", **(result or {}),
        }
        self.telemetry.record_service_metric(payload, traffic_role="HEALTH_PROBE")
        return payload

    async def _default_heavy_probe(self, service_id: str) -> dict[str, Any]:
        from api.service_validation import run_service_validation

        return await run_service_validation(service_id)

    def _due_normal_light_probes(self) -> None:
        interval = max(float(os.getenv("IDN_PROBE_NORMAL_LIGHT_INTERVAL_SECONDS", "30")), 5.0)
        now = time.monotonic()
        running_service_ids = {
            item.service_id for item in get_traffic_orchestrator().list(lifecycle_state="RUNNING")
            if item.traffic_role.value in {"PRODUCTION", "BACKGROUND"}
        }
        for service_id in sorted(running_service_ids):
            if now - self._last_light.get(service_id, 0.0) >= interval:
                self.notify(service_id, "NORMAL_HEALTH_INTERVAL", heavy=False)

    async def run_once(self) -> dict[str, Any]:
        async with self._lock:
            self._due_normal_light_probes()
            executed, deferred, failed = [], [], []
            initial_count = len(self._pending)
            for _ in range(initial_count):
                request = self._pending.popleft()
                cost = self.heavy_cost if request.heavy else self.light_cost
                if not self.budget.consume(cost):
                    self._pending.append(request)
                    deferred.append(request.service_id)
                    continue
                try:
                    result = await (self.heavy_probe if request.heavy else self.light_probe)(request.service_id)
                    if not request.heavy:
                        self._last_light[request.service_id] = time.monotonic()
                    executed.append({
                        "service_id": request.service_id, "reason": request.reason,
                        "probe_type": "HEAVY_VALIDATION" if request.heavy else "LIGHT_HEALTH",
                        "status": result.get("status"),
                    })
                    self.store.add_audit_event(
                        event_type="PROBE_EXECUTED", resource_type="service",
                        resource_id=request.service_id,
                        correlation={"traffic_role": "VALIDATION" if request.heavy else "HEALTH_PROBE"},
                        action={"probe_type": executed[-1]["probe_type"]}, result=result,
                        reason=request.reason,
                    )
                except Exception as exc:
                    failed.append({"service_id": request.service_id, "error": str(exc)})
            return {
                "status": "ok" if not failed else "partial",
                "executed": executed, "deferred": deferred, "failed": failed,
                "pending": len(self._pending), "budget_available": self.budget.available(),
            }

    def status(self) -> dict[str, Any]:
        return {
            "pending": [item.__dict__ for item in self._pending],
            "budget_capacity_per_minute": self.budget.capacity,
            "budget_available": self.budget.available(),
            "light_cost": self.light_cost, "heavy_cost": self.heavy_cost,
            "last_light": dict(self._last_light),
        }

    async def run_loop(self, shutdown: asyncio.Event) -> None:
        interval = max(float(os.getenv("IDN_PROBE_SCHEDULER_INTERVAL_SECONDS", "5")), 1.0)
        while not shutdown.is_set():
            try:
                await self.run_once()
            except Exception:
                pass
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


_scheduler: ProbeScheduler | None = None


def get_probe_scheduler() -> ProbeScheduler:
    global _scheduler
    if _scheduler is None or _scheduler.store is not get_store():
        _scheduler = ProbeScheduler(store=get_store())
    return _scheduler

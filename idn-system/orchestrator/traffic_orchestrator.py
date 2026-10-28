"""Long-running, intent-independent traffic workload orchestration."""
from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from api._probe_session import get_probe_session
from api.service_profiles import get_service_profile
from api.store import IntentStore, get_store
from models.traffic_workload import (
    RecoveryPolicy,
    TrafficWorkload,
    TrafficWorkloadCreate,
    TrafficWorkloadPatch,
    WorkloadLifecycle,
    WorkloadStartMode,
    ensure_workload_transition,
)

logger = logging.getLogger(__name__)


class TrafficGeneratorError(RuntimeError):
    pass


class TrafficGenerator(ABC):
    """Runtime adapter; workload policy is deliberately outside generators."""

    @abstractmethod
    async def start(self, workload: TrafficWorkload) -> dict[str, Any]: ...

    @abstractmethod
    async def stop(self, workload_id: str) -> dict[str, Any]: ...

    @abstractmethod
    async def update(self, workload: TrafficWorkload) -> dict[str, Any]: ...

    @abstractmethod
    async def inspect(self, workload_id: str) -> dict[str, Any]: ...

    @abstractmethod
    async def list_runtime(self) -> list[dict[str, Any]]: ...


class MininetTrafficGenerator(TrafficGenerator):
    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        probe = await get_probe_session()
        result = await probe.post(path, payload)
        if not result or result.get("status") not in {"ok", "not_found"}:
            raise TrafficGeneratorError(str((result or {}).get("error") or "Mininet runtime unavailable"))
        return result

    async def start(self, workload: TrafficWorkload) -> dict[str, Any]:
        return await self._post("/workloads/start", workload.model_dump(mode="json"))

    async def stop(self, workload_id: str) -> dict[str, Any]:
        return await self._post("/workloads/stop", {"workload_id": workload_id})

    async def update(self, workload: TrafficWorkload) -> dict[str, Any]:
        return await self._post("/workloads/update", workload.model_dump(mode="json"))

    async def inspect(self, workload_id: str) -> dict[str, Any]:
        probe = await get_probe_session()
        result = await probe.get("/workloads", {"workload_id": workload_id})
        if not result:
            raise TrafficGeneratorError("Mininet runtime unavailable")
        return result

    async def list_runtime(self) -> list[dict[str, Any]]:
        probe = await get_probe_session()
        result = await probe.get("/workloads")
        if not result or result.get("status") != "ok":
            raise TrafficGeneratorError(str((result or {}).get("error") or "Mininet runtime unavailable"))
        return list(result.get("workloads") or [])


class TrafficOrchestrator:
    def __init__(
        self, store: IntentStore | None = None, generator: TrafficGenerator | None = None,
    ):
        self.store = store or get_store()
        self.generator = generator or MininetTrafficGenerator()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, workload_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(workload_id, asyncio.Lock())

    @staticmethod
    def from_service_profile(
        service_id: str, *, workload_id: str | None = None, **overrides: Any,
    ) -> TrafficWorkloadCreate:
        profile = get_service_profile(service_id)
        payload: dict[str, Any] = {
            "service_id": profile.service_id,
            "src_endpoint": profile.src_host,
            "dst_endpoint": profile.dst_host,
            "selector": {
                "source_ip": profile.src_ip,
                "destination_ip": profile.dst_ip,
                "protocol": profile.protocol.value,
                "source_port": profile.src_port,
                "destination_port": profile.dst_port,
                "dscp": profile.dscp,
                "vlan_id": profile.vlan_id,
            },
            "traffic_pattern": (
                "BURSTY" if profile.traffic_pattern.value == "BURST" else profile.traffic_pattern.value
            ),
            "offered_rate_mbps": profile.default_offered_rate_mbps,
            "packet_size_bytes": profile.packet_size_bytes,
            "parallel_flows": profile.parallel_streams,
            "duration_seconds": None,
            "tenant_id": profile.tenant_id,
            "labels": {"service_name": profile.service_name},
        }
        if workload_id:
            payload["workload_id"] = workload_id
        payload.update({key: value for key, value in overrides.items() if value is not None})
        return TrafficWorkloadCreate(**payload)

    async def create(self, request: TrafficWorkloadCreate) -> TrafficWorkload:
        # Service existence is a configuration invariant, not a scenario branch.
        get_service_profile(request.service_id)
        workload = TrafficWorkload.from_create(request)
        record = self.store.create_workload(workload.model_dump(mode="json"))
        self.store.add_audit_event(
            event_type="WORKLOAD_CREATED", resource_type="workload",
            resource_id=workload.workload_id, correlation={"service_id": workload.service_id},
            expected={"desired_state": workload.desired_state.value}, result={"status": "created"},
        )
        created = TrafficWorkload(**record)
        if request.start_mode == WorkloadStartMode.IMMEDIATE:
            return await self.start(created.workload_id)
        return created

    def get(self, workload_id: str) -> TrafficWorkload | None:
        record = self.store.get_workload(workload_id)
        return TrafficWorkload(**record) if record else None

    def list(
        self, *, service_id: str | None = None, lifecycle_state: str | None = None,
        traffic_role: str | None = None,
    ) -> list[TrafficWorkload]:
        return [TrafficWorkload(**item) for item in self.store.list_workloads(
            service_id=service_id, lifecycle_state=lifecycle_state, traffic_role=traffic_role,
        )]

    def _persist(self, workload: TrafficWorkload, **changes: Any) -> TrafficWorkload:
        updated = self.store.update_workload(
            workload.workload_id, expected_version=workload.version, **changes,
        )
        if updated is None:
            raise KeyError(workload.workload_id)
        return TrafficWorkload(**updated)

    async def start(self, workload_id: str, *, recovery: bool = False) -> TrafficWorkload:
        lock = await self._lock_for(workload_id)
        async with lock:
            return await self._start_locked(workload_id, recovery=recovery)

    async def _start_locked(self, workload_id: str, *, recovery: bool = False) -> TrafficWorkload:
        """Start while the caller owns this workload's lifecycle lock."""
        workload = self.get(workload_id)
        if workload is None:
            raise KeyError(workload_id)
        if workload.lifecycle_state == WorkloadLifecycle.RUNNING and not recovery:
            return workload
        if workload.lifecycle_state == WorkloadLifecycle.RUNNING and recovery:
            # Observed runtime disappeared while desired state stayed RUNNING.
            # Record the mismatch before entering the normal retry transition.
            workload = self._persist(
                workload, lifecycle_state="FAILED", desired_state="RUNNING",
                last_error="observed runtime missing",
                event_data={"reason": "observed_runtime_missing"},
            )
        ensure_workload_transition(workload.lifecycle_state, WorkloadLifecycle.STARTING)
        workload = self._persist(
            workload, lifecycle_state="STARTING", desired_state="RUNNING", last_error=None,
            event_data={"recovery": recovery},
        )
        try:
            runtime = await self.generator.start(workload)
            observed = dict(runtime.get("workload") or runtime)
            workload = self._persist(
                workload, lifecycle_state="RUNNING", desired_state="RUNNING",
                process_refs=observed.get("process_refs") or [],
                statistics=observed.get("statistics") or {},
                started_at=observed.get("started_at") or datetime.now(timezone.utc).isoformat(),
                stopped_at=None,
            )
            self.store.add_audit_event(
                event_type="WORKLOAD_RECOVERED" if recovery else "WORKLOAD_STARTED",
                resource_type="workload", resource_id=workload_id,
                correlation={"service_id": workload.service_id},
                expected={"state": "RUNNING"}, observed=observed,
                action={"generator": type(self.generator).__name__}, result={"status": "ok"},
            )
            return workload
        except Exception as exc:
            self._persist(
                workload, lifecycle_state="FAILED", desired_state="RUNNING",
                last_error=str(exc), process_refs=[],
            )
            self.store.add_audit_event(
                event_type="WORKLOAD_START_FAILED", resource_type="workload",
                resource_id=workload_id, expected={"state": "RUNNING"},
                observed={"state": "FAILED"}, result={"status": "error"}, reason=str(exc),
            )
            raise TrafficGeneratorError(str(exc)) from exc

    async def stop(self, workload_id: str) -> TrafficWorkload:
        lock = await self._lock_for(workload_id)
        async with lock:
            workload = self.get(workload_id)
            if workload is None:
                raise KeyError(workload_id)
            if workload.lifecycle_state in {WorkloadLifecycle.CREATED, WorkloadLifecycle.SCHEDULED}:
                ensure_workload_transition(workload.lifecycle_state, WorkloadLifecycle.STOPPED)
                return self._persist(
                    workload, lifecycle_state="STOPPED", desired_state="STOPPED",
                    stopped_at=datetime.now(timezone.utc).isoformat(),
                )
            if workload.lifecycle_state == WorkloadLifecycle.STOPPED:
                return workload
            if workload.lifecycle_state == WorkloadLifecycle.FAILED:
                # A failed start may still have created a partial server or
                # client process.  FAILED transitions directly to STOPPED in
                # the lifecycle model, so perform an idempotent runtime stop
                # before clearing the durable process references.
                try:
                    runtime = await self.generator.stop(workload_id)
                    observed = dict(runtime.get("workload") or runtime)
                    stopped = self._persist(
                        workload, lifecycle_state="STOPPED", desired_state="STOPPED",
                        process_refs=[], statistics=observed.get("statistics") or workload.statistics,
                        stopped_at=observed.get("stopped_at") or datetime.now(timezone.utc).isoformat(),
                        last_error=None,
                    )
                    self.store.add_audit_event(
                        event_type="WORKLOAD_STOPPED", resource_type="workload",
                        resource_id=workload_id, expected={"state": "STOPPED"},
                        observed=observed, result={"status": "ok", "from_state": "FAILED"},
                    )
                    return stopped
                except Exception as exc:
                    self._persist(workload, lifecycle_state="FAILED", last_error=str(exc))
                    raise TrafficGeneratorError(str(exc)) from exc
            ensure_workload_transition(workload.lifecycle_state, WorkloadLifecycle.STOPPING)
            workload = self._persist(workload, lifecycle_state="STOPPING", desired_state="STOPPED")
            try:
                runtime = await self.generator.stop(workload_id)
                observed = dict(runtime.get("workload") or runtime)
                stopped = self._persist(
                    workload, lifecycle_state="STOPPED", desired_state="STOPPED",
                    process_refs=[], statistics=observed.get("statistics") or workload.statistics,
                    stopped_at=observed.get("stopped_at") or datetime.now(timezone.utc).isoformat(),
                    last_error=None,
                )
                self.store.add_audit_event(
                    event_type="WORKLOAD_STOPPED", resource_type="workload",
                    resource_id=workload_id, expected={"state": "STOPPED"},
                    observed=observed, result={"status": "ok"},
                )
                return stopped
            except Exception as exc:
                failed = self._persist(workload, lifecycle_state="FAILED", last_error=str(exc))
                raise TrafficGeneratorError(str(exc)) from exc

    async def patch(self, workload_id: str, patch: TrafficWorkloadPatch) -> TrafficWorkload:
        lock = await self._lock_for(workload_id)
        async with lock:
            workload = self.get(workload_id)
            if workload is None:
                raise KeyError(workload_id)
            changes = patch.model_dump(exclude_unset=True, mode="json")
            if not changes:
                return workload
            candidate = TrafficWorkload(**{**workload.model_dump(mode="json"), **changes})
            if workload.lifecycle_state in {WorkloadLifecycle.RUNNING, WorkloadLifecycle.DEGRADED}:
                runtime = await self.generator.update(candidate)
                observed = dict(runtime.get("workload") or runtime)
                changes.update({
                    "process_refs": observed.get("process_refs") or [],
                    "statistics": observed.get("statistics") or workload.statistics,
                    "started_at": observed.get("started_at") or workload.started_at,
                    "lifecycle_state": "RUNNING",
                })
            updated = self._persist(workload, **changes)
            self.store.add_audit_event(
                event_type="WORKLOAD_UPDATED", resource_type="workload", resource_id=workload_id,
                expected=changes, result={"status": "ok"},
            )
            return updated

    async def delete(self, workload_id: str) -> None:
        workload = self.get(workload_id)
        if workload is None:
            raise KeyError(workload_id)
        if workload.lifecycle_state not in {WorkloadLifecycle.CREATED, WorkloadLifecycle.STOPPED}:
            await self.stop(workload_id)
        if not self.store.delete_workload(workload_id):
            raise KeyError(workload_id)
        self.store.add_audit_event(
            event_type="WORKLOAD_DELETED", resource_type="workload", resource_id=workload_id,
            result={"status": "deleted"},
        )

    async def reconcile(self) -> dict[str, Any]:
        runtime_items = {item["workload_id"]: item for item in await self.generator.list_runtime()}
        repaired, synchronized, orphaned, failed = [], [], [], []
        persisted = {item.workload_id: item for item in self.list()}
        for workload_id, runtime in runtime_items.items():
            if workload_id not in persisted:
                try:
                    await self.generator.stop(workload_id)
                    orphaned.append(workload_id)
                    self.store.add_audit_event(
                        event_type="ORPHAN_WORKLOAD_CLEANED", resource_type="workload",
                        resource_id=workload_id, observed=runtime, result={"status": "stopped"},
                    )
                except Exception as exc:
                    failed.append({"workload_id": workload_id, "error": str(exc)})
        for workload_id in persisted:
            runtime = runtime_items.get(workload_id)
            runtime_running = bool(runtime and runtime.get("lifecycle_state") == "RUNNING")
            lock = await self._lock_for(workload_id)
            async with lock:
                # The desired state may have changed while runtime inventory
                # was collected.  Re-read it under the same lock used by
                # start/stop so reconciliation cannot resurrect STOPPING or
                # STOPPED workloads or advance their optimistic version.
                workload = self.get(workload_id)
                if workload is None:
                    continue
                if workload.desired_state == WorkloadLifecycle.RUNNING and not runtime_running:
                    if workload.recovery_policy == RecoveryPolicy.AUTO_RESTART:
                        try:
                            await self._start_locked(workload_id, recovery=True)
                            repaired.append(workload_id)
                        except Exception as exc:
                            failed.append({"workload_id": workload_id, "error": str(exc)})
                    elif workload.lifecycle_state != WorkloadLifecycle.FAILED:
                        self._persist(
                            workload, lifecycle_state="FAILED",
                            last_error="desired RUNNING but runtime is missing; recovery is not automatic",
                        )
                elif (
                    runtime_running
                    and workload.desired_state == WorkloadLifecycle.RUNNING
                    and workload.lifecycle_state not in {
                        WorkloadLifecycle.STOPPING, WorkloadLifecycle.STOPPED,
                    }
                    and (
                        workload.lifecycle_state != WorkloadLifecycle.RUNNING
                        or workload.process_refs != (runtime.get("process_refs") or [])
                    )
                ):
                    self._persist(
                        workload, lifecycle_state="RUNNING",
                        process_refs=runtime.get("process_refs") or [],
                        statistics=runtime.get("statistics") or {}, last_error=None,
                    )
                    synchronized.append(workload_id)
        return {"status": "ok", "repaired": repaired, "synchronized": synchronized,
                "orphaned": orphaned, "failed": failed}

    async def run_loop(self, shutdown: asyncio.Event) -> None:
        interval = max(float(os.getenv("IDN_WORKLOAD_RECONCILE_SECONDS", "10")), 1.0)
        while not shutdown.is_set():
            now = datetime.now(timezone.utc)
            for workload in self.list(lifecycle_state="SCHEDULED"):
                start_time = workload.start_time
                if start_time and start_time <= now:
                    try:
                        await self.start(workload.workload_id)
                    except Exception:
                        logger.exception("Scheduled workload %s failed", workload.workload_id)
            try:
                await self.reconcile()
            except Exception:
                logger.exception("Workload reconciliation failed")
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


_orchestrator: TrafficOrchestrator | None = None


def get_traffic_orchestrator() -> TrafficOrchestrator:
    global _orchestrator
    current_store = get_store()
    if _orchestrator is None or _orchestrator.store is not current_store:
        _orchestrator = TrafficOrchestrator(store=current_store)
    return _orchestrator


def reset_traffic_orchestrator() -> None:
    global _orchestrator
    _orchestrator = None

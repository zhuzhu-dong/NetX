"""Bounded workload-churn soak runner with process/FD/task leak evidence."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from api.store import IntentStore, get_store
from models.traffic_workload import RecoveryPolicy, TrafficRole, WorkloadLifecycle


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fd_count() -> int | None:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


class SoakRunner:
    def __init__(self, *, store: IntentStore | None = None, workloads: Any = None,
                 reconciler: Any = None, probe: Any = None):
        self.store = store or get_store()
        self.workloads = workloads
        self.reconciler = reconciler
        self.probe = probe
        self._tasks: dict[str, asyncio.Task] = {}
        self._results: dict[str, dict[str, Any]] = {}

    async def _retire(self, workload_id: str) -> None:
        """Idempotently retire one workload owned by this soak run."""
        getter = getattr(self.workloads, "get", None)
        if getter is not None and getter(workload_id) is None:
            return
        stop_error: Exception | None = None
        try:
            await self.workloads.stop(workload_id)
        except KeyError:
            return
        except Exception as exc:
            stop_error = exc
        try:
            await self.workloads.delete(workload_id)
        except KeyError:
            return
        except Exception as exc:
            if stop_error is not None:
                raise RuntimeError(
                    f"workload cleanup failed: stop={stop_error}; delete={exc}"
                ) from exc
            raise

    @staticmethod
    def _server_selector(workload: Any) -> tuple[str | None, int | None]:
        selector = getattr(workload, "selector", None)
        if isinstance(workload, dict):
            destination = workload.get("dst_endpoint")
            selector = workload.get("selector") or {}
        else:
            destination = getattr(workload, "dst_endpoint", None)
        if isinstance(selector, dict):
            port = selector.get("destination_port")
        else:
            port = getattr(selector, "destination_port", None)
        return destination, port

    async def start(self, *, duration_seconds: float, service_ids: list[str],
                    churn_interval_seconds: float = 10.0) -> dict[str, Any]:
        if duration_seconds <= 0 or churn_interval_seconds <= 0:
            raise ValueError("soak durations must be positive")
        if not service_ids:
            raise ValueError("at least one configured service_id is required")
        run_id = f"SOAK-{uuid.uuid4().hex[:12]}"
        self._tasks[run_id] = asyncio.create_task(
            self._run(run_id, duration_seconds, service_ids, churn_interval_seconds),
            name=f"soak-{run_id}",
        )
        return {"run_id": run_id, "status": "RUNNING"}

    async def _run(self, run_id: str, duration: float, service_ids: list[str], interval: float) -> dict[str, Any]:
        if self.workloads is None:
            from orchestrator.traffic_orchestrator import get_traffic_orchestrator

            self.workloads = get_traffic_orchestrator()
        if self.reconciler is None:
            from infra.network_reconciler import get_network_reconciler

            self.reconciler = get_network_reconciler()
        started, loop = _now(), asyncio.get_running_loop()
        deadline = loop.time() + duration
        created: list[str] = []
        created_services: dict[str, str] = {}
        samples: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        baseline_fd, baseline_tasks = _fd_count(), len(asyncio.all_tasks())
        with self.store._lock, self.store._connect() as connection:
            connection.execute("INSERT INTO soak_runs VALUES (?, 'RUNNING', ?, '{}', ?, NULL)",
                               (run_id, duration, started))
        cycle = 0
        try:
            while loop.time() < deadline:
                service_id = service_ids[cycle % len(service_ids)]
                try:
                    request = self.workloads.from_service_profile(
                        service_id, workload_id=f"{run_id}-{cycle}", traffic_role=TrafficRole.PRODUCTION,
                        recovery_policy=RecoveryPolicy.AUTO_RESTART,
                    )
                    # A service profile owns a concrete transport selector.
                    # Retire only the previous workload created by this soak
                    # run before cycling that profile, otherwise two servers
                    # briefly contend for the same destination port.  External
                    # long-running production workloads are never considered.
                    previous = next(
                        (
                            workload_id for workload_id in created
                            if created_services.get(workload_id) == service_id
                        ),
                        None,
                    )
                    destination, port = self._server_selector(request)
                    conflict_ids: list[str] = []
                    if destination and port:
                        for current in self.workloads.list(lifecycle_state="RUNNING"):
                            current_id = str(getattr(current, "workload_id", ""))
                            if current_id == previous:
                                continue
                            if self._server_selector(current) == (destination, port):
                                conflict_ids.append(current_id)
                    if conflict_ids:
                        skipped.append({
                            "cycle": cycle, "service_id": service_id,
                            "reason": "WORKLOAD_SERVER_SELECTOR_IN_USE",
                            "destination": destination, "destination_port": port,
                            "conflicting_workload_ids": conflict_ids,
                            "timestamp": _now(),
                        })
                        cycle += 1
                        await asyncio.sleep(min(interval, max(0.0, deadline - loop.time())))
                        continue
                    if previous is not None:
                        await self._retire(previous)
                        created.remove(previous)
                        created_services.pop(previous, None)
                    candidate_id = request.workload_id
                    created.append(candidate_id)
                    created_services[candidate_id] = service_id
                    try:
                        workload = await self.workloads.create(request)
                    except Exception:
                        await self._retire(candidate_id)
                        created.remove(candidate_id)
                        created_services.pop(candidate_id, None)
                        raise
                    if workload.lifecycle_state != WorkloadLifecycle.RUNNING:
                        workload = await self.workloads.start(workload.workload_id)
                    # Keep several heterogeneous flows overlapping, then retire
                    # only the oldest one to exercise independent lifecycle.
                    if len(created) > max(2, len(service_ids)):
                        old = created.pop(0)
                        await self._retire(old)
                        created_services.pop(old, None)
                    reconciliation = await self.reconciler.run_once(auto_repair=True)
                    samples.append({
                        "timestamp": _now(), "cycle": cycle,
                        "running_workloads": len(self.workloads.list(lifecycle_state="RUNNING")),
                        "fd_count": _fd_count(), "asyncio_tasks": len(asyncio.all_tasks()),
                        "reconciliation_status": getattr(reconciliation, "status", None) or
                                                 (reconciliation.get("status") if isinstance(reconciliation, dict) else None),
                    })
                except Exception as exc:
                    errors.append({"cycle": cycle, "error": str(exc), "timestamp": _now()})
                cycle += 1
                await asyncio.sleep(min(interval, max(0.0, deadline - loop.time())))
        finally:
            for workload_id in list(created):
                try:
                    await self._retire(workload_id)
                    created_services.pop(workload_id, None)
                except Exception as exc:
                    errors.append({"cleanup": workload_id, "error": str(exc), "timestamp": _now()})
        # Allow completed HTTP requests and runtime process watchers to release
        # their descriptors before leak accounting.
        await asyncio.sleep(min(2.0, interval))
        final_fd, final_tasks = _fd_count(), len(asyncio.all_tasks())
        fd_growth = (final_fd - baseline_fd) if final_fd is not None and baseline_fd is not None else None
        task_growth = final_tasks - baseline_tasks
        status = (
            "PASSED"
            if samples and not errors and (fd_growth is None or fd_growth <= 3) and task_growth <= 3
            else "FAILED"
        )
        result = {
            "run_id": run_id, "status": status, "duration_seconds": duration,
            "cycles": cycle, "service_ids": service_ids, "samples": samples,
            "skipped": skipped, "errors": errors,
            "resources": {"fd_before": baseline_fd, "fd_after": final_fd, "fd_growth": fd_growth,
                          "tasks_before": baseline_tasks, "tasks_after": final_tasks, "task_growth": task_growth},
            "finished_at": _now(),
        }
        self._results[run_id] = result
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                "UPDATE soak_runs SET status=?, result_json=?, finished_at=? WHERE run_id=?",
                (status, json.dumps(result, default=str), result["finished_at"], run_id),
            )
        self.store.add_audit_event(
            event_type="SOAK_COMPLETED", resource_type="soak", resource_id=run_id,
            observed=result["resources"], result={"status": status, "cycles": cycle},
        )
        return result

    def status(self, run_id: str) -> dict[str, Any]:
        if run_id in self._results:
            return self._results[run_id]
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            return {"run_id": run_id, "status": "RUNNING"}
        with self.store._lock, self.store._connect() as connection:
            row = connection.execute("SELECT * FROM soak_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row["result_json"]) if row["result_json"] != "{}" else {"run_id": run_id, "status": row["status"]}


_runner: SoakRunner | None = None


def get_soak_runner() -> SoakRunner:
    global _runner
    current = get_store()
    if _runner is None or _runner.store is not current:
        _runner = SoakRunner(store=current)
    return _runner

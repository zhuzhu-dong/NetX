"""Passive interface/workload telemetry collector."""
from __future__ import annotations

import asyncio
import logging
import os

from api._probe_session import get_probe_session
from api.store import get_store
from orchestrator.traffic_orchestrator import get_traffic_orchestrator
from persistence.telemetry_store import TelemetryStore

logger = logging.getLogger(__name__)


class ContinuousTelemetryCollector:
    def __init__(self, store: TelemetryStore | None = None):
        self.store = store or TelemetryStore(get_store())

    async def collect_once(self) -> dict:
        probe = await get_probe_session()
        payload = await probe.get("/telemetry/interfaces")
        if not payload or payload.get("status") != "ok":
            return {"status": "unavailable", "samples": [], "error": (payload or {}).get("error")}
        samples = self.store.record_interface_batch(payload)
        self.store.record_generic("INTERFACE_BATCH", {
            "timestamp": payload.get("timestamp"), "sample_count": len(samples),
        }, "MEASURED" if samples else "NO_SAMPLE")
        orchestrator = get_traffic_orchestrator()
        try:
            runtime_index = {
                item["workload_id"]: item for item in await orchestrator.generator.list_runtime()
            }
        except Exception as exc:
            logger.warning("Workload runtime telemetry unavailable: %s", exc)
            runtime_index = {}
        for workload in orchestrator.list():
            sample = workload.model_dump(mode="json")
            runtime = runtime_index.get(workload.workload_id)
            if runtime:
                sample.update({
                    "lifecycle_state": runtime.get("lifecycle_state") or sample["lifecycle_state"],
                    "process_refs": runtime.get("process_refs") or sample.get("process_refs") or [],
                    "statistics": runtime.get("statistics") or {},
                    "runtime_observed": True,
                })
            else:
                sample["runtime_observed"] = False
            self.store.record_workload_metric(sample)
        return {"status": "ok", "samples": samples, "count": len(samples)}

    async def run_loop(self, shutdown: asyncio.Event) -> None:
        interval = max(float(os.getenv("IDN_INTERFACE_TELEMETRY_INTERVAL_SECONDS", "2")), 1.0)
        while not shutdown.is_set():
            try:
                await self.collect_once()
            except Exception:
                logger.exception("Continuous telemetry collection failed")
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


_collector: ContinuousTelemetryCollector | None = None


def get_continuous_telemetry_collector() -> ContinuousTelemetryCollector:
    global _collector
    if _collector is None or _collector.store.store is not get_store():
        _collector = ContinuousTelemetryCollector(TelemetryStore(get_store()))
    return _collector

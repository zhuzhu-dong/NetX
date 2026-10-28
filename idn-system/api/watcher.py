"""Background KPI watcher that turns sustained anomalies into internal intents."""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from api.store import get_store


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _incident_id() -> str:
    return f"INC-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


def _bool_env(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() == "true"


@dataclass
class WatcherConfig:
    api_base_url: str = os.getenv("IDN_API_BASE_URL", "http://localhost:8000/api/v1")
    enabled: bool = _bool_env("IDN_WATCHER_ENABLED", "true")

    light_interval_sec: float = float(os.getenv("IDN_WATCHER_LIGHT_INTERVAL_SEC", "5"))
    deep_every_n: int = int(os.getenv("IDN_WATCHER_DEEP_EVERY_N", "3"))
    iperf_on_suspect: bool = _bool_env("IDN_WATCHER_IPERF_ON_SUSPECT", "true")

    debounce_samples: int = int(os.getenv("IDN_WATCHER_DEBOUNCE_SAMPLES", "3"))
    cooldown_sec: int = int(os.getenv("IDN_WATCHER_COOLDOWN_SEC", "60"))

    max_latency_ms: float = float(os.getenv("IDN_WATCHER_MAX_LATENCY_MS", "10"))
    max_loss_rate: float = float(os.getenv("IDN_WATCHER_MAX_LOSS_RATE", "0.01"))
    min_throughput_mbps: float = float(os.getenv("IDN_WATCHER_MIN_THROUGHPUT_MBPS", "0"))
    throughput_requires_iperf: bool = _bool_env("IDN_WATCHER_THROUGHPUT_REQUIRES_IPERF", "true")

    queue_depth_bytes_threshold: int = int(os.getenv("IDN_WATCHER_QUEUE_DEPTH_BYTES", "65536"))
    queue_overlimits_delta_threshold: int = int(os.getenv("IDN_WATCHER_OVERLIMITS_DELTA", "10"))
    queue_drops_delta_threshold: int = int(os.getenv("IDN_WATCHER_DROPS_DELTA", "1"))

    # -- Monitored service (all selector values come from ServiceProfile) --
    service_id: str = os.getenv("IDN_WATCHER_SERVICE_ID", "")
    src_host: str = ""
    dst_host: str = ""
    src_ip: str = ""
    dst_ip: str = ""

    def __post_init__(self) -> None:
        if not self.service_id:
            self.enabled = False
            return
        from api.service_profiles import get_service_profile

        profile = get_service_profile(self.service_id)
        self.src_host = profile.src_host
        self.dst_host = profile.dst_host
        self.src_ip = profile.src_ip
        self.dst_ip = profile.dst_ip
        if self.min_throughput_mbps <= 0:
            configured = profile.validation_thresholds.get("minimum_received_mbps")
            self.min_throughput_mbps = float(configured or 0)


@dataclass
class WatcherState:
    state: str = "NORMAL"
    consecutive_bad: int = 0
    consecutive_errors: int = 0
    last_trigger_ts: float = 0
    healing_start_ts: float = 0
    last_kpi: dict[str, Any] | None = None
    active_incident_id: str | None = None
    last_triggered_intent_id: str | None = None
    healing_intent_id: str | None = None
    sample_seq: int = 0
    triggered_intents: list[str] = field(default_factory=list)


class KpiWatcher:
    """Poll telemetry, debounce anomalies, and create internal self-healing intents."""

    def __init__(self, config: WatcherConfig | None = None):
        self.config = config or WatcherConfig()
        self.state = WatcherState()
        self.store = get_store()
        # Self-referencing HTTP client for sync writes (set in run_forever)
        self._sync_client: httpx.AsyncClient | None = None

    async def _sync_write(self, **kwargs) -> None:
        """Persist watcher state via HTTP to avoid multi-writer SQLite contention.

        All writes from the watcher go through this single endpoint so the
        API process is the only writer to the database.
        """
        if self._sync_client is None:
            return
        try:
            await self._sync_client.post(
                f"{self.config.api_base_url}/watcher/_sync",
                json=kwargs,
            )
        except Exception:
            pass  # best-effort — watcher state is not critical

    async def run_forever(self) -> None:
        if not self.config.enabled:
            await self._persist_state("DISABLED", "watcher disabled by IDN_WATCHER_ENABLED")
            print("[watcher] disabled")
            return

        print(f"[watcher] started, api={self.config.api_base_url}")
        await self._persist_state("NORMAL", "watcher started")
        api_key = os.getenv("IDN_API_KEY", "demo-key-xxx")
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={"X-API-Key": api_key},
        ) as client:
            self._sync_client = client
            while True:
                try:
                    await self.tick(client)
                except Exception as exc:
                    self.state.consecutive_errors += 1
                    await self._persist_state("ERROR", f"tick failed: {exc!r}")
                    print(f"[watcher] tick failed: {exc!r}")
                await asyncio.sleep(self.config.light_interval_sec)

    async def tick(self, client: httpx.AsyncClient) -> dict[str, Any]:
        self.state.sample_seq += 1

        # ── HEALING_IN_PROGRESS: poll for completion, block new triggers ──
        if self.state.state == "HEALING_IN_PROGRESS":
            if self.state.healing_intent_id:
                try:
                    resp = await client.get(
                        f"{self.config.api_base_url}/intents/{self.state.healing_intent_id}"
                    )
                    resp.raise_for_status()
                    intent_data = resp.json()
                    healing_status = intent_data.get("status", "")
                    if healing_status in ("success", "failed", "cancelled"):
                        self.state.state = "NORMAL" if healing_status == "success" else "COOLDOWN"
                        self.state.healing_intent_id = None
                        self.state.healing_start_ts = 0
                        print(f"[watcher] healing completed: intent={self.state.last_triggered_intent_id} status={healing_status}")
                except Exception:
                    pass
            # Timeout: force exit HEALING_IN_PROGRESS after 300s
            if self.state.healing_start_ts and time.time() - self.state.healing_start_ts > 300:
                logger = __import__("logging").getLogger("api.watcher")
                logger.warning("Healing intent %s timed out after 300s — forcing exit", self.state.healing_intent_id)
                self.state.state = "COOLDOWN"
                self.state.healing_intent_id = None
                self.state.healing_start_ts = 0
            if self.state.state == "HEALING_IN_PROGRESS":
                await self._persist_state("HEALING_IN_PROGRESS", "waiting for healing completion")
                return {"status": "waiting_for_healing"}

        deep = self.state.sample_seq % max(1, self.config.deep_every_n) == 0

        telemetry = await self.collect_telemetry(
            client,
            include_probe=deep,
            include_iperf=False,
        )
        source = str(telemetry.get("source") or "")
        kpi = telemetry.get("kpi") or {}
        verdict = self.evaluate(kpi, telemetry_source=source)

        if verdict["bad"]:
            self.state.consecutive_bad += 1
            if self.state.consecutive_bad >= self.config.debounce_samples:
                if not self.state.active_incident_id:
                    self.state.active_incident_id = _incident_id()
                await self._sync_write(incident={
                    "incident_id": self.state.active_incident_id,
                    "status": "open",
                    "reason": verdict["reason"],
                    "kpi_snapshot": kpi,
                })
                if self.config.iperf_on_suspect:
                    telemetry = await self.collect_telemetry(
                        client,
                        include_probe=True,
                        include_iperf=True,
                    )
                    source = str(telemetry.get("source") or "")
                    kpi = telemetry.get("kpi") or {}
                    verdict = self.evaluate(kpi, telemetry_source=source)

                if not verdict["bad"]:
                    await self._resolve_active_incident(kpi, "KPI recovered during deep confirmation")
                elif self.can_trigger():
                    await self.trigger_healing_intent(client, kpi, verdict)
                    # State set to HEALING_IN_PROGRESS inside trigger_healing_intent
                else:
                    self.state.state = "COOLDOWN"
                    await self._sync_write(incident={
                        "incident_id": self.state.active_incident_id,
                        "status": "cooldown",
                        "reason": verdict["reason"],
                        "kpi_snapshot": kpi,
                    })
            else:
                self.state.state = "SUSPECT"
        else:
            await self._resolve_active_incident(kpi, "KPI recovered")

        await self._sync_write(sample={
            "incident_id": self.state.active_incident_id,
            "kpi_snapshot": kpi,
            "verdict": verdict,
        })
        # Also write sample via direct store for backward compat
        sample = self.store.record_watcher_sample(
            incident_id=self.state.active_incident_id,
            kpi_snapshot=kpi,
            verdict=verdict,
        )
        self.state.last_kpi = kpi
        await self._persist_state(self.state.state, verdict["reason"], sample_id=sample["sample_id"])
        print(
            "[watcher] sample",
            self.state.sample_seq,
            "state=", self.state.state,
            "bad=", verdict["bad"],
            "reason=", verdict["reason"],
        )
        return {"telemetry": telemetry, "verdict": verdict, "sample": sample}

    async def collect_telemetry(
        self,
        client: httpx.AsyncClient,
        *,
        include_probe: bool,
        include_iperf: bool,
    ) -> dict[str, Any]:
        response = await client.get(
            f"{self.config.api_base_url}/telemetry/current",
            params={
                "include_probe": str(include_probe).lower(),
                "include_iperf": str(include_iperf).lower(),
                "service_id": self.config.service_id,
            },
        )
        response.raise_for_status()
        return response.json()

    def evaluate(
        self,
        kpi: dict[str, Any],
        *,
        telemetry_source: str = "",
    ) -> dict[str, Any]:
        reasons: list[str] = []

        if kpi.get("congestion_detected") is True:
            reasons.append("congestion_detected=true")

        latency = _number(kpi.get("latency_ms"))
        if latency is not None and latency > self.config.max_latency_ms:
            reasons.append(f"latency_ms={latency} > {self.config.max_latency_ms}")

        loss = _number(kpi.get("packet_loss_rate"))
        if loss is not None and loss > self.config.max_loss_rate:
            reasons.append(f"packet_loss_rate={loss} > {self.config.max_loss_rate}")

        throughput = _number(kpi.get("throughput_mbps"))
        throughput_allowed = (
            not self.config.throughput_requires_iperf
            or "mininet_iperf" in telemetry_source
        )
        if (
            throughput is not None
            and throughput_allowed
            and self.config.min_throughput_mbps > 0
            and throughput < self.config.min_throughput_mbps
        ):
            reasons.append(
                f"throughput_mbps={throughput} < {self.config.min_throughput_mbps}"
            )

        queue_bytes = _number(kpi.get("queue_depth_bytes")) or 0
        if queue_bytes > self.config.queue_depth_bytes_threshold:
            reasons.append(f"queue_depth_bytes={queue_bytes}")

        prev = self.state.last_kpi or {}
        overlimits_delta = (_number(kpi.get("queue_overlimits")) or 0) - (
            _number(prev.get("queue_overlimits")) or 0
        )
        drops_delta = (_number(kpi.get("queue_drops")) or 0) - (
            _number(prev.get("queue_drops")) or 0
        )

        if overlimits_delta >= self.config.queue_overlimits_delta_threshold:
            reasons.append(f"queue_overlimits_delta={overlimits_delta}")
        if drops_delta >= self.config.queue_drops_delta_threshold:
            reasons.append(f"queue_drops_delta={drops_delta}")

        return {
            "bad": bool(reasons),
            "reason": "; ".join(reasons) if reasons else "ok",
            "reasons": reasons,
            "overlimits_delta": overlimits_delta,
            "drops_delta": drops_delta,
            "telemetry_source": telemetry_source,
        }

    def can_trigger(self) -> bool:
        return time.time() - self.state.last_trigger_ts >= self.config.cooldown_sec

    async def trigger_healing_intent(
        self,
        client: httpx.AsyncClient,
        kpi: dict[str, Any],
        verdict: dict[str, Any],
    ) -> str:
        incident_id = self.state.active_incident_id or _incident_id()
        self.state.active_incident_id = incident_id

        cfg = self.config
        text = (
            f"系统后台 Watcher 检测到已注册业务 {cfg.service_id} "
            f"（{cfg.src_host} 到 {cfg.dst_host}）KPI 异常。请基于实时拓扑和资源账本"
            "诊断、计算受约束替代路径并重新验证；不得停止该业务负载。"
        )
        payload = {
            "text": text,
            "priority": "critical",
            "user_id": "watcher",
            "context": {
                "triggered_by": "kpi_watcher",
                "incident_id": incident_id,
                "target_flow": {
                    "service_id": cfg.service_id,
                    "source": cfg.src_host,
                    "destination": cfg.dst_host,
                    "source_ip": cfg.src_ip,
                    "destination_ip": cfg.dst_ip,
                },
                "self_healing": {
                    "enabled": True,
                    "autonomous": True,
                    "reason": verdict["reason"],
                    "repair_actions": [
                        "reroute_constrained_path",
                    ],
                    "min_throughput_mbps": cfg.min_throughput_mbps,
                },
                "evidence": {
                    "kpi": kpi,
                    "verdict": verdict,
                },
            },
        }

        response = await client.post(f"{self.config.api_base_url}/intents", json=payload)
        response.raise_for_status()
        result = response.json()
        intent_id = result["intent_id"]

        self.state.last_trigger_ts = time.time()
        self.state.consecutive_bad = 0
        self.state.triggered_intents.append(intent_id)
        self.state.last_triggered_intent_id = intent_id
        self.state.healing_intent_id = intent_id
        self.state.healing_start_ts = time.time()
        self.state.state = "HEALING_IN_PROGRESS"
        await self._sync_write(incident={
            "incident_id": incident_id,
            "status": "triggered",
            "reason": verdict["reason"],
            "kpi_snapshot": kpi,
            "triggered_intent_id": intent_id,
        })
        if self._sync_client is None:
            self.store.upsert_watcher_incident(
                incident_id=incident_id,
                status="triggered",
                reason=verdict["reason"],
                kpi_snapshot=kpi,
                triggered_intent_id=intent_id,
            )
        await self._record_watcher_a2a(intent_id, incident_id, kpi, verdict)
        print(f"[watcher] triggered healing intent: incident={incident_id}, intent_id={intent_id}")
        return intent_id

    async def _resolve_active_incident(self, kpi: dict[str, Any], reason: str) -> None:
        if self.state.active_incident_id:
            await self._sync_write(incident={
                "incident_id": self.state.active_incident_id,
                "status": "resolved",
                "reason": reason,
                "kpi_snapshot": kpi,
                "resolved": True,
            })
            if self._sync_client is None:
                self.store.upsert_watcher_incident(
                    incident_id=self.state.active_incident_id,
                    status="resolved",
                    reason=reason,
                    kpi_snapshot=kpi,
                    resolved=True,
                )
        self.state.consecutive_bad = 0
        self.state.active_incident_id = None
        self.state.state = "NORMAL"

    async def _persist_state(
        self,
        state: str,
        reason: str,
        *,
        sample_id: str | None = None,
    ) -> None:
        self.state.state = state
        cooldown_remaining = max(
            0,
            int(self.config.cooldown_sec - (time.time() - self.state.last_trigger_ts)),
        ) if self.state.last_trigger_ts else 0
        value = {
            "enabled": self.config.enabled,
            "state": state,
            "last_sample_at": _utc_now() if sample_id else None,
            "sample_seq": self.state.sample_seq,
            "last_sample_id": sample_id,
            "consecutive_bad": self.state.consecutive_bad,
            "active_incident_id": self.state.active_incident_id,
            "last_triggered_intent_id": self.state.last_triggered_intent_id,
            "triggered_intents": self.state.triggered_intents[-10:],
            "cooldown_remaining_sec": cooldown_remaining,
            "last_reason": reason,
            "last_kpi": self.state.last_kpi or {},
            "consecutive_errors": self.state.consecutive_errors,
        }
        await self._sync_write(state=value)
        # Fallback: direct write for backward compat (when sync client unavailable)
        if self._sync_client is None:
            self.store.upsert_watcher_state(value)

    async def _record_watcher_a2a(
        self,
        intent_id: str,
        incident_id: str,
        kpi: dict[str, Any],
        verdict: dict[str, Any],
    ) -> None:
        now = _utc_now()
        message = {
            "id": f"A2A-{uuid.uuid4().hex[:12]}",
            "protocol_version": "a2a.runtime.v1",
            "trace_id": f"TRACE-{intent_id}",
            "intent_id": intent_id,
            "timestamp": now,
            "jsonrpc": "2.0",
            "method": "a2a.task.delegate",
            "message_type": "Task_Delegation",
            "task_id": f"TASK-{intent_id}-watcher-trigger",
            "correlation_id": f"TASK-{intent_id}-watcher-trigger",
            "source_agent": "kpi_watcher",
            "target_agent": "orchestrator",
            "target_agents": ["orchestrator"],
            "stage": "sense",
            "status": "success",
            "input_summary": "Watcher 连续检测到 KPI 异常，创建内部自愈意图。",
            "output_summary": f"KPI 异常，触发内部自愈意图；incident={incident_id}",
            "decision": "KPI 异常，触发内部自愈意图",
            "confidence": 0.88,
            "risk_score": 70,
            "evidence_refs": _evidence_refs(verdict),
            "payload_schema": "Watcher.IncidentTrigger.v1",
            "payload": {
                "incident_id": incident_id,
                "kpi": kpi,
                "verdict": verdict,
            },
            "metadata": {
                "triggered_by": "kpi_watcher",
            },
            "params": {
                "task_id": f"TASK-{intent_id}-watcher-trigger",
                "intent_id": intent_id,
                "source_agent": "kpi_watcher",
                "target_agent": "orchestrator",
                "stage": "sense",
                "status": "success",
                "payload_schema": "Watcher.IncidentTrigger.v1",
            },
        }
        try:
            from api.a2a_transport import get_a2a_transport

            transport = get_a2a_transport()
            message["transport"] = {
                "selected": transport.name,
                "event_type": "a2a_message",
            }
            transport.publish(intent_id, "a2a_message", message)
        except Exception:
            await self._sync_write(a2a_event={
                "intent_id": intent_id,
                "event_type": "a2a_message",
                "data": message,
            })
            if self._sync_client is None:
                self.store.add_event(intent_id, "a2a_message", message)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _evidence_refs(verdict: dict[str, Any]) -> list[str]:
    refs = []
    for reason in verdict.get("reasons") or []:
        if reason.startswith("congestion_detected"):
            refs.append("telemetry.kpi.congestion_detected")
        elif reason.startswith("queue_overlimits"):
            refs.append("telemetry.kpi.queue_overlimits")
        elif reason.startswith("queue_drops"):
            refs.append("telemetry.kpi.queue_drops")
        elif reason.startswith("packet_loss_rate"):
            refs.append("telemetry.kpi.packet_loss_rate")
        elif reason.startswith("latency_ms"):
            refs.append("telemetry.kpi.latency_ms")
        elif reason.startswith("throughput_mbps"):
            refs.append("telemetry.kpi.throughput_mbps")
        elif reason.startswith("queue_depth_bytes"):
            refs.append("telemetry.kpi.queue_depth_bytes")
    return refs or ["telemetry.kpi"]


async def main() -> None:
    watcher = KpiWatcher(WatcherConfig())
    await watcher.run_forever()


if __name__ == "__main__":
    asyncio.run(main())

"""Infrastructure generation tracking and restart-safe desired-state recovery."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from api._probe_session import get_probe_session
from api.store import IntentStore, get_store

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecoveryManager:
    def __init__(
        self, *, store: IntentStore | None = None, probe: Any = None,
        onos: Any = None, baseline_installer: Callable[[], Awaitable[dict]] | None = None,
        baseline_observer: Callable[..., Awaitable[dict]] | None = None,
        baseline_repairer: Callable[..., Awaitable[dict]] | None = None,
        policy_reconciler: Any = None, workload_orchestrator: Any = None,
        fault_controller: Any = None,
    ):
        self.store = store or get_store()
        self.probe = probe
        self.onos = onos
        self.baseline_installer = baseline_installer
        self.baseline_observer = baseline_observer
        self.baseline_repairer = baseline_repairer
        self.policy_reconciler = policy_reconciler
        self.workload_orchestrator = workload_orchestrator
        self.fault_controller = fault_controller
        self._lock = asyncio.Lock()

    def _previous(self, component: str) -> dict[str, Any] | None:
        with self.store._lock, self.store._connect() as connection:
            row = connection.execute(
                "SELECT identity_json FROM infrastructure_state WHERE component=?", (component,),
            ).fetchone()
        return json.loads(row["identity_json"]) if row else None

    def _remember(self, component: str, identity: dict[str, Any]) -> None:
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                """INSERT INTO infrastructure_state(component, identity_json, observed_at)
                VALUES (?, ?, ?) ON CONFLICT(component) DO UPDATE SET
                identity_json=excluded.identity_json, observed_at=excluded.observed_at""",
                (component, json.dumps(identity, default=str), _now()),
            )

    @staticmethod
    def _desired_onos_configuration(registry: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        pipeconf = os.getenv("IDN_PIPECONF_ID", "org.onosproject.pipelines.idn-basic")
        devices_by_id = {item.logical_device_id: item for item in registry.devices}
        payload: dict[str, Any] = {
            "devices": {
                device.logical_device_id: {
                    "basic": {
                        "driver": "stratum-bmv2",
                        "managementAddress": (
                            f"grpc://{device.p4runtime_endpoint}"
                            f"?device_id={device.p4runtime_node_id}"
                        ),
                        "pipeconf": pipeconf,
                    }
                }
                for device in registry.devices
            },
            "ports": {},
            "hosts": {},
        }
        host_inventory: list[dict[str, Any]] = []
        for endpoint in registry.endpoints:
            device = devices_by_id[endpoint.attached_device]
            connect_point = f"{endpoint.attached_device}/{endpoint.attached_port}"
            payload["ports"][connect_point] = {
                "interfaces": [{
                    "name": f"{device.switch_name}-{endpoint.host_name}",
                    "ips": [f"{endpoint.gateway_ip}/{endpoint.ip_prefix_length}"],
                    "mac": endpoint.gateway_mac,
                }]
            }
            host_id = f"{endpoint.mac_address}/None"
            payload["hosts"][host_id] = {
                "basic": {
                    "name": endpoint.host_name,
                    "ips": [endpoint.ip_address],
                    "locations": [connect_point],
                }
            }
            host_inventory.append({
                "mac": endpoint.mac_address,
                "vlan": "-1",
                "ipAddresses": [endpoint.ip_address],
                "locations": [{
                    "elementId": endpoint.attached_device,
                    "port": str(endpoint.attached_port),
                }],
            })
        return payload, host_inventory

    async def _ensure_onos_inventory(self, health: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Reconcile device, interface and host state after an ONOS restart.

        Mininet owns the running Stratum endpoints, while ONOS owns their
        connection configuration.  A fresh ONOS data directory therefore has
        zero devices until the desired netcfg is replayed.
        """
        if "device_count" not in health:
            return health, {"status": "PRESERVED"}
        from infra.resource_registry import get_resource_registry

        registry = get_resource_registry().snapshot()
        payload, host_inventory = self._desired_onos_configuration(registry)
        current_config: dict[str, Any] = {}
        try:
            current_config = await self.onos.get("/network/configuration/")
        except Exception as exc:
            logger.info("Unable to read ONOS netcfg before reconciliation: %s", exc)

        missing_sections = [
            section for section in ("devices", "ports", "hosts")
            if not set(payload[section]).issubset(set(current_config.get(section) or {}))
        ]
        devices_missing = (
            int(health.get("device_count") or 0) == 0 or "devices" in missing_sections
        )
        response: dict[str, Any] | None = None
        if missing_sections:
            response = await self.onos.post("/network/configuration/", payload)

        latest = health
        if devices_missing:
            timeout = max(float(os.getenv("IDN_ONOS_REPROVISION_TIMEOUT_SECONDS", "75")), 1.0)
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(2)
                latest = await self.onos.check_health()
                if int(latest.get("available_devices") or 0) >= len(registry.devices):
                    break
            else:
                return latest, {
                    "status": "TIMEOUT", "configured_devices": len(registry.devices),
                    "available_devices": int(latest.get("available_devices") or 0),
                    "missing_sections": missing_sections, "response": response,
                }

        existing_host_ids: set[str] = set()
        try:
            existing = await self.onos.get_hosts()
            existing_host_ids = {
                str(item.get("id") or "").lower()
                for item in existing.get("hosts", []) or []
            }
        except Exception as exc:
            logger.info("Unable to read ONOS host inventory before reconciliation: %s", exc)
        hosts_posted = 0
        for host in host_inventory:
            host_id = f'{host["mac"]}/None'.lower()
            if host_id in existing_host_ids:
                continue
            await self.onos.post("/hosts", host)
            hosts_posted += 1

        repaired = bool(missing_sections or hosts_posted)
        return latest, {
            "status": "RESTORED" if repaired else "PRESERVED",
            "configured_devices": len(registry.devices),
            "configured_ports": len(payload["ports"]),
            "configured_hosts": len(payload["hosts"]),
            "missing_sections": missing_sections,
            "host_entities_posted": hosts_posted,
            "response": response,
        }

    async def _identities(self) -> tuple[dict[str, Any], dict[str, Any]]:
        probe = self.probe or await get_probe_session()
        mininet = await probe.get("/runtime/identity")
        if not mininet or mininet.get("status") != "ok":
            raise RuntimeError("MININET_IDENTITY_UNAVAILABLE")
        if self.onos is None:
            from mcp_servers.onos_server import get_onos_client

            self.onos = get_onos_client()
        health = await self.onos.check_health()
        health, inventory_recovery = await self._ensure_onos_inventory(health)
        flows = await self.onos.get_flows()
        flow_items = flows.get("flows") or []
        controller_nodes: list[dict[str, Any]] = []
        try:
            cluster = await self.onos.get("/cluster")
            controller_nodes = [
                {"id": item.get("id"), "last_update": item.get("lastUpdate")}
                for item in cluster.get("nodes") or []
            ]
        except Exception as exc:
            logger.info("Unable to read ONOS controller epoch: %s", exc)
        epoch_payload = json.dumps(sorted(controller_nodes, key=lambda item: str(item.get("id"))), sort_keys=True)
        controller_epoch = hashlib.sha256(epoch_payload.encode("utf-8")).hexdigest() if controller_nodes else None
        try:
            if self.baseline_observer is None:
                from api.baseline import observe_baseline_connectivity
                self.baseline_observer = observe_baseline_connectivity
            baseline_observation = await self.baseline_observer(onos_client=self.onos)
        except Exception as exc:
            baseline_observation = {"status": "UNAVAILABLE", "error": str(exc)}
        onos = {
            "reachable": health.get("status") in {"ok", "healthy", "UP"} or bool(health),
            "flow_count": len(flow_items),
            "added_flow_count": sum(1 for item in flow_items if str(item.get("state") or "").upper() == "ADDED"),
            "inventory_recovery": inventory_recovery,
            "controller_epoch": controller_epoch,
            "controller_nodes": controller_nodes,
            "baseline_observation": baseline_observation,
        }
        return mininet, onos

    async def recover_once(self, *, trigger: str = "periodic") -> dict[str, Any]:
        if self._lock.locked():
            return {"status": "SKIPPED_SINGLE_FLIGHT"}
        async with self._lock:
            run_id, started = f"RCV-{uuid.uuid4().hex[:12]}", _now()
            result: dict[str, Any] = {"run_id": run_id, "trigger": trigger, "steps": {}}
            try:
                mininet, onos = await self._identities()
                previous_mininet, previous_onos = self._previous("mininet"), self._previous("onos")
                mininet_restarted = bool(previous_mininet and previous_mininet.get("runtime_id") != mininet.get("runtime_id"))
                first_observation = previous_mininet is None
                onos_lost_state = bool(
                    previous_onos and int(previous_onos.get("flow_count") or 0) > 0
                    and int(onos.get("flow_count") or 0) == 0
                )
                onos_inventory_repaired = (
                    (onos.get("inventory_recovery") or {}).get("status") == "RESTORED"
                )
                controller_epoch_changed = bool(
                    previous_onos
                    and previous_onos.get("controller_epoch")
                    and previous_onos.get("controller_epoch") != onos.get("controller_epoch")
                )
                baseline_drift = (onos.get("baseline_observation") or {}).get("status") == "DRIFTED"
                result["detection"] = {
                    "first_observation": first_observation,
                    "mininet_restarted": mininet_restarted,
                    "onos_flow_state_lost": onos_lost_state,
                    "onos_inventory_repaired": onos_inventory_repaired,
                    "controller_epoch_changed": controller_epoch_changed,
                    "baseline_drift": baseline_drift,
                    "mininet": mininet, "onos": onos,
                }
                recovery_trigger = (
                    "CONTROLLER_RESTART_DRIFT" if controller_epoch_changed or baseline_drift
                    else "DATAPLANE_STATE_LOST" if mininet_restarted or onos_lost_state
                    else "INITIAL_RECONCILIATION" if first_observation or onos_inventory_repaired
                    else None
                )
                baseline_before = onos.get("baseline_observation") or {}
                if baseline_drift:
                    if self.baseline_repairer is None:
                        from api.baseline import repair_baseline_connectivity
                        self.baseline_repairer = repair_baseline_connectivity
                    result["steps"]["baseline"] = await self.baseline_repairer(
                        baseline_before, onos_client=self.onos,
                    )
                    # ONOS accepts a flow before the P4Runtime provider has
                    # necessarily completed the physical table write.  Poll
                    # the two independent readers for a bounded period; this
                    # performs no further write and reports a degraded result
                    # rather than concealing an incomplete repair.
                    # The pipeconf/provider re-establishes its P4Runtime
                    # session after ONOS restart.  This is a bounded
                    # observation window, not a retry of the write path.
                    attempts = max(1, int(os.getenv("IDN_BASELINE_READBACK_ATTEMPTS", "45")))
                    interval = max(0.0, float(os.getenv("IDN_BASELINE_READBACK_INTERVAL_SECONDS", "1")))
                    observations: list[dict[str, Any]] = []
                    readback: dict[str, Any] = {}
                    for attempt in range(1, attempts + 1):
                        readback = await self.baseline_observer(onos_client=self.onos)
                        observations.append({
                            "attempt": attempt,
                            "status": readback.get("status"),
                            "verified_entry_count": readback.get("verified_entry_count"),
                            "missing_entry_count": readback.get("missing_entry_count"),
                        })
                        if readback.get("status") == "VERIFIED" or attempt == attempts:
                            break
                        await asyncio.sleep(interval)
                    result["steps"]["baseline_readback"] = readback
                    result["steps"]["baseline_readback_attempts"] = observations
                elif baseline_before.get("status") == "VERIFIED":
                    # A successful dual readback is the idempotency gate.  In
                    # particular, a backend restart must never repost the
                    # complete baseline just because it is the first cycle of
                    # this RecoveryManager process.
                    result["steps"]["baseline"] = {
                        "status": "VERIFIED",
                        "reason": "DESIRED_OBSERVED_IN_SYNC",
                    }
                elif first_observation or mininet_restarted or onos_lost_state or onos_inventory_repaired:
                    # A reader failure must not be interpreted as a healthy
                    # data plane.  Retain the existing full bootstrap fallback
                    # for this exceptional path only.
                    if self.baseline_installer is None:
                        from api.baseline import install_baseline_connectivity

                        self.baseline_installer = install_baseline_connectivity
                    result["steps"]["baseline"] = await self.baseline_installer()
                    result["steps"]["baseline_readback"] = await self.baseline_observer(onos_client=self.onos)
                elif controller_epoch_changed:
                    # Epoch changes always force a fresh Desired-vs-Observed
                    # readback, but a fully verified table must not be posted
                    # again merely because ONOS restarted.
                    result["steps"]["baseline"] = {"status": "VERIFIED", "reason": "CONTROLLER_EPOCH_READBACK"}
                else:
                    result["steps"]["baseline"] = {"status": "PRESERVED", "reason": "runtime generation unchanged"}

                result["recovery_execution"] = {
                    "execution_id": run_id,
                    "trigger": recovery_trigger or trigger,
                    "previous_environment_epoch": {
                        "mininet_runtime_id": (previous_mininet or {}).get("runtime_id"),
                        "controller_epoch": (previous_onos or {}).get("controller_epoch"),
                    },
                    "current_environment_epoch": {
                        "mininet_runtime_id": mininet.get("runtime_id"),
                        "controller_epoch": onos.get("controller_epoch"),
                    },
                    "desired_entries": int(baseline_before.get("desired_entry_count") or 0),
                    "missing_entries": int(baseline_before.get("missing_entry_count") or 0),
                    "repaired_entries": int((result["steps"].get("baseline") or {}).get("missing_entries") or 0),
                }
                self._remember("baseline_forwarding", {
                    "desired_entry_count": baseline_before.get("desired_entry_count"),
                    "verified_entry_count": (result["steps"].get("baseline_readback") or baseline_before).get("verified_entry_count"),
                    "controller_epoch": onos.get("controller_epoch"),
                    "updated_by": run_id,
                })

                if self.workload_orchestrator is None:
                    from orchestrator.traffic_orchestrator import get_traffic_orchestrator

                    self.workload_orchestrator = get_traffic_orchestrator()
                result["steps"]["workloads"] = await self.workload_orchestrator.reconcile()

                if self.fault_controller is None:
                    from infra.fault_controller import get_fault_controller

                    self.fault_controller = get_fault_controller()
                result["steps"]["faults"] = await self.fault_controller.reconcile()

                if self.policy_reconciler is None:
                    from infra.network_reconciler import get_network_reconciler

                    self.policy_reconciler = get_network_reconciler()
                policy_result = await self.policy_reconciler.run_once(auto_repair=True)
                result["steps"]["policies"] = (
                    policy_result.model_dump(mode="json")
                    if hasattr(policy_result, "model_dump") else policy_result
                )
                self._remember("mininet", mininet)
                self._remember("onos", onos)
                baseline_readback = result["steps"].get("baseline_readback")
                result["status"] = (
                    "DEGRADED" if baseline_readback and baseline_readback.get("status") != "VERIFIED"
                    else "COMPLETED"
                )
                self.store.add_audit_event(
                    event_type="RECOVERY_COMPLETED" if result["status"] == "COMPLETED" else "RECOVERY_DEGRADED",
                    resource_type="recovery", resource_id=run_id,
                    correlation={"trigger": trigger}, observed=result["detection"],
                    action={"steps": list(result["steps"])}, result={"status": result["status"]},
                )
            except Exception as exc:
                result.update({"status": "FAILED", "error": str(exc)})
                logger.warning("Recovery cycle failed: %s", exc)
            finished = _now()
            with self.store._lock, self.store._connect() as connection:
                connection.execute(
                    "INSERT INTO recovery_runs VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, trigger, result["status"], json.dumps(result, default=str), started, finished),
                )
            return result

    def status(self) -> dict[str, Any]:
        with self.store._lock, self.store._connect() as connection:
            rows = connection.execute("SELECT * FROM infrastructure_state ORDER BY component").fetchall()
            last = connection.execute("SELECT * FROM recovery_runs ORDER BY finished_at DESC LIMIT 1").fetchone()
        return {
            "components": {row["component"]: json.loads(row["identity_json"]) for row in rows},
            "last_run": json.loads(last["result_json"]) if last else None,
            "running": self._lock.locked(),
        }

    async def run_loop(self, shutdown: asyncio.Event) -> None:
        first = True
        while not shutdown.is_set():
            await self.recover_once(trigger="startup" if first else "periodic")
            first = False
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass


_manager: RecoveryManager | None = None


def get_recovery_manager() -> RecoveryManager:
    global _manager
    current = get_store()
    if _manager is None or _manager.store is not current:
        _manager = RecoveryManager(store=current)
    return _manager

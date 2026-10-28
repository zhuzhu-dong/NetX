"""Persistent, reversible network fault controller with residual detection."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from api._probe_session import get_probe_session
from api.store import IntentStore, get_store
from infra.resource_registry import ResourceRegistry, get_resource_registry
from models.faults import FaultDefinition, FaultLifecycle, FaultRecord, FaultType, PathReversionPolicy

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FaultController:
    def __init__(
        self, *, store: IntentStore | None = None, registry: ResourceRegistry | None = None,
        probe: Any = None, topology_healer: Any = None,
    ):
        self.store = store or get_store()
        self.registry = registry or get_resource_registry()
        self.probe = probe
        self.topology_healer = topology_healer
        self._locks: dict[str, asyncio.Lock] = {}

    async def _probe(self):
        return self.probe or await get_probe_session()

    def _lock(self, resource_id: str) -> asyncio.Lock:
        return self._locks.setdefault(resource_id, asyncio.Lock())

    def _resolve_target(self, definition: FaultDefinition) -> tuple[str, dict[str, Any]]:
        if definition.fault_type.value.startswith("LINK_"):
            link = self.registry.get_link(definition.target_resource_id)
            return link.resource_id, {
                "canonical_resource_id": link.resource_id,
                "runtime_candidates": [definition.target_resource_id, link.physical_link_id],
            }
        if definition.fault_type == FaultType.DEVICE_DOWN:
            device_id = self.registry.resolve(definition.target_resource_id)
            device = next((item for item in self.registry.snapshot().devices if item.resource_id == device_id), None)
            if device is None:
                raise ValueError(f"fault target is not a device: {definition.target_resource_id}")
            return device.resource_id, {"canonical_resource_id": device.resource_id, "switch_name": device.switch_name}
        return definition.target_resource_id, {"canonical_resource_id": definition.target_resource_id}

    def _save(
        self, definition: FaultDefinition, state: FaultLifecycle, *, desired: FaultLifecycle,
        observed: dict[str, Any] | None = None, error: str | None = None,
        applied_at: str | None = None, removed_at: str | None = None,
    ) -> None:
        now = _now()
        with self.store._lock, self.store._connect() as connection:
            existing = connection.execute(
                "SELECT created_at, applied_at, removed_at FROM faults WHERE fault_id=?",
                (definition.fault_id,),
            ).fetchone()
            connection.execute(
                """INSERT INTO faults(
                    fault_id, fault_type, target_resource_id, lifecycle_state, desired_state,
                    reversion_policy, definition_json, observed_json, last_error,
                    applied_at, removed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fault_id) DO UPDATE SET
                    fault_type=excluded.fault_type, target_resource_id=excluded.target_resource_id,
                    lifecycle_state=excluded.lifecycle_state, desired_state=excluded.desired_state,
                    reversion_policy=excluded.reversion_policy, definition_json=excluded.definition_json,
                    observed_json=excluded.observed_json, last_error=excluded.last_error,
                    applied_at=excluded.applied_at, removed_at=excluded.removed_at,
                    updated_at=excluded.updated_at""",
                (definition.fault_id, definition.fault_type.value, definition.target_resource_id,
                 state.value, desired.value, definition.reversion_policy.value,
                 definition.model_dump_json(), json.dumps(observed or {}, default=str), error,
                 applied_at if applied_at is not None else (existing["applied_at"] if existing else None),
                 removed_at if removed_at is not None else (existing["removed_at"] if existing else None),
                 existing["created_at"] if existing else now, now),
            )

    @staticmethod
    def _record(row: Any) -> FaultRecord:
        definition = json.loads(row["definition_json"])
        return FaultRecord(
            **definition, lifecycle_state=row["lifecycle_state"], desired_state=row["desired_state"],
            observed=json.loads(row["observed_json"] or "{}"), last_error=row["last_error"],
            applied_at=row["applied_at"], removed_at=row["removed_at"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def get(self, fault_id: str) -> FaultRecord:
        with self.store._lock, self.store._connect() as connection:
            row = connection.execute("SELECT * FROM faults WHERE fault_id=?", (fault_id,)).fetchone()
        if row is None:
            raise KeyError(fault_id)
        return self._record(row)

    def list(self, *, include_removed: bool = True) -> list[FaultRecord]:
        where = "" if include_removed else "WHERE lifecycle_state != 'REMOVED'"
        with self.store._lock, self.store._connect() as connection:
            rows = connection.execute(f"SELECT * FROM faults {where} ORDER BY updated_at DESC").fetchall()
        return [self._record(row) for row in rows]

    async def apply(self, definition: FaultDefinition, *, heal: bool = True) -> FaultRecord:
        target, resolution = self._resolve_target(definition)
        async with self._lock(target):
            try:
                existing = self.get(definition.fault_id)
                if existing.lifecycle_state == FaultLifecycle.ACTIVE:
                    return existing
                if existing.lifecycle_state not in {FaultLifecycle.REMOVED, FaultLifecycle.FAILED, FaultLifecycle.RESIDUAL}:
                    raise ValueError(f"fault {definition.fault_id} is {existing.lifecycle_state.value}")
            except KeyError:
                pass
            self._save(definition, FaultLifecycle.APPLYING, desired=FaultLifecycle.ACTIVE)
            try:
                probe = await self._probe()
                result = await probe.post("/faults/apply", {
                    **definition.model_dump(mode="json"), "target_resolution": resolution,
                })
                if not result or result.get("status") != "ok" or not result.get("active"):
                    raise RuntimeError(f"FAULT_APPLY_NOT_OBSERVED: {result}")
                self._save(definition, FaultLifecycle.ACTIVE, desired=FaultLifecycle.ACTIVE,
                           observed=result, applied_at=_now())
                self.store.add_audit_event(
                    event_type="FAULT_APPLIED", resource_type="fault", resource_id=definition.fault_id,
                    actor=definition.actor, expected={"state": "ACTIVE"}, observed=result,
                    action=definition.model_dump(mode="json"), result={"status": "ok"}, reason=definition.reason,
                )
                if heal and definition.fault_type in {FaultType.LINK_DOWN, FaultType.DEVICE_DOWN}:
                    await self._heal("FAULT_APPLIED", definition)
                return self.get(definition.fault_id)
            except Exception as exc:
                self._save(definition, FaultLifecycle.FAILED, desired=FaultLifecycle.ACTIVE, error=str(exc))
                self.store.add_audit_event(
                    event_type="FAULT_APPLY_FAILED", resource_type="fault", resource_id=definition.fault_id,
                    actor=definition.actor, action=definition.model_dump(mode="json"),
                    result={"status": "error", "error": str(exc)}, reason=definition.reason,
                )
                raise

    async def remove(self, fault_id: str, *, heal: bool = True) -> FaultRecord:
        current = self.get(fault_id)
        definition = FaultDefinition(**current.model_dump(exclude={
            "lifecycle_state", "desired_state", "observed", "applied_at", "removed_at",
            "created_at", "updated_at", "last_error",
        }))
        target, resolution = self._resolve_target(definition)
        async with self._lock(target):
            self._save(definition, FaultLifecycle.REMOVING, desired=FaultLifecycle.REMOVED,
                       observed=current.observed)
            try:
                probe = await self._probe()
                result = await probe.post("/faults/remove", {
                    "fault_id": fault_id, "fault_type": definition.fault_type.value,
                    "target_resource_id": definition.target_resource_id,
                    "target_resolution": resolution,
                })
                if not result or result.get("status") != "ok" or result.get("active"):
                    raise RuntimeError(f"FAULT_REMOVE_NOT_OBSERVED: {result}")
                self._save(definition, FaultLifecycle.REMOVED, desired=FaultLifecycle.REMOVED,
                           observed=result, removed_at=_now())
                self.store.add_audit_event(
                    event_type="FAULT_REMOVED", resource_type="fault", resource_id=fault_id,
                    actor=definition.actor, expected={"state": "REMOVED"}, observed=result,
                    action={"operation": "REMOVE"}, result={"status": "ok"}, reason=definition.reason,
                )
                if heal and definition.reversion_policy == PathReversionPolicy.PREEMPTIVE:
                    await self._heal("FAULT_REMOVED", definition)
                return self.get(fault_id)
            except Exception as exc:
                self._save(definition, FaultLifecycle.RESIDUAL, desired=FaultLifecycle.REMOVED,
                           observed=current.observed, error=str(exc))
                raise

    async def inspect(self, fault_id: str) -> FaultRecord:
        current = self.get(fault_id)
        probe = await self._probe()
        observed = await probe.get("/faults", params={"fault_id": fault_id})
        active = bool(observed and observed.get("active"))
        desired_active = current.desired_state == FaultLifecycle.ACTIVE
        state = FaultLifecycle.ACTIVE if active and desired_active else (
            FaultLifecycle.REMOVED if not active and not desired_active else FaultLifecycle.RESIDUAL
        )
        definition = FaultDefinition(**current.model_dump(exclude={
            "lifecycle_state", "desired_state", "observed", "applied_at", "removed_at",
            "created_at", "updated_at", "last_error",
        }))
        self._save(definition, state, desired=current.desired_state, observed=observed or {})
        return self.get(fault_id)

    async def reconcile(self) -> dict[str, Any]:
        probe = await self._probe()
        runtime = await probe.get("/faults") or {"faults": []}
        runtime_ids = {str(item["fault_id"]) for item in runtime.get("faults") or [] if item.get("active")}
        known = {item.fault_id: item for item in self.list()}
        residual_runtime = sorted(runtime_ids - set(known))
        results = []
        for fault in known.values():
            try:
                results.append((await self.inspect(fault.fault_id)).model_dump(mode="json"))
            except Exception as exc:
                results.append({"fault_id": fault.fault_id, "status": "error", "error": str(exc)})
        for fault_id in residual_runtime:
            self.store.add_audit_event(
                event_type="FAULT_RESIDUAL_DETECTED", resource_type="fault", resource_id=fault_id,
                observed={"active": True, "source": "mininet"}, result={"status": "needs_operator"},
            )
        return {"status": "ok", "checked": len(known), "results": results,
                "unknown_runtime_fault_ids": residual_runtime}

    async def _heal(self, event: str, definition: FaultDefinition) -> None:
        try:
            if self.topology_healer is not None:
                result = await self.topology_healer(event, definition)
            else:
                from api.baseline import install_baseline_connectivity
                from infra.network_reconciler import get_network_reconciler

                baseline = await install_baseline_connectivity()
                preemptive_migrations: list[dict[str, Any]] = []
                if (
                    event == "FAULT_REMOVED"
                    and definition.reversion_policy == PathReversionPolicy.PREEMPTIVE
                ):
                    # APPLY performs an explicit fresh path selection in the
                    # Mininet adapter.  VERIFY intentionally retains any
                    # viable alternate path for NON_PREEMPTIVE semantics.
                    from api.qos import apply_qos_policy
                    from api.service_profiles import resolve_service_profile_for_policy
                    from infra.qos_resource_allocator import QoSResourceAllocator
                    from models.yang_policy import YANGPolicy
                    from persistence.policy_store import PolicyStore

                    policy_store = PolicyStore(self.store)
                    allocator = QoSResourceAllocator(self.store)
                    for item in policy_store.list_all():
                        policy_data = dict(item)
                        store_data = dict(policy_data.pop("_store", {}) or {})
                        try:
                            runtime = dict(store_data.get("runtime") or {})
                            scope = str(store_data.get("traffic_scope_key") or "")
                            binding = allocator.get(scope) if scope else None
                            service_id = runtime.get("service_id")
                            if not service_id and binding is not None:
                                service_id = binding.service_id
                            policy = YANGPolicy(
                                network_policy=policy_data,
                                service_id=service_id,
                            )
                            if not policy.service_id:
                                try:
                                    profile = resolve_service_profile_for_policy(policy)
                                    policy = policy.model_copy(update={"service_id": profile.service_id})
                                except (KeyError, ValueError):
                                    logger.info(
                                        "No unique service profile during fault recovery for policy_id=%s",
                                        policy.network_policy.policy_id,
                                    )
                            migrated = await apply_qos_policy(policy, operation="APPLY")
                            resolved_service_id = (
                                policy.service_id
                                or (migrated.get("resource_binding") or {}).get("service_id")
                            )
                            if resolved_service_id:
                                runtime["service_id"] = resolved_service_id
                            runtime["qos_enforcement"] = migrated
                            policy_store.commit_staged(
                                store_data["traffic_scope_key"],
                                intent_id=str(store_data.get("intent_id") or "FAULT_PREEMPTIVE_REVERSION"),
                                runtime=runtime,
                            )
                            preemptive_migrations.append({
                                "policy_id": policy.network_policy.policy_id,
                                "status": "MIGRATED" if migrated.get("migrated") else "PRESERVED",
                                "resource_binding": migrated.get("resource_binding"),
                            })
                        except Exception as exc:
                            preemptive_migrations.append({
                                "policy_id": policy_data.get("policy_id"),
                                "status": "FAILED", "error": str(exc),
                            })
                reconciliation = await get_network_reconciler().run_once(auto_repair=True)
                result = {
                    "baseline": baseline,
                    "preemptive_migrations": preemptive_migrations,
                    "reconciliation": reconciliation.model_dump(mode="json"),
                }
            self.store.add_audit_event(
                event_type="FAULT_TOPOLOGY_RECONCILED", resource_type="fault",
                resource_id=definition.fault_id, correlation={"trigger": event},
                action={"operation": "REPLAN_AND_RECONCILE"}, result=result,
            )
        except Exception as exc:
            logger.exception("Fault-triggered topology reconciliation failed")
            self.store.add_audit_event(
                event_type="FAULT_TOPOLOGY_RECONCILE_FAILED", resource_type="fault",
                resource_id=definition.fault_id, correlation={"trigger": event},
                result={"status": "error", "error": str(exc)},
            )

    async def run_loop(self, shutdown: asyncio.Event) -> None:
        while not shutdown.is_set():
            try:
                await self.reconcile()
            except Exception:
                logger.exception("Fault reconciliation cycle failed")
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass


_controller: FaultController | None = None


def get_fault_controller() -> FaultController:
    global _controller
    current = get_store()
    if _controller is None or _controller.store is not current:
        _controller = FaultController(store=current)
    return _controller

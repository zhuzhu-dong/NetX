"""One truthful withdrawal workflow shared by every API surface."""

from __future__ import annotations

import json
from typing import Any

from agents.intent_execution import IntentExecutionAgent
from api.store import IntentStore
from models.intent_domain import (
    AgentDecision,
    ExecutionStatus,
    IntentExecutionResult,
    IntentLifecycle,
    IntentRuntimeState,
    RevisionStatus,
)
from persistence.intent_domain_store import IntentDomainStore


class IntentWithdrawalService:
    def __init__(
        self,
        store: IntentStore,
        domain: IntentDomainStore,
        execution: IntentExecutionAgent,
    ):
        self.store = store
        self.domain = domain
        self.execution = execution

    async def withdraw(
        self, intent_id: str, *, actor: str = "intent_api"
    ) -> IntentRuntimeState:
        if self.store.get(intent_id) is None:
            raise KeyError(intent_id)
        runtime = self.domain.get_runtime(intent_id) or IntentRuntimeState(
            intent_id=intent_id
        )
        from persistence.preference_relation_store import PreferenceRelationStore

        preference_store = PreferenceRelationStore(self.store)
        preference_history = preference_store.by_intent(intent_id)
        # A preference relation is scheduler-only only when it owns no
        # committed QoS document.  Earlier workflows updated existing policy
        # rows in place and then withdrew the parent as if it had no resources,
        # leaving a WITHDRAWN runtime as the live owner.  Restore the durable
        # predecessor ownership through PolicyStore history before finalizing
        # the withdrawn parent; never infer it from a dashboard projection.
        if preference_history and runtime.lifecycle == IntentLifecycle.WITHDRAWN:
            ownership = self._reconcile_preference_owned_policies(intent_id)
            if ownership["status"] == "REPAIRED":
                runtime = self._finalize_runtime_bindings(runtime)
                self._decision(intent_id, runtime, {"preference_ownership_reconciliation": ownership}, actor)
                self.domain.record_event(
                    intent_id, "WITHDRAWN_OWNERSHIP_RECONCILED", actor=actor,
                    reason="restored live policy ownership to predecessor intents",
                    data=ownership,
                )
                return runtime
            if ownership["status"] == "BLOCKED":
                raise RuntimeError(ownership["reason"])
        if runtime.lifecycle == IntentLifecycle.WITHDRAWN and not self._owned_live_policies(intent_id):
            runtime = self._finalize_runtime_bindings(runtime)
            self._decision(intent_id, runtime, {"status": "ALREADY_ABSENT"}, actor)
            return runtime
        if runtime.lifecycle == IntentLifecycle.WITHDRAWN:
            # A previous withdrawal response must not be terminal if its
            # durable policy is still live.  Re-open only to the explicit
            # pending-cleanup state and retry the formal cleanup; this is not
            # a normal business lifecycle revival.
            runtime = self.domain.transition(
                intent_id,
                IntentLifecycle.WITHDRAW_PENDING_CLEANUP,
                actor=actor,
                reason="withdrawn runtime still owns a live policy; retrying verified cleanup",
            )
            self.store.update(intent_id, status="withdraw_pending_cleanup")
        if preference_history:
            # A scheduler-only relation owns no P4Runtime, ONOS, TC or QoS
            # resource.  Its formal withdrawal only retires the durable
            # effective relation and retains the complete history.
            runtime = self.domain.transition(
                intent_id,
                IntentLifecycle.WITHDRAW_REQUESTED,
                actor=actor,
                reason="control-plane preference withdrawal requested",
            )
            retired = preference_store.withdraw_intent(intent_id)
            cleanup = {
                "control_plane_preference": {
                    "retired_relations": retired,
                    "southbound_writes": {"p4": 0, "tc": 0, "onos": 0},
                },
                "path": {"status": "NOT_REQUIRED"},
                "legacy": {"status": "NOT_REQUIRED"},
                "qos": [{"status": "NOT_REQUIRED"}],
            }
            self._decision(intent_id, runtime, cleanup, actor)
            ownership = self._reconcile_preference_owned_policies(intent_id)
            if ownership["status"] == "BLOCKED":
                self.store.update(intent_id, status="withdraw_pending_cleanup")
                return self.domain.transition(
                    intent_id, IntentLifecycle.WITHDRAW_PENDING_CLEANUP,
                    actor=actor, reason=ownership["reason"], data=ownership,
                )
            if ownership["status"] == "REPAIRED":
                cleanup["preference_ownership_reconciliation"] = ownership
                self._decision(intent_id, runtime, {"preference_ownership_reconciliation": ownership}, actor)
            runtime = self._finalize_runtime_bindings(runtime)
            self.store.update(intent_id, status="cancelled")
            return self.domain.transition(
                intent_id,
                IntentLifecycle.WITHDRAWN,
                actor=actor,
                reason="control-plane preference withdrawn; no southbound cleanup required",
                data={"cleanup": cleanup},
            )
        if runtime.lifecycle != IntentLifecycle.WITHDRAW_PENDING_CLEANUP:
            runtime = self.domain.transition(
                intent_id,
                IntentLifecycle.WITHDRAW_REQUESTED,
                actor=actor,
                reason="withdrawal requested; active resources remain until cleanup is verified",
            )

        # Withdrawal changes controller, P4 and TC resources just like a
        # workflow execution.  Give the cleanup a real fenced writer identity
        # instead of invoking a guarded TC actuator with no active lease.
        from infra.network_write_fencing import NetworkWriteFencing, write_context

        lease = NetworkWriteFencing(self.store).acquire(
            owner_type="INTENT_WITHDRAWAL",
            owner_id=intent_id,
            recovery_id=f"WITHDRAW-{intent_id}",
            ttl_seconds=120,
        )
        try:
            with write_context(
                owner_type=lease["owner_type"],
                owner_id=lease["owner_id"],
                fencing_token=int(lease["fencing_token"]),
            ):
                cleanup = await self._cleanup_resources(intent_id, runtime)
        finally:
            NetworkWriteFencing(self.store).release(lease["lease_id"])

        failed = int(cleanup["path"].get("failed") or 0)
        failed += int(cleanup["legacy"].get("failed") or 0)
        failed += sum(
            1
            for item in cleanup["qos"]
            if item.get("status")
            not in {"ok", "not_found", "skipped", "ALREADY_ABSENT"}
        )
        cleanup["cleanup_failures"] = failed
        self._decision(intent_id, runtime, cleanup, actor)
        if failed:
            self.store.update(intent_id, status="withdraw_pending_cleanup")
            return self.domain.transition(
                intent_id,
                IntentLifecycle.WITHDRAW_PENDING_CLEANUP,
                actor=actor,
                reason="withdrawal cleanup is incomplete and requires reconciliation",
                data={"cleanup": cleanup},
            )
        retirement = self._retire_owned_policies(intent_id, cleanup)
        cleanup["policy_retirement"] = retirement
        if retirement["failed"]:
            self.store.update(intent_id, status="withdraw_pending_cleanup")
            return self.domain.transition(
                intent_id,
                IntentLifecycle.WITHDRAW_PENDING_CLEANUP,
                actor=actor,
                reason="withdrawal data-plane cleanup completed but policy retirement failed",
                data={"cleanup": cleanup},
            )
        runtime = self._finalize_runtime_bindings(runtime)
        self.store.update(intent_id, status="cancelled")
        return self.domain.transition(
            intent_id,
            IntentLifecycle.WITHDRAWN,
            actor=actor,
            reason="withdrawal cleanup completed or resources were already absent",
            data={"cleanup": cleanup},
        )

    def _reconcile_preference_owned_policies(self, intent_id: str) -> dict[str, Any]:
        """Restore only proven predecessor owners for an already-withdrawn preference."""
        from persistence.policy_store import PolicyStore

        policy_store = PolicyStore(self.store)
        candidates = policy_store.preference_owner_candidates(intent_id)
        if not candidates:
            return {"status": "NOT_REQUIRED", "restored": [], "southbound_writes": {"p4": 0, "tc": 0, "onos": 0}}
        expected: dict[str, str] = {}
        for candidate in candidates:
            predecessor = candidate.get("predecessor_intent_id")
            predecessor_runtime = self.domain.get_runtime(str(predecessor)) if predecessor else None
            lifecycle = str(getattr(predecessor_runtime, "lifecycle", "").value if predecessor_runtime else "")
            # ``CANCELLED``/``SUPERSEDED``/``EXPIRED`` are legacy persisted
            # values, not members of the first-class IntentLifecycle enum.
            # Compare their durable strings so reconciliation remains safe for
            # historical data without dereferencing a nonexistent enum value.
            if not predecessor or lifecycle in {
                IntentLifecycle.WITHDRAWN.value, "CANCELLED", "SUPERSEDED", "EXPIRED",
            }:
                return {
                    "status": "BLOCKED", "reason": "PREFERENCE_OWNER_PREDECESSOR_NOT_LIVE",
                    "candidates": candidates,
                }
            expected[str(candidate["policy_id"])] = str(predecessor)
        restored = policy_store.restore_preference_ownership(intent_id, expected_owners=expected)
        return {
            "status": "REPAIRED", "restored": restored, "candidates": candidates,
            "southbound_writes": {"p4": 0, "tc": 0, "onos": 0},
        }

    async def withdraw_effective_scope(
        self, scope_key: str, *, expected_policy_id: str, expected_policy_version: int,
        expected_binding_id: str, actor: str = "dashboard", reason: str = "USER_REQUESTED",
    ) -> dict[str, Any]:
        """Withdraw one authoritative scope with an optimistic owner check.

        Unlike the legacy Intent-ID endpoint this method treats a compound
        parent as orchestration metadata: it never removes sibling scopes.
        """
        from infra.network_write_fencing import NetworkWriteFencing, write_context
        from infra.qos_resource_allocator import QoSResourceAllocator
        from persistence.policy_store import PolicyStore

        resolver = PolicyStore(self.store, read_only=True)
        owner = resolver.resolve_managed_desired_for_scope(scope_key)
        if owner is None:
            historical = resolver.get_by_scope(scope_key)
            if historical and historical.get("policy_id") == expected_policy_id and historical.get("status") == "WITHDRAWN":
                return {"status": "ALREADY_WITHDRAWN", "scope_key": scope_key, "policy_id": expected_policy_id}
            raise RuntimeError("CURRENT_OWNER_CHANGED")
        binding = QoSResourceAllocator(self.store).get_active_for_scope(scope_key)
        if (
            str(owner.get("policy_id")) != expected_policy_id
            or int(owner.get("version") or 0) != int(expected_policy_version)
            or binding is None or binding.binding_id != expected_binding_id
        ):
            raise RuntimeError("CURRENT_OWNER_CHANGED")

        lease = NetworkWriteFencing(self.store).acquire(
            owner_type="EFFECTIVE_SCOPE_WITHDRAWAL", owner_id=scope_key,
            recovery_id=f"WITHDRAW-SCOPE-{scope_key}", ttl_seconds=120,
        )
        try:
            with write_context(owner_type=lease["owner_type"], owner_id=lease["owner_id"], fencing_token=int(lease["fencing_token"])):
                # Re-read after lease acquisition: an update may have committed
                # while the user was confirming the modal.
                owner = PolicyStore(self.store, read_only=True).resolve_managed_desired_for_scope(scope_key)
                binding = QoSResourceAllocator(self.store).get_active_for_scope(scope_key)
                if (
                    owner is None or str(owner.get("policy_id")) != expected_policy_id
                    or int(owner.get("version") or 0) != int(expected_policy_version)
                    or binding is None or binding.binding_id != expected_binding_id
                ):
                    raise RuntimeError("CURRENT_OWNER_CHANGED")

                source_intent_id = str(owner["intent_id"])
                # Exact flow rollback refs are currently stored per execution,
                # not per child scope.  Refuse a destructive parent rollback
                # until the execution ledger can identify this scope alone.
                active_for_source = [
                    row for row in PolicyStore(self.store, read_only=True).resolve_managed_desired_policies().values()
                    if str(row.get("intent_id")) == source_intent_id
                ]
                if len(active_for_source) != 1:
                    raise RuntimeError("SCOPE_EXECUTION_LINEAGE_AMBIGUOUS")

                runtime = self.domain.get_runtime(source_intent_id) or IntentRuntimeState(intent_id=source_intent_id)
                if runtime.lifecycle != IntentLifecycle.WITHDRAW_PENDING_CLEANUP:
                    runtime = self.domain.transition(
                        source_intent_id, IntentLifecycle.WITHDRAW_REQUESTED,
                        actor=actor, reason=reason,
                        data={"effective_scope_id": scope_key, "policy_id": expected_policy_id},
                    )
                cleanup = await self._cleanup_resources(source_intent_id, runtime)
                failures = int(cleanup["path"].get("failed") or 0) + int(cleanup["legacy"].get("failed") or 0)
                failures += sum(1 for item in cleanup["qos"] if item.get("status") not in {"ok", "not_found", "skipped", "ALREADY_ABSENT"})
                if failures:
                    self.store.update(source_intent_id, status="withdraw_pending_cleanup")
                    self.domain.transition(source_intent_id, IntentLifecycle.WITHDRAW_PENDING_CLEANUP, actor=actor,
                        reason="scope withdrawal cleanup incomplete", data={"cleanup": cleanup, "effective_scope_id": scope_key})
                    return {"status": "WITHDRAW_PENDING_CLEANUP", "scope_key": scope_key, "cleanup": cleanup}

                retired = PolicyStore(self.store).withdraw_committed_scope(
                    scope_key, expected_policy_id=expected_policy_id,
                    expected_version=expected_policy_version, intent_id=source_intent_id,
                    runtime={"withdraw_reason": reason, "cleanup": cleanup, "binding_id": expected_binding_id},
                )
                runtime = self._finalize_runtime_bindings(runtime)
                self.store.update(source_intent_id, status="cancelled")
                self.domain.transition(source_intent_id, IntentLifecycle.WITHDRAWN, actor=actor,
                    reason=reason, data={"cleanup": cleanup, "effective_scope_id": scope_key, "policy": retired})
                self._decision(source_intent_id, runtime, {"scope_withdrawal": cleanup, "policy_id": expected_policy_id}, actor)
                return {"status": "WITHDRAWN", "scope_key": scope_key, "policy_id": expected_policy_id,
                        "binding_id": expected_binding_id, "cleanup": cleanup}
        finally:
            NetworkWriteFencing(self.store).release(lease["lease_id"])

    async def _cleanup_resources(
        self, intent_id: str, runtime: IntentRuntimeState
    ) -> dict[str, Any]:
        """Remove only this intent's resources under an already-held lease."""
        cleanup: dict[str, Any] = {
            "path": {"status": "ALREADY_ABSENT"},
            "legacy": {},
            "qos": [],
        }
        executions = self.domain.executions(intent_id)
        current = self._active_execution(executions, runtime)
        if current is not None and current.rollback_refs:
            try:
                cleanup["path"] = await self.execution.rollback(current)
            except Exception as exc:
                cleanup["path"] = {
                    "failed": len(current.rollback_refs),
                    "error": str(exc),
                }

        with self.store._connect() as connection:
            policies = connection.execute(
                "SELECT yang_json, runtime_json FROM policies WHERE intent_id=?", (intent_id,)
            ).fetchall()
        if not policies:
            cleanup["qos"].append({"status": "ALREADY_ABSENT"})
        else:
            from api.qos import clear_qos_policies

            for row in policies:
                try:
                    result = await clear_qos_policies(
                        yang_policy=json.loads(row["yang_json"])
                    )
                    if result.get("status") in {"not_found", "skipped"}:
                        result["status"] = "ALREADY_ABSENT"
                    cleanup["qos"].append(result)
                except Exception as exc:
                    cleanup["qos"].append({"status": "error", "error": str(exc)})

        try:
            from api.worker import _compensate_flows

            # Scope withdrawal must retain its current policy document until
            # ``withdraw_committed_scope`` atomically records WITHDRAWN
            # history.  Failed-job compensation still uses the default that
            # removes abandoned metadata.
            policy_flow_refs: list[dict[str, str]] = []
            for row in policies:
                try:
                    runtime_json = json.loads(row["runtime_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    runtime_json = {}
                for flow in runtime_json.get("installed_flows") or []:
                    if isinstance(flow, dict):
                        policy_flow_refs.append(flow)

            cleanup["legacy"] = await _compensate_flows(
                intent_id,
                self.store,
                delete_policy_metadata=False,
                # ``withdraw`` already established the INTENT_WITHDRAWAL
                # fenced writer context above.  Reuse it for controller/P4
                # compensation instead of trying to acquire a nested global
                # lease, which fencing correctly rejects.
                acquire_lease=False,
                flow_refs=policy_flow_refs,
            )
            if not cleanup["legacy"]:
                cleanup["legacy"] = {"status": "ALREADY_ABSENT"}
        except Exception as exc:
            cleanup["legacy"] = {"failed": 1, "error": str(exc)}
        return cleanup

    def _owned_live_policies(self, intent_id: str) -> list[dict[str, Any]]:
        """Return only durable, currently-live policies owned by this intent."""
        with self.store._connect() as connection:
            rows = connection.execute(
                """SELECT policy_id, traffic_scope_key, version, status
                   FROM policies
                   WHERE intent_id=? AND status IN ('ACTIVE', 'DEGRADED', 'RECONCILING')""",
                (intent_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _retire_owned_policies(
        self, intent_id: str, cleanup: dict[str, Any]
    ) -> dict[str, Any]:
        """Write durable WITHDRAWN history only after southbound cleanup."""
        from persistence.policy_store import PolicyStore

        retired: list[str] = []
        errors: list[dict[str, str]] = []
        store = PolicyStore(self.store)
        for policy in self._owned_live_policies(intent_id):
            try:
                store.withdraw_committed_scope(
                    str(policy["traffic_scope_key"]),
                    expected_policy_id=str(policy["policy_id"]),
                    expected_version=int(policy.get("version") or 0),
                    intent_id=intent_id,
                    runtime={"withdrawal_cleanup": cleanup},
                )
                retired.append(str(policy["policy_id"]))
            except Exception as exc:
                errors.append({"policy_id": str(policy["policy_id"]), "error": str(exc)})
        return {"retired": retired, "failed": len(errors), "errors": errors}

    def _finalize_runtime_bindings(
        self, runtime: IntentRuntimeState
    ) -> IntentRuntimeState:
        """Retire pending work and clear only active runtime references."""
        if runtime.pending_revision is not None:
            pending = self.domain.get_revision(
                runtime.intent_id, runtime.pending_revision
            )
            if pending is not None and pending.status not in {
                RevisionStatus.ACTIVE,
                RevisionStatus.FAILED,
                RevisionStatus.SUPERSEDED,
                RevisionStatus.WITHDRAWN,
            }:
                self.domain.finish_revision(
                    pending,
                    runtime,
                    status=RevisionStatus.WITHDRAWN,
                    error="intent withdrawn before pending revision activation",
                )
                runtime = self.domain.get_runtime(runtime.intent_id) or runtime
            else:
                runtime.pending_revision = None
        runtime.active_plan_id = None
        runtime.active_execution_id = None
        runtime.active_policy_ids = []
        runtime.current_path = []
        runtime.context_id = None
        runtime.grace_until = None
        runtime.cooldown_until = None
        return self.domain.save_runtime(runtime)

    @staticmethod
    def _active_execution(
        executions: list[dict[str, Any]], runtime: IntentRuntimeState
    ) -> IntentExecutionResult | None:
        for item in executions:
            if item.get("execution_id") == runtime.active_execution_id:
                return IntentExecutionResult.model_validate(item)
        for item in executions:
            if item.get("status") == ExecutionStatus.COMPLETED.value:
                return IntentExecutionResult.model_validate(item)
        return None

    def _decision(
        self,
        intent_id: str,
        runtime: IntentRuntimeState,
        output: dict[str, Any],
        actor: str,
    ) -> None:
        self.domain.save_decision(
            AgentDecision(
                intent_id=intent_id,
                intent_revision=runtime.pending_revision or runtime.current_revision,
                agent_id="intent_execution",
                decision_type="WITHDRAW_CLEANUP",
                plan_id=runtime.active_plan_id,
                context_id=runtime.context_id,
                output=output,
                facts=[
                    "path, policy and QoS cleanup results are reported without suppression"
                ],
                rationale_summary=f"withdrawal requested by {actor}",
            )
        )

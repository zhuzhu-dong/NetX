"""Deterministic execution of validated IntentPlan path actions.

The LLM-facing agents can select an approved candidate, but only this adapter
may translate that IR into ONOS/P4 rules.  It keeps real controller flow IDs,
performs readback, rolls a partial install back, and removes the superseded
execution only after the new path is verified.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from models.intent_domain import (
    DeviceExecutionReceipt,
    ExecutionStatus,
    IntentExecutionResult,
    IntentPlanIR,
    MigrationProof,
    PlanValidationStatus,
)


TopologyFetcher = Callable[[], Awaitable[dict[str, Any]]]
PrepareCommit = Callable[[list[str]], Awaitable[list[dict[str, Any]]]]
PrecommitVerify = Callable[[list[str]], Awaitable[dict[str, Any]]]
PostcommitVerify = Callable[[list[str]], Awaitable[dict[str, Any]]]
CommitResources = Callable[
    [list[str], list[dict[str, Any]]], Awaitable[dict[str, Any]]
]
RollbackResources = Callable[
    [list[str], list[dict[str, Any]], str], Awaitable[dict[str, Any]]
]


class ExecutionSkippedNoop(ValueError):
    """Raised before any controller write when healing has no desired-state delta."""

    code = "EXECUTION_SKIPPED_NOOP"


class IntentExecutionAgent:
    agent_id = "intent_execution"

    def __init__(
        self,
        *,
        topology_fetcher: TopologyFetcher | None = None,
        onos_client: Any | None = None,
        flow_adapter: Any | None = None,
    ) -> None:
        self._topology_fetcher = topology_fetcher
        self._onos_client = onos_client
        self._flow_adapter = flow_adapter
        self.readback_attempts = max(
            1, int(os.getenv("IDN_INTENT_EXECUTION_READBACK_ATTEMPTS", "20"))
        )
        self.readback_delay = max(
            0.05, float(os.getenv("IDN_INTENT_EXECUTION_READBACK_DELAY_SECONDS", "2"))
        )

    async def execute(
        self,
        plan: IntentPlanIR,
        *,
        previous: IntentExecutionResult | None = None,
        generation: int = 1,
        prepare_commit: PrepareCommit | None = None,
        precommit_verify: PrecommitVerify | None = None,
        postcommit_verify: PostcommitVerify | None = None,
        commit_resources: CommitResources | None = None,
        rollback_resources: RollbackResources | None = None,
    ) -> IntentExecutionResult:
        # This is deliberately duplicated from the orchestration gate.  A
        # caller may never turn a new plan ID into a controller/P4/TC write
        # when its desired data-plane effect matches the committed plan.
        if plan.plan_diff is not None and plan.plan_diff.is_noop():
            raise ExecutionSkippedNoop(
                "EXECUTION_SKIPPED_NOOP: PlanDiff reports no desired data-plane change"
            )
        candidate = next(
            (item for item in plan.candidates if item.candidate_id == plan.selected_candidate_id),
            None,
        )
        if plan.validation_status != PlanValidationStatus.VALID:
            raise ValueError("execution requires a deterministically VALID IntentPlan")
        if candidate is None or len(candidate.path) < 3:
            raise ValueError("execution requires a selected host-to-host candidate path")

        result = IntentExecutionResult(
            intent_id=plan.intent_id,
            plan_id=plan.plan_id,
            context_id=plan.context_id,
            selected_candidate_id=candidate.candidate_id,
            selected_path=list(candidate.path),
            previous_execution_id=previous.execution_id if previous else None,
        )
        self._phase(result, ExecutionStatus.PREPARING, "validated plan accepted")
        installed: dict[str, list[str]] = {}
        topology: dict[str, Any] = {}
        old_cleanup_started = False
        try:
            topology = await self._fetch_topology()
            if topology.get("status") != "ok":
                raise RuntimeError("runtime Mininet topology is unavailable")

            from api.baseline import build_explicit_path_write_requests

            # A later generation has higher precedence during make-before-break.
            base_priority = int(os.getenv("IDN_INTENT_PATH_PRIORITY", "30000"))
            priority = min(65535, base_priority + max(1, generation))
            requests = build_explicit_path_write_requests(
                topology, list(candidate.path), priority=priority
            )
            rules_by_device = self._adapter().batch_convert(requests)
            if not rules_by_device:
                raise RuntimeError("compiler produced no path flow rules")

            self._phase(result, ExecutionStatus.INSTALLING, "installing compiled ONOS flows")
            client = self._client()
            for device_id, rules in rules_by_device.items():
                response = await client.post_flows(
                    device_id, rules, correlation_id=result.execution_id
                )
                flow_ids = [str(value) for value in response.get("flow_ids") or [] if value]
                errors = [str(value) for value in response.get("errors") or []]
                receipt = DeviceExecutionReceipt(
                    device_id=device_id,
                    requested_flows=len(rules),
                    installed_flows=int(response.get("flows_posted") or 0),
                    flow_ids=flow_ids,
                    errors=errors,
                )
                result.device_receipts.append(receipt)
                installed[device_id] = flow_ids
                result.rollback_refs.extend(
                    {"device_id": device_id, "flow_id": flow_id}
                    for flow_id in flow_ids
                )
                if receipt.installed_flows != receipt.requested_flows or errors:
                    raise RuntimeError(
                        f"ONOS installed {receipt.installed_flows}/{receipt.requested_flows} "
                        f"path rules on {device_id}: {errors}"
                    )

            self._phase(result, ExecutionStatus.READING_BACK, "reading installed flows from ONOS")
            self._phase(result, ExecutionStatus.VERIFYING_CONFIG, "polling controller state for every installed flow ID")
            await self._verify_readback(result)
            result.flows_installed = sum(item.installed_flows for item in result.device_receipts)
            result.config_verified = all(item.verified for item in result.device_receipts)
            if not result.config_verified:
                raise RuntimeError("ONOS readback did not verify every installed path rule")

            # Path-coupled resources (for example the TC bottleneck binding)
            # are part of the same make-before-break transaction.  They must
            # be installed and read back before the old path is removed.
            if prepare_commit is not None:
                result.device_config_receipts = await prepare_commit(list(candidate.path))

            if precommit_verify is not None:
                result.precommit_verification = await precommit_verify(
                    list(candidate.path)
                )
            result.migration_proofs = self._migration_proofs(
                topology,
                list(candidate.path),
                result,
            )
            passed_proofs = [item for item in result.migration_proofs if item.passed]
            # Formal violation-driven migrations always carry parent_plan_id.
            # Keeping this marker explicit avoids treating a generic resource
            # refresh with a previous receipt as proof of a path migration.
            is_migration = bool(plan.parent_plan_id)
            result.migration_verified = len(passed_proofs) >= (2 if is_migration else 1)
            if is_migration and not result.migration_verified:
                raise RuntimeError(
                    "migration proof contract failed: two independent path proofs are required"
                )

            self._phase(result, ExecutionStatus.COMMITTING, "new path verified; cleaning superseded path")
            if commit_resources is not None:
                result.previous_cleanup["resource_commit"] = await commit_resources(
                    list(candidate.path), result.device_config_receipts
                )
            if previous and previous.rollback_refs:
                old_cleanup_started = True
                old_flow_cleanup = await self.rollback(previous)
                result.previous_cleanup = {
                    **result.previous_cleanup,
                    **old_flow_cleanup,
                }
                # The new generation has a higher table priority.  A failed
                # best-effort cleanup is recorded for the reconciler, but must
                # not tear down the already verified replacement path.
                if result.previous_cleanup.get("failed"):
                    result.phase_history.append({
                        "status": ExecutionStatus.COMMITTING.value,
                        "reason": "replacement committed; superseded-flow cleanup requires reconciliation",
                        "at": datetime.now(timezone.utc).isoformat(),
                    })

                # ``ipv4_routing`` is a pure LPM table.  ONOS can retain a
                # priority-10 baseline flow for the exact same physical /32
                # key even though BMv2 has only one entry for that key.  Remove
                # only those exact-key baseline controller flows, then reassert
                # the replacement generation.  Other applications, priorities
                # and selectors are deliberately outside this transaction.
                baseline_cleanup = await self._cleanup_same_key_baseline_rules(
                    rules_by_device, installed
                )
                result.previous_cleanup["same_key_baseline_cleanup"] = baseline_cleanup
                if baseline_cleanup.get("failed"):
                    raise RuntimeError(
                        "same-key baseline cleanup failed before replacement reassert"
                    )
                result.previous_cleanup["replacement_reassert"] = await self._reassert_rules(
                    result, rules_by_device, installed
                )
                await self._verify_readback(result)
                result.config_verified = all(
                    item.verified for item in result.device_receipts
                )
                if not result.config_verified:
                    raise RuntimeError(
                        "replacement path reassert readback failed after old-flow cleanup"
                    )

            if postcommit_verify is not None:
                result.postcommit_verification = await postcommit_verify(
                    list(candidate.path)
                )
                result.migration_proofs = self._migration_proofs(
                    topology, list(candidate.path), result
                )
                passed_sources = {
                    item.source for item in result.migration_proofs if item.passed
                }
                if is_migration and (
                    "INT_PATH_TRACE" not in passed_sources
                    or not (passed_sources - {"INT_PATH_TRACE"})
                ):
                    result.migration_verified = False
                    raise RuntimeError(
                        "post-commit migration proof contract failed: trusted INT path "
                        "plus one independent readback are required"
                    )
            result.migration_verified = len(
                [item for item in result.migration_proofs if item.passed]
            ) >= (2 if is_migration else 1)
            result.flows_installed = sum(
                item.installed_flows for item in result.device_receipts
            )
            result.commit_verified = True

            self._phase(result, ExecutionStatus.COMPLETED, "configuration install and readback completed")
            result.completed_at = datetime.now(timezone.utc)
            return result
        except Exception as exc:
            result.error = str(exc)
            resource_cleanup: dict[str, Any] = {
                "rolled_back": 0,
                "failed": 0,
                "reason": "no path-coupled resource was prepared",
            }
            if rollback_resources is not None and result.device_config_receipts:
                try:
                    resource_cleanup = await rollback_resources(
                        list(candidate.path), result.device_config_receipts, str(exc)
                    )
                except Exception as rollback_exc:
                    resource_cleanup = {
                        "rolled_back": 0,
                        "failed": 1,
                        "error": str(rollback_exc),
                    }
            cleanup = await self._rollback_refs(installed)
            restore_failed = False
            old_path_restore: dict[str, Any] | None = None
            if old_cleanup_started and previous and previous.selected_path and topology:
                try:
                    old_path_restore = await self._restore_path(
                        topology,
                        list(previous.selected_path),
                        generation=max(1, generation - 1),
                    )
                except Exception as restore_exc:
                    restore_failed = True
                    old_path_restore = {
                        "status": "FAILED",
                        "error": str(restore_exc),
                    }
            result.previous_cleanup = {
                **result.previous_cleanup,
                "new_install_rollback": cleanup,
                "new_resource_rollback": resource_cleanup,
            }
            if old_path_restore is not None:
                result.previous_cleanup["old_path_restore"] = old_path_restore
            final_status = (
                ExecutionStatus.ROLLED_BACK
                if cleanup.get("failed", 0) == 0
                and resource_cleanup.get("failed", 0) == 0
                and not restore_failed
                else ExecutionStatus.FAILED
            )
            self._phase(result, final_status, str(exc))
            result.completed_at = datetime.now(timezone.utc)
            return result

    async def rollback(self, execution: IntentExecutionResult) -> dict[str, Any]:
        by_device: dict[str, list[str]] = {}
        for ref in execution.rollback_refs:
            device_id = str(ref.get("device_id") or "")
            flow_id = str(ref.get("flow_id") or "")
            if device_id and flow_id:
                by_device.setdefault(device_id, []).append(flow_id)
        return await self._rollback_refs(by_device)

    async def _verify_readback(self, result: IntentExecutionResult) -> None:
        pending = {item.device_id: set(item.flow_ids) for item in result.device_receipts}
        states: dict[str, dict[str, str]] = {item.device_id: {} for item in result.device_receipts}
        client = self._client()
        for attempt in range(self.readback_attempts):
            for receipt in result.device_receipts:
                if not pending[receipt.device_id]:
                    continue
                payload = await client.get_flows(receipt.device_id)
                for flow in payload.get("flows") or []:
                    flow_id = str(flow.get("id") or "")
                    if flow_id in pending[receipt.device_id]:
                        state = str(flow.get("state") or "UNKNOWN").upper()
                        states[receipt.device_id][flow_id] = state
                        if state == "ADDED":
                            pending[receipt.device_id].discard(flow_id)
            if not any(pending.values()):
                break
            if attempt + 1 < self.readback_attempts:
                await asyncio.sleep(self.readback_delay)

        for receipt in result.device_receipts:
            receipt.readback_states = states[receipt.device_id]
            receipt.verified = not pending[receipt.device_id]
            if pending[receipt.device_id]:
                receipt.errors.append(
                    "readback missing/non-ADDED flow IDs: "
                    + ", ".join(sorted(pending[receipt.device_id]))
                )

    async def _rollback_refs(self, by_device: dict[str, list[str]]) -> dict[str, Any]:
        summary: dict[str, Any] = {"deleted": 0, "failed": 0, "devices": {}}
        client = self._client()
        for device_id, flow_ids in by_device.items():
            unique = list(dict.fromkeys(value for value in flow_ids if value))
            if not unique:
                continue
            try:
                response = await client.delete_flows(
                    device_id, unique, correlation_id="intent-execution-rollback"
                )
                deleted = int(response.get("flows_deleted") or 0)
                failed = max(len(unique) - deleted, 0)
                summary["deleted"] += deleted
                summary["failed"] += failed
                summary["devices"][device_id] = response
            except Exception as exc:
                summary["failed"] += len(unique)
                summary["devices"][device_id] = {"error": str(exc)}
        return summary

    async def _reassert_rules(
        self,
        result: IntentExecutionResult,
        rules_by_device: dict[str, list[dict[str, Any]]],
        installed: dict[str, list[str]],
    ) -> dict[str, Any]:
        """Reassert replacement LPM entries after deleting same-key old flows."""
        summary: dict[str, Any] = {"posted": 0, "devices": {}}
        receipts = {item.device_id: item for item in result.device_receipts}
        client = self._client()
        for device_id, rules in rules_by_device.items():
            response = await client.post_flows(
                device_id, rules, correlation_id=result.execution_id
            )
            flow_ids = [str(value) for value in response.get("flow_ids") or [] if value]
            errors = [str(value) for value in response.get("errors") or []]
            posted = int(response.get("flows_posted") or 0)
            if posted != len(rules) or errors or len(flow_ids) != len(rules):
                raise RuntimeError(
                    f"replacement reassert installed {posted}/{len(rules)} path rules "
                    f"on {device_id}: {errors}"
                )
            receipt = receipts[device_id]
            for flow_id in flow_ids:
                if flow_id not in receipt.flow_ids:
                    receipt.flow_ids.append(flow_id)
                    installed.setdefault(device_id, []).append(flow_id)
                    result.rollback_refs.append(
                        {"device_id": device_id, "flow_id": flow_id}
                    )
            receipt.installed_flows += posted
            receipt.verified = False
            summary["posted"] += posted
            summary["devices"][device_id] = {"flow_ids": flow_ids, "posted": posted}
        return summary

    @staticmethod
    def _flow_key(rule: dict[str, Any]) -> tuple[str, str] | None:
        """Return the physical table key represented by an ONOS PI rule."""
        table_id = rule.get("tableId")
        selector = rule.get("selector")
        if table_id is None or not isinstance(selector, dict):
            return None
        return (
            str(table_id),
            json.dumps(selector, sort_keys=True, separators=(",", ":")),
        )

    async def _cleanup_same_key_baseline_rules(
        self,
        rules_by_device: dict[str, list[dict[str, Any]]],
        installed: dict[str, list[str]],
    ) -> dict[str, Any]:
        """Delete only priority-10 baseline flows colliding with new LPM keys."""
        from api.baseline import BASELINE_PRIORITY

        summary: dict[str, Any] = {
            "matched": 0,
            "deleted": 0,
            "failed": 0,
            "devices": {},
        }
        client = self._client()
        for device_id, rules in rules_by_device.items():
            replacement_keys = {
                key for key in (self._flow_key(rule) for rule in rules) if key is not None
            }
            if not replacement_keys:
                summary["devices"][device_id] = {"status": "NO_IDENTIFIABLE_KEYS"}
                continue
            payload = await client.get_flows(device_id)
            replacement_ids = set(installed.get(device_id) or [])
            baseline_ids: list[str] = []
            for flow in payload.get("flows") or []:
                flow_id = str(flow.get("id") or "")
                try:
                    priority = int(flow.get("priority"))
                except (TypeError, ValueError):
                    continue
                if (
                    flow_id
                    and flow_id not in replacement_ids
                    and priority == BASELINE_PRIORITY
                    and str(flow.get("state") or "").upper() == "ADDED"
                    and self._flow_key(flow) in replacement_keys
                ):
                    baseline_ids.append(flow_id)
            baseline_ids = list(dict.fromkeys(baseline_ids))
            if not baseline_ids:
                summary["devices"][device_id] = {"status": "ALREADY_ABSENT"}
                continue
            summary["matched"] += len(baseline_ids)
            try:
                response = await client.delete_flows(
                    device_id,
                    baseline_ids,
                    correlation_id="intent-execution-same-key-baseline-cleanup",
                )
                deleted = int(response.get("flows_deleted") or 0)
                failed = max(len(baseline_ids) - deleted, 0)
                summary["deleted"] += deleted
                summary["failed"] += failed
                summary["devices"][device_id] = {
                    "flow_ids": baseline_ids,
                    "response": response,
                }
            except Exception as exc:
                summary["failed"] += len(baseline_ids)
                summary["devices"][device_id] = {
                    "flow_ids": baseline_ids,
                    "error": str(exc),
                }
        return summary

    async def _restore_path(
        self,
        topology: dict[str, Any],
        selected_path: list[str],
        *,
        generation: int,
    ) -> dict[str, Any]:
        """Best-effort restoration when a post-cleanup proof fails."""
        from api.baseline import build_explicit_path_write_requests

        base_priority = int(os.getenv("IDN_INTENT_PATH_PRIORITY", "30000"))
        requests = build_explicit_path_write_requests(
            topology,
            selected_path,
            priority=min(65535, base_priority + max(1, generation)),
        )
        rules_by_device = self._adapter().batch_convert(requests)
        client = self._client()
        summary: dict[str, Any] = {"status": "RESTORED", "posted": 0, "devices": {}}
        for device_id, rules in rules_by_device.items():
            response = await client.post_flows(
                device_id, rules, correlation_id="intent-execution-old-path-restore"
            )
            posted = int(response.get("flows_posted") or 0)
            errors = [str(value) for value in response.get("errors") or []]
            summary["posted"] += posted
            summary["devices"][device_id] = {
                "flow_ids": [str(value) for value in response.get("flow_ids") or []],
                "posted": posted,
                "errors": errors,
            }
            if posted != len(rules) or errors:
                summary["status"] = "PARTIAL"
        if summary["status"] != "RESTORED":
            raise RuntimeError(f"old path restoration incomplete: {summary}")
        return summary

    @classmethod
    def _migration_proofs(
        cls,
        topology: dict[str, Any],
        selected_path: list[str],
        result: IntentExecutionResult,
    ) -> list[MigrationProof]:
        """Build independent, explicit proof records; never infer success from logs."""
        switches = {
            str(item.get("name")): str(item.get("device_id"))
            for item in topology.get("switches") or []
            if item.get("name") and item.get("device_id")
        }
        expected_switch_path = [node for node in selected_path if node in switches]
        if not expected_switch_path and len(selected_path) > 2:
            expected_switch_path = list(selected_path[1:-1])
        expected_devices = [switches[node] for node in expected_switch_path if node in switches]
        verified_devices = [
            item.device_id for item in result.device_receipts if item.verified
        ]
        onos_passed = bool(verified_devices) and (
            not expected_devices or set(expected_devices).issubset(set(verified_devices))
        )
        proofs = [
            MigrationProof(
                source="ONOS_FLOW_READBACK",
                expected_path=list(expected_switch_path or selected_path),
                observed_path=list(expected_switch_path or selected_path)
                if onos_passed
                else [],
                passed=onos_passed,
                evidence_refs=[
                    f"onos-flow:{item.device_id}:{flow_id}"
                    for item in result.device_receipts
                    for flow_id in item.flow_ids
                ],
                details={
                    "expected_device_ids": expected_devices,
                    "verified_device_ids": verified_devices,
                },
            )
        ]

        payloads = [
            result.postcommit_verification,
            result.precommit_verification,
            *result.device_config_receipts,
        ]
        qos_path = cls._find_path(payloads, ("qos_binding", "current_path"))
        expected = list(expected_switch_path or selected_path)
        qos_passed = bool(qos_path) and qos_path == expected
        proofs.append(
            MigrationProof(
                source="TC_QOS_BINDING_READBACK",
                expected_path=expected,
                observed_path=qos_path,
                passed=qos_passed,
                evidence_refs=["precommit:path-coupled-qos-readback"],
            )
        )
        int_path = cls._find_path(payloads, ("int_analysis", "switch_path"))
        if int_path:
            proofs.append(
                MigrationProof(
                    source="INT_PATH_TRACE",
                    expected_path=expected,
                    observed_path=int_path,
                    passed=int_path == expected,
                    evidence_refs=["precommit:int-hop-records"],
                )
            )
        return proofs

    @classmethod
    def _find_path(cls, value: Any, key_path: tuple[str, str]) -> list[str]:
        if isinstance(value, list):
            for item in value:
                found = cls._find_path(item, key_path)
                if found:
                    return found
            return []
        if not isinstance(value, dict):
            return []
        nested = value.get(key_path[0])
        if isinstance(nested, dict) and isinstance(nested.get(key_path[1]), list):
            return [str(item) for item in nested[key_path[1]]]
        for item in value.values():
            found = cls._find_path(item, key_path)
            if found:
                return found
        return []

    async def _fetch_topology(self) -> dict[str, Any]:
        if self._topology_fetcher is not None:
            return await self._topology_fetcher()
        from api.topology import fetch_mininet_topology

        return await fetch_mininet_topology()

    def _client(self) -> Any:
        if self._onos_client is None:
            from mcp_servers.onos_server import get_onos_client

            self._onos_client = get_onos_client()
        return self._onos_client

    def _adapter(self) -> Any:
        if self._flow_adapter is None:
            from mcp_servers.flow_adapter import FlowAdapter

            self._flow_adapter = FlowAdapter()
        return self._flow_adapter

    @staticmethod
    def _phase(
        result: IntentExecutionResult, status: ExecutionStatus, reason: str
    ) -> None:
        result.status = status
        result.phase_history.append(
            {
                "status": status.value,
                "reason": reason,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )

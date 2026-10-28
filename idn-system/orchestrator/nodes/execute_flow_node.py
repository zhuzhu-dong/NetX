"""
orchestrator/nodes/execute_flow_node.py
========================================

流表下发节点 (Execute Flow Node) —— STAV 闭环的 ACT 阶段核心。

# 系统角色
将验证通过的 FlowTablePlan 转换为 ONOS FlowRule JSON，
并通过 FlowTransaction 事务性下发到数据平面。

# 执行流程
1. 检查是否有待下发流表项（空计划 → SKIPPED）
2. 采集下发前遥测快照（pre_execution_telemetry）
3. FlowAdapter 将 WriteRequest → ONOS FlowRule JSON
4. FlowTransaction 逐设备事务性下发：
   - 每台设备写入成功后立即增量持久化 installed_flows
   - 任何设备失败触发整体回滚
5. 下发成功后应用 QoS 策略和自主自愈

# 输入/输出
- 输入：state.verified_flow_table_plan（或 state.flow_table_plan）
- 输出：state.execution_result（含状态、已确认/失败设备列表）
"""
from __future__ import annotations

import logging
import time
import os
from datetime import datetime
from typing import Any

from models.agent_state import (
    ExecutionLogEntry,
    ExecutionStage,
    GlobalAgentState,
    StageStatus,
)
from models.intent import IntentStatus
from ..state_generations import initialize_transient_state_for_policy_reuse

from .base_node import BaseNode


def _raise_if_execution_failpoint(stage: str) -> None:
    """Raise an explicit, default-off execution test fault at ``stage``.

    This hook is generic rather than keyed to a specific service or intent.
    Production execution is unchanged unless validation explicitly sets
    ``IDN_EXECUTION_FAILPOINT`` to the named stage.
    """
    configured = {
        value.strip().lower()
        for value in os.getenv("IDN_EXECUTION_FAILPOINT", "").split(",")
        if value.strip()
    }
    if stage.lower() in configured:
        raise RuntimeError(f"EXECUTION_FAILPOINT:{stage}")

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 基础节点类
# ──────────────────────────────────────────────


class ExecuteFlowNode(BaseNode):
    """
    流表下发节点 (P4RuntimeExecutor)
    职责: 将验证通过的 WriteRequest 转为南向规则，并通过
          FlowDelivery 策略下发到 ONOS 或演示后端。
          
    输入: state.flow_table_plan
    输出: state.execution_result
    """
    agent_id = "p4_runtime_executor"
    stage = ExecutionStage.EXECUTE

    async def _apply_qos_with_execution_lease(
        self,
        state: GlobalAgentState,
        *,
        operation: str,
    ) -> dict[str, Any]:
        """Apply TC through the same fenced single-writer path as recovery.

        A policy execution may follow a completed restart recovery, when no
        recovery lease remains active.  The QoS actuator correctly rejects an
        unscoped write in that case; acquiring a short-lived execution lease
        here gives the ordinary workflow a real owner/token instead of
        bypassing the guard.
        """
        if operation.upper() == "VERIFY":
            # Readback is deliberately lease-free: it must not allocate or
            # mutate TC state while confirming a reusable policy.
            from api.qos import verify_qos_policy

            return await verify_qos_policy(state.yang_draft)

        from api.qos import apply_qos_policy
        from infra.network_write_fencing import NetworkWriteFencing, write_context

        owner_id = str(state.intent_id or state.run_id or "workflow")
        lease = NetworkWriteFencing().acquire(
            owner_type="WORKFLOW_EXECUTION",
            owner_id=owner_id,
            recovery_id=f"EXEC-{str(state.run_id or owner_id)}",
            ttl_seconds=120,
        )
        try:
            with write_context(
                owner_type=lease["owner_type"],
                owner_id=lease["owner_id"],
                fencing_token=int(lease["fencing_token"]),
            ):
                return await apply_qos_policy(state.yang_draft, operation=operation)
        finally:
            NetworkWriteFencing().release(lease["lease_id"])

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        admission = (state.conflict_report.admission_decision if state.conflict_report else {}) or {}
        if admission and admission.get("decision") != "ADMITTED":
            state.current_status = IntentStatus.UNSATISFIABLE
            state.execution_result = {
                "status": "NOT_RUN", "executor_status": "NOT_RUN",
                "reason_code": admission.get("reason_code", "INSUFFICIENT_CAPACITY"),
                "admission_decision": admission, "data_plane_changed": False,
            }
            self._current_policy_result(state)["execution_result"] = state.execution_result
            return self._end_log(
                state, start, StageStatus.SKIPPED,
                f"EXECUTION_NOT_RUN: {state.execution_result['reason_code']}",
            )
        plan = state.verified_flow_table_plan or state.flow_table_plan or {}
        write_requests = plan.get("write_requests", [])
        operation = str(plan.get("policy_operation") or "CREATE")
        from infra.healing_plan_gate import is_repeated_noop

        prior_execution = dict(state.execution_result or {})
        repeated_noop, effect_signature = is_repeated_noop(
            flow_plan=plan,
            yang_draft=state.yang_draft,
            previous_execution=prior_execution,
        )
        if state.react_state.retry_count > 0 and repeated_noop:
            state.execution_result = {
                "status": "SKIPPED",
                "executor_status": "SKIPPED_NOOP",
                "reason_code": "NO_OP_PLAN",
                "no_change": True,
                "data_plane_changed": False,
                "desired_effect_signature": effect_signature,
                "previous_execution_id": prior_execution.get("execution_id"),
                "installed_flows": list(prior_execution.get("installed_flows") or []),
            }
            self._current_policy_result(state)["execution_result"] = state.execution_result
            return self._end_log(
                state,
                start,
                StageStatus.SUCCESS,
                "HEALING_PLAN_SKIPPED: NO_OP_PLAN; desired data-plane effect is unchanged",
            )

        state.pre_execution_telemetry = await self._collect_pre_execution_telemetry(state)
        self._current_policy_result(state)["pre_execution_telemetry"] = state.pre_execution_telemetry

        policy_store = None
        staged = None
        old_policy = None
        old_runtime: dict[str, Any] = {}
        scope_key = str(plan.get("traffic_scope_key") or "")

        try:
            # QoS state is staged before touching runtime.  The active JSON is
            # retained in the row, providing an exact rollback target.
            from models.yang_policy import ActionType, YANGPolicy

            action_type = state.yang_draft.network_policy.action.action_type if state.yang_draft else None
            if action_type in {ActionType.SET_PRIORITY, ActionType.RATE_LIMIT}:
                from persistence.policy_store import PolicyStore
                from models.policy_update import traffic_scope_key

                policy_store = PolicyStore()
                staged = policy_store.stage(state.yang_draft, state.intent_id, state.conflict_report)
                scope_key = staged.get("traffic_scope_key") or traffic_scope_key(state.yang_draft).key
                if int(staged.get("version") or 0) > 0:
                    import json

                    old_policy = YANGPolicy(**json.loads(staged["yang_json"]))
                    old_runtime = json.loads(staged.get("runtime_json") or "{}")

            classifier_reused = False
            if (
                write_requests
                and operation in {"VERIFY_EXISTING", "MODIFY_QOS_RESOURCE"}
            ):
                try:
                    from api.qos import verify_p4_classifier

                    # This is a *reuse decision*, not final delivery
                    # verification.  After an ONOS/BMv2 restart the durable
                    # policy can name a classifier that no longer exists.
                    # Waiting through the full verification window before
                    # repairing it consumes the execute-node budget and can
                    # leave P4 written but TC unapplied.  Fail fast here;
                    # the normal post-write verifier retains the longer,
                    # controller-convergence-aware deadline.
                    reusable_readback = await verify_p4_classifier(
                        state.yang_draft,
                        old_runtime,
                        timeout_seconds=float(
                            os.getenv("IDN_EXISTING_CLASSIFIER_REUSE_TIMEOUT_SECONDS", "5")
                        ),
                        poll_interval_seconds=0.25,
                    )
                    if int(reusable_readback.get("matching_entry_count") or 0) != 1:
                        raise RuntimeError(
                            "CLASSIFIER_EXACT_SELECTOR_DUPLICATE_REQUIRES_SUPERSESSION:"
                            f"{reusable_readback.get('duplicate_flow_ids') or []}"
                        )
                    write_requests = []
                    classifier_reused = True
                except Exception as classifier_exc:
                    logger.warning(
                        "[%s] existing classifier is not reusable; repairing through ONOS: %s",
                        self.agent_id,
                        classifier_exc,
                    )

            # Idempotent reuse and bandwidth-only modify deliberately have no
            # P4 writes.  They are successful executor operations only after
            # the TC resource has been read back (or updated and read back).
            if not write_requests:
                if operation == "VERIFY_EXISTING":
                    # Reuse is an execution event, never permission to carry
                    # forward a prior run's readback, samples, violation or
                    # diagnostic hypothesis.  VerifyStatusNode will perform
                    # the new readback before assurance starts.
                    initialize_transient_state_for_policy_reuse(state)
                qos_result = await self._apply_qos_with_execution_lease(
                    state,
                    operation="APPLY" if operation != "VERIFY_EXISTING" else "VERIFY",
                )
                qos_repaired = False
                if operation == "VERIFY_EXISTING" and not qos_result.get("config_applied"):
                    # A reusable classifier does not prove that its paired
                    # switch-egress resource survived.  Recover the missing
                    # TC half through the normal fenced actuator instead of
                    # declaring a readback-only reuse successful.
                    qos_result = await self._apply_qos_with_execution_lease(
                        state, operation="APPLY"
                    )
                    qos_repaired = True
                if not qos_result.get("config_applied"):
                    raise RuntimeError(
                        "QOS_READBACK_OR_RECOVERY_FAILED:"
                        f"{qos_result.get('reason_code') or qos_result.get('error') or qos_result}"
                    )
                # The QoS actuator has completed, while the desired policy is
                # still staged.  A validation-only fault here must exercise
                # the normal outer compensation path.
                _raise_if_execution_failpoint("after_qos_apply")
                state.execution_result = {
                    "status": "SUCCESS",
                    "executor_status": (
                        "RECONCILED" if qos_repaired else
                        "REUSED" if operation == "VERIFY_EXISTING" else "UPDATED"
                    ),
                    "operation": operation,
                    "no_change": operation == "VERIFY_EXISTING" and not qos_repaired,
                    "data_plane_changed": qos_repaired or (
                        operation != "VERIFY_EXISTING" and bool(qos_result.get("config_applied"))
                    ),
                    "desired_effect_signature": effect_signature,
                    "config_applied": bool(qos_result.get("config_applied")),
                    "qos_enforcement": qos_result,
                    "traffic_scope_key": scope_key,
                    "existing_policy_id": plan.get("existing_policy_id"),
                    "policy_change": plan.get("policy_change") or {},
                    "installed_flows": list(old_runtime.get("installed_flows") or []),
                    "delivered_rules": old_runtime.get("delivered_rules") or {},
                    "classifier_reused": classifier_reused,
                    "classifier_operation": "REUSED" if classifier_reused else "REPAIRED",
                    "classifier_ownership": ([{
                        "state": "CURRENT",
                        "operation": "REUSED_DURABLE_RUNTIME",
                        "flow_ids": [str(item.get("flow_id")) for item in old_runtime.get("installed_flows") or []
                                     if item.get("flow_id")],
                    }] if classifier_reused else []),
                    "tc_operation": (
                        "REPAIRED" if qos_repaired else
                        "VERIFIED" if operation == "VERIFY_EXISTING" else "UPDATED"
                    ),
                    # This is an execution-time accounting record, not an
                    # inference from the final dataplane.  It makes a
                    # semantic reuse auditable: readback and traffic/counter
                    # collection are intentionally absent because they are
                    # not configuration mutations.
                    "southbound_mutations": {
                        "onos_post": 0,
                        "onos_delete": 0,
                        "p4_insert": 0,
                        "p4_modify": 0,
                        "p4_delete": 0,
                        "tc_qdisc_apply": 0,
                        "tc_class_apply": 1 if qos_repaired else 0,
                        "tc_filter_apply": 1 if qos_repaired else 0,
                        "tc_qdisc_delete": 0,
                        "tc_class_delete": 0,
                        "tc_filter_delete": 0,
                        "ingress_apply": 0,
                        "resource_allocation_mutation": 1 if qos_repaired else 0,
                        "classification": (
                            "REPAIR_REQUIRED" if qos_repaired else
                            "NO_NETWORK_CONFIGURATION_MUTATION"
                        ),
                    },
                    "rollback_context": {
                        "old_policy": old_policy.model_dump(mode="json") if old_policy else None,
                        "old_runtime": old_runtime,
                    },
                    "timestamp": datetime.utcnow().isoformat(),
                }
                self._current_policy_result(state)["execution_result"] = state.execution_result
                label = (
                    "恢复缺失的 QoS 资源"
                    if qos_repaired
                    else "复用并验证现有策略"
                    if operation == "VERIFY_EXISTING"
                    else "原地更新 QoS 资源"
                )
                return self._end_log(state, start, StageStatus.SUCCESS, f"{label}: {scope_key}")

            # ── Step 1: 选择 southbound delivery 策略 ──
            from mcp_servers.flow_delivery import FlowDeliveryFactory

            delivery = FlowDeliveryFactory.create()

            # ── Step 2: WriteRequest → ONOS FlowRule JSON ──
            from mcp_servers.flow_adapter import FlowAdapter
            from models.flow_table import WriteRequest

            adapter = FlowAdapter()

            # Reconstruct WriteRequest model objects from serialized dicts
            write_req_objects: list[WriteRequest] = []
            for wr_dict in write_requests:
                try:
                    write_req_objects.append(WriteRequest(**wr_dict))
                except Exception as parse_err:
                    logger.error(
                        "[%s] 无法解析 WriteRequest: %s", self.agent_id, parse_err,
                    )
                    continue

            # Convert to ONOS FlowRule JSON, grouped by device
            device_rules = adapter.batch_convert(write_req_objects)

            # A QoS selector has one canonical P4 classifier identity.  Before
            # appending a FlowRule, inspect the authoritative controller view.
            # This catches an interrupted prior generation that is not yet in
            # the active-policy snapshot and prevents priority-only shadowing.
            classifier_ownership: list[dict[str, Any]] = []
            reused_classifier_flows: list[dict[str, str]] = []
            superseded_classifier_flows: list[dict[str, str]] = []
            if action_type in {ActionType.SET_PRIORITY, ActionType.RATE_LIMIT}:
                from api.qos import classify_classifier_overlap
                from mcp_servers.onos_server import canonicalize_onos_device_id

                for device_id, rules in list(device_rules.items()):
                    try:
                        visible = await delivery.onos.get_flows(device_id)
                        overlaps = classify_classifier_overlap(
                            state.yang_draft, device_id, list(visible.get("flows") or []),
                        )
                    except Exception as overlap_exc:
                        raise RuntimeError(
                            f"CLASSIFIER_OVERLAP_READ_FAILED:{device_id}:{overlap_exc}"
                        ) from overlap_exc
                    exact = [item for item in overlaps if item["classification"] == "EXACT_SAME_SELECTOR"
                             and str(item.get("state") or "").upper() == "ADDED"]
                    partial = [item for item in overlaps if item["classification"] == "PARTIAL_OVERLAP"]
                    if partial:
                        raise RuntimeError(
                            f"CLASSIFIER_PARTIAL_OVERLAP_REQUIRES_SAFETY_REVIEW:{device_id}:{partial}"
                        )
                    if not exact:
                        classifier_ownership.append({
                            "state": "CURRENT", "operation": "CREATE", "device_id": device_id,
                            "overlap": "DISJOINT",
                        })
                        continue
                    equivalent = [item for item in exact if item.get("treatment_equivalent")]
                    if not equivalent:
                        # There is no safe append path for a competing action on
                        # the same selector.  A future staged/atomic replace must
                        # acquire traffic and validate it before old retirement.
                        raise RuntimeError(
                            f"CLASSIFIER_EXACT_SELECTOR_REQUIRES_STAGED_REPLACEMENT:{device_id}:{exact}"
                        )
                    keep = max(equivalent, key=lambda item: int(item.get("priority") or 0))
                    # Same selector and treatment is a semantic resource reuse.
                    # The latest execution becomes its owner; no lower-priority
                    # duplicate is written merely to get a new generation ID.
                    device_rules[device_id] = []
                    canonical_device = canonicalize_onos_device_id(device_id)
                    reused_classifier_flows.append({
                        "device_id": canonical_device,
                        "flow_id": str(keep["flow_id"]),
                    })
                    superseded_classifier_flows.extend({
                        "device_id": canonical_device,
                        "flow_id": str(item["flow_id"]),
                    } for item in exact if item["flow_id"] != keep["flow_id"])
                    classifier_ownership.append({
                        "state": "CURRENT",
                        "operation": "REUSED_EXACT_SELECTOR",
                        "device_id": canonical_device,
                        "flow_id": str(keep["flow_id"]),
                        "canonical_identity": keep.get("canonical_identity"),
                        "superseded_ownership": [
                            item["flow_id"] for item in exact if item["flow_id"] != keep["flow_id"]
                        ],
                    })

            # Do not issue an empty batch for an exactly reused classifier.
            device_rules = {
                device_id: rules for device_id, rules in device_rules.items() if rules
            }

            confirmed_devices: list[str] = []
            failed_devices: list[str] = []
            rolled_back_devices: list[str] = []
            total_written = 0
            errors: list[str] = []

            # ── Step 3: 事务性逐设备下发 (FlowTransaction) ──
            from infra.flow_transaction import FlowTransaction, FlowDeliveryError

            onos_client = delivery.onos
            txn = FlowTransaction(onos_client)

            # Persist installed flows incrementally so compensation logic
            # can clean up even when the job is cancelled mid-execution.
            # A ReAct retry re-enters this node after an earlier successful
            # delivery.  Keep every controller-assigned ID until the overall
            # intent reaches a verified success; otherwise terminal failure
            # compensation can only remove the most recent retry's rules.
            previously_installed = list((state.execution_result or {}).get("installed_flows") or [])
            for item in reused_classifier_flows:
                if item not in previously_installed:
                    previously_installed.append(item)
            state.execution_result = {
                "status": "IN_PROGRESS",
                "devices_confirmed": [],
                "installed_flows": previously_installed,
                "timestamp": datetime.utcnow().isoformat(),
            }
            await self._persist_installed_flows(state)

            try:
                for device_id, flow_rules in device_rules.items():
                    logger.info(
                        "[%s] 下发到 %s: %d 条 FlowRule (事务中)",
                        self.agent_id, device_id, len(flow_rules),
                    )

                    try:
                        flow_ids = await txn.add_batch(device_id, flow_rules)
                        confirmed_devices.append(device_id)
                        total_written += len(flow_rules)
                        # ── Immediately persist installed flows for Saga compensation ──
                        for fid in flow_ids:
                            state.execution_result["installed_flows"].append({
                                "device_id": device_id,
                                "flow_id": fid,
                            })
                        state.execution_result["devices_confirmed"] = list(confirmed_devices)
                        # Write to API store so compensation logic can find flows even
                        # if the orchestrator is cancelled before completing the job.
                        await self._persist_installed_flows(state)
                        logger.info(
                            "[%s] %s 事务内下发成功: %d 条",
                            self.agent_id, device_id, len(flow_rules),
                        )
                    except FlowDeliveryError as batch_err:
                        failed_devices.append(device_id)
                        errors.append(f"{device_id}: {batch_err}")
                        logger.error(
                            "[%s] %s 下发失败，触发回滚: %s",
                            self.agent_id, device_id, batch_err,
                        )
                        raise  # re-raise to trigger rollback in outer except

                await txn.commit()
                logger.info(
                    "[%s] 事务提交成功: %d 设备, %d 条规则",
                    self.agent_id, len(confirmed_devices), total_written,
                )

            except Exception:
                rollback_result = await txn.rollback()
                rolled_back_devices = [r.device_id for r in rollback_result.removed]
                for dev, fid, err in rollback_result.failed:
                    logger.error(
                        "[%s] 回滚失败: %s / %s: %s",
                        self.agent_id, dev, fid, err,
                    )
                # Clear persisted installed flows after successful rollback
                state.execution_result["installed_flows"] = [
                    f for f in state.execution_result.get("installed_flows", [])
                    if f["device_id"] not in rolled_back_devices
                ]
                if failed_devices:
                    errors.insert(
                        0,
                        f"已回滚设备: {rolled_back_devices}; "
                        f"回滚失败: {len(rollback_result.failed)} 条规则",
                    )

            # ── Step 4: 汇总结果 ──
            all_success = len(failed_devices) == 0 and len(errors) == 0
            # Preserve the concrete ONOS flow IDs captured during delivery.
            # Saga compensation needs them after the workflow has reached a
            # terminal state; replacing the in-progress result without this
            # field made later rollback unable to find installed rules.
            installed_flows = list(
                (state.execution_result or {}).get("installed_flows") or []
            )
            state.execution_result = {
                "status": "SUCCESS" if all_success else "PARTIAL",
                "devices_confirmed": confirmed_devices,
                "devices_failed": failed_devices,
                "devices_rolled_back": rolled_back_devices,
                "total_entries_written": total_written,
                "southbound_mutations": {
                    "onos_post": total_written,
                    "onos_delete": 0,
                    "p4_insert": total_written,
                    "p4_modify": 0,
                    "p4_delete": 0,
                    "tc_qdisc_apply": 0,
                    "tc_class_apply": 0,
                    "tc_filter_apply": 0,
                    "tc_qdisc_delete": 0,
                    "tc_class_delete": 0,
                    "tc_filter_delete": 0,
                    "ingress_apply": 0,
                    "resource_allocation_mutation": 0,
                    "classification": "P4_FLOW_DELIVERY",
                },
                "installed_flows": installed_flows,
                "errors": errors if errors else None,
                "timestamp": datetime.utcnow().isoformat(),
            }

            if all_success:
                try:
                    qos_result = await self._apply_qos_with_execution_lease(
                        state, operation="APPLY"
                    )
                except Exception:
                    # ONOS REST has already made the classifier live at this
                    # point.  TC is the second half of the execution plane, so
                    # a TC install/readback failure must compensate the P4
                    # classifier before the exception escapes this node.
                    rollback_result = await txn.rollback()
                    removed = {
                        (item.device_id, item.flow_id)
                        for item in rollback_result.removed
                    }
                    state.execution_result["installed_flows"] = [
                        item
                        for item in state.execution_result.get("installed_flows", [])
                        if (item.get("device_id"), item.get("flow_id")) not in removed
                    ]
                    state.execution_result["cross_plane_rollback"] = {
                        "status": "SUCCESS" if rollback_result.fully_cleaned else "PARTIAL",
                        "removed": len(rollback_result.removed),
                        "failed": rollback_result.failed,
                    }
                    await self._persist_installed_flows(state)
                    raise
                state.execution_result["qos_enforcement"] = qos_result
                state.execution_result["config_applied"] = bool(qos_result.get("config_applied", True))
                state.execution_result["operation"] = operation
                reconciled_existing = operation == "VERIFY_EXISTING" and bool(
                    total_written or state.execution_result.get("config_applied")
                )
                state.execution_result["executor_status"] = (
                    "CREATED"
                    if operation == "CREATE"
                    else "RECONCILED"
                    if reconciled_existing
                    else "UPDATED"
                )
                state.execution_result["no_change"] = not reconciled_existing and operation == "VERIFY_EXISTING"
                state.execution_result["traffic_scope_key"] = scope_key
                state.execution_result["policy_change"] = plan.get("policy_change") or {}
                state.execution_result["classifier_reused"] = False
                state.execution_result["classifier_operation"] = (
                    "REUSED" if reused_classifier_flows
                    else "CREATED" if operation == "CREATE" else "REPLACED"
                )
                state.execution_result["classifier_ownership"] = classifier_ownership
                state.execution_result["superseded_classifier_flows"] = list(
                    dict.fromkeys(
                        (item["device_id"], item["flow_id"])
                        for item in superseded_classifier_flows
                    )
                )
                state.execution_result["superseded_classifier_flows"] = [
                    {"device_id": device_id, "flow_id": flow_id}
                    for device_id, flow_id in state.execution_result["superseded_classifier_flows"]
                ]
                state.execution_result["tc_operation"] = "UPDATED" if operation != "CREATE" else "CREATED"
                mutations = state.execution_result["southbound_mutations"]
                if state.execution_result.get("config_applied"):
                    mutations["tc_class_apply"] = 1
                    mutations["tc_filter_apply"] = 1
                    mutations["resource_allocation_mutation"] = 1
                    mutations["classification"] = "P4_AND_QOS_DELIVERY"
                state.execution_result["desired_effect_signature"] = effect_signature
                state.execution_result["data_plane_changed"] = bool(
                    total_written or state.execution_result.get("config_applied")
                )
                state.execution_result["delivered_rules"] = device_rules
                state.execution_result["rollback_context"] = {
                    "old_policy": old_policy.model_dump(mode="json") if old_policy else None,
                    "old_runtime": old_runtime,
                }
                for flow_id in qos_result.get("flow_ids") or []:
                    flow_item = {
                        "device_id": qos_result.get("device_id"),
                        "flow_id": flow_id,
                    }
                    if flow_item["device_id"] and flow_item not in state.execution_result["installed_flows"]:
                        state.execution_result["installed_flows"].append(flow_item)
                healing_result = await self._maybe_apply_autonomous_self_healing(state)
                if healing_result:
                    state.execution_result["autonomous_self_healing"] = healing_result

            self._current_policy_result(state)["execution_result"] = state.execution_result

            if all_success:
                return self._end_log(
                    state, start, StageStatus.SUCCESS,
                    f"流表下发成功: {len(confirmed_devices)}台设备, "
                    f"共{total_written}条表项",
                )
            else:
                errors = state.execution_result.get("errors") or []
                state.error_message = f"部分设备下发失败: {'; '.join(errors)}"
                if policy_store is not None and scope_key:
                    policy_store.rollback_staged(scope_key, error=state.error_message)
                return self._end_log(
                    state, start, StageStatus.SUCCESS,
                    f"流表下发部分成功: {len(state.execution_result.get('devices_confirmed', []))}/{len(write_requests)}台设备",
                    error=state.error_message,
                )

        except Exception as e:
            # Restore the old concrete QoS class first, then expose the old
            # desired state again.  A failed create releases its new class.
            if policy_store is not None and scope_key:
                try:
                    from api.qos import rollback_qos_policy

                    await rollback_qos_policy(old_policy, state.yang_draft)
                except Exception as rollback_exc:
                    logger.error("[%s] QoS rollback failed: %s", self.agent_id, rollback_exc)
                policy_store.rollback_staged(scope_key, error=str(e))
            state.error_message = f"流表下发异常: {str(e)}"
            logger.error("[%s] %s", self.agent_id, state.error_message)
            state.execution_result = {
                "status": "FAILED",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            }
            self._current_policy_result(state)["execution_result"] = state.execution_result
            return self._end_log(
                state, start, StageStatus.FAILED,
                "流表下发失败",
                error=str(e),
            )

    async def _collect_pre_execution_telemetry(
        self,
        state: GlobalAgentState,
    ) -> dict[str, Any]:
        """Delegate to FlowExecutionEngine (infra/flow_execution.py)."""
        from infra.flow_execution import FlowExecutionEngine
        return await FlowExecutionEngine.collect_pre_execution_telemetry(
            intent_id=state.intent_id,
            policy_index=state.current_policy_index,
        )

    async def _maybe_apply_autonomous_self_healing(
        self,
        state: GlobalAgentState,
    ) -> dict[str, Any] | None:
        if not self._should_apply_autonomous_self_healing(state):
            return None

        task_id = None
        try:
            from api.a2a_trace import record_delegation

            task_id = record_delegation(
                state,
                source_agent="p4_runtime_executor",
                target_agent="auto_healing_controller",
                stage=self.stage,
                input_summary="Watcher internal intent requests autonomous congestion repair.",
                payload_schema="AutoHealing.Input.v1",
                payload={
                    "triggered_by": (state.user_intent.context or {}).get("triggered_by"),
                    "self_healing": (state.user_intent.context or {}).get("self_healing") or {},
                },
                evidence_refs=["UserIntent.context.self_healing", "Watcher.IncidentTrigger.v1"],
                confidence=0.9,
                risk_score=35,
            )
        except Exception:
            task_id = None

        try:
            from api.healing import apply_congestion_healing

            result = await apply_congestion_healing(
                state.yang_draft,
                None,
                intent_id=state.intent_id,
            )
            try:
                from api.a2a_trace import record_response

                record_response(
                    state,
                    source_agent="auto_healing_controller",
                    target_agent="p4_runtime_executor",
                    stage=self.stage,
                    status=StageStatus.SUCCESS if result.get("status") == "ok" else StageStatus.FAILED,
                    task_id=task_id,
                    output_summary=(
                        "Autonomous healing actions executed."
                        if result.get("status") == "ok"
                        else f"Autonomous healing failed: {result.get('error') or result.get('status')}"
                    ),
                    payload_schema="AutoHealing.Result.v1",
                    payload=result,
                    evidence_refs=["FlowDelivery post_flows", "Mininet tc/qdisc"],
                    confidence=0.85,
                    risk_score=45 if result.get("status") == "ok" else 80,
                )
            except Exception:
                pass
            return result
        except Exception as exc:
            logger.warning("[%s] Watcher autonomous healing failed: %s", self.agent_id, exc)
            return {"status": "error", "error": str(exc), "action": "autonomous_self_healing"}

    async def _persist_installed_flows(self, state: GlobalAgentState) -> None:
        """Persist installed flow IDs to the API store for Saga compensation.

        Called after each device's flows are successfully posted, so that
        even if the orchestrator is cancelled by a timeout, the compensation
        logic can find and delete the partially-installed flows.
        """
        try:
            from api.store import get_current_store
            store = get_current_store()
            if store is None:
                return
            record = store.get(state.intent_id)
            if record is None:
                return
            existing_result = record.get("result") or {}
            existing_result["execution_result"] = state.execution_result
            store.update(state.intent_id, result=existing_result)
        except Exception:
            pass  # best-effort: compensation will still try even without full data

    def _should_apply_autonomous_self_healing(self, state: GlobalAgentState) -> bool:
        context = state.user_intent.context or {}
        healing = context.get("self_healing") or {}
        context_enabled = (
            context.get("triggered_by") == "kpi_watcher"
            and bool(healing.get("enabled"))
            and bool(healing.get("autonomous", True))
        )
        plan_enabled = any(
            bool((item.constraints or {}).get("autonomous_self_healing"))
            for item in ((state.intent_plan.sub_intents if state.intent_plan else []) or [])
        )
        if not (context_enabled or plan_enabled):
            return False

        for prior_result in state.policy_results:
            execution = prior_result.get("execution_result") or {}
            if (execution.get("autonomous_self_healing") or {}).get("status") == "ok":
                return False
        return True



# ──────────────────────────────────────────────
# Verify阶段 — 效果验证节点
# ──────────────────────────────────────────────

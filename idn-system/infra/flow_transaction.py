"""
infra/flow_transaction.py
==========================

跨设备原子流表事务 —— Saga 补偿模式实现。

# 系统角色
将多台 ONOS 设备的 REST post_flows 调用包装为单一事务：
任何设备失败时，回滚已安装的规则（LIFO 顺序），
确保网络不会停留在半配置状态。

# Saga 补偿模式
- 逐设备下发，每成功一台立即记录 installed_flows
- 后续设备失败 → rollback() 逆向删除已安装规则
- asyncio.shield 保护每个 DELETE 操作不被 CancelledError 中断
- 「难点解释」ONOS REST 没有真正的 commit/rollback 语义，
  FlowTransaction 通过补偿操作（DELETE）模拟回滚
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _InstalledRule:
    device_id: str
    flow_id: str


@dataclass
class RollbackResult:
    removed: list[_InstalledRule] = field(default_factory=list)
    failed: list[tuple[str, str, str]] = field(default_factory=list)  # (device, flow_id, error)

    @property
    def fully_cleaned(self) -> bool:
        return len(self.failed) == 0


class FlowTransaction:
    """Atomic flow installation across multiple ONOS devices.

    Usage::

        txn = FlowTransaction(onos_client)
        try:
            for device_id, rules in rules_by_device.items():
                for rule in rules:
                    await txn.add(device_id, rule)
            await txn.commit()
        except Exception:
            result = await txn.rollback()
            if not result.fully_cleaned:
                logger.error("Partial rollback: %s", result.failed)
    """

    def __init__(self, onos_client: Any):
        self._onos = onos_client
        self._installed: list[_InstalledRule] = []
        self._committed = False

    async def add(self, device_id: str, flow_rule: dict[str, Any]) -> str:
        """Post a single flow rule to one device.

        Returns the ONOS flow ID assigned by the controller.
        """
        if self._committed:
            raise RuntimeError("Cannot add rules to a committed transaction")

        result = await self._onos.post_flows(device_id, [flow_rule])
        if result.get("errors"):
            raise FlowDeliveryError(
                f"Failed to post flow to {device_id}: {result['errors']}",
                device_id=device_id,
                partial_result=result,
            )

        # Prefer the ONOS-assigned flow ID from the POST response;
        # fall back to the deterministic derived ID.
        onos_ids: list[str] = result.get("flow_ids") or []
        flow_id = onos_ids[0] if onos_ids and onos_ids[0] else _extract_flow_id(flow_rule, device_id)
        self._installed.append(_InstalledRule(device_id=device_id, flow_id=flow_id))
        logger.debug("[FlowTransaction] added %s → %s", flow_id, device_id)
        return flow_id

    async def add_batch(self, device_id: str, flow_rules: list[dict[str, Any]]) -> list[str]:
        """Post a batch of flow rules to one device atomically.

        Each rule is posted individually via ONOS REST.  On any
        single-rule failure, raises :exc:`FlowDeliveryError` so the
        caller can invoke :meth:`rollback` on all previously-installed
        rules (including those from earlier devices).

        Returns the list of ONOS flow IDs in order.
        """
        if self._committed:
            raise RuntimeError("Cannot add rules to a committed transaction")

        flow_ids: list[str] = []
        for i, flow_rule in enumerate(flow_rules):
            result = await self._onos.post_flows(device_id, [flow_rule])
            if result.get("errors"):
                raise FlowDeliveryError(
                    f"Failed to post flow {i}/{len(flow_rules)} to {device_id}: "
                    f"{result['errors']}",
                    device_id=device_id,
                    partial_result=result,
                )
            onos_ids: list[str] = result.get("flow_ids") or []
            flow_id = onos_ids[0] if onos_ids and onos_ids[0] else _extract_flow_id(flow_rule, device_id)
            self._installed.append(_InstalledRule(device_id=device_id, flow_id=flow_id))
            flow_ids.append(flow_id)
            logger.debug(
                "[FlowTransaction] add_batch[%d/%d] %s → %s",
                i + 1, len(flow_rules), flow_id, device_id,
            )

        return flow_ids

    async def commit(self) -> None:
        """Mark the transaction as committed.

        In the current ONOS REST path there is no explicit commit step
        (rules are active immediately on POST).  This gates ``rollback()``
        so it is only called when a subsequent add fails.
        """
        self._committed = True
        logger.info("[FlowTransaction] committed %d rules", len(self._installed))

    def get_installed_flows(self) -> list[tuple[str, str]]:
        """Return the list of (device_id, flow_id) installed so far.

        Used by the caller (ExecuteFlowNode / worker) to persist
        compensation data for Saga-style cleanup on timeout.
        """
        return [(item.device_id, item.flow_id) for item in self._installed]

    async def rollback(self) -> RollbackResult:
        """Remove every rule installed in this transaction.

        Called when a multi-device install fails partway through.
        Rolls back in LIFO order.  Each delete is shielded so a
        CancelledError cannot interrupt cleanup mid-way.
        """
        result = RollbackResult()
        for installed in reversed(self._installed):
            try:
                await asyncio.shield(
                    self._onos.delete_flows(installed.device_id, [installed.flow_id])
                )
                result.removed.append(installed)
                logger.info("[FlowTransaction] rolled back %s on %s", installed.flow_id, installed.device_id)
            except Exception as exc:
                result.failed.append((installed.device_id, installed.flow_id, str(exc)))
                logger.error(
                    "[FlowTransaction] rollback failed for %s on %s: %s",
                    installed.flow_id, installed.device_id, exc,
                )

        if not result.fully_cleaned:
            logger.error(
                "[FlowTransaction] partial rollback: %d removed, %d failed",
                len(result.removed), len(result.failed),
            )
        else:
            logger.info("[FlowTransaction] rollback complete — %d rules removed", len(result.removed))
        return result


class FlowDeliveryError(Exception):
    """Raised when a flow rule cannot be delivered to a device."""

    def __init__(self, message: str, *, device_id: str, partial_result: dict[str, Any] | None = None):
        super().__init__(message)
        self.device_id = device_id
        self.partial_result = partial_result


def _extract_flow_id(flow_rule: dict[str, Any], device_id: str) -> str:
    """Derive a repeatable flow ID from the rule payload.

    ONOS assigns a flow ID automatically; if it's returned in the
    response we use it, otherwise we construct one from the rule's
    match fields so ``delete_flows`` can target it.
    """
    flow_id = flow_rule.get("id") or flow_rule.get("flowId")
    if flow_id:
        return str(flow_id)

    # Construct deterministic ID: {device}:{table}:{priority}:{hash}
    table = flow_rule.get("tableId") or flow_rule.get("table_id") or "0"
    priority = flow_rule.get("priority", 0)
    return f"{device_id}-{table}-{priority}"

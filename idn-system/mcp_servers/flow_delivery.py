"""
mcp_servers/flow_delivery.py
Southbound flow delivery abstraction — protocol-agnostic interface.

ExecuteFlowNode depends ONLY on the FlowDelivery ABC, never on
ONOS REST directly.  The concrete strategy is ONOSRestFlowDelivery.

Extensibility:
  - Implement new FlowDelivery subclasses for new southbound paths
  - FlowDeliveryFactory.create() is the single switch point
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ── Abstract base ────────────────────────────────────────────────────────────

class FlowDelivery(ABC):
    """Southbound flow delivery interface — protocol-agnostic.

    All methods accept ONOS FlowRule JSON (already converted from
    IDN internal models by the caller using FlowAdapter).
    """

    @abstractmethod
    async def post_flows(self, device_id: str, flow_rules: List[dict]) -> Dict[str, Any]:
        """Post flow rules to a device.

        Returns:
            {
                "status": "SUCCESS" | "PARTIAL" | "FAILED",
                "flows_posted": int,
                "total": int,
                "errors": [...] | None,
            }
        """

    @abstractmethod
    async def delete_flows(self, device_id: str, flow_ids: List[str]) -> Dict[str, Any]:
        """Delete flow rules from a device."""

    @abstractmethod
    async def check_health(self) -> Dict[str, Any]:
        """Check southbound health."""


# ── ONOS REST implementation ─────────────────────────────────────────────────

class ONOSRestFlowDelivery(FlowDelivery):
    """Posts FlowRule JSON to ONOS REST API POST /onos/v1/flows.

    The caller (ExecuteFlowNode) is responsible for converting
    WriteRequest → ONOS FlowRule JSON via FlowAdapter before calling
    post_flows().  This class only handles the HTTP transport.
    """

    onos: "ONOSClient"  # exposed for FlowTransaction / direct access

    def __init__(self, onos_client=None):
        from mcp_servers.onos_server import ONOSClient
        if onos_client is None:
            from mcp_servers.onos_server import get_onos_client
            onos_client = get_onos_client()
        self.onos = onos_client

    async def post_flows(self, device_id: str, flow_rules: List[dict]) -> Dict[str, Any]:
        if not flow_rules:
            return {
                "status": "SUCCESS",
                "flows_posted": 0,
                "total": 0,
                "flow_ids": [],
                "errors": None,
                "timestamp": datetime.utcnow().isoformat(),
            }

        logger.info(
            "[ONOSRestFlowDelivery] post_flows: %d rules → %s",
            len(flow_rules), device_id,
        )
        result = await self.onos.post_flows(device_id, flow_rules)
        all_ok = result.get("errors") is None
        return {
            "status": "SUCCESS" if all_ok else "PARTIAL",
            **result,
            "timestamp": datetime.utcnow().isoformat(),
        }

    async def post_group(self, device_id: str, group_payload: Dict[str, Any]) -> bool:
        """Create an action profile group on ONOS before posting flow rules."""
        logger.info(
            "[ONOSRestFlowDelivery] post_group: groupId=%s device=%s",
            group_payload.get("groupId"), device_id,
        )
        return await self.onos.post_group(device_id, group_payload)

    async def delete_flows(self, device_id: str, flow_ids: List[str]) -> Dict[str, Any]:
        if not flow_ids:
            return {
                "status": "SUCCESS",
                "flows_deleted": 0,
                "total": 0,
                "errors": None,
                "timestamp": datetime.utcnow().isoformat(),
            }

        logger.info(
            "[ONOSRestFlowDelivery] delete_flows: %d ids → %s",
            len(flow_ids), device_id,
        )
        result = await self.onos.delete_flows(device_id, flow_ids)
        all_ok = result.get("errors") is None
        return {
            "status": "SUCCESS" if all_ok else "PARTIAL",
            **result,
            "timestamp": datetime.utcnow().isoformat(),
        }

    async def check_health(self) -> Dict[str, Any]:
        return await self.onos.check_health()


# ── Factory ──────────────────────────────────────────────────────────────────

class FlowDeliveryFactory:
    """Creates the active FlowDelivery strategy — always ONOS REST."""

    @staticmethod
    def create() -> FlowDelivery:
        instance = ONOSRestFlowDelivery()
        logger.info("[FlowDeliveryFactory] → ONOSRestFlowDelivery")
        return instance

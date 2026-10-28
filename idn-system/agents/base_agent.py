"""
Shared capabilities for the domain agents.

The test model speaks the same contracts as the production pipeline:
IntentPlannerAgent receives an IntentPlan response, while PolicyCompilerAgent
receives a flow-table response.
"""
from __future__ import annotations

import json
import logging
import os
from abc import ABC
from typing import Any, Dict, Optional

from langchain_core.language_models import BaseChatModel

from agents.llm_client import get_llm as get_configured_llm

logger = logging.getLogger(__name__)


class _MockChatModel:
    """Small deterministic LLM replacement for local tests and demos."""

    async def ainvoke(self, messages, **kwargs):
        from langchain_core.messages import AIMessage

        return AIMessage(content=self._response_for(messages))

    def _response_for(self, messages):
        last_text = str(messages[-1].content) if messages else ""
        text = "\n".join(str(getattr(message, "content", message)) for message in messages)
        lowered = text.lower()

        if "selected_candidate_id" in lowered and '"candidates"' in last_text:
            try:
                payload = json.loads(last_text)
                candidates = payload.get("candidates") or []
                candidate_id = str(candidates[0]["candidate_id"])
                return json.dumps(
                    {
                        "selected_candidate_id": candidate_id,
                        "ranking": [
                            {
                                "candidate_id": str(item["candidate_id"]),
                                "rank": index + 1,
                                "reason": "deterministic mock ranking",
                            }
                            for index, item in enumerate(candidates)
                        ],
                        "rationale": "selected from the supplied feasible IDs",
                        "tradeoffs": [],
                        "confidence": 0.8,
                    }
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return "{}"
        if "selected_cause" in lowered and '"deterministic_authority"' in last_text:
            try:
                payload = json.loads(last_text)
                selected = str(payload["deterministic_authority"])
                candidates = payload.get("candidates") or []
                facts = payload.get("facts") or []
                fact_ids = [
                    str(item.get("fact_id"))
                    for item in facts
                    if item.get("fact_id")
                ]
                ordered = sorted(
                    candidates,
                    key=lambda item: int(item.get("deterministic_rank") or 999),
                )
                return json.dumps(
                    {
                        "selected_cause": selected,
                        "ranking": [
                            {
                                "cause": str(item["cause"]),
                                "rank": index + 1,
                                "confidence": 0.8 if index == 0 else 0.2,
                                "reason": str(item.get("reason") or "candidate"),
                                "evidence_fact_ids": fact_ids if index == 0 else [],
                            }
                            for index, item in enumerate(ordered)
                        ],
                        "explanation": "mock ranking follows deterministic facts",
                        "confidence": 0.8,
                    }
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return "{}"

        if "用户意图:" in last_text:
            return self._intent_plan_response(last_text)
        if "P4Runtime" in text or "流表项" in text or "flow table" in lowered:
            if '"action_type": "DROP"' in text:
                return MOCK_POLICY_GENERATE_DROP_RESPONSE
            return MOCK_POLICY_GENERATE_RESPONSE
        if "RootCauseAnalyzer" in text or "候选根因" in text:
            return json.dumps(
                {
                    "selected_cause": "primary_path_congestion",
                    "confidence": 0.91,
                    "reasoning_summary": "qdisc and P4 INT both indicate primary-path congestion; counters exclude a table miss.",
                    "evidence_used": ["tc_qdisc", "p4_int", "p4_direct_counter"],
                    "rejected_hypotheses": [
                        {
                            "cause": "flow_rule_mismatch_or_miss",
                            "reason": "counter data shows packets hitting installed rules",
                        }
                    ],
                    "recommended_action": "candidate_action_evaluation_required",
                    "human_readable_explanation": "The diagnosis identifies the suspected fault location; runtime-aware candidate generation and deterministic applicability validation must select any repair action.",
                },
                ensure_ascii=False,
            )
        if "澄清" in text or "clarification" in lowered:
            return "请提供更明确的网络需求描述，例如具体带宽、IP 网段和业务类型。"
        if "故障分析" in text or "根因" in text:
            return "链路拥塞导致队列深度过高，建议调整 QoS 优先级。"
        return "{}"

    def _intent_plan_response(self, prompt: str) -> str:
        """Return semantic planning only; deterministic fields are filled by rules."""
        raw_intent = prompt.split("用户意图:", 1)[1].splitlines()[0].strip()
        lowered = raw_intent.lower()
        is_rate_limit = any(word in raw_intent for word in ("限制", "限速", "不超过", "P2P")) or "rate limit" in lowered
        is_drop = any(word in raw_intent for word in ("阻断", "禁止", "拒绝", "丢弃")) or "drop" in lowered or "block" in lowered
        if is_rate_limit:
            action_type = "RATE_LIMIT"
        elif is_drop:
            action_type = "DROP"
        else:
            action_type = "SET_PRIORITY"

        payload = {
            "intent_plan": {
                "raw_intent": raw_intent,
                "sub_intents": [
                    {
                        "sequence": 1,
                        "objective": raw_intent,
                        "intent_type": action_type,
                        "match": {},
                        "action": {"action_type": action_type},
                        "constraints": {},
                        "priority": 100,
                    }
                ],
                "confidence_score": 0.9,
                "parse_warnings": [],
                "clarification_questions": [],
            }
        }
        return f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


MOCK_POLICY_GENERATE_RESPONSE = """```json
{
  "table_entries": [
    {
      "table_name": "MyIngress.sla_enforcement",
      "match_fields": [
        {"field_name": "hdr.intent.intent_type", "match_type": "exact", "value": 1}
      ],
      "action_name": "MyIngress.apply_meter_policy",
      "action_params": [],
      "priority": 100,
      "target_device_id": "device:bmv2:1"
    }
  ],
  "explanation": "Intent-based SLA enforcement for the selected QoS traffic class."
}
```"""

MOCK_POLICY_GENERATE_DROP_RESPONSE = """```json
{
  "table_entries": [
    {
      "table_name": "MyIngress.ipv4_routing",
      "match_fields": [
        {"field_name": "hdr.ipv4.dstAddr", "match_type": "lpm", "value": "10.0.0.50", "prefix_len": 32}
      ],
      "action_name": "MyIngress.drop_packet",
      "action_params": [],
      "priority": 50,
      "target_device_id": "device:bmv2:1"
    }
  ],
  "explanation": "Drop the selected traffic."
}
```"""


def is_llm_mock_mode() -> bool:
    """True when ``IDN_MOCK_MODE=true`` — enables deterministic LLM replacement."""
    from infra.config import ConfigManager
    return ConfigManager.is_mock_mode()


def get_llm() -> BaseChatModel:
    """Create the configured chat model or the deterministic local test model.

    When ``IDN_MOCK_MODE=true``, returns ``_MockChatModel`` which provides
    canned responses for known intent patterns and ``{}`` for unknowns.
    """
    if is_llm_mock_mode():
        logger.info("Mock mode: using deterministic local LLM replacement")
        return _MockChatModel()
    return get_configured_llm(mock_factory=_MockChatModel)


class BaseAgent(ABC):
    """Common LLM, MCP and A2A helpers shared by all agents."""

    agent_id: str = "base_agent"
    description: str = "Base agent"

    def __init__(self, llm: Optional[BaseChatModel] = None):
        self._llm = llm
        self._mcp_clients: Dict[str, Any] = {}
        logger.info("[%s] initialized", self.agent_id)

    @property
    def llm(self) -> BaseChatModel:
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    def _get_mcp_client(self, server_name: str):
        """Return one cached MCP client per server for this agent."""
        if server_name not in self._mcp_clients:
            from mcp_servers.client import MCPClient

            self._mcp_clients[server_name] = MCPClient(server_name)
        return self._mcp_clients[server_name]

    async def _call_mcp_tool(
        self, server: str, tool_name: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Call a tool on the MCP server owned by this agent capability."""
        client = self._get_mcp_client(server)
        logger.debug("[%s] MCP call: %s/%s params=%s", self.agent_id, server, tool_name, params)
        return await client.call_tool(tool_name, params)

    async def _read_mcp_resource(self, server: str, resource_path: str) -> Dict[str, Any]:
        """Read a resource from the MCP server owned by this agent capability."""
        client = self._get_mcp_client(server)
        logger.debug("[%s] MCP resource: %s%s", self.agent_id, server, resource_path)
        return await client.read_resource(resource_path)

    async def close(self):
        """Close all MCP client subprocess connections held by this agent."""
        for server_name, client in list(self._mcp_clients.items()):
            logger.debug("[%s] closing MCP client: %s", self.agent_id, server_name)
            await client.close()
        self._mcp_clients.clear()

    async def send_a2a_message(
        self,
        target_agent: str,
        task_id: str,
        intent_id: str,
        payload: Dict[str, Any],
        priority: str = "medium",
    ) -> Dict[str, Any]:
        """Build the JSON-RPC envelope used for agent-to-agent delegation."""
        import uuid

        message = {
            "jsonrpc": "2.0",
            "method": "a2a.task.delegate",
            "params": {
                "task_id": task_id,
                "intent_id": intent_id,
                "target_agent": target_agent,
                "source_agent": self.agent_id,
                "payload": payload,
                "priority": priority,
            },
            "id": str(uuid.uuid4()),
        }
        logger.info("[%s] A2A -> %s: task_id=%s", self.agent_id, target_agent, task_id)
        return message

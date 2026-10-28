"""
agents/policy_compiler.py
==========================

策略编译器 Agent —— YANG 策略 → P4Runtime 流表计划。

# 系统角色
将 YANGPolicy（抽象网络策略）编译为 FlowTablePlan（具体 P4Runtime 流表项），
包括：表项匹配字段选择、动作参数映射、设备分配、Meter 配置、
ECMP Action Profile 构造和依赖拓扑排序。

# 双路径编译
1. LLM 路径：语义驱动，适合复杂策略（QoS、ECMP、Action Profile）
2. 确定性规则路径：基于 P4Info schema + YANG 模板，LLM 失败时降级使用

# 主要依赖
- agents/base_agent.py：LLM 和 MCP 客户端基类
- models/flow_table.py：P4Runtime 流表数据模型
- mcp_servers/p4info_registry.py：P4Info schema 查询
- utils/llm_json.py：从 LLM 输出提取 JSON

# 关键设计
- MAC 地址解析：BMv2 软件交换机使用固定中继 MAC（020000000000），
  最后一跳必须使用 ONOS Host DB 中的真实主机 MAC
- Meter 配置：CIR/CBS 用于限速，PIR/PBS 用于峰值控制
- Action Profile：member → group → table_entry 严格写入顺序
- ReAct 重编译：接收 react_context，根据上次失败原因调整策略
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import uuid
import zlib
from ipaddress import ip_address, ip_network
from typing import Any, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from agents.base_agent import BaseAgent
from models.flow_table import ActionParam, AtomicityMode, FlowTablePlan, MatchField, MeterConfig, TableEntry, WriteRequest
from models.network import NetworkSnapshot
from models.yang_policy import ActionType, YANGPolicy
from mcp_servers.p4info_registry import p4info_registry
from utils.llm_json import extract_json_object

logger = logging.getLogger(__name__)

# Relay-hop MACs for inter-switch routes.
#
# BMv2 software switches process all Ethernet frames regardless of destination
# MAC, so these values are functionally arbitrary on BMv2.  The last-hop
# (host-facing) route MUST use the real host MAC from the ONOS host DB.
#
# WARNING: These are NOT valid on real hardware switches.  See
# CONTEXT.md "MAC 地址动态解析" for the production MAC resolution path.
_RELAY_SRC_MAC = "020000000000"
_RELAY_DST_MAC = "020000000001"

# Log once at module load so operators see this in production logs.
logger.info(
    "policy_compiler loaded: relay-hop MACs are BMv2 placeholders (%s / %s). "
    "Real hardware requires dynamic MAC resolution from ONOS host DB.",
    _RELAY_SRC_MAC, _RELAY_DST_MAC,
)


def _build_bandwidth_match_fields(match) -> list[MatchField]:
    """Build bandwidth_policy match fields (src/dst ternary only)."""
    def _cidr_to_ternary(cidr: str | None) -> tuple[str, str]:
        if not cidr:
            return ("00000000", "00000000")
        net = ip_network(cidr, strict=False)
        addr_int = int(net.network_address)
        prefix = net.prefixlen
        addr_hex = format(addr_int, "08x")
        mask_int = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF if prefix > 0 else 0
        mask_hex = format(mask_int, "08x")
        return (addr_hex, mask_hex)

    src_val, src_mask = _cidr_to_ternary(getattr(match, "source_ip", None))
    dst_val, dst_mask = _cidr_to_ternary(getattr(match, "destination_ip", None))

    return [
        MatchField(field_name="hdr.ipv4.srcAddr", match_type="ternary",
                   value=src_val, mask=src_mask),
        MatchField(field_name="hdr.ipv4.dstAddr", match_type="ternary",
                   value=dst_val, mask=dst_mask),
    ]


def _build_ternary_ip_match_fields(match) -> list[MatchField]:
    """Build ternary IP match fields for the acl table."""
    def _cidr_to_ternary(cidr):
        if not cidr:
            return ("00000000", "00000000")
        net = ip_network(cidr, strict=False)
        a = format(int(net.network_address), "08x")
        p = net.prefixlen
        m = format((0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF if p > 0 else 0, "08x")
        return (a, m)
    sv, sm = _cidr_to_ternary(getattr(match, "source_ip", None))
    dv, dm = _cidr_to_ternary(getattr(match, "destination_ip", None))
    return [
        MatchField(field_name="hdr.ipv4.srcAddr", match_type="ternary", value=sv, mask=sm),
        MatchField(field_name="hdr.ipv4.dstAddr", match_type="ternary", value=dv, mask=dm),
    ]


def _build_sla_match_fields(match) -> list[MatchField]:
    """Build sla_enforcement ternary match fields from YANG MatchCriteria.

    Converts CIDR source/destination IPs to ternary (value, mask) pairs.
    Unset fields use mask=0 (wildcard) so they match all traffic.
    """
    def _cidr_to_ternary(cidr: str | None) -> tuple[str, str]:
        if not cidr:
            return ("00000000", "00000000")  # wildcard
        net = ip_network(cidr, strict=False)
        addr_int = int(net.network_address)
        prefix = net.prefixlen
        addr_hex = format(addr_int, "08x")
        mask_int = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF if prefix > 0 else 0
        mask_hex = format(mask_int, "08x")
        return (addr_hex, mask_hex)

    src_val, src_mask = _cidr_to_ternary(getattr(match, "source_ip", None))
    dst_val, dst_mask = _cidr_to_ternary(getattr(match, "destination_ip", None))
    protocol = getattr(match, "protocol", None)
    # ICMP is a first-class ServiceProfile protocol (network health), not an
    # unspecified transport.  A protocol-specific match is needed whenever a
    # connectivity policy is compiled alongside other IPv4 traffic.
    _PROTO_MAP = {"ICMP": 1, "TCP": 6, "UDP": 17}
    if protocol and hasattr(protocol, "value"):
        proto_val = _PROTO_MAP.get(protocol.value, 0)
    else:
        proto_val = 0
    proto_val_hex = format(proto_val, "02x")  # 8-bit field
    destination_port = getattr(match, "destination_port", None)
    udp_port_mask = "ffff" if proto_val == 17 and destination_port is not None else "0000"
    tcp_port_mask = "ffff" if proto_val == 6 and destination_port is not None else "0000"

    return [
        MatchField(field_name="hdr.ipv4.srcAddr", match_type="ternary",
                   value=src_val, mask=src_mask),
        MatchField(field_name="hdr.ipv4.dstAddr", match_type="ternary",
                   value=dst_val, mask=dst_mask),
        MatchField(field_name="hdr.ipv4.protocol", match_type="ternary",
                   value=proto_val_hex, mask="ff" if proto_val else "00"),
        MatchField(field_name="hdr.udp.dstPort", match_type="ternary",
                   value=format(int(destination_port or 0), "04x"),
                   mask=udp_port_mask),
        MatchField(field_name="hdr.tcp.dstPort", match_type="ternary",
                   value=format(int(destination_port or 0), "04x"),
                   mask=tcp_port_mask),
    ]


class PolicyCompilationGateError(ValueError):
    code = "POLICY_COMPILATION_REJECTED"


class PolicyCompilerAgent(BaseAgent):
    """Compile YANG policies into P4Runtime flow-table plans.

    Real deployments use the deterministic P4Info/YANG compiler.  The legacy
    LLM compilation path remains reachable only in explicit mock mode for
    compatibility tests; it is never part of the production authority chain.
    """

    agent_id = "policy_compiler"
    description = "Compile YANG policies into P4Runtime flow-table plans"

    def __init__(self, llm: Optional[BaseChatModel] = None):
        super().__init__(llm=llm)

    async def compile(
        self,
        yang_draft: YANGPolicy | dict[str, Any],
        network_snapshot: NetworkSnapshot | dict[str, Any] | None = None,
        p4info: Optional[dict[str, Any]] = None,
        react_context: Optional[dict[str, Any]] = None,
        *,
        execution_capability: str | None = None,
        southbound_mutation_required: bool | None = None,
    ) -> FlowTablePlan:
        yang = self._coerce_yang(yang_draft)
        if (
            execution_capability == "CONTROL_PLANE_SCHEDULING_PREFERENCE"
            or southbound_mutation_required is False
        ):
            # Defense in depth: a caller that incorrectly reaches the compiler
            # with a pure scheduler relation still cannot manufacture P4 writes.
            return FlowTablePlan(
                plan_id=self._new_plan_id(),
                intent_id=yang.network_policy.policy_id,
                policy_operation="CONTROL_PLANE_ONLY",
                write_requests=[],
                action_profile_requests=[],
                capacity_warnings=["southbound mutation is not required for a scheduling preference"],
            )
        self._require_formal_plan(yang)
        snapshot = self._coerce_snapshot(network_snapshot)
        p4info = p4info or await self.load_p4info(snapshot)

        if os.getenv("IDN_MOCK_MODE", "false").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            plan = self._generate_fallback(yang, snapshot)
            plan = self._align_plan_to_p4info(plan, p4info)
            self._require_known_match_fields(plan, p4info)
            self._validate_action_semantics(plan, yang)
            return self._apply_react_adjustments(plan, react_context)

        try:
            raw_plan = await self._generate_with_qwen(yang, snapshot, p4info, react_context)
            plan = self._build_plan(raw_plan, yang, snapshot)
            plan = self._align_plan_to_p4info(plan, p4info)
            self._require_known_match_fields(plan, p4info)
            self._validate_action_semantics(plan, yang)
        except Exception as exc:
            logger.error(
                "[%s] LLM generation failed, using deterministic fallback: %s",
                self.agent_id,
                exc,
            )
            plan = self._generate_fallback(yang, snapshot)
            plan.ai_degraded = True
            plan.degradation_reason = f"LLM unavailable ({type(exc).__name__}), using deterministic rules"
            plan = self._align_plan_to_p4info(plan, p4info)
            self._require_known_match_fields(plan, p4info)

        return self._apply_react_adjustments(plan, react_context)

    @staticmethod
    def _require_formal_plan(yang: YANGPolicy) -> None:
        if not yang.source_plan_id or yang.source_plan_status != "PLAN_READY":
            raise PolicyCompilationGateError(
                "POLICY_COMPILATION_REJECTED: a PLAN_READY formal IntentPlan is required"
            )

    @staticmethod
    def _build_table_action_prompt() -> str:
        """Build a dynamic TABLE MAPPING section from the P4Info registry."""
        from mcp_servers.p4info_registry import P4InfoRegistry
        lines = ["TABLE MAPPING (action type -> candidate tables):"]
        for at in ("FORWARD","DROP","SET_PRIORITY","RATE_LIMIT","MIRROR","ACL_BLOCK"):
            tables = P4InfoRegistry.suggested_tables_for_action(at)
            ti = []
            for t in tables:
                acts = [a["name"] for a in P4InfoRegistry.table_actions(t)]
                ti.append(f"{t} + " + " | ".join(acts[:3]))
            lines.append(f"- {at} -> {'; '.join(ti) if ti else 'N/A'}")
        lines.append("\nAVAILABLE P4 ACTIONS (use ONLY these names):")
        for aname in sorted(P4InfoRegistry.action_names()):
            params = P4InfoRegistry.action_params(aname)
            if params:
                pl = ", ".join(f"{p['name']}({p['bitwidth']}b)" for p in params)
                lines.append(f"  {aname}({pl})")
            else:
                lines.append(f"  {aname}()")
        return "\n".join(lines)

    async def _generate_with_qwen(
        self,
        yang: YANGPolicy,
        snapshot: Optional[NetworkSnapshot],
        p4info: dict[str, Any],
        react_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        prompt_payload = {
            "yang_policy": yang.model_dump(mode="json"),
            "network_snapshot": self._brief_snapshot(snapshot) if snapshot else None,
            "p4info_brief": self._brief_p4info(p4info),
            "react_context": react_context or None,
        }
        # Experience memory hint (best-effort)
        experience_hint = ""
        try:
            from agents.experience_memory import ExperienceMemoryAgent
            mem = ExperienceMemoryAgent()
            similar = mem.retrieve_similar(
                intent_type=yang.network_policy.action.action_type.value, limit=3
            )
            successful = [c for c in similar if c.get("success")]
            if successful:
                hints = []
                for c in successful:
                    root = c.get("root_cause") or c.get("symptom") or ""
                    lesson = c.get("lesson") or ""
                    hints.append(f"- [{c.get('case_id','?')}] {root}: {lesson}")
                experience_hint = (
                    "\nSIMILAR SUCCESSFUL COMPILATIONS (context only):\n"
                    + "\n".join(hints)
                )
        except Exception:
            pass
        prompt_payload["experience_hint"] = experience_hint
        messages = [
            SystemMessage(
                content=(
                    "You are a P4Runtime policy compiler. Convert YANG policies into flow table plans.\n\n"
                    "OUTPUT FORMAT — return ONLY valid JSON with this EXACT flat structure:\n"
                    "{\n"
                    '  "plan_id": "<unique>",\n'
                    '  "intent_id": "<policy_id>",\n'
                    '  "write_requests": [\n'
                    '    {\n'
                    '      "device_id": "device:bmv2:N",\n'
                    '      "table_entries": [\n'
                    '        {\n'
                    '          "table_name": "MyIngress.ipv4_routing",\n'
                    '          "match_fields": [\n'
                    '            {"field_name": "hdr.ipv4.dstAddr", "match_type": "lpm", '
                    '"value": "10.0.0.0", "prefix_len": 8}\n'
                    '          ],\n'
                    '          "action_name": "MyIngress.set_nhop",\n'
                    '          "action_params": [\n'
                    '            {"param_name": "port", "value": 2}\n'
                    '          ],\n'
                    '          "priority": 100\n'
                    '        }\n'
                    '      ],\n'
                    '      "atomicity": "ROLLBACK_ON_ERROR"\n'
                    '    }\n'
                    '  ]\n'
                    '}\n\n'
                    "CRITICAL RULES:\n"
                    "1. Use FLAT field names: match_fields (NOT match), "
                    "action_name (NOT action.name or action: {name: ...}), "
                    "action_params (NOT params).\n"
                    "2. Ternary matches: {\"field_name\": \"...\", \"match_type\": \"ternary\", "
                    "\"value\": \"hex\", \"mask\": \"hex\"} — do NOT nest inside "
                    "\"ternary\": {\"value\": ..., \"mask\": ...}.\n"
                    "3. LPM matches: {\"field_name\": \"...\", \"match_type\": \"lpm\", "
                    "\"value\": \"ip\", \"prefix_len\": N}.\n"
                    "4. action_name MUST be a real P4 action: MyIngress.set_nhop, "
                    "MyIngress.drop_packet, MyIngress.set_dscp, "
                    "MyIngress.apply_meter_bytes_policy, MyIngress.apply_meter_policy. "
                    "NEVER use NoAction for traffic rules.\n"
                    "5. Do NOT nest entries inside request.updates[].entity.table_entry. "
                    "Use flat table_entries array directly under each write_request.\n"
                    "6. MAC ADDRESSES: All MAC values in action_params (srcMac, dstMac) "
                    "MUST be exactly 12 lowercase hex characters WITHOUT colons or dashes. "
                    "Valid: \"020000000000\". INVALID: \"02:00:00:00:00:00\". "
                    "Use the relay MAC \"020000000000\" for inter-switch hops.\n"
                    "7. Omit meter_config, counter_id, queue_id unless this is a "
                    "RATE_LIMIT policy.\n"
                    "8. ACTION PARAMS: Use ONLY the exact param names and bitwidths "
                    "from the P4Info tables→actions→params definitions in p4info_brief. "
                    "NEVER invent param names. Actions with empty params (e.g. "
                    "MyIngress.drop_packet) MUST have \"action_params\": []. "
                    "Param values must fit within the specified bitwidth.\n\n"
                    + self._build_table_action_prompt() + "\n\n"
                    "REFERENCE EXAMPLES (copy match_fields and action_params exactly):\n\n"
                    "FORWARD example:\n"
                    '{"table_name":"MyIngress.ipv4_routing","match_fields":'
                    '[{"field_name":"hdr.ipv4.dstAddr","match_type":"lpm",'
                    '"value":"10.0.0.0/8","prefix_len":8}],'
                    '"action_name":"MyIngress.set_nhop",'
                    '"action_params":[{"param_name":"port","value":1},'
                    '{"param_name":"srcMac","value":"020000000000"},'
                    '{"param_name":"dstMac","value":"ffffffffffff"}],"priority":100}\n\n'
                    "DROP example:\n"
                    '{"table_name":"MyIngress.ipv4_routing","match_fields":'
                    '[{"field_name":"hdr.ipv4.dstAddr","match_type":"lpm",'
                    '"value":"10.0.0.50/32","prefix_len":32}],'
                    '"action_name":"MyIngress.drop_packet",'
                    '"action_params":[],"priority":10000}\n\n'
                    "SET_PRIORITY example:\n"
                    '{"table_name":"MyIngress.sla_enforcement","match_fields":'
                    '[{"field_name":"hdr.ipv4.srcAddr","match_type":"ternary",'
                    '"value":"0xc0a80a00","mask":"0xffffff00"},'
                    '{"field_name":"hdr.ipv4.dstAddr","match_type":"ternary",'
                    '"value":"0x0a000000","mask":"0xff000000"},'
                    '{"field_name":"hdr.ipv4.protocol","match_type":"ternary",'
                    '"value":"0x11","mask":"0xff"},'
                    '{"field_name":"hdr.ipv4.diffserv","match_type":"ternary",'
                    '"value":"0","mask":"0"}],'
                    '"action_name":"MyIngress.set_dscp",'
                    '"action_params":[{"param_name":"dscp_value","value":46}],"priority":200}\n\n'
                    "RATE_LIMIT example:\n"
                    '{"table_name":"MyIngress.bandwidth_policy","match_fields":'
                    '[{"field_name":"hdr.ipv4.srcAddr","match_type":"ternary",'
                    '"value":"0xc0a80a00","mask":"0xffffff00"},'
                    '{"field_name":"hdr.ipv4.dstAddr","match_type":"ternary",'
                    '"value":"0x0a000000","mask":"0xff000000"}],'
                    '"action_name":"MyIngress.apply_meter_bytes_policy",'
                    '"action_params":[],"meter_config":{"cir":10000,"cbs":128},"priority":200}\n'
                )
            ),
            HumanMessage(
                content=(
                    "Generate a P4Runtime flow table plan for this payload:\n"
                    f"{json.dumps(prompt_payload, ensure_ascii=False, indent=2)}"
                )
            ),
        ]
        # Compiler 实际输出通常 500-1500 tokens；收紧 max_tokens 减少
        # DeepSeek Flash 输出阶段延迟，同时避免无意义的超长生成。
        response = await self.llm.ainvoke(messages, max_tokens=2048)
        content = getattr(response, "content", response)
        return extract_json_object(str(content))

    def _build_plan(
        self,
        raw_plan: dict[str, Any],
        yang: YANGPolicy,
        snapshot: Optional[NetworkSnapshot],
    ) -> FlowTablePlan:
        if "write_requests" in raw_plan:
            write_requests = self._parse_write_requests(
                raw_plan["write_requests"], yang, snapshot,
            )
            return FlowTablePlan(
                plan_id=str(raw_plan.get("plan_id") or self._new_plan_id()),
                intent_id=str(raw_plan.get("intent_id") or yang.network_policy.policy_id),
                write_requests=write_requests,
                capacity_check_passed=bool(raw_plan.get("capacity_check_passed", True)),
                capacity_warnings=list(raw_plan.get("capacity_warnings", [])),
            )

        entries_payload = raw_plan.get("table_entries")
        if not entries_payload:
            raise ValueError("Qwen output does not contain write_requests or table_entries")

        grouped: dict[str, list[TableEntry]] = {}
        default_device = self._default_device_id(snapshot)
        for entry_payload in entries_payload:
            entry_data = self._normalize_entry(dict(entry_payload))
            device_id = str(
                entry_data.pop("target_device_id", None)
                or entry_data.pop("device_id", None)
                or default_device
            )
            grouped.setdefault(device_id, []).append(TableEntry(**entry_data))

        return FlowTablePlan(
            plan_id=str(raw_plan.get("plan_id") or self._new_plan_id()),
            intent_id=str(raw_plan.get("intent_id") or yang.network_policy.policy_id),
            write_requests=[
                WriteRequest(
                    device_id=device_id,
                    table_entries=entries,
                    atomicity=AtomicityMode.ROLLBACK_ON_ERROR,
                )
                for device_id, entries in grouped.items()
            ],
            capacity_check_passed=bool(raw_plan.get("capacity_check_passed", True)),
            capacity_warnings=list(raw_plan.get("capacity_warnings", [])),
        )

    def _parse_write_requests(
        self,
        raw_requests: list[dict[str, Any]],
        yang: YANGPolicy,
        snapshot: Optional[NetworkSnapshot],
    ) -> list[WriteRequest]:
        """Parse write_requests, normalizing both IDN flat format and
        P4Runtime nested format (request.updates[].entity.table_entry)."""
        write_requests: list[WriteRequest] = []
        for wr in raw_requests:
            device_id = str(wr.get("device_id") or self._default_device_id(snapshot))
            table_entries: list[TableEntry] = []

            # P4Runtime nested format: request.updates[].entity.table_entry
            if "request" in wr and "table_entries" not in wr:
                inner = wr["request"]
                updates = inner.get("updates", [])
                for upd in updates:
                    entity = upd.get("entity", {})
                    te = entity.get("table_entry", {})
                    if te:
                        table_entries.append(
                            TableEntry(**self._normalize_entry(dict(te)))
                        )
            elif "table_entries" in wr:
                for entry_data in wr["table_entries"]:
                    table_entries.append(
                        TableEntry(**self._normalize_entry(dict(entry_data)))
                    )

            if table_entries:
                write_requests.append(WriteRequest(
                    device_id=device_id,
                    table_entries=table_entries,
                    atomicity=AtomicityMode.ROLLBACK_ON_ERROR,
                ))

        return write_requests

    @staticmethod
    def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
        """Normalize LLM output to match TableEntry Pydantic schema.

        Handles P4Runtime-native field names that LLMs commonly produce:
        - ``match`` → ``match_fields`` (flattening ternary/lpm/exact nests)
        - ``action`` → ``action_name`` + ``action_params``
        """
        # match → match_fields (P4Runtime nested format)
        if "match" in entry and "match_fields" not in entry:
            raw_match = entry.pop("match")
            entry["match_fields"] = []
            if isinstance(raw_match, list):
                for m in raw_match:
                    mf: dict[str, Any] = {"field_name": m.get("field_name", "")}
                    if "ternary" in m:
                        mf["match_type"] = "ternary"
                        mf["value"] = m["ternary"].get("value", "00")
                        mf["mask"] = m["ternary"].get("mask", "00")
                    elif "lpm" in m:
                        mf["match_type"] = "lpm"
                        mf["value"] = m["lpm"].get("value", "0.0.0.0")
                        mf["prefix_len"] = m["lpm"].get("prefix_len", 0)
                    elif "exact" in m:
                        mf["match_type"] = "exact"
                        mf["value"] = str(m["exact"].get("value", ""))
                    elif "range" in m:
                        mf["match_type"] = "range"
                        mf["value"] = str(m["range"].get("low", ""))
                        mf["mask"] = str(m["range"].get("high", ""))
                    else:
                        mf["match_type"] = m.get("match_type", "exact")
                        mf["value"] = m.get("value", "")
                    entry["match_fields"].append(mf)

        # action → action_name + action_params
        if "action" in entry and "action_name" not in entry:
            action = entry.pop("action")
            if isinstance(action, dict):
                entry["action_name"] = str(action.get("name", ""))
                entry["action_params"] = list(action.get("params") or [])

        # Ensure required fields have sensible defaults
        entry.setdefault("priority", 100)
        if "action_name" not in entry:
            entry["action_name"] = "MyIngress.NoAction"
        if "action_params" not in entry:
            entry["action_params"] = []
        if "match_fields" not in entry:
            entry["match_fields"] = []

        return entry

    @staticmethod
    def _brief_p4info(p4info: dict[str, Any], include_action_params: bool = True) -> dict[str, Any]:
        """Return a compact P4Info summary (table → match_fields + actions with params)
        to reduce LLM prompt size while keeping all decision-relevant info."""
        # Build a lookup of action_name → params for all actions in P4Info
        action_params_lookup: dict[str, list[dict[str, Any]]] = {}
        for action in p4info.get("actions", []):
            name = action.get("name")
            if name:
                action_params_lookup[name] = [
                    {"name": p.get("name"), "bitwidth": p.get("bitwidth")}
                    for p in action.get("params", [])
                ]

        tables_brief = {}
        for table in p4info.get("tables", []):
            name = table.get("name")
            if not name:
                continue
            brief: dict[str, Any] = {
                "match_fields": [
                    {"name": f.get("name"), "match_type": f.get("match_type"), "bitwidth": f.get("bitwidth")}
                    for f in table.get("match_fields", [])
                ],
            }
            if include_action_params:
                brief["actions"] = {
                    a.get("name"): {
                        "params": action_params_lookup.get(a.get("name"), []),
                    }
                    for a in table.get("actions", [])
                    if a.get("name")
                }
            else:
                brief["actions"] = [
                    a.get("name") for a in table.get("actions", [])
                ]
            tables_brief[name] = brief
        return {"tables": tables_brief}

    @staticmethod
    def _brief_snapshot(snapshot: NetworkSnapshot) -> dict[str, Any]:
        """Return a compact topology summary to reduce LLM prompt size."""
        return {
            "devices": [
                {"device_id": d.device_id, "name": getattr(d, "name", None)}
                for d in (snapshot.devices or [])
            ],
            "links_count": len(snapshot.links or []),
            "hosts": [
                {
                    "host_id": h.host_id,
                    "ip_addresses": h.ip_addresses,
                    "location_device": h.location_device,
                    "location_port": h.location_port,
                }
                for h in (snapshot.hosts or [])
            ],
        }

    def _generate_fallback(
        self,
        yang: YANGPolicy,
        snapshot: Optional[NetworkSnapshot],
    ) -> FlowTablePlan:
        policy = yang.network_policy
        action_type = policy.action.action_type
        device_id = self._default_device_id(snapshot)
        priority = policy.metadata.priority

        if action_type == ActionType.DROP:
            # DROP rules must win over FORWARD in P4Runtime LPM table
            # ordering (priority field, not prefix-length, determines match
            # order).  Boost well above any realistic forwarding priority.
            drop_priority = max(priority, 10000)
            entry = TableEntry(
                table_name="MyIngress.ipv4_routing",
                match_fields=[
                    self._lpm_field(
                        "hdr.ipv4.dstAddr",
                        policy.match.destination_ip or policy.match.source_ip or "0.0.0.0/0",
                    )
                ],
                action_name="MyIngress.drop_packet",
                action_params=[],
                priority=drop_priority,
            )
        elif action_type == ActionType.RATE_LIMIT:
            qos = policy.action.qos_params
            if not qos or qos.bandwidth is None:
                raise ValueError("RATE_LIMIT policy requires qos_params.bandwidth")
            device_id = self._resolve_classifier_device(policy.match.source_ip, snapshot)
            dscp_value = int(qos.dscp_value if qos.dscp_value is not None else 46)
            entry = TableEntry(
                table_name="MyIngress.sla_enforcement",
                match_fields=_build_sla_match_fields(policy.match),
                action_name="MyIngress.set_dscp",
                action_params=[ActionParam(param_name="dscp_value", value=dscp_value)],
                priority=priority,
            )
        elif action_type == ActionType.SET_PRIORITY:
            qos = policy.action.qos_params
            dscp_value = int(
                qos.dscp_value if qos is not None and qos.dscp_value is not None else 46
            )  # EF default only when the profile did not specify DSCP.
            device_id = self._resolve_classifier_device(policy.match.source_ip, snapshot)
            entry = TableEntry(
                table_name="MyIngress.sla_enforcement",
                match_fields=_build_sla_match_fields(policy.match),
                action_name="MyIngress.set_dscp",
                action_params=[
                    ActionParam(param_name="dscp_value", value=dscp_value),
                ],
                priority=priority,
            )
        elif action_type == ActionType.ACL_BLOCK:
            entry = TableEntry(
                table_name="MyIngress.acl",
                match_fields=_build_ternary_ip_match_fields(policy.match),
                action_name="MyIngress.acl_deny",
                action_params=[],
                priority=60000,
            )
        elif action_type == ActionType.MIRROR:
            dst_ip = policy.match.destination_ip or "0.0.0.0/0"
            nhop_device, nhop_port = self._resolve_nhop(dst_ip, snapshot)
            device_id = nhop_device
            dst_mac = self._resolve_mac_for_ip(dst_ip, snapshot)
            src_mac = self._resolve_gateway_mac_for_ip(dst_ip, snapshot)
            entry = TableEntry(
                table_name="MyIngress.ipv4_routing",
                match_fields=[self._lpm_field("hdr.ipv4.dstAddr", dst_ip)],
                action_name="MyIngress.set_nhop",
                action_params=[
                    ActionParam(param_name="port", value=nhop_port),
                    ActionParam(param_name="srcMac", value=src_mac),
                    ActionParam(param_name="dstMac", value=dst_mac),
                ],
                priority=priority,
                clone_session_id=1,
            )
        else:
            # Resolve nhop from topology; then create a route on EVERY
            # device so intermediate switches can forward toward the
            # destination (unmatched IPv4 packets are sent to CPU, not
            # flooded — every hop needs an LPM entry).
            dst_ip = policy.match.destination_ip or "0.0.0.0/0"
            nhop_device, nhop_port = self._resolve_nhop(dst_ip, snapshot)
            device_id = nhop_device
            dst_mac = self._resolve_mac_for_ip(dst_ip, snapshot)
            dst_gateway_mac = self._resolve_gateway_mac_for_ip(dst_ip, snapshot)
            entry = TableEntry(
                table_name="MyIngress.ipv4_routing",
                match_fields=[
                    self._lpm_field("hdr.ipv4.dstAddr", dst_ip)
                ],
                action_name="MyIngress.set_nhop",
                action_params=[
                    ActionParam(param_name="port", value=nhop_port),
                    ActionParam(param_name="srcMac", value=dst_gateway_mac),
                    ActionParam(param_name="dstMac", value=dst_mac),
                ],
                priority=priority,
            )

            # Build per-device WriteRequests for intermediate switches
            write_requests: list[WriteRequest] = []
            write_requests.append(WriteRequest(
                device_id=device_id,
                table_entries=[entry],
                atomicity=AtomicityMode.ROLLBACK_ON_ERROR,
            ))

            # ── Forward-path relay routes (toward destination) ──────
            if snapshot and snapshot.devices:
                for dev in snapshot.devices:
                    if dev.device_id == device_id:
                        continue
                    relay = self._relay_port_toward(
                        dev.device_id, nhop_device, snapshot,
                    )
                    write_requests.append(WriteRequest(
                        device_id=dev.device_id,
                        table_entries=[TableEntry(
                            table_name="MyIngress.ipv4_routing",
                            match_fields=[self._lpm_field("hdr.ipv4.dstAddr", dst_ip)],
                            action_name="MyIngress.set_nhop",
                            action_params=[
                                ActionParam(param_name="port", value=relay),
                                ActionParam(param_name="srcMac", value=_RELAY_SRC_MAC),
                                ActionParam(param_name="dstMac", value=_RELAY_DST_MAC),
                            ],
                            priority=priority,
                        )],
                        atomicity=AtomicityMode.ROLLBACK_ON_ERROR,
                    ))

            # ── Return-path routes (toward source) ──────────────────
            src_ip = policy.match.source_ip
            if src_ip and snapshot and snapshot.devices:
                src_device, src_port = self._resolve_nhop(src_ip, snapshot)
                src_mac = self._resolve_mac_for_ip(src_ip, snapshot)
                src_gateway_mac = self._resolve_gateway_mac_for_ip(src_ip, snapshot)
                # Edge switch: forward directly to source host port
                write_requests.append(WriteRequest(
                    device_id=src_device,
                    table_entries=[TableEntry(
                        table_name="MyIngress.ipv4_routing",
                        match_fields=[self._lpm_field("hdr.ipv4.dstAddr", src_ip)],
                        action_name="MyIngress.set_nhop",
                        action_params=[
                            ActionParam(param_name="port", value=src_port),
                            ActionParam(param_name="srcMac", value=src_gateway_mac),
                            ActionParam(param_name="dstMac", value=src_mac),
                        ],
                        priority=priority,
                    )],
                    atomicity=AtomicityMode.ROLLBACK_ON_ERROR,
                ))
                # Relay switches: forward toward source switch
                for dev in snapshot.devices:
                    if dev.device_id == src_device:
                        continue  # already has direct host-facing entry above
                    relay = self._relay_port_toward(
                        dev.device_id, src_device, snapshot,
                    )
                    write_requests.append(WriteRequest(
                        device_id=dev.device_id,
                        table_entries=[TableEntry(
                            table_name="MyIngress.ipv4_routing",
                            match_fields=[self._lpm_field("hdr.ipv4.dstAddr", src_ip)],
                            action_name="MyIngress.set_nhop",
                            action_params=[
                                ActionParam(param_name="port", value=relay),
                                ActionParam(param_name="srcMac", value=_RELAY_SRC_MAC),
                                ActionParam(param_name="dstMac", value=_RELAY_DST_MAC),
                            ],
                            priority=priority,
                        )],
                        atomicity=AtomicityMode.ROLLBACK_ON_ERROR,
                    ))

        # Ensure write_requests is set for all action types
        if "write_requests" not in locals():
            write_requests = [
                WriteRequest(
                    device_id=device_id,
                    table_entries=[entry],
                    atomicity=AtomicityMode.ROLLBACK_ON_ERROR,
                )
            ]

        return FlowTablePlan(
            plan_id=self._new_plan_id(),
            intent_id=policy.policy_id,
            write_requests=write_requests,
            capacity_check_passed=True,
        )

    def _validate_action_semantics(self, plan: FlowTablePlan, yang: YANGPolicy) -> None:
        """Reject LLM output that is P4-shaped but violates the requested action type.

        Each action type requires specific P4 tables — if the LLM chooses the
        wrong table, the plan will fail P4Info verification.  Catch it early
        with a clear error so the retry (or fallback) gets actionable feedback.
        """
        from mcp_servers.p4info_registry import P4InfoRegistry

        action_type = yang.network_policy.action.action_type
        entries = [
            entry
            for request in plan.write_requests
            for entry in request.table_entries
        ]
        if not entries:
            return

        valid_tables = P4InfoRegistry.suggested_tables_for_action(
            action_type.value if hasattr(action_type, "value") else str(action_type)
        )
        for entry in entries:
            if entry.table_name not in valid_tables:
                raise ValueError(
                    f"{action_type.value} entries must use one of {valid_tables}, "
                    f"got {entry.table_name}"
                )

    # ── P4Info alignment (contributed by demo-layer integration) ──────────────

    def _align_plan_to_p4info(self, plan: FlowTablePlan, p4info: dict[str, Any]) -> FlowTablePlan:
        """Make generated entries obey the active device P4Info contract.

        Fixes applied in order:
        1. Table name fuzzy match (LLM may omit the MyIngress. prefix)
        2. Match field name fuzzy match + missing ternary mask
        3. Action param name fuzzy match
        """
        table_fields = self._p4info_table_fields(p4info)
        all_table_names = list(table_fields.keys())

        # Build action param lookup: action_name → {param_name: bitwidth}
        action_params_lookup: dict[str, dict[str, int]] = {}
        for action in p4info.get("actions", []):
            name = action.get("name")
            if name:
                action_params_lookup[name] = {
                    str(p.get("name")): p.get("bitwidth", 0) or 0
                    for p in (action.get("params") or [])
                }

        for request in plan.write_requests:
            for entry in request.table_entries:
                # ── 1. Table name fuzzy match ──
                if entry.table_name not in table_fields and all_table_names:
                    candidates = difflib.get_close_matches(
                        entry.table_name, all_table_names, n=1, cutoff=0.6
                    )
                    if candidates:
                        logger.warning(
                            "_align_plan: corrected table_name %r → %r",
                            entry.table_name, candidates[0],
                        )
                        entry.table_name = candidates[0]

                fields = table_fields.get(entry.table_name, {})
                field_names = list(fields.keys())

                # ── 2. Match field fuzzy match + type/mask correction ──
                for match_field in entry.match_fields:
                    spec = fields.get(match_field.field_name)
                    if not spec and field_names:
                        candidates = difflib.get_close_matches(
                            match_field.field_name, field_names, n=1, cutoff=0.5
                        )
                        if candidates:
                            logger.warning(
                                "_align_plan: corrected match field %r → %r",
                                match_field.field_name, candidates[0],
                            )
                            match_field.field_name = candidates[0]
                            spec = fields.get(candidates[0])

                    if not spec:
                        continue
                    expected_type = str(spec.get("match_type") or "").lower()
                    if expected_type not in {"exact", "lpm", "ternary", "range"}:
                        continue

                    actual_type = str(match_field.match_type or "").lower()
                    if actual_type != expected_type:
                        match_field.match_type = expected_type

                    if expected_type == "ternary" and match_field.mask is None:
                        prefix_len = match_field.prefix_len
                        if prefix_len is None and "/" in str(match_field.value):
                            try:
                                prefix_len = ip_network(str(match_field.value), strict=False).prefixlen
                            except ValueError:
                                prefix_len = None
                        if prefix_len is not None:
                            bitwidth = self._to_int(spec.get("bitwidth")) or 32
                            match_field.mask = self._prefix_mask(prefix_len, bitwidth)

                # ── 3. Action param name fuzzy match ──
                expected_params = action_params_lookup.get(entry.action_name, {})
                if expected_params:
                    for param in entry.action_params:
                        if param.param_name not in expected_params:
                            param_names = list(expected_params.keys())
                            candidates = difflib.get_close_matches(
                                param.param_name, param_names, n=1, cutoff=0.5
                            )
                            if candidates:
                                logger.warning(
                                    "_align_plan: corrected action param %r → %r "
                                    "in action %r",
                                    param.param_name, candidates[0], entry.action_name,
                                )
                                param.param_name = candidates[0]

                # ── 4. Auto-fill missing required match fields ──
                # LLM frequently omits non-optional TERNARY fields (e.g.
                # hdr.ipv4.diffserv in sla_enforcement).  Insert wildcard
                # entries (value=0, mask=0) so the plan passes P4 verification
                # on the first attempt instead of triggering a ReAct retry.
                required_fields = {
                    name: spec for name, spec in fields.items()
                    if str(spec.get("match_type", "")).upper() != "OPTIONAL"
                }
                existing_names = {mf.field_name for mf in entry.match_fields}
                for field_name in sorted(required_fields):
                    if field_name not in existing_names:
                        bitwidth = self._to_int(required_fields[field_name].get("bitwidth")) or 32
                        entry.match_fields.append(MatchField(
                            field_name=field_name,
                            match_type="ternary",
                            value="0x0" if bitwidth > 16 else "0",
                            mask="0x0" if bitwidth > 16 else "0",
                        ))
                        logger.warning(
                            "_align_plan: auto-filled missing required field "
                            "%r (bitwidth=%d) in table %r",
                            field_name, bitwidth, entry.table_name,
                        )
        return plan

    def _p4info_table_fields(self, p4info: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
        tables: dict[str, dict[str, dict[str, Any]]] = {}
        for table in p4info.get("tables", []):
            table_name = str(table.get("name") or "")
            if not table_name:
                continue
            tables[table_name] = {
                str(field.get("name")): dict(field)
                for field in table.get("match_fields", [])
                if field.get("name")
            }
        return tables

    def _require_known_match_fields(
        self,
        plan: FlowTablePlan,
        p4info: dict[str, Any],
    ) -> None:
        """Reject generated selectors that the active P4Info cannot encode.

        Alignment may correct a close spelling, but it must never silently
        retain a semantic/LLM-only field such as ``hdr.intent.intent_type``.
        Raising here activates the deterministic compiler before the plan can
        reach the rule verifier or deployment path.
        """
        table_fields = self._p4info_table_fields(p4info)
        for request in plan.write_requests:
            for entry in request.table_entries:
                known = table_fields.get(entry.table_name)
                if known is None:
                    raise ValueError(
                        f"P4Info has no table {entry.table_name!r}"
                    )
                unknown = sorted(
                    field.field_name
                    for field in entry.match_fields
                    if field.field_name not in known
                )
                if unknown:
                    raise ValueError(
                        f"P4Info table {entry.table_name!r} has no match fields: "
                        + ", ".join(unknown)
                    )

    def _prefix_mask(self, prefix_len: int, bitwidth: int = 32) -> int:
        prefix_len = max(0, min(prefix_len, bitwidth))
        return ((1 << bitwidth) - 1) ^ ((1 << (bitwidth - prefix_len)) - 1)

    # ── Post-compile adjustments ─────────────────────────────────────────────

    def _apply_react_adjustments(
        self,
        plan: FlowTablePlan,
        react_context: Optional[dict[str, Any]],
    ) -> FlowTablePlan:
        """Apply deterministic corrections for rule conflicts understood by the compiler."""
        if not react_context:
            return plan

        report = react_context.get("rule_verification_report") or {}
        if hasattr(report, "model_dump"):
            report = report.model_dump(mode="json")

        priority_floor: Optional[int] = None
        for item in report.get("violation_items", []):
            violation_type = str(item.get("violation_type") or item.get("type") or "")
            if violation_type == "SHADOWED_RULE":
                existing_priority = self._to_int(item.get("existing_priority"))
                if existing_priority is not None:
                    priority_floor = max(priority_floor or 0, existing_priority + 1)
            elif violation_type == "RUNTIME_RULE_CONFLICT":
                existing_priority = self._to_int(item.get("priority"))
                if existing_priority is not None:
                    priority_floor = max(priority_floor or 0, existing_priority + 1)

        if priority_floor is None:
            return plan

        adjusted_entries = 0
        for request in plan.write_requests:
            for entry in request.table_entries:
                if entry.priority < priority_floor:
                    entry.priority = priority_floor
                    adjusted_entries += 1

        if adjusted_entries:
            plan.react_adjustments.append(
                f"ReAct: raised priority to {priority_floor} for {adjusted_entries} table entry(s) "
                "after rule verification conflict."
            )
        return plan

    async def load_p4info(self, network_snapshot: NetworkSnapshot | dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the P4 program contract for the target device.

        Uses the compiled p4info_registry as the single authoritative source.
        The caller may pass a network_snapshot for device-id selection; when
        absent the default device is used.
        """
        snapshot = self._coerce_snapshot(network_snapshot)
        _ = self._default_device_id(snapshot)  # resolved but not needed for registry lookup
        return p4info_registry.to_dict()

    def _demo_p4info(self) -> dict[str, Any]:
        """Backward-compatible test helper for the compiled P4Info registry."""
        return p4info_registry.to_dict()


    def _lpm_field(self, field_name: str, cidr: str) -> MatchField:
        network = ip_network(str(cidr), strict=False)
        return MatchField(
            field_name=field_name,
            match_type="lpm",
            value=str(network.network_address),
            prefix_len=network.prefixlen,
        )

    def _default_device_id(self, snapshot: Optional[NetworkSnapshot]) -> str:
        if snapshot and snapshot.devices:
            return snapshot.devices[0].device_id
        return "device:bmv2:1"

    def _resolve_classifier_device(
        self,
        source_ip: str | None,
        snapshot: Optional[NetworkSnapshot],
    ) -> str:
        """Resolve the ingress switch attached to the policy source host."""
        if snapshot and source_ip:
            try:
                source_net = ip_network(source_ip, strict=False)
            except ValueError:
                source_net = None
            if source_net is not None:
                for host in snapshot.hosts:
                    if not host.location_device:
                        continue
                    for address in host.ip_addresses:
                        try:
                            if ip_address(str(address).split("/", 1)[0]) in source_net:
                                return host.location_device
                        except ValueError:
                            continue
        return self._default_device_id(snapshot)

    @staticmethod
    def _parse_port(port_str: str) -> int | None:
        """Extract a numeric port from ONOS formats like '[name](3)' or '3'."""
        import re
        if not port_str:
            return None
        # Try direct integer parse first
        try:
            return int(port_str)
        except ValueError:
            pass
        # Extract number from [name](number) format
        m = re.search(r"\((\d+)\)", port_str)
        return int(m.group(1)) if m else None

    def _relay_port_toward(
        self,
        from_device: str,
        toward_device: str,
        snapshot: NetworkSnapshot,
    ) -> int:
        """Return port on *from_device* that is the next hop toward
        *toward_device*, computed via BFS shortest-path over the link
        graph."""
        if from_device == toward_device:
            return 1

        # Build adjacency: device → [(port, neighbor)]
        adjacency: dict[str, list[tuple[int, str]]] = {}
        for link in snapshot.links:
            sp = self._parse_port(link.src_port)
            dp = self._parse_port(link.dst_port)
            if sp is None or dp is None:
                continue
            adjacency.setdefault(link.src_device, []).append((sp, link.dst_device))
            adjacency.setdefault(link.dst_device, []).append((dp, link.src_device))

        logger.info(
            "[%s] _relay_port_toward: %s → %s  (links=%d, adjacency=%s)",
            self.agent_id, from_device, toward_device,
            len(snapshot.links), list(adjacency.keys()),
        )

        # BFS from toward_device — compute distances
        from collections import deque
        dist: dict[str, int] = {toward_device: 0}
        q = deque([toward_device])
        while q:
            cur = q.popleft()
            for _port, neighbor in adjacency.get(cur, []):
                if neighbor not in dist:
                    dist[neighbor] = dist[cur] + 1
                    q.append(neighbor)

        # Find neighbor of from_device closest to toward_device
        best_port = 1  # default: first port
        best_dist = 10_000
        from_neighbors = adjacency.get(from_device, [])
        if not from_neighbors:
            logger.warning(
                "[%s] _relay_port_toward: %s has no neighbors — fallback port 1",
                self.agent_id, from_device,
            )
        for port, neighbor in from_neighbors:
            d = dist.get(neighbor, 10_000)
            if d < best_dist:
                best_dist = d
                best_port = port
        logger.info(
            "[%s] _relay_port_toward: %s → %s  best_port=%d (dist=%d)",
            self.agent_id, from_device, toward_device, best_port, best_dist,
        )
        return best_port

    def _resolve_nhop(
        self,
        destination_ip: str,
        snapshot: Optional[NetworkSnapshot],
    ) -> tuple[str, int]:
        """Find (device_id, egress_port) that reaches *destination_ip*.

        Strategy (in order):
        1. ONOS host discovery — find a host whose IP matches the
           destination prefix and return its location_device / location_port.
        2. Link topology — for each device, find a port that connects to
           exactly one link (likely host-facing) and isn't a switch-to-switch
           trunk.  Use the device closest to the destination side.
        3. Fallback — first available device, port 1.
        """
        import ipaddress as _ipaddress

        # ── Strategy 1: ONOS host discovery ──────────────────────────
        if snapshot and snapshot.hosts:
            try:
                dst_net = _ipaddress.ip_network(destination_ip, strict=False)
            except ValueError:
                dst_net = None

            if dst_net:
                for host in snapshot.hosts:
                    for ip_str in host.ip_addresses:
                        try:
                            host_ip = _ipaddress.ip_address(ip_str.split("/")[0])
                        except ValueError:
                            continue
                        if host_ip in dst_net:
                            if host.location_device and host.location_port:
                                try:
                                    port = int(host.location_port)
                                    logger.info(
                                        "[%s] nhop resolved via host %s → %s port %d",
                                        self.agent_id, host.host_id,
                                        host.location_device, port,
                                    )
                                    return host.location_device, port
                                except ValueError:
                                    pass

        # ── Strategy 2: link topology (host-facing port heuristic) ───
        if snapshot and snapshot.devices and snapshot.links:
            trunk_ports: dict[str, set[int]] = {}
            for link in snapshot.links:
                try:
                    trunk_ports.setdefault(link.src_device, set()).add(int(link.src_port))
                    trunk_ports.setdefault(link.dst_device, set()).add(int(link.dst_port))
                except ValueError:
                    pass

            # Mininet assigns host-facing ports AFTER switch-to-switch
            # links.  The host port is max_trunk + 1 on the edge switch.
            # Pick the device with the largest non-trunk port ratio.
            best_dev, best_port = None, 0
            for dev in snapshot.devices:
                trunks = trunk_ports.get(dev.device_id, set())
                max_trunk = max(trunks) if trunks else 0
                # Host port is immediately after the last trunk port
                candidate = max_trunk + 1
                # Only consider if this port is NOT a trunk (sanity check)
                if candidate not in trunks:
                    if candidate > best_port:
                        best_dev, best_port = dev.device_id, candidate
            if best_dev is not None:
                logger.info(
                    "[%s] nhop resolved via topology: %s port %d (host-facing heuristic)",
                    self.agent_id, best_dev, best_port,
                )
                return best_dev, best_port

        # ── Strategy 3: fallback ─────────────────────────────────────
        device_id = self._default_device_id(snapshot)
        logger.warning(
            "[%s] No host or topology match for %s, using %s port 1",
            self.agent_id, destination_ip, device_id,
        )
        return device_id, 1

    def _resolve_mac_for_ip(
        self,
        ip_str: str,
        snapshot: Optional[NetworkSnapshot],
    ) -> str:
        """Resolve the MAC address for a destination IP from ONOS host DB.

        Returns a 12-char hex string WITHOUT colons (e.g. "72bfd722827b")
        suitable for P4 action params.  Falls back to broadcast MAC if the
        host is not found in the ONOS host database.
        """
        import ipaddress as _ipaddress

        try:
            dst_net = _ipaddress.ip_network(ip_str, strict=False)
        except ValueError:
            logger.warning("[%s] Invalid IP prefix %s, using broadcast MAC", self.agent_id, ip_str)
            return "ffffffffffff"

        if snapshot and snapshot.hosts:
            for host in snapshot.hosts:
                for host_ip_str in host.ip_addresses:
                    try:
                        host_ip = _ipaddress.ip_address(host_ip_str.split("/")[0])
                    except ValueError:
                        continue
                    if host_ip in dst_net:
                        mac = host.mac.replace(":", "").replace("-", "").lower()
                        if len(mac) != 12:
                            continue
                        logger.info(
                            "[%s] MAC resolved for %s: %s (host %s)",
                            self.agent_id, ip_str, mac, host.host_id,
                        )
                        return mac

        logger.warning(
            "[%s] MAC not found for %s in %d hosts, using broadcast",
            self.agent_id, ip_str,
            len(snapshot.hosts) if snapshot and snapshot.hosts else 0,
        )
        return "ffffffffffff"

    def _resolve_gateway_mac_for_ip(
        self,
        ip_str: str,
        snapshot: Optional[NetworkSnapshot],
    ) -> str:
        """Resolve the destination subnet's authoritative virtual gateway MAC."""
        import ipaddress as _ipaddress

        try:
            dst_net = _ipaddress.ip_network(ip_str, strict=False)
        except ValueError:
            return _RELAY_SRC_MAC

        if snapshot and snapshot.hosts:
            for host in snapshot.hosts:
                if not host.gateway_mac:
                    continue
                for host_ip_str in host.ip_addresses:
                    try:
                        host_ip = _ipaddress.ip_address(host_ip_str.split("/")[0])
                    except ValueError:
                        continue
                    if host_ip in dst_net:
                        mac = host.gateway_mac.replace(":", "").replace("-", "").lower()
                        if len(mac) == 12 and mac != "000000000000":
                            return mac
        logger.warning(
            "[%s] Gateway MAC not found for %s; using non-zero relay MAC",
            self.agent_id,
            ip_str,
        )
        return _RELAY_SRC_MAC

    def _coerce_yang(self, yang_draft: YANGPolicy | dict[str, Any]) -> YANGPolicy:
        if isinstance(yang_draft, YANGPolicy):
            return yang_draft
        return YANGPolicy(**yang_draft)

    def _coerce_snapshot(
        self,
        network_snapshot: NetworkSnapshot | dict[str, Any] | None,
    ) -> Optional[NetworkSnapshot]:
        if network_snapshot is None or isinstance(network_snapshot, NetworkSnapshot):
            return network_snapshot
        return NetworkSnapshot(**network_snapshot)

    def _new_plan_id(self) -> str:
        return f"PLAN-{uuid.uuid4().hex[:8].upper()}"

    def _meter_id(self, policy_id: str) -> int:
        """Allocate a repeatable non-zero meter index for one policy."""
        return zlib.crc32(policy_id.encode("utf-8")) % 4095 + 1

    def _to_int(self, value: Any) -> Optional[int]:
        try:
            return int(value) if value not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None


async def compile_policy(
    yang_draft: YANGPolicy | dict[str, Any],
    network_snapshot: NetworkSnapshot | dict[str, Any] | None = None,
) -> FlowTablePlan:
    return await PolicyCompilerAgent().compile(yang_draft, network_snapshot)

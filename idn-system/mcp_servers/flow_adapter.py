"""
mcp_servers/flow_adapter.py
============================

流表适配器 —— IDN 内部模型 ↔ ONOS 3.0 FlowRule JSON。

# 系统角色
将 IDN-System 内部的 P4Runtime 模型（TableEntry/WriteRequest）
转换为 ONOS 3.0 PI（Protocol Independent）格式的 FlowRule JSON。

# ONOS 3.0 PI 格式要点
- 字段名使用字符串（非数字字段 ID）
- 匹配类型使用小写（"lpm", "ternary", "exact"）
- bit<32> 参数使用 hex 编码的值
- actionParams 使用 Map 格式：{"paramId": "hex_value", ...}
- POST 到 /onos/v1/flows/{deviceId}（裸 FlowRule，无 "flows" 包装）

# 主要依赖
- models/flow_table.py：TableEntry, WriteRequest, MatchField
- ipaddress：IP 地址 hex 编码

# 设计意图
- 提供 batch_convert() 一次转换多个 WriteRequest
- 按设备分组返回 {device_id: [flow_rules]}
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Any, Dict, List, Optional

from models.flow_table import TableEntry, WriteRequest, MatchField

logger = logging.getLogger(__name__)


def _ip_to_hex(ip_str: str) -> str:
    """Convert an IPv4 dotted-decimal string to hex (e.g. 10.0.0.1 → '0a000001')."""
    try:
        return ipaddress.IPv4Address(ip_str).packed.hex()
    except Exception:
        return ip_str


def _int_to_hex(value: int, bitwidth: int = 32) -> str:
    """Convert an integer to hex string padded to bitwidth bytes (ceil division)."""
    byte_len = max(1, (bitwidth + 7) // 8)
    return format(value, f'0{byte_len * 2}x')


def _value_to_hex(value, bitwidth: int = 32) -> str:
    """Convert a value to hex string.

    - IPv4 strings → hex (e.g. '10.0.0.1' → '0a000001')
    - Integers → hex padded to bitwidth
    - Strings that look like hex → pass through
    - Other strings → encode to hex
    """
    if isinstance(value, int):
        return _int_to_hex(value, bitwidth)
    s = str(value).strip()
    # Check if it looks like an IPv4 address
    if '.' in s:
        try:
            return _ip_to_hex(s)
        except Exception:
            pass
    # Normalize explicit hex emitted by LLM/compiler paths.  Encoding a value
    # such as ``0xc0a80a0a`` as UTF-8 produced ``3078...`` on the wire and
    # ONOS rejected it because value/mask sizes no longer matched.
    hex_len = max(1, (bitwidth + 3) // 4)
    if s.lower().startswith("0x"):
        raw = s[2:]
        if raw and all(c in '0123456789abcdefABCDEF' for c in raw):
            return raw.lower().zfill(hex_len)[-hex_len:]
    # Accept short hex strings and left-pad them to the P4 field width.  This
    # turns wildcard value/mask strings such as "0" into "00" for bit<8>
    # instead of ASCII hex "30".
    if s and all(c in '0123456789abcdefABCDEF' for c in s) and len(s) <= hex_len:
        return s.lower().zfill(hex_len)
    # Fallback: encode string bytes to hex
    try:
        return s.encode().hex()
    except Exception:
        return s


class FlowAdapter:
    """Converts IDN flow models to ONOS 3.0 REST API FlowRule JSON.

    For P4Runtime devices, ONOS 3.0 expects PROTOCOL_INDEPENDENT
    instruction/criterion types with P4 field names and hex values.
    """

    def __init__(self, p4info_registry=None):
        from mcp_servers.p4info_registry import p4info_registry as default_registry
        self.p4info = p4info_registry or default_registry

    def to_onos_flow_rule(
        self, entry: TableEntry, device_id: str, group_id: Optional[int] = None,
    ) -> dict:
        """Convert a single TableEntry to ONOS 3.0 FlowRule JSON.

        Args:
            entry: IDN internal TableEntry (P4 table name, match fields, action)
            device_id: Target device (e.g. 'device:bmv2:1')
            group_id: If set, the flow rule references an action profile group
                      (indirect table) instead of a direct action.

        Returns:
            ONOS 3.0 FlowRule JSON dict ready for POST /onos/v1/flows/{deviceId}
        """
        table_id = self.p4info.table_flow_type(entry.table_name)

        treatment = (
            self._build_indirect_treatment(group_id)
            if group_id is not None
            else self._build_treatment(entry)
        )

        rule: Dict[str, Any] = {
            "priority": entry.priority,
            "timeout": 0,
            "isPermanent": True,
            "deviceId": device_id,
            "tableId": table_id,
            "treatment": treatment,
            "selector": self._build_selector(entry),
        }
        return rule

    # ── Treatment builders ──────────────────────────────────────────────────

    def _build_treatment(self, entry: TableEntry) -> dict:
        """Build ONOS 3.0 treatment with PROTOCOL_INDEPENDENT direct action.

        CRITICAL: ONOS interprets string actionId as a P4 action NAME
        (e.g. "MyIngress.set_nhop"), NOT a numeric ID.  Sending a
        numeric ID as a string (e.g. "24952588") causes ONOS to look
        for an action named "24952588", which does not exist.

        ONOS 3.0 format:
          {"instructions":[{"type":"PROTOCOL_INDEPENDENT",
             "subtype":"ACTION",
             "actionId":"MyIngress.set_nhop",   ← P4 action NAME
             "actionParams":{"port":"0001"}     ← param NAME → hex value
          }], "deferred":[]}
        """
        return {
            "instructions": [
                {
                    "type": "PROTOCOL_INDEPENDENT",
                    "subtype": "ACTION",
                    "actionId": entry.action_name,
                    "actionParams": self._format_action_params_by_name(
                        entry.action_name, entry.action_params,
                    ),
                }
            ],
            "deferred": [],
        }

    def _build_indirect_treatment(self, group_id: int) -> dict:
        """Build ONOS PI treatment referencing an action profile group.

        When a P4 table has an action profile, the flow rule must reference
        a pre-created action profile group instead of a direct action.

        Uses PI (Protocol Independent) format which bypasses the Java
        interpreterʼs mapTreatment() and is handled natively by ONOS's
        P4Runtime pipeline.  (The standard GROUP instruction type requires
        interpreter support that the custom IdnInterpreter does not yet
        implement for P4Runtime action-profile tables.)

        ONOS PI format:
          {"instructions":[{"type":"PROTOCOL_INDEPENDENT",
             "subtype":"ACTION_PROFILE_GROUP",
             "groupId":1235755039}], "deferred":[]}
        """
        return {
            "instructions": [
                {
                    "type": "PROTOCOL_INDEPENDENT",
                    "subtype": "ACTION_PROFILE_GROUP",
                    "groupId": group_id,
                }
            ],
            "deferred": [],
        }

    # ── Action param formatting ─────────────────────────────────────────────

    def _format_action_params_by_name(
        self, action_name: str, action_params: List[MatchField],
    ) -> Dict[str, str]:
        """Convert action params → ONOS PI wire format using param NAMES as keys.

        ONOS interprets actionParams keys as P4 param NAMES (e.g. "port"),
        not numeric param IDs.  Using numeric IDs (e.g. "1") causes
        ONOS to look for a param named "1", which may not match.

        Returns {param_name_str: hex_value_str}, e.g. {"port": "0001"}.
        """
        formatted: Dict[str, str] = {}
        for ap in action_params:
            bitwidth = getattr(ap, "bitwidth", 32) or 32
            # For known P4 params, get bitwidth from registry
            try:
                bitwidth = self.p4info.param_bitwidth(action_name, ap.param_name)
            except Exception:
                # Use default bitwidth for unknown actions (e.g. in group payloads)
                pass
            formatted[ap.param_name] = _value_to_hex(ap.value, bitwidth)
        return formatted

    def _format_action_params(
        self, action_name: str, action_params: List[MatchField],
    ) -> Dict[str, str]:
        """Convert action params (name+value) → ONOS PI wire format.

        Returns a dict of {param_id_str: hex_value_str}, e.g.
        {"1": "0a000001"} for port=10.0.0.1.

        Deprecated in favor of _format_action_params_by_name for new code.
        """
        formatted: Dict[str, str] = {}
        for ap in action_params:
            param_id = self.p4info.param_id(action_name, ap.param_name)
            if param_id is None:
                logger.warning(
                    "Unknown param %s for action %s, skipping",
                    ap.param_name, action_name,
                )
                continue
            bitwidth = self.p4info.param_bitwidth(action_name, ap.param_name)
            formatted[str(param_id)] = _value_to_hex(ap.value, bitwidth)
        return formatted

    # ── Group payload builder ───────────────────────────────────────────────

    def build_group_payload(
        self,
        device_id: str,
        group_id: int,
        profile_name: str,
        action_name: str,
        action_params: List[MatchField],
    ) -> Dict[str, Any]:
        """Build a standard ONOS Group JSON payload for an action profile group.

        Returns a dict suitable for POST /onos/v1/groups.
        The group wraps a single action with parameters in a SELECT bucket.
        """
        action_id = str(self.p4info.action_id(action_name))
        formatted_params = self._format_action_params(action_name, action_params)

        return {
            "deviceId": device_id,
            "appId": "org.onosproject.idn",
            "appCookie": f"0x{group_id:08x}",
            "groupId": group_id,
            "type": "SELECT",
            "buckets": [
                {
                    "weight": 1,
                    "treatment": {
                        "instructions": [
                            {
                                "type": "PROTOCOL_INDEPENDENT",
                                "subtype": "ACTION",
                                "actionId": action_id,
                                "actionParams": formatted_params,
                            }
                        ],
                        "deferred": [],
                    },
                }
            ],
        }

    def _build_selector(self, entry: TableEntry) -> dict:
        """Build ONOS 3.0 selector with PROTOCOL_INDEPENDENT match criteria.

        ONOS 3.0 format (inside criteria array):
          {"type": "PROTOCOL_INDEPENDENT",
           "matches": [
             {"field": "hdr.ipv4.dstAddr",        ← P4 field NAME
              "match": "ternary",                 ← lowercase
              "value": "0a000000",                ← hex
              "mask": "ff000000"}                 ← hex (ternary only)
           ]}
        """
        matches = []
        for mf in entry.match_fields:
            match_obj = self._build_match(entry.table_name, mf)
            if match_obj:
                matches.append(match_obj)

        return {
            "criteria": [{
                "type": "PROTOCOL_INDEPENDENT",
                "matches": matches,
            }],
        }

    def _build_match(self, table_name: str, mf: MatchField) -> dict | None:
        """Build a single ONOS 3.0 PI match entry.

        Returns None if the field name cannot be resolved.
        """
        # Field name is the P4Info match field name (e.g. "hdr.ipv4.dstAddr")
        field_name = mf.field_name
        match_info = self.p4info.table_match_fields(table_name).get(field_name)
        if match_info is None:
            logger.warning("Unknown field %s in table %s, skipping", field_name, table_name)
            return None

        match_type = match_info["match_type"].lower()  # "TERNARY" → "ternary"
        bitwidth = match_info.get("bitwidth", 32)

        match_obj: Dict[str, Any] = {
            "field": field_name,
            "match": match_type,
        }

        if match_type == "ternary":
            match_obj["value"] = _value_to_hex(mf.value, bitwidth)
            # Mask: use provided mask, or derive from prefix_len, or full mask
            if mf.mask:
                match_obj["mask"] = _value_to_hex(mf.mask, bitwidth)
            elif mf.prefix_len:
                mask_int = (0xFFFFFFFF << (32 - mf.prefix_len)) & 0xFFFFFFFF
                match_obj["mask"] = _int_to_hex(mask_int)
            else:
                match_obj["mask"] = "f" * max(1, (bitwidth + 3) // 4)
            # P4Runtime represents a ternary wildcard by omitting the field;
            # a zero mask is not a valid PI ternary match and leaves ONOS
            # flows stuck in PENDING_ADD.
            if int(match_obj["mask"], 16) == 0:
                return None
        elif match_type == "lpm":
            match_obj["value"] = _value_to_hex(
                mf.value.split("/")[0] if "/" in str(mf.value) else mf.value,
                bitwidth,
            )
            if mf.prefix_len is not None:
                match_obj["prefixLength"] = mf.prefix_len
            elif "/" in str(mf.value):
                match_obj["prefixLength"] = int(str(mf.value).split("/")[-1])
            else:
                match_obj["prefixLength"] = 32
        elif match_type == "exact":
            match_obj["value"] = _value_to_hex(mf.value, bitwidth)
        elif match_type == "range":
            match_obj["lowValue"] = _value_to_hex(mf.value, bitwidth)
            match_obj["highValue"] = _value_to_hex(mf.mask or mf.value, bitwidth)
        else:
            # Fallback: treat as exact
            match_obj["value"] = _value_to_hex(mf.value, bitwidth)

        return match_obj

    def batch_convert(self, write_requests: List[WriteRequest]) -> Dict[str, List[dict]]:
        """Convert multiple WriteRequests grouped by device.

        Returns:
            {device_id: [flow_rule_json, ...]}
        """
        device_rules: Dict[str, List[dict]] = {}
        for wr in write_requests:
            if wr.device_id not in device_rules:
                device_rules[wr.device_id] = []
            for entry in wr.table_entries:
                rule = self.to_onos_flow_rule(entry, wr.device_id)
                device_rules[wr.device_id].append(rule)
        return device_rules

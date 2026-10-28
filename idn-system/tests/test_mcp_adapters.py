"""
Unit tests for MCP server adapters, transformers, and backends.

Tests the seams where data crosses between ONOS ↔ IDN models and
where mock/real backends diverge. These are pure unit tests — no
network, no LLM, no Docker.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_servers.onos_server import (
    ONOSClient,
    ONOSDataTransformer,
    _controller_inventory_status,
    _flow_id_from_location,
)
from mcp_servers.flow_adapter import FlowAdapter, _ip_to_hex, _int_to_hex, _value_to_hex
from mcp_servers.p4info_registry import p4info_registry
from mcp_servers.flow_delivery import ONOSRestFlowDelivery, FlowDeliveryFactory
from models.flow_table import ActionParam, MatchField, TableEntry, WriteRequest, AtomicityMode


# ═══════════════════════════════════════════════════════════════════════════════
# Hex helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestHexHelpers:
    def test_ip_to_hex(self):
        assert _ip_to_hex("10.0.0.1") == "0a000001"
        assert _ip_to_hex("192.168.1.1") == "c0a80101"
        assert _ip_to_hex("0.0.0.0") == "00000000"

    def test_ip_to_hex_passthrough_non_ip(self):
        assert _ip_to_hex("not-an-ip") == "not-an-ip"

    def test_int_to_hex_32bit(self):
        assert _int_to_hex(1, 32) == "00000001"
        assert _int_to_hex(255, 32) == "000000ff"
        assert _int_to_hex(0x0A000001, 32) == "0a000001"

    def test_int_to_hex_16bit(self):
        assert _int_to_hex(0x1234, 16) == "1234"

    def test_value_to_hex_ipv4_string(self):
        assert _value_to_hex("10.0.0.1") == "0a000001"

    def test_value_to_hex_integer(self):
        assert _value_to_hex(255, 32) == "000000ff"


class TestONOSFlowIdParsing:
    def test_extracts_id_from_absolute_location(self):
        location = "http://localhost:8181/onos/v1/flows/device:bmv2:5/49539597563367395"
        assert _flow_id_from_location(location) == "49539597563367395"

    def test_extracts_id_from_relative_location_and_ignores_query(self):
        location = "/onos/v1/flows/device:bmv2:2/12345?source=rest"
        assert _flow_id_from_location(location) == "12345"

    def test_empty_or_malformed_location_returns_empty(self):
        assert _flow_id_from_location(None) == ""
        assert _flow_id_from_location("") == ""
        assert _flow_id_from_location("not-a-path") == ""

    @pytest.mark.asyncio
    async def test_post_flows_uses_location_id_for_empty_201_response(self, monkeypatch):
        class FakeResponse:
            status = 201
            headers = {
                "Location": "http://onos:8181/onos/v1/flows/device:bmv2:5/987654321"
            }

            async def text(self):
                return ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class FakeSession:
            def post(self, *_args, **_kwargs):
                return FakeResponse()

        client = ONOSClient()
        monkeypatch.setattr(client, "_get_session", AsyncMock(return_value=FakeSession()))

        result = await client.post_flows("device:bmv2:5", [{"priority": 100}])

        assert result["flows_posted"] == 1
        assert result["flow_ids"] == ["987654321"]
        assert result["errors"] is None


class TestONOSControllerInventoryHealth:
    def test_empty_controller_is_still_starting(self):
        assert _controller_inventory_status({"devices": []}) == ("starting", 0, 0)

    def test_requires_available_devices(self):
        payload = {"devices": [{"available": False}, {"available": True}]}
        assert _controller_inventory_status(payload, min_devices=2) == ("starting", 2, 1)

    def test_ready_when_minimum_available_devices_exist(self):
        payload = {"devices": [{"available": True}, {"available": True}]}
        assert _controller_inventory_status(payload, min_devices=2) == ("ok", 2, 2)

    @pytest.mark.asyncio
    async def test_reachable_empty_inventory_closes_transport_circuit(self, monkeypatch):
        class FakeResponse:
            async def json(self):
                return {"devices": []}

            def raise_for_status(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class FakeSession:
            def get(self, *_args, **_kwargs):
                return FakeResponse()

        client = ONOSClient()
        client._failure_count = client._cb_threshold
        client._circuit_open_until = 999999999.0
        monkeypatch.setattr(client, "_get_session", AsyncMock(return_value=FakeSession()))

        result = await client.check_health()

        assert result["status"] == "starting"
        assert result["device_count"] == 0
        assert client._failure_count == 0
        assert client._circuit_open_until == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# ONOSDataTransformer
# ═══════════════════════════════════════════════════════════════════════════════

ONOS_DEVICE = {
    "id": "of:0000000000000001",
    "type": "SWITCH",
    "available": True,
    "annotations": {"grpcPort": "50051", "p4Program": "idn_basic"},
    "ports": [{"port": "1", "isEnabled": True}],
}

ONOS_LINK = {
    "src": {"device": "of:0000000000000001", "port": "1"},
    "dst": {"device": "of:0000000000000002", "port": "1"},
    "state": "ACTIVE",
}

ONOS_HOST = {
    "id": "00:00:00:00:00:01/None",
    "mac": "00:00:00:00:00:01",
    "ipAddresses": ["192.168.10.10"],
    "locations": [{"elementId": "of:0000000000000001", "port": "3"}],
}

BMV2_DEVICE_ID = "device:bmv2:1"
BMV2_DEVICE_ID_2 = "device:bmv2:2"


class TestONOSDataTransformer:
    def test_map_device_id_algorithmic_for_any_dpid(self):
        """Algorithmic mapping: of:<hex_dpid> → device:bmv2:<int(dpid,16)>."""
        assert ONOSDataTransformer._map_device_id("of:0000000000000007") == "device:bmv2:7"
        assert ONOSDataTransformer._map_device_id("of:000000000000000a") == "device:bmv2:10"
        assert ONOSDataTransformer._map_device_id("of:00000000000000ff") == "device:bmv2:255"

    def test_map_device_id_preserves_non_of_format(self):
        """Non-of: IDs are returned as-is."""
        assert ONOSDataTransformer._map_device_id("device:bmv2:5") == "device:bmv2:5"
        assert ONOSDataTransformer._map_device_id(None) == "unknown"
        assert ONOSDataTransformer._map_device_id("") == "unknown"

    def test_device_to_dict_maps_id_to_device_id(self):
        result = ONOSDataTransformer.device_to_dict(ONOS_DEVICE)
        assert result["device_id"] == "device:bmv2:1"

    def test_device_to_dict_maps_available_to_status(self):
        result = ONOSDataTransformer.device_to_dict(ONOS_DEVICE)
        assert result["status"] == "AVAILABLE"

    def test_device_to_dict_unavailable_device(self):
        dev = {**ONOS_DEVICE, "available": False}
        result = ONOSDataTransformer.device_to_dict(dev)
        assert result["status"] == "UNAVAILABLE"

    def test_device_to_dict_extracts_grpc_port_from_annotations(self):
        result = ONOSDataTransformer.device_to_dict(ONOS_DEVICE)
        assert result["grpc_port"] == 50051

    def test_device_to_dict_grpc_port_none_when_missing(self):
        dev = {**ONOS_DEVICE, "annotations": {}}
        result = ONOSDataTransformer.device_to_dict(dev)
        assert result["grpc_port"] is None

    def test_device_to_dict_extracts_p4_program(self):
        result = ONOSDataTransformer.device_to_dict(ONOS_DEVICE)
        assert result["p4_program"] == "idn_basic"

    def test_link_to_dict_generates_link_id(self):
        result = ONOSDataTransformer.link_to_dict(ONOS_LINK)
        assert result["link_id"] == "link-device:bmv2:1-1-to-device:bmv2:2-1"

    def test_link_to_dict_maps_src_dst(self):
        result = ONOSDataTransformer.link_to_dict(ONOS_LINK)
        assert result["src_device"] == "device:bmv2:1"
        assert result["src_port"] == "1"
        assert result["dst_device"] == "device:bmv2:2"
        assert result["dst_port"] == "1"

    def test_link_to_dict_preserves_state(self):
        result = ONOSDataTransformer.link_to_dict(ONOS_LINK)
        assert result["state"] == "ACTIVE"

    def test_host_to_dict_maps_id_to_host_id(self):
        result = ONOSDataTransformer.host_to_dict(ONOS_HOST)
        assert result["host_id"] == "00:00:00:00:00:01/None"

    def test_host_to_dict_extracts_ip_addresses(self):
        result = ONOSDataTransformer.host_to_dict(ONOS_HOST)
        assert result["ip_addresses"] == ["192.168.10.10"]

    def test_host_to_dict_extracts_location(self):
        result = ONOSDataTransformer.host_to_dict(ONOS_HOST)
        assert result["location_device"] == "device:bmv2:1"
        assert result["location_port"] == "3"

    def test_host_to_dict_empty_locations(self):
        host = {**ONOS_HOST, "locations": []}
        result = ONOSDataTransformer.host_to_dict(host)
        assert result["location_device"] is None
        assert result["location_port"] is None

    def test_flow_to_dict_maps_basic_fields(self):
        flow = {
            "id": "flow-123",
            "deviceId": "of:0000000000000001",
            "tableId": 0,
            "priority": 100,
            "state": "ADDED",
        }
        result = ONOSDataTransformer.flow_to_dict(flow)
        assert result["flow_id"] == "flow-123"
        assert result["device_id"] == "device:bmv2:1"
        assert result["priority"] == 100


# ═══════════════════════════════════════════════════════════════════════════════
# FlowAdapter
# ═══════════════════════════════════════════════════════════════════════════════

def _make_entry(
    table_name="MyIngress.ipv4_routing",
    action_name="MyIngress.set_nhop",
    match_fields=None,
    action_params=None,
    priority=100,
) -> TableEntry:
    return TableEntry(
        table_name=table_name,
        match_fields=match_fields or [
            MatchField(field_name="hdr.ipv4.dstAddr", match_type="lpm", value="10.0.0.0", prefix_len=8),
        ],
        action_name=action_name,
        action_params=action_params if action_params is not None else [
            ActionParam(param_name="port", value=1),
        ],
        priority=priority,
    )


class TestFlowAdapter:
    def test_to_onos_flow_rule_returns_expected_keys(self):
        adapter = FlowAdapter()
        entry = _make_entry()
        rule = adapter.to_onos_flow_rule(entry, "device:bmv2:1")

        assert rule["deviceId"] == "device:bmv2:1"
        assert rule["priority"] == 100
        assert rule["isPermanent"] is True
        assert rule["timeout"] == 0
        assert "tableId" in rule
        assert "treatment" in rule
        assert "selector" in rule

    def test_to_onos_flow_rule_table_id_from_registry(self):
        adapter = FlowAdapter()
        entry = _make_entry(table_name="MyIngress.ipv4_routing")
        rule = adapter.to_onos_flow_rule(entry, "device:bmv2:1")
        assert rule["tableId"] == p4info_registry.table_flow_type("MyIngress.ipv4_routing")

    def test_to_onos_flow_rule_different_table_different_id(self):
        adapter = FlowAdapter()
        entry = _make_entry(table_name="MyIngress.sla_enforcement",
                            action_name="MyIngress.apply_meter_policy",
                            match_fields=[
                                MatchField(field_name="hdr.ipv4.srcAddr", match_type="lpm", value="192.168.0.0", prefix_len=16),
                                MatchField(field_name="hdr.ipv4.dstAddr", match_type="ternary", value="10.0.0.0", prefix_len=8),
                            ],
                            action_params=[ActionParam(param_name="port", value=46)])
        rule = adapter.to_onos_flow_rule(entry, "device:bmv2:1")
        assert rule["tableId"] == p4info_registry.table_flow_type("MyIngress.sla_enforcement")

    def test_treatment_builds_protocol_independent_action(self):
        adapter = FlowAdapter()
        entry = _make_entry()
        treatment = adapter._build_treatment(entry)

        instructions = treatment["instructions"]
        assert len(instructions) == 1
        instr = instructions[0]
        assert instr["type"] == "PROTOCOL_INDEPENDENT"
        assert instr["subtype"] == "ACTION"
        assert instr["actionId"] == "MyIngress.set_nhop"

    def test_treatment_action_params_are_hex_mapped_by_param_id(self):
        adapter = FlowAdapter()
        entry = _make_entry(action_params=[
            ActionParam(param_name="port", value=1),
            ActionParam(param_name="srcMac", value="000000000011"),
        ])
        treatment = adapter._build_treatment(entry)
        params = treatment["instructions"][0]["actionParams"]

        assert params["port"] == "0001"
        # srcMac is bitwidth 48 → 12 hex chars
        assert params["srcMac"] == "000000000011"

    def test_selector_builds_pi_criteria(self):
        adapter = FlowAdapter()
        entry = _make_entry()
        selector = adapter._build_selector(entry)

        criteria = selector["criteria"]
        assert len(criteria) == 1
        assert criteria[0]["type"] == "PROTOCOL_INDEPENDENT"

    def test_lpm_match_encodes_value_and_prefix(self):
        adapter = FlowAdapter()
        entry = _make_entry(
            match_fields=[
                MatchField(field_name="hdr.ipv4.dstAddr", match_type="lpm", value="10.0.0.0", prefix_len=8),
            ],
        )
        selector = adapter._build_selector(entry)
        match = selector["criteria"][0]["matches"][0]

        assert match["field"] == "hdr.ipv4.dstAddr"
        assert match["match"] == "lpm"
        assert match["value"] == "0a000000"
        assert match["prefixLength"] == 8

    def test_ternary_match_encodes_value_and_mask(self):
        adapter = FlowAdapter()
        entry = _make_entry(
            table_name="MyIngress.acl",
            action_name="MyIngress.acl_deny",
            match_fields=[
                MatchField(field_name="hdr.ipv4.srcAddr", match_type="ternary", value="10.0.0.0", prefix_len=8),
            ],
            action_params=[],
        )
        selector = adapter._build_selector(entry)
        match = selector["criteria"][0]["matches"][0]

        assert match["match"] == "ternary"
        assert match["value"] == "0a000000"
        assert match["mask"] == "ff000000"

    def test_ternary_match_with_explicit_mask(self):
        adapter = FlowAdapter()
        entry = _make_entry(
            table_name="MyIngress.acl",
            action_name="MyIngress.acl_deny",
            match_fields=[
                MatchField(field_name="hdr.ipv4.srcAddr", match_type="ternary", value="10.0.0.0", mask="255.255.0.0"),
            ],
            action_params=[],
        )
        selector = adapter._build_selector(entry)
        match = selector["criteria"][0]["matches"][0]

        assert match["mask"] == "ffff0000"

    def test_drop_packet_entry_has_no_params(self):
        adapter = FlowAdapter()
        entry = _make_entry(
            table_name="MyIngress.ipv4_routing",
            action_name="MyIngress.drop_packet",
            action_params=[],
        )
        treatment = adapter._build_treatment(entry)
        assert treatment["instructions"][0]["actionParams"] == {}

    def test_batch_convert_groups_by_device(self):
        adapter = FlowAdapter()
        entry_a = _make_entry()
        entry_b = _make_entry(action_name="MyIngress.drop_packet", action_params=[])

        requests = [
            WriteRequest(device_id="device:bmv2:1", table_entries=[entry_a], atomicity=AtomicityMode.ROLLBACK_ON_ERROR),
            WriteRequest(device_id="device:bmv2:2", table_entries=[entry_b], atomicity=AtomicityMode.ROLLBACK_ON_ERROR),
        ]
        result = adapter.batch_convert(requests)

        assert "device:bmv2:1" in result
        assert "device:bmv2:2" in result
        assert len(result["device:bmv2:1"]) == 1
        assert len(result["device:bmv2:2"]) == 1

    def test_batch_convert_multiple_entries_same_device(self):
        adapter = FlowAdapter()
        entry_a = _make_entry()
        entry_b = _make_entry(action_name="MyIngress.drop_packet", action_params=[])

        requests = [
            WriteRequest(device_id="device:bmv2:1", table_entries=[entry_a, entry_b], atomicity=AtomicityMode.ROLLBACK_ON_ERROR),
        ]
        result = adapter.batch_convert(requests)

        assert len(result["device:bmv2:1"]) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# P4InfoRegistry
# ═══════════════════════════════════════════════════════════════════════════════

class TestP4InfoRegistry:
    def test_table_id_lookup(self):
        tid = p4info_registry.table_id("MyIngress.ipv4_routing")
        assert isinstance(tid, int)
        assert tid > 0

    def test_action_id_lookup(self):
        aid = p4info_registry.action_id("MyIngress.set_nhop")
        assert isinstance(aid, int)
        assert aid > 0

    def test_param_id_lookup(self):
        pid = p4info_registry.param_id("MyIngress.set_nhop", "port")
        assert pid == 1

    def test_param_bitwidth(self):
        bw = p4info_registry.param_bitwidth("MyIngress.set_nhop", "srcMac")
        assert bw == 48
        bw = p4info_registry.param_bitwidth("MyIngress.set_nhop", "port")
        assert bw == 9

    def test_param_bitwidth_default(self):
        bw = p4info_registry.param_bitwidth("NoAction", "nonexistent")
        assert bw == 32

    def test_table_size(self):
        size = p4info_registry.table_size("MyIngress.ipv4_routing")
        assert size == 1024

    def test_table_match_fields(self):
        fields = p4info_registry.table_match_fields("MyIngress.ipv4_routing")
        assert "hdr.ipv4.dstAddr" in fields
        assert fields["hdr.ipv4.dstAddr"]["match_type"] in ("LPM", "TERNARY")

    def test_match_type(self):
        mt = p4info_registry.match_type("MyIngress.ipv4_routing", "hdr.ipv4.dstAddr")
        assert mt in ("LPM", "TERNARY")

    def test_to_dict_has_expected_top_level_keys(self):
        d = p4info_registry.to_dict()
        assert "tables" in d
        assert "actions" in d
        assert "meters" in d

    def test_to_dict_tables_have_required_fields(self):
        d = p4info_registry.to_dict()
        for table in d["tables"]:
            assert "name" in table
            assert "match_fields" in table
            assert "action_refs" in table
            assert "size" in table

    def test_to_dict_actions_have_required_fields(self):
        d = p4info_registry.to_dict()
        for action in d["actions"]:
            assert "name" in action
            assert "params" in action
            for param in action["params"]:
                assert "name" in param
                assert "bitwidth" in param

    def test_to_dict_includes_meters(self):
        d = p4info_registry.to_dict()
        assert isinstance(d["meters"], list)
        for meter in d["meters"]:
            assert "name" in meter
            assert "size" in meter

    def test_table_actions_returns_list_of_dicts(self):
        actions = p4info_registry.table_actions("MyIngress.ipv4_routing")
        assert isinstance(actions, list)
        for a in actions:
            assert "name" in a
            assert "id" in a

    def test_direct_meter_id_lookup(self):
        mid = p4info_registry.direct_meter_id("MyIngress.host_meter")
        assert mid == 355791019


# ═══════════════════════════════════════════════════════════════════════════════
# FlowDelivery
# ═══════════════════════════════════════════════════════════════════════════════

class TestFlowDeliveryMock:
    """Verify FlowDelivery ABC contract via a minimal stub."""

    @pytest.mark.asyncio
    async def test_flow_delivery_factory_creates_onos_rest(self):
        delivery = FlowDeliveryFactory.create()
        assert isinstance(delivery, ONOSRestFlowDelivery)

    @pytest.mark.asyncio
    async def test_onos_rest_health_check_structure(self):
        """Health check returns expected keys even when ONOS is unreachable."""
        delivery = ONOSRestFlowDelivery()
        try:
            result = await delivery.check_health()
            assert "status" in result
        finally:
            await delivery.onos.close()

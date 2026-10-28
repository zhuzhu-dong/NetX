from __future__ import annotations

from typing import Any

from api.baseline import build_baseline_onos_rules, build_baseline_write_requests
from mcp_servers.flow_adapter import FlowAdapter, _value_to_hex
from mcp_servers.p4info_registry import p4info_registry
from models.flow_table import ActionParam, MatchField, TableEntry


def test_flow_adapter_uses_p4_names_for_pi_action_and_params():
    entry = TableEntry(
        table_name="MyIngress.ipv4_routing",
        match_fields=[
            MatchField(
                field_name="hdr.ipv4.dstAddr",
                match_type="lpm",
                value="192.168.10.0",
                prefix_len=24,
            ),
        ],
        action_name="MyIngress.set_nhop",
        action_params=[
            ActionParam(param_name="port", value=2, bitwidth=9),
            ActionParam(param_name="srcMac", value="000000000011"),
            ActionParam(param_name="dstMac", value="000000000022"),
        ],
        priority=100,
    )

    rule = FlowAdapter().to_onos_flow_rule(entry, "device:bmv2:1")

    assert rule["tableId"] == p4info_registry.table_flow_type("MyIngress.ipv4_routing")
    instruction = rule["treatment"]["instructions"][0]
    assert instruction["actionId"] == "MyIngress.set_nhop"
    assert instruction["actionParams"]["port"] == "0002"
    assert instruction["actionParams"]["srcMac"] == "000000000011"
    assert instruction["actionParams"]["dstMac"] == "000000000022"


def test_flow_adapter_pads_9_bit_port_params_for_ipv4_forwarding():
    entry = TableEntry(
        table_name="MyIngress.ipv4_routing",
        match_fields=[
            MatchField(
                field_name="hdr.ipv4.dstAddr",
                match_type="lpm",
                value="10.0.0.10",
                prefix_len=32,
            )
        ],
        action_name="MyIngress.set_nhop",
        action_params=[
            ActionParam(param_name="port", value=1),
            ActionParam(param_name="srcMac", value="020000000000"),
            ActionParam(param_name="dstMac", value="020000000001"),
        ],
        priority=10,
    )

    rule = FlowAdapter().to_onos_flow_rule(entry, "device:bmv2:1")

    params = rule["treatment"]["instructions"][0]["actionParams"]
    assert params["port"] == "0001"
    assert params["srcMac"] == "020000000000"
    assert params["dstMac"] == "020000000001"


def test_value_to_hex_normalizes_prefixed_and_short_hex_without_ascii_encoding():
    assert _value_to_hex("0xc0a80a0a", 32) == "c0a80a0a"
    assert _value_to_hex("0xff", 8) == "ff"
    assert _value_to_hex("0", 8) == "00"


def test_sla_selector_omits_zero_mask_wildcard_and_preserves_field_widths():
    entry = TableEntry(
        table_name="MyIngress.sla_enforcement",
        match_fields=[
            MatchField(
                field_name="hdr.ipv4.srcAddr",
                match_type="ternary",
                value="0xc0a80a0a",
                mask="0xffffffff",
            ),
            MatchField(
                field_name="hdr.ipv4.dstAddr",
                match_type="ternary",
                value="0x0a00000a",
                mask="0xffffffff",
            ),
            MatchField(
                field_name="hdr.ipv4.protocol",
                match_type="ternary",
                value="0x11",
                mask="0xff",
            ),
            MatchField(
                field_name="hdr.ipv4.diffserv",
                match_type="ternary",
                value="0",
                mask="0",
            ),
        ],
        action_name="MyIngress.set_dscp",
        action_params=[ActionParam(param_name="dscp_value", value=46)],
        priority=100,
    )

    rule = FlowAdapter().to_onos_flow_rule(entry, "device:bmv2:5")
    matches = rule["selector"]["criteria"][0]["matches"]

    assert [match["field"] for match in matches] == [
        "hdr.ipv4.srcAddr",
        "hdr.ipv4.dstAddr",
        "hdr.ipv4.protocol",
    ]
    assert matches[0]["value"] == "c0a80a0a"
    assert matches[0]["mask"] == "ffffffff"
    assert matches[2]["value"] == "11"
    assert matches[2]["mask"] == "ff"


def test_baseline_connectivity_requires_topology_data():
    """With static BASELINE_RULES removed, baseline generation requires topology."""
    import pytest

    with pytest.raises(RuntimeError, match="topology"):
        build_baseline_onos_rules(None)


def test_baseline_endpoint_pairs_come_from_service_profiles(monkeypatch):
    import api.baseline as baseline

    calls = []

    def fake_rules(_topology, src_host, dst_host):
        calls.append((src_host, dst_host))
        return [
            baseline.ForwardingRule(
                device_id=f"device:{src_host}:{dst_host}",
                destination_ip=f"10.255.0.{len(calls)}",
                egress_port=1,
                src_mac="020000000001",
                dst_mac="020000000002",
            )
        ]

    monkeypatch.setattr(baseline, "_dynamic_forwarding_rules", fake_rules)
    topology = {
        "status": "ok",
        "hosts": [
            {"name": name}
            for name in ("h_research", "h_video", "h_ai", "h_dc", "h_backup")
        ],
    }
    baseline._rules_from_topology_or_fallback(topology)
    assert set(calls) == {
        ("h_research", "h_dc"),
        ("h_video", "h_dc"),
        ("h_ai", "h_dc"),
        ("h_backup", "h_research"),
    }


def test_baseline_shortest_path_excludes_runtime_down_links():
    import api.baseline as baseline

    def link(link_id, left, right, *, cost, state="ACTIVE"):
        return {
            "id": link_id, "cost": cost, "state": state,
            "endpoints": [{"node": left}, {"node": right}],
        }

    topology = {"links": [
        link("src-primary", "src", "primary", cost=1),
        link("primary-dst", "primary", "dst", cost=1, state="DOWN"),
        link("src-backup", "src", "backup", cost=2),
        link("backup-dst", "backup", "dst", cost=2),
    ]}

    assert baseline._shortest_path(topology, "src", "dst") == [
        "src", "backup", "dst",
    ]


def test_baseline_connectivity_can_be_generated_from_dynamic_topology(
    topo_yaml,
    topo_device_ids,
):
    """Build a topology dict from the YAML fixture and verify forwarding rules."""
    # Construct a topology snapshot dict from the YAML fixture
    # (mimics what the Mininet probe server returns at /topology)
    topology: dict[str, Any] = {
        "status": "ok",
        "source": "mininet_probe",
        "hosts": [
            {
                "name": h["name"],
                "ip": h["ip"].split("/")[0],
                "mac": h["mac"],
                "gateway_ip": topo_yaml["subnets"][h["subnet"]]["gateway_ip"],
                "gateway_mac": topo_yaml["subnets"][h["subnet"]]["gateway_mac"],
            }
            for h in topo_yaml.get("hosts", [])
        ],
        "switches": [
            {
                "name": s["name"],
                "device_id": f"device:bmv2:{s['device_id']}",
                "router_mac": s["router_mac"],
            }
            for s in topo_yaml.get("switches", [])
        ],
        "links": _build_topology_links_from_yaml(topo_yaml),
    }

    rules = build_baseline_onos_rules(topology)

    # Should produce rules for the switches on the primary path
    assert set(rules) == set(topo_device_ids) - {"device:bmv2:2"}
    # The baseline expands with ServiceProfile and host-resolution changes.
    # Assert adapter completeness against the current generated IR, not an
    # obsolete hard-coded rule count.
    expected_entries = sum(
        len(request.table_entries)
        for request in build_baseline_write_requests(topology)
    )
    assert sum(len(device_rules) for device_rules in rules.values()) == expected_entries
    arp_table = p4info_registry.table_flow_type("MyIngress.arp_proxy")
    arp_rules = [
        rule
        for device_rules in rules.values()
        for rule in device_rules
        if rule["tableId"] == arp_table
    ]
    assert len(arp_rules) == 3
    assert {
        rule["selector"]["criteria"][0]["matches"][0]["value"] for rule in arp_rules
    } == {
        "c0a80a01",
        "c0a81401",
        "0a000001",
    }
    assert all(
        rule["tableId"] == p4info_registry.table_flow_type("MyIngress.ipv4_routing")
        for device_rules in rules.values()
        for rule in device_rules
        if rule["tableId"] == p4info_registry.table_flow_type("MyIngress.ipv4_routing")
    )
    route_rules = [
        rule
        for device_rules in rules.values()
        for rule in device_rules
        if rule["tableId"] == p4info_registry.table_flow_type("MyIngress.ipv4_routing")
    ]
    assert all(
        rule["treatment"]["instructions"][0]["actionParams"][name] != "000000000000"
        for rule in route_rules
        for name in ("srcMac", "dstMac")
    )


def _build_topology_links_from_yaml(topo_yaml: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert YAML link definitions to the topology snapshot format.

    Ports are assigned sequentially per switch based on link ordering in YAML.
    """
    switch_port_counter: dict[str, int] = {}
    device_ids = {
        item["name"]: f'device:bmv2:{item["device_id"]}'
        for item in topo_yaml.get("switches", [])
    }
    result = []
    for link in topo_yaml.get("links", []):
        a, b = link["a"], link["b"]
        a_port = switch_port_counter.get(a, 0) + 1
        switch_port_counter[a] = a_port
        b_port = switch_port_counter.get(b, 0) + 1
        switch_port_counter[b] = b_port

        def ep(node: str, port: int) -> dict[str, Any]:
            is_switch = node.startswith("s_")
            ep_data: dict[str, Any] = {
                "node": node,
                "type": "switch" if is_switch else "host",
            }
            if is_switch:
                ep_data["port"] = port
                ep_data["device_id"] = device_ids[node]
            return ep_data

        result.append(
            {
                "id": f"{a}-{b}",
                "cost": float(link.get("cost", 1)),
                "endpoints": [ep(a, a_port), ep(b, b_port)],
            }
        )
    return result

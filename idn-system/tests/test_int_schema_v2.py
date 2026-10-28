from __future__ import annotations

import json
import sys
from pathlib import Path

_mounted_workspace = Path("/workspace")
ROOT = (
    _mounted_workspace
    if (_mounted_workspace / "mininet" / "int_schema.py").exists()
    else Path(__file__).resolve().parents[2]
)
sys.path.insert(0, str(ROOT / "mininet"))

from int_schema import decode_header, decode_hops, encode_header, encode_hop, parse_ethernet_frame, schema


def _record(switch_id: int, ingress: int, egress: int, start: int) -> dict:
    return {
        "switch_id": switch_id,
        "ingress_port": ingress,
        "egress_port": egress,
        "ingress_timestamp_us": start,
        "egress_timestamp_us": start + 7,
        "queue_occupancy": switch_id + 2,
        "flags": 1,
    }


def test_int_schema_widths_and_generated_p4_constants_are_in_sync():
    cfg = schema()
    assert cfg["header"]["length_bytes"] == 6
    assert cfg["hop_record"]["length_bytes"] == 26
    assert sum(item["bit_width"] for item in cfg["header"]["fields"]) == 48
    assert sum(item["bit_width"] for item in cfg["hop_record"]["fields"]) == 208
    generated = (ROOT / "p4" / "int_schema.p4").read_text(encoding="utf-8")
    assert "IDN_INT_MAGIC = 0x4944" in generated
    assert f"IDN_INT_VERSION = {cfg['schema_version']}" in generated


def test_p4_marks_each_pushed_int_hop_valid_before_field_assignment():
    source = (ROOT / "p4" / "idn_basic.p4").read_text(encoding="utf-8")
    pushed = source.index("hdr.int_hops.push_front(1);")
    made_valid = source.index("hdr.int_hops[0].setValid();", pushed)
    first_write = source.index("hdr.int_hops[0].switch_id = switch_id;", pushed)
    assert pushed < made_valid < first_write


def test_sla_classifier_declares_both_udp_and_tcp_destination_ports():
    source = (ROOT / "p4" / "idn_basic.p4").read_text(encoding="utf-8")
    table = source[source.index("table sla_enforcement"):source.index("table bandwidth_policy")]
    assert "hdr.udp.dstPort: ternary;" in table
    assert "hdr.tcp.dstPort: ternary;" in table


def test_int_schema_round_trip_restores_source_to_destination_order():
    source, destination = _record(5, 1, 3, 1000), _record(3, 2, 4, 1100)
    # P4 header-stack push_front serializes the newest (destination-side) hop first.
    wire = encode_hop(destination) + encode_hop(source)
    decoded = decode_hops(wire, 2, wire_newest_first=True)
    assert decoded == [source, destination]


def test_magic_version_frame_parser_has_no_legacy_heuristic():
    cfg = schema()
    hop = _record(7, 1, 2, 123456)
    ethernet = bytes.fromhex("00112233445566778899aabb") + int(cfg["ether_type"]).to_bytes(2, "big")
    intent = bytes((0, 0, 1, 8))
    ipv4 = bytes.fromhex("450000140000000040fd00000a0000010a000002")
    frame = ethernet + intent + encode_header(1) + encode_hop(hop) + ipv4
    parsed = parse_ethernet_frame(frame)
    assert parsed["header"] == decode_header(encode_header(1))
    assert parsed["hops"] == [hop]

    bad = bytearray(frame)
    bad[18:20] = b"\x00\x00"
    try:
        parse_ethernet_frame(bytes(bad))
    except ValueError as exc:
        assert "MAGIC" in str(exc)
    else:
        raise AssertionError("legacy/heuristic frame was accepted")

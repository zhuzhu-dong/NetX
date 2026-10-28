"""Canonical IDN INT v2 wire codec shared by probe receiver and tests."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "idn-system" / "config" / "int_schema.json"


@lru_cache(maxsize=1)
def schema() -> dict:
    path = Path(os.getenv("IDN_INT_SCHEMA_FILE", "/app/config/int_schema.json"))
    if not path.exists():
        path = _DEFAULT_SCHEMA
    value = json.loads(path.read_text(encoding="utf-8"))
    header_bits = sum(int(item["bit_width"]) for item in value["header"]["fields"])
    hop_bits = sum(int(item["bit_width"]) for item in value["hop_record"]["fields"])
    if header_bits != int(value["header"]["length_bytes"]) * 8:
        raise ValueError("INT_SCHEMA_INVALID: header length")
    if hop_bits != int(value["hop_record"]["length_bytes"]) * 8:
        raise ValueError("INT_SCHEMA_INVALID: hop record length")
    if value.get("byte_order") != "big":
        raise ValueError("INT_SCHEMA_INVALID: only network byte order is supported")
    return value


def _uint(value: int, length: int) -> bytes:
    return int(value).to_bytes(length, "big", signed=False)


def encode_header(hop_count: int, *, flags: int = 0) -> bytes:
    cfg = schema()
    if not 0 <= int(hop_count) <= int(cfg["max_hops"]):
        raise ValueError("hop_count outside schema range")
    return b"".join((
        _uint(cfg["header"]["magic"], 2),
        _uint(cfg["schema_version"], 1),
        _uint(hop_count, 1),
        _uint(cfg["max_hops"], 1),
        _uint(flags, 1),
    ))


def decode_header(data: bytes) -> dict:
    cfg = schema()
    length = int(cfg["header"]["length_bytes"])
    if len(data) < length:
        raise ValueError("INT_HEADER_TRUNCATED")
    result = {
        "magic": int.from_bytes(data[0:2], "big"),
        "version": data[2],
        "hop_count": data[3],
        "max_hops": data[4],
        "flags": data[5],
    }
    if result["magic"] != int(cfg["header"]["magic"]):
        raise ValueError("INT_MAGIC_MISMATCH")
    if result["version"] != int(cfg["schema_version"]):
        raise ValueError("INT_VERSION_UNSUPPORTED")
    if result["max_hops"] != int(cfg["max_hops"]):
        raise ValueError("INT_MAX_HOPS_MISMATCH")
    if result["hop_count"] > result["max_hops"]:
        raise ValueError("INT_HOP_COUNT_INVALID")
    return result


def encode_hop(record: dict) -> bytes:
    return b"".join((
        _uint(record["switch_id"], 4),
        _uint(record["ingress_port"], 2),
        _uint(record["egress_port"], 2),
        _uint(record["ingress_timestamp_us"], 6),
        _uint(record["egress_timestamp_us"], 6),
        _uint(record["queue_occupancy"], 4),
        _uint(record.get("flags", 1), 2),
    ))


def decode_hop(data: bytes) -> dict:
    length = int(schema()["hop_record"]["length_bytes"])
    if len(data) != length:
        raise ValueError("INT_HOP_LENGTH_INVALID")
    return {
        "switch_id": int.from_bytes(data[0:4], "big"),
        "ingress_port": int.from_bytes(data[4:6], "big"),
        "egress_port": int.from_bytes(data[6:8], "big"),
        "ingress_timestamp_us": int.from_bytes(data[8:14], "big"),
        "egress_timestamp_us": int.from_bytes(data[14:20], "big"),
        "queue_occupancy": int.from_bytes(data[20:24], "big"),
        "flags": int.from_bytes(data[24:26], "big"),
    }


def decode_hops(data: bytes, hop_count: int, *, wire_newest_first: bool = True) -> list[dict]:
    width = int(schema()["hop_record"]["length_bytes"])
    if len(data) != int(hop_count) * width:
        raise ValueError("INT_HOPS_LENGTH_MISMATCH")
    records = [decode_hop(data[offset:offset + width]) for offset in range(0, len(data), width)]
    return list(reversed(records)) if wire_newest_first else records


def parse_ethernet_frame(frame: bytes) -> dict:
    """Validate and split one Ethernet + intent + INT + IPv4 frame."""
    cfg = schema()
    if len(frame) < 14 + 4 + int(cfg["header"]["length_bytes"]) + 20:
        raise ValueError("INT_FRAME_TRUNCATED")
    ether_type = int.from_bytes(frame[12:14], "big")
    if ether_type != int(cfg["ether_type"]):
        raise ValueError("INT_ETHERTYPE_MISMATCH")
    intent = frame[14:18]
    if intent[2] != 1 or intent[3] != 0x08:
        raise ValueError("INT_INTENT_HEADER_INVALID")
    header_offset = 18
    header = decode_header(frame[header_offset:])
    hop_offset = header_offset + int(cfg["header"]["length_bytes"])
    hop_end = hop_offset + header["hop_count"] * int(cfg["hop_record"]["length_bytes"])
    if len(frame) < hop_end + 20:
        raise ValueError("INT_FRAME_HOPS_TRUNCATED")
    ipv4 = frame[hop_end:]
    if ipv4[0] >> 4 != 4:
        raise ValueError("INT_INNER_IPV4_INVALID")
    return {
        "header": header,
        "hops_wire": frame[hop_offset:hop_end],
        "hops": decode_hops(frame[hop_offset:hop_end], header["hop_count"]),
        "ipv4": ipv4,
    }

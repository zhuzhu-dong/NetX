"""
infra/p4_counter_reader.py
P4 direct-counter reader — gRPC-based, using p4info_registry for P4Info.
Replaces the former ``mcp_servers/p4runtime_server.py`` P4RuntimeClient.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from mcp_servers.p4info_registry import p4info_registry

logger = logging.getLogger(__name__)

# See p4info_registry for the authoritative P4Info — no hardcoded copies.


def _parse_device_id(device_id: str) -> int:
    """Extract numeric ID from ``device:bmv2:N`` strings."""
    try:
        return int(device_id.split(":")[-1])
    except (ValueError, IndexError, AttributeError):
        return 1


def _grpc_port(device_id: str) -> int:
    """BMv2 gRPC port: 50050 + numeric device ID."""
    return 50050 + _parse_device_id(device_id)


def _p4runtime_node_id() -> int:
    """Return the local node ID used inside each standalone Stratum process."""
    return int(os.getenv("P4RUNTIME_NODE_ID", "1"))


async def read_direct_counters(device_id: str) -> dict[str, Any] | None:
    """Read direct counters from a single BMv2 switch via P4Runtime gRPC.

    Uses ``p4info_registry`` to resolve table/action IDs at runtime —
    always in sync with the compiled P4 program.

    Returns None when gRPC is unavailable or the read fails.
    """
    try:
        import grpc
        from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc
    except ImportError:
        logger.debug("grpcio / p4runtime not installed — counter read skipped")
        return None

    host = os.getenv("P4RUNTIME_SWITCH_HOST", "localhost")
    port = _grpc_port(device_id)
    # Each logical switch is exposed by a distinct gRPC port, but every
    # Stratum process has one local chassis node whose P4Runtime device_id is
    # 1.  The numeric suffix in device:bmv2:N selects the endpoint only.
    numeric_id = _p4runtime_node_id()

    try:
        channel = grpc.insecure_channel(f"{host}:{port}")
        stub = p4runtime_pb2_grpc.P4RuntimeStub(channel)
    except Exception as exc:
        logger.warning("P4Runtime gRPC connect failed for %s:%d: %s", host, port, exc)
        return None

    # Resolve table IDs from p4info_registry (authoritative source)
    tables_by_id: dict[int, str] = {}
    all_table_names = p4info_registry.table_names()
    for tname in all_table_names:
        tid = p4info_registry.table_id(tname)
        tables_by_id[tid] = tname

    # Build ReadRequest for direct counters on every table that has one
    request = p4runtime_pb2.ReadRequest()
    request.device_id = numeric_id

    # Check which tables have direct counters (from P4Info compiled data)
    direct_counter_tables: set[str] = set()
    try:
        p4info_dict = p4info_registry.to_dict()
        for counter in p4info_dict.get("direct_counters", []):
            parent = counter.get("direct_table") or counter.get("parent_table") or ""
            if parent:
                direct_counter_tables.add(parent)
    except Exception:
        pass

    for tname in all_table_names:
        if tname in direct_counter_tables:
            entity = request.entities.add()
            table_entry = entity.table_entry
            table_entry.table_id = p4info_registry.table_id(tname)
            if hasattr(table_entry, "counter_data"):
                table_entry.counter_data.SetInParent()

    if not request.entities:
        channel.close()
        return {"packets": 0, "bytes": 0, "direct_entries": 0}

    try:
        entries: list[dict[str, Any]] = []
        total_packets = 0
        total_bytes = 0
        timeout = float(os.getenv("P4RUNTIME_COUNTER_READ_TIMEOUT_SEC", "0.75"))
        for response in stub.Read(request, timeout=timeout):
            for entity in response.entities:
                if entity.HasField("table_entry"):
                    te = entity.table_entry
                    if te.HasField("counter_data"):
                        packets = int(te.counter_data.packet_count)
                        byte_count = int(te.counter_data.byte_count)
                        total_packets += packets
                        total_bytes += byte_count
                        entries.append({
                            "table_id": int(te.table_id),
                            "table_name": tables_by_id.get(int(te.table_id), str(te.table_id)),
                            "priority": int(te.priority),
                            "packets": packets,
                            "bytes": byte_count,
                        })
        channel.close()
        return {
            "packets": total_packets,
            "bytes": total_bytes,
            "direct_entries": len(entries),
            "entries": entries,
        }
    except Exception as exc:
        logger.debug("P4Runtime counter read failed for %s: %s", device_id, exc)
        try:
            channel.close()
        except Exception:
            pass
        return None

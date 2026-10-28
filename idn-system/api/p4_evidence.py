"""Exact, service-scoped P4Runtime evidence for table-bound direct counters."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from ipaddress import ip_network
from typing import Any

try:
    import grpc
except ImportError:  # pragma: no cover - lightweight host test environment
    class _GrpcUnavailable:
        class RpcError(Exception):
            pass

        class FutureTimeoutError(TimeoutError):
            pass

        def __getattr__(self, name: str):
            raise RuntimeError(f"grpc runtime is unavailable ({name})")

    grpc = _GrpcUnavailable()  # type: ignore[assignment]



def _p4info_registry():
    # Keep API importable in lightweight host test environments that do not
    # install the P4Runtime protobuf wheel.  Live evidence reads resolve the
    # registry lazily and the production backend image includes that runtime.
    from mcp_servers.p4info_registry import p4info_registry

    return p4info_registry


P4_EVIDENCE_STATUSES = {
    "VERIFIED",
    "MATCHING_ENTRY_NO_TRAFFIC",
    "NO_MATCHING_ENTRY",
    "READ_FAILED",
    "DEVICE_UNAVAILABLE",
    "TABLE_NOT_FOUND",
    "COUNTER_NOT_BOUND",
    "SELECTOR_MISMATCH",
    "P4RUNTIME_ERROR",
    "UNSUPPORTED",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_ip_protocol(value: Any) -> int:
    """Normalize protocol names, decimal values and explicit hexadecimal."""
    raw = getattr(value, "value", value)
    text = str(raw if raw is not None else "").strip().lower()
    names = {"icmp": 1, "tcp": 6, "udp": 17}
    if text in names:
        return names[text]
    if text.startswith("0x"):
        return int(text, 16)
    return int(text, 10)


def canonical_pi_value_matches(field: str, actual: Any, expected_hex: str) -> bool:
    """Compare ONOS PI renderings with the canonical P4 byte value.

    ONOS can render the one-byte IP protocol as decimal (``17``) or as the
    underlying P4 bytes (``11`` / ``0x11``).  Wider fields remain strict hex.
    """
    actual_text = str(actual if actual is not None else "").strip().lower()
    expected_text = str(expected_hex).strip().lower().removeprefix("0x")
    explicit_hex = actual_text.startswith("0x")
    actual_text = actual_text.removeprefix("0x")
    if actual_text == expected_text:
        return True
    if field != "hdr.ipv4.protocol" or not actual_text:
        return False
    try:
        expected = int(expected_text, 16)
        if explicit_hex:
            return int(actual_text, 16) == expected
        # An all-numeric ONOS rendering is ambiguous.  Accept either the
        # decimal semantic value or the P4 byte rendering.
        return int(actual_text, 10) == expected or int(actual_text, 16) == expected
    except ValueError:
        return False


def canonical_service_selector(profile: Any) -> dict[str, Any]:
    return {
        "src": str(profile.src_ip),
        "dst": str(profile.dst_ip),
        "protocol": canonical_ip_protocol(profile.protocol),
        "dst_port": int(profile.dst_port) if profile.dst_port is not None else None,
        "dscp": int(profile.dscp) if profile.dscp is not None else None,
    }


def selector_match_fields(selector: dict[str, Any]) -> dict[str, str]:
    """Build the protocol-correct exact P4 selector for evidence reads.

    ``sla_enforcement`` has distinct TCP and UDP destination-port fields.
    Direct-counter readback must use the transport selected by the policy;
    otherwise a valid TCP classifier is incorrectly reported as absent.
    """
    protocol = int(selector["protocol"])
    fields = {
        "hdr.ipv4.srcAddr": ip_network(selector["src"], strict=False).network_address.packed.hex(),
        "hdr.ipv4.dstAddr": ip_network(selector["dst"], strict=False).network_address.packed.hex(),
        "hdr.ipv4.protocol": protocol.to_bytes(1, "big").hex(),
    }
    destination_port = selector.get("dst_port")
    if destination_port is not None and protocol in {6, 17}:
        transport = "tcp" if protocol == 6 else "udp"
        fields[f"hdr.{transport}.dstPort"] = int(destination_port).to_bytes(2, "big").hex()
    return fields


def _failure(
    status: str,
    reason_code: str,
    reason_detail: str,
    *,
    context: dict[str, Any],
    exception: BaseException | None = None,
    read_request: dict[str, Any] | None = None,
    read_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **context,
        "status": status,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "matching_entry_count": 0,
        "packets": 0,
        "bytes": 0,
        "read_request": read_request or {},
        "read_response": read_response or {},
        "exception": (
            {"type": type(exception).__name__, "detail": str(exception)}
            if exception is not None else None
        ),
        "read_at": _utc_now(),
    }


async def resolve_p4runtime_endpoint(profile: Any) -> dict[str, Any]:
    binding = dict(profile.p4_counter_binding or {})
    logical_device_id = str(binding.get("device_id") or "")
    switch_name = str((profile.qos_binding or {}).get("switch") or "")
    grpc_port = None
    try:
        from api._probe_session import get_probe_session

        probe = await get_probe_session()
        topology = await probe.get("/topology") or {}
        for item in topology.get("switches") or []:
            if item.get("device_id") == logical_device_id:
                grpc_port = int(item.get("grpc_port"))
                switch_name = str(item.get("name") or switch_name)
                break
    except Exception:
        # The read below returns DEVICE_UNAVAILABLE with the full endpoint if
        # the fallback cannot actually be reached.
        pass
    try:
        logical_number = int(logical_device_id.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        logical_number = 0
    if grpc_port is None and logical_number:
        grpc_port = 50050 + logical_number
    host = os.getenv("P4RUNTIME_SWITCH_HOST", "mininet")
    node_id = int(os.getenv("P4RUNTIME_NODE_ID", "1"))
    return {
        "device_id": logical_device_id,
        "logical_device_number": logical_number,
        "switch": switch_name,
        "grpc_host": host,
        "grpc_port": grpc_port,
        "grpc_endpoint": f"{host}:{grpc_port}" if grpc_port else None,
        # Each Stratum process has its own endpoint and chassis node 1.  This
        # is intentionally distinct from ONOS' logical device:bmv2:N URI.
        "p4runtime_node_id": node_id,
    }


def _match_dict(table_entry: Any, table_name: str) -> dict[str, dict[str, Any]]:
    p4info_registry = _p4info_registry()
    by_id = {
        int(data["field_id"]): name
        for name, data in p4info_registry.table_match_fields(table_name).items()
    }
    result: dict[str, dict[str, Any]] = {}
    for item in table_entry.match:
        name = by_id.get(int(item.field_id), f"field:{item.field_id}")
        kind = item.WhichOneof("field_match_type")
        value_obj = getattr(item, kind) if kind else None
        value = bytes(getattr(value_obj, "value", b"")) if value_obj else b""
        entry = {"field_id": int(item.field_id), "match": kind, "value": value.hex()}
        if kind == "ternary":
            entry["mask"] = bytes(value_obj.mask).hex()
        elif kind == "lpm":
            entry["prefix_len"] = int(value_obj.prefix_len)
        result[name] = entry
    return result


def _entry_matches(
    table_entry: Any,
    table_name: str,
    selector: dict[str, Any],
    *,
    require_action: bool = True,
) -> bool:
    p4info_registry = _p4info_registry()
    matches = _match_dict(table_entry, table_name)
    expected = selector_match_fields(selector)
    fields_match = all(
        canonical_pi_value_matches(field, (matches.get(field) or {}).get("value"), value)
        for field, value in expected.items()
    )
    if not fields_match or not require_action or selector.get("dscp") is None:
        return fields_match
    try:
        action = table_entry.action.action
        action_name = "MyIngress.set_dscp"
        if int(action.action_id) != p4info_registry.action_id(action_name):
            return False
        param_id = p4info_registry.param_id(action_name, "dscp_value")
        return any(
            int(param.param_id) == int(param_id)
            and int.from_bytes(bytes(param.value), "big") == int(selector["dscp"])
            for param in action.params
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _entry_summary(table_entry: Any, table_name: str) -> dict[str, Any]:
    p4info_registry = _p4info_registry()
    action = table_entry.action.action
    action_name = next(
        (
            name for name in p4info_registry.action_names()
            if p4info_registry.action_id(name) == int(action.action_id)
        ),
        None,
    )
    return {
        "table_id": int(table_entry.table_id),
        "priority": int(table_entry.priority),
        "matches": _match_dict(table_entry, table_name),
        "action_id": int(action.action_id) if action else None,
        "action_name": action_name,
        "action_params": [
            {"param_id": int(param.param_id), "value": bytes(param.value).hex()}
            for param in action.params
        ] if action else [],
    }


def _read_entities_sync(
    endpoint: str,
    node_id: int,
    table_id: int,
    timeout: float,
) -> tuple[list[Any], list[Any]]:
    from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc

    channel = grpc.insecure_channel(endpoint)
    grpc.channel_ready_future(channel).result(timeout=timeout)
    stub = p4runtime_pb2_grpc.P4RuntimeStub(channel)
    request = p4runtime_pb2.ReadRequest(device_id=node_id)
    request.entities.add().table_entry.table_id = table_id
    request.entities.add().direct_counter_entry.table_entry.table_id = table_id
    table_entries: list[Any] = []
    counters: list[Any] = []
    try:
        for response in stub.Read(request, timeout=timeout):
            for entity in response.entities:
                kind = entity.WhichOneof("entity")
                if kind == "table_entry":
                    table_entries.append(entity.table_entry)
                elif kind == "direct_counter_entry":
                    counters.append(entity.direct_counter_entry)
    finally:
        channel.close()
    return table_entries, counters


def _read_table_entries_sync(
    endpoint: str,
    node_id: int,
    table_id: int,
    timeout: float,
) -> list[Any]:
    """Read one P4 table without requesting a direct counter.

    Forwarding, host-MAC and ARP tables do not have direct counters.  The
    classifier reader above intentionally asks for one, but using that request
    for a forwarding read causes Stratum to reject the whole RPC.
    """
    from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc

    channel = grpc.insecure_channel(endpoint)
    grpc.channel_ready_future(channel).result(timeout=timeout)
    stub = p4runtime_pb2_grpc.P4RuntimeStub(channel)
    request = p4runtime_pb2.ReadRequest(device_id=node_id)
    request.entities.add().table_entry.table_id = table_id
    entries: list[Any] = []
    try:
        for response in stub.Read(request, timeout=timeout):
            entries.extend(
                entity.table_entry
                for entity in response.entities
                if entity.WhichOneof("entity") == "table_entry"
            )
    finally:
        channel.close()
    return entries


async def read_p4_table_entries(device_id: str, table_name: str) -> dict[str, Any]:
    """Return actual table entries for a logical BMv2 device.

    This is deliberately a generic, read-only P4Runtime primitive so restart
    reconciliation can verify reachability tables independently of ONOS' flow
    inventory.  It does not install, delete, or infer entries.
    """
    registry = _p4info_registry()
    try:
        logical_number = int(str(device_id).rsplit(":", 1)[1])
        table_id = int(registry.table_id(table_name))
    except (IndexError, KeyError, ValueError) as exc:
        return {
            "status": "READ_FAILED",
            "device_id": device_id,
            "table_name": table_name,
            "entries": [],
            "error": str(exc),
        }
    endpoint = f"{os.getenv('P4RUNTIME_SWITCH_HOST', 'mininet')}:{50050 + logical_number}"
    node_id = int(os.getenv("P4RUNTIME_NODE_ID", "1"))
    timeout = max(float(os.getenv("P4RUNTIME_READ_TIMEOUT_SECONDS", "5")), 0.5)
    try:
        entries = await asyncio.to_thread(
            _read_table_entries_sync, endpoint, node_id, table_id, timeout,
        )
    except Exception as exc:
        return {
            "status": "READ_FAILED",
            "device_id": device_id,
            "table_name": table_name,
            "table_id": table_id,
            "endpoint": endpoint,
            "entries": [],
            "error": str(exc),
        }
    return {
        "status": "VERIFIED",
        "device_id": device_id,
        "table_name": table_name,
        "table_id": table_id,
        "endpoint": endpoint,
        "entries": [_entry_summary(entry, table_name) for entry in entries],
        "read_at": _utc_now(),
    }


def _read_indexed_counters_sync(
    endpoint: str,
    node_id: int,
    counters: dict[str, int],
    timeout: float,
) -> dict[str, dict[str, int]]:
    from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc

    channel = grpc.insecure_channel(endpoint)
    grpc.channel_ready_future(channel).result(timeout=timeout)
    stub = p4runtime_pb2_grpc.P4RuntimeStub(channel)
    request = p4runtime_pb2.ReadRequest(device_id=node_id)
    names_by_id = {int(counter_id): name for name, counter_id in counters.items()}
    for counter_id in names_by_id:
        entry = request.entities.add().counter_entry
        entry.counter_id = counter_id
        entry.index.index = 0
    result = {
        name: {"packets": 0, "bytes": 0, "index": 0}
        for name in counters
    }
    try:
        for response in stub.Read(request, timeout=timeout):
            for entity in response.entities:
                if entity.WhichOneof("entity") != "counter_entry":
                    continue
                entry = entity.counter_entry
                name = names_by_id.get(int(entry.counter_id))
                if name is None:
                    continue
                result[name] = {
                    "packets": int(entry.data.packet_count),
                    "bytes": int(entry.data.byte_count),
                    "index": int(entry.index.index),
                }
    finally:
        channel.close()
    return result


async def read_p4_diagnostic_counters(profile: Any) -> dict[str, Any]:
    """Read index 0 from every diagnostic counter for the profile device."""
    registry = _p4info_registry()
    endpoint = await resolve_p4runtime_endpoint(profile)
    names = [
        name for name in registry.counter_names()
        if name.rsplit(".", 1)[-1].startswith("diag_")
    ]
    counter_ids = {
        name: int(registry.counter_id(name))
        for name in names
        if registry.counter_id(name) is not None
    }
    context = {
        **endpoint,
        "service_id": profile.service_id,
        "counter_index": 0,
        "counter_ids": counter_ids,
        "read_at": _utc_now(),
    }
    if not endpoint.get("grpc_endpoint"):
        return {
            **context,
            "status": "DEVICE_UNAVAILABLE",
            "reason": "P4Runtime endpoint is unavailable",
            "counters": {},
        }
    timeout = max(float(os.getenv("P4RUNTIME_READ_TIMEOUT_SECONDS", "5")), 0.5)
    try:
        values = await asyncio.to_thread(
            _read_indexed_counters_sync,
            str(endpoint["grpc_endpoint"]),
            int(endpoint["p4runtime_node_id"]),
            counter_ids,
            timeout,
        )
    except Exception as exc:
        return {
            **context,
            "status": "READ_FAILED",
            "reason": str(exc),
            "counters": {},
        }
    return {
        **context,
        "status": "VERIFIED",
        "reason": None,
        "counters": values,
    }


async def read_p4_snapshot(profile: Any) -> dict[str, Any]:
    """Read the exact table entry and its direct counter from live BMv2."""
    binding = dict(profile.p4_counter_binding or {})
    p4info_registry = _p4info_registry()
    endpoint = await resolve_p4runtime_endpoint(profile)
    table_name = str(binding.get("table") or "")
    counter_name = str(binding.get("counter") or "")
    selector = canonical_service_selector(profile)
    context = {
        "service_id": profile.service_id,
        **endpoint,
        "table_name": table_name,
        "table_id": None,
        "action_name": "MyIngress.set_dscp",
        "counter_name": counter_name,
        "counter_id": None,
        "selector": selector,
    }
    if not binding:
        return _failure("UNSUPPORTED", "NO_P4_BINDING", "ServiceProfile has no P4 binding", context=context)
    if not endpoint.get("grpc_endpoint"):
        return _failure("DEVICE_UNAVAILABLE", "GRPC_ENDPOINT_MISSING", "No gRPC endpoint for logical device", context=context)
    try:
        table_id = p4info_registry.table_id(table_name)
    except KeyError as exc:
        return _failure("TABLE_NOT_FOUND", "TABLE_NOT_FOUND", f"{table_name} is absent from current P4Info", context=context, exception=exc)
    context["table_id"] = table_id
    counter_id = p4info_registry.direct_counter_id(counter_name)
    context["counter_id"] = counter_id
    if counter_id is None or p4info_registry.direct_counter_table(counter_name) != table_name:
        return _failure(
            "COUNTER_NOT_BOUND", "COUNTER_NOT_BOUND",
            f"{counter_name} is not bound to {table_name} in current P4Info",
            context=context,
        )

    read_request = {
        "device_id": endpoint["p4runtime_node_id"],
        "logical_device_id": endpoint["device_id"],
        "grpc_endpoint": endpoint["grpc_endpoint"],
        "entities": [
            {"type": "TableEntry", "table_id": table_id},
            {"type": "DirectCounterEntry", "counter_id": counter_id, "table_id": table_id},
        ],
        "selector": selector,
    }
    timeout = max(float(os.getenv("P4RUNTIME_READ_TIMEOUT_SECONDS", "5")), 0.5)
    try:
        table_entries, counters = await asyncio.to_thread(
            _read_entities_sync,
            str(endpoint["grpc_endpoint"]),
            int(endpoint["p4runtime_node_id"]),
            table_id,
            timeout,
        )
    except grpc.RpcError as exc:
        detail = exc.details() if hasattr(exc, "details") else str(exc)
        code = exc.code().name if hasattr(exc, "code") else "P4RUNTIME_ERROR"
        return _failure(
            "P4RUNTIME_ERROR", code, detail,
            context=context, exception=exc, read_request=read_request,
        )
    except Exception as exc:
        return _failure(
            "DEVICE_UNAVAILABLE" if isinstance(exc, (TimeoutError, grpc.FutureTimeoutError)) else "READ_FAILED",
            "GRPC_UNAVAILABLE" if isinstance(exc, (TimeoutError, grpc.FutureTimeoutError)) else "READ_EXCEPTION",
            str(exc), context=context, exception=exc, read_request=read_request,
        )

    matching_tables = [entry for entry in table_entries if _entry_matches(entry, table_name, selector)]
    response_summary = {
        "table_entry_count": len(table_entries),
        "direct_counter_entry_count": len(counters),
        "matching_table_entries": [_entry_summary(entry, table_name) for entry in matching_tables],
    }
    if not matching_tables:
        status = "SELECTOR_MISMATCH" if table_entries else "NO_MATCHING_ENTRY"
        reason = (
            "table contains entries but none match the canonical ServiceProfile selector"
            if table_entries else "target table contains no entries"
        )
        return _failure(status, status, reason, context=context, read_request=read_request, read_response=response_summary)

    matching_counters = [
        entry for entry in counters
        # P4Runtime identifies a DirectCounterEntry by its owning table key;
        # implementations are not required to repeat the table action in this
        # nested key.  The full table entry above already verified DSCP/action.
        if _entry_matches(entry.table_entry, table_name, selector, require_action=False)
    ]
    if not matching_counters:
        return _failure(
            "COUNTER_NOT_BOUND", "COUNTER_ENTRY_MISSING",
            "exact table entry exists but its direct counter entry was not returned",
            context=context, read_request=read_request, read_response=response_summary,
        )
    packet_count = sum(int(item.data.packet_count) for item in matching_counters)
    byte_count = sum(int(item.data.byte_count) for item in matching_counters)
    response_summary["matching_counter_entries"] = len(matching_counters)
    response_summary["packet_count"] = packet_count
    response_summary["byte_count"] = byte_count
    return {
        **context,
        "status": "VERIFIED",
        "reason_code": None,
        "reason_detail": None,
        "matching_entry_count": len(matching_tables),
        "packets": packet_count,
        "bytes": byte_count,
        "read_request": read_request,
        "read_response": response_summary,
        "exception": None,
        "read_at": _utc_now(),
    }

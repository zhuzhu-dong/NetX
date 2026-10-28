"""
mcp_servers/onos_server.py
ONOS MCP Server — 封装ONOS REST API为标准MCP工具

提供工具:
  - onos_get_topology      : 查询完整网络拓扑 (设备+链路+主机)
  - onos_get_device        : 查询指定设备信息
  - onos_get_flows         : 查询设备流表统计
  - onos_get_ports         : 查询设备端口信息

提供资源 (只读):
  - /topology/current      : 当前完整拓扑快照
  - /devices               : 设备列表
  - /links                 : 链路列表
  - /hosts                 : 主机列表
  - /flows/{device_id}     : 指定设备流表

权限级别: 只读 (ReadOnly)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import json
import os
from urllib.parse import quote, unquote
from typing import Any, Dict, List, Optional

import aiohttp
from models.network import parse_device_availability


def _flow_id_from_location(location: str | None) -> str:
    """Extract the controller-assigned flow ID from an ONOS Location header."""
    if not location:
        return ""
    path = location.split("?", 1)[0].rstrip("/")
    return path.rsplit("/", 1)[-1] if "/" in path else ""


def canonicalize_onos_device_id(device_id: str) -> str:
    """Return the domain device identity used by every ONOS REST operation.

    URL encoding is strictly a transport concern: callers retain e.g.
    ``device:bmv2:5`` in state and flow semantics, while paths are derived
    with :func:`onos_device_path_segment`.  Decoding first prevents accidental
    double encoding when a caller already carries an encoded REST segment.
    """
    value = unquote(str(device_id or "").strip())
    if not value or "/" in value or "\\" in value:
        raise ValueError(f"invalid ONOS device id: {device_id!r}")
    return value


def onos_device_path_segment(device_id: str) -> str:
    """Build one RFC 3986 encoded path segment for an ONOS device id."""
    return quote(canonicalize_onos_device_id(device_id), safe="")


def _controller_inventory_status(
    payload: dict[str, Any], min_devices: int = 1,
) -> tuple[str, int, int]:
    """Return controller readiness plus total/available device counts."""
    devices = payload.get("devices") or []
    total = len(devices)
    available = sum(1 for device in devices if device.get("available") is True)
    status = "ok" if total >= min_devices and available >= min_devices else "starting"
    return status, total, available

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Resource,
        TextContent,
        Tool,
    )
    import mcp.types as types
except ImportError:
    from dataclasses import dataclass

    @dataclass
    class TextContent:
        type: str
        text: str

    @dataclass
    class Tool:
        name: str
        description: str
        inputSchema: dict[str, Any]

    @dataclass
    class Resource:
        uri: str
        name: str
        description: str
        mimeType: str

    class _Types:
        AnyUrl = str

    types = _Types()

    class Server:
        def __init__(self, _name: str):
            self.name = _name

        def list_tools(self):
            return lambda fn: fn

        def call_tool(self):
            return lambda fn: fn

        def list_resources(self):
            return lambda fn: fn

        def read_resource(self):
            return lambda fn: fn

    async def stdio_server():
        raise RuntimeError("mcp package is not installed")

logger = logging.getLogger(__name__)

# ── 数据转换器 ────────────────────────────────────────────────────────────────

def _format_error(message: str, error_code: str = "INTERNAL_ERROR") -> dict:
    return {
        "success": False,
        "error_code": error_code,
        "message": message,
    }

class ONOSDataTransformer:
    """
    ONOS原始数据 → IDN业务模型格式的转换器

    将所有ONOS REST API返回的原始JSON字段映射为
    models.network 中各模型的字段名，实现南向接口的数据格式解耦。

    设备ID统一映射: ONOS的 of:000000000000000X → 系统的 device:bmv2:X

    映射是算法化的 — ONOS 的 ``of:`` 前缀后是 16 进制 DPID，直接用
    ``int(dpid, 16)`` 转换为 device_id。不再依赖硬编码映射表。
    """

    @staticmethod
    def _map_device_id(device_id: str) -> str:
        """将 ONOS 的 of:ID 映射为系统的 device:bmv2:N 格式。

        算法: 解析 of:<hex_dpid> → int(hex_dpid, 16) → device:bmv2:<N>.
        例如 of:0000000000000001 → device:bmv2:1.
        """
        if not device_id:
            return "unknown"
        if device_id.startswith("of:"):
            try:
                dpid = int(device_id[3:], 16)
                return f"device:bmv2:{dpid}"
            except ValueError:
                pass
        # Already in device:bmv2:N format or unrecognized — return as-is
        return device_id

    @staticmethod
    def device_to_dict(dev: Dict[str, Any]) -> Dict[str, Any]:
        """
        转换单个设备数据
        
        ONOS格式:
          {"id": "of:...", "available": true, "annotations": {...}, ...}
        
        业务格式:
          {"device_id": "device:bmv2:1", "status": "AVAILABLE", "grpc_port": 50051, ...}
        """
        # Absence is an observation failure, not controller proof that a
        # device is down.  Preserve the raw value for downstream audit.
        raw_available = dev.get("available")
        availability = parse_device_availability(raw_available)
        annotations = dev.get("annotations", {}) or {}

        # 提取grpc_port（可能在annotations中，注意ONOS偶有尾部空格）
        grpc_port = annotations.get("grpcPort") or annotations.get("grpcPort ", None)
        if grpc_port is not None:
            grpc_port = int(grpc_port)

        device_id = dev.get("id", "unknown")
        mapped_id = ONOSDataTransformer._map_device_id(device_id)

        return {
            "device_id": mapped_id,
            "type": dev.get("type", "SWITCH"),
            "status": availability.value,
            "present_in_topology": True,
            "controller_available": (
                True if availability.value == "AVAILABLE"
                else False if availability.value == "UNAVAILABLE"
                else None
            ),
            "availability_source": "ONOS_DEVICE_AVAILABLE",
            "availability_reason_code": (
                None if availability.value != "UNKNOWN"
                else "DEVICE_AVAILABILITY_NOT_OBSERVED"
            ),
            "raw_availability": raw_available,
            "grpc_port": grpc_port,
            "p4_program": annotations.get("p4Program", None),
            "ports": dev.get("ports", []),
            "annotations": annotations,
        }

    @staticmethod
    def link_to_dict(link: Dict[str, Any]) -> Dict[str, Any]:
        """
        转换单个链路数据
        
        ONOS格式:
          {"src": {"device": "...", "port": "..."}, "dst": {...}, "state": "ACTIVE"}
        
        业务格式:
          {"link_id": "link-...", "src_device": "...", "src_port": "...", ...}
        """
        src = link.get("src", {}) or {}
        dst = link.get("dst", {}) or {}

        src_device = src.get("device", "unknown")
        src_port = str(src.get("port", ""))
        dst_device = dst.get("device", "unknown")
        dst_port = str(dst.get("port", ""))

        # 映射设备ID
        src_device = ONOSDataTransformer._map_device_id(src_device)
        dst_device = ONOSDataTransformer._map_device_id(dst_device)

        return {
            "link_id": f"link-{src_device}-{src_port}-to-{dst_device}-{dst_port}",
            "src_device": src_device,
            "src_port": src_port,
            "dst_device": dst_device,
            "dst_port": dst_port,
            "state": link.get("state", "ACTIVE"),
            # ONOS拓扑API不直接提供带宽和时延，后续可从端口统计或外部系统获取
            "bandwidth_mbps": None,
            "delay_ms": None,
        }

    @staticmethod
    def host_to_dict(host: Dict[str, Any]) -> Dict[str, Any]:
        """
        转换单个主机数据
        
        ONOS格式:
          {"id": "...", "mac": "...", "ipAddresses": [...], "locations": [...]}
        
        业务格式:
          {"host_id": "...", "mac": "...", "ip_addresses": [...], "location_device": "...", ...}
        """
        locations = host.get("locations", []) or []
        first_location = locations[0] if locations else {}

        location_device = first_location.get("elementId", None)
        location_device = ONOSDataTransformer._map_device_id(location_device) if location_device else None

        return {
            "host_id": host.get("id", f"host-{host.get('mac', 'unknown')}"),
            "mac": host.get("mac", ""),
            "ip_addresses": host.get("ipAddresses", []),
            "location_device": location_device,
            "location_port": first_location.get("port", None),
        }
    
    @staticmethod
    def flow_to_dict(flow: Dict[str, Any]) -> Dict[str, Any]:
        """
        转换单个流表项数据

        ONOS格式:
          {"id": "...", "deviceId": "...", "tableId": 0, "priority": 100, ...}

        业务格式:
          {"flow_id": "...", "device_id": "...", "table_name": "table-0", "priority": 100, ...}
        """
        table_id = flow.get("tableId", 0)
        device_id = flow.get("deviceId", "unknown")
        mapped_id = ONOSDataTransformer._map_device_id(device_id)

        return {
            "flow_id": flow.get("id", "unknown"),
            "device_id": mapped_id,
            "table_name": f"table-{table_id}",  # ONOS用数字ID，暂时映射为 table-N
            "priority": flow.get("priority", 0),
            "timeout": flow.get("timeout", 0),
            "is_permanent": flow.get("isPermanent", False),
            "packets": flow.get("packets", 0),
            "bytes": flow.get("bytes", 0),
            # 保留原始数据供冲突检测使用
            "raw": {
                "selector": flow.get("selector", {}),
                "treatment": flow.get("treatment", {}),
            },
        }

    @staticmethod
    def port_to_dict(port: Dict[str, Any]) -> Dict[str, Any]:
        """
        转换单个端口数据

        ONOS格式:
          {"port": "1", "isEnabled": true, "type": "copper", "portSpeed": 10000, ...}

        业务格式:
          {"port_id": "1", "enabled": true, "type": "copper", "speed_mbps": 10000, ...}
        """
        return {
            "port_id": str(port.get("port", "unknown")),
            "enabled": port.get("isEnabled", False),
            "type": port.get("type", "unknown"),
            "speed_mbps": port.get("portSpeed", None),
        }

transformer = ONOSDataTransformer()

# ── ONOS REST API 客户端 ──────────────────────────────────────────────────────

class ONOSClient:
    """ONOS REST API client with connection pooling and circuit breaker.

    Maintains a single persistent ``aiohttp.ClientSession`` with a
    ``TCPConnector`` for connection reuse.  A lightweight circuit breaker
    fast-fails when ONOS has been unreachable for several consecutive
    attempts.
    """

    def __init__(self):
        host = os.getenv("ONOS_HOST", "localhost")
        port = os.getenv("ONOS_PORT", "8181")
        user = os.getenv("ONOS_USERNAME", "onos")
        passwd = os.getenv("ONOS_PASSWORD", "rocks")
        self.base_url = f"http://{host}:{port}/onos/v1"
        self._auth = aiohttp.BasicAuth(user, passwd)
        self._headers = {"Accept": "application/json"}

        # ── Persistent session (lazily created) ──
        self._session: aiohttp.ClientSession | None = None
        self._connector: aiohttp.TCPConnector | None = None
        self._session_lock: asyncio.Lock | None = None

        # ── Circuit breaker state ──
        self._failure_count = 0
        self._circuit_open_until = 0.0
        self._cb_threshold = int(os.getenv("ONOS_CB_THRESHOLD", "5"))
        self._cb_cooldown = int(os.getenv("ONOS_CB_COOLDOWN", "30"))

    # ── Session management ────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared session, creating it on first access."""
        if self._session is not None and not self._session.closed:
            return self._session
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                return self._session
            pool_size = int(os.getenv("ONOS_POOL_SIZE", "8"))
            default_timeout = int(os.getenv("ONOS_TIMEOUT", "15"))
            self._connector = aiohttp.TCPConnector(
                limit=pool_size,
                keepalive_timeout=30,
                force_close=False,
            )
            self._session = aiohttp.ClientSession(
                auth=self._auth,
                connector=self._connector,
                timeout=aiohttp.ClientTimeout(total=default_timeout),
            )
            logger.info(
                "[ONOSClient] Created persistent session pool_size=%d timeout=%ds",
                pool_size, default_timeout,
            )
        return self._session

    async def close(self) -> None:
        """Close the persistent session and connector."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            logger.info("[ONOSClient] Persistent session closed.")
        if self._connector is not None:
            await self._connector.close()
            self._connector = None

    # ── Circuit breaker ───────────────────────────────────────────────────

    def _check_circuit(self) -> None:
        """Raise if the circuit breaker is currently open."""
        import time as _time
        if _time.monotonic() < self._circuit_open_until:
            remaining = self._circuit_open_until - _time.monotonic()
            raise ConnectionError(
                f"ONOS circuit breaker open (cooldown {remaining:.0f}s remaining)"
            )

    def _record_result(self, success: bool) -> None:
        """Update circuit breaker state after an HTTP call."""
        import time as _time
        if success:
            if self._failure_count > 0:
                logger.info(
                    "[ONOSClient] Circuit breaker reset after %d failures",
                    self._failure_count,
                )
            self._failure_count = 0
            self._circuit_open_until = 0.0
        else:
            self._failure_count += 1
            if self._failure_count >= self._cb_threshold:
                self._circuit_open_until = _time.monotonic() + self._cb_cooldown
                logger.error(
                    "[ONOSClient] Circuit breaker OPEN after %d consecutive failures "
                    "(cooldown=%ds)",
                    self._failure_count, self._cb_cooldown,
                )

    async def get(self, path: str) -> Dict[str, Any]:
        """Send GET request to ONOS REST API."""
        self._check_circuit()
        session = await self._get_session()
        try:
            async with session.get(f"{self.base_url}{path}", headers=self._headers) as resp:
                resp.raise_for_status()
                result = await resp.json()
        except Exception:
            self._record_result(False)
            raise
        self._record_result(True)
        return result

    async def get_topology(self) -> Dict[str, Any]:
        return await self.get("/topology")

    async def get_devices(self) -> Dict[str, Any]:
        return await self.get("/devices")

    async def get_links(self) -> Dict[str, Any]:
        return await self.get("/links")

    async def get_hosts(self) -> Dict[str, Any]:
        return await self.get("/hosts")

    async def get_flows(self, device_id: Optional[str] = None) -> Dict[str, Any]:
        path = f"/flows/{onos_device_path_segment(device_id)}" if device_id else "/flows"
        return await self.get(path)

    async def get_ports(self, device_id: str) -> Dict[str, Any]:
        return await self.get(f"/devices/{device_id}/ports")

    async def get_port_statistics(self, device_id: str) -> Dict[str, Any]:
        """Query ONOS port statistics for a device.

        Returns per-port counters: packets/bytes received/sent, drops.
        Endpoint: GET /onos/v1/statistics/ports/{deviceId}
        """
        return await self.get(f"/statistics/ports/{device_id}")

    async def get_all_devices(self) -> List[Dict[str, Any]]:
        """Return all available devices (convenience wrapper)."""
        data = await self.get_devices()
        return data.get("devices", [])

    async def post(self, path: str, body: Any) -> Dict[str, Any]:
        """Send POST request to ONOS REST API."""
        self._check_circuit()
        headers = {**self._headers, "Content-Type": "application/json"}
        session = await self._get_session()
        try:
            async with session.post(f"{self.base_url}{path}", json=body, headers=headers) as resp:
                resp.raise_for_status()
                if resp.status == 204:
                    result = {"status": "ok"}
                else:
                    text = await resp.text()
                    result = await resp.json() if text else {"status": "ok"}
        except Exception:
            self._record_result(False)
            raise
        self._record_result(True)
        return result

    async def delete(self, path: str) -> Dict[str, Any]:
        """Send DELETE request to ONOS REST API."""
        self._check_circuit()
        session = await self._get_session()
        try:
            async with session.delete(f"{self.base_url}{path}", headers=self._headers) as resp:
                resp.raise_for_status()
                if resp.status == 204:
                    result = {"status": "ok"}
                else:
                    text = await resp.text()
                    result = await resp.json() if text else {"status": "ok"}
        except Exception:
            self._record_result(False)
            raise
        self._record_result(True)
        return result

    async def post_flows(self, device_id: str, flow_rules: List[dict], correlation_id: str = "") -> Dict[str, Any]:
        """POST flow rules to ONOS REST API via POST /onos/v1/flows/{deviceId}.

        Posts each flow rule individually for reliability.
        Returns summary: {flows_posted, total, flow_ids, errors}
        """
        self._check_circuit()
        posted = 0
        total = len(flow_rules)
        errors: List[str] = []
        flow_ids: List[str] = []
        delivery_receipts: List[Dict[str, Any]] = []

        session = await self._get_session()
        any_success = False
        any_failure = False
        for i, flow in enumerate(flow_rules):
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if correlation_id:
                headers["X-Correlation-Id"] = correlation_id
            try:
                request_url = f"{self.base_url}/flows/{onos_device_path_segment(device_id)}"
                canonical_device_id = canonicalize_onos_device_id(device_id)
                async with session.post(
                    request_url, json=flow, headers=headers,
                ) as resp:
                    response_text = await resp.text()
                    delivery_receipts.append({
                        "method": "POST", "request_url": request_url,
                        "canonical_device_id": canonical_device_id,
                        "request_body_hash": hashlib.sha256(json.dumps(flow, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                        "http_status": resp.status, "location": resp.headers.get("Location"),
                        "response_body": response_text, "response_body_hash": hashlib.sha256(response_text.encode()).hexdigest(),
                    })
                    if resp.status in (200, 201):
                        # ONOS normally returns 201 with an empty body and puts
                        # the controller-assigned flow ID in Location.  Keeping
                        # that real ID is essential for Saga compensation;
                        # deriving an ID from the request payload cannot address
                        # the installed ONOS flow reliably.
                        flow_id = ""
                        try:
                            flow_obj = json.loads(response_text) if response_text.strip() else {}
                            flow_id = str(flow_obj.get("id") or "")
                        except (TypeError, ValueError, AttributeError):
                            flow_id = ""
                        flow_id = flow_id or _flow_id_from_location(resp.headers.get("Location"))

                        if flow_id:
                            posted += 1
                            any_success = True
                            flow_ids.append(flow_id)
                            logger.debug(
                                "[ONOSClient] Flow %d/%d posted to %s as %s",
                                i + 1, total, device_id, flow_id,
                            )
                        else:
                            errors.append(
                                f"flow {i}: ONOS accepted the rule but returned no flow ID"
                            )
                            flow_ids.append("")
                            any_failure = True
                            logger.error(
                                "[ONOSClient] Flow %d/%d accepted by %s without an ID",
                                i + 1, total, device_id,
                            )
                    else:
                        text = response_text
                        err = f"flow {i}: HTTP {resp.status} — {text[:200]}"
                        errors.append(err)
                        flow_ids.append("")
                        any_failure = True
                        logger.warning("[ONOSClient] Failed to post flow %d/%d: %s", i + 1, total, err)
            except aiohttp.ClientResponseError as e:
                err = f"flow {i}: {e.status} — {e.message}"
                errors.append(err)
                flow_ids.append("")
                any_failure = True
                logger.error("[ONOSClient] HTTP error posting flow %d/%d: %s", i + 1, total, err)
            except Exception as e:
                err = f"flow {i}: {e}"
                errors.append(err)
                flow_ids.append("")
                any_failure = True
                logger.error("[ONOSClient] Error posting flow %d/%d: %s", i + 1, total, e)

        # Circuit breaker: a batch is considered failed if ALL flows failed
        self._record_result(not any_failure or any_success)

        logger.info(
            "[ONOSClient] post_flows complete: %d/%d posted to %s%s",
            posted, total, device_id,
            f" ({len(errors)} errors)" if errors else "",
        )
        return {
            "device_id": canonicalize_onos_device_id(device_id),
            "request_url": f"{self.base_url}/flows/{onos_device_path_segment(device_id)}",
            "flows_posted": posted,
            "total": total,
            "flow_ids": flow_ids,
            "delivery_receipts": delivery_receipts,
            "errors": errors if errors else None,
        }

    async def post_group(self, device_id: str, group_payload: Dict[str, Any]) -> bool:
        """POST an action profile group to ONOS REST API via POST /onos/v1/groups/{deviceId}.

        ONOS auto-manages group lifecycle for P4Runtime action profile groups.
        Returns True if the group was created or already exists.
        """
        self._check_circuit()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.base_url}/groups/{device_id}",
                json=group_payload, headers=headers,
            ) as resp:
                if resp.status in (200, 201):
                    logger.debug(
                        "[ONOSClient] Group posted: groupId=%s deviceId=%s",
                        group_payload.get("groupId"), device_id,
                    )
                    self._record_result(True)
                    return True
                text = await resp.text()
                logger.warning(
                    "[ONOSClient] Failed to post group: HTTP %s — %s",
                    resp.status, text[:200],
                )
        except Exception as e:
            logger.error("[ONOSClient] Error posting group: %s", e)
        self._record_result(False)
        return False

    async def delete_flows(self, device_id: str, flow_ids: List[str], correlation_id: str = "") -> Dict[str, Any]:
        """Delete flow rules from ONOS by flow ID.

        Deletes each flow individually via DELETE /onos/v1/flows/{deviceId}/{flowId}.
        Returns summary: {flows_deleted, total, errors}
        """
        self._check_circuit()
        deleted = 0
        total = len(flow_ids)
        errors: List[str] = []

        session = await self._get_session()
        any_success = False
        any_failure = False
        for flow_id in flow_ids:
            headers = {"Accept": "application/json"}
            if correlation_id:
                headers["X-Correlation-Id"] = correlation_id
            try:
                async with session.delete(
                    f"{self.base_url}/flows/{device_id}/{flow_id}",
                    headers=headers,
                ) as resp:
                    # DELETE is idempotent for Saga compensation. A 404 means
                    # the rule was already removed by an earlier attempt and
                    # should not poison the circuit breaker or block metadata
                    # cleanup.
                    if resp.status in (200, 204, 404):
                        deleted += 1
                        any_success = True
                    else:
                        errors.append(f"{flow_id}: HTTP {resp.status}")
                        any_failure = True
            except Exception as e:
                errors.append(f"{flow_id}: {e}")
                any_failure = True

        self._record_result(not any_failure or any_success)

        logger.info(
            "[ONOSClient] delete_flows complete: %d/%d deleted from %s",
            deleted, total, device_id,
        )
        return {
            "flows_deleted": deleted,
            "total": total,
            "errors": errors if errors else None,
        }

    async def check_health(self) -> Dict[str, Any]:
        """Check ONOS controller health.

        Returns {status: "ok"|"unavailable", latency_ms: float}
        """
        import time
        start = time.monotonic()
        try:
            # Bypass circuit breaker for health checks — always attempt
            session = await self._get_session()
            async with session.get(f"{self.base_url}/devices", headers=self._headers) as resp:
                resp.raise_for_status()
                payload = await resp.json()
            latency = (time.monotonic() - start) * 1000
            min_devices = max(1, int(os.getenv("IDN_MIN_ONOS_DEVICES", "1")))
            status, device_count, available_devices = _controller_inventory_status(
                payload, min_devices=min_devices,
            )
            # A successful HTTP response proves the transport is available.
            # An empty/degraded device inventory is a controller-state signal
            # for RecoveryManager, not a connection failure. Treating it as a
            # breaker failure creates a deadlock: recovery cannot POST netcfg
            # because each zero-device health poll re-opens the circuit.
            self._record_result(True)
            return {
                "status": status,
                "latency_ms": round(latency, 2),
                "device_count": device_count,
                "available_devices": available_devices,
                "minimum_devices": min_devices,
            }
        except Exception as e:
            latency_ms = (time.monotonic() - start) * 1000
            self._record_result(False)
            logger.warning("[ONOSClient] Health check failed: %s", e)
            return {"status": "unavailable", "latency_ms": round(latency_ms, 2), "error": str(e)}


# ── MCP Server 定义 ──────────────────────────────────────────────────────────

_DEFAULT_ONOS_CLIENT_CLASS = ONOSClient
onos_client = ONOSClient()
server = Server("onos-mcp-server")


def get_onos_client() -> ONOSClient:
    """Return the process-wide client closed by the application lifespan."""

    # Unit tests and integrations can replace ONOSClient with a scoped fake.
    # Production code keeps using the one lifespan-managed singleton.
    if ONOSClient is not _DEFAULT_ONOS_CLIENT_CLASS:
        return ONOSClient()
    return onos_client


@server.list_tools()
async def list_tools() -> List[Tool]:
    """声明所有可用工具及其JSON Schema"""
    return [
        Tool(
            name="onos_get_topology",
            description="查询ONOS控制器管理的完整网络拓扑，包括设备、链路和主机信息",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="onos_get_device",
            description="查询指定设备的详细信息，包括端口、状态和P4程序",
            inputSchema={
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "设备唯一标识 (e.g. device:bmv2:1)",
                    }
                },
                "required": ["device_id"],
            },
        ),
        Tool(
            name="onos_get_flows",
            description="查询设备当前流表项及统计信息 (包数/字节数)",
            inputSchema={
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "设备ID (不填则查询所有设备)",
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="onos_get_ports",
            description="查询设备端口列表及状态",
            inputSchema={
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "设备唯一标识",
                    }
                },
                "required": ["device_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """处理工具调用请求"""

    try:
        if name == "onos_get_topology":
            devices = await onos_client.get_devices()
            links = await onos_client.get_links()
            hosts = await onos_client.get_hosts()
            flows = await onos_client.get_flows()
            result = {
                "devices": [transformer.device_to_dict(d) for d in devices.get("devices", [])],
                "links": [transformer.link_to_dict(l) for l in links.get("links", [])],
                "hosts": [transformer.host_to_dict(h) for h in hosts.get("hosts", [])],
                "flows": [transformer.flow_to_dict(f) for f in flows.get("flows", [])],
            }
        elif name == "onos_get_device":
            device_id = arguments["device_id"]
            raw = await onos_client.get(f"/devices/{device_id}")
            result = transformer.device_to_dict(raw)
        elif name == "onos_get_flows":
            device_id = arguments.get("device_id")
            raw = await onos_client.get_flows(device_id)
            result = {
                "flows": [transformer.flow_to_dict(f) for f in raw.get("flows", [])]
            }
        elif name == "onos_get_ports":
            device_id = arguments["device_id"]
            raw = await onos_client.get_ports(device_id)
            result = {
                "ports": [transformer.port_to_dict(p) for p in raw.get("ports", [])]
            }
        else:
            result = _format_error(f"未知工具: {name}")

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    except aiohttp.ClientResponseError as e:
        error = _format_error(f"ONOS API错误: {e.status} - {e.message}", "ONOS_UNAVAILABLE")
        return [TextContent(type="text", text=json.dumps(error))]
    except Exception as e:
        error = _format_error(f"工具调用失败: {str(e)}")
        return [TextContent(type="text", text=json.dumps(error))]


@server.list_resources()
async def list_resources() -> List[Resource]:
    """声明可订阅的只读资源"""
    return [
        Resource(
            uri="onos://topology/current",
            name="当前网络拓扑",
            description="ONOS控制器维护的实时网络拓扑数据",
            mimeType="application/json",
        ),
        Resource(
            uri="onos://devices",
            name="设备列表",
            description="所有受管网络设备信息",
            mimeType="application/json",
        ),
        Resource(
            uri="onos://links",
            name="链路列表",
            description="所有网络链路信息",
            mimeType="application/json",
        ),
        Resource(
            uri="onos://hosts",
            name="主机列表",
            description="已发现的终端主机信息",
            mimeType="application/json",
        ),
        Resource(
            uri="onos://flows",
            name="流表列表",
            description="所有设备当前流表项",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def read_resource(uri: types.AnyUrl) -> str:
    """读取资源数据（已转换为业务模型格式）"""
    uri_str = str(uri)
    try:
        if "topology/current" in uri_str:
            devices = await onos_client.get_devices()
            links = await onos_client.get_links()
            flows = await onos_client.get_flows()
            data = {
                "devices": [transformer.device_to_dict(d) for d in devices.get("devices", [])],
                "links": [transformer.link_to_dict(l) for l in links.get("links", [])],
                "flows": [transformer.flow_to_dict(f) for f in flows.get("flows", [])],
            }
        elif "devices" in uri_str:
            devices = await onos_client.get_devices()
            data = {
                "devices": [transformer.device_to_dict(d) for d in devices.get("devices", [])]
            }
        elif "links" in uri_str:
            links = await onos_client.get_links()
            data = {
                "links": [transformer.link_to_dict(l) for l in links.get("links", [])]
            }
        elif "hosts" in uri_str:
            hosts = await onos_client.get_hosts()
            data = {
                "hosts": [transformer.host_to_dict(h) for h in hosts.get("hosts", [])]
            }
        elif "flows" in uri_str:
            flows_raw = await onos_client.get_flows()
            data = {
                "flows": [transformer.flow_to_dict(f) for f in flows_raw.get("flows", [])]
            }
        else:
            data = _format_error(f"未知资源: {uri_str}")
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps(_format_error(str(e)))


async def main():
    """启动ONOS MCP Server"""
    logging.basicConfig(level=logging.INFO)
    logger.info("ONOS MCP Server 启动中...")
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await onos_client.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

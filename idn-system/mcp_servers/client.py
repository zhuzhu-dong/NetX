"""
mcp_servers/client.py
MCP 客户端 — 通过 stdio 子进程与 MCP Server 通信（正宗 MCP 协议）

每个 MCPClient 实例管理一个 MCP Server 子进程的生命周期：
  1. 首次 call_tool / read_resource 时自动启动子进程并完成 MCP 握手
  2. 后续调用复用同一 session
  3. close() 时优雅关闭子进程

用法:
    client = MCPClient("onos")
    topology = await client.read_resource("/topology/current")
    flows = await client.call_tool("onos_get_flows", {"device_id": "device:bmv2:1"})
    await client.close()
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from typing import Any, Dict, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

logger = logging.getLogger(__name__)

# ── 子进程启动配置 ──────────────────────────────────────────────────────────────

# server_name → module_name (python -m mcp_servers.<module>)
_SERVER_MODULES: Dict[str, str] = {
    "onos": "onos_server",
    "telemetry": "telemetry_server",
    "p4runtime": "p4runtime_server",
}

# idn-system 包的根目录，子进程需要将其加入 PYTHONPATH
# client.py 位于 <root>/mcp_servers/client.py
_PACKAGE_ROOT = os.getenv(
    "IDN_PACKAGE_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)


def _build_server_params(server_name: str) -> StdioServerParameters:
    """为指定 MCP Server 构建子进程启动参数。"""
    module = _SERVER_MODULES.get(server_name)
    if module is None:
        raise ValueError(
            f"未知的MCP Server: {server_name}，可用: {list(_SERVER_MODULES.keys())}"
        )

    env = os.environ.copy()
    pythonpath = _PACKAGE_ROOT
    if "PYTHONPATH" in env and env["PYTHONPATH"]:
        pythonpath = os.pathsep.join([_PACKAGE_ROOT, env["PYTHONPATH"]])
    env["PYTHONPATH"] = pythonpath

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", f"mcp_servers.{module}"],
        env=env,
        cwd=_PACKAGE_ROOT,
    )


# ── 结果提取工具 ────────────────────────────────────────────────────────────────

def _extract_call_tool_text(content: list) -> str:
    """从 CallToolResult.content 中提取第一个文本片段。"""
    for item in content:
        if hasattr(item, "text") and item.text:
            return item.text
        # EmbeddedResource: item.type == "resource"
        if hasattr(item, "resource") and hasattr(item.resource, "text") and item.resource.text:
            return item.resource.text
    return ""


def _extract_resource_text(contents: list) -> str:
    """从 ReadResourceResult.contents 中提取第一个文本片段。"""
    for item in contents:
        if hasattr(item, "text") and item.text:
            return item.text
        # BlobResourceContents
        if hasattr(item, "blob") and item.blob:
            return item.blob
    return ""


# ── 客户端 ──────────────────────────────────────────────────────────────────────

# 按 server_name 分区的启动锁，防止同一 MCP Server 被并发启动多个子进程
_mcp_startup_locks: dict[str, asyncio.Lock] = {}


class MCPClient:
    """MCP stdio 客户端 — 与独立 MCP Server 子进程通过 JSON-RPC 通信。

    连接是惰性的：第一次 call_tool / read_resource 时才启动子进程。
    """

    def __init__(self, server_name: str):
        self.server_name = server_name
        self._server_params = _build_server_params(server_name)
        self._exit_stack: Optional[AsyncExitStack] = None
        self._session: Optional[ClientSession] = None

    async def _ensure_connected(self) -> ClientSession:
        """惰性启动子进程并完成 MCP 握手，返回可用的 session。

        使用按 server_name 分区的 asyncio.Lock 防止并发启动同一子进程。
        """
        if self._session is not None:
            return self._session

        lock = _mcp_startup_locks.setdefault(self.server_name, asyncio.Lock())
        async with lock:
            # 双重检查：获取锁后可能已被其他协程初始化
            if self._session is not None:
                return self._session

            logger.info(
                "[MCPClient] 启动子进程: %s %s",
                self._server_params.command,
                " ".join(self._server_params.args),
            )

            self._exit_stack = AsyncExitStack()
            try:
                read, write = await self._exit_stack.enter_async_context(
                    stdio_client(self._server_params)
                )
                session = await self._exit_stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                self._session = session
                logger.info("[MCPClient] %s 连接成功", self.server_name)
                return session
            except Exception:
                # 启动失败或握手失败，清理已分配的资源
                if self._exit_stack:
                    await self._exit_stack.aclose()
                self._exit_stack = None
                self._session = None
                raise ConnectionError(
                    f"无法连接到MCP Server: {self.server_name}"
                ) from None

    async def call_tool(
        self, tool_name: str, params: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        """调用 MCP Server 的工具。

        Args:
            tool_name: 工具名称 (e.g. "onos_get_device")
            params: 工具参数

        Returns:
            工具执行结果 (JSON 解析后的字典)
        """
        session = await self._ensure_connected()
        logger.debug(
            "[MCPClient] call_tool: %s/%s params=%s",
            self.server_name, tool_name, params,
        )

        result = await session.call_tool(tool_name, params or {})
        text = _extract_call_tool_text(result.content)
        if result.isError:
            logger.warning(
                "[MCPClient] %s/%s 返回错误: %s",
                self.server_name, tool_name, text[:200],
            )
        return json.loads(text) if text else {}

    async def read_resource(self, resource_path: str) -> Dict[str, Any]:
        """读取 MCP Server 的资源。

        Args:
            resource_path: 资源路径 (e.g. "/topology/current", "/devices")

        Returns:
            资源数据 (JSON 解析后的字典)
        """
        session = await self._ensure_connected()
        logger.debug(
            "[MCPClient] read_resource: %s/%s",
            self.server_name, resource_path,
        )

        uri = f"{self.server_name}://{resource_path.lstrip('/')}"
        result = await session.read_resource(uri)
        text = _extract_resource_text(result.contents)
        return json.loads(text) if text else {}

    async def close(self):
        """关闭与 MCP Server 子进程的连接（发送 SIGTERM 并清理）。"""
        if self._exit_stack is None:
            return
        logger.info("[MCPClient] 断开 %s", self.server_name)
        await self._exit_stack.aclose()
        self._exit_stack = None
        self._session = None


# ── 便捷函数 ──

def get_onos_client() -> MCPClient:
    """获取 ONOS MCP Client 实例"""
    return MCPClient("onos")


def get_p4runtime_client() -> MCPClient:
    """获取 P4Runtime/P4Info 工具 Client 实例"""
    return MCPClient("p4runtime")


def get_telemetry_client() -> MCPClient:
    """获取 Telemetry MCP Client 实例"""
    return MCPClient("telemetry")

"""
orchestrator/nodes/sense_node.py
=================================

感知节点 (Sense Node) —— STAV 闭环的第一阶段。

# 系统角色
通过 MCP 协议调用 NetworkSensingAgent 采集当前网络状态快照，
包括：拓扑结构（设备/链路）、主机列表、当前流表项，
作为后续所有阶段的基础输入数据。

# 输入/输出
- 输入：无前置状态依赖（工作流起点）
- 输出：state.network_snapshot（NetworkSnapshot 对象）

# 异常处理
- ONOS 不可达时：设置 network_snapshot=None（sentinel 值），
  下游节点通过检查 None 来跳过无意义的 LLM 调用
"""
from __future__ import annotations

import logging
import time
import os
from datetime import datetime
from typing import Any

from models.agent_state import (
    ExecutionLogEntry,
    ExecutionStage,
    GlobalAgentState,
    StageStatus,
)
from models.intent import IntentStatus

from .base_node import BaseNode

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 基础节点类
# ──────────────────────────────────────────────


class SenseNode(BaseNode):
    """
    感知节点 (Sense)
    职责: 通过MCP协议调用 onos-mcp-server 采集当前网络拓扑快照
    """
    agent_id = "sense_agent"
    stage = ExecutionStage.SENSE

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        state.current_status = IntentStatus.SENSING

        # ── Re-use snapshot from prior pipeline run (e.g., clarification re-run) ──
        if state.network_snapshot is not None:
            snapshot = state.network_snapshot
            logger.info(
                "复用已有拓扑: devices=%d links=%d hosts=%d",
                len(snapshot.devices), len(snapshot.links), len(snapshot.hosts),
            )
            return self._end_log(
                state, start, StageStatus.SUCCESS,
                f"复用已有拓扑: {len(snapshot.devices)}台设备, "
                f"{len(snapshot.links)}条链路, {len(snapshot.hosts)}台主机",
            )

        agent = None
        try:
            from agents.network_sensing import NetworkSensingAgent

            agent = NetworkSensingAgent()
            state.network_snapshot = await agent.collect_snapshot()

            # ── Pre-warm P4Info while downstream stages run ──
            try:
                import asyncio
                from mcp_servers.p4info_registry import p4info_registry
                asyncio.ensure_future(
                    asyncio.get_running_loop().run_in_executor(
                        None, lambda: p4info_registry.to_dict()
                    )
                )
            except Exception:
                pass  # fire-and-forget, failure is harmless

            snapshot = state.network_snapshot
            return self._end_log(
                state, start, StageStatus.SUCCESS,
                f"成功采集拓扑: {len(snapshot.devices)}台设备, "
                f"{len(snapshot.links)}条链路, {len(snapshot.hosts)}台主机"
            )

        except Exception as e:
            # ── ONOS不可达 ──
            logger.warning(f"ONOS连接失败 ({os.getenv('ONOS_HOST', 'localhost')}:{os.getenv('ONOS_PORT', '8181')})")
            state.error_message = f"Sense阶段失败 (ONOS连接异常): {str(e)}"
            # Use None sentinel instead of empty NetworkSnapshot so downstream
            # nodes can detect the failure and skip meaningless LLM calls.
            state.network_snapshot = None
            return self._end_log(state, start, StageStatus.FAILED, "ONOS连接失败", error=str(e))
        finally:
            if agent is not None:
                try:
                    await agent.close()
                except Exception:
                    pass  # 清理阶段所有异常都吞掉，不掩盖原始错误


# ──────────────────────────────────────────────
# Think阶段 — 意图解析节点
# ──────────────────────────────────────────────


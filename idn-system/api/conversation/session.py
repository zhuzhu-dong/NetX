"""
api/conversation/session.py
ChatSession — unified entry point for all conversation interactions.

Replaces the old split of:
- POST /chat  (130-line inline in routes.py)
- POST /chat/non-stream  (80% duplicated)
- POST /intents  (separate intent creation)

A single ``ChatSession.send_message()`` returns an async iterator of
``SSEEvent`` items.  The route handler becomes a thin adapter:

    @router.post("/conversations/{id}/message")
    async def send_message(id: str, req: SendRequest):
        session = ChatSession(id, get_store())
        return StreamingResponse(
            (event.to_sse() for event in session.send_message(req.text, req.user_id)),
            media_type="text/event-stream",
        )
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from typing import AsyncIterator

from langchain_core.messages import SystemMessage as LangSystemMessage
from langchain_core.messages import HumanMessage as LangHumanMessage
from langchain_core.messages import AIMessage as LangAIMessage

from api.conversation.models import (
    Message, MessageType, SSEEvent, ApprovalContext,
)
from api.conversation.classifier import (
    classify_chat_response, ClassificationResult, CHAT_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)


_FLOW_QUERY_TERMS = ("流表", "flow", "flows")
_READ_QUERY_TERMS = ("查", "看", "获取", "读取", "统计", "show", "list", "get", "query")
_FLOW_MUTATION_TERMS = (
    "下发", "安装", "部署", "写入", "删除", "清空", "修改", "生成", "创建",
    "apply", "post", "delete", "install", "deploy", "write", "create",
)


def _parse_flow_stats_query(text: str) -> dict | None:
    """Return a read-only flow-query descriptor, or None for normal chat.

    Flow-table inspection is observational.  It should not enter the STAV
    policy pipeline, otherwise the user sees orchestration progress instead of
    the live ONOS table they asked to inspect.
    """
    raw = text or ""
    normalized = raw.lower()
    if not any(term in normalized for term in _FLOW_QUERY_TERMS):
        return None
    if not any(term in normalized for term in _READ_QUERY_TERMS):
        return None
    if any(term in normalized for term in _FLOW_MUTATION_TERMS):
        return None

    device_id = None
    match = re.search(r"(?:device\s*:\s*)?bmv2\s*[:：#-]?\s*(\d+)", normalized)
    if match:
        device_id = f"device:bmv2:{int(match.group(1))}"
    return {"kind": "flow_stats", "device_id": device_id}


def _table_name_for_id(table_id: object) -> str:
    try:
        tid = int(table_id)
    except (TypeError, ValueError):
        return str(table_id or "-")
    try:
        from mcp_servers.p4info_registry import P4InfoRegistry

        name = P4InfoRegistry.table_name_for_id(tid)
        return name or str(tid)
    except Exception:
        return str(tid)


def _format_hex_value(value: object, prefix_len: object = None) -> str:
    text = str(value or "")
    if re.fullmatch(r"[0-9a-fA-F]{8}", text):
        ip = ".".join(str(int(text[i:i + 2], 16)) for i in range(0, 8, 2))
        return f"{ip}/{prefix_len}" if prefix_len is not None else ip
    if re.fullmatch(r"[0-9a-fA-F]{12}", text):
        return ":".join(text[i:i + 2] for i in range(0, 12, 2)).lower()
    return text or "-"


def _summarize_selector(selector: dict) -> str:
    parts: list[str] = []
    for criterion in selector.get("criteria") or []:
        for item in criterion.get("matches") or []:
            field = item.get("field") or "field"
            match_type = item.get("match") or "exact"
            value = _format_hex_value(item.get("value"), item.get("prefixLength"))
            if match_type == "ternary":
                mask = item.get("mask")
                value = f"{value} mask={mask}" if mask else value
            parts.append(f"{field} {match_type} {value}")
    return "; ".join(parts) if parts else "-"


def _summarize_treatment(treatment: dict) -> str:
    parts: list[str] = []
    for instruction in treatment.get("instructions") or []:
        action = instruction.get("actionId") or instruction.get("subtype") or instruction.get("type") or "action"
        params = instruction.get("actionParams") or {}
        formatted_params = []
        for key, value in params.items():
            rendered = _format_hex_value(value)
            if key == "port" and re.fullmatch(r"[0-9a-fA-F]{1,4}", str(value or "")):
                try:
                    rendered = str(int(str(value), 16))
                except ValueError:
                    pass
            formatted_params.append(f"{key}={rendered}")
        parts.append(f"{action}({', '.join(formatted_params)})" if formatted_params else action)
    return "; ".join(parts) if parts else "-"


def _format_flow_stats_reply(device_id: str | None, flows: list[dict], *, limit: int = 20) -> str:
    label = device_id or "所有 BMv2 设备"
    state_counts = Counter(str(flow.get("state") or "UNKNOWN") for flow in flows)
    total_packets = sum(int(flow.get("packets") or 0) for flow in flows)
    total_bytes = sum(int(flow.get("bytes") or 0) for flow in flows)
    state_summary = "，".join(f"{state} {count}" for state, count in sorted(state_counts.items())) or "无状态"

    lines = [
        f"{label} 当前实时流表：共 {len(flows)} 条（{state_summary}）。",
        f"累计计数：{total_packets} packets / {total_bytes} bytes。",
    ]

    if not flows:
        lines.append("ONOS 当前没有返回该设备的流表项。")
        return "\n".join(lines)

    lines.append(f"明细如下（最多显示前 {min(limit, len(flows))} 条）：")
    for index, flow in enumerate(flows[:limit], start=1):
        table_name = _table_name_for_id(flow.get("tableId") or flow.get("tableName"))
        lines.extend([
            f"{index}. {flow.get('state', '-')} | {table_name} | priority={flow.get('priority', '-')}"
            f" | packets={flow.get('packets', 0)} | bytes={flow.get('bytes', 0)}",
            f"   match: {_summarize_selector(flow.get('selector') or {})}",
            f"   action: {_summarize_treatment(flow.get('treatment') or {})}",
        ])
    if len(flows) > limit:
        lines.append(f"还有 {len(flows) - limit} 条未展开。")
    return "\n".join(lines)


def _chunk_text(text: str, size: int = 120) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _demo_scenario_label(scenario: str) -> str:
    """Return a human-readable Chinese label for a demo scenario key."""
    return {
        "qos": "QoS 带宽保障 (DSCP EF + 100Mbps)",
        "block": "流量阻断 (DROP, 冲突拦截演示)",
        "reroute": "路径切换 (s_core1 → s_core2)",
        "ratelimit": "速率限制 (限速 10Mbps)",
        "self_heal": "自主自愈 (拥塞检测→切换→限速→验证)",
        "diagnostic": "诊断评估 (路径健康分析)",
    }.get(scenario, "通用网络意图")


class ChatSession:
    """Unified conversation session.

    One instance per session_id.  The route handler calls one method and
    iterates the returned SSE events — no business logic in the router.
    """

    def __init__(self, session_id: str, store):
        self.session_id = session_id
        self._store = store

    # ── Public API ────────────────────────────────────────────────────────

    async def send_message(
        self, text: str, user_id: str = "anonymous",
        *, _trigger_pipeline: bool = True,
    ) -> AsyncIterator[SSEEvent]:
        """Send a user message and stream all resulting events.

        1. Persist user message
        2. Load conversation history
        3. Stream LLM reply token-by-token
        4. Classify intent
        5. If intent detected and _trigger_pipeline: enqueue STAV job
        6. Emit final metadata

        The caller iterates SSEEvent items and converts to SSE frames.
        """
        import os
        _demo = os.getenv("IDN_DEMO_MODE", "").lower() == "true"

        # ── 1. User message ──
        user_msg = Message.user_text(self.session_id, text)
        self._persist_message(user_msg)
        yield SSEEvent.message_created(user_msg)

        # ── 2. Load history ──
        history = self._store.list_messages(self.session_id)
        history = [m for m in history if m.get("id") != user_msg.id]

        # ── 3. Create thinking placeholder ──
        thinking_msg = Message.assistant_thinking(self.session_id)
        self._persist_message(thinking_msg)
        yield SSEEvent.message_created(thinking_msg)

        # ── 4. Stream reply ──
        full_reply = ""
        seq = 0

        readonly_query = _parse_flow_stats_query(text)
        if readonly_query is not None:
            try:
                from mcp_servers.onos_server import onos_client

                raw = await onos_client.get_flows(readonly_query.get("device_id"))
                full_reply = _format_flow_stats_reply(
                    readonly_query.get("device_id"),
                    raw.get("flows") or [],
                )
            except Exception as exc:
                logger.exception("Read-only flow table query failed")
                target = readonly_query.get("device_id") or "所有 BMv2 设备"
                full_reply = f"读取 {target} 实时流表失败：{exc}"

            for chunk in _chunk_text(full_reply):
                seq += 1
                yield SSEEvent.token(token=chunk, message_id=thinking_msg.id, seq=seq)

            thinking_msg.type = MessageType.ASSISTANT_TEXT
            thinking_msg.content = full_reply
            self._update_message_content(thinking_msg.id, full_reply, MessageType.ASSISTANT_TEXT)
            yield SSEEvent.message_updated(thinking_msg)
            yield SSEEvent.stream_end(extra={
                "session_id": self.session_id,
                "assistant_message_id": thinking_msg.id,
                "reply": full_reply,
                "intent_detected": False,
                "intent_id": None,
                "intent_type": "READ_FLOW_STATS",
                "confidence": 1.0,
                "query_type": "flow_stats",
            })
            return

        if _demo:
            # ── Demo mode: simulate token streaming without real LLM ──
            from verify.demo_simulator import match_scenario, STAGE_SUMMARIES
            scenario = match_scenario(text)
            sums = STAGE_SUMMARIES.get(scenario, STAGE_SUMMARIES["qos"])
            demo_reply = (
                f"收到，正在为您编排网络意图。\n"
                f"场景识别：{_demo_scenario_label(scenario)}\n"
                f"启动六阶段 STAV 管线：感知→解析→检查→生成→执行→验证。\n"
                f"请稍候，约 10 秒后返回执行结果..."
            )
            for ch in demo_reply:
                token = ch
                full_reply += token
                seq += 1
                yield SSEEvent.token(token=token, message_id=thinking_msg.id, seq=seq)
                await asyncio.sleep(0.03)  # simulate typing speed

            thinking_msg.type = MessageType.ASSISTANT_TEXT
            thinking_msg.content = full_reply
            self._update_message_content(thinking_msg.id, full_reply, MessageType.ASSISTANT_TEXT)
            yield SSEEvent.message_updated(thinking_msg)
        else:
            # ── Real mode: call LLM ──
            llm_messages = self._build_llm_messages(history, text)
            try:
                from agents.llm_client import stream_llm
                async for token in self._stream_tokens(llm_messages):
                    full_reply += token
                    seq += 1
                    yield SSEEvent.token(token=token, message_id=thinking_msg.id, seq=seq)

                if not full_reply.strip():
                    raise RuntimeError(
                        "LLM returned an empty response. Please check WANGTIAO2026_API_KEY "
                        "or DASHSCOPE_API_KEY, or enable IDN_MOCK_MODE=true for local demo."
                    )

                thinking_msg.type = MessageType.ASSISTANT_TEXT
                thinking_msg.content = full_reply
                self._update_message_content(thinking_msg.id, full_reply, MessageType.ASSISTANT_TEXT)
                yield SSEEvent.message_updated(thinking_msg)

            except Exception as exc:
                logger.exception("LLM streaming failed")
                error_msg = Message.error(self.session_id, f"AI 回复生成失败: {exc}")
                self._persist_message(error_msg)
                yield SSEEvent.message_created(error_msg)
                yield SSEEvent.stream_end()
                return

        # ── 5. Intent classification ──
        intent_id = None
        intent_detected = False
        classification = ClassificationResult()

        if _trigger_pipeline:
            if _demo:
                # ── Demo mode: always detect intent, match scenario ──
                from verify.demo_simulator import match_scenario
                scenario = match_scenario(text)
                classification = ClassificationResult(
                    intent_detected=True,
                    actionable=True,
                    intent_hint=text,
                    intent_type=scenario,
                    confidence=0.95,
                )
            else:
                try:
                    from agents.llm_client import get_llm
                    llm = get_llm()
                    classification = await classify_chat_response(full_reply, text, llm)
                except Exception as exc:
                    logger.warning("Intent classification failed: %s", exc)

            # ── 6. Handle classification result ──
            if classification.actionable and classification.intent_detected:
                intent_id = self._create_intent_and_enqueue(
                    text=classification.intent_hint or text,
                    user_id=user_id,
                    original_input=text,
                )
                if intent_id:
                    intent_detected = True
                    submitted_msg = Message.intent_submitted(
                        self.session_id, intent_id,
                        f"意图已识别：{classification.intent_hint or text}（置信度 {classification.confidence:.0%}）",
                    )
                    self._persist_message(submitted_msg)
                    yield SSEEvent.message_created(submitted_msg)

            elif classification.intent_detected and classification.clarification_question:
                intent_id = self._create_intent_and_enqueue(
                    text=text, user_id=user_id, original_input=text,
                    clarification_pending=True,
                )
                if intent_id:
                    clarification_msg = Message.clarification_question(
                        self.session_id, intent_id,
                        classification.clarification_question,
                    )
                    self._persist_message(clarification_msg)
                    yield SSEEvent.message_created(clarification_msg)

        # ── 7. Final SSE event ──
        yield SSEEvent.stream_end(extra={
            "session_id": self.session_id,
            "assistant_message_id": thinking_msg.id,
            "reply": full_reply,
            "intent_detected": intent_detected,
            "intent_id": intent_id,
            "intent_type": classification.intent_type,
            "confidence": classification.confidence,
        })

    async def answer_clarification(
        self, intent_id: str, answer: str,
    ) -> AsyncIterator[SSEEvent]:
        """Submit a clarification answer and resume the pipeline."""
        answer_msg = Message.clarification_answer(self.session_id, intent_id, answer)
        self._persist_message(answer_msg)
        yield SSEEvent.message_created(answer_msg)

        # Update the intent with the clarification
        record = self._store.get(intent_id)
        if record:
            previous_context = dict(record.get("context") or {})
            clarifications = previous_context.get("clarifications") or []
            clarifications.append({
                "question": (record.get("snapshot") or {}).get("clarification_question"),
                "answer": answer,
            })
            previous_context["clarifications"] = clarifications
            previous_context["latest_clarification_answer"] = answer
            execution_text = f"{record['execution_text']}\n用户补充说明：{answer}"
            self._store.update(intent_id, status="pending", execution_text=execution_text,
                              context=previous_context)
            self._store.enqueue_job(intent_id, {
                "text": record["text"],
                "priority": record["priority"],
                "user_id": record["user_id"],
                "session_id": self.session_id,
                "context": previous_context,
            })

            progress_msg = Message.system(
                self.session_id,
                f"已收到补充说明，继续执行意图 {intent_id[-8:]}...",
            )
            self._persist_message(progress_msg)
            yield SSEEvent.message_created(progress_msg)

        yield SSEEvent.stream_end(extra={"intent_id": intent_id, "status": "pending"})

    async def submit_approval(
        self, intent_id: str, decision: str, reason: str = "",
    ) -> AsyncIterator[SSEEvent]:
        """Submit an approval decision and resume the graph."""
        decision_msg = Message.approval_decision(self.session_id, intent_id, decision, reason)
        self._persist_message(decision_msg)
        yield SSEEvent.message_created(decision_msg)

        yield SSEEvent.stream_end(extra={"intent_id": intent_id, "decision": decision})

    # ── Internal helpers ──────────────────────────────────────────────────

    def _persist_message(self, message: Message) -> None:
        """Persist a Message to the API store."""
        try:
            self._store.add_message(
                session_id=message.session_id,
                role=message.role,
                message_type=message.type.value,
                content=message.content,
                intent_id=message.intent_id,
                payload=message.metadata,
            )
        except Exception as exc:
            logger.exception("Failed to persist message %s: %s", message.id, exc)

    def _update_message_content(self, message_id: str, content: str, msg_type: MessageType) -> None:
        """Update an existing message's content (used after streaming completes)."""
        # The store doesn't have a direct update_message, but we can
        # re-persist with the same id.  Since we use INSERT (not UPSERT),
        # we store the final content in the metadata for later retrieval.
        try:
            self._store.add_message(
                session_id=self.session_id,
                role="assistant",
                message_type=msg_type.value,
                content=content,
                intent_id=None,
                payload={"original_message_id": message_id, "final": True},
            )
        except Exception:
            pass  # best-effort; the streaming SSE already delivered the content

    async def _stream_tokens(self, llm_messages: list) -> AsyncIterator[str]:
        """Stream tokens from the LLM, yielding each token."""
        from agents.llm_client import stream_llm

        token_queue: asyncio.Queue = asyncio.Queue()
        stream_error: Exception | None = None

        def _on_token(token: str):
            token_queue.put_nowait(("token", token))

        async def _produce():
            nonlocal stream_error
            try:
                await stream_llm(llm_messages, on_token=_on_token)
            except Exception as exc:
                stream_error = exc
            finally:
                token_queue.put_nowait(("done", None))

        producer = asyncio.create_task(_produce())

        while True:
            kind, value = await token_queue.get()
            if kind == "done":
                break
            if stream_error:
                raise stream_error
            yield value

        await producer

    def _build_llm_messages(self, history: list[dict], user_text: str) -> list:
        """Build LangChain messages from conversation history."""
        messages: list = [LangSystemMessage(content=CHAT_SYSTEM_PROMPT)]

        for msg in history[-20:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                continue
            if role == "user":
                messages.append(LangHumanMessage(content=content))
            elif role == "assistant":
                messages.append(LangAIMessage(content=content))
            elif role == "system":
                messages.append(LangSystemMessage(content=f"[系统通知] {content}"))

        messages.append(LangHumanMessage(content=user_text))
        return messages

    def _create_intent_and_enqueue(
        self, text: str, user_id: str, original_input: str,
        clarification_pending: bool = False,
    ) -> str | None:
        """Create an intent record and enqueue a worker job. Returns intent_id or None."""
        try:
            from uuid import uuid4
            from datetime import datetime, timezone

            intent_id = f"INT-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid4().hex[:8]}"
            status = "pending_clarification" if clarification_pending else "pending"

            self._store.create(
                intent_id=intent_id, text=text, priority="medium",
                user_id=user_id, session_id=self.session_id,
                context={"source": "chat_escalation", "original_input": original_input},
            )
            if status == "pending_clarification":
                self._store.update(intent_id, status="pending_clarification",
                    snapshot={"needs_clarification": True})

            if not clarification_pending:
                self._store.enqueue_job(intent_id, {
                    "text": text, "priority": "medium",
                    "user_id": user_id, "session_id": self.session_id,
                    "context": {"source": "chat_escalation", "original_input": original_input},
                })

            return intent_id
        except Exception as exc:
            logger.exception("Failed to create intent: %s", exc)
            return None

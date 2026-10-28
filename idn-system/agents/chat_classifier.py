"""
agents/chat_classifier.py
Structured intent detection for the chat endpoint.

The chat LLM outputs plain-text replies (no JSON wrapper — that would
leak raw JSON into the user-visible stream).  Intent classification
runs as a separate, small LLM call after the reply text is complete.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

CHAT_SYSTEM_PROMPT = """你是 IDN 智能网络助手。用简洁的中文回答用户的网络相关问题。

## 风格要求

- 回复直接使用中文自然语言，不要输出 JSON、代码块或 Markdown 格式标记。
- 对于问候和闲聊，友好地自我介绍并引导用户描述网络需求。
- 如果用户描述了一个网络操作需求（带宽保障、流量阻断、路由调整等），
  先简短确认你理解了这个需求，然后告诉用户系统即将进行意图编排。
- 简洁专业，每次回复控制在 200 字以内。
- 如果对话历史中有之前的上下文，请结合上下文理解用户的意图。"""


async def classify_chat_response(
    reply_text: str,
    user_input: str,
    llm,
) -> dict[str, Any]:
    """Run a separate classification pass to get structured intent metadata.

    This is a focused, single-turn call that asks the LLM only: "is this
    a network intent?" — independent of the conversational reply prompt.
    """
    classify_prompt = f"""判断以下用户输入是否包含网络操作意图。

用户输入: {user_input}
助手回复: {reply_text[:200]}

返回 JSON 对象（只输出 JSON，不要有其他文字）:
{{"intent_detected": true或false, "intent_hint": "意图的简要中文描述", "intent_type": "SET_PRIORITY或RATE_LIMIT或DROP或FORWARD或MIRROR或空字符串", "confidence": 0.0到1.0之间的数值}}

网络操作意图包括：IP/网段配置、带宽保障/限速、QoS/SLA设置、流量阻断/隔离/绕行、
网络优化、策略部署、流表下发、路由修改等。
普通对话（问候、咨询、概念解释）intent_detected应为false。"""

    try:
        response = await llm.ainvoke([
            SystemMessage(content="你是网络意图分类器。只输出 JSON，不要有其他文字。"),
            HumanMessage(content=classify_prompt),
        ])
        text = response.content if hasattr(response, "content") else str(response)
        return _parse_classification(text)
    except Exception as exc:
        logger.warning("Intent classification failed: %s", exc)
        return {
            "intent_detected": False,
            "intent_hint": "",
            "intent_type": "",
            "confidence": 0.0,
        }


def _parse_classification(text: str) -> dict[str, Any]:
    """Parse the classification JSON from LLM output, with fallback."""
    try:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("\n```", 1)[0]
        elif cleaned.startswith("```json"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("\n```", 1)[0]
        data = json.loads(cleaned)
        return {
            "intent_detected": bool(data.get("intent_detected", False)),
            "intent_hint": str(data.get("intent_hint") or ""),
            "intent_type": str(data.get("intent_type") or ""),
            "confidence": float(data.get("confidence", 0.0)),
        }
    except (json.JSONDecodeError, ValueError):
        return {
            "intent_detected": False,
            "intent_hint": "",
            "intent_type": "",
            "confidence": 0.0,
        }


def build_chat_messages(user_text: str, history: list[dict] | None = None) -> list:
    """Build the LangChain message list for a chat completion.

    Includes conversation history as proper message types so the LLM
    can maintain multi-turn context.
    """
    messages: list = [SystemMessage(content=CHAT_SYSTEM_PROMPT)]

    if history:
        for msg in history[-10:]:  # Last 10 messages
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                continue
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
            # system messages (cancellation notices etc.) are informational
            elif role == "system":
                messages.append(SystemMessage(content=f"[系统通知] {content}"))

    messages.append(HumanMessage(content=user_text))
    return messages

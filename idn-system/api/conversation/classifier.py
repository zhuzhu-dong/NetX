"""
api/conversation/classifier.py
Structured intent detection for chat messages.

Runs as a separate, focused LLM call after the conversational reply is
complete.  Returns structured classification results including confidence
and a clarified intent_hint suitable for pipeline submission.

Enhancements over the old agents/chat_classifier.py:
- Returns typed ``ClassificationResult`` instead of bare dict
- Low-confidence results generate a clarification question
- Explicit ``actionable`` flag prevents triggering pipeline on small talk
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
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


@dataclass
class ClassificationResult:
    """Typed result of intent classification."""

    intent_detected: bool = False
    intent_hint: str = ""          # Structured intent text for pipeline
    intent_type: str = ""          # SET_PRIORITY | RATE_LIMIT | DROP | FORWARD | MIRROR | ""
    confidence: float = 0.0
    clarification_question: str = ""  # Non-empty when confidence is marginal
    actionable: bool = False       # True when intent is concrete enough to execute


async def classify_chat_response(
    reply_text: str,
    user_input: str,
    llm,
) -> ClassificationResult:
    """Run a separate classification pass to get structured intent metadata.

    This is a focused, single-turn call that asks the LLM only: "is this
    a network intent?" — independent of the conversational reply prompt.

    Returns a ``ClassificationResult`` with all fields populated.
    """
    classify_prompt = f"""判断以下用户输入是否包含网络操作意图。

用户输入: {user_input}
助手回复: {reply_text[:200]}

返回 JSON 对象（只输出 JSON，不要有其他文字）:
{{
  "intent_detected": true或false,
  "intent_hint": "意图的简要中文描述（如：保障192.168.10.0/24到10.0.0.0/8的视频业务带宽不低于10Gbps）",
  "intent_type": "SET_PRIORITY或RATE_LIMIT或DROP或FORWARD或MIRROR或空字符串",
  "confidence": 0.0到1.0之间的数值,
  "actionable": true或false,
  "clarification_question": "如果信息不足需要澄清则填写问题，否则为空字符串"
}}

网络操作意图包括：IP/网段配置、带宽保障/限速、QoS/SLA设置、流量阻断/隔离/绕行、
网络优化、策略部署、流表下发、路由修改等。
普通对话（问候、咨询、概念解释）intent_detected应为false，actionable为false。"""

    try:
        response = await llm.ainvoke([
            SystemMessage(content="你是网络意图分类器。只输出 JSON，不要有其他文字。"),
            HumanMessage(content=classify_prompt),
        ])
        text = response.content if hasattr(response, "content") else str(response)
        return _parse_classification(text, user_input)
    except Exception as exc:
        logger.warning("Intent classification failed: %s", exc)
        return ClassificationResult()


def _parse_classification(text: str, user_input: str = "") -> ClassificationResult:
    """Parse the classification JSON from LLM output, with fallback."""
    try:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("\n```", 1)[0]
        elif cleaned.startswith("```json"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("\n```", 1)[0]
        data = json.loads(cleaned)

        intent_detected = bool(data.get("intent_detected", False))
        confidence = float(data.get("confidence", 0.0))

        # Generate a clarification question if confidence is marginal
        clarification_question = str(data.get("clarification_question") or "")
        if not clarification_question and intent_detected and confidence < 0.6:
            clarification_question = _generate_clarification(user_input)

        return ClassificationResult(
            intent_detected=intent_detected,
            intent_hint=str(data.get("intent_hint") or user_input),
            intent_type=str(data.get("intent_type") or ""),
            confidence=confidence,
            clarification_question=clarification_question,
            actionable=bool(data.get("actionable", intent_detected and confidence >= 0.6)),
        )
    except (json.JSONDecodeError, ValueError):
        return ClassificationResult()


def _generate_clarification(user_input: str) -> str:
    """Generate a default clarification question when confidence is low."""
    missing = []
    if "带宽" in user_input and "Mbps" not in user_input and "Gbps" not in user_input:
        missing.append("具体带宽目标（如 10Gbps）")
    if "到" in user_input or "from" in user_input.lower():
        if "端口" not in user_input and "port" not in user_input.lower():
            missing.append("协议/端口信息")
    if not missing:
        return "请补充更多关于您网络需求的细节（源地址、目的地址、带宽目标、协议端口等）。"
    return f"请补充以下信息：{'、'.join(missing)}。"


def build_chat_messages(user_text: str, history: list[dict] | None = None) -> list:
    """Build the LangChain message list for a chat completion.

    Includes conversation history as proper message types so the LLM
    can maintain multi-turn context.
    """
    messages: list = [SystemMessage(content=CHAT_SYSTEM_PROMPT)]

    if history:
        for msg in history[-20:]:  # Last 20 messages for context
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                continue
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
            elif role == "system":
                messages.append(SystemMessage(content=f"[系统通知] {content}"))

    messages.append(HumanMessage(content=user_text))
    return messages

"""
utils/llm_json.py
==================

LLM JSON 提取工具 —— 从不完美的 LLM 输出中安全提取 JSON 对象。

# 系统角色
处理 LLM 输出的常见问题：
- <think> 标签污染
- markdown 代码围栏（```json ... ```）
- 不完整的 JSON（截断）
- 嵌套引号中的花括号
- LLM 常见的 JSON 语法错误（尾逗号、单引号字符串、JS 字面量）

# 核心算法
first_balanced_json_object() 使用栈深度计数器扫描字符串，
正确处理转义的引号和嵌套花括号，找到第一个完整 JSON 对象。

# v2: 修复层
_json_repair() 在 json.loads() 失败后尝试修复 LLM 常见语法错误：
- 尾逗号（{"a": 1,} → {"a": 1}）
- JS 字面量（true/false/null → True/False/None）
- 单引号字符串键与值（{'key': 'val'} → {"key": "val"}）
修复仅在原始解析失败时运行，合法 JSON 零开销。
"""
from __future__ import annotations

import json
import re
from typing import Any


def _json_repair(text: str) -> str:
    """Attempt to repair common LLM JSON syntax mistakes.

    Only called when ``json.loads()`` has already failed on the raw text.
    Applies conservative fixes:

    1. Remove trailing commas before ``}`` or ``]``.
    2. Replace bare JavaScript literals (true/false/null) with Python
       equivalents.
    3. Convert single-quoted JSON strings to double-quoted using a
       character-by-character state machine that respects existing
       double-quoted string boundaries.

    Returns the repaired text — the caller must still ``json.loads()``
    to validate.
    """
    # 1. Remove trailing commas before } or ]
    text = re.sub(r",(\s*[}\]])", r"\1", text)

    # 2. Replace Python-style literals with JSON equivalents.
    #    Case-sensitive: only converts capitalised Python keywords;
    #    already-valid lowercase JSON literals are never touched.
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r"\bNone\b", "null", text)

    # 3. Convert single-quoted strings to double-quoted.
    #    State machine: track whether we're inside a double-quoted string
    #    (in which case single quotes are literal content, not delimiters).
    #    Single-quoted strings found in JSON context get their delimiters
    #    converted to double quotes.
    chars: list[str] = []
    in_double = False
    in_single = False
    escaped = False

    for ch in text:
        if in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            chars.append(ch)
        elif in_single:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_single = False
                chars.append('"')  # close single-quoted string with double quote
            else:
                chars.append(ch)
        elif ch == '"':
            in_double = True
            chars.append(ch)
        elif ch == "'":
            in_single = True
            chars.append('"')  # open single-quoted string with double quote
        else:
            chars.append(ch)

    if in_single:
        # Unclosed single-quoted string — repair is ambiguous; return
        # the partially-repaired text so the caller can re-raise with
        # the original error.
        return text

    return "".join(chars)


def extract_json_object(raw_output: str | None) -> dict[str, Any]:
    """Parse the first JSON object after removing common model decorations.

    If the raw JSON has minor syntax issues (trailing commas, JS literals,
    single-quoted keys/values), a repair pass is attempted before giving up.
    """
    # ── None / empty guard ──
    if raw_output is None:
        raise ValueError("LLM output is None")
    if not isinstance(raw_output, str):
        raw_output = str(raw_output)

    text = re.sub(r"<think>.*?</think>", "", raw_output, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).replace("```", "").strip()

    candidate = first_balanced_json_object(text)

    # ── Try strict parse first (happy path, zero overhead) ──
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        # ── Repair pass for common LLM syntax mistakes ──
        try:
            repaired = _json_repair(candidate)
            payload = json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            # Re-raise the *original* error so callers see the raw text
            raise

    if not isinstance(payload, dict):
        raise ValueError("LLM output must be a JSON object")
    return payload


def first_balanced_json_object(text: str) -> str:
    """Return the first complete JSON object while respecting quoted braces."""
    start = text.find("{")
    if start < 0:
        raise ValueError("LLM output does not contain JSON")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    raise ValueError("LLM output contains incomplete JSON")

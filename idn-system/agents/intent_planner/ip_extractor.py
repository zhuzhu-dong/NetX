"""
agents/intent_planner/ip_extractor.py
Pure IP, CIDR, bandwidth, and number extraction functions.

Extracted from ``IntentPlannerAgent`` to make NLP extraction independently
testable without LLM or agent infrastructure.
"""
from __future__ import annotations

import re
from ipaddress import ip_network
from typing import Optional


def chinese_number(text: str) -> Optional[int]:
    """Convert Chinese numeral text to integer (e.g. '一百二十' → 120)."""
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = section = number = 0
    for char in text:
        if char in digits:
            number = digits[char]
        elif char in units:
            unit = units[char]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = number = 0
            else:
                section += (number or 1) * unit
                number = 0
        else:
            return None
    return total + section + number


def normalize_cidr(value: any) -> Optional[str]:
    """Normalize an IP or CIDR string; return None for invalid input."""
    if value in (None, "", "null"):
        return None
    text = str(value).strip()
    try:
        return str(ip_network(text if "/" in text else f"{text}/32", strict=False))
    except ValueError:
        return None


def extract_ip_prefixes(text: str) -> list[str]:
    """Extract all IPv4 CIDR prefixes from *text*."""
    values = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b", text)
    output: list[str] = []
    for value in values:
        normalized = normalize_cidr(value)
        if normalized and normalized not in output:
            output.append(normalized)
    return output


def extract_directional_ip_roles(text: str) -> tuple[Optional[str], Optional[str]]:
    """Parse directional IP patterns like 'A to B' → (A, B)."""
    ip = r"((?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?)"
    patterns = [
        (rf"{ip}\s*(?:被|接收来自)\s*{ip}", True),
        (rf"{ip}\s+is\s+accessed\s+by\s+{ip}", True),
        (rf"{ip}\s*(?:到|至|访问|连接到|连到)\s*{ip}", False),
        (rf"{ip}\s+(?:to|access(?:es)?|connect(?:s)?\s+to)\s+{ip}", False),
    ]
    for pattern, passive in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            first = normalize_cidr(match.group(1))
            second = normalize_cidr(match.group(2))
            return (second, first) if passive else (first, second)
    return None, None


def extract_bandwidth_mbps(text: str) -> Optional[int]:
    """Extract bandwidth value in Mbps from *text*."""
    pattern = (
        r"(\d+(?:\.\d+)?)\s*"
        r"(gbps|gbit/s|g\b|mbps|mbit/s|(?<![a-zA-Z])m(?!s|[a-zA-Z])|kbps|kbit/s|k\b|兆|吉)"
    )
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match:
        value = float(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("g") or match.group(2) == "吉":
            return int(value * 1000)
        if unit.startswith("k"):
            return max(1, int(value / 1000))
        return int(value)

    chinese = re.search(r"([零一二两三四五六七八九十百千万]+)\s*兆", text)
    return chinese_number(chinese.group(1)) if chinese else None


def extract_latency_ms(text: str) -> Optional[float]:
    """Extract latency value in milliseconds from *text*."""
    match = re.search(r"(?:时延|延迟|latency).*?(\d+(?:\.\d+)?)\s*ms", text, re.IGNORECASE)
    return float(match.group(1)) if match else None

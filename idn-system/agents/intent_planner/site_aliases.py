"""
agents/intent_planner/site_aliases.py
Site alias resolution — lookup named sites in natural-language intent text.

Extracted from ``IntentPlannerAgent`` for independent testability.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .ip_extractor import normalize_cidr


def add_alias_site(aliases: dict[str, dict[str, str]], site: dict[str, Any]) -> None:
    """Register a site (with its IP prefix) under every known alias."""
    prefix = normalize_cidr(site.get("ip_prefix"))
    name = str(site.get("name", "")).strip()
    if not prefix or not name:
        return
    roles = site.get("roles") or [site.get("role", "source")]
    if isinstance(roles, str):
        roles = [roles]
    for alias in [name, *site.get("aliases", [])]:
        mapping = aliases.setdefault(str(alias).lower(), {})
        for role in roles:
            mapping[str(role)] = prefix


def lookup_alias(text: str, aliases: dict[str, dict[str, str]], role: str) -> Optional[str]:
    """Resolve a role-bound site name in *text* against the alias table."""
    lowered = text.lower()
    for alias, mapping in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if alias not in lowered or role not in mapping:
            continue
        escaped = re.escape(alias)
        if role == "source" and re.search(
            rf"{escaped}\s*(?:到|至|->|=>|to\b|访问|连接到|发往)", lowered,
        ):
            return mapping[role]
        if role == "destination" and re.search(
            rf"(?:到|至|->|=>|\bto|访问|连接到|发往)\s*{escaped}", lowered,
        ):
            return mapping[role]
        if role == "source" and re.search(
            rf"{escaped}\s*(?:访问|到|至|连接到)|(?:来自|from)\s*{escaped}", lowered,
        ):
            return mapping[role]
        if role == "destination" and re.search(
            rf"(?:访问|到|至|连接到|to)\s*{escaped}|{escaped}\s*(?:接收来自|被)", lowered,
        ):
            return mapping[role]
        ordered = lookup_alias_by_order(lowered, aliases, alias, role)
        if ordered:
            return ordered
        if len(mapping) == 1:
            return mapping[role]
def resolve_device_for_ip(ip_str: str | None) -> str | None:
    """Return the *device_id* of the site whose *ip_prefix* contains *ip_str*.

    Uses ``ipaddress`` for correct CIDR containment (``ip_address in
    ip_network``) rather than brittle string-prefix matching.

    Returns *None* when *ip_str* is empty, not a valid IP address, or
    does not match any known site.
    """
    if not ip_str:
        return None

    try:
        from ipaddress import ip_address, ip_network
    except ImportError:  # pragma: no cover — stdlib
        return None

    try:
        host_ip = ip_address(str(ip_str).split("/")[0])
    except ValueError:
        return None

    config_path = (
        Path(__file__).resolve().parent.parent.parent / "config" / "site_aliases.json"
    )
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    for site in data.get("known_sites", []):
        prefix = site.get("ip_prefix")
        device_id = site.get("device_id")
        if not prefix or not device_id:
            continue
        try:
            net = ip_network(prefix, strict=False)
        except ValueError:
            continue
        if host_ip in net:
            return str(device_id)

    return None


def lookup_alias_by_order(
    lowered: str,
    aliases: dict[str, dict[str, str]],
    alias: str,
    role: str,
) -> Optional[str]:
    """Resolve alias by word-order heuristic when directional patterns don't match."""
    position = lowered.find(alias)
    if position < 0:
        return None
    opposite_role = "destination" if role == "source" else "source"
    for other_alias, other_mapping in sorted(
        aliases.items(), key=lambda item: len(item[0]), reverse=True,
    ):
        if other_alias == alias or opposite_role not in other_mapping:
            continue
        other_position = lowered.find(other_alias)
        if other_position < 0:
            continue
        if role == "source" and position < other_position:
            return aliases[alias][role]
        if role == "destination" and other_position < position:
            return aliases[alias][role]
    return None

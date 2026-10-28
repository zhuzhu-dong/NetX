"""
infra/config.py
ConfigManager — single source of truth for all environment-variable-driven
runtime configuration.

Only one mock mode remains: ``IDN_MOCK_MODE=true`` which mocks **only** LLM
calls (replaces all LLM invocations with deterministic canned responses).
All other mock modes (MCP, flow delivery, telemetry, demo) have been removed.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ── LLM defaults ────────────────────────────────────────────────────────

DEFAULT_LLM_PROVIDER = "openai"
DEFAULT_LLM_MODEL = "qwen-plus"
DEFAULT_LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_LLM_MAX_TOKENS = 4096
DEFAULT_LLM_TEMPERATURE = 0.0


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    base_url: Optional[str]
    api_key: Optional[str]
    temperature: float
    max_tokens: Optional[int]


class LLMConfigurationError(RuntimeError):
    """Raised when real mode lacks an explicitly configured provider contract."""

    code = "LLM_CONFIGURATION_MISSING"

    def __init__(self, missing: list[str]):
        self.missing = list(missing)
        super().__init__(f"{self.code}: missing {', '.join(self.missing)}")


# ── Internal helpers ─────────────────────────────────────────────────────

def _env_bool(*names: str, default: bool = False) -> bool:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        value = value.strip()
        if not value:
            continue
        return value.lower() == "true"
    return default


def _env_value(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        value = value.strip()
        if value:
            return value
    return default


# ── ConfigManager ────────────────────────────────────────────────────────

class ConfigManager:
    """Centralised runtime configuration — all env-var reads go through here."""

    # ── Mode flags (only one mock mode remains) ──────────────────────────

    @classmethod
    def is_mock_mode(cls) -> bool:
        """LLM mock?

        When ``true``, all LLM calls use deterministic canned responses.
        Priority: ``IDN_MOCK_MODE`` > default (false).
        """
        return _env_bool("IDN_MOCK_MODE", default=False)

    # ── Watcher ──────────────────────────────────────────────────────────

    @classmethod
    def watcher_enabled(cls) -> bool:
        return _env_bool("IDN_WATCHER_ENABLED", default=True)

    # ── LLM config ───────────────────────────────────────────────────────

    @classmethod
    def llm_config(cls) -> LLMConfig:
        mock = cls.is_mock_mode()
        provider = (
            _env_value("LLM_PROVIDER", default=DEFAULT_LLM_PROVIDER if mock else None)
            or ""
        ).lower()
        model = _env_value(
            "LLM_MODEL",
            "LOCAL_QWEN_MODEL",
            default=DEFAULT_LLM_MODEL if mock else None,
        )
        base_url = _env_value(
            "LLM_BASE_URL", "OPENAI_API_BASE", "OPENAI_BASE_URL",
            "LOCAL_QWEN_BASE_URL", default=DEFAULT_LLM_BASE_URL if mock else None,
        )
        api_key = _env_value(
            "LLM_API_KEY", "WANGTIAO2026_API_KEY", "DASHSCOPE_API_KEY",
            "OPENAI_API_KEY", "LOCAL_QWEN_API_KEY", default=None,
        )
        temperature = float(_env_value("LLM_TEMPERATURE", default=str(DEFAULT_LLM_TEMPERATURE)) or 0)
        max_tokens_str = _env_value("LLM_MAX_TOKENS", default=str(DEFAULT_LLM_MAX_TOKENS))
        max_tokens = int(max_tokens_str) if max_tokens_str and max_tokens_str.isdigit() else None

        if provider == "anthropic":
            api_key = _env_value("LLM_API_KEY", "ANTHROPIC_API_KEY")
            base_url = None
            max_tokens = None

        if not mock:
            missing = []
            if not provider:
                missing.append("LLM_PROVIDER")
            if not model:
                missing.append("LLM_MODEL")
            if not api_key:
                missing.append("LLM_API_KEY")
            if missing:
                raise LLMConfigurationError(missing)

        return LLMConfig(
            provider=provider, model=model, base_url=base_url,
            api_key=api_key, temperature=temperature, max_tokens=max_tokens,
        )

    # ── Validation ───────────────────────────────────────────────────────

    @classmethod
    def validate(cls) -> list[str]:
        """Run startup consistency checks.  Returns a list of WARNING messages."""
        warnings: list[str] = []

        if cls.is_mock_mode():
            logger.info("LLM mock mode enabled — all LLM calls use canned responses")

        if not cls.is_mock_mode():
            try:
                cls.llm_config()
            except LLMConfigurationError as exc:
                warnings.append(str(exc))

        return warnings

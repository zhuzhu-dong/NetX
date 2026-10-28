"""Central, secret-safe LLM runtime and audited structured invocation helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Generic, Optional, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from models.intent_domain import LLMCallRecord, LLMRuntimeMode
from utils.llm_json import extract_json_object

logger = logging.getLogger(__name__)
T = TypeVar("T")


class _DefaultMockChatModel:
    def invoke(self, messages, **kwargs):
        return AIMessage(content="{}")

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content="{}")


class LLMInvocationError(RuntimeError):
    """A sanitized invocation failure safe for API, audit and UI surfaces."""

    def __init__(self, code: str, record: LLMCallRecord):
        self.code = code
        self.record = record
        super().__init__(code)


@dataclass(frozen=True)
class LLMInvocationResult(Generic[T]):
    value: T
    record: LLMCallRecord


_readiness: dict[str, Any] = {}


def load_llm_config():
    from infra.config import ConfigManager

    return ConfigManager.llm_config()


def llm_runtime_mode() -> LLMRuntimeMode:
    from infra.config import ConfigManager

    if ConfigManager.is_mock_mode():
        return LLMRuntimeMode.MOCK_LLM
    try:
        ConfigManager.llm_config()
    except Exception:
        return LLMRuntimeMode.LLM_UNAVAILABLE
    return LLMRuntimeMode.REAL_LLM


def get_llm(mock_factory: Optional[Callable[[], BaseChatModel]] = None) -> BaseChatModel:
    """Create the configured model; real mode never falls through to a mock."""

    from infra.config import ConfigManager

    if ConfigManager.is_mock_mode():
        logger.info("LLM runtime mode=MOCK_LLM")
        return mock_factory() if mock_factory else _DefaultMockChatModel()

    config = load_llm_config()
    if config.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=config.model,
            temperature=config.temperature,
            api_key=config.api_key,
        )

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=config.model,
        temperature=config.temperature,
        api_key=config.api_key,
        base_url=config.base_url,
        max_tokens=config.max_tokens,
        request_timeout=float(os.getenv("IDN_LLM_REQUEST_TIMEOUT", "20.0")),
        max_retries=0,  # retries are bounded and audited by invoke_structured_llm
    )


def _messages_hash(messages: list[Any]) -> str:
    normalized = [
        {
            "role": message.__class__.__name__,
            "content": str(getattr(message, "content", message)),
        }
        for message in messages
    ]
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _structured_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    raise ValueError("structured validator must return an object")


def _json_shape(value: Any, *, depth: int = 0) -> Any:
    """Return a value-free JSON shape for safe schema-failure diagnostics."""

    if depth >= 3:
        return type(value).__name__
    if isinstance(value, dict):
        return {
            "type": "object",
            "keys": {
                str(key): _json_shape(item, depth=depth + 1)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            },
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "length": len(value),
            "item_shape": _json_shape(value[0], depth=depth + 1) if value else None,
        }
    return type(value).__name__


def _contains_device_command(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False, default=str).casefold()
    forbidden = (
        "tc qdisc",
        "p4runtime write",
        "docker exec",
        "curl http",
        "curl -",
        "rm -rf",
        "powershell -command",
        "bash -c",
        "shell command",
    )
    return any(token in text for token in forbidden)


def _classify_provider_error(exc: Exception) -> tuple[str, bool]:
    name = exc.__class__.__name__.casefold()
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    status = status or getattr(response, "status_code", None)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in name:
        return "LLM_TIMEOUT", True
    if status == 429 or "ratelimit" in name or "rate_limit" in name:
        return "LLM_RATE_LIMITED", True
    if isinstance(status, int) and status >= 500:
        return "LLM_PROVIDER_ERROR", True
    if "connection" in name or "unavailable" in name:
        return "LLM_UNAVAILABLE", True
    return "LLM_PROVIDER_ERROR", False


def _response_metadata(response: Any) -> tuple[str | None, int | None, int | None, int | None]:
    metadata = dict(getattr(response, "response_metadata", {}) or {})
    request_id = next(
        (
            str(value)
            for value in (
                metadata.get("request_id"),
                metadata.get("response_id"),
                metadata.get("id"),
                getattr(response, "id", None),
            )
            if value
        ),
        None,
    )
    usage = dict(getattr(response, "usage_metadata", {}) or {})
    token_usage = dict(metadata.get("token_usage") or {})
    input_tokens = usage.get("input_tokens", token_usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", token_usage.get("completion_tokens"))
    total_tokens = usage.get("total_tokens", token_usage.get("total_tokens"))
    return request_id, input_tokens, output_tokens, total_tokens


def _persist_call(record: LLMCallRecord) -> None:
    try:
        from persistence.intent_domain_store import get_intent_domain_store

        get_intent_domain_store().save_llm_call(record)
    except Exception:
        logger.warning("LLM audit persistence failed call_id=%s", record.call_id)


def finalize_llm_call_record(
    record: LLMCallRecord,
    *,
    validator_result: str,
    fallback_used: bool | None = None,
) -> LLMCallRecord:
    """Attach the downstream deterministic validator verdict to an LLM call."""

    record.validator_result = validator_result
    if fallback_used is not None:
        record.fallback_used = fallback_used
    _persist_call(record)
    return record


def _set_readiness(record: LLMCallRecord) -> None:
    global _readiness
    if record.runtime_mode == LLMRuntimeMode.MOCK_LLM:
        status = "MOCK"
    elif record.status == "SUCCESS":
        status = "READY"
    elif record.status in {"LLM_TIMEOUT", "LLM_RATE_LIMITED", "LLM_PROVIDER_ERROR"}:
        status = "DEGRADED"
    else:
        status = "UNAVAILABLE"
    _readiness = {
        "status": status,
        "runtime_mode": record.runtime_mode.value,
        "provider": record.provider,
        "model": record.model,
        "last_call_id": record.call_id,
        "last_call_status": record.status,
        "checked_at": (record.completed_at or record.started_at).isoformat(),
    }


async def invoke_structured_llm(
    messages: list[Any],
    *,
    agent: str,
    input_schema_version: str,
    output_schema_version: str,
    validator: Callable[[dict[str, Any]], T],
    llm: BaseChatModel | None = None,
    intent_id: str | None = None,
    revision: int | None = None,
    trace_id: str | None = None,
    correlation_id: str | None = None,
    input_refs: list[str] | None = None,
    fallback_on_error: bool = True,
    on_token: Callable[[str], None] | None = None,
    max_attempts: int | None = None,
) -> LLMInvocationResult[T]:
    """Invoke once (plus bounded transient retries), validate, and persist proof."""

    started = datetime.now(timezone.utc)
    started_perf = time.perf_counter()
    mode = llm_runtime_mode()
    try:
        config = load_llm_config()
        provider = config.provider
        model_name = config.model
    except Exception:
        provider = None
        model_name = None
    record = LLMCallRecord(
        intent_id=intent_id,
        revision=revision,
        agent=agent,
        provider=provider,
        model=model_name,
        runtime_mode=mode,
        started_at=started,
        input_schema_version=input_schema_version,
        output_schema_version=output_schema_version,
        trace_id=trace_id,
        correlation_id=correlation_id or f"CORR-{hashlib.sha256(f'{agent}:{started.isoformat()}'.encode()).hexdigest()[:12].upper()}",
        prompt_hash=_messages_hash(messages),
        input_refs=list(input_refs or []),
    )
    if mode == LLMRuntimeMode.LLM_UNAVAILABLE:
        record.status = "LLM_CONFIGURATION_MISSING"
        record.error_code = record.status
        record.fallback_used = fallback_on_error
        record.completed_at = datetime.now(timezone.utc)
        record.duration_ms = int((time.perf_counter() - started_perf) * 1000)
        _persist_call(record)
        _set_readiness(record)
        raise LLMInvocationError(record.status, record)

    attempts = max_attempts or max(1, int(os.getenv("IDN_LLM_MAX_ATTEMPTS", "2")))
    timeout = max(0.1, float(os.getenv("IDN_LLM_REQUEST_TIMEOUT", "20.0")))
    backoff = max(0.0, float(os.getenv("IDN_LLM_RETRY_BACKOFF_SECONDS", "0.25")))
    model_client = llm or get_llm()
    response = None
    final_code = "LLM_PROVIDER_ERROR"

    for attempt in range(1, attempts + 1):
        record.attempt_count = attempt
        try:
            response = await asyncio.wait_for(model_client.ainvoke(messages), timeout=timeout)
            break
        except Exception as exc:
            final_code, transient = _classify_provider_error(exc)
            if not transient or attempt >= attempts:
                response = None
                break
            await asyncio.sleep(min(backoff * (2 ** (attempt - 1)), 2.0))

    if response is None:
        record.status = final_code
        record.error_code = final_code
        record.fallback_used = fallback_on_error
        record.completed_at = datetime.now(timezone.utc)
        record.duration_ms = int((time.perf_counter() - started_perf) * 1000)
        _persist_call(record)
        _set_readiness(record)
        raise LLMInvocationError(final_code, record)

    raw_text = str(getattr(response, "content", response))
    if on_token:
        on_token(raw_text)
    request_id, input_tokens, output_tokens, total_tokens = _response_metadata(response)
    record.provider_request_id = request_id
    record.input_tokens = input_tokens
    record.output_tokens = output_tokens
    record.total_tokens = total_tokens
    try:
        payload = extract_json_object(raw_text)
        if _contains_device_command(payload):
            raise ValueError("device or shell command rejected")
        value = validator(payload)
        structured = _structured_dict(value)
    except Exception:
        record.status = "LLM_SCHEMA_INVALID"
        record.error_code = record.status
        record.structured_output_valid = False
        record.validator_result = "REJECTED"
        record.structured_output = {
            "validation_diagnostic": {
                "received_shape": _json_shape(locals().get("payload"))
            }
        }
        record.fallback_used = fallback_on_error
        record.completed_at = datetime.now(timezone.utc)
        record.duration_ms = int((time.perf_counter() - started_perf) * 1000)
        _persist_call(record)
        _set_readiness(record)
        raise LLMInvocationError(record.status, record)

    record.status = "SUCCESS"
    record.structured_output_valid = True
    record.validator_result = "PASS"
    record.structured_output = structured
    record.completed_at = datetime.now(timezone.utc)
    record.duration_ms = int((time.perf_counter() - started_perf) * 1000)
    _persist_call(record)
    _set_readiness(record)
    logger.info(
        "LLM call complete call_id=%s agent=%s provider=%s model=%s status=%s",
        record.call_id,
        agent,
        provider,
        model_name,
        record.status,
    )
    return LLMInvocationResult(value=value, record=record)


def get_llm_readiness() -> dict[str, Any]:
    mode = llm_runtime_mode()
    try:
        config = load_llm_config()
        provider, model_name = config.provider, config.model
    except Exception:
        provider, model_name = None, None
    if mode == LLMRuntimeMode.MOCK_LLM:
        return {
            "status": "MOCK",
            "runtime_mode": mode.value,
            "provider": provider,
            "model": model_name,
        }
    if mode == LLMRuntimeMode.LLM_UNAVAILABLE:
        return {
            "status": "UNAVAILABLE",
            "runtime_mode": mode.value,
            "provider": provider,
            "model": model_name,
            "reason": "LLM_CONFIGURATION_MISSING",
        }
    if _readiness:
        return dict(_readiness)
    return {
        "status": "DEGRADED",
        "runtime_mode": mode.value,
        "provider": provider,
        "model": model_name,
        "reason": "NOT_PROBED",
    }


async def probe_llm_readiness() -> dict[str, Any]:
    from langchain_core.messages import HumanMessage, SystemMessage

    if llm_runtime_mode() == LLMRuntimeMode.MOCK_LLM:
        return get_llm_readiness()
    try:
        await invoke_structured_llm(
            [
                SystemMessage(content='Return only {"ready": true}.'),
                HumanMessage(content="readiness probe"),
            ],
            agent="llm_readiness",
            input_schema_version="LLMReadinessInput.v1",
            output_schema_version="LLMReadinessOutput.v1",
            validator=lambda payload: payload
            if payload.get("ready") is True
            else (_ for _ in ()).throw(ValueError("invalid readiness response")),
            fallback_on_error=False,
            max_attempts=1,
        )
    except LLMInvocationError:
        pass
    return get_llm_readiness()


async def stream_llm(
    messages: list[Any],
    *,
    on_token: Optional[Callable[[str], None]] = None,
    mock_factory: Optional[Callable[[], BaseChatModel]] = None,
) -> str:
    """Compatibility streaming helper with a finite timeout and no mock failover."""

    model_client = get_llm(mock_factory=mock_factory)
    timeout = max(0.1, float(os.getenv("IDN_LLM_REQUEST_TIMEOUT", "20.0")))
    response = await asyncio.wait_for(model_client.ainvoke(messages), timeout=timeout)
    text = str(getattr(response, "content", response))
    if on_token:
        on_token(text)
    return text

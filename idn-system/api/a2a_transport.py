"""Pluggable A2A transport adapters.

The A2A protocol envelope is built by ``api.a2a_trace``. This module owns the
delivery boundary so the current in-process + SQLite evidence path can later be
replaced by Redis Streams without changing LangGraph node code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class A2ATransportResult:
    """Publish result returned by transport adapters."""

    transport: str
    status: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {"transport": self.transport, "status": self.status}
        if self.detail:
            payload["detail"] = self.detail
        return payload


class A2ATransport(Protocol):
    """Transport boundary for runtime A2A messages."""

    name: str

    def publish(self, intent_id: str, event_type: str, message: dict[str, Any]) -> A2ATransportResult:
        """Publish one normalized A2A message."""

    def describe(self) -> dict[str, Any]:
        """Return a public description for health/docs endpoints."""


class SQLiteEventTransport:
    """Persist A2A messages into the existing SQLite event stream."""

    name = "sqlite_event"

    def publish(self, intent_id: str, event_type: str, message: dict[str, Any]) -> A2ATransportResult:
        from api.events import get_event_manager

        transport = message.setdefault("transport", {})
        transport.setdefault("selected", self.name)
        transport.update(
            {
                "event_transport": self.name,
                "event_status": "persisted",
                "event_type": event_type,
            }
        )
        get_event_manager().push(intent_id, event_type, message)
        return A2ATransportResult(
            transport=self.name,
            status="persisted",
            detail="message persisted to api.store.events",
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "protocol_layer": "A2A JSON-RPC envelope",
            "event_layer": "SQLite events table",
            "transport_layer": "persistent local event stream",
            "durable": True,
            "distributed": False,
        }


class InProcessTransport:
    """Default transport: LangGraph in-process handoff plus SQLite evidence."""

    name = "inprocess"

    def __init__(self, event_transport: A2ATransport | None = None):
        self.event_transport = event_transport or SQLiteEventTransport()

    def publish(self, intent_id: str, event_type: str, message: dict[str, Any]) -> A2ATransportResult:
        transport = message.setdefault("transport", {})
        transport.update(
            {
                "selected": self.name,
                "event_transport": self.event_transport.name,
                "transport_layer": "inprocess",
                "status": "persisted",
            }
        )
        result = self.event_transport.publish(intent_id, event_type, message)
        transport.update(
            {
                "status": result.status,
                "detail": f"LangGraph in-process handoff; evidence via {result.transport}",
            }
        )
        return A2ATransportResult(
            transport=self.name,
            status=result.status,
            detail=f"LangGraph in-process handoff; evidence via {result.transport}",
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "protocol_layer": "A2A JSON-RPC envelope",
            "event_layer": self.event_transport.describe(),
            "transport_layer": "LangGraph in-process calls + durable event evidence",
            "durable": True,
            "distributed": False,
        }


class RedisStreamTransport:
    """Adapter placeholder for a future Redis Streams transport.

    The class is intentionally present so the message bus is pluggable today.
    Until the Redis dependency and stream consumer are introduced, publish falls
    back to SQLite evidence and marks the delivery as fallback.
    """

    name = "redis_stream"

    def __init__(self, fallback: A2ATransport | None = None):
        self.fallback = fallback or SQLiteEventTransport()

    def publish(self, intent_id: str, event_type: str, message: dict[str, Any]) -> A2ATransportResult:
        transport = message.setdefault("transport", {})
        transport.update(
            {
                "selected": self.name,
                "event_transport": self.fallback.name,
                "transport_layer": "redis_stream",
                "status": "fallback",
            }
        )
        result = self.fallback.publish(intent_id, event_type, message)
        transport.update(
            {
                "status": "fallback",
                "detail": (
                    "Redis Streams adapter boundary selected, but no Redis producer is "
                    f"enabled yet; evidence persisted via {result.transport}"
                ),
            }
        )
        return A2ATransportResult(
            transport=self.name,
            status="fallback",
            detail=(
                "Redis Streams adapter boundary selected, but no Redis producer is "
                f"enabled yet; evidence persisted via {result.transport}"
            ),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "protocol_layer": "A2A JSON-RPC envelope",
            "event_layer": self.fallback.describe(),
            "transport_layer": "Redis Streams adapter placeholder",
            "durable": True,
            "distributed": False,
            "status": "fallback_to_sqlite",
            "redis_url_configured": bool(os.getenv("IDN_A2A_REDIS_URL")),
        }


def get_a2a_transport() -> A2ATransport:
    """Return the configured A2A transport adapter."""
    mode = os.getenv("IDN_A2A_TRANSPORT", "inprocess").strip().lower()
    if mode in {"sqlite", "sqlite_event", "event"}:
        return SQLiteEventTransport()
    if mode in {"redis", "redis_stream", "redis-stream"}:
        return RedisStreamTransport()
    return InProcessTransport()


def describe_a2a_transport() -> dict[str, Any]:
    """Return selected transport and available adapter names."""
    selected = get_a2a_transport()
    return {
        "selected": selected.describe(),
        "available": ["inprocess", "sqlite_event", "redis_stream"],
        "env": {
            "IDN_A2A_TRANSPORT": os.getenv("IDN_A2A_TRANSPORT", "inprocess"),
            "IDN_A2A_REDIS_URL_CONFIGURED": bool(os.getenv("IDN_A2A_REDIS_URL")),
        },
    }

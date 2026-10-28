"""Persistent HTTP idempotency contract for mutation endpoints."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class IdempotencyConflictError(Exception):
    operation: str
    key: str
    code: str = "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"

    def __str__(self) -> str:
        return f"idempotency key {self.key!r} was already used for {self.operation}"

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "operation": self.operation,
            "idempotency_key": self.key,
            "message": str(self),
        }


class IdempotencyInProgressError(IdempotencyConflictError):
    code: str = "IDEMPOTENCY_REQUEST_IN_PROGRESS"

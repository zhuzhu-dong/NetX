"""Persistent single-writer fencing for network configuration writes.

The guard is intentionally independent of HTTP routing: callers at the API,
service and actuator boundaries use the same authorization decision.  Read
operations never need a lease; every mutating operation must carry the active
owner and the monotonically allocated token.
"""
from __future__ import annotations

import contextvars
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from api.store import IntentStore, get_store


GLOBAL_NETWORK_CONFIGURATION = "GLOBAL_NETWORK_CONFIGURATION"


def resource_write_scope(resource_key: str | None) -> str:
    """Return the durable write domain for one managed dataplane resource.

    A service classifier, its egress HTB tree and the ingress allocation used
    to exercise it must be fenced together.  Unbound state cannot name a safe
    resource and therefore remains globally fenced.
    """
    value = str(resource_key or "").strip()
    if not value or value.upper() == "UNBOUND":
        return GLOBAL_NETWORK_CONFIGURATION
    return f"DATAPLANE_RESOURCE::{value}"
_write_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "network_write_context", default=None
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NetworkWriteFencing:
    """SQLite-backed lease allocator and non-bypassable authorization check."""

    def __init__(self, store: IntentStore | None = None):
        self.store = store or get_store()
        with self.store._lock, self.store._connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS network_configuration_write_leases_v1 (
                    lease_id TEXT PRIMARY KEY, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL,
                    recovery_id TEXT NOT NULL, fencing_token INTEGER NOT NULL, acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, renewed_at TEXT, status TEXT NOT NULL,
                    resource_scope TEXT NOT NULL DEFAULT 'GLOBAL_NETWORK_CONFIGURATION'
                );
                CREATE TABLE IF NOT EXISTS network_write_fencing_sequences_v1 (
                    scope TEXT PRIMARY KEY, last_token INTEGER NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS network_write_authorizations_v1 (
                    authorization_id TEXT PRIMARY KEY, lease_id TEXT, owner_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL, resource_scope TEXT NOT NULL, operation TEXT NOT NULL,
                    trace_id TEXT NOT NULL, supplied_fencing_token INTEGER,
                    active_fencing_token INTEGER, authorized INTEGER NOT NULL,
                    write_executed INTEGER NOT NULL, reason TEXT NOT NULL, checked_at TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_network_write_authorizations_scope
                    ON network_write_authorizations_v1(resource_scope, checked_at);
            """)
            columns = {row[1] for row in c.execute("PRAGMA table_info(network_configuration_write_leases_v1)")}
            if "resource_scope" not in columns:
                c.execute("ALTER TABLE network_configuration_write_leases_v1 ADD COLUMN resource_scope TEXT NOT NULL DEFAULT 'GLOBAL_NETWORK_CONFIGURATION'")

    def acquire(self, *, owner_type: str, owner_id: str, recovery_id: str,
                resource_scope: str = GLOBAL_NETWORK_CONFIGURATION,
                ttl_seconds: int = 120) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(seconds=max(int(ttl_seconds), 1))
        with self.store._lock, self.store._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                # Global recovery conflicts with every resource; a resource
                # lease conflicts only with that resource.  The decision is
                # made in this SQLite transaction, rather than through an
                # in-process flag, so calibrator, resident and recovery share
                # one fencing authority.
                active = c.execute("""SELECT * FROM network_configuration_write_leases_v1
                    WHERE status='ACTIVE' AND expires_at>?
                      AND (resource_scope=? OR resource_scope=? OR ?=?)
                    ORDER BY fencing_token DESC LIMIT 1""",
                    (now.isoformat(), resource_scope, GLOBAL_NETWORK_CONFIGURATION,
                     resource_scope, GLOBAL_NETWORK_CONFIGURATION),
                ).fetchone()
                # Never supersede a live writer.  Supersession invalidated an
                # in-flight calibration token and permitted recovery writes
                # between its samples.  Expiry remains the bounded takeover
                # mechanism for a crashed writer.
                if active:
                    c.rollback()
                    if active["owner_type"] == "CAPACITY_CALIBRATION":
                        raise RuntimeError("NETWORK_WRITE_LEASE_HELD_BY_CAPACITY_CALIBRATION")
                    if active["owner_type"] == "RESTART_RECOVERY":
                        raise RuntimeError("NETWORK_WRITE_LEASE_HELD_BY_RECOVERY")
                    if active["owner_type"] == owner_type and active["owner_id"] == owner_id:
                        raise RuntimeError("NETWORK_WRITE_LEASE_HELD_BY_OWNER")
                    raise RuntimeError("NETWORK_WRITE_LEASE_HELD_BY_ACTIVE_WRITER")
                row = c.execute("SELECT last_token FROM network_write_fencing_sequences_v1 WHERE scope=?", (resource_scope,)).fetchone()
                token = int(row["last_token"] if row else 0) + 1
                c.execute("""INSERT INTO network_write_fencing_sequences_v1(scope,last_token,updated_at)
                    VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET last_token=excluded.last_token,updated_at=excluded.updated_at""", (resource_scope, token, now.isoformat()))
                lease_id = "NWL-" + uuid.uuid4().hex[:16]
                c.execute("""INSERT INTO network_configuration_write_leases_v1
                    (lease_id,owner_type,owner_id,recovery_id,fencing_token,acquired_at,expires_at,status,resource_scope)
                    VALUES (?,?,?,?,?,?,?,?,?)""", (lease_id, owner_type, owner_id, recovery_id, token, now.isoformat(), expiry.isoformat(), "ACTIVE", resource_scope))
                c.commit()
            except Exception:
                c.rollback()
                raise
        return {"lease_id": lease_id, "owner_type": owner_type, "owner_id": owner_id,
                "recovery_id": recovery_id, "resource_scope": resource_scope,
                "fencing_token": token, "acquired_at": now.isoformat(),
                "expires_at": expiry.isoformat(), "status": "ACTIVE"}

    def release(self, lease_id: str) -> bool:
        with self.store._lock, self.store._connect() as c:
            return bool(c.execute("UPDATE network_configuration_write_leases_v1 SET status='RELEASED', renewed_at=? WHERE lease_id=? AND status='ACTIVE'", (_now(), lease_id)).rowcount)

    def active(self, resource_scope: str = GLOBAL_NETWORK_CONFIGURATION) -> dict[str, Any] | None:
        now = _now()
        with self.store._lock, self.store._connect() as c:
            c.execute("UPDATE network_configuration_write_leases_v1 SET status='EXPIRED' WHERE status='ACTIVE' AND expires_at<=?", (now,))
            row = c.execute("""SELECT * FROM network_configuration_write_leases_v1
                WHERE status='ACTIVE' AND (resource_scope=? OR resource_scope=? OR ?=?)
                ORDER BY fencing_token DESC LIMIT 1""",
                (resource_scope, GLOBAL_NETWORK_CONFIGURATION, resource_scope,
                 GLOBAL_NETWORK_CONFIGURATION),
            ).fetchone()
        return dict(row) if row else None

    def authorize_network_configuration_write(self, *, owner_type: str, owner_id: str,
                                              fencing_token: int | None,
                                              resource_scope: str,
                                              operation: str, trace_id: str,
                                              write_executed: bool = False) -> dict[str, Any]:
        active = self.active(resource_scope)
        result: dict[str, Any] = {"authorized": False, "active_lease_id": active and active["lease_id"],
            "active_owner_type": active and active["owner_type"], "active_owner_id": active and active["owner_id"],
            "expected_fencing_token": active and active["fencing_token"], "supplied_fencing_token": fencing_token,
            "token_match": False, "owner_match": False, "scope_match": bool(resource_scope),
            "lease_active": active is not None, "reason": "NETWORK_WRITE_LEASE_REQUIRED", "checked_at": _now()}
        if not active:
            result["reason"] = "NETWORK_WRITE_LEASE_NOT_ACTIVE"
        elif fencing_token is None:
            result["reason"] = "NETWORK_WRITE_FENCING_TOKEN_MISSING"
        else:
            result["token_match"] = int(fencing_token) == int(active["fencing_token"])
            result["owner_match"] = owner_type == active["owner_type"] and owner_id == active["owner_id"]
            if not result["token_match"]:
                result["reason"] = "NETWORK_WRITE_FENCING_TOKEN_STALE"
            elif not result["owner_match"]:
                result["reason"] = "NETWORK_WRITE_OWNER_MISMATCH"
            else:
                result.update({"authorized": True, "reason": "AUTHORIZED"})
        result["authorization_id"] = "NWA-" + uuid.uuid4().hex[:16]
        with self.store._lock, self.store._connect() as c:
            c.execute("""INSERT INTO network_write_authorizations_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result["authorization_id"], result["active_lease_id"], owner_type, owner_id,
                 resource_scope, operation, trace_id, fencing_token, result["expected_fencing_token"],
                 int(result["authorized"]), int(write_executed), result["reason"], result["checked_at"], json.dumps(result, sort_keys=True, default=str)))
        return result

    def record_execution(self, authorization: dict[str, Any], *, write_executed: bool) -> None:
        """Finalize an authorization audit after the southbound operation returns."""
        authorization_id = authorization.get("authorization_id")
        if not authorization_id:
            return
        with self.store._lock, self.store._connect() as c:
            c.execute("UPDATE network_write_authorizations_v1 SET write_executed=? WHERE authorization_id=?",
                      (int(bool(write_executed)), authorization_id))

    def resident_state(self, resource_scope: str = GLOBAL_NETWORK_CONFIGURATION) -> dict[str, Any]:
        active = self.active(resource_scope)
        if active and active["owner_type"] == "CAPACITY_CALIBRATION":
            status = "SKIPPED_RESOURCE_BUSY"
        elif active and active["owner_type"] == "RESTART_RECOVERY":
            status = "DEFERRED_BY_RECOVERY"
        else:
            status = "ACTIVE"
        return {"status": status, "active_lease": active, "resource_scope": resource_scope}


def write_context(*, owner_type: str, owner_id: str, fencing_token: int, resource_scope: str = GLOBAL_NETWORK_CONFIGURATION, trace_id: str | None = None):
    """Context manager for a service-to-actuator write authorization."""
    class _Context:
        def __enter__(self):
            self.token = _write_context.set({"owner_type": owner_type, "owner_id": owner_id,
                "fencing_token": fencing_token, "resource_scope": resource_scope,
                "trace_id": trace_id or "NWT-" + uuid.uuid4().hex[:16]})
            return _write_context.get()
        def __exit__(self, *_exc):
            _write_context.reset(self.token)
    return _Context()


def current_write_context() -> dict[str, Any] | None:
    """Return the active in-process write identity without authorizing a write."""
    context = _write_context.get()
    return dict(context) if context else None


def require_network_write(*, operation: str, store: IntentStore | None = None) -> dict[str, Any]:
    context = _write_context.get()
    if not context:
        return NetworkWriteFencing(store).authorize_network_configuration_write(
            owner_type="UNSPECIFIED", owner_id="UNSPECIFIED", fencing_token=None,
            resource_scope=GLOBAL_NETWORK_CONFIGURATION, operation=operation, trace_id="MISSING_CONTEXT")
    return NetworkWriteFencing(store).authorize_network_configuration_write(
        owner_type=context["owner_type"], owner_id=context["owner_id"], fencing_token=context["fencing_token"],
        resource_scope=context["resource_scope"], operation=operation, trace_id=context["trace_id"])

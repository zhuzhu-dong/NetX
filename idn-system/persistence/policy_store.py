"""Versioned desired-state store for active network policies.

There is one active row per exact traffic scope.  QoS changes are staged as
``ACTIVE -> UPDATING -> ACTIVE`` and committed only after the data-plane
configuration can be read back.  The old desired state remains available for
rollback throughout the update.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from models.policy_update import action_type_value, traffic_scope_key

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> str:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return json.dumps(payload, ensure_ascii=False, default=str)


class PolicyStore:
    """Transactional policy upsert store consumed by conflict detection."""

    def __init__(self, store=None, *, read_only: bool = False):
        from api.store import get_store

        self._store = store or get_store()
        # Query projections must not turn a dashboard refresh into a database
        # migration.  Mutating control-plane callers retain the historical
        # default migration behaviour.
        if not read_only:
            self._backfill_scope_keys()

    def _backfill_scope_keys(self) -> None:
        """Migrate pre-upsert rows and hide older duplicates per scope."""
        try:
            with self._store._lock, self._store._connect() as conn:
                rows = conn.execute(
                    "SELECT policy_id, yang_json, traffic_scope_key, status, updated_at FROM policies"
                ).fetchall()
                for row in rows:
                    payload = json.loads(row["yang_json"])
                    network_payload = payload.get("network_policy") or payload
                    if network_payload.get("policy_id") != row["policy_id"]:
                        network_payload["policy_id"] = row["policy_id"]
                        conn.execute(
                            "UPDATE policies SET yang_json=? WHERE policy_id=?",
                            (_dump(payload), row["policy_id"]),
                        )
                    key = traffic_scope_key(network_payload).key
                    if row["traffic_scope_key"] != key:
                        conn.execute(
                            "UPDATE policies SET traffic_scope_key=? WHERE policy_id=?",
                            (key, row["policy_id"]),
                        )
                    # Keep the audit chain addressable through the current
                    # canonical scope key as the key format evolves.  History
                    # rows retain their old/new JSON snapshots, so changing
                    # this lookup key does not rewrite the audited policy.
                    conn.execute(
                        """UPDATE policy_history SET traffic_scope_key=?
                           WHERE policy_id=? AND traffic_scope_key<>?""",
                        (key, row["policy_id"], key),
                    )
                duplicates = conn.execute(
                    """SELECT traffic_scope_key FROM policies WHERE status='ACTIVE'
                       AND traffic_scope_key IS NOT NULL GROUP BY traffic_scope_key HAVING COUNT(*) > 1"""
                ).fetchall()
                for duplicate in duplicates:
                    active_rows = conn.execute(
                        """SELECT policy_id FROM policies WHERE traffic_scope_key=? AND status='ACTIVE'
                           ORDER BY updated_at DESC""",
                        (duplicate["traffic_scope_key"],),
                    ).fetchall()
                    for older in active_rows[1:]:
                        conn.execute(
                            "UPDATE policies SET status='SUPERSEDED' WHERE policy_id=?",
                            (older["policy_id"],),
                        )
        except Exception as exc:
            logger.warning("[PolicyStore] scope-key backfill failed: %s", exc)

    def get_by_scope(self, scope_key: str) -> dict[str, Any] | None:
        with self._store._lock, self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM policies WHERE traffic_scope_key = ? ORDER BY updated_at DESC LIMIT 1",
                (scope_key,),
            ).fetchone()
        return dict(row) if row else None

    def resolve_managed_desired_policies(self) -> dict[str, dict[str, Any]]:
        """Return the one durable, committed desired owner for each scope.

        ``ACTIVE`` is an assurance state as well as a lifecycle state: a
        readback failure may make a fully committed policy ``DEGRADED`` or
        ``RECONCILING`` without withdrawing its desired configuration.  A
        staged transaction, on the other hand, owns the writer until it is
        committed or rolled back, so its old document must not be replayed by
        a resident process.  The active ResourceBinding is the tie breaker for
        legacy duplicate rows, but never the source of a desired document.

        This is deliberately the sole managed-desired selector used by the
        resident reconciler and restart egress readback.  It prevents either
        consumer from selecting a different historical generation.
        """
        with self._store._lock, self._store._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM policies
                   WHERE status IN ('ACTIVE','DEGRADED','RECONCILING')
                     AND pending_yang_json IS NULL
                   ORDER BY
                     CASE WHEN EXISTS (
                         SELECT 1 FROM qos_resource_bindings binding
                         WHERE binding.policy_id = policies.policy_id
                           AND binding.traffic_scope_key = policies.traffic_scope_key
                           AND binding.active = 1
                     ) THEN 0 ELSE 1 END,
                     updated_at DESC, version DESC, policy_id DESC"""
            ).fetchall()
        managed: dict[str, dict[str, Any]] = {}
        for row in rows:
            record = dict(row)
            scope = str(record.get("traffic_scope_key") or "")
            if scope and scope not in managed:
                managed[scope] = record
        return managed

    def resolve_managed_desired_for_scope(self, scope_key: str) -> dict[str, Any] | None:
        """Return the authoritative committed owner for one resource scope."""
        return self.resolve_managed_desired_policies().get(scope_key)

    def stage(self, yang_policy: Any, intent_id: str, decision: Any = None) -> dict[str, Any]:
        """Stage CREATE/MODIFY while retaining the current active JSON."""
        policy = getattr(yang_policy, "network_policy", yang_policy)
        incoming_policy_id = str(getattr(policy, "policy_id", "") or "")
        scope = traffic_scope_key(policy).key
        action_type = action_type_value(policy)
        pending = _dump(yang_policy)
        now = _utc_now()
        decision_data = (
            decision.model_dump(mode="json") if hasattr(decision, "model_dump")
            else dict(decision or {})
        )
        operation = decision_data.get("execution_mode") or "CREATE"

        with self._store._lock, self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM policies WHERE traffic_scope_key = ? ORDER BY updated_at DESC LIMIT 1",
                (scope,),
            ).fetchone()
            if row:
                record = dict(row)
                if operation == "VERIFY_EXISTING":
                    # A prior interrupted generation can leave the durable row
                    # in UPDATING.  Reusing its classifier is safe only if the
                    # *current* intent becomes the pending lineage that will be
                    # committed after fresh readback; otherwise an older intent
                    # silently becomes the owner of this execution.
                    if record.get("status") == "UPDATING":
                        conn.execute(
                            """UPDATE policies SET pending_yang_json=?, pending_intent_id=?,
                               pending_operation=?, audit_json=?, error_message=NULL,
                               updated_at=? WHERE policy_id=?""",
                            (pending, intent_id, operation, _dump(decision_data), now, record["policy_id"]),
                        )
                        record = dict(conn.execute(
                            "SELECT * FROM policies WHERE policy_id=?", (record["policy_id"],)
                        ).fetchone())
                    return record
                conn.execute(
                    """UPDATE policies SET status='UPDATING', pending_yang_json=?,
                       pending_intent_id=?, pending_operation=?, audit_json=?,
                       error_message=NULL, updated_at=? WHERE policy_id=?""",
                    (pending, intent_id, operation, _dump(decision_data), now, record["policy_id"]),
                )
                conn.execute(
                    """INSERT INTO policy_history(policy_id, traffic_scope_key, intent_id,
                       version, event_type, old_yang_json, new_yang_json, runtime_json, created_at)
                       VALUES (?, ?, ?, ?, 'UPDATE_STARTED', ?, ?, ?, ?)""",
                    (
                        record["policy_id"], scope, intent_id, int(record.get("version") or 1),
                        record["yang_json"], pending, record.get("runtime_json"), now,
                    ),
                )
                return {**record, "status": "UPDATING", "pending_yang_json": pending,
                        "pending_intent_id": intent_id, "pending_operation": operation}

            conn.execute(
                """INSERT INTO policies(policy_id, intent_id, action_type, yang_json,
                   created_at, updated_at, traffic_scope_key, status, version,
                   pending_yang_json, pending_intent_id, pending_operation, audit_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'UPDATING', 0, ?, ?, ?, ?)""",
                (
                    incoming_policy_id, intent_id, action_type, pending, now, now, scope,
                    pending, intent_id, "CREATE", _dump(decision_data),
                ),
            )
            return {
                "policy_id": incoming_policy_id,
                "intent_id": intent_id,
                "action_type": action_type,
                "traffic_scope_key": scope,
                "status": "UPDATING",
                "version": 0,
                "pending_yang_json": pending,
                "pending_intent_id": intent_id,
                "pending_operation": "CREATE",
                "yang_json": pending,
            }

    def commit_staged(
        self,
        scope_key: str,
        *,
        intent_id: str,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        with self._store._lock, self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM policies WHERE traffic_scope_key=? ORDER BY updated_at DESC LIMIT 1",
                (scope_key,),
            ).fetchone()
            if not row:
                return None
            record = dict(row)
            if record.get("status") != "UPDATING":
                runtime_json = _dump(runtime or {})
                conn.execute(
                    """INSERT INTO policy_history(policy_id, traffic_scope_key, intent_id,
                       version, event_type, old_yang_json, new_yang_json, runtime_json, created_at)
                       VALUES (?, ?, ?, ?, 'EXISTING_CONFIG_VERIFIED', ?, ?, ?, ?)""",
                    (
                        record["policy_id"], scope_key, intent_id,
                        int(record.get("version") or 1), record.get("yang_json"),
                        record.get("yang_json"), runtime_json, now,
                    ),
                )
                conn.execute(
                    "UPDATE policies SET runtime_json=?, updated_at=? WHERE policy_id=?",
                    (runtime_json, now, record["policy_id"]),
                )
                return record
            new_json = record.get("pending_yang_json") or record["yang_json"]
            new_version = max(int(record.get("version") or 0) + 1, 1)
            new_payload = json.loads(new_json)
            network_payload = new_payload.get("network_policy") or new_payload
            old_payload = json.loads(record["yang_json"])
            old_network_policy = old_payload.get("network_policy") or old_payload
            old_qos = ((old_network_policy.get("action") or {}).get("qos_params") or {})
            new_qos = ((network_payload.get("action") or {}).get("qos_params") or {})
            # Preference-only updates may deliberately omit the quantitative
            # QoS target.  Omitting it is not a request to erase a committed
            # SLA: retain the durable target (and optional burst) so a later
            # exact request remains VERIFY_EXISTING rather than a spurious
            # parameter update with a TC rewrite.
            for field in ("bandwidth", "burst_size"):
                if new_qos.get(field) is None and old_qos.get(field) is not None:
                    new_qos[field] = old_qos[field]
            # The row's policy_id is the stable identity of this traffic
            # scope.  Incoming YANG drafts carry random IDs and must not leak
            # into the committed active document during an in-place update.
            network_payload["policy_id"] = record["policy_id"]
            new_json = _dump(new_payload)
            action = network_payload.get("action") or {}
            action_type = str(action.get("action_type") or record["action_type"])
            runtime_json = _dump(runtime or {})
            conn.execute(
                """UPDATE policies SET intent_id=?, action_type=?, yang_json=?, status='ACTIVE',
                   version=?, pending_yang_json=NULL, pending_intent_id=NULL,
                   pending_operation=NULL, runtime_json=?, error_message=NULL, updated_at=?
                   WHERE policy_id=?""",
                (intent_id, action_type, new_json, new_version, runtime_json, now, record["policy_id"]),
            )
            conn.execute(
                """INSERT INTO policy_history(policy_id, traffic_scope_key, intent_id,
                   version, event_type, old_yang_json, new_yang_json, runtime_json, created_at)
                   VALUES (?, ?, ?, ?, 'CONFIG_VERIFIED_COMMIT', ?, ?, ?, ?)""",
                (
                    record["policy_id"], scope_key, intent_id, new_version,
                    record["yang_json"], new_json, runtime_json, now,
                ),
            )
        self._invalidate_snapshot_cache()
        return self.get_by_scope(scope_key)

    def recover_managed_desired_from_artifact(
        self,
        yang_policy: Any,
        *,
        intent_id: str,
        execution_runtime: dict[str, Any],
    ) -> dict[str, Any]:
        """Restore a missing desired owner from a durable pre-write artifact.

        This is deliberately not an observed-state import.  Callers must supply
        the persisted YANG policy from the original Intent/Plan and its durable
        execution lineage.  It is safe only when no current policy already owns
        the traffic scope; normal reconciliation then owns any data-plane repair.
        """
        policy = getattr(yang_policy, "network_policy", yang_policy)
        scope = traffic_scope_key(policy).key
        existing = self.get_by_scope(scope)
        if existing is not None:
            raise RuntimeError(
                f"DURABLE_DESIRED_RECOVERY_CONFLICT: scope already has policy "
                f"{existing.get('policy_id')}"
            )
        staged = self.stage(yang_policy, intent_id, {
            "execution_mode": "RECOVERY_MANAGE",
            "desired_source": "DURABLE_INTENT_ARTIFACT",
        })
        committed = self.commit_staged(
            scope, intent_id=intent_id, runtime=dict(execution_runtime or {}),
        )
        if committed is None or committed.get("status") != "ACTIVE":
            raise RuntimeError("DURABLE_DESIRED_RECOVERY_COMMIT_FAILED")
        return committed

    def rollback_staged(self, scope_key: str, *, error: str = "") -> dict[str, Any] | None:
        now = _utc_now()
        with self._store._lock, self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM policies WHERE traffic_scope_key=? ORDER BY updated_at DESC LIMIT 1",
                (scope_key,),
            ).fetchone()
            if not row:
                return None
            record = dict(row)
            # Observation health is orthogonal to stage ownership.  A
            # controller readback can mark an in-flight stage DEGRADED before
            # its worker crashes; stale recovery must still clear that stage
            # and restore the committed document to ACTIVE.
            if not record.get("pending_yang_json"):
                return record
            if int(record.get("version") or 0) == 0:
                conn.execute("DELETE FROM policies WHERE policy_id=?", (record["policy_id"],))
            else:
                conn.execute(
                    """UPDATE policies SET status='ACTIVE', pending_yang_json=NULL,
                       pending_intent_id=NULL, pending_operation=NULL, error_message=?,
                       updated_at=? WHERE policy_id=?""",
                    (error, now, record["policy_id"]),
                )
            conn.execute(
                """INSERT INTO policy_history(policy_id, traffic_scope_key, intent_id,
                   version, event_type, old_yang_json, new_yang_json, runtime_json, created_at)
                   VALUES (?, ?, ?, ?, 'UPDATE_ROLLED_BACK', ?, ?, ?, ?)""",
                (
                    record["policy_id"], scope_key, record.get("pending_intent_id"),
                    int(record.get("version") or 0), record.get("yang_json"),
                    record.get("pending_yang_json"), record.get("runtime_json"), now,
                ),
            )
        self._invalidate_snapshot_cache()
        return self.get_by_scope(scope_key)

    def withdraw_committed_scope(
        self, scope_key: str, *, expected_policy_id: str, expected_version: int,
        intent_id: str, runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Terminally retire one committed desired scope without deleting audit.

        The caller must have completed southbound cleanup under the formal
        scope lease.  The compare-and-set prevents a stale Dashboard card from
        withdrawing a newer in-place policy revision.
        """
        now = _utc_now()
        with self._store._lock, self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM policies WHERE traffic_scope_key=? ORDER BY updated_at DESC LIMIT 1",
                (scope_key,),
            ).fetchone()
            if row is None:
                raise KeyError(scope_key)
            record = dict(row)
            if record["policy_id"] != expected_policy_id or int(record.get("version") or 0) != int(expected_version):
                raise RuntimeError("CURRENT_OWNER_CHANGED")
            if record.get("status") == "WITHDRAWN":
                return record
            if record.get("status") not in {"ACTIVE", "DEGRADED", "RECONCILING"} or record.get("pending_yang_json"):
                raise RuntimeError("CURRENT_OWNER_CHANGED")
            runtime_json = _dump(runtime or {})
            conn.execute(
                """UPDATE policies SET status='WITHDRAWN', pending_yang_json=NULL,
                   pending_intent_id=NULL, pending_operation=NULL, runtime_json=?,
                   error_message=NULL, updated_at=? WHERE policy_id=?""",
                (runtime_json, now, expected_policy_id),
            )
            conn.execute(
                """INSERT INTO policy_history(policy_id, traffic_scope_key, intent_id,
                   version, event_type, old_yang_json, new_yang_json, runtime_json, created_at)
                   VALUES (?, ?, ?, ?, 'WITHDRAWN', ?, ?, ?, ?)""",
                (expected_policy_id, scope_key, intent_id, int(expected_version),
                 record["yang_json"], record["yang_json"], runtime_json, now),
            )
        self._invalidate_snapshot_cache()
        return {**record, "status": "WITHDRAWN", "updated_at": now}

    def recover_stale_pending(self, *, timeout_seconds: float | None = None) -> list[dict[str, Any]]:
        """Resolve crashed update stages instead of excluding them forever.

        While an update is genuinely in flight the resident reconciler must
        not replay the old committed document.  That safety hold is bounded:
        after the durable timeout, rollback returns the scope to its last
        committed policy (or deterministically removes an uncommitted create)
        and clears the pending lineage.  The caller records the resulting
        audit event as part of its formal recovery run.
        """
        timeout = float(
            timeout_seconds if timeout_seconds is not None
            else os.getenv("IDN_POLICY_PENDING_TIMEOUT_SECONDS", "300")
        )
        now = datetime.now(timezone.utc)
        candidates: list[dict[str, Any]] = []
        with self._store._lock, self._store._connect() as conn:
            rows = conn.execute(
                """SELECT policy_id, traffic_scope_key, pending_intent_id, updated_at
                   FROM policies
                   WHERE pending_yang_json IS NOT NULL
                     AND status IN ('UPDATING', 'ACTIVE', 'DEGRADED', 'RECONCILING')"""
            ).fetchall()
        for row in rows:
            try:
                staged_at = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00"))
                age_seconds = (now - staged_at).total_seconds()
            except (TypeError, ValueError):
                age_seconds = timeout
            # A terminal workflow can no longer own a pending mutation.  Do
            # not leave its former committed desired state invisible until a
            # wall-clock timeout merely because an earlier compound child
            # finished without reaching the commit/rollback transition.
            intent = self._store.get(str(row["pending_intent_id"] or "")) if row["pending_intent_id"] else None
            terminal = str((intent or {}).get("status") or "").lower() in {
                "success", "failed", "unsatisfiable", "pending_clarification", "cancelled", "withdrawn",
            }
            if age_seconds < timeout and not terminal:
                continue
            rolled_back = self.rollback_staged(
                str(row["traffic_scope_key"]),
                error="PENDING_UPDATE_TERMINAL_ROLLBACK" if terminal else "PENDING_UPDATE_TIMEOUT_ROLLBACK",
            )
            candidates.append({
                "policy_id": str(row["policy_id"]),
                "traffic_scope_key": str(row["traffic_scope_key"]),
                "pending_intent_id": row["pending_intent_id"],
                "age_seconds": age_seconds,
                "terminal_workflow": terminal,
                "result": "ROLLED_BACK" if rolled_back is not None else "WITHDRAWN_UNCOMMITTED_CREATE",
            })
        return candidates

    def save(self, yang_policy: Any, intent_id: str) -> dict[str, Any] | None:
        """Backward-compatible immediate CREATE/upsert for callers without staging."""
        try:
            staged = self.stage(yang_policy, intent_id, {"execution_mode": "CREATE"})
            return self.commit_staged(
                staged["traffic_scope_key"], intent_id=intent_id, runtime={}
            )
        except Exception as exc:
            logger.warning("[PolicyStore] Save failed (non-fatal): %s", exc)
            return None

    def list_all(self) -> list[dict[str, Any]]:
        try:
            with self._store._lock, self._store._connect() as conn:
                # ``UPDATING`` keeps the last known-good desired document in
                # ``yang_json`` while a replacement is being verified.  A
                # ``DEGRADED`` or ``RECONCILING`` row is likewise still the
                # committed owner of its traffic scope: those values describe
                # observed health, not withdrawal.  Hiding any of these states
                # from sensing makes a retry classify the same five-tuple as a
                # CREATE and can cause needless P4/TC writes.  Expose the
                # committed side of the transaction so conflict checking can
                # select VERIFY_EXISTING / in-place QoS update.  Pending JSON
                # deliberately remains private until execution readback commits
                # it.
                rows = conn.execute(
                    "SELECT * FROM policies WHERE status IN "
                    "('ACTIVE', 'UPDATING', 'DEGRADED', 'RECONCILING') "
                    "ORDER BY updated_at DESC"
                ).fetchall()
        except Exception as exc:
            logger.warning("[PolicyStore] List failed: %s", exc)
            return []

        policies: list[dict[str, Any]] = []
        for row in rows:
            try:
                record = dict(row)
                item = json.loads(record["yang_json"])
                net_policy = item.get("network_policy") or item
                net_policy["_store"] = {
                    "status": record.get("status"),
                    "version": record.get("version"),
                    "traffic_scope_key": record.get("traffic_scope_key"),
                    "runtime": json.loads(record.get("runtime_json") or "{}"),
                    "intent_id": record.get("intent_id"),
                    "pending_intent_id": record.get("pending_intent_id"),
                    "pending_operation": record.get("pending_operation"),
                }
                policies.append(net_policy)
            except Exception as exc:
                logger.warning("[PolicyStore] Failed to deserialize policy: %s", exc)
        return policies

    def history(self, scope_key: str) -> list[dict[str, Any]]:
        with self._store._lock, self._store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM policy_history WHERE traffic_scope_key=? ORDER BY history_id",
                (scope_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def preference_owner_candidates(self, preference_intent_id: str) -> list[dict[str, Any]]:
        """Find the committed owner immediately preceding a preference update.

        Preference intents may update the desired QoS document of an already
        active traffic scope without creating a new TC/P4 resource.  Their
        withdrawal must therefore restore the predecessor's ownership rather
        than treating the preference row as a scheduler-only relation.  The
        immutable policy-history chain is the authority for that predecessor;
        display text and service names are intentionally not consulted.
        """
        with self._store._lock, self._store._connect() as conn:
            rows = conn.execute(
                """SELECT policy_id, traffic_scope_key, intent_id, version
                   FROM policies
                   WHERE intent_id=?
                     AND status IN ('ACTIVE','DEGRADED','RECONCILING')""",
                (preference_intent_id,),
            ).fetchall()
            candidates: list[dict[str, Any]] = []
            for row in rows:
                predecessor = conn.execute(
                    """SELECT intent_id, version, history_id
                       FROM policy_history
                       WHERE policy_id=?
                         AND event_type='CONFIG_VERIFIED_COMMIT'
                         AND history_id < (
                             SELECT MIN(history_id) FROM policy_history
                             WHERE policy_id=? AND intent_id=?
                               AND event_type='UPDATE_STARTED'
                         )
                       ORDER BY history_id DESC LIMIT 1""",
                    (row["policy_id"], row["policy_id"], preference_intent_id),
                ).fetchone()
                candidates.append({
                    "policy_id": str(row["policy_id"]),
                    "traffic_scope_key": str(row["traffic_scope_key"]),
                    "current_owner_intent_id": str(row["intent_id"]),
                    "current_version": int(row["version"] or 0),
                    "predecessor_intent_id": (
                        str(predecessor["intent_id"]) if predecessor else None
                    ),
                    "predecessor_version": (
                        int(predecessor["version"] or 0) if predecessor else None
                    ),
                    "predecessor_history_id": (
                        int(predecessor["history_id"]) if predecessor else None
                    ),
                })
        return candidates

    def restore_preference_ownership(
        self,
        preference_intent_id: str,
        *,
        expected_owners: dict[str, str],
        reason: str = "preference withdrawal restored predecessor policy ownership",
    ) -> list[dict[str, Any]]:
        """Atomically restore live policy ownership after preference withdrawal.

        This is a control-plane reconciliation only: stable policy IDs,
        bindings, reservations, P4 entries, ONOS flows and TC classes remain
        unchanged.  Compare-and-set ownership protects a concurrent QoS
        update from being silently reassigned.
        """
        now = _utc_now()
        restored: list[dict[str, Any]] = []
        with self._store._lock, self._store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for policy_id, predecessor_intent_id in expected_owners.items():
                    row = conn.execute(
                        "SELECT * FROM policies WHERE policy_id=?", (policy_id,)
                    ).fetchone()
                    if row is None:
                        raise RuntimeError("PREFERENCE_OWNER_POLICY_MISSING")
                    record = dict(row)
                    if (
                        record.get("intent_id") != preference_intent_id
                        or record.get("status") not in {"ACTIVE", "DEGRADED", "RECONCILING"}
                        or record.get("pending_yang_json")
                    ):
                        raise RuntimeError("PREFERENCE_OWNER_CHANGED")
                    binding_rows = conn.execute(
                        """SELECT binding_id FROM qos_resource_bindings
                           WHERE policy_id=? AND traffic_scope_key=? AND active=1""",
                        (policy_id, record["traffic_scope_key"]),
                    ).fetchall()
                    conn.execute(
                        "UPDATE policies SET intent_id=?, updated_at=? WHERE policy_id=?",
                        (predecessor_intent_id, now, policy_id),
                    )
                    runtime = {
                        "event": "PREFERENCE_OWNER_RESTORED",
                        "former_owner_intent_id": preference_intent_id,
                        "restored_owner_intent_id": predecessor_intent_id,
                        "reason": reason,
                        "binding_ids": [str(item["binding_id"]) for item in binding_rows],
                    }
                    conn.execute(
                        """INSERT INTO policy_history(policy_id, traffic_scope_key, intent_id,
                           version, event_type, old_yang_json, new_yang_json, runtime_json, created_at)
                           VALUES (?, ?, ?, ?, 'PREFERENCE_OWNER_RESTORED', ?, ?, ?, ?)""",
                        (
                            policy_id, record["traffic_scope_key"], predecessor_intent_id,
                            int(record.get("version") or 0), record["yang_json"],
                            record["yang_json"], _dump(runtime), now,
                        ),
                    )
                    restored.append({
                        "policy_id": policy_id,
                        "traffic_scope_key": str(record["traffic_scope_key"]),
                        "from_intent_id": preference_intent_id,
                        "to_intent_id": predecessor_intent_id,
                        "binding_ids": runtime["binding_ids"],
                    })
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if restored:
            self._invalidate_snapshot_cache()
        return restored

    def count(self) -> int:
        try:
            with self._store._lock, self._store._connect() as conn:
                row = conn.execute("SELECT COUNT(*) FROM policies WHERE status='ACTIVE'").fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def delete_by_intent(self, intent_id: str) -> int:
        if not intent_id:
            return 0
        try:
            with self._store._lock, self._store._connect() as conn:
                pending = conn.execute(
                    "SELECT traffic_scope_key FROM policies WHERE pending_intent_id=? AND status='UPDATING'",
                    (intent_id,),
                ).fetchall()
            for row in pending:
                self.rollback_staged(row["traffic_scope_key"], error="intent compensation")
            with self._store._lock, self._store._connect() as conn:
                cursor = conn.execute("DELETE FROM policies WHERE intent_id=?", (intent_id,))
                deleted = max(int(cursor.rowcount or 0), 0)
            if deleted:
                self._invalidate_snapshot_cache()
            return deleted
        except Exception as exc:
            logger.warning("[PolicyStore] Delete failed for intent=%s: %s", intent_id, exc)
            return 0

    @staticmethod
    def _invalidate_snapshot_cache() -> None:
        try:
            from agents.network_sensing import invalidate_snapshot_cache

            invalidate_snapshot_cache()
        except Exception:
            pass

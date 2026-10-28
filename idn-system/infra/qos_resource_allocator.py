"""Collision-free persistent TC/P4-related resource allocation."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from api.store import IntentStore, get_store
from models.resources import ResourceBinding


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QoSResourceAllocator:
    """Allocate explicit identifiers under a SQLite write transaction.

    Identifiers are globally unique among active bindings.  This is stricter
    than TC requires (TC only needs per-interface uniqueness) and therefore
    remains safe before the final egress interface is observed from Mininet.
    """

    def __init__(self, store: IntentStore | None = None):
        self.store = store or get_store()

    @staticmethod
    def _record(row) -> ResourceBinding:
        return ResourceBinding(
            binding_id=row["binding_id"], policy_id=row["policy_id"],
            service_id=row["service_id"], traffic_scope_key=row["traffic_scope_key"],
            resource_key=row["resource_key"], device_id=row["device_id"],
            interface_id=row["interface_id"], class_id=row["class_id"],
            filter_id=row["filter_id"], handle=row["handle"], dscp=row["dscp"],
            rate_mbps=row["rate_mbps"], ceil_mbps=row["ceil_mbps"],
            reservation_kind=row["reservation_kind"], tenant_id=row["tenant_id"],
            created_at=row["created_at"], active=bool(row["active"]),
            runtime=json.loads(row["runtime_json"] or "{}"),
        )

    def allocate(
        self, *, policy_id: str, traffic_scope_key: str, rate_mbps: float,
        ceil_mbps: float, service_id: str | None = None, dscp: int | None = None,
        reservation_kind: str = "GUARANTEED", tenant_id: str = "default",
    ) -> ResourceBinding:
        now = _now()
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM qos_resource_bindings WHERE traffic_scope_key=? AND active=1",
                (traffic_scope_key,),
            ).fetchone()
            if existing:
                connection.execute(
                    """UPDATE qos_resource_bindings SET policy_id=?, service_id=?, dscp=?,
                    rate_mbps=?, ceil_mbps=?, reservation_kind=?, tenant_id=?, updated_at=?
                    WHERE binding_id=?""",
                    (policy_id, service_id, dscp, rate_mbps, ceil_mbps,
                     reservation_kind, tenant_id, now, existing["binding_id"]),
                )
                row = connection.execute(
                    "SELECT * FROM qos_resource_bindings WHERE binding_id=?",
                    (existing["binding_id"],),
                ).fetchone()
                return self._record(row)

            used_classes = {
                int(str(row["class_id"]).split(":", 1)[-1], 16)
                for row in connection.execute(
                    "SELECT class_id FROM qos_resource_bindings WHERE active=1"
                ).fetchall()
            }
            used_filters = {
                int(row["filter_id"]) for row in connection.execute(
                    "SELECT filter_id FROM qos_resource_bindings WHERE active=1"
                ).fetchall()
            }
            minor = next((value for value in range(0x100, 0xFE00) if value not in used_classes), None)
            filter_id = next((value for value in range(100, 32000) if value not in used_filters), None)
            if minor is None or filter_id is None:
                raise RuntimeError("QOS_RESOURCE_EXHAUSTED")
            binding_id = f"RB-{uuid.uuid4().hex[:16]}"
            class_id = f"1:{minor:x}"
            connection.execute(
                """INSERT INTO qos_resource_bindings(
                    binding_id, policy_id, service_id, traffic_scope_key, resource_key,
                    device_id, interface_id, class_id, filter_id, handle, dscp,
                    rate_mbps, ceil_mbps, reservation_kind, tenant_id, runtime_json,
                    active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'UNBOUND', NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, '{}', 1, ?, ?)""",
                (binding_id, policy_id, service_id, traffic_scope_key, class_id,
                 filter_id, filter_id, dscp, rate_mbps, ceil_mbps,
                 reservation_kind, tenant_id, now, now),
            )
            row = connection.execute(
                "SELECT * FROM qos_resource_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
        return self._record(row)

    def bind_runtime(self, binding_id: str, runtime: dict[str, Any]) -> ResourceBinding:
        qos = dict(runtime.get("qos_binding") or runtime)
        resource_key = str(qos.get("link_id") or qos.get("interface_name") or "UNBOUND")
        if resource_key != "UNBOUND":
            try:
                from infra.resource_registry import get_resource_registry

                resource_key = get_resource_registry().resolve(resource_key)
            except KeyError:
                pass
        device_id = qos.get("device_id")
        interface_id = qos.get("interface_name")
        now = _now()
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                """UPDATE qos_resource_bindings SET resource_key=?, device_id=?, interface_id=?,
                runtime_json=?, updated_at=? WHERE binding_id=? AND active=1""",
                (resource_key, device_id, interface_id, json.dumps(runtime, default=str), now, binding_id),
            )
            row = connection.execute(
                "SELECT * FROM qos_resource_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
        if row is None:
            raise KeyError(binding_id)
        binding = self._record(row)
        self._upsert_reservation(binding)
        return binding

    def _upsert_reservation(self, binding: ResourceBinding) -> None:
        if binding.resource_key == "UNBOUND":
            return
        now = _now()
        reservation_id = f"RES-{binding.binding_id}"
        with self.store._lock, self.store._connect() as connection:
            connection.execute(
                """INSERT INTO resource_reservations(
                    reservation_id, policy_id, traffic_scope_key, resource_id, tenant_id,
                    reservation_kind, bandwidth_mbps, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(reservation_id) DO UPDATE SET
                    policy_id=excluded.policy_id, resource_id=excluded.resource_id,
                    tenant_id=excluded.tenant_id, reservation_kind=excluded.reservation_kind,
                    bandwidth_mbps=excluded.bandwidth_mbps, active=1, updated_at=excluded.updated_at""",
                (reservation_id, binding.policy_id, binding.traffic_scope_key,
                 binding.resource_key, binding.tenant_id, binding.reservation_kind,
                 binding.rate_mbps, now, now),
            )

    def reconcile_reservation_integrity(self) -> dict[str, Any]:
        """Retire reservations that no longer have an active binding owner.

        Resource reservations are derived accounting records.  Their authority
        is the active ``qos_resource_bindings`` row with the same binding id,
        not an earlier policy revision.  Older interrupted update paths could
        retire a binding without retiring its ``RES-<binding_id>`` reservation;
        that leaves a ghost guaranteed allocation in admission accounting.

        This is deliberately a narrow repair: it never creates capacity, never
        changes an active binding, and never releases a reservation unless its
        owning binding is absent or inactive.  The NetworkReconciler invokes it
        before capacity-sensitive desired/observed reconciliation so the
        correction remains in the formal control-plane path and is auditable.
        """
        now = _now()
        released_ids: list[str] = []
        synchronized_ids: list[str] = []
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """SELECT reservation_id FROM resource_reservations
                       WHERE active=1
                         AND reservation_id LIKE 'RES-RB-%'
                         AND NOT EXISTS (
                           SELECT 1 FROM qos_resource_bindings binding
                           WHERE binding.binding_id=substr(resource_reservations.reservation_id, 5)
                             AND binding.active=1
                         )"""
                ).fetchall()
                released_ids = [str(row["reservation_id"]) for row in rows]
                if released_ids:
                    connection.executemany(
                        "UPDATE resource_reservations SET active=0, updated_at=? WHERE reservation_id=? AND active=1",
                        [(now, reservation_id) for reservation_id in released_ids],
                    )

                # An active binding may have been updated immediately before a
                # process interruption.  Keep its derived reservation exactly
                # aligned with the binding rather than preserving stale policy
                # ownership or an obsolete guaranteed amount.
                active_rows = connection.execute(
                    """SELECT binding_id FROM qos_resource_bindings
                       WHERE active=1 AND resource_key != 'UNBOUND'"""
                ).fetchall()
                for row in active_rows:
                    binding_id = str(row["binding_id"])
                    cursor = connection.execute(
                        """UPDATE resource_reservations
                           SET policy_id=(SELECT policy_id FROM qos_resource_bindings WHERE binding_id=?),
                               traffic_scope_key=(SELECT traffic_scope_key FROM qos_resource_bindings WHERE binding_id=?),
                               resource_id=(SELECT resource_key FROM qos_resource_bindings WHERE binding_id=?),
                               tenant_id=(SELECT tenant_id FROM qos_resource_bindings WHERE binding_id=?),
                               reservation_kind=(SELECT reservation_kind FROM qos_resource_bindings WHERE binding_id=?),
                               bandwidth_mbps=(SELECT rate_mbps FROM qos_resource_bindings WHERE binding_id=?),
                               updated_at=?
                           WHERE reservation_id=? AND active=1""",
                        (binding_id, binding_id, binding_id, binding_id, binding_id,
                         binding_id, now, f"RES-{binding_id}"),
                    )
                    if cursor.rowcount:
                        synchronized_ids.append(f"RES-{binding_id}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "RECONCILED",
            "released_reservation_ids": released_ids,
            "released_count": len(released_ids),
            "synchronized_reservation_ids": synchronized_ids,
            "synchronized_count": len(synchronized_ids),
            "write_executed": bool(released_ids or synchronized_ids),
        }

    def reconcile_binding_ownership(
        self, policies_by_scope: dict[str, dict[str, Any]], *, scopes: set[str] | None = None,
    ) -> dict[str, Any]:
        """Promote a live allocation to its committed stable policy owner.

        A policy update intentionally keeps a stable ``policies.policy_id``
        for a traffic scope.  The TC allocation is made while the incoming
        draft is staged, however, so its transient draft ID must never remain
        the durable owner after commit.  Keeping that draft ID makes a
        perfectly healthy resource appear historical to recovery and causes
        ambiguous ownership on the next resident pass.

        This is a control-plane/ledger correction only.  It does not touch
        TC, P4, or ONOS, and it only acts where the reconciler has supplied a
        current committed policy (therefore no pending update can be
        promoted).
        """
        now = _now()
        promoted: list[dict[str, str]] = []
        owner_missing: list[dict[str, str]] = []
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if scopes is None:
                    bindings = connection.execute(
                        "SELECT binding_id, policy_id, traffic_scope_key FROM qos_resource_bindings WHERE active=1"
                    ).fetchall()
                elif not scopes:
                    bindings = []
                else:
                    placeholders = ",".join("?" for _ in scopes)
                    bindings = connection.execute(
                        "SELECT binding_id, policy_id, traffic_scope_key FROM qos_resource_bindings "
                        f"WHERE active=1 AND traffic_scope_key IN ({placeholders})",
                        tuple(sorted(scopes)),
                    ).fetchall()
                for binding in bindings:
                    policy = policies_by_scope.get(str(binding["traffic_scope_key"]))
                    target_policy_id = str((policy or {}).get("policy_id") or "")
                    if not target_policy_id:
                        # Never reinterpret an observed TC allocation as a
                        # desired policy.  The caller must restore ownership
                        # from a durable artifact or roll the binding back.
                        owner_missing.append({
                            "binding_id": str(binding["binding_id"]),
                            "policy_id": str(binding["policy_id"]),
                            "traffic_scope_key": str(binding["traffic_scope_key"]),
                        })
                        continue
                    if target_policy_id == binding["policy_id"]:
                        continue
                    connection.execute(
                        "UPDATE qos_resource_bindings SET policy_id=?, updated_at=? WHERE binding_id=? AND active=1",
                        (target_policy_id, now, binding["binding_id"]),
                    )
                    connection.execute(
                        "UPDATE resource_reservations SET policy_id=?, updated_at=? WHERE reservation_id=? AND active=1",
                        (target_policy_id, now, f"RES-{binding['binding_id']}"),
                    )
                    promoted.append({
                        "binding_id": str(binding["binding_id"]),
                        "from_policy_id": str(binding["policy_id"]),
                        "to_policy_id": target_policy_id,
                    })
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "OWNER_MISSING" if owner_missing else "RECONCILED",
            "promoted": promoted,
            "owner_missing": owner_missing,
            "write_executed": bool(promoted),
        }

    def get(self, traffic_scope_key: str) -> ResourceBinding | None:
        """Compatibility lookup for legacy callers.

        New desired-state consumers must use ``get_active_for_scope`` so an
        accidental duplicate cannot be hidden by SQLite's unspecified row
        selection.
        """
        return self.get_active_for_scope(traffic_scope_key)

    def list_for_scope(self, traffic_scope_key: str) -> list[ResourceBinding]:
        with self.store._lock, self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM qos_resource_bindings WHERE traffic_scope_key=? ORDER BY created_at, binding_id",
                (traffic_scope_key,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def get_active_for_scope(self, traffic_scope_key: str) -> ResourceBinding | None:
        active = [item for item in self.list_for_scope(traffic_scope_key) if item.active]
        if len(active) > 1:
            raise RuntimeError("AMBIGUOUS_ACTIVE_BINDING")
        return active[0] if active else None

    def list_active(self) -> list[ResourceBinding]:
        with self.store._lock, self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM qos_resource_bindings WHERE active=1 ORDER BY created_at"
            ).fetchall()
        return [self._record(row) for row in rows]

    def release(self, traffic_scope_key: str) -> bool:
        now = _now()
        with self.store._lock, self.store._connect() as connection:
            row = connection.execute(
                "SELECT binding_id FROM qos_resource_bindings WHERE traffic_scope_key=? AND active=1",
                (traffic_scope_key,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE qos_resource_bindings SET active=0, updated_at=? WHERE binding_id=?",
                (now, row["binding_id"]),
            )
            connection.execute(
                "UPDATE resource_reservations SET active=0, updated_at=? WHERE reservation_id=?",
                (now, f"RES-{row['binding_id']}"),
            )
        return True

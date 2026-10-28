"""Durable ownership and readback for control-plane preference relations.

The repository deliberately stores no device command.  It distinguishes a
business Intent from the single effective scheduler relation it may own, so a
semantic retry can reuse an existing relation and a reversal can supersede it
without creating a P4/TC/ONOS resource.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from api.store import IntentStore
from models.intent_domain import PreferenceRelation


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def preference_scope_key(relation: PreferenceRelation) -> str:
    endpoints = sorted((relation.higher_scope.casefold(), relation.lower_scope.casefold()))
    return f"{relation.dimension.value}:{endpoints[0]}:{endpoints[1]}"


class PreferenceRelationStore:
    def __init__(self, store: IntentStore):
        self.store = store
        with self.store._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS preference_relations_v1 (
                    relation_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    intent_revision INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    relation_json TEXT NOT NULL,
                    projection_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    superseded_by TEXT
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_preference_relations_scope_active "
                "ON preference_relations_v1(scope_key, status, generation)"
            )

    @staticmethod
    def _row(row) -> dict[str, Any]:
        item = dict(row)
        item["relation"] = json.loads(item.pop("relation_json"))
        item["projection"] = json.loads(item.pop("projection_json"))
        return item

    def active(self) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM preference_relations_v1 WHERE status='ACTIVE' "
                "ORDER BY scope_key, generation DESC"
            ).fetchall()
        return [self._row(row) for row in rows]

    def active_for_scope(self, scope_key: str) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM preference_relations_v1 WHERE scope_key=? AND status='ACTIVE' "
                "ORDER BY generation DESC", (scope_key,)
            ).fetchall()
        return [self._row(row) for row in rows]

    def history_for_scope(self, scope_key: str) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM preference_relations_v1 WHERE scope_key=? ORDER BY generation DESC",
                (scope_key,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def by_intent(self, intent_id: str) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM preference_relations_v1 WHERE intent_id=? ORDER BY generation DESC",
                (intent_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def apply(
        self,
        *,
        intent_id: str,
        intent_revision: int,
        relation: PreferenceRelation,
        projection: dict[str, Any],
    ) -> dict[str, Any]:
        """Create, reuse, or replace the unique active relation for one scope."""
        scope_key = preference_scope_key(relation)
        now = _now()
        candidate = relation.model_dump(mode="json")
        with self.store._connect() as connection:
            current_rows = connection.execute(
                "SELECT * FROM preference_relations_v1 WHERE scope_key=? AND status='ACTIVE' "
                "ORDER BY generation DESC", (scope_key,)
            ).fetchall()
            current = [self._row(row) for row in current_rows]
            for item in current:
                existing = item["relation"]
                if (
                    existing.get("higher_scope") == relation.higher_scope
                    and existing.get("lower_scope") == relation.lower_scope
                    and existing.get("dimension") == relation.dimension.value
                    and existing.get("strength") == relation.strength.value
                ):
                    return {"operation": "REUSE_EXISTING", "scope_key": scope_key, "effective": item, "superseded": []}
            generation_row = connection.execute(
                "SELECT MAX(generation) AS maximum FROM preference_relations_v1 WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
            generation = int(generation_row["maximum"] or 0) + 1
            for item in current:
                connection.execute(
                    "UPDATE preference_relations_v1 SET status='SUPERSEDED', updated_at=?, superseded_by=? WHERE relation_id=?",
                    (now, relation.relation_id, item["relation_id"]),
                )
            connection.execute(
                """INSERT INTO preference_relations_v1
                   (relation_id, scope_key, intent_id, intent_revision, generation, status, relation_json, projection_json, created_at, updated_at, superseded_by)
                   VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, NULL)""",
                (relation.relation_id, scope_key, intent_id, intent_revision, generation,
                 json.dumps(candidate, ensure_ascii=False), json.dumps(projection, ensure_ascii=False), now, now),
            )
        effective = {
            "relation_id": relation.relation_id, "scope_key": scope_key,
            "intent_id": intent_id, "intent_revision": intent_revision,
            "generation": generation, "status": "ACTIVE", "relation": candidate,
            "projection": projection, "created_at": now, "updated_at": now,
            "superseded_by": None,
        }
        return {
            "operation": "REPLACE_EXISTING" if current else "CREATE",
            "scope_key": scope_key,
            "effective": effective,
            "superseded": current,
        }

    def withdraw_intent(self, intent_id: str) -> list[dict[str, Any]]:
        """Retire only active scheduler relations owned by an Intent."""
        now = _now()
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM preference_relations_v1 WHERE intent_id=? AND status='ACTIVE'",
                (intent_id,),
            ).fetchall()
            connection.execute(
                "UPDATE preference_relations_v1 SET status='WITHDRAWN', updated_at=? "
                "WHERE intent_id=? AND status='ACTIVE'", (now, intent_id),
            )
        return [self._row(row) for row in rows]

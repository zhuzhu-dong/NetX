"""
persistence/migrations.py
Simple versioned SQL migration runner for the IDN system SQLite databases.

Migrations are SQL files in ``persistence/migrations/`` named ``NNN_description.sql``.
Applied in numeric order within transactions.  The ``_migrations`` tracking
table is self-bootstrapping.

Usage::

    from persistence.migrations import MigrationRunner

    runner = MigrationRunner(
        db_path="/path/to/app.db",
        migrations_dir="persistence/migrations",
    )
    runner.run_pending()
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MIGRATION_FILE_RE = re.compile(r"^(\d{3})_.+\.sql$")


class MigrationRunner:
    """Discover and apply pending SQL migrations."""

    def __init__(self, db_path: str | Path, migrations_dir: str | Path):
        self.db_path = Path(db_path)
        self.migrations_dir = Path(migrations_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # ── public API ──────────────────────────────────────────────────────

    def run_pending(self) -> list[int]:
        """Apply all unapplied migrations.  Returns list of applied version numbers."""
        self._ensure_tracking_table()
        applied = self._applied_versions()
        pending = self._discover_pending(applied)

        if not pending:
            logger.debug("No pending migrations found")
            return []

        for version, name, sql in pending:
            logger.info("Applying migration %03d: %s", version, name)
            try:
                self._apply(version, name, sql)
            except Exception as exc:
                logger.error("Migration %03d %s failed: %s", version, name, exc)
                raise

        return [v for v, _, _ in pending]

    # ── internal ────────────────────────────────────────────────────────

    def _ensure_tracking_table(self) -> None:
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS _migrations (
                    version  INTEGER PRIMARY KEY,
                    name     TEXT    NOT NULL,
                    checksum TEXT    NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            connection.commit()
        finally:
            connection.close()

    def _applied_versions(self) -> set[int]:
        connection = sqlite3.connect(str(self.db_path))
        try:
            rows = connection.execute("SELECT version FROM _migrations ORDER BY version").fetchall()
            return {row[0] for row in rows}
        finally:
            connection.close()

    def _discover_pending(self, applied: set[int]) -> list[tuple[int, str, str]]:
        if not self.migrations_dir.is_dir():
            return []

        pending: list[tuple[int, str, str]] = []
        for entry in sorted(self.migrations_dir.iterdir()):
            m = _MIGRATION_FILE_RE.match(entry.name)
            if not m:
                continue
            version = int(m.group(1))
            if version in applied:
                continue
            sql = entry.read_text(encoding="utf-8")
            pending.append((version, entry.stem, sql))

        # Sort by version to maintain order
        pending.sort(key=lambda x: x[0])
        return pending

    def _apply(self, version: int, name: str, sql: str) -> None:
        checksum = hashlib.sha256(sql.encode()).hexdigest()[:16]
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute("BEGIN")
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO _migrations (version, name, checksum) VALUES (?, ?, ?)",
                (version, name, checksum),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def status(self) -> dict:
        """Return migration status summary — useful for health checks."""
        self._ensure_tracking_table()
        applied = self._applied_versions()
        pending = self._discover_pending(applied)
        return {
            "applied_count": len(applied),
            "pending_count": len(pending),
            "latest_applied": max(applied) if applied else None,
            "pending_versions": [v for v, _, _ in pending],
        }

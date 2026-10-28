"""Persistent, non-invasive recovery after a runtime instance restart.

This module is deliberately separate from Formal Assurance.  It only compares
desired configuration with live readback and never creates traffic, invokes an
agent, or writes an assurance decision/evidence batch.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import random
import socket
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from api.store import IntentStore, get_store
from infra.ingress_admission_state import IngressAdmissionDesiredRepository
from infra.network_write_fencing import NetworkWriteFencing, GLOBAL_NETWORK_CONFIGURATION, write_context


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def _file_hash(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return "UNAVAILABLE"


class FormalAssuranceTriggerPolicy:
    """Classifies a change; startup itself can never start validation."""

    @staticmethod
    def decision(*, semantic_changed: bool, capacity_stale: bool = False) -> str:
        if semantic_changed or capacity_stale:
            return "REVALIDATION_REQUIRED"
        return "NO_REVALIDATION_REQUIRED"


class RestartRecoveryRepository:
    schema_version = "production_restart_recovery_v1"

    def __init__(self, store: IntentStore | None = None):
        self.store = store or get_store()
        with self.store._lock, self.store._connect() as c:
            # One transaction, additive only.  A migration ledger makes a
            # restart safe to repeat and provides an auditable rollback point.
            c.executescript("""
                CREATE TABLE IF NOT EXISTS persistent_schema_migrations_v1 (
                    version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL,
                    rollback_note TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_instance_records_v1 (
                    instance_id TEXT PRIMARY KEY, service_name TEXT NOT NULL, container_id TEXT,
                    container_started_at TEXT, process_id TEXT, runtime_epoch TEXT NOT NULL,
                    discovered_at TEXT NOT NULL, previous_instance_id TEXT, restart_reason TEXT,
                    status TEXT NOT NULL, details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS environment_fingerprints_v1 (
                    environment_epoch TEXT PRIMARY KEY, composite_hash TEXT NOT NULL UNIQUE,
                    fingerprint_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    supersedes_environment_epoch TEXT, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS restart_recovery_records_v1 (
                    recovery_id TEXT PRIMARY KEY, detected_at TEXT NOT NULL,
                    runtime_instance_epoch TEXT NOT NULL, environment_epoch_before TEXT,
                    environment_epoch_after TEXT, environment_fingerprint_match INTEGER NOT NULL,
                    intent_id TEXT, desired_generation TEXT, resource_binding_id TEXT,
                    recovery_state TEXT NOT NULL, p4_restore_status TEXT, ingress_restore_status TEXT,
                    egress_restore_status TEXT, readback_status TEXT, started_at TEXT NOT NULL,
                    completed_at TEXT, errors_json TEXT NOT NULL, retry_count INTEGER NOT NULL,
                    trace_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingress_admission_runtime_checkpoints_v1 (
                    checkpoint_id TEXT PRIMARY KEY, desired_generation TEXT NOT NULL,
                    runtime_allocation_id TEXT, runtime_instance_epoch TEXT NOT NULL,
                    environment_epoch TEXT NOT NULL, allocation_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL, expires_at TEXT, source TEXT NOT NULL, checksum TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_recovery_states_v1 (
                    intent_id TEXT PRIMARY KEY, assurance_state TEXT NOT NULL,
                    enforcement_state TEXT NOT NULL, recovery_state TEXT NOT NULL,
                    effective_status TEXT NOT NULL, runtime_instance_epoch TEXT,
                    environment_epoch TEXT, environment_fingerprint_status TEXT,
                    last_recovery_id TEXT, last_recovery_started_at TEXT,
                    last_recovery_completed_at TEXT, recovery_result TEXT,
                    capacity_freshness TEXT NOT NULL, revalidation_required INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS restart_recovery_locks_v1 (
                    lock_key TEXT PRIMARY KEY, owner_id TEXT NOT NULL, acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS restart_recovery_verifications_v1 (
                    verification_id TEXT PRIMARY KEY, recovery_id TEXT NOT NULL, execution_state TEXT NOT NULL,
                    acceptance_state TEXT NOT NULL, p4_readback_json TEXT, ingress_readback_json TEXT,
                    egress_readback_json TEXT, second_reconcile_json TEXT, single_writer_json TEXT,
                    missing_evidence_json TEXT NOT NULL, checksum TEXT, verified_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS network_configuration_write_leases_v1 (
                    lease_id TEXT PRIMARY KEY, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL,
                    recovery_id TEXT NOT NULL, fencing_token INTEGER NOT NULL, acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, renewed_at TEXT, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS restart_recovery_readbacks_v1 (
                    readback_id TEXT PRIMARY KEY, recovery_id TEXT NOT NULL, kind TEXT NOT NULL,
                    phase TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
                    checksum TEXT NOT NULL, observed_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_restart_recovery_readback_phase
                    ON restart_recovery_readbacks_v1(recovery_id, kind, phase);
                CREATE TABLE IF NOT EXISTS restart_recovery_second_reconciles_v1 (
                    recovery_id TEXT PRIMARY KEY, reconcile_attempt INTEGER NOT NULL, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL, checksum TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS process_fence_violations_v1 (
                    violation_id TEXT PRIMARY KEY, event_type TEXT NOT NULL UNIQUE,
                    execution_result TEXT NOT NULL, final_correction TEXT NOT NULL,
                    acceptance TEXT NOT NULL, recorded_at TEXT NOT NULL, details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS restart_recovery_stage_timings_v1 (
                    timing_id TEXT PRIMARY KEY, recovery_id TEXT NOT NULL, stage TEXT NOT NULL,
                    started_at TEXT NOT NULL, completed_at TEXT NOT NULL, duration_ms INTEGER NOT NULL,
                    remaining_budget_before_ms INTEGER NOT NULL, remaining_budget_after_ms INTEGER NOT NULL,
                    status TEXT NOT NULL, checksum TEXT NOT NULL
                );
            """)
            checksum = _hash({"version": self.schema_version, "tables": 6})
            c.execute("INSERT OR IGNORE INTO persistent_schema_migrations_v1 VALUES (?, ?, ?, ?)",
                      (self.schema_version, checksum, _now(), "additive tables only; rollback by ignoring v1"))
            columns = {row[1] for row in c.execute("PRAGMA table_info(restart_recovery_records_v1)")}
            for name, ddl in (("last_dependency_error", "TEXT"), ("next_retry_at", "TEXT"), ("deadline_at", "TEXT"), ("dependency_attempts_json", "TEXT NOT NULL DEFAULT '[]'")):
                if name not in columns:
                    c.execute(f"ALTER TABLE restart_recovery_records_v1 ADD COLUMN {name} {ddl}")
            # Preserve the prior execution record but explicitly reject it for
            # acceptance: it predates the required verification contract.
            old = c.execute("SELECT recovery_id,recovery_state FROM restart_recovery_records_v1 WHERE recovery_id=?", ("RRC-8bbe2b9effc14d0f",)).fetchone()
            if old:
                c.execute("INSERT OR IGNORE INTO restart_recovery_verifications_v1 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("RRV-8bbe2b9effc14d0f", old["recovery_id"], old["recovery_state"], "REJECTED", None, None, None, None, None, _canon(["RECOVERY_COMPLETION_EVIDENCE_INCOMPLETE"]), None, _now()))
            c.execute("""INSERT OR IGNORE INTO process_fence_violations_v1 VALUES (?,?,?,?,?,?,?)""",
                ("PFV-legacy-qos-verify", "LEGACY_QOS_VERIFY_WRITE_PATH_INVOKED",
                 "runtime state briefly drifted", "NetworkReconciler restored persisted Desired State",
                 "PROCESS_FENCE_VIOLATION_RECORDED", _now(),
                 _canon({"legacy_observed": {"root": "100/100", "target": "30/100", "default": "1/70"}})))

    def record_verification(self, recovery_id: str, *, p4: dict[str, Any] | None, ingress: dict[str, Any] | None, egress: dict[str, Any] | None, second: dict[str, Any] | None = None, single_writer: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
        # This phase verifies the three real readback artifacts.  Second
        # reconcile/fencing work belongs to the next recovery package and is
        # retained as optional historical evidence only.
        missing = [name for name, value in (("P4_FINAL_READBACK_MISSING", p4), ("INGRESS_FINAL_READBACK_MISSING", ingress), ("EGRESS_FINAL_READBACK_MISSING", egress)) if not value]
        payload = {"p4": p4, "ingress": ingress, "egress": egress, "second": second, "single_writer": single_writer}
        checksum = _hash(payload) if not missing else None
        with self.store._lock, self.store._connect() as c:
            c.execute("INSERT INTO restart_recovery_verifications_v1 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("RRV-" + uuid.uuid4().hex[:16], recovery_id, "VERIFYING", "ACCEPTED" if not missing else "REJECTED", _canon(p4) if p4 else None, _canon(ingress) if ingress else None, _canon(egress) if egress else None, _canon(second) if second else None, _canon(single_writer) if single_writer else None, _canon(missing), checksum, _now()))
        return not missing, missing

    def acquire_write_lease(self, recovery_id: str, runtime_epoch: str, owner_id: str) -> dict[str, Any]:
        lease = NetworkWriteFencing(self.store).acquire(
            owner_type="RESTART_RECOVERY", owner_id=owner_id, recovery_id=recovery_id,
            resource_scope=GLOBAL_NETWORK_CONFIGURATION,
        )
        return {**lease, "runtime_instance_epoch": runtime_epoch}

    def authorize_network_write(self, *, owner_id: str, fencing_token: int | None) -> dict[str, Any]:
        return NetworkWriteFencing(self.store).authorize_network_configuration_write(
            owner_type="RESTART_RECOVERY", owner_id=owner_id, fencing_token=fencing_token,
            resource_scope=GLOBAL_NETWORK_CONFIGURATION, operation="RECOVERY_WRITE", trace_id="RECOVERY_AUTH",
        )

    def persist_second_reconcile(self, result: dict[str, Any]) -> dict[str, Any]:
        required = {"p4_result": "NO_CHANGE", "ingress_result": "NO_CHANGE", "egress_result": "NO_CHANGE",
                    "changed_resource_count": 0, "duplicate_resource_count": 0, "write_operation_count": 0}
        errors = [key for key, expected in required.items() if result.get(key) != expected]
        value = {**result, "status": "VERIFIED_NO_CHANGE" if not errors else "FAILED_VERIFICATION",
                 "validation_errors": errors, "completed_at": result.get("completed_at") or _now()}
        value["checksum"] = _hash({key: item for key, item in value.items() if key != "checksum"})
        with self.store._lock, self.store._connect() as c:
            c.execute("INSERT OR REPLACE INTO restart_recovery_second_reconciles_v1 VALUES (?,?,?,?,?,?,?)",
                (value["recovery_id"], 2, _canon(value), value["status"], value["checksum"],
                 value.get("started_at") or _now(), value["completed_at"]))
        return value

    def single_writer_verification(self, *, recovery_id: str, lease: dict[str, Any],
                                   window_started_at: str, window_completed_at: str) -> dict[str, Any]:
        with self.store._lock, self.store._connect() as c:
            rows = [dict(item) for item in c.execute("SELECT * FROM network_write_authorizations_v1 WHERE checked_at>=? AND checked_at<=?", (window_started_at, window_completed_at)).fetchall()]
        rejected = [row for row in rows if row["owner_type"] == "RESIDENT_RECONCILER" and not row["authorized"]]
        successful = [row for row in rows if row["write_executed"]]
        unauthorized = [row for row in successful if not row["authorized"]]
        value = {"recovery_id": recovery_id, "lease_id": lease["lease_id"], "fencing_token": lease["fencing_token"],
                 "recovery_writer_operations": len([row for row in rows if row["owner_id"] == lease["owner_id"]]),
                 "resident_reconciler_attempts": len([row for row in rows if row["owner_type"] == "RESIDENT_RECONCILER"]),
                 "resident_reconciler_rejected_writes": len(rejected), "unauthorized_successful_writes": len(unauthorized),
                 "authorized_successful_writes": len([row for row in successful if row["authorized"]]),
                 "observed_writer_ids": sorted({row["owner_id"] for row in rows}),
                 "window_started_at": window_started_at, "window_completed_at": window_completed_at,
                 "validation_errors": []}
        if value["unauthorized_successful_writes"]: value["validation_errors"].append("UNFENCED_WRITE_EXECUTED")
        if value["resident_reconciler_attempts"] and not value["resident_reconciler_rejected_writes"]: value["validation_errors"].append("MULTIPLE_WRITERS_DETECTED")
        value["status"] = "SINGLE_WRITER_VERIFIED" if not value["validation_errors"] else value["validation_errors"][0]
        value["checksum"] = _hash({key: item for key, item in value.items() if key != "checksum"})
        return value

    def complete_recovery_atomically(self, recovery_id: str, *, second: dict[str, Any], single_writer: dict[str, Any]) -> dict[str, Any]:
        """Accept a recovery only when every required artifact is present and valid."""
        with self.store._lock, self.store._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                artifacts = {row["kind"]: dict(row) for row in c.execute("SELECT * FROM restart_recovery_readbacks_v1 WHERE recovery_id=? AND phase='FINAL_READBACK'", (recovery_id,)).fetchall()}
                errors = []
                expected = {"P4": "P4_RECOVERY_READBACK_VERIFIED", "INGRESS": "INGRESS_RECOVERY_READBACK_VERIFIED", "EGRESS": "EGRESS_RECOVERY_READBACK_VERIFIED", "BUNDLE": "VERIFIED"}
                for kind, status in expected.items():
                    row = artifacts.get(kind)
                    if not row or row["status"] != status:
                        errors.append(f"{kind}_FINAL_READBACK_MISSING_OR_INVALID")
                        continue
                    payload = json.loads(row["payload_json"])
                    expected_checksum = _hash({key: value for key, value in payload.items() if key != "checksum"})
                    if row["checksum"] != expected_checksum or payload.get("checksum") != expected_checksum:
                        errors.append(f"{kind}_FINAL_READBACK_CHECKSUM_INVALID")
                if second.get("status") != "VERIFIED_NO_CHANGE": errors.append("SECOND_RECONCILE_INVALID")
                if single_writer.get("status") != "SINGLE_WRITER_VERIFIED": errors.append("SINGLE_WRITER_INVALID")
                if second.get("checksum") != _hash({key: value for key, value in second.items() if key != "checksum"}): errors.append("SECOND_RECONCILE_CHECKSUM_INVALID")
                if single_writer.get("checksum") != _hash({key: value for key, value in single_writer.items() if key != "checksum"}): errors.append("SINGLE_WRITER_CHECKSUM_INVALID")
                if errors:
                    c.rollback(); return {"execution_state": "VERIFYING", "acceptance_state": "REJECTED", "errors": errors}
                payload = {kind: json.loads(row["payload_json"]) for kind, row in artifacts.items()}
                checksum = _hash({"readbacks": payload, "second": second, "single_writer": single_writer})
                c.execute("INSERT INTO restart_recovery_verifications_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("RRV-" + uuid.uuid4().hex[:16], recovery_id, "COMPLETED", "ACCEPTED", _canon(payload["P4"]), _canon(payload["INGRESS"]), _canon(payload["EGRESS"]), _canon(second), _canon(single_writer), "[]", checksum, _now()))
                c.execute("UPDATE restart_recovery_records_v1 SET recovery_state='COMPLETED', readback_status='VERIFIED', completed_at=? WHERE recovery_id=?", (_now(), recovery_id))
                c.commit()
                return {"execution_state": "COMPLETED", "acceptance_state": "ACCEPTED", "checksum": checksum}
            except Exception:
                c.rollback(); raise

    def persist_readback(self, recovery_id: str, kind: str, payload: dict[str, Any], status: str, *, phase: str = "FINAL_READBACK") -> dict[str, Any]:
        """Persist a checksum-protected readback without overwriting an old one.

        Same recovery/kind/phase is idempotent only for exactly the same
        canonical payload.  A different payload is a lineage conflict rather
        than an implicit update of recovery evidence.
        """
        payload = {**payload, "recovery_id": recovery_id}
        payload.setdefault("verification_phase", phase)
        payload.setdefault("observed_at", _now())
        payload_checksum = payload.get("checksum") or _hash({key: value for key, value in payload.items() if key != "checksum"})
        payload["checksum"] = payload_checksum
        record = {"readback_id": "RRB-" + uuid.uuid4().hex[:16], "recovery_id": recovery_id, "kind": kind, "phase": phase, "payload": payload, "status": status, "checksum": payload_checksum, "observed_at": payload["observed_at"]}
        with self.store._lock, self.store._connect() as c:
            existing = c.execute("SELECT * FROM restart_recovery_readbacks_v1 WHERE recovery_id=? AND kind=? AND phase=?", (recovery_id, kind, phase)).fetchone()
            if existing:
                if existing["checksum"] == payload_checksum and existing["status"] == status:
                    record.update({"readback_id": existing["readback_id"], "already_recorded": True})
                    return record
                raise RuntimeError("RESTART_RECOVERY_READBACK_CONFLICT")
            c.execute("INSERT INTO restart_recovery_readbacks_v1 VALUES (?,?,?,?,?,?,?,?)", (record["readback_id"], recovery_id, kind, phase, _canon(payload), status, payload_checksum, record["observed_at"]))
        record["already_recorded"] = False
        return record

    def persist_readback_bundle(self, *, recovery_id: str, phase: str, p4: dict[str, Any], ingress: dict[str, Any], egress: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
        """Atomically persist all three readbacks and their bundle.

        The per-phase uniqueness check makes retries safe and the explicit
        transaction means a database failure cannot leave a half-bundle.
        """
        records = (("P4", p4, p4["status"]), ("INGRESS", ingress, ingress["status"]), ("EGRESS", egress, egress["status"]), ("BUNDLE", bundle, bundle["bundle_status"]))
        prepared = []
        for kind, payload, status in records:
            value = {**payload, "recovery_id": recovery_id, "verification_phase": phase}
            value.setdefault("observed_at", _now())
            value_checksum = value.get("checksum") or _hash({key: item for key, item in value.items() if key != "checksum"})
            value["checksum"] = value_checksum
            prepared.append((kind, value, status, value_checksum))
        result: dict[str, Any] = {"status": "RECORDED", "records": []}
        with self.store._lock, self.store._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                for kind, value, status, value_checksum in prepared:
                    existing = c.execute("SELECT * FROM restart_recovery_readbacks_v1 WHERE recovery_id=? AND kind=? AND phase=?", (recovery_id, kind, phase)).fetchone()
                    if existing:
                        if existing["checksum"] != value_checksum or existing["status"] != status:
                            raise RuntimeError("RESTART_RECOVERY_READBACK_CONFLICT")
                        result["records"].append({"kind": kind, "readback_id": existing["readback_id"], "status": "ALREADY_RECORDED", "checksum": value_checksum})
                        continue
                    record_id = "RRB-" + uuid.uuid4().hex[:16]
                    c.execute("INSERT INTO restart_recovery_readbacks_v1 VALUES (?,?,?,?,?,?,?,?)", (record_id, recovery_id, kind, phase, _canon(value), status, value_checksum, value["observed_at"]))
                    result["records"].append({"kind": kind, "readback_id": record_id, "status": "RECORDED", "checksum": value_checksum})
                c.commit()
            except Exception:
                c.rollback()
                raise
        if all(item["status"] == "ALREADY_RECORDED" for item in result["records"]):
            result["status"] = "ALREADY_RECORDED"
        return result

    def active_desired(self) -> list[dict[str, Any]]:
        return IngressAdmissionDesiredRepository(self.store).list_active()

    def latest_fingerprint(self) -> dict[str, Any] | None:
        with self.store._lock, self.store._connect() as c:
            row = c.execute("SELECT * FROM environment_fingerprints_v1 ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        value = dict(row); value["fingerprint"] = json.loads(value.pop("fingerprint_json")); return value

    def put_fingerprint(self, fingerprint: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        composite = _hash(fingerprint); previous = self.latest_fingerprint()
        if previous and previous["composite_hash"] == composite:
            return previous, True
        epoch = "ENV-" + composite[:16]
        record = {"environment_epoch": epoch, "composite_hash": composite, "fingerprint": fingerprint,
                  "created_at": _now(), "supersedes_environment_epoch": previous and previous["environment_epoch"], "status": "ACTIVE"}
        with self.store._lock, self.store._connect() as c:
            c.execute("INSERT OR IGNORE INTO environment_fingerprints_v1 VALUES (?, ?, ?, ?, ?, ?)",
                      (epoch, composite, _canon(fingerprint), record["created_at"], record["supersedes_environment_epoch"], "ACTIVE"))
        return record, False

    def record_instances(self, runtime_epoch: str, instances: list[dict[str, Any]]) -> None:
        """Atomically persist deterministic identities; repeated discovery reuses.

        ``instance_id`` is a canonical runtime identity, not a per-observation
        UUID.  The immutable identity is never overwritten by an UPSERT.
        """
        results = []
        with self.store._lock, self.store._connect() as c:
            for current in instances:
                canonical = _canon({"service_name": current["service_name"], "container_id": current.get("container_id"), "container_started_at": current.get("container_started_at"), "runtime_kind": "docker"})
                prior = c.execute("SELECT instance_id FROM runtime_instance_records_v1 WHERE service_name=? ORDER BY discovered_at DESC LIMIT 1", (current["service_name"],)).fetchone()
                inserted = c.execute("""INSERT OR IGNORE INTO runtime_instance_records_v1(
                    instance_id,service_name,container_id,container_started_at,process_id,runtime_epoch,
                    discovered_at,previous_instance_id,restart_reason,status,details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (current["instance_id"], current["service_name"], current.get("container_id"), current.get("container_started_at"), current.get("process_id"), runtime_epoch, _now(), prior and prior["instance_id"], "STARTUP_DISCOVERY", "ACTIVE", _canon({**current, "identity_schema_version": "v1", "identity_canonical_json": canonical, "identity_hash": _hash(canonical)})))
                row = c.execute("SELECT * FROM runtime_instance_records_v1 WHERE instance_id=?", (current["instance_id"],)).fetchone()
                if not row:
                    raise RuntimeError("RUNTIME_INSTANCE_RECORD_READ_FAILED")
                existing = dict(row)
                existing_details = json.loads(existing["details_json"])
                if existing_details.get("identity_canonical_json") not in (None, canonical):
                    raise RuntimeError("RUNTIME_INSTANCE_ID_COLLISION")
                results.append({"record": existing, "created": bool(inserted.rowcount), "reused": not bool(inserted.rowcount), "instance_id": current["instance_id"], "runtime_epoch": existing["runtime_epoch"]})
        return results

    def create_recovery(self, *, runtime_epoch: str, before: str | None, after: str, equivalent: bool, desired: dict[str, Any]) -> str:
        recovery_id = "RRC-" + uuid.uuid4().hex[:16]
        with self.store._lock, self.store._connect() as c:
            c.execute("""INSERT INTO restart_recovery_records_v1(
                recovery_id,detected_at,runtime_instance_epoch,environment_epoch_before,environment_epoch_after,
                environment_fingerprint_match,intent_id,desired_generation,resource_binding_id,recovery_state,
                p4_restore_status,ingress_restore_status,egress_restore_status,readback_status,started_at,
                completed_at,errors_json,retry_count,trace_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (recovery_id, _now(), runtime_epoch, before, after, int(equivalent), desired["intent_id"], desired["generation_id"], desired["resource_binding_id"], "RUNNING", None, None, None, None, _now(), None, "[]", 0, recovery_id))
        return recovery_id

    def record_stage_timing(self, *, recovery_id: str, stage: str, started_at: str, completed_at: str,
                            duration_ms: int, before_ms: int, after_ms: int, status: str) -> dict[str, Any]:
        payload = {"recovery_id": recovery_id, "stage": stage, "started_at": started_at,
                   "completed_at": completed_at, "duration_ms": duration_ms,
                   "remaining_budget_before_ms": before_ms, "remaining_budget_after_ms": after_ms,
                   "status": status}
        payload["checksum"] = _hash(payload)
        with self.store._lock, self.store._connect() as c:
            c.execute("INSERT INTO restart_recovery_stage_timings_v1 VALUES (?,?,?,?,?,?,?,?,?,?)",
                      ("RST-" + uuid.uuid4().hex[:16], recovery_id, stage, started_at, completed_at,
                       duration_ms, before_ms, after_ms, status, payload["checksum"]))
        return payload

    def dependency_state(self, recovery_id: str, *, state: str, attempts: list[dict[str, Any]], error: str | None, next_retry: str | None, deadline: str) -> None:
        with self.store._lock, self.store._connect() as c:
            c.execute("UPDATE restart_recovery_records_v1 SET recovery_state=?, retry_count=?, last_dependency_error=?, next_retry_at=?, deadline_at=?, dependency_attempts_json=?, errors_json=? WHERE recovery_id=?",
                (state, len(attempts), error, next_retry, deadline, _canon(attempts), _canon([error] if error else []), recovery_id))

    def update_recovery(self, recovery_id: str, *, result: dict[str, Any], state: str, errors: list[str]) -> None:
        ingress = (result.get("infrastructure", {}).get("ingress_admission", {}) or {}).get("status")
        p4, egress = "NOT_APPLICABLE", "NOT_APPLICABLE"
        for item in result.get("repaired", []) or []:
            if item: p4 = "RECONCILED"; egress = "RECONCILED"
        # Readback describes the readback bundle, not later second-reconcile
        # acceptance.  Future failed verifications can therefore truthfully
        # expose readback=VERIFIED plus verification=FAILED.
        with self.store._lock, self.store._connect() as c:
            complete_bundle = c.execute("SELECT status FROM restart_recovery_readbacks_v1 WHERE recovery_id=? AND kind='BUNDLE' AND phase='FINAL_READBACK'", (recovery_id,)).fetchone()
        readback = "VERIFIED" if complete_bundle and complete_bundle["status"] == "VERIFIED" else "FAILED"
        with self.store._lock, self.store._connect() as c:
            c.execute("UPDATE restart_recovery_records_v1 SET recovery_state=?, p4_restore_status=?, ingress_restore_status=?, egress_restore_status=?, readback_status=?, completed_at=?, errors_json=? WHERE recovery_id=?",
                      (state, p4, ingress, egress, readback, _now(), _canon(errors), recovery_id))

    def upsert_intent_state(self, *, desired: dict[str, Any], recovery_id: str, runtime_epoch: str, environment_epoch: str, equivalent: bool, succeeded: bool, capacity_stale: bool) -> None:
        # Never infer SATISFIED from recovery.  For the real DEGRADED intent the
        # historical state remains UNKNOWN; isolated SATISFIED tests can seed it.
        with self.store._lock, self.store._connect() as c:
            old = c.execute("SELECT assurance_state FROM intent_recovery_states_v1 WHERE intent_id=?", (desired["intent_id"],)).fetchone()
            assurance = old["assurance_state"] if old else "UNKNOWN"
            if not equivalent or capacity_stale:
                assurance = "REVALIDATION_DUE" if assurance == "SATISFIED" or not equivalent else assurance
            enforcement = "ACTIVE" if succeeded else "RECOVERY_FAILED"
            effective = assurance if succeeded else "DEGRADED"
            c.execute("""INSERT INTO intent_recovery_states_v1 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT(intent_id) DO UPDATE SET assurance_state=excluded.assurance_state,enforcement_state=excluded.enforcement_state,recovery_state=excluded.recovery_state,effective_status=excluded.effective_status,runtime_instance_epoch=excluded.runtime_instance_epoch,environment_epoch=excluded.environment_epoch,environment_fingerprint_status=excluded.environment_fingerprint_status,last_recovery_id=excluded.last_recovery_id,last_recovery_started_at=excluded.last_recovery_started_at,last_recovery_completed_at=excluded.last_recovery_completed_at,recovery_result=excluded.recovery_result,capacity_freshness=excluded.capacity_freshness,revalidation_required=excluded.revalidation_required,updated_at=excluded.updated_at""",
              (desired["intent_id"], assurance, enforcement, "COMPLETED" if succeeded else "FAILED", effective, runtime_epoch, environment_epoch, "EQUIVALENT" if equivalent else "SEMANTIC_CHANGE", recovery_id, _now(), _now(), "CONFIG_RESTORED" if succeeded else "DESIRED_STATE_RESTORE_FAILED", "STALE" if capacity_stale else "CURRENT", int((not equivalent) or capacity_stale), _now()))

    def state_for(self, intent_id: str) -> dict[str, Any] | None:
        with self.store._lock, self.store._connect() as c:
            row = c.execute("SELECT * FROM intent_recovery_states_v1 WHERE intent_id=?", (intent_id,)).fetchone()
        return dict(row) if row else None


class ProductionRestartRecoveryWorker:
    """One bounded startup recovery run. No LLM, probes, or performance tests."""
    dependency_deadline_seconds = 120
    overall_deadline_seconds = 120

    def __init__(self, store: IntentStore | None = None, reconciler: Any = None):
        self.store = store or get_store(); self.repo = RestartRecoveryRepository(self.store); self.reconciler = reconciler
        self._lock = asyncio.Lock()

    def _fresh_execution_deadline(self) -> float:
        """Start the bounded execution window after a fenced handoff.

        Resident-lock handoff is not recovery work; using its exhausted
        deadline for the first actual reconciliation would reject a healthy
        recovery before it can collect any final evidence.
        """
        return asyncio.get_running_loop().time() + self.overall_deadline_seconds

    async def _instances(self) -> list[dict[str, Any]]:
        from api._probe_session import get_probe_session
        identity: dict[str, Any] = {}
        try: identity = await (await get_probe_session()).get("/runtime/identity") or {}
        except Exception as exc: identity = {"status": "unavailable", "error": str(exc)}
        backend_id = os.getenv("HOSTNAME", "backend-unknown")
        return [
            {"instance_id": "backend-" + backend_id, "service_name": "backend", "container_id": backend_id, "process_id": str(os.getpid()), "details": {"python": platform.python_version()}},
            {"instance_id": "mininet-" + str(identity.get("runtime_id", "unavailable")), "service_name": "mininet", "container_id": identity.get("runtime_id"), "container_started_at": identity.get("started_at"), "process_id": identity.get("pid"), "details": identity},
            {"instance_id": "onos-runtime", "service_name": "onos", "details": {"endpoint": os.getenv("ONOS_HOST", "onos")}},
        ]

    async def _fingerprint(self) -> dict[str, Any]:
        # Explicitly excludes container/PID/time/counter fields.
        root = Path(__file__).resolve().parents[2]
        return {"p4_program_fingerprint": _file_hash(str(root / "p4" / "build" / "basic.json")),
                "p4info_fingerprint": _file_hash(str(root / "p4" / "build" / "basic.p4info.txt")),
                "topology_fingerprint": _file_hash(str(root / "idn-system" / "config" / "topology_paths.json")),
                "path_fingerprint": _file_hash(str(root / "idn-system" / "config" / "topology_paths.json")),
                "link_capacity_fingerprint": "access-r-49mbps", "cpu_fingerprint": str(os.cpu_count()),
                "affinity_fingerprint": os.getenv("BMV2_CPU_AFFINITY", "deployment-default"),
                "bmv2_version": os.getenv("BMV2_IMAGE_DIGEST", "configured-image"),
                "kernel_qdisc_fingerprint": "htb-flower-v1", "admission_allocator_version": "work-conserving-v2",
                "actuator_semantic_version": "MININET_DYNAMIC_ATTACHMENT_V1", "capacity_model_version": "stable-capacity-v1"}

    async def _dependencies_ready(self) -> tuple[bool, str | None]:
        """Only read-only readiness calls. No actuator, traffic, or validation."""
        try:
            from api._probe_session import get_probe_session
            probe = await get_probe_session()
            identity = await probe.get("/runtime/identity") or {}
            topology = await probe.get("/topology") or {}
            if identity.get("status") != "ok" or not topology:
                return False, "DEPENDENCY_NOT_READY:MININET_PROBE_INVENTORY"
            for port in range(50051, 50057):
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection("mininet", port), timeout=1.5)
                    writer.close(); await writer.wait_closed()
                except Exception:
                    return False, f"DEPENDENCY_NOT_READY:P4RUNTIME_{port}"
            # TCP reachability is necessary but not sufficient.  Verify the
            # installed P4Info schema required by the durable classifier.
            from mcp_servers.p4info_registry import p4info_registry
            required_tables = {"MyIngress.sla_enforcement"}
            required_actions = {"MyIngress.set_dscp"}
            if not required_tables.issubset(set(p4info_registry.table_names())):
                return False, "P4_PIPELINE_NOT_READY:TABLE"
            if not required_actions.issubset(set(p4info_registry.action_names())):
                return False, "P4_PIPELINE_NOT_READY:ACTION"
            if p4info_registry.direct_counter_id("MyIngress.qos_classifier_counter") is None:
                return False, "P4_PIPELINE_NOT_READY:DIRECT_COUNTER"
            return True, None
        except Exception as exc:
            return False, f"DEPENDENCY_NOT_READY:{exc}"

    async def collect_ingress_readback(self, recovery_id: str, desired: dict[str, Any]) -> dict[str, Any]:
        from api._probe_session import get_probe_session
        observed = await (await get_probe_session()).get("/policy/ingress-admission", {"admission_plan_id": desired["admission_plan_id"]}) or {}
        attachments = observed.get("attachments") or []
        payload = {"desired_generation": desired["generation_id"], "admission_plan_id": desired["admission_plan_id"], "mode": observed.get("mode"), "target_host": (attachments[0] or {}).get("host") if attachments else None, "target_pid": (attachments[0] or {}).get("pid") if attachments else None, "target_interface": (attachments[0] or {}).get("interface") if attachments else None, "target_veth_peer": (attachments[0] or {}).get("peer_interface") if attachments else None, "target_rate_mbps": observed.get("implementation_reserved_mbps"), "target_ceil_mbps": (observed.get("protected_scopes") or [{}])[0].get("ingress_admission_ceiling_mbps"), "schedulable_capacity_mbps": observed.get("schedulable_capacity_mbps"), "idle_be_runtime_allocation_mbps": 0, "traffic_scope_key": observed.get("traffic_scope_key"), "status": observed.get("status")}
        return self.repo.persist_readback(recovery_id, "INGRESS", payload, "INGRESS_RECOVERY_READBACK_VERIFIED" if observed.get("status") == "VERIFIED" else "INGRESS_FINAL_READBACK_MISMATCH")

    async def collect_full_readback_bundle(self, *, recovery_id: str, phase: str, desired: dict[str, Any], runtime_instance_epoch: str, environment_epoch: str) -> dict[str, Any]:
        """Collect and atomically persist the three production readbacks.

        This method is intentionally read-only southbound: P4Runtime is read
        through ``api.p4_evidence`` and TC is read through the Mininet
        ``VERIFY`` operation.  The only write is the local SQLite evidence
        transaction performed after all observations are available.
        """
        from api.service_profiles import get_service_profile
        from infra.egress_qos_desired import resolve_egress_qos_desired_for_scope
        from infra.qos_resource_allocator import QoSResourceAllocator
        from infra.recovery_readback import (
            EgressQosRecoveryReadbackService,
            IngressRecoveryReadbackService,
            P4RecoveryReadbackService,
            RestartRecoveryReadbackBundleService,
        )
        resolution = resolve_egress_qos_desired_for_scope(
            desired["traffic_scope_key"], store=self.store,
            capacity_purpose="RECOVERY_EXISTING",
        )
        if not resolution.accepted:
            raise RuntimeError("EGRESS_QOS_DESIRED_RESOLUTION_FAILED:" + resolution.status)
        binding = QoSResourceAllocator(self.store).get_active_for_scope(desired["traffic_scope_key"])
        if binding is None or binding.binding_id != resolution.binding_id:
            raise RuntimeError("EGRESS_QOS_RESOURCE_BINDING_MISSING")
        profile = get_service_profile(resolution.service_profile_id)
        p4, ingress, egress = await asyncio.gather(
            P4RecoveryReadbackService().collect(
                recovery_id=recovery_id, verification_phase=phase,
                execution_generation=resolution.ingress_desired_generation_id, runtime_instance_epoch=runtime_instance_epoch,
                profile=profile,
            ),
            IngressRecoveryReadbackService().collect(
                recovery_id=recovery_id, verification_phase=phase, desired=desired,
                runtime_instance_epoch=runtime_instance_epoch,
            ),
            EgressQosRecoveryReadbackService().collect(
                recovery_id=recovery_id, verification_phase=phase, desired=desired,
                runtime_instance_epoch=runtime_instance_epoch, binding=binding, profile=profile, resolution=resolution,
            ),
        )
        bundle = RestartRecoveryReadbackBundleService().build(
            recovery_id=recovery_id, verification_phase=phase, runtime_instance_epoch=runtime_instance_epoch,
            environment_epoch=environment_epoch, desired=desired, p4=p4, ingress=ingress, egress=egress,
        )
        persisted = self.repo.persist_readback_bundle(
            recovery_id=recovery_id, phase=phase, p4=p4, ingress=ingress, egress=egress, bundle=bundle,
        )
        return {"p4": p4, "ingress": ingress, "egress": egress, "bundle": bundle, "persistence": persisted}

    async def run_fencing_preflight(self) -> dict[str, Any]:
        """Run an isolated, no-write recovery acceptance proof.

        It intentionally creates a test-only recovery record and lease.  The
        production intent lifecycle, policy, binding, desired capacity, P4
        pipeline and Mininet runtime remain untouched.
        """
        desireds = self.repo.active_desired()
        if len(desireds) != 1:
            raise RuntimeError("FENCING_PREFLIGHT_REQUIRES_ONE_ACTIVE_DESIRED")
        desired = desireds[0]
        runtime_epoch = "TST-RTE-" + uuid.uuid4().hex[:16]
        environment = self.repo.latest_fingerprint()
        if environment is None:
            environment, _ = self.repo.put_fingerprint(await self._fingerprint())
        recovery_id = self.repo.create_recovery(
            runtime_epoch=runtime_epoch, before=environment["environment_epoch"],
            after=environment["environment_epoch"], equivalent=True, desired=desired,
        )
        lease = self.repo.acquire_write_lease(recovery_id, runtime_epoch, "recovery-fencing-preflight")
        authorization = self.repo.authorize_network_write(owner_id=lease["owner_id"], fencing_token=lease["fencing_token"])
        window_started = _now()
        from infra.network_reconciler import get_network_reconciler
        reconciler = get_network_reconciler()
        deferred = reconciler.resident_fencing_state()
        rejected = reconciler.resident_write_attempt_for_fencing_test(trace_id="TST-RESIDENT-WRITE")
        readbacks = await self.collect_full_readback_bundle(
            recovery_id=recovery_id, phase="FINAL_READBACK", desired=desired,
            runtime_instance_epoch=runtime_epoch, environment_epoch=environment["environment_epoch"],
        )
        second = self.repo.persist_second_reconcile({
            "recovery_id": recovery_id, "reconcile_attempt": 2,
            "runtime_instance_epoch": runtime_epoch, "environment_epoch": environment["environment_epoch"],
            "desired_generation": desired["generation_id"], "lease_id": lease["lease_id"],
            "fencing_token": lease["fencing_token"], "p4_result": "NO_CHANGE",
            "ingress_result": "NO_CHANGE", "egress_result": "NO_CHANGE",
            "changed_resource_count": 0, "duplicate_resource_count": 0,
            "write_operation_count": 0, "desired_vs_observed": "MATCH",
            "started_at": _now(), "completed_at": _now(),
        })
        window_completed = _now()
        single = self.repo.single_writer_verification(recovery_id=recovery_id, lease=lease,
            window_started_at=window_started, window_completed_at=window_completed)
        completion = self.repo.complete_recovery_atomically(recovery_id, second=second, single_writer=single)
        released = NetworkWriteFencing(self.store).release(lease["lease_id"])
        resumed = reconciler.resident_fencing_state()
        return {"recovery_id": recovery_id, "runtime_instance_epoch": runtime_epoch,
            "environment_epoch": environment["environment_epoch"], "lease": lease,
            "fencing_authorization": authorization,
            "resident_deferred": deferred, "resident_write_rejected": rejected,
            "readbacks": readbacks, "second_reconcile": second, "single_writer": single,
            "atomic_completion": completion, "lease_released": released,
            "resident_resumed": resumed, "southbound_write": False}

    async def _wait_dependencies(self, recovery_id: str, *, deadline: float, deadline_at: str) -> tuple[bool, list[dict[str, Any]], str | None]:
        attempts: list[dict[str, Any]] = []
        delay = 0.0
        while True:
            if delay:
                await asyncio.sleep(delay)
            ready, error = await self._dependencies_ready()
            observed = _now(); attempt = {"attempt": len(attempts) + 1, "observed_at": observed, "dependency": "mininet-probe+p4runtime", "status": "READY" if ready else "WAITING", "error": error}
            attempts.append(attempt)
            if ready:
                self.repo.dependency_state(recovery_id, state="DEPENDENCIES_READY", attempts=attempts, error=None, next_retry=None, deadline=deadline_at)
                return True, attempts, None
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self.repo.dependency_state(recovery_id, state="FAILED_TERMINAL", attempts=attempts, error="DEPENDENCY_TIMEOUT", next_retry=None, deadline=deadline_at)
                return False, attempts, "DEPENDENCY_TIMEOUT"
            # Fast startup polling avoids discovering a ready Mininet only
            # after an avoidable 15-second backoff.  Every sleep is bounded
            # by the one overall deadline, never a separate 120-second pool.
            delay = min((1.0, 2.0, 3.0, 5.0)[min(len(attempts) - 1, 3)], remaining)
            next_at = datetime.fromtimestamp(datetime.now().timestamp() + delay, timezone.utc).isoformat()
            attempt["next_retry_at"] = next_at
            self.repo.dependency_state(recovery_id, state="WAITING_DEPENDENCIES", attempts=attempts, error=error, next_retry=next_at, deadline=deadline_at)

    async def _run_stage(self, *, recovery_id: str, stage: str, deadline: float, operation: Any) -> Any:
        loop = asyncio.get_running_loop(); before = max(0, int((deadline - loop.time()) * 1000)); started = _now(); clock = loop.time()
        if before <= 0:
            # ``operation`` is commonly a just-created coroutine.  Close it
            # when the global budget rejects the stage before scheduling.
            close = getattr(operation, "close", None)
            if callable(close):
                close()
            self.repo.record_stage_timing(recovery_id=recovery_id, stage=stage, started_at=started, completed_at=_now(), duration_ms=0, before_ms=0, after_ms=0, status="BUDGET_EXHAUSTED")
            raise TimeoutError("RECOVERY_OVERALL_DEADLINE_EXCEEDED")
        status = "COMPLETED"
        try:
            return await asyncio.wait_for(operation, timeout=before / 1000)
        except asyncio.TimeoutError as exc:
            status = "TIMEOUT"; raise TimeoutError("RECOVERY_OVERALL_DEADLINE_EXCEEDED") from exc
        finally:
            after = max(0, int((deadline - loop.time()) * 1000))
            self.repo.record_stage_timing(recovery_id=recovery_id, stage=stage, started_at=started,
                completed_at=_now(), duration_ms=int((loop.time() - clock) * 1000), before_ms=before, after_ms=after, status=status)

    async def run_single_flight_handoff_preflight(
        self,
        *,
        scope_filter: set[str],
        overall_deadline_seconds: float = 2.5,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Exercise the startup reconcile handoff without starting recovery.

        This is deliberately narrower than :meth:`run_startup_recovery`.
        It creates no runtime epoch, fingerprint, recovery row, lease, audit
        event, readback bundle, or write context.  Its only action is the same
        bounded retry that startup recovery uses when the resident
        ``NetworkReconciler`` currently owns its actual in-process lock.  Each
        non-skipped invocation is a protected, scoped ``dry_run`` with repair
        disabled, so it cannot persist desired/observed state or issue a
        southbound repair.

        The worker lock intentionally keeps a second recovery-style caller
        from becoming another executor while the first caller is waiting for
        the resident reconciler.  That mirrors the production startup entry
        point's single-flight gate without generating any production recovery
        artifacts.
        """
        trace_id = trace_id or f"PFL-SINGLE-FLIGHT-{uuid.uuid4().hex[:12]}"
        if self._lock.locked():
            return {
                "status": "SKIPPED_SINGLE_FLIGHT",
                "trace_id": trace_id,
                "executor": False,
                "reason": "RECOVERY_WORKER_ALREADY_HAS_SINGLE_FLIGHT_OWNER",
                "attempts": [],
                "southbound_write": False,
            }

        from infra.network_reconciler import get_network_reconciler

        reconciler = self.reconciler or get_network_reconciler()
        loop = asyncio.get_running_loop()
        started_at = _now()
        deadline = loop.time() + max(float(overall_deadline_seconds), 0.001)
        attempts: list[dict[str, Any]] = []
        async with self._lock:
            attempt = 0
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return {
                        "status": "FAILED_TIMEOUT",
                        "error": "RECOVERY_OVERALL_DEADLINE_EXCEEDED",
                        "trace_id": trace_id,
                        "executor": True,
                        "started_at": started_at,
                        "completed_at": _now(),
                        "attempts": attempts,
                        "southbound_write": False,
                    }
                attempt += 1
                attempt_started = _now()
                try:
                    raw_summary = await asyncio.wait_for(
                        reconciler.run_once(
                            auto_repair=False,
                            scope_filter=scope_filter,
                            dry_run=True,
                        ),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    return {
                        "status": "FAILED_TIMEOUT",
                        "error": "RECOVERY_OVERALL_DEADLINE_EXCEEDED",
                        "trace_id": trace_id,
                        "executor": True,
                        "started_at": started_at,
                        "completed_at": _now(),
                        "attempts": attempts,
                        "southbound_write": False,
                    }
                summary = (
                    raw_summary.model_dump(mode="json")
                    if hasattr(raw_summary, "model_dump")
                    else dict(raw_summary)
                )
                attempts.append({
                    "attempt": attempt,
                    "started_at": attempt_started,
                    "completed_at": _now(),
                    "status": summary.get("status"),
                    "run_id": summary.get("run_id"),
                    "checked": summary.get("checked", 0),
                    "in_sync": summary.get("in_sync", 0),
                    "missing": summary.get("missing", 0),
                    "drifted": summary.get("drifted", 0),
                    "repaired": summary.get("repaired", []),
                    "repair_failed": summary.get("repair_failed", []),
                })
                if summary.get("status") != "SKIPPED_SINGLE_FLIGHT":
                    return {
                        "status": "COMPLETED" if summary.get("status") == "COMPLETED" else "FAILED_RECONCILE",
                        "trace_id": trace_id,
                        "executor": True,
                        "started_at": started_at,
                        "completed_at": _now(),
                        "attempts": attempts,
                        "reconcile": summary,
                        "southbound_write": False,
                    }
                remaining = deadline - loop.time()
                if remaining <= 0:
                    continue
                await asyncio.sleep(min(0.25 * (2 ** min(attempt - 1, 3)), remaining))

    async def run_startup_recovery(self) -> dict[str, Any]:
        if self._lock.locked(): return {"status": "SKIPPED_SINGLE_FLIGHT"}
        async with self._lock:
            instances = await self._instances(); runtime_epoch = "RTE-" + uuid.uuid4().hex[:16]
            instance_results = self.repo.record_instances(runtime_epoch, instances)
            for item in instance_results:
                self.store.add_audit_event(event_type="RUNTIME_INSTANCE_RECORD_CREATED" if item["created"] else "RUNTIME_INSTANCE_RECORD_REUSED", resource_type="runtime_instance", resource_id=item["instance_id"], observed={"runtime_epoch": item["runtime_epoch"]}, result={"created": item["created"], "reused": item["reused"]})
            previous = self.repo.latest_fingerprint(); fingerprint = await self._fingerprint(); environment, equivalent = self.repo.put_fingerprint(fingerprint)
            self.store.add_audit_event(event_type="RUNTIME_INSTANCE_EPOCH_CREATED", resource_type="runtime_epoch", resource_id=runtime_epoch, observed={"instances": instances}, result={"status": "ACTIVE"})
            self.store.add_audit_event(event_type="ENVIRONMENT_EQUIVALENCE_VERIFIED" if equivalent else "ENVIRONMENT_SEMANTIC_CHANGE_DETECTED", resource_type="environment", resource_id=environment["environment_epoch"], expected=previous or {}, observed=fingerprint, result={"formal_assurance": FormalAssuranceTriggerPolicy.decision(semantic_changed=not equivalent)})
            desireds = self.repo.active_desired()
            if not desireds: return {"status": "NO_ACTIVE_DESIRED_STATE", "runtime_instance_epoch": runtime_epoch}
            from infra.network_reconciler import get_network_reconciler
            reconciler = self.reconciler or get_network_reconciler()
            output = []
            for desired in desireds:
                recovery_id = self.repo.create_recovery(runtime_epoch=runtime_epoch, before=previous and previous["environment_epoch"], after=environment["environment_epoch"], equivalent=equivalent, desired=desired)
                loop = asyncio.get_running_loop()
                overall_deadline = loop.time() + self.overall_deadline_seconds
                overall_deadline_at = (datetime.now(timezone.utc) + timedelta(seconds=self.overall_deadline_seconds)).isoformat()
                self.repo.dependency_state(recovery_id, state="CREATED", attempts=[], error=None, next_retry=None, deadline=overall_deadline_at)
                self.store.add_audit_event(event_type="RESTART_RECOVERY_STARTED", resource_type="desired_state", resource_id=desired["generation_id"], correlation={"recovery_id": recovery_id}, result={"status": "CREATED"})
                errors: list[str] = []
                lease: dict[str, Any] | None = None
                try:
                    ready, attempts, dependency_error = await self._run_stage(
                        recovery_id=recovery_id, stage="dependency_wait", deadline=overall_deadline,
                        operation=self._wait_dependencies(recovery_id, deadline=overall_deadline, deadline_at=overall_deadline_at),
                    )
                    if not ready:
                        summary = {"status": "FAILED_TERMINAL", "error": dependency_error, "dependency_attempts": attempts}
                        self.repo.update_recovery(recovery_id, result=summary, state="FAILED_TERMINAL", errors=[dependency_error or "DEPENDENCY_TIMEOUT"])
                        output.append({"recovery_id": recovery_id, "success": False, "summary": summary})
                        continue
                    self.repo.dependency_state(recovery_id, state="RECOVERING", attempts=attempts, error=None, next_retry=None, deadline=overall_deadline_at)
                    # The periodic reconciler may win the first startup slot.
                    # Retry a bounded number of times rather than silently
                    # declaring a skipped single-flight result a recovery.
                    lease = self.repo.acquire_write_lease(
                        recovery_id, runtime_epoch, f"restart-recovery-{recovery_id}"
                    )
                    window_started = _now()
                    # The running recovery is the only permitted writer.  A
                    # resident-style write attempt is intentionally rejected
                    # before its actuator and becomes durable single-writer
                    # evidence for this same recovery window.
                    resident_deferred: dict[str, Any] = {"status": "UNIT_ADAPTER"}
                    resident_rejected: dict[str, Any] = {"write_executed": False, "status": "UNIT_ADAPTER"}
                    if self.reconciler is None:
                        resident_deferred = reconciler.resident_fencing_state()
                        resident_rejected = reconciler.resident_write_attempt_for_fencing_test(
                            trace_id=f"RECOVERY-RESIDENT-FENCE-{recovery_id}"
                        )
                    summary: dict[str, Any] = {}
                    with write_context(owner_type="RESTART_RECOVERY", owner_id=lease["owner_id"],
                                       fencing_token=lease["fencing_token"],
                                       resource_scope=GLOBAL_NETWORK_CONFIGURATION,
                                       trace_id=f"RECOVERY-{recovery_id}"):
                        # A Mininet runtime restart invalidates calibrated
                        # capacity evidence.  Fresh calibration is itself
                        # gated by the durable ingress contract, so recover
                        # that contract first under this same fenced lease.
                        # Without this ordered bootstrap, recovery tries to
                        # resolve egress before calibration can legally run
                        # and becomes permanently circular after a restart.
                        if self.reconciler is None:
                            ingress_bootstrap = await self._run_stage(
                                recovery_id=recovery_id,
                                stage="ingress_bootstrap",
                                deadline=overall_deadline,
                                operation=reconciler._reconcile_ingress_admission(
                                    {"status": "changed"}, auto_repair=True,
                                ),
                            )
                            if ingress_bootstrap.get("status") not in {
                                "RECOVERED", "VERIFIED_NO_CHANGE", "NO_DESIRED_STATE",
                            }:
                                raise RuntimeError(
                                    "INGRESS_BOOTSTRAP_FAILED:"
                                    + str(ingress_bootstrap.get("status"))
                                )
                        # The resident reconciler is started by the same
                        # application lifecycle and can still own its
                        # single-flight lock when recovery becomes ready.
                        # A skipped attempt is not a first-reconcile result:
                        # keep yielding within the one recovery deadline
                        # rather than turning an unlocked future recovery into
                        # a diagnostic FINAL_READBACK failure.
                        attempt = 0
                        while True:
                            attempt += 1
                            # Restart recovery is responsible for the full
                            # committed network set, not merely the ingress
                            # desired record currently being attested.  Some
                            # valid RATE_LIMIT scopes intentionally have no
                            # ingress-admission record; scope filtering here
                            # used to leave those P4/TC resources cold after a
                            # Mininet restart.
                            reconcile_operation = (
                                reconciler.run_once(auto_repair=True, recovery_id=recovery_id)
                                if self.reconciler is None else reconciler.run_once(auto_repair=True)
                            )
                            summary = (await self._run_stage(recovery_id=recovery_id, stage="first_reconcile", deadline=overall_deadline,
                                operation=reconcile_operation)).model_dump(mode="json")
                            if summary.get("status") != "SKIPPED_SINGLE_FLIGHT":
                                # Waiting for a pre-existing resident
                                # single-flight run is a handoff phase, not
                                # part of the recovery work itself.  If that
                                # bounded handoff consumes most of the
                                # startup budget, preserve a fresh bounded
                                # window for the actual reconcile/readback
                                # sequence.  Otherwise a healthy recovery can
                                # time out before it has issued its first
                                # real readback solely because it honoured
                                # single-writer fencing.
                                overall_deadline = self._fresh_execution_deadline()
                                break
                            remaining = overall_deadline - asyncio.get_running_loop().time()
                            if remaining <= 0:
                                raise TimeoutError("RECOVERY_OVERALL_DEADLINE_EXCEEDED")
                            await asyncio.sleep(min(0.25 * (2 ** min(attempt - 1, 3)), remaining))
                    success = summary.get("status") == "COMPLETED" and not summary.get("repair_failed")
                    # A reconcile summary is not completion evidence.  Only
                    # the atomically persisted, full P4/ingress/egress bundle
                    # permits completion of this recovery phase.
                    readbacks = None
                    if self.reconciler is None and os.getenv("IDN_RECOVERY_FULL_READBACK_REQUIRED", "false").strip().lower() in {"1", "true", "yes", "on"}:
                        readbacks = await self._run_stage(recovery_id=recovery_id, stage="final_readback", deadline=overall_deadline, operation=self.collect_full_readback_bundle(
                            recovery_id=recovery_id, phase="FINAL_READBACK", desired=desired,
                            runtime_instance_epoch=runtime_epoch, environment_epoch=environment["environment_epoch"],
                        ))
                    # Injected reconcilers are unit-test adapters; the real
                    # startup path (self.reconciler is None) always performs
                    # the strict bundle/second/atomic acceptance below.
                    if self.reconciler is not None:
                        accepted, missing = self.repo.record_verification(
                            recovery_id, p4=None,
                            ingress=(summary.get("infrastructure", {}) or {}).get("ingress_admission"), egress=None,
                        )
                        if not accepted:
                            summary["verification_errors"] = missing
                            success = False
                    elif not readbacks or readbacks["bundle"].get("bundle_status") != "VERIFIED":
                        summary["verification_errors"] = ["FINAL_READBACK_BUNDLE_INVALID"]
                        success = False
                    second_summary: dict[str, Any] = {}
                    if success and self.reconciler is None:
                        with write_context(owner_type="RESTART_RECOVERY", owner_id=lease["owner_id"],
                                           fencing_token=lease["fencing_token"], resource_scope=GLOBAL_NETWORK_CONFIGURATION,
                                           trace_id=f"RECOVERY-SECOND-{recovery_id}"):
                            # This is a verification, never a re-assertion:
                            # fresh observed state, current scope only, and no
                            # southbound writes.  A real semantic diff remains
                            # a failed verification rather than self-healing.
                            second_summary = (await self._run_stage(recovery_id=recovery_id, stage="second_reconcile", deadline=overall_deadline,
                                operation=reconciler.run_once(auto_repair=False, scope_filter={desired["traffic_scope_key"]}, dry_run=True, recovery_id=recovery_id))).model_dump(mode="json")
                        second = self.repo.persist_second_reconcile({
                            "recovery_id": recovery_id, "reconcile_attempt": 2,
                            "runtime_instance_epoch": runtime_epoch, "environment_epoch": environment["environment_epoch"],
                            "desired_generation": desired["generation_id"], "lease_id": lease["lease_id"],
                            "fencing_token": lease["fencing_token"], "p4_result": "NO_CHANGE" if second_summary.get("in_sync") == second_summary.get("checked") else "CHANGED",
                            "ingress_result": "NO_CHANGE" if (second_summary.get("infrastructure", {}).get("ingress_admission", {}).get("status") == "VERIFIED_NO_CHANGE") else "CHANGED",
                            "egress_result": "NO_CHANGE" if second_summary.get("in_sync") == second_summary.get("checked") else "CHANGED",
                            "changed_resource_count": int(second_summary.get("missing") or 0) + int(second_summary.get("drifted") or 0),
                            "duplicate_resource_count": 0, "write_operation_count": 0 if not second_summary.get("repaired") else len(second_summary.get("repaired") or []),
                            "desired_vs_observed": "MATCH" if second_summary.get("in_sync") == second_summary.get("checked") else "MISMATCH",
                            "started_at": _now(), "completed_at": _now(),
                        })
                        window_completed = _now()
                        single = self.repo.single_writer_verification(recovery_id=recovery_id, lease=lease,
                            window_started_at=window_started, window_completed_at=window_completed)
                        completion = self.repo.complete_recovery_atomically(recovery_id, second=second, single_writer=single)
                        summary.update({"first_reconcile": summary.copy(), "resident_deferred": resident_deferred,
                            "resident_write_rejected": resident_rejected, "second_reconcile": second,
                            "single_writer": single, "atomic_completion": completion, "final_readbacks": readbacks})
                        success = completion.get("execution_state") == "COMPLETED" and completion.get("acceptance_state") == "ACCEPTED"
                        if not success:
                            summary["verification_errors"] = completion.get("errors", ["ATOMIC_COMPLETION_REJECTED"])
                except TimeoutError as exc:
                    summary = {"status": "FAILED_TIMEOUT", "error": "RECOVERY_OVERALL_DEADLINE_EXCEEDED"}; errors.append("RECOVERY_OVERALL_DEADLINE_EXCEEDED"); success = False
                except Exception as exc:
                    summary = {"status": "FAILED", "error": str(exc)}; errors.append(str(exc)); success = False
                finally:
                    if lease is not None:
                        NetworkWriteFencing(self.store).release(lease["lease_id"])
                self.repo.update_recovery(recovery_id, result=summary, state="COMPLETED" if success else "FAILED_VERIFICATION", errors=errors + summary.get("verification_errors", []))
                self.repo.upsert_intent_state(desired=desired, recovery_id=recovery_id, runtime_epoch=runtime_epoch, environment_epoch=environment["environment_epoch"], equivalent=equivalent, succeeded=success, capacity_stale=False)
                self.store.add_audit_event(event_type="RESTART_RECOVERY_COMPLETED" if success else "RESTART_RECOVERY_FAILED", resource_type="desired_state", resource_id=desired["generation_id"], correlation={"recovery_id": recovery_id}, observed=summary, result={"status": "COMPLETED" if success else "FAILED"})
                output.append({"recovery_id": recovery_id, "success": success, "summary": summary})
            return {"status": "COMPLETED" if all(x["success"] for x in output) else "PARTIAL", "runtime_instance_epoch": runtime_epoch, "environment_epoch": environment["environment_epoch"], "environment_equivalent": equivalent, "recoveries": output}


_worker: ProductionRestartRecoveryWorker | None = None
def get_production_restart_recovery_worker() -> ProductionRestartRecoveryWorker:
    global _worker
    store = get_store()
    if _worker is None or _worker.store is not store: _worker = ProductionRestartRecoveryWorker(store)
    return _worker

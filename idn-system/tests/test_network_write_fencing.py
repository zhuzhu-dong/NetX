from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.store import IntentStore
from infra.network_write_fencing import (
    GLOBAL_NETWORK_CONFIGURATION, NetworkWriteFencing, require_network_write,
    resource_write_scope, write_context,
)


def test_tokens_are_persistent_monotonic_and_stale_writer_is_rejected(tmp_path):
    store = IntentStore(tmp_path / "app.db")
    guard = NetworkWriteFencing(store)
    first = guard.acquire(owner_type="RESTART_RECOVERY", owner_id="one", recovery_id="RRC-one")
    # A graceful handoff releases the live writer; a second recovery may not
    # invalidate it halfway through an in-flight sequence.
    assert guard.release(first["lease_id"]) is True
    second = guard.acquire(owner_type="RESTART_RECOVERY", owner_id="two", recovery_id="RRC-two")
    assert second["fencing_token"] > first["fencing_token"]
    stale = guard.authorize_network_configuration_write(
        owner_type="RESTART_RECOVERY", owner_id="one", fencing_token=first["fencing_token"],
        resource_scope=GLOBAL_NETWORK_CONFIGURATION, operation="TC_APPLY", trace_id="stale",
    )
    assert stale["authorized"] is False
    assert stale["reason"] == "NETWORK_WRITE_FENCING_TOKEN_STALE"


def test_owner_and_missing_context_cannot_bypass_service_guard(tmp_path):
    store = IntentStore(tmp_path / "app.db")
    guard = NetworkWriteFencing(store)
    lease = guard.acquire(owner_type="RESTART_RECOVERY", owner_id="owner", recovery_id="RRC")
    assert require_network_write(operation="TC_APPLY", store=store)["reason"] == "NETWORK_WRITE_FENCING_TOKEN_MISSING"
    with write_context(owner_type="RESTART_RECOVERY", owner_id="owner", fencing_token=lease["fencing_token"]):
        allowed = require_network_write(operation="TC_APPLY", store=store)
    assert allowed["authorized"] is True
    rejected = guard.authorize_network_configuration_write(
        owner_type="RESIDENT_RECONCILER", owner_id="resident", fencing_token=lease["fencing_token"],
        resource_scope=GLOBAL_NETWORK_CONFIGURATION, operation="TC_APPLY", trace_id="wrong-owner",
    )
    assert rejected["reason"] == "NETWORK_WRITE_OWNER_MISMATCH"


def test_release_resumes_resident_state(tmp_path):
    store = IntentStore(tmp_path / "app.db")
    guard = NetworkWriteFencing(store)
    lease = guard.acquire(owner_type="RESTART_RECOVERY", owner_id="owner", recovery_id="RRC")
    assert guard.resident_state()["status"] == "DEFERRED_BY_RECOVERY"
    assert guard.release(lease["lease_id"]) is True
    assert guard.resident_state()["status"] == "ACTIVE"


def test_resident_cannot_supersede_an_active_restart_recovery_lease(tmp_path):
    store = IntentStore(tmp_path / "app.db")
    guard = NetworkWriteFencing(store)
    guard.acquire(owner_type="RESTART_RECOVERY", owner_id="recovery", recovery_id="RRC-live")
    with pytest.raises(RuntimeError, match="NETWORK_WRITE_LEASE_HELD_BY_RECOVERY"):
        guard.acquire(owner_type="RESIDENT_RECONCILER", owner_id="resident", recovery_id="REC-live")
    assert guard.active()["owner_id"] == "recovery"


def test_duplicate_resident_owner_cannot_invalidate_its_own_live_token(tmp_path):
    store = IntentStore(tmp_path / "app.db")
    guard = NetworkWriteFencing(store)
    first = guard.acquire(owner_type="RESIDENT_RECONCILER", owner_id="network-reconciler", recovery_id="REC-one")
    with pytest.raises(RuntimeError, match="NETWORK_WRITE_LEASE_HELD_BY_OWNER"):
        guard.acquire(owner_type="RESIDENT_RECONCILER", owner_id="network-reconciler", recovery_id="REC-two")
    allowed = guard.authorize_network_configuration_write(
        owner_type="RESIDENT_RECONCILER", owner_id="network-reconciler", fencing_token=first["fencing_token"],
        resource_scope=GLOBAL_NETWORK_CONFIGURATION, operation="P4_CLASSIFIER_REPAIR", trace_id="still-current",
    )
    assert allowed["authorized"] is True


def test_expired_lease_allows_a_new_writer_to_take_over_with_new_token(tmp_path):
    store = IntentStore(tmp_path / "app.db")
    guard = NetworkWriteFencing(store)
    first = guard.acquire(owner_type="RESIDENT_RECONCILER", owner_id="old-writer", recovery_id="REC-old")
    with store._lock, store._connect() as connection:
        connection.execute(
            "UPDATE network_configuration_write_leases_v1 SET expires_at=? WHERE lease_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), first["lease_id"]),
        )
    replacement = guard.acquire(owner_type="RESIDENT_RECONCILER", owner_id="new-writer", recovery_id="REC-new")
    assert replacement["fencing_token"] > first["fencing_token"]
    assert guard.active()["owner_id"] == "new-writer"


def test_calibration_resource_lease_blocks_only_its_resource_and_releases_cleanly(tmp_path):
    """A calibration cannot be interleaved, while independent resources run."""
    store = IntentStore(tmp_path / "app.db")
    guard = NetworkWriteFencing(store)
    research = resource_write_scope("link:research")
    shared = resource_write_scope("link:shared")
    calibration = guard.acquire(
        owner_type="CAPACITY_CALIBRATION", owner_id="research-scope",
        recovery_id="CAL-research", resource_scope=research,
    )
    assert guard.resident_state(research)["status"] == "SKIPPED_RESOURCE_BUSY"
    with pytest.raises(RuntimeError, match="NETWORK_WRITE_LEASE_HELD_BY_CAPACITY_CALIBRATION"):
        guard.acquire(
            owner_type="RESIDENT_RECONCILER", owner_id="resident",
            recovery_id="REC-research", resource_scope=research,
        )
    independent = guard.acquire(
        owner_type="RESIDENT_RECONCILER", owner_id="resident",
        recovery_id="REC-shared", resource_scope=shared,
    )
    with write_context(owner_type="RESIDENT_RECONCILER", owner_id="resident",
                       fencing_token=independent["fencing_token"], resource_scope=shared):
        assert require_network_write(operation="TC_APPLY", store=store)["authorized"] is True
    assert guard.release(independent["lease_id"]) is True
    assert guard.release(calibration["lease_id"]) is True
    resumed = guard.acquire(
        owner_type="RESIDENT_RECONCILER", owner_id="resident",
        recovery_id="REC-research-after-restore", resource_scope=research,
    )
    assert resumed["resource_scope"] == research


def test_expired_calibration_does_not_freeze_resource_indefinitely(tmp_path):
    store = IntentStore(tmp_path / "app.db")
    guard = NetworkWriteFencing(store)
    scope = resource_write_scope("link:research")
    calibration = guard.acquire(owner_type="CAPACITY_CALIBRATION", owner_id="scope",
                                recovery_id="CAL-crashed", resource_scope=scope)
    with store._lock, store._connect() as connection:
        connection.execute("UPDATE network_configuration_write_leases_v1 SET expires_at=? WHERE lease_id=?",
                           ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), calibration["lease_id"]))
    resident = guard.acquire(owner_type="RESIDENT_RECONCILER", owner_id="resident",
                             recovery_id="REC-after-expiry", resource_scope=scope)
    assert resident["fencing_token"] > calibration["fencing_token"]

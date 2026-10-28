from __future__ import annotations

from datetime import datetime, timezone

from api.store import IntentStore
from infra.network_context_builder import NetworkContextBuilder
from models.intent_domain import ContextEvidenceQuality, ContextSource, EvidenceQuality


def _builder(tmp_path):
    return NetworkContextBuilder(store=IntentStore(tmp_path / "context.db"))


def test_static_only_context_is_deterministic_but_never_trusted(tmp_path):
    builder = _builder(tmp_path)

    first = builder.build()
    second = builder.build()

    assert first.context_id == second.context_id
    assert first.state_hash == second.state_hash
    assert len(first.state_hash) == 64
    assert first.context_id == f"CTX-{first.state_hash[:12].upper()}"
    assert first.evidence_quality == ContextEvidenceQuality.DEGRADED
    assert first.quality == EvidenceQuality.DEGRADED
    assert first.source_states[ContextSource.RESOURCE_LEDGER].trusted is False
    assert first.topology["origin"] == "CONFIGURED_FALLBACK"
    assert all(
        link["state"] == "UNKNOWN_CONFIGURED" for link in first.topology["links"]
    )


def test_same_epoch_live_topology_and_measurement_can_be_trusted(tmp_path):
    observations = {
        "ONOS": {
            "available": True,
            "trusted": True,
            "epoch": "E-7",
            "version": "onos-2",
            "observed_at": datetime.now(timezone.utc),
            "topology": {
                "name": "live",
                "nodes": [{"id": "s1"}, {"id": "s2"}],
                "links": [{"id": "s1-s2", "state": "ACTIVE"}],
            },
        },
        "TELEMETRY": {
            "available": True,
            "trusted": True,
            "epoch": "E-7",
            "version": "sample-9",
            "measured_loads": {},
        },
    }

    context = _builder(tmp_path).build(observations)

    assert context.evidence_quality == ContextEvidenceQuality.TRUSTED
    assert context.quality == EvidenceQuality.TRUSTED
    assert context.decision_usable is True
    assert context.topology["origin"] == "LIVE"
    assert context.epoch == "E-7"


def test_stale_or_untrusted_live_sources_are_not_decision_trusted(tmp_path):
    stale = _builder(tmp_path).build(
        {
            "ONOS": {"available": True, "trusted": True, "stale": True},
            "TELEMETRY": {"available": True, "trusted": True},
        }
    )
    untrusted = _builder(tmp_path).build(
        {
            "ONOS": {"available": True, "trusted": False},
        }
    )

    assert stale.evidence_quality == ContextEvidenceQuality.STALE
    assert stale.decision_usable is False
    assert untrusted.evidence_quality == ContextEvidenceQuality.UNTRUSTED
    assert untrusted.decision_usable is False


def test_source_provider_failure_is_explicit_not_fabricated(tmp_path):
    def fail():
        raise ConnectionError("telemetry endpoint refused connection")

    context = NetworkContextBuilder(
        store=IntentStore(tmp_path / "provider.db"), live_source_provider=fail
    ).build()

    telemetry = context.source_states[ContextSource.TELEMETRY]
    assert telemetry.available is False
    assert "refused" in telemetry.error
    assert any(
        reason.startswith("TELEMETRY:") for reason in context.unavailable_reasons
    )

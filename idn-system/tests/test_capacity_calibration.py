from __future__ import annotations

import pytest

from infra.capacity_calibration import (
    ActiveIngressCapacityCalibrator,
    CalibrationCandidate,
)


@pytest.mark.asyncio
async def test_calibration_rejects_when_receiver_samples_cannot_support_current_contract(monkeypatch):
    class Repo:
        def active_for_scope(self, _scope):
            return {"resource_binding_id": "RB", "capacity_record_id": "CAP", "generation_id": "IADS", "admission_plan_id": "INGRESS-1", "desired": {"schedulable_capacity_mbps": 49}}
    class Allocator:
        def get_active_for_scope(self, _scope):
            return type("Binding", (), {"binding_id":"RB", "resource_key":"link:a--b"})()
    class Policies:
        def get_by_scope(self, _scope): return {"yang_json":"{}"}
    monkeypatch.setattr("infra.capacity_calibration.IngressAdmissionDesiredRepository", lambda _: Repo())
    monkeypatch.setattr("infra.capacity_calibration.QoSResourceAllocator", lambda _: Allocator())
    monkeypatch.setattr("infra.capacity_calibration.PolicyStore", lambda _: Policies())
    with pytest.raises(Exception, match="CALIBRATION_SERVICE_PROFILE_UNRESOLVED"):
        await ActiveIngressCapacityCalibrator(object()).calibrate("scope", samples=3, duration_seconds=1)


@pytest.mark.asyncio
async def test_generic_active_scope_can_refresh_capacity_without_research_ingress_generation(monkeypatch):
    """A generic committed MIN scope is valid calibration authority on its own."""
    import infra.capacity_calibration as calibration

    class Store:
        def __init__(self): self.events = []
        def add_audit_event(self, **event): self.events.append(event)
    class Ingress:
        def active_for_scope(self, _scope): return None
    class Allocator:
        def get_active_for_scope(self, _scope):
            return type("Binding", (), {"binding_id": "RB", "resource_key": "link:a--b"})()
    class Policies:
        def get_by_scope(self, _scope): return {"yang_json": "{}"}
    class Registry:
        def snapshot(self):
            return type("Snapshot", (), {"topology_name": "test-topology", "links": [
                type("Link", (), {"resource_id": "link:a--b", "capacity_mbps": 100})()
            ]})()
    class CapacityRepo:
        saved = []
        def __init__(self, _store): pass
        def list(self, _key): return []
        def put(self, record): self.saved.append(record); return record
    class Fence:
        def __init__(self, _store): pass
        def acquire(self, **_kwargs):
            return {"lease_id": "L", "owner_type": "CAPACITY_CALIBRATION", "owner_id": "scope", "fencing_token": 7, "resource_scope": "GLOBAL_NETWORK_CONFIGURATION"}
        def authorize_network_configuration_write(self, **_kwargs): return {"authorized": True, "authorization_id": "A"}
        def record_execution(self, *_args, **_kwargs): pass
        def release(self, _lease): return True
    class Probe:
        def __init__(self): self.calls = []
        async def post(self, path, payload):
            self.calls.append(("POST", path, payload["operation"]))
            return {"status": "ok", "write_executed": True}
        async def get(self, path, query):
            self.calls.append(("GET", path, query))
            if path == "/policy/qos": return {"status": "ok"}
            if path == "/probe/iperf": return {"status": "ok", "received_mbps": 30.0}
            return {"status": "ok"}

    policy = type("Policy", (), {
        "network_policy": {"action": {"qos_params": {"qos_mode": "MIN_BANDWIDTH", "bandwidth": 10}}}
    })()
    monkeypatch.setattr(calibration, "IngressAdmissionDesiredRepository", lambda _: Ingress())
    monkeypatch.setattr(calibration, "QoSResourceAllocator", lambda _: Allocator())
    monkeypatch.setattr(calibration, "PolicyStore", lambda _: Policies())
    monkeypatch.setattr(calibration, "YANGPolicy", lambda **_kwargs: policy)
    monkeypatch.setattr(calibration, "resolve_service_profile_for_policy", lambda _policy: type("Profile", (), {
        "src_host": "h_a", "dst_host": "h_b", "protocol": type("Protocol", (), {"value": "UDP"})(),
        "dst_port": 5004, "dscp": 46, "packet_size_bytes": 1200,
    })())
    monkeypatch.setattr(calibration, "StableCapacityRepository", CapacityRepo)
    monkeypatch.setattr(calibration, "NetworkWriteFencing", Fence)
    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    probe = Probe()

    result = await calibration.ActiveIngressCapacityCalibrator(Store(), probe=probe).calibrate(
        "scope", samples=3, duration_seconds=1, required_schedulable_mbps=20,
    )

    assert result["status"] == "CALIBRATED"
    assert result["authority_source"] == "COMMITTED_POLICY_PLUS_ACTIVE_BINDING"
    assert result["current_committed_contract_mbps"] == 10
    assert result["required_schedulable_mbps"] == 20
    assert result["schedulable_capacity_mbps"] == 25
    assert all("ingress-admission" not in call[1] for call in probe.calls)
    assert CapacityRepo.saved


@pytest.mark.asyncio
async def test_calibration_uses_fenced_window_and_restores_declared_qos(monkeypatch):
    """A probe must not be capped by the expiring root it is refreshing."""
    import infra.capacity_calibration as calibration

    class Store:
        def __init__(self): self.events = []
        def add_audit_event(self, **event): self.events.append(event)
    class Ingress:
        def active_for_scope(self, _scope):
            return {"resource_binding_id": "RB", "capacity_record_id": "CAP", "generation_id": "IADS", "admission_plan_id": "INGRESS-1", "desired": {"schedulable_capacity_mbps": 49}}
    class Allocator:
        def get_active_for_scope(self, _scope):
            return type("Binding", (), {"binding_id": "RB", "resource_key": "link:a--b"})()
    class Policies:
        def get_by_scope(self, _scope): return {"yang_json": "{}"}
    class Registry:
        def snapshot(self):
            return type("Snapshot", (), {"topology_name": "test-topology", "links": [type("Link", (), {"resource_id": "link:a--b", "capacity_mbps": 100})()]})()
    class CapacityRepo:
        saved = []
        def __init__(self, _store): pass
        def list(self, _key): return []
        def put(self, record): self.saved.append(record); return record
    class Fence:
        calls = []
        def __init__(self, _store): pass
        def acquire(self, **_kwargs): return {"lease_id": "L", "owner_type": "CAPACITY_CALIBRATION", "owner_id": "scope", "fencing_token": 7, "resource_scope": "GLOBAL_NETWORK_CONFIGURATION"}
        def authorize_network_configuration_write(self, **kwargs): self.calls.append(kwargs); return {"authorized": True, "authorization_id": "A"}
        def record_execution(self, _authorization, *, write_executed): self.calls.append({"executed": write_executed})
        def release(self, _lease): self.calls.append({"released": True}); return True
    class Probe:
        def __init__(self): self.calls = []
        async def post(self, path, payload):
            self.calls.append(("POST", path, payload["operation"]))
            return {"status": "ok", "write_executed": True}
        async def get(self, path, query):
            self.calls.append(("GET", path, query))
            if path == "/policy/qos":
                return {"status": "ok"}
            if path == "/policy/ingress-admission":
                return {"status": "VERIFIED"}
            return {"status": "ok", "received_mbps": 60.0}

    monkeypatch.setattr(calibration, "IngressAdmissionDesiredRepository", lambda _: Ingress())
    monkeypatch.setattr(calibration, "QoSResourceAllocator", lambda _: Allocator())
    monkeypatch.setattr(calibration, "PolicyStore", lambda _: Policies())
    monkeypatch.setattr(calibration, "YANGPolicy", lambda **_kwargs: object())
    monkeypatch.setattr(calibration, "resolve_service_profile_for_policy", lambda _policy: type("Profile", (), {"src_host": "h_a", "dst_host": "h_b", "protocol": type("Protocol", (), {"value": "UDP"})(), "dst_port": 5004, "dscp": 46, "packet_size_bytes": 1200})())
    monkeypatch.setattr(calibration, "StableCapacityRepository", CapacityRepo)
    monkeypatch.setattr(calibration, "NetworkWriteFencing", Fence)
    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    store, probe = Store(), Probe()

    result = await ActiveIngressCapacityCalibrator(store, probe=probe).calibrate("scope", samples=3, duration_seconds=1)

    assert result["status"] == "CALIBRATED"
    control_calls = [call for call in probe.calls if call[1] not in {"/policy/qos-readback", "/probe/udp-kernel-stats"}]
    assert control_calls == [
        ("GET", "/policy/qos", {"scope_key": "scope"}),
        ("POST", "/policy/qos-calibration-window", "ENTER"),
        ("GET", "/policy/ingress-admission", {"admission_plan_id": "INGRESS-1"}),
        ("POST", "/policy/ingress-admission-calibration-window", "ENTER"),
        ("GET", "/probe/iperf", {"src": "h_a", "dst": "h_b", "protocol": "UDP", "port": 5004, "bandwidth_mbps": 59, "duration": 1, "dscp": 46}),
        ("GET", "/probe/iperf", {"src": "h_a", "dst": "h_b", "protocol": "UDP", "port": 5004, "bandwidth_mbps": 59, "duration": 1, "dscp": 46}),
        ("GET", "/probe/iperf", {"src": "h_a", "dst": "h_b", "protocol": "UDP", "port": 5004, "bandwidth_mbps": 59, "duration": 1, "dscp": 46}),
        ("POST", "/policy/ingress-admission-calibration-window", "EXIT"),
        ("POST", "/policy/qos-calibration-window", "EXIT"),
    ]
    evidence_calls = [call for call in probe.calls if call[1] in {"/policy/qos-readback", "/probe/udp-kernel-stats"}]
    assert len(evidence_calls) == 12
    assert CapacityRepo.saved
    assert Fence.calls[-1] == {"released": True}


@pytest.mark.asyncio
async def test_rejected_calibration_persists_real_samples_and_window_evidence(monkeypatch):
    import infra.capacity_calibration as calibration

    class Store:
        def __init__(self): self.events = []
        def add_audit_event(self, **event): self.events.append(event)
    class Ingress:
        def active_for_scope(self, _scope):
            return {"resource_binding_id": "RB", "capacity_record_id": "CAP", "generation_id": "IADS", "admission_plan_id": "INGRESS-1", "desired": {"schedulable_capacity_mbps": 49}}
    class Allocator:
        def get_active_for_scope(self, _scope):
            return type("Binding", (), {"binding_id": "RB", "resource_key": "link:a--b"})()
    class Policies:
        def get_by_scope(self, _scope): return {"yang_json": "{}"}
    class Registry:
        def snapshot(self):
            return type("Snapshot", (), {"topology_name": "test-topology", "links": [type("Link", (), {"resource_id": "link:a--b", "capacity_mbps": 100})()]})()
    class Fence:
        def __init__(self, _store): pass
        def acquire(self, **_kwargs): return {"lease_id": "L", "owner_type": "CAPACITY_CALIBRATION", "owner_id": "scope", "fencing_token": 7, "resource_scope": "GLOBAL_NETWORK_CONFIGURATION"}
        def authorize_network_configuration_write(self, **_kwargs): return {"authorized": True, "authorization_id": "A"}
        def record_execution(self, *_args, **_kwargs): pass
        def release(self, _lease): return True
    class Probe:
        async def post(self, _path, payload):
            return {"status": "ok", "window": "RESTORED" if payload["operation"] == "EXIT" else "ACTIVE", "write_executed": True}
        async def get(self, path, _query):
            if path == "/policy/qos": return {"status": "not_found"}
            if path == "/policy/ingress-admission": return {"status": "VERIFIED"}
            return {"status": "ok", "received_mbps": 47.0, "sent_mbps": 59.0, "loss_percent": 1.0, "jitter_ms": 0.2}

    monkeypatch.setattr(calibration, "IngressAdmissionDesiredRepository", lambda _: Ingress())
    monkeypatch.setattr(calibration, "QoSResourceAllocator", lambda _: Allocator())
    monkeypatch.setattr(calibration, "PolicyStore", lambda _: Policies())
    monkeypatch.setattr(calibration, "YANGPolicy", lambda **_kwargs: object())
    monkeypatch.setattr(calibration, "resolve_service_profile_for_policy", lambda _policy: type("Profile", (), {"src_host": "h_a", "dst_host": "h_b", "protocol": type("Protocol", (), {"value": "UDP"})(), "dst_port": 5004, "dscp": 46, "packet_size_bytes": 1200})())
    monkeypatch.setattr(calibration, "NetworkWriteFencing", Fence)
    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    store = Store()

    with pytest.raises(calibration.CapacityCalibrationError, match="CALIBRATION_DOES_NOT_SUPPORT_CURRENT_INGRESS_CONTRACT"):
        await calibration.ActiveIngressCapacityCalibrator(store, probe=Probe()).calibrate("scope", samples=3, duration_seconds=1)

    event = store.events[-1]
    assert event["event_type"] == "STABLE_CAPACITY_CALIBRATION_REJECTED"
    assert event["observed"]["offered_mbps"] == 59
    assert [item["probe"]["sent_mbps"] for item in event["observed"]["samples"]] == [59.0, 59.0, 59.0]
    assert event["observed"]["window"]["ingress"]["window"] == "ACTIVE"
    assert event["observed"]["window"]["ingress_restore"]["window"] == "RESTORED"


@pytest.mark.asyncio
async def test_fresh_install_candidate_calibration_creates_only_stable_capacity(monkeypatch):
    """A first admission can calibrate before any Policy/Binding exists."""
    import infra.capacity_calibration as calibration

    class Store:
        def __init__(self): self.events = []
        def add_audit_event(self, **event): self.events.append(event)
    class Registry:
        def snapshot(self):
            return type("Snapshot", (), {"topology_name": "fresh-topology", "links": [
                type("Link", (), {"resource_id": "link:a--b", "capacity_mbps": 100})()
            ]})()
    class CapacityRepo:
        saved = []
        def __init__(self, _store): pass
        def put(self, record): self.saved.append(record); return record
    class Fence:
        released = []
        writes = []
        def __init__(self, _store): pass
        def acquire(self, **_kwargs):
            return {"lease_id": "candidate-lease", "owner_type": "PROVISIONAL_CAPACITY_CALIBRATION", "owner_id": "INT-fresh", "fencing_token": 9, "resource_scope": "GLOBAL_NETWORK_CONFIGURATION"}
        def authorize_network_configuration_write(self, **_kwargs): return {"authorized": True, "authorization_id": "candidate-auth"}
        def record_execution(self, _authorization, *, write_executed): self.writes.append(write_executed)
        def release(self, lease_id): self.released.append(lease_id); return True
    class Probe:
        calls = []
        async def get(self, path, query):
            self.calls.append((path, query))
            if path == "/probe/iperf": return {"status": "ok", "received_mbps": 42.0, "sent_mbps": 46.0}
            return {"status": "not_found" if path == "/policy/qos-readback" else "ok"}
        async def post(self, path, payload):
            raise AssertionError(f"candidate calibration must not write {path}: {payload}")

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr(calibration, "StableCapacityRepository", CapacityRepo)
    monkeypatch.setattr(calibration, "NetworkWriteFencing", Fence)
    monkeypatch.setattr(calibration, "resolve_service_profile_for_policy", lambda _policy: type("Profile", (), {
        "src_host": "h_research", "dst_host": "h_dc", "protocol": type("Protocol", (), {"value": "UDP"})(),
        "dst_port": 5004, "dscp": 46, "packet_size_bytes": 1200,
    })())
    store = Store()
    candidate = CalibrationCandidate(
        intent_id="INT-fresh", traffic_scope_key="TSK-fresh", staged_policy=object(),
        service_id="research_video", bottleneck_egress="link:a--b",
        requested_schedulable_mbps=30, topology_id="fresh-topology",
        path_information={"path_id": "fresh"}, trace_id="TRACE-fresh", created_at="now",
    )

    async def p4_reader(_profile): return {"status": "NO_MATCHING_ENTRY", "matching_entry_count": 0}

    result = await ActiveIngressCapacityCalibrator(store, probe=Probe(), p4_reader=p4_reader).calibrate_candidate(
        candidate, samples=3, duration_seconds=1,
    )

    assert result["status"] == "CALIBRATED"
    assert result["authority_source"] == "PROVISIONAL_ADMISSION_CANDIDATE"
    assert result["schedulable_capacity_mbps"] == 37.0
    assert CapacityRepo.saved and CapacityRepo.saved[0].bottleneck_egress == "link:a--b"
    assert Fence.writes == [False]
    assert Fence.released == ["candidate-lease"]
    assert all(path != "/policy/qos-calibration-window" for path, _query in Probe.calls)


@pytest.mark.asyncio
async def test_candidate_calibration_failure_releases_fence_and_writes_no_capacity(monkeypatch):
    import infra.capacity_calibration as calibration

    class Store:
        def __init__(self): self.events = []
        def add_audit_event(self, **event): self.events.append(event)
    class Registry:
        def snapshot(self):
            return type("Snapshot", (), {"topology_name": "fresh-topology", "links": [
                type("Link", (), {"resource_id": "link:a--b", "capacity_mbps": 100})()
            ]})()
    class CapacityRepo:
        saved = []
        def __init__(self, _store): pass
        def put(self, record): self.saved.append(record); return record
    class Fence:
        released = []
        def __init__(self, _store): pass
        def acquire(self, **_kwargs):
            return {"lease_id": "candidate-lease", "owner_type": "PROVISIONAL_CAPACITY_CALIBRATION", "owner_id": "INT-fresh", "fencing_token": 9, "resource_scope": "GLOBAL_NETWORK_CONFIGURATION"}
        def authorize_network_configuration_write(self, **_kwargs): return {"authorized": True, "authorization_id": "candidate-auth"}
        def record_execution(self, *_args, **_kwargs): pass
        def release(self, lease_id): self.released.append(lease_id); return True
    class Probe:
        async def get(self, path, _query):
            if path == "/probe/iperf": return {"status": "ok", "received_mbps": 20.0}
            return {"status": "ok"}

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr(calibration, "StableCapacityRepository", CapacityRepo)
    monkeypatch.setattr(calibration, "NetworkWriteFencing", Fence)
    monkeypatch.setattr(calibration, "resolve_service_profile_for_policy", lambda _policy: type("Profile", (), {
        "src_host": "h_research", "dst_host": "h_dc", "protocol": type("Protocol", (), {"value": "UDP"})(),
        "dst_port": 5004, "dscp": 46, "packet_size_bytes": 1200,
    })())
    store = Store()
    candidate = CalibrationCandidate(
        intent_id="INT-fresh", traffic_scope_key="TSK-fresh", staged_policy=object(),
        service_id="research_video", bottleneck_egress="link:a--b",
        requested_schedulable_mbps=30, topology_id="fresh-topology",
        path_information={}, trace_id="TRACE-fresh", created_at="now",
    )

    async def p4_reader(_profile): return {"status": "NO_MATCHING_ENTRY", "matching_entry_count": 0}

    with pytest.raises(calibration.CapacityCalibrationError, match="CALIBRATION_DOES_NOT_SUPPORT_CANDIDATE"):
        await ActiveIngressCapacityCalibrator(
            store, probe=Probe(), p4_reader=p4_reader,
        ).calibrate_candidate(
            candidate, samples=3, duration_seconds=1,
        )

    assert CapacityRepo.saved == []
    assert Fence.released == ["candidate-lease"]
    assert store.events[-1]["event_type"] == "CANDIDATE_CAPACITY_CALIBRATION_REJECTED"


@pytest.mark.asyncio
async def test_candidate_calibration_cleanup_on_probe_exception(monkeypatch):
    """A failing receiver probe cannot leave a lease or capacity evidence behind."""
    import infra.capacity_calibration as calibration

    class Store:
        def __init__(self): self.events = []
        def add_audit_event(self, **event): self.events.append(event)
    class Registry:
        def snapshot(self):
            return type("Snapshot", (), {"topology_name": "fresh-topology", "links": [
                type("Link", (), {"resource_id": "link:a--b", "capacity_mbps": 100})()
            ]})()
    class CapacityRepo:
        saved = []
        def __init__(self, _store): pass
        def put(self, record): self.saved.append(record); return record
    class Fence:
        released = []
        def __init__(self, _store): pass
        def acquire(self, **_kwargs):
            return {"lease_id": "candidate-lease", "owner_type": "PROVISIONAL_CAPACITY_CALIBRATION", "owner_id": "INT-fresh", "fencing_token": 9, "resource_scope": "GLOBAL_NETWORK_CONFIGURATION"}
        def authorize_network_configuration_write(self, **_kwargs): return {"authorized": True, "authorization_id": "candidate-auth"}
        def record_execution(self, *_args, **_kwargs): pass
        def release(self, lease_id): self.released.append(lease_id); return True
    class Probe:
        async def get(self, path, _query):
            if path == "/probe/iperf":
                raise TimeoutError("receiver iperf timeout")
            return {"status": "ok"}

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr(calibration, "StableCapacityRepository", CapacityRepo)
    monkeypatch.setattr(calibration, "NetworkWriteFencing", Fence)
    monkeypatch.setattr(calibration, "resolve_service_profile_for_policy", lambda _policy: type("Profile", (), {
        "src_host": "h_research", "dst_host": "h_dc", "protocol": type("Protocol", (), {"value": "UDP"})(),
        "dst_port": 5004, "dscp": 46, "packet_size_bytes": 1200,
    })())
    candidate = CalibrationCandidate(
        intent_id="INT-fresh", traffic_scope_key="TSK-fresh", staged_policy=object(),
        service_id="research_video", bottleneck_egress="link:a--b",
        requested_schedulable_mbps=30, topology_id="fresh-topology",
        path_information={}, trace_id="TRACE-fresh", created_at="now",
    )
    store = Store()

    async def p4_reader(_profile): return {"status": "NO_MATCHING_ENTRY", "matching_entry_count": 0}

    with pytest.raises(calibration.CapacityCalibrationError, match="CALIBRATION_PROBE_EXCEPTION"):
        await ActiveIngressCapacityCalibrator(store, probe=Probe(), p4_reader=p4_reader).calibrate_candidate(
            candidate, samples=3, duration_seconds=1,
        )

    assert CapacityRepo.saved == []
    assert Fence.released == ["candidate-lease"]
    assert store.events[-1]["observed"]["reason"] == "CALIBRATION_PROBE_EXCEPTION"


@pytest.mark.asyncio
async def test_candidate_calibration_cleanup_on_p4_evidence_failure(monkeypatch):
    """A failed P4Runtime readback is a fail-closed calibration failure."""
    import infra.capacity_calibration as calibration

    class Store:
        def __init__(self): self.events = []
        def add_audit_event(self, **event): self.events.append(event)
    class Registry:
        def snapshot(self):
            return type("Snapshot", (), {"topology_name": "fresh-topology", "links": [
                type("Link", (), {"resource_id": "link:a--b", "capacity_mbps": 100})()
            ]})()
    class CapacityRepo:
        saved = []
        def __init__(self, _store): pass
        def put(self, record): self.saved.append(record); return record
    class Fence:
        released = []
        def __init__(self, _store): pass
        def acquire(self, **_kwargs):
            return {"lease_id": "candidate-lease", "owner_type": "PROVISIONAL_CAPACITY_CALIBRATION", "owner_id": "INT-fresh", "fencing_token": 9, "resource_scope": "GLOBAL_NETWORK_CONFIGURATION"}
        def authorize_network_configuration_write(self, **_kwargs): return {"authorized": True, "authorization_id": "candidate-auth"}
        def record_execution(self, *_args, **_kwargs): pass
        def release(self, lease_id): self.released.append(lease_id); return True
    class Probe:
        async def get(self, _path, _query):
            return {"status": "ok", "received_mbps": 42.0}

    async def p4_reader(_profile):
        # ``read_p4_snapshot`` reports transport failures as a structured
        # READ_FAILED result rather than raising, so exercise the production
        # failure contract directly.
        return {"status": "READ_FAILED", "reason_code": "GRPC_UNAVAILABLE"}

    monkeypatch.setattr("infra.resource_registry.get_resource_registry", lambda: Registry())
    monkeypatch.setattr(calibration, "StableCapacityRepository", CapacityRepo)
    monkeypatch.setattr(calibration, "NetworkWriteFencing", Fence)
    monkeypatch.setattr(calibration, "resolve_service_profile_for_policy", lambda _policy: type("Profile", (), {
        "src_host": "h_research", "dst_host": "h_dc", "protocol": type("Protocol", (), {"value": "UDP"})(),
        "dst_port": 5004, "dscp": 46, "packet_size_bytes": 1200,
    })())
    candidate = CalibrationCandidate(
        intent_id="INT-fresh", traffic_scope_key="TSK-fresh", staged_policy=object(),
        service_id="research_video", bottleneck_egress="link:a--b",
        requested_schedulable_mbps=30, topology_id="fresh-topology",
        path_information={}, trace_id="TRACE-fresh", created_at="now",
    )
    store = Store()

    with pytest.raises(calibration.CapacityCalibrationError, match="CALIBRATION_P4_EVIDENCE_UNAVAILABLE"):
        await ActiveIngressCapacityCalibrator(store, probe=Probe(), p4_reader=p4_reader).calibrate_candidate(
            candidate, samples=3, duration_seconds=1,
        )

    assert CapacityRepo.saved == []
    assert Fence.released == ["candidate-lease"]
    assert store.events[-1]["observed"]["reason"] == "CALIBRATION_P4_EVIDENCE_UNAVAILABLE"

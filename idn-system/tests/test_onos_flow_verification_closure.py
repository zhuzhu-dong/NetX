"""POST-V3 regressions for canonical ONOS flow ownership verification."""
from __future__ import annotations

import asyncio

import pytest

from api.qos import verify_p4_classifier
from mcp_servers.onos_server import canonicalize_onos_device_id, onos_device_path_segment


def _flow():
    return {
        "id": "runtime-only-id", "deviceId": "device:bmv2:5", "state": "ADDED",
        "priority": 101, "appId": "org.onosproject.rest", "packets": 0, "bytes": 0,
        "selector": {"criteria": [{"matches": [
            {"field": "hdr.ipv4.srcAddr", "match": "ternary", "value": "c0a80a0a", "mask": "ffffffff"},
            {"field": "hdr.ipv4.dstAddr", "match": "ternary", "value": "0a00000a", "mask": "ffffffff"},
            {"field": "hdr.ipv4.protocol", "match": "ternary", "value": "11", "mask": "ff"},
            {"field": "hdr.udp.dstPort", "match": "ternary", "value": "138c", "mask": "ffff"},
        ]}]},
        "treatment": {"instructions": [{"subtype": "ACTION", "actionId": "MyIngress.set_dscp", "actionParams": {"dscp_value": "2e"}}]},
    }


def test_onos_device_id_transport_encoding_is_canonical_and_not_double_encoded():
    assert canonicalize_onos_device_id("device%3Abmv2%3A5") == "device:bmv2:5"
    assert onos_device_path_segment("device:bmv2:5") == "device%3Abmv2%3A5"
    assert onos_device_path_segment("of:0000000000000001") == "of%3A0000000000000001"


@pytest.mark.asyncio
async def test_scoped_404_uses_global_inventory_semantic_fallback(monkeypatch, yang_policy_factory):
    policy = yang_policy_factory(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32")

    class Client:
        async def get_flows(self, device_id=None):
            if device_id is not None:
                raise RuntimeError("HTTP 404 scoped endpoint")
            return {"flows": [_flow()]}
        async def close(self): pass

    import mcp_servers.onos_server
    monkeypatch.setattr(mcp_servers.onos_server, "ONOSClient", Client)
    result = await verify_p4_classifier(policy, {"qos_enforcement": {"qos_binding": {"classifier_device_id": "device:bmv2:5"}}})
    detail = result["onos_flow_verification"]
    assert detail["control_plane_verified"] is True
    assert detail["verification_method"] == "global_fallback"
    assert detail["expected_flow_semantic_hash"] == detail["observed_flow_semantic_hash"]


@pytest.mark.asyncio
async def test_missing_global_flow_never_becomes_control_plane_success(monkeypatch, yang_policy_factory):
    policy = yang_policy_factory(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32")

    class Client:
        async def get_flows(self, device_id=None):
            if device_id is not None:
                raise RuntimeError("HTTP 404")
            return {"flows": []}
        async def close(self): pass

    import mcp_servers.onos_server
    monkeypatch.setattr(mcp_servers.onos_server, "ONOSClient", Client)
    monkeypatch.setenv("ONOS_FLOW_VERIFY_TIMEOUT_SECONDS", "0.001")
    with pytest.raises(RuntimeError, match="ONOS_FLOW_NOT_YET_VISIBLE"):
        await verify_p4_classifier(policy, {"qos_enforcement": {"qos_binding": {"classifier_device_id": "device:bmv2:5"}}})


@pytest.mark.asyncio
async def test_classifier_readback_budgets_stalled_shared_onos_calls(monkeypatch, yang_policy_factory):
    """A stale pooled request must fail within the classifier deadline.

    This prevents the verifier node's outer timeout from repeatedly cancelling
    an in-flight read and leaving an otherwise valid workflow in RECEIVED.
    """
    policy = yang_policy_factory()

    class SlowClient:
        async def get_flows(self, device_id=None):
            await asyncio.sleep(1)
            return {"flows": []}

        async def close(self):
            pass

    import mcp_servers.onos_server

    monkeypatch.setattr(mcp_servers.onos_server, "ONOSClient", SlowClient)
    monkeypatch.setenv("ONOS_FLOW_VERIFY_TIMEOUT_SECONDS", "0.02")
    monkeypatch.setenv("ONOS_FLOW_VERIFY_INTERVAL_SECONDS", "0.001")
    started = asyncio.get_running_loop().time()
    with pytest.raises(RuntimeError, match="ONOS_FLOW_NOT_YET_VISIBLE"):
        await verify_p4_classifier(
            policy,
            {"qos_enforcement": {"qos_binding": {"classifier_device_id": "device:bmv2:5"}}},
        )
    assert asyncio.get_running_loop().time() - started < 0.2

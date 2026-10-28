from __future__ import annotations

import pytest

from agents.network_sensing import _devices_from_merged_topology
from agents.policy_conflict_checker import PolicyConflictCheckerAgent
from infra.network_context_builder import NetworkContextBuilder
from mcp_servers.onos_server import ONOSDataTransformer
from models.network import DeviceInfo, DeviceStatus, HostInfo, NetworkSnapshot
from models.yang_policy import ActionRules, ActionType, MatchCriteria, NetworkPolicy, YANGPolicy


def _policy() -> YANGPolicy:
    return YANGPolicy(
        network_policy=NetworkPolicy(
            policy_id="POL-AVAILABILITY",
            policy_name="availability-test",
            match=MatchCriteria(source_ip="192.168.10.10/32", destination_ip="10.0.0.10/32"),
            action=ActionRules(action_type=ActionType.SET_PRIORITY),
        ),
        confidence_score=0.98,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, DeviceStatus.AVAILABLE), (False, DeviceStatus.UNAVAILABLE), ("true", DeviceStatus.AVAILABLE), ("false", DeviceStatus.UNAVAILABLE), (None, DeviceStatus.UNKNOWN)],
)
def test_onos_availability_normalization_preserves_unknown(raw, expected):
    payload = {"id": "of:0000000000000001"}
    if raw is not None:
        payload["available"] = raw
    result = ONOSDataTransformer.device_to_dict(payload)
    assert result["status"] == expected.value
    if expected == DeviceStatus.UNKNOWN:
        assert result["controller_available"] is None
        assert result["availability_reason_code"] == "DEVICE_AVAILABILITY_NOT_OBSERVED"


def test_mininet_topology_without_onos_availability_is_unknown_not_unavailable():
    devices = _devices_from_merged_topology({
        "source": "mininet_probe",
        "switches": [{"name": "s_access_r", "device_id": "device:bmv2:5"}],
        "onos": {"devices": []},
    })
    assert devices[0].status == DeviceStatus.UNKNOWN
    assert devices[0].controller_available is None


@pytest.mark.asyncio
async def test_unknown_requests_refresh_without_claiming_device_is_unavailable():
    snapshot = NetworkSnapshot(
        devices=[DeviceInfo(device_id="device:bmv2:5", status=DeviceStatus.UNKNOWN)],
        hosts=[HostInfo(host_id="h_research", mac="00:00:00:00:00:01", ip_addresses=["192.168.10.10"], location_device="device:bmv2:5")],
    )
    report = await PolicyConflictCheckerAgent().check(_policy(), snapshot)
    assert report.has_conflict is False
    assert report.context_refresh_required is True
    assert report.warning_items[0]["conflict_type"] == "NETWORK_CONTEXT_UNKNOWN"
    assert "不可用" not in report.warning_items[0]["description"]


@pytest.mark.asyncio
async def test_explicit_unavailable_required_device_is_infrastructure_block():
    snapshot = NetworkSnapshot(
        devices=[
            DeviceInfo(device_id="device:bmv2:5", status=DeviceStatus.UNAVAILABLE),
            DeviceInfo(device_id="device:bmv2:1", status=DeviceStatus.UNAVAILABLE),
        ],
        hosts=[HostInfo(host_id="h_research", mac="00:00:00:00:00:01", ip_addresses=["192.168.10.10"], location_device="device:bmv2:5")],
    )
    report = await PolicyConflictCheckerAgent().check(_policy(), snapshot)
    assert report.has_conflict is True
    assert report.blocking is True
    assert report.conflict_type == "INFRASTRUCTURE_UNAVAILABLE"
    assert report.conflict_items[0]["unavailable_devices"] == ["device:bmv2:5"]


def test_network_context_state_hash_changes_with_device_availability():
    def observation(status: str) -> dict:
        return {
            "ONOS": {
                "available": True,
                "trusted": True,
                "topology": {"nodes": [], "links": []},
                "inventory": {"devices": [{"device_id": "device:bmv2:5", "availability_status": status}]},
            }
        }

    builder = NetworkContextBuilder()
    available = builder.build(observation("AVAILABLE"))
    unavailable = builder.build(observation("UNAVAILABLE"))
    assert available.state_hash != unavailable.state_hash

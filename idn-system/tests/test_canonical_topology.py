from __future__ import annotations

import pytest

from api.baseline import _shortest_path
from api.topology import canonicalize_topology


def _probe_topology() -> dict:
    return {
        "status": "ok", "topology_name": "six-switch",
        "hosts": [
            {"name": "h_research", "ip": "192.168.10.10"},
            {"name": "h_dc", "ip": "10.0.0.10"},
        ],
        "switches": [
            {"name": "s_access_r", "device_id": "device:bmv2:5"},
            {"name": "s_agg_r", "device_id": "device:bmv2:3"},
            {"name": "s_core", "device_id": "device:bmv2:1"},
            {"name": "s_agg_dc", "device_id": "device:bmv2:4"},
            {"name": "s_access_dc", "device_id": "device:bmv2:6"},
        ],
        "links": [
            {"id": "research-access", "state": "ACTIVE", "endpoints": [
                {"node": "h_research", "type": "host", "interface": "h_research-eth0"},
                {"node": "s_access_r", "type": "switch", "device_id": "device:bmv2:5", "port": 1},
            ]},
            {"id": "access-agg", "state": "ACTIVE", "capacity_mbps": 100, "endpoints": [
                {"node": "s_access_r", "type": "switch", "device_id": "device:bmv2:5", "port": 2},
                {"node": "s_agg_r", "type": "switch", "device_id": "device:bmv2:3", "port": 2},
            ]},
            {"id": "agg-core", "state": "ACTIVE", "endpoints": [
                {"node": "s_agg_r", "type": "switch", "device_id": "device:bmv2:3", "port": 3},
                {"node": "s_core", "type": "switch", "device_id": "device:bmv2:1", "port": 3},
            ]},
            {"id": "core-agg", "state": "ACTIVE", "endpoints": [
                {"node": "s_core", "type": "switch", "device_id": "device:bmv2:1", "port": 4},
                {"node": "s_agg_dc", "type": "switch", "device_id": "device:bmv2:4", "port": 3},
            ]},
            {"id": "agg-access", "state": "ACTIVE", "endpoints": [
                {"node": "s_agg_dc", "type": "switch", "device_id": "device:bmv2:4", "port": 2},
                {"node": "s_access_dc", "type": "switch", "device_id": "device:bmv2:6", "port": 2},
            ]},
            {"id": "access-dc", "state": "ACTIVE", "endpoints": [
                {"node": "s_access_dc", "type": "switch", "device_id": "device:bmv2:6", "port": 1},
                {"node": "h_dc", "type": "host", "interface": "h_dc-eth0"},
            ]},
        ],
    }


def test_mininet_probe_adapter_preserves_runtime_path_and_device_identity():
    canonical = canonicalize_topology(_probe_topology())

    assert canonical["schema"] == "idn.canonical_topology.v1"
    assert _shortest_path(canonical, "h_research", "h_dc")[0] == "h_research"
    access = next(node for node in canonical["nodes"] if node["runtime_name"] == "s_access_r")
    assert access["device_id"] == "device:bmv2:5"


def test_onos_snapshot_adapter_uses_the_same_canonical_graph_contract():
    canonical = canonicalize_topology({
        "status": "ok",
        "onos": {"devices": [{"id": "device:bmv2:5"}, {"id": "device:bmv2:3"}], "links": [{
            "src": {"device": "device:bmv2:5", "port": "2"},
            "dst": {"device": "device:bmv2:3", "port": "2"},
        }]},
    })
    assert canonical["links"][0]["endpoints"][0]["node_id"] == "device:bmv2:5"
    assert canonical["links"][0]["endpoints"][1]["device_id"] == "device:bmv2:3"


def test_recovery_payload_adapter_is_idempotent_and_keeps_path_resolvable():
    initial = canonicalize_topology(_probe_topology())
    recovered = canonicalize_topology({"canonical": initial})
    assert recovered == initial
    assert _shortest_path(recovered, "h_research", "h_dc")[-1] == "h_dc"


def test_unknown_endpoint_is_a_no_path_result_not_a_schema_failure():
    canonical = canonicalize_topology(_probe_topology())
    with pytest.raises(ValueError, match="NO_PATH"):
        _shortest_path(canonical, "missing-host", "h_dc")

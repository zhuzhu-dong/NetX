from __future__ import annotations

import sys
from pathlib import Path


_DEFAULT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = next(
    (candidate for candidate in (_DEFAULT_ROOT, Path("/workspace")) if (candidate / "mininet").is_dir()),
    _DEFAULT_ROOT,
)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mininet.topo_loader import load_config


def test_research_dc_host_identity_and_ports_are_deterministic():
    topo = load_config(PROJECT_ROOT / "mininet" / "topologies" / "research_dc.yaml")

    assert {host.name: host.mac for host in topo.hosts} == {
        "h_research": "02:00:00:00:00:11",
        "h_video": "02:00:00:00:00:12",
        "h_ai": "02:00:00:00:00:13",
        "h_dc": "02:00:00:00:00:14",
        "h_backup": "02:00:00:00:00:15",
    }
    assert {item["host"]: (item["switch"], item["port"]) for item in topo.host_attachments} == {
        "h_research": ("s_access_r", 1),
        "h_video": ("s_access_r", 2),
        "h_ai": ("s_agg_r", 1),
        "h_dc": ("s_access_d", 2),
        "h_backup": ("s_access_d", 3),
    }
    assert topo.host_facing_ports == {
        "device:bmv2:3": [1],
        "device:bmv2:5": [1, 2],
        "device:bmv2:6": [2, 3],
    }


def test_research_dc_netcfg_and_capacity_come_from_yaml():
    topo = load_config(PROJECT_ROOT / "mininet" / "topologies" / "research_dc.yaml")

    assert topo.host_by_name["h_dc"].ip == "10.0.0.10/24"
    assert topo.subnets["dc"].network == "10.0.0.0/24"
    assert max(link.bw_mbps or 0 for link in topo.links) <= 1000
    bottleneck = next(link for link in topo.links if link.link_id == "s_access_r-s_agg_r")
    assert bottleneck.bw_mbps == 100

    interfaces = topo.build_interface_config()["ports"]
    assert set(interfaces) == {
        "device:bmv2:5/1",
        "device:bmv2:5/2",
        "device:bmv2:3/1",
        "device:bmv2:6/2",
        "device:bmv2:6/3",
    }
    hosts = topo.build_host_config()["hosts"]
    assert len(hosts) == 5
    assert hosts["02:00:00:00:00:14/None"]["basic"] == {
        "name": "h_dc",
        "ips": ["10.0.0.10"],
        "locations": ["device:bmv2:6/2"],
    }

    inventory = topo.build_host_inventory()
    assert len(inventory) == 5
    assert inventory[0] == {
        "mac": "02:00:00:00:00:11",
        "vlan": "-1",
        "ipAddresses": ["192.168.10.10"],
        "locations": [{"elementId": "device:bmv2:5", "port": "1"}],
    }
    assert inventory[3]["locations"] == [
        {"elementId": "device:bmv2:6", "port": "2"}
    ]

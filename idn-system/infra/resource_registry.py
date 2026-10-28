"""Single source of truth for Mininet, ONOS, P4Runtime and Linux IDs."""
from __future__ import annotations

import os
import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from models.resources import (
    DeviceResource,
    EndpointResource,
    InterfaceResource,
    LinkResource,
    ResourceRegistrySnapshot,
)


def _topology_path() -> Path:
    explicit = os.getenv("IDN_TOPOLOGY_FILE")
    if explicit:
        return Path(explicit)
    topology_name = os.getenv("TOPO_NAME", "research_dc")
    # In development, /workspace contains the complete repository.  In the
    # production image only /app is present, so the image-owned topology copy
    # must be searched first.  Keeping both roots preserves the existing dev
    # workflow without making fresh image startup depend on a bind mount.
    roots = [Path(__file__).resolve().parents[1], Path("/workspace"), Path(__file__).resolve().parents[2]]
    for root in roots:
        candidate = root / "mininet" / "topologies" / f"{topology_name}.yaml"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"topology {topology_name!r} is unavailable; set IDN_TOPOLOGY_FILE"
    )


@lru_cache(maxsize=4)
def _build(path_value: str, mtime_ns: int) -> ResourceRegistrySnapshot:
    del mtime_ns
    path = Path(path_value)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    topology_name = str(data.get("name") or path.stem)
    pipeline = str(data.get("p4_pipeline") or "unknown")
    link_profiles = dict(data.get("link_profiles") or {})
    default_profile = str(data.get("default_link_profile") or "default")
    switches = list(data.get("switches") or [])
    hosts = list(data.get("hosts") or [])
    subnets = dict(data.get("subnets") or {})
    raw_links = list(data.get("links") or [])
    switch_names = {str(item["name"]) for item in switches}

    port_counters: dict[str, int] = {}
    port_by_edge: dict[tuple[str, str], int] = {}
    for link in raw_links:
        a, b = str(link["a"]), str(link["b"])
        for node, peer in ((a, b), (b, a)):
            port_counters[node] = port_counters.get(node, 0) + 1
            port_by_edge[(node, peer)] = port_counters[node]

    devices: list[DeviceResource] = []
    interfaces: list[InterfaceResource] = []
    endpoints: list[EndpointResource] = []
    links: list[LinkResource] = []
    aliases: dict[str, str] = {}

    for item in switches:
        name = str(item["name"])
        node_id = int(item["device_id"])
        resource_id = f"device:bmv2:{node_id}"
        device = DeviceResource(
            resource_id=resource_id,
            logical_device_id=resource_id,
            switch_name=name,
            p4runtime_endpoint=f"mininet:{50050 + node_id}",
            p4runtime_node_id=int(item.get("p4runtime_node_id") or 1),
            node_id=node_id,
            role=str(item.get("role") or "switch"),
            pipeline=pipeline,
            ports=[f"{name}-eth{index}" for index in range(1, port_counters.get(name, 0) + 1)],
        )
        devices.append(device)
        for alias in (name, resource_id, f"node_id:{node_id}", device.p4runtime_endpoint):
            aliases[alias] = resource_id

    device_by_name = {item.switch_name: item.resource_id for item in devices}
    host_by_name = {str(item["name"]): item for item in hosts}
    for host in hosts:
        name = str(host["name"])
        subnet_name = str(host.get("subnet") or "")
        subnet = dict(subnets.get(subnet_name) or {})
        if not subnet_name or not subnet:
            raise ValueError(f"host {name} references unknown subnet {subnet_name!r}")
        host_interface = ipaddress.ip_interface(str(host["ip"]))
        gateway_ip = str(subnet.get("gateway_ip") or "")
        gateway_mac = str(subnet.get("gateway_mac") or "")
        if not gateway_ip or not gateway_mac:
            raise ValueError(f"subnet {subnet_name} has incomplete gateway metadata")
        attachment = next(
            (link for link in raw_links if name in {str(link["a"]), str(link["b"])}), None
        )
        if attachment is None:
            raise ValueError(f"host {name} has no attachment link")
        peer = str(attachment["b"] if str(attachment["a"]) == name else attachment["a"])
        attached_device = device_by_name[peer]
        endpoint_id = f"endpoint:{name}"
        endpoints.append(EndpointResource(
            resource_id=endpoint_id, host_name=name,
            ip_address=str(host_interface.ip), ip_prefix_length=host_interface.network.prefixlen,
            mac_address=str(host["mac"]), subnet_name=subnet_name,
            gateway_ip=gateway_ip, gateway_mac=gateway_mac,
            namespace=name, attached_device=attached_device,
            attached_port=port_by_edge[(peer, name)],
        ))
        for alias in (name, endpoint_id, str(host["ip"]).split("/", 1)[0], str(host["mac"])):
            aliases[alias] = endpoint_id

    for raw in raw_links:
        a, b = str(raw["a"]), str(raw["b"])
        profile = dict(link_profiles.get(str(raw.get("profile") or default_profile)) or {})
        capacity = float(raw.get("bw_mbps") or profile.get("bw_mbps") or 0)
        if capacity <= 0:
            raise ValueError(f"link {a}-{b} has no positive capacity")
        delay = float(raw.get("delay_ms") or profile.get("delay_ms") or 0)
        physical_id = "link:" + "--".join(sorted((a, b)))

        def interface_id(node: str, peer: str) -> str:
            return f"interface:{node}:{port_by_edge[(node, peer)]}"

        a_interface, b_interface = interface_id(a, b), interface_id(b, a)
        if a in switch_names:
            interfaces.append(InterfaceResource(
                resource_id=a_interface, device_id=device_by_name[a], peer_resource_id=(
                    device_by_name[b] if b in switch_names else f"endpoint:{b}"
                ), linux_interface=f"{a}-eth{port_by_edge[(a, b)]}",
                onos_port=port_by_edge[(a, b)], p4_port=port_by_edge[(a, b)],
                capacity_mbps=capacity,
            ))
        if b in switch_names:
            interfaces.append(InterfaceResource(
                resource_id=b_interface, device_id=device_by_name[b], peer_resource_id=(
                    device_by_name[a] if a in switch_names else f"endpoint:{a}"
                ), linux_interface=f"{b}-eth{port_by_edge[(b, a)]}",
                onos_port=port_by_edge[(b, a)], p4_port=port_by_edge[(b, a)],
                capacity_mbps=capacity,
            ))
        links.append(LinkResource(
            resource_id=physical_id,
            physical_link_id=physical_id,
            src_interface_id=a_interface,
            dst_interface_id=b_interface,
            directed_link_ids=[f"{physical_id}:{a}->{b}", f"{physical_id}:{b}->{a}"],
            capacity_mbps=capacity, delay_ms=delay,
            loss_pct=float(raw.get("loss_pct") or profile.get("loss_pct") or 0),
            cost=float(raw.get("cost") or 1), role=str(raw.get("role") or "default"),
        ))
        aliases[f"{a}-{b}"] = physical_id
        aliases[f"{b}-{a}"] = physical_id
        aliases[physical_id] = physical_id
        aliases[a_interface] = a_interface
        aliases[b_interface] = b_interface

    return ResourceRegistrySnapshot(
        schema_version="1.0", topology_name=topology_name, endpoints=endpoints,
        devices=devices, interfaces=interfaces, links=links, aliases=aliases,
    )


class ResourceRegistry:
    def __init__(self, path: Path | None = None):
        self.path = path or _topology_path()

    def snapshot(self) -> ResourceRegistrySnapshot:
        return _build(str(self.path), self.path.stat().st_mtime_ns)

    def resolve(self, alias: str) -> str:
        snapshot = self.snapshot()
        try:
            return snapshot.aliases[str(alias)]
        except KeyError as exc:
            raise KeyError(f"unknown resource alias: {alias}") from exc

    def get_link(self, alias: str) -> LinkResource:
        resource_id = self.resolve(alias)
        match = next((item for item in self.snapshot().links if item.resource_id == resource_id), None)
        if match is None:
            raise KeyError(f"resource is not a link: {alias}")
        return match

    def interface_for(self, device_alias: str, port: int) -> InterfaceResource:
        device_id = self.resolve(device_alias)
        match = next(
            (item for item in self.snapshot().interfaces
             if item.device_id == device_id and item.p4_port == int(port)), None,
        )
        if match is None:
            raise KeyError(f"unknown interface: {device_alias}/{port}")
        return match


_registry = ResourceRegistry()


def get_resource_registry() -> ResourceRegistry:
    return _registry

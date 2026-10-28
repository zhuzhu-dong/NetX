"""Topology loader — build Mininet/BMv2 topologies from YAML descriptions.

Single source of truth:  mininet/topologies/<name>.yaml
All derived configs (SWITCH_CONFIG, gateway interfaces, ARP proxy rules,
default routes) are auto-generated here.  No topology-specific values should
be hardcoded in this file.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── P4 table ID lookup (reads generated p4info.env; no p4runtime dependency) ──

def _load_p4info_env() -> dict[str, int]:
    """Parse p4/p4info.env into a {NAME: id} dict. Falls back to defaults."""
    env_path = Path(__file__).resolve().parent.parent / "p4" / "p4info.env"
    ids: dict[str, int] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                try:
                    ids[key] = int(val)
                except ValueError:
                    pass
    return ids

_P4INFO_IDS = _load_p4info_env()


def configure_dataplane_buffers(net) -> list[dict[str, Any]]:
    """Apply bounded host buffer, queue, and TCP pacing settings.

    TCLink creates host access links as ``HTB -> pfifo``.  A pfifo leaf does
    not honour Linux ``SO_MAX_PACING_RATE``, so an iperf TCP sender using
    ``--fq-rate`` still emits line-rate bursts.  Across the multi-hop BMv2
    fabric that produced retransmission storms and an unfinished receiver
    summary even though the packets and ACKs were visible at both hosts.
    Replace only the leaf with FQ; the TCLink root/class rate remains the
    topology-defined physical limit and policy HTB is still installed solely
    on switch egress interfaces.
    """
    settings = {
        "net.core.rmem_default": int(os.getenv("MININET_UDP_RMEM_DEFAULT", "1048576")),
        "net.core.wmem_default": int(os.getenv("MININET_UDP_WMEM_DEFAULT", "1048576")),
        "net.ipv4.udp_rmem_min": int(os.getenv("MININET_UDP_RMEM_MIN", "65536")),
        "net.ipv4.udp_wmem_min": int(os.getenv("MININET_UDP_WMEM_MIN", "65536")),
    }
    queue_len = int(os.getenv("MININET_TX_QUEUE_LEN", "1000"))
    results = []
    for name, node in net.nameToNode.items():
        if not name.startswith("h_"):
            continue
        for key, value in settings.items():
            before = node.cmd(f"sysctl -n {key} 2>/dev/null").strip()
            output = node.cmd(f"sysctl -w {key}={value} 2>&1").strip()
            after = node.cmd(f"sysctl -n {key} 2>/dev/null").strip()
            results.append({"node": name, "setting": key, "before": before, "after": after, "command": output})
    for name, node in net.nameToNode.items():
        if not name.startswith("h_"):
            continue
        for intf in node.intfList():
            if intf.name == "lo":
                continue
            node.cmd(f"ip link set dev {intf.name} txqueuelen {queue_len} 2>&1")
            # The host side of a TCLink uses handle 5: / class 5:1.  FQ is a
            # leaf-only replacement, so it cannot overwrite a policy HTB
            # resource (those are deliberately forbidden on host interfaces).
            output = node.cmd(
                f"tc qdisc replace dev {intf.name} parent 5:1 handle 10: fq 2>&1"
            ).strip()
            results.append({
                "node": name,
                "setting": "host_tcp_pacing_leaf",
                "interface": intf.name,
                "qdisc": "fq",
                "command": output,
            })
    print(f"[dataplane] host buffers, FQ TCP pacing leaves, and txqueuelen={queue_len} configured")
    return results

def _p4_table_flow_type(name: str) -> int:
    """Return the ONOS flow-type table ID for a P4 table name."""
    var_name = "P4_TABLE_" + name.replace(".", "_").upper()
    if var_name in _P4INFO_IDS:
        return _P4INFO_IDS[var_name]
    raise KeyError(
        f"Table '{name}' (var: {var_name}) not found in p4info.env. "
        "Run 'p4/compile.sh' to regenerate."
    )

try:
    from . import probe_server
except ImportError:  # /app/mn entry-point execution
    from mn import probe_server


# ── Data classes — parsed YAML structure ──────────────────────────────────────


@dataclass
class SubnetDef:
    name: str
    network: str
    gateway_ip: str
    gateway_mac: str


@dataclass
class SwitchDef:
    name: str
    device_id: int
    role: str
    router_mac: str

    @property
    def grpc_port(self) -> int:
        return 50050 + self.device_id

    @property
    def device_uri(self) -> str:
        return f"device:bmv2:{self.device_id}"


@dataclass
class HostDef:
    name: str
    ip: str          # CIDR, e.g. "192.168.10.10/24"
    mac: str
    subnet_name: str  # references SubnetDef.name


@dataclass
class LinkDef:
    a: str
    b: str
    role: str = "primary"
    cost: float = 1.0
    bw_mbps: int | None = None
    delay_ms: float | None = None
    profile: str = "default"

    @property
    def link_id(self) -> str:
        return f"{self.a}-{self.b}"


@dataclass
class TopoConfig:
    name: str
    p4_pipeline: str
    subnets: dict[str, SubnetDef] = field(default_factory=dict)
    switches: list[SwitchDef] = field(default_factory=list)
    hosts: list[HostDef] = field(default_factory=list)
    links: list[LinkDef] = field(default_factory=list)

    # ── Derived lookups ──

    @property
    def switch_by_name(self) -> dict[str, SwitchDef]:
        return {s.name: s for s in self.switches}

    @property
    def host_by_name(self) -> dict[str, HostDef]:
        return {h.name: h for h in self.hosts}

    @property
    def subnet_by_name(self) -> dict[str, SubnetDef]:
        return self.subnets

    @property
    def switch_config(self) -> dict[str, dict]:
        """Generate the SWITCH_CONFIG dict for research_dc_topo compatibility."""
        return {
            s.name: {
                "device_id": s.device_id,
                "grpc_port": s.grpc_port,
                "device_uri": s.device_uri,
                "router_mac": s.router_mac,
            }
            for s in self.switches
        }

    @property
    def switch_name_by_id(self) -> dict[int, str]:
        """Generate the SWITCH_NAME_BY_ID dict for INT probe annotation."""
        return {s.device_id: s.name for s in self.switches}

    @property
    def host_ips(self) -> dict[str, str]:
        """Generate the HOST_IPS dict for Mininet host creation."""
        return {h.name: h.ip for h in self.hosts}

    @property
    def host_runtime_config(self) -> dict[str, dict[str, str]]:
        """Metadata exported by the Mininet probe for routing and UI users."""
        result: dict[str, dict[str, str]] = {}
        for host in self.hosts:
            subnet = self.gateway_for_host(host)
            result[host.name] = {
                "ip_cidr": host.ip,
                "mac": host.mac,
                "subnet": host.subnet_name,
                "gateway_ip": subnet.gateway_ip,
                "gateway_mac": subnet.gateway_mac,
            }
        return result

    @property
    def port_map(self) -> dict[tuple[str, str], int]:
        """Return ``(link_id, switch_name) -> BMv2 port`` (always 1-based).

        Mininet allocates ``s_name-eth1``, ``s_name-eth2``, ... in addLink()
        order.  Stratum's chassis config uses the same 1-based numbering.
        """
        counters = {switch.name: 0 for switch in self.switches}
        result: dict[tuple[str, str], int] = {}
        for link in self.links:
            for endpoint in (link.a, link.b):
                if endpoint not in counters:
                    continue
                counters[endpoint] += 1
                result[(link.link_id, endpoint)] = counters[endpoint]
        return result

    @property
    def switch_egress_mappings(self) -> list[dict[str, Any]]:
        """Derive every switch egress mapping from links and port order.

        Linux interface names are generated by Mininet from the same 1-based
        port number.  The probe later validates these names against the live
        interfaces before any TC command is issued.
        """
        mappings: list[dict[str, Any]] = []
        ports = self.port_map
        for link in self.links:
            for switch_name, peer in ((link.a, link.b), (link.b, link.a)):
                switch = self.switch_by_name.get(switch_name)
                if switch is None:
                    continue
                port = ports[(link.link_id, switch_name)]
                peer_switch = self.switch_by_name.get(peer)
                mappings.append({
                    "device_id": switch.device_uri,
                    "switch_name": switch_name,
                    "egress_port": port,
                    "linux_interface": f"{switch_name}-eth{port}",
                    "peer_device": peer,
                    "peer_device_id": peer_switch.device_uri if peer_switch else None,
                    "link_capacity_mbps": link.bw_mbps,
                    "link_id": link.link_id,
                    "role": link.role,
                    "cost": link.cost,
                })
        return mappings

    @property
    def host_attachments(self) -> list[dict[str, Any]]:
        """Derive the five host-facing connect points from link definitions."""
        attachments: list[dict[str, Any]] = []
        ports = self.port_map
        for link in self.links:
            host_name = link.a if link.a in self.host_by_name else link.b if link.b in self.host_by_name else None
            switch_name = link.a if link.a in self.switch_by_name else link.b if link.b in self.switch_by_name else None
            if not host_name or not switch_name:
                continue
            host = self.host_by_name[host_name]
            switch = self.switch_by_name[switch_name]
            subnet = self.gateway_for_host(host)
            attachments.append({
                "host": host.name,
                "switch": switch.name,
                "device_id": switch.device_uri,
                "port": ports[(link.link_id, switch.name)],
                "ip": host.ip,
                "mac": host.mac,
                "subnet": host.subnet_name,
                "gateway_ip": subnet.gateway_ip,
                "gateway_mac": subnet.gateway_mac,
            })
        return attachments

    @property
    def host_facing_ports(self) -> dict[str, list[int]]:
        ports: dict[str, list[int]] = {}
        for attachment in self.host_attachments:
            ports.setdefault(attachment["device_id"], []).append(attachment["port"])
        return {device: sorted(values) for device, values in ports.items()}

    @property
    def expected_ports(self) -> dict[str, int]:
        """Compute expected port count per device for gNMI discovery.

        Each link endpoint on a switch adds one port.  A switch with 3 links
        gets 3 ports.
        """
        counts: dict[str, int] = {}
        switch_names = {s.name for s in self.switches}
        for link in self.links:
            if link.a in switch_names:
                counts[link.a] = counts.get(link.a, 0) + 1
            if link.b in switch_names:
                counts[link.b] = counts.get(link.b, 0) + 1
        return {
            self.switch_by_name[name].device_uri: cnt
            for name, cnt in counts.items()
        }

    def gateway_for_host(self, host: HostDef) -> SubnetDef:
        return self.subnets[host.subnet_name]

    # ── ONOS configuration generators ──

    def build_interface_config(self) -> dict:
        """Build ONOS host-facing interface config (gateway IP/MAC per subnet).

        Each subnet's gateway is assigned to a switch port.  The specific
        port is determined by scanning link definitions: find the link
        where a host of this subnet connects to a switch.
        """
        iface_config: dict[str, dict] = {"ports": {}}
        for attachment in self.host_attachments:
            subnet = self.subnets[attachment["subnet"]]
            connect_point = f'{attachment["device_id"]}/{attachment["port"]}'
            iface_config["ports"][connect_point] = {
                "interfaces": [{
                    "name": f'{attachment["switch"]}-{attachment["host"]}',
                    "ips": [subnet.gateway_ip + "/" + subnet.network.split("/")[1]],
                    "mac": subnet.gateway_mac,
                }],
            }
        return iface_config

    def build_host_config(self) -> dict:
        """Build deterministic ONOS BasicHostConfig entries for real hosts only."""
        hosts: dict[str, dict[str, Any]] = {}
        for attachment in self.host_attachments:
            host_id = f'{attachment["mac"]}/None'
            hosts[host_id] = {
                "basic": {
                    "name": attachment["host"],
                    "ips": [attachment["ip"].split("/", 1)[0]],
                    "locations": [f'{attachment["device_id"]}/{attachment["port"]}'],
                }
            }
        return {"hosts": hosts}

    def build_host_inventory(self) -> list[dict[str, Any]]:
        """Build payloads for ONOS ``POST /hosts`` from authoritative YAML.

        BasicHostConfig annotates an existing host but does not create a Host
        entity. These payloads are posted after netcfg so ONOS has the real
        IPv4/MAC/location tuples even though proxy ARP stays in the data plane.
        """
        return [
            {
                "mac": attachment["mac"],
                "vlan": "-1",
                "ipAddresses": [attachment["ip"].split("/", 1)[0]],
                "locations": [
                    {
                        "elementId": attachment["device_id"],
                        "port": str(attachment["port"]),
                    }
                ],
            }
            for attachment in self.host_attachments
        ]

    def build_arp_proxy_rules(self) -> list[dict]:
        """Build ARP proxy flow rules for each subnet gateway.

        Uses the P4 table MyIngress.arp_proxy.
        """
        ARP_PROXY_TABLE = _p4_table_flow_type("MyIngress.arp_proxy")

        # Map subnet → switch device_uri (host-connected switch)
        subnet_to_device: dict[str, str] = {}
        for host in self.hosts:
            for link in self.links:
                if link.a == host.name and link.b in self.switch_by_name:
                    subnet_to_device[host.subnet_name] = self.switch_by_name[link.b].device_uri
                    break
                if link.b == host.name and link.a in self.switch_by_name:
                    subnet_to_device[host.subnet_name] = self.switch_by_name[link.a].device_uri
                    break

        rules = []
        for subnet_name, subnet in self.subnets.items():
            device_uri = subnet_to_device.get(subnet_name)
            if not device_uri:
                continue
            # Convert gateway IP to hex (e.g. 192.168.10.1 → c0a80a01)
            parts = subnet.gateway_ip.split(".")
            ip_hex = "".join(f"{int(p):02x}" for p in parts)
            mac_hex = subnet.gateway_mac.replace(":", "").replace("-", "")
            rules.append({
                "deviceId": device_uri,
                "tableId": ARP_PROXY_TABLE,
                "priority": 1000,
                "isPermanent": True,
                "treatment": {"instructions": [{
                    "type": "PROTOCOL_INDEPENDENT",
                    "subtype": "ACTION",
                    "actionId": "MyIngress.arp_reply",
                    "actionParams": {"target_mac": mac_hex},
                }], "deferred": []},
                "selector": {"criteria": [{"type": "PROTOCOL_INDEPENDENT",
                    "matches": [{"field": "hdr.arp.targetProtoAddr",
                                 "match": "exact",
                                 "value": ip_hex}]}]},
            })
        return rules

    def default_route_commands(self) -> dict[str, str]:
        """Return {host_name: "ip route replace default via <gateway_ip>"}."""
        routes = {}
        for host in self.hosts:
            subnet = self.gateway_for_host(host)
            routes[host.name] = f"ip route replace default via {subnet.gateway_ip}"
        return routes


# ── Topology loading ─────────────────────────────────────────────────────────


def _validated_mac(value: str, *, label: str) -> str:
    """Return a normalized, non-zero unicast MAC or raise a config error."""
    compact = str(value).replace(":", "").replace("-", "").lower()
    if len(compact) != 12:
        raise ValueError(f"{label} must be a 48-bit MAC address, got {value!r}")
    try:
        raw = bytes.fromhex(compact)
    except ValueError as exc:
        raise ValueError(f"{label} is not hexadecimal: {value!r}") from exc
    if raw == bytes(6):
        raise ValueError(f"{label} must not be 00:00:00:00:00:00")
    if raw[0] & 1:
        raise ValueError(f"{label} must be a unicast MAC address, got {value!r}")
    return ":".join(f"{part:02x}" for part in raw)


def _validate_topology(topo: TopoConfig) -> None:
    """Fail fast when addressing or topology metadata can create bad flows."""
    if len({switch.device_id for switch in topo.switches}) != len(topo.switches):
        raise ValueError("switch device_id values must be unique")
    if len({switch.name for switch in topo.switches}) != len(topo.switches):
        raise ValueError("switch names must be unique")
    if len({host.name for host in topo.hosts}) != len(topo.hosts):
        raise ValueError("host names must be unique")

    all_macs = [host.mac for host in topo.hosts]
    if len(set(all_macs)) != len(all_macs):
        raise ValueError("host MAC addresses must be unique")

    node_names = set(topo.switch_by_name) | set(topo.host_by_name)
    for link in topo.links:
        if link.a not in node_names or link.b not in node_names:
            raise ValueError(f"link {link.link_id} references an unknown node")
        if link.a == link.b:
            raise ValueError(f"link {link.link_id} is a self-link")
        if link.bw_mbps is not None and not 0 < link.bw_mbps <= 1000:
            raise ValueError(
                f"link {link.link_id} bandwidth {link.bw_mbps} Mbps is outside TCLink range 1..1000"
            )

    attachment_names = [item["host"] for item in topo.host_attachments]
    if sorted(attachment_names) != sorted(topo.host_by_name):
        raise ValueError("every host must have exactly one switch-facing access link")

    for host in topo.hosts:
        if host.subnet_name not in topo.subnets:
            raise ValueError(f"host {host.name} references unknown subnet {host.subnet_name}")
        interface = ipaddress.ip_interface(host.ip)
        subnet = ipaddress.ip_network(topo.subnets[host.subnet_name].network, strict=True)
        if interface.ip not in subnet or interface.network.prefixlen != subnet.prefixlen:
            raise ValueError(
                f"host {host.name} address {host.ip} must use subnet prefix {subnet}"
            )


def load_config(topo_path: str | Path) -> TopoConfig:
    """Load a topology description from JSON (pre-converted from YAML by compile.sh).

    Args:
        topo_path: Path to a JSON file in p4/build/topologies/ or mininet/topologies/.

    Returns:
        TopoConfig with all derived properties populated.
    """
    # Try JSON first, fall back to YAML (for dev convenience on host)
    if str(topo_path).endswith(".json"):
        with open(topo_path, encoding="utf-8") as f:
            raw = json.load(f)
    else:
        # Fallback: parse YAML directly (requires pyyaml on host)
        import yaml
        with open(topo_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

    # Parse subnets
    subnets = {}
    for name, data in raw.get("subnets", {}).items():
        subnets[name] = SubnetDef(
            name=name,
            network=data["network"],
            gateway_ip=data["gateway_ip"],
            gateway_mac=_validated_mac(data["gateway_mac"], label=f"subnet {name} gateway_mac"),
        )

    # Parse switches
    switches = [
        SwitchDef(
            name=s["name"],
            device_id=int(s["device_id"]),
            role=s["role"],
            router_mac=_validated_mac(s["router_mac"], label=f'switch {s["name"]} router_mac'),
        )
        for s in raw.get("switches", [])
    ]

    # Parse hosts
    hosts = [
        HostDef(
            name=h["name"],
            ip=h["ip"],
            mac=_validated_mac(h["mac"], label=f'host {h["name"]} mac'),
            subnet_name=h["subnet"],
        )
        for h in raw.get("hosts", [])
    ]

    # Parse links
    link_profiles = raw.get("link_profiles", {}) or {}
    default_profile = raw.get("default_link_profile", "default")
    if default_profile not in link_profiles:
        raise ValueError(f"default_link_profile {default_profile!r} is not defined")
    links = []
    for link_data in raw.get("links", []):
        profile_name = link_data.get("profile", default_profile)
        if profile_name not in link_profiles:
            raise ValueError(
                f"link {link_data.get('a')}-{link_data.get('b')} uses unknown profile {profile_name!r}"
            )
        profile = link_profiles[profile_name] or {}
        links.append(LinkDef(
            a=link_data["a"],
            b=link_data["b"],
            role=link_data.get("role", "primary"),
            cost=float(link_data.get("cost", 1.0)),
            bw_mbps=int(link_data.get("bw_mbps", profile.get("bw_mbps")))
                if link_data.get("bw_mbps", profile.get("bw_mbps")) is not None else None,
            delay_ms=float(link_data.get("delay_ms", profile.get("delay_ms")))
                if link_data.get("delay_ms", profile.get("delay_ms")) is not None else None,
            profile=profile_name,
        ))

    topo = TopoConfig(
        name=raw["name"],
        p4_pipeline=raw.get("p4_pipeline", "idn_basic"),
        subnets=subnets,
        switches=switches,
        hosts=hosts,
        links=links,
    )
    _validate_topology(topo)
    return topo


# ── Mininet topology builder ──────────────────────────────────────────────────


def _disable_ipv6_for_data_plane(host_objs: dict[str, Any], switch_objs: dict[str, Any]) -> None:
    """Disable IPv6 before Stratum starts, preventing transient NDP Packet-Ins."""
    for host_name, host in host_objs.items():
        output = host.cmd(
            "sysctl -q -w net.ipv6.conf.all.disable_ipv6=1; "
            "sysctl -q -w net.ipv6.conf.default.disable_ipv6=1; "
            "cat /proc/sys/net/ipv6/conf/all/disable_ipv6"
        ).strip()
        if not output.endswith("1"):
            raise RuntimeError(f"failed to disable IPv6 in host namespace {host_name}: {output}")

    # Switches share the container namespace, so change data-plane interfaces
    # individually instead of disabling IPv6 on Docker's management interface.
    for switch in switch_objs.values():
        for intf in switch.intfList():
            intf_name = getattr(intf, "name", "")
            if not intf_name or intf_name == "lo":
                continue
            output = switch.cmd(
                f"sysctl -q -w net.ipv6.conf.{intf_name}.disable_ipv6=1; "
                f"cat /proc/sys/net/ipv6/conf/{intf_name}/disable_ipv6"
            ).strip()
            if not output.endswith("1"):
                raise RuntimeError(f"failed to disable IPv6 on {intf_name}: {output}")
    print("[主机发现] IPv6 已在主机命名空间和交换机数据端口关闭")


def _run_cli_resilient(net, cli_cls) -> None:
    """Keep a command-level CLI error from tearing down the whole topology."""
    consecutive_failures = 0
    while True:
        try:
            cli_cls(net)
            return
        except KeyboardInterrupt:
            # Mininet normally catches this itself.  Keep this outer guard for
            # interrupts that escape while a host command is being monitored.
            print("\n[CLI] 命令已中断，拓扑保持运行；输入 exit/quit 才会停止网络。")
            consecutive_failures = 0
        except EOFError:
            print("[CLI] 标准输入已关闭，开始正常清理拓扑。")
            return
        except Exception:
            consecutive_failures += 1
            print("[CLI] 命令处理异常，拓扑保持运行；完整堆栈如下：", file=sys.stderr)
            traceback.print_exc()
            if consecutive_failures >= 3:
                raise RuntimeError("Mininet CLI failed three consecutive times")
            time.sleep(0.5)


def create_topo_from_config(
    topo: TopoConfig,
    *,
    p4_json: str,
    onos_ip: str,
    probe_port: int | None = None,
):
    """Create a Mininet/BMv2 topology from a TopoConfig.

    Args:
        topo: Parsed topology configuration.
        p4_json: Path to the compiled P4 JSON pipeline.
        onos_ip: ONOS controller IP address.
        probe_port: Override for the telemetry probe HTTP port.

    Returns:
        None — enters Mininet CLI (blocking). Caller is responsible for
        cleanup on exit.
    """
    from mininet.net import Mininet
    from mininet.link import TCLink
    from mininet.cli import CLI
    from mininet.log import setLogLevel
    try:
        from .bmv2_switch import BMv2Switch
    except ImportError:
        from mn.bmv2_switch import BMv2Switch

    setLogLevel("info")

    net = Mininet(controller=None)

    # 1. Add hosts
    host_objs: dict[str, Any] = {}
    for hdef in topo.hosts:
        host_objs[hdef.name] = net.addHost(hdef.name, ip=hdef.ip, mac=hdef.mac)

    # 2. Add BMv2 switches
    switch_config = topo.switch_config
    switch_objs: dict[str, Any] = {}
    for sdef in topo.switches:
        cfg = switch_config[sdef.name]
        switch_objs[sdef.name] = net.addSwitch(
            sdef.name,
            cls=BMv2Switch,
            cfg=cfg,
            p4_json=p4_json,
            onos_ip=onos_ip,
            dpid=f"{sdef.device_id:016x}",
            inNamespace=False,
        )

    print("[拓扑] 节点创建完成，开始建立链路...")

    # 3. Create links
    for link_def in topo.links:
        node_a = host_objs.get(link_def.a) or switch_objs[link_def.a]
        node_b = host_objs.get(link_def.b) or switch_objs[link_def.b]

        params: dict[str, Any] = {"cls": TCLink}
        bw = link_def.bw_mbps
        delay = link_def.delay_ms
        if bw is not None:
            params["bw"] = bw
        if delay is not None:
            params["delay"] = f"{delay}ms"

        link = net.addLink(node_a, node_b, **params)
        link.idn_metadata = {
            "id": link_def.link_id,
            "role": link_def.role,
            "bandwidth_mbps": bw,
            "delay_ms": delay,
            "cost": link_def.cost,
            "profile": link_def.profile,
        }

    print("[拓扑] 链路创建完成")

    # IPv4-only project: suppress link-local IPv6/NDP before switch processes
    # can emit those frames to the controller.
    _disable_ipv6_for_data_plane(host_objs, switch_objs)

    # 4. Start network
    probe_srv = None
    try:
        net.start()

        # 5. Configure host default routes
        routes = topo.default_route_commands()
        for host_name, route_cmd in routes.items():
            host_objs[host_name].cmd(route_cmd)

        # 6. Disable checksum offload (BMv2 is userspace)
        probe_server.disable_checksum_offload(net)
        configure_dataplane_buffers(net)
        print("[主机] 默认网关路由已配置，校验和卸载已关闭")

        # 7. Start the probe before ONOS retries so health checks and the IDN
        # bootstrap can observe the stable, fixed-MAC topology immediately.
        probe_srv = probe_server.start_probe_server(
            net,
            switch_config,
            topo.switch_name_by_id,
            probe_port,
            host_runtime_config=topo.host_runtime_config,
            host_facing_ports=topo.host_facing_ports,
        )

        # 8. ONOS config: devices, exact host-facing interfaces, real hosts,
        # and ARP proxy entries are all generated from the same YAML.
        _post_port_config_from_topo(onos_ip, topo)

        print("[拓扑] host-facing 端口映射：")
        for item in topo.host_attachments:
            print(
                f'  {item["host"]} -> {item["switch"]}/{item["port"]} '
                f'{item["ip"]} {item["mac"]}'
            )
        print(f"\n=== 拓扑就绪 ({topo.name}) ===")
        _run_cli_resilient(net, CLI)
    finally:
        if probe_srv is not None:
            probe_srv.shutdown()
            probe_srv.server_close()
        net.stop()


# ── ONOS configuration (YAML-driven) ──────────────────────────────────────────


def _post_port_config_from_topo(onos_ip: str, topo: TopoConfig) -> None:
    """Provision ONOS devices, interfaces, hosts, and ARP proxy rules.

    Every object is derived from the same topology YAML. Host inventory uses
    the ONOS REST host provider in addition to BasicHostConfig because netcfg
    alone only annotates hosts that a provider has already discovered.
    """
    import base64
    import urllib.request
    import urllib.error

    ONOS_API = f"http://{onos_ip}:8181/onos/v1"
    AUTH = base64.b64encode(b"onos:rocks").decode()

    def _req(method, path, data=None):
        url = f"{ONOS_API}{path}"
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Basic {AUTH}")
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            print(f"  [netcfg] HTTP {e.code} on {method} {path}: {e.reason}")
            return None
        except Exception as e:
            print(f"  [netcfg] {type(e).__name__} on {method} {path}: {e}")
            return None

    expected = topo.expected_ports
    total_devices = len(expected)

    # ── Step 0: Provision devices in ONOS ──────────────────────────────
    print("[netcfg] Provisioning devices in ONOS...")
    devices_cfg = {
        "devices": {
            sw.device_uri: {
                "basic": {
                    "driver": "stratum-bmv2",
                    "managementAddress": f"grpc://mininet:{sw.grpc_port}?device_id=1",
                    "pipeconf": "org.onosproject.pipelines.idn-basic",
                }
            }
            for sw in topo.switches
        }
    }
    for attempt in range(1, 11):
        result = _req("POST", "/network/configuration/", devices_cfg)
        if result is not None:
            # Verify the config was actually stored (ONOS may return 200
            # before the netcfg subsystem is ready to persist).
            verify = _req("GET", "/network/configuration/")
            if verify and len(verify.get("devices", {})) >= len(topo.switches):
                print(f"[netcfg] {len(topo.switches)} devices provisioned (attempt {attempt}).")
                break
            print(f"[netcfg] Config not persisted yet (attempt {attempt}), retrying...")
        else:
            print(f"[netcfg] Retry {attempt}/10 in 3s...")
        time.sleep(3)
    else:
        print("[netcfg] WARNING: Failed to provision devices after 10 attempts")
    time.sleep(5)

    # Wait for gNMI port discovery
    print("[netcfg] Waiting for gNMI port discovery (stratum_bmv2)...")
    for _ in range(30):
        all_ok = True
        for device_id, expected_count in expected.items():
            data = _req("GET", f"/devices/{device_id}/ports")
            if not data or len(data.get("ports", [])) < expected_count:
                all_ok = False
                break
        if all_ok:
            print(f"[netcfg] gNMI port discovery complete on all {total_devices} devices")
            break
        time.sleep(2)
    else:
        print("[netcfg] WARNING: gNMI port discovery timeout")

    # Push host-facing interface config
    iface_config = topo.build_interface_config()
    if iface_config.get("ports"):
        print("[netcfg] Pushing host interface config...")
        if _req("POST", "/network/configuration/", iface_config) is not None:
            print("[netcfg] Host interface config pushed.")
        else:
            print("[netcfg] WARNING: Host interface config was not accepted")

    # Push the exact five real hosts.  P4 proxy ARP answers gateway requests in
    # the data plane, so BasicHostConfig supplies ONOS with the authoritative
    # IPv4/MAC/location tuple without learning transient switch-port identities.
    host_config = topo.build_host_config()
    if host_config.get("hosts"):
        print("[netcfg] Pushing deterministic host config...")
        if _req("POST", "/network/configuration/", host_config) is not None:
            print(f'[netcfg] {len(host_config["hosts"])} real host annotations configured.')
        else:
            print("[netcfg] WARNING: Host config was not accepted")

    # BasicHostConfig does not itself create Host entities. Inject the same
    # authoritative tuples through ONOS's REST host provider. Existing IDs are
    # skipped, making Mininet restarts stable and idempotent.
    host_inventory = topo.build_host_inventory()
    if host_inventory:
        print("[host-provider] Reconciling deterministic IPv4 host inventory...")
        current = _req("GET", "/hosts") or {}
        existing_ids = {item.get("id") for item in current.get("hosts", [])}
        accepted = 0
        for host in host_inventory:
            host_id = f'{host["mac"]}/None'
            if host_id in existing_ids:
                accepted += 1
                continue
            if _req("POST", "/hosts", host) is not None:
                accepted += 1

        verified = _req("GET", "/hosts") or {}
        expected_ids = {f'{host["mac"]}/None' for host in host_inventory}
        actual_ids = {item.get("id") for item in verified.get("hosts", [])}
        present = len(expected_ids & actual_ids)
        if accepted == len(host_inventory) and present == len(host_inventory):
            print(f"[host-provider] {present}/{len(host_inventory)} real IPv4 hosts present in ONOS.")
        else:
            print(
                f"[host-provider] WARNING: accepted={accepted}/{len(host_inventory)}, "
                f"verified={present}/{len(host_inventory)}"
            )

    # Push ARP proxy rules
    arp_rules = topo.build_arp_proxy_rules()
    if arp_rules:
        print("[netcfg] Pushing arp_proxy gateway entries...")
        posted = 0
        for rule in arp_rules:
            dev = rule["deviceId"]
            if _req("POST", f"/flows/{dev}", rule) is not None:
                posted += 1
        print(f"[netcfg] arp_proxy entries pushed: {posted}/{len(arp_rules)}.")


# ── Root check ────────────────────────────────────────────────────────────────


def _check_root():
    if os.geteuid() != 0:
        print("错误: 需要 root 权限运行 (Mininet 需要创建网络命名空间)", file=sys.stderr)
        print("请使用: sudo python3 ...", file=sys.stderr)
        sys.exit(1)


# ── CLI entry ─────────────────────────────────────────────────────────────────


def resolve_topo_path(topo_name: str, topo_dir: Path | None = None) -> Path:
    """Resolve a topology name to its JSON (or YAML) file path.

    Prefers p4/build/topologies/<name>.json (generated by compile.sh),
    falls back to mininet/topologies/<name>.yaml (for dev convenience).
    """
    if topo_dir is None:
        topo_dir = Path(__file__).resolve().parent / "topologies"
    # Check for pre-compiled JSON first (generated by compile.sh alongside YAML)
    json_path = topo_dir / f"{topo_name}.json"
    if json_path.is_file():
        return json_path
    # Fallback to YAML source
    yaml_path = topo_dir / f"{topo_name}.yaml"
    if yaml_path.is_file():
        return yaml_path
    print(f"错误: 拓扑文件不存在: {yaml_path}", file=sys.stderr)
    available = {p.stem for p in topo_dir.glob("*.*") if p.suffix in (".json", ".yaml")}
    print(f"可用的拓扑: {sorted(available)}", file=sys.stderr)
    sys.exit(1)


def run_topo(
    topo_name: str = "research_dc",
    *,
    onos_ip: str = "127.0.0.1",
    p4_dir: str | None = None,
    probe_port: int | None = None,
    topo_dir: str | None = None,
):
    """Load a topology from YAML and start Mininet.

    Args:
        topo_name: Name of the topology (matches <name>.yaml in topologies/).
        onos_ip: ONOS controller IP.
        p4_dir: Path to compiled P4 artifacts (default: ../p4/build).
        probe_port: Telemetry HTTP port (default: env MININET_PROBE_PORT or 9001).
        topo_dir: Override topology search directory.
    """
    _check_root()

    # Resolve topology YAML
    topo_dir_path = Path(topo_dir) if topo_dir else None
    yaml_path = resolve_topo_path(topo_name, topo_dir_path)

    # Resolve P4 artifacts
    script_dir = Path(__file__).resolve().parent
    p4_dir_path = Path(p4_dir) if p4_dir else (script_dir / ".." / "p4" / "build").resolve()
    p4_json = str(p4_dir_path / "idn_basic.json")
    p4info = str(p4_dir_path / "idn_basic.p4info.txt")

    if not os.path.isfile(p4_json):
        print(f"错误: P4 JSON 不存在: {p4_json}", file=sys.stderr)
        print("请先运行: cd p4 && bash compile.sh", file=sys.stderr)
        sys.exit(1)

    print(f"拓扑:    {topo_name}")
    print(f"P4 JSON: {p4_json}")
    print(f"P4 Info: {p4info}")
    print(f"ONOS IP: {onos_ip}")
    print()

    # Load and build
    topo = load_config(yaml_path)
    try:
        print(f"=== 创建 Mininet 拓扑 ({topo.name}) ===")
        create_topo_from_config(topo, p4_json=p4_json, onos_ip=onos_ip, probe_port=probe_port)
    except KeyboardInterrupt:
        print("Mininet 收到终止中断，已执行正常清理。", file=sys.stderr)
    except Exception:
        print("Mininet 运行失败，完整异常堆栈如下：", file=sys.stderr)
        traceback.print_exc()
        raise


# ── Standalone execution ──────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="IDN-System Mininet 拓扑 (YAML驱动)")
    parser.add_argument("--topo", default="research_dc", help="拓扑名称 (对应 topologies/<name>.yaml)")
    parser.add_argument("--onos-ip", default="127.0.0.1", help="ONOS 控制器 IP 地址")
    parser.add_argument("--p4-dir", default=None, help="P4 编译产物目录 (默认: ../p4/build)")
    parser.add_argument("--probe-port", type=int, default=None, help="遥测探针 HTTP 端口")
    parser.add_argument("--list", action="store_true", help="列出所有可用的拓扑")
    args = parser.parse_args()

    if args.list:
        topo_dir = Path(__file__).resolve().parent / "topologies"
        names = sorted(p.stem for p in topo_dir.glob("*.yaml"))
        print("可用拓扑:")
        for n in names:
            print(f"  {n}")
        return

    run_topo(
        topo_name=args.topo,
        onos_ip=args.onos_ip,
        p4_dir=args.p4_dir,
        probe_port=args.probe_port,
    )


if __name__ == "__main__":
    main()

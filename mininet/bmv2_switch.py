"""BMv2 switch node for Mininet using stratum_bmv2.

stratum_bmv2 provides gNMI-based port discovery which eliminates the need
for manual port configuration via ONOS REST.  Each switch gets a chassis
config file and starts as an independent process within its Mininet namespace.
"""

import os
import signal
import subprocess
import time
from mininet.node import Switch

# stratum_bmv2 binary (from opennetworking/mn-stratum Docker image)
STRATUM_BIN = os.environ.get("STRATUM_BMV2_BIN", "/usr/bin/stratum_bmv2")


def resolve_cpu_affinity(switch_name, raw_mapping, cpu_count=None):
    """Return a validated taskset CPU list for one switch.

    ``BMV2_CPU_AFFINITY`` is a comma-separated deployment setting such as
    ``s_core1=0-1,s_core2=2-3``.  It deliberately lives outside the topology:
    changing host CPU allocation must not change device IDs, ports, or links.
    An invalid or undersized CPU allocation safely leaves the affected switch
    unpinned rather than starting it with an illegal taskset range.
    """
    if not raw_mapping:
        return None
    available = os.cpu_count() if cpu_count is None else cpu_count
    if not available or available < 1:
        return None
    entries: dict[str, str] = {}
    for item in raw_mapping.split(","):
        key, separator, value = item.strip().partition("=")
        if not separator or not key or not value:
            continue
        entries[key.strip()] = value.strip()
    cpu_spec = entries.get(switch_name)
    if not cpu_spec:
        return None
    selected: set[int] = set()
    try:
        for item in cpu_spec.split(","):
            start_text, dash, end_text = item.strip().partition("-")
            start = int(start_text)
            end = int(end_text) if dash else start
            if start < 0 or end < start:
                return None
            selected.update(range(start, end + 1))
    except ValueError:
        return None
    if not selected or max(selected) >= available:
        return None
    return ",".join(str(cpu) for cpu in sorted(selected))


class BMv2Switch(Switch):
    """Custom Mininet switch using stratum_bmv2.

    Wraps stratum_bmv2 as a Mininet-managed process.  Each switch gets a
    chassis config file derived from its Mininet interfaces.
    """

    def __init__(self, name, cfg, p4_json, onos_ip, **kwargs):
        Switch.__init__(self, name, **kwargs)
        self.device_id = cfg["device_id"]
        self.grpc_port = cfg["grpc_port"]
        self.p4_json = p4_json
        self.onos_ip = onos_ip
        self.process = None

    def start(self, controllers):
        # Docker mode: switches are already started inside containers
        if os.environ.get("BMV2_DOCKER_MODE") == "1":
            return

        self.cmd("ip link set lo up")

        chassis_lines = [
            f'description: "IDN {self.name} (device_id={self.device_id})"',
            'chassis { platform: PLT_P4_SOFT_SWITCH name: "bmv2" }',
            'nodes { id: 1 slot: 1 index: 1 }',
        ]
        port_id = 1
        for intf in self.intfs.values():
            if intf.name == "lo" or intf.IP():
                continue
            self.cmd(f"ip link set {intf.name} up")
            chassis_lines.append("singleton_ports {")
            chassis_lines.append(
                f'  id: {port_id}  name: "{intf.name}"  slot: 1  '
                f"port: {port_id}  channel: {port_id}"
            )
            chassis_lines.append("  speed_bps: 10000000000")
            chassis_lines.append("  config_params { admin_state: ADMIN_STATE_ENABLED }")
            chassis_lines.append("  node: 1")
            chassis_lines.append("}")
            port_id += 1

        chassis_path = f"/tmp/chassis_{self.name}.pb.txt"
        with open(chassis_path, "w") as f:
            f.write("\n".join(chassis_lines))

        cmd = [
            STRATUM_BIN,
            "-chassis_config_file", chassis_path,
            "-initial_pipeline", self.p4_json,
            "-forwarding_pipeline_configs_file", f"/tmp/pipeline_{self.name}",
            "-cpu_port", "255",
            "-external_stratum_urls", f"0.0.0.0:{self.grpc_port}",
            "-persistent_config_dir", "/tmp",
        ]

        affinity = resolve_cpu_affinity(
            self.name,
            os.environ.get("BMV2_CPU_AFFINITY"),
        )
        if affinity:
            cmd = ["taskset", "-c", affinity, *cmd]
            print(f"  [{self.name}] CPU affinity={affinity}")
        elif os.environ.get("BMV2_CPU_AFFINITY"):
            print(f"  [{self.name}] CPU affinity unavailable; starting safely unpinned")

        print(f"  [{self.name}] stratum_bmv2: gRPC={self.grpc_port} ports={port_id - 1}")
        log_file = open(f"/tmp/{self.name}.log", "w")
        self.process = self.popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )
        time.sleep(3)

    def stop(self, deleteIntfs=True):
        if self.process:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=3)
            except Exception:
                pass
            print(f"  [{self.name}] stopped")
        Switch.stop(self, deleteIntfs=deleteIntfs)

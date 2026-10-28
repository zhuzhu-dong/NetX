from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import yaml

from api.capacity_evidence import aggregate_capacity_evidence
from api.decision_report import _execution_plane
from api.int_analysis import analyze_int_probe
from api.int_quality import DEGRADED, TRUSTED, UNTRUSTED, evaluate_int_probe
from api.topology_paths import (
    BACKUP_PATH_IDENTICAL,
    PATH_DEVICE_MISSING,
    PATH_LINK_MISSING,
    TopologyPathRegistry,
    topology_paths,
)

_DEFAULT_ROOT = Path(__file__).resolve().parents[2]
ROOT = next(
    (
        candidate
        for candidate in (_DEFAULT_ROOT, Path("/workspace"))
        if (candidate / "docker-compose.app.yml").is_file()
    ),
    _DEFAULT_ROOT,
)
PATH_CONFIG = ROOT / "idn-system" / "config" / "topology_paths.json"
TOPOLOGY_YAML = ROOT / "mininet" / "topologies" / "research_dc.yaml"


def _runtime_topology(*, omit_device: str | None = None, omit_pair: tuple[str, str] | None = None):
    cfg = json.loads(PATH_CONFIG.read_text(encoding="utf-8"))
    switches = [
        {"device_id": device, "name": switch}
        for device, switch in cfg["device_switch_map"].items()
        if device != omit_device
    ]
    hosts = [{"name": name} for name in cfg["host_attachment_map"]]
    links = []
    for item in cfg["physical_links"]:
        pair = tuple(sorted((item["device_a"], item["device_b"])))
        if omit_pair and pair == tuple(sorted(omit_pair)):
            continue
        links.append({
            "state": "ACTIVE",
            "endpoints": [
                {"type": "switch", "device_id": item["device_a"], "port": item["port_a"]},
                {"type": "switch", "device_id": item["device_b"], "port": item["port_b"]},
            ],
        })
    return {"switches": switches, "hosts": hosts, "links": links}


def test_topology_paths_load_and_map_primary_backup():
    assert topology_paths.valid
    primary = topology_paths.get("research_dc_primary")
    backup = topology_paths.get("research_dc_backup")
    assert primary.devices == ["device:bmv2:5", "device:bmv2:3", "device:bmv2:1", "device:bmv2:4", "device:bmv2:6"]
    assert backup.devices == ["device:bmv2:5", "device:bmv2:3", "device:bmv2:2", "device:bmv2:4", "device:bmv2:6"]
    assert [topology_paths.device_switch_map[item] for item in primary.devices] == primary.switches


def test_physical_link_and_host_ports_match_yaml_addlink_order():
    cfg = json.loads(PATH_CONFIG.read_text(encoding="utf-8"))
    topology = yaml.safe_load(TOPOLOGY_YAML.read_text(encoding="utf-8"))
    switches = {item["name"]: f"device:bmv2:{item['device_id']}" for item in topology["switches"]}
    profiles = topology["link_profiles"]
    default_profile = topology["default_link_profile"]
    port_counter: dict[str, int] = {}
    expected_physical = {}
    expected_hosts = {}
    for link in topology["links"]:
        left, right = link["a"], link["b"]
        port_counter[left] = port_counter.get(left, 0) + 1
        port_counter[right] = port_counter.get(right, 0) + 1
        if left in switches and right in switches:
            key = tuple(sorted((switches[left], switches[right])))
            profile = profiles[link.get("profile", default_profile)]
            expected_physical[key] = {
                switches[left]: port_counter[left],
                switches[right]: port_counter[right],
                "capacity_mbps": profile["bw_mbps"],
            }
        elif left.startswith("h_") or right.startswith("h_"):
            host = left if left.startswith("h_") else right
            switch = right if host == left else left
            expected_hosts[host] = (switches[switch], port_counter[switch])

    for link in cfg["physical_links"]:
        key = tuple(sorted((link["device_a"], link["device_b"])))
        expected = expected_physical[key]
        assert expected[link["device_a"]] == link["port_a"]
        assert expected[link["device_b"]] == link["port_b"]
        assert expected["capacity_mbps"] == link["capacity_mbps"]
    for host, attachment in cfg["host_attachment_map"].items():
        assert expected_hosts[host] == (attachment["device_id"], attachment["port"])


def test_runtime_path_validation_reports_missing_device_and_link():
    missing_device = topology_paths.validate_runtime(_runtime_topology(omit_device="device:bmv2:1"))
    assert any(item["code"] == PATH_DEVICE_MISSING for item in missing_device["errors"])
    missing_link = topology_paths.validate_runtime(_runtime_topology(omit_pair=("device:bmv2:3", "device:bmv2:1")))
    assert any(item["code"] == PATH_LINK_MISSING for item in missing_link["errors"])


def test_runtime_selects_backup_when_primary_link_is_unavailable():
    topology = _runtime_topology(omit_pair=("device:bmv2:3", "device:bmv2:1"))
    selected = topology_paths.select_available_path("h_research", "h_dc", topology)
    assert selected and selected.path_id == "research_dc_backup"
    restored = topology_paths.select_available_path("h_research", "h_dc", _runtime_topology())
    assert restored and restored.path_id == "research_dc_primary"


def test_runtime_port_mismatch_is_rejected_and_backup_remains_available():
    topology = _runtime_topology()
    target = next(
        item for item in topology["links"]
        if {endpoint["device_id"] for endpoint in item["endpoints"]}
        == {"device:bmv2:3", "device:bmv2:1"}
    )
    target["endpoints"][0]["port"] = 99
    report = topology_paths.validate_runtime(topology)
    assert report["status"] == "degraded"
    assert any(item["code"] == PATH_LINK_MISSING for item in report["errors"])
    selected = topology_paths.select_available_path("h_research", "h_dc", topology)
    assert selected and selected.path_id == "research_dc_backup"


def test_identical_primary_and_backup_is_rejected(tmp_path):
    cfg = json.loads(PATH_CONFIG.read_text(encoding="utf-8"))
    primary = next(item for item in cfg["paths"] if item["path_id"] == "research_dc_primary")
    backup = next(item for item in cfg["paths"] if item["path_id"] == "research_dc_backup")
    for key in ("devices", "switches", "expected_links", "qos_egress_candidates", "path_capacity_mbps", "bottleneck_link_id"):
        backup[key] = deepcopy(primary[key])
    path = tmp_path / "paths.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    registry = TopologyPathRegistry(str(path))
    assert any(item.code == BACKUP_PATH_IDENTICAL for item in registry.errors)


def _valid_int_probe(queue: int = 3):
    hops = [
        {"hop_index": 0, "switch_id_raw": 5, "device_id": "device:bmv2:5", "switch_name": "s_access_r", "ingress_port": 1, "egress_port": 3, "ingress_timestamp_ns": 1000, "egress_timestamp_ns": 1200, "hop_latency_ns": 200, "queue_occupancy": queue},
        {"hop_index": 1, "switch_id_raw": 3, "device_id": "device:bmv2:3", "switch_name": "s_agg_r", "ingress_port": 2, "egress_port": 3, "ingress_timestamp_ns": 1300, "egress_timestamp_ns": 1600, "hop_latency_ns": 300, "queue_occupancy": queue},
    ]
    for hop in hops:
        hop["flags"] = 1
    return {"status": "ok", "source": "p4_int_probe", "parser_version": "idn-int-v2", "schema_version": 2, "byte_order": "big", "hop_record_bytes": 26, "parsed_hop_count": 2, "payload_bytes": 58, "total_hop_latency_ms": 0.0005, "hop_records": hops}


def test_int_quality_trusted_and_raw_values_are_auditable():
    result = evaluate_int_probe(_valid_int_probe())
    assert result["quality_status"] == TRUSTED
    assert all(
        check["detail"].endswith("validation passed")
        for check in result["validation_checks"] if check["passed"]
    )
    assert result["used_for_decision"] is True
    assert result["hop_records"][0]["raw_value"]["switch_id_raw"] == 5
    assert result["hop_records"][0]["parser_version"]


def test_bad_device_and_negative_latency_are_untrusted_and_excluded():
    bad_device = _valid_int_probe()
    bad_device["hop_records"][0].update({"switch_id_raw": 99, "device_id": "device:bmv2:99", "switch_name": None})
    assert evaluate_int_probe(bad_device)["quality_status"] == UNTRUSTED
    negative = _valid_int_probe()
    negative["hop_records"][0]["hop_latency_ns"] = -1
    evaluated = evaluate_int_probe(negative)
    analysis = analyze_int_probe(evaluated)
    assert evaluated["quality_status"] == UNTRUSTED
    assert evaluated["used_for_decision"] is False
    assert analysis["status"] == "excluded"
    assert analysis["switch_path"] == []
    assert analysis["path_latency_ms"] is None


def test_excessive_queue_is_degraded_and_not_used_for_decision():
    result = evaluate_int_probe(_valid_int_probe(queue=20_000))
    assert result["quality_status"] == DEGRADED
    assert result["used_for_decision"] is False


def _sixteen_directional_links():
    result = []
    for item in topology_paths.physical_links:
        for reverse in (False, True):
            src = item["device_b"] if reverse else item["device_a"]
            dst = item["device_a"] if reverse else item["device_b"]
            src_port = item["port_b"] if reverse else item["port_a"]
            dst_port = item["port_a"] if reverse else item["port_b"]
            result.append(SimpleNamespace(
                link_id=f"{src}/{src_port}-{dst}/{dst_port}", src_device=src,
                src_port=str(src_port), dst_device=dst, dst_port=str(dst_port),
                bandwidth_mbps=None, state="ACTIVE",
            ))
    return result


def test_capacity_aggregation_preserves_16_directions_and_folds_to_8():
    evidence = aggregate_capacity_evidence(
        _sixteen_directional_links(), requested_mbps=120, reserved_mbps=0, headroom_mbps=1,
    )
    assert evidence["raw_directed_link_count"] == 16
    assert evidence["physical_link_count"] == 8
    bottleneck = evidence["default_view"]["bottleneck"]
    assert bottleneck["link_id"] == "s_access_r-s_agg_r"
    assert bottleneck["configured_capacity_mbps"] == 100
    access_summary = next(item for item in evidence["physical_link_summary"] if item["link_id"] == "s_access_r-s_agg_r")
    assert len(access_summary["directional_available_mbps"]) == 2
    assert evidence["default_view"]["blocking_direction"]["src_device"] == "device:bmv2:5"


def test_path_report_has_explicit_unchanged_path_id():
    report = _execution_plane({"qos_enforcement": {"path_changed": False, "current_path_id": "research_dc_primary", "qos_binding": {"path_id": "research_dc_primary", "configured_primary_path_id": "research_dc_primary", "current_path": ["s_access_r", "s_agg_r"]}}}, {})
    assert report["path"]["display_state"] == "无路径变化"
    assert report["path"]["current_path_id"] == "research_dc_primary"


def test_path_report_does_not_fake_unchanged_when_path_is_missing():
    report = _execution_plane({}, {})
    assert report["path"]["current_path_id"] is None
    assert report["path"]["path_changed"] is False
    assert report["path"]["path_change_reason"] == "PATH_UNAVAILABLE"
    assert report["path"]["display_state"] != "无路径变化"


def test_path_report_exposes_backup_transition_and_qos_migration_order():
    report = _execution_plane(
        {
            "qos_enforcement": {
                "path_changed": True,
                "previous_path_id": "research_dc_primary",
                "current_path_id": "research_dc_backup",
                "path_change_reason": "RUNTIME_PRIMARY_UNAVAILABLE",
                "qos_resource_migrated": True,
                "migration_sequence": "INSTALL_NEW_THEN_REMOVE_OLD",
                "qos_binding": {
                    "path_id": "research_dc_backup",
                    "configured_primary_path_id": "research_dc_primary",
                    "current_path": ["s_access_r", "s_agg_r", "s_core2", "s_agg_d", "s_access_d"],
                    "alternative_path_ids": ["research_dc_primary"],
                },
            }
        },
        {},
    )
    path = report["path"]
    assert path["path_changed"] is True
    assert path["previous_path_id"] == "research_dc_primary"
    assert path["current_path_id"] == "research_dc_backup"
    assert path["path_change_reason"] == "RUNTIME_PRIMARY_UNAVAILABLE"
    assert path["qos_resource_migrated"] is True
    assert path["migration_sequence"] == "INSTALL_NEW_THEN_REMOVE_OLD"


def test_frontend_healthcheck_matches_vite_and_is_backend_independent():
    compose = yaml.safe_load((ROOT / "docker-compose.app.yml").read_text(encoding="utf-8"))
    frontend = compose["services"]["idn-frontend"]
    health = frontend["healthcheck"]
    # The probe lives in a script so Compose/shell escaping cannot weaken it.
    assert health["test"] == ["CMD", "/usr/local/bin/frontend-healthcheck"]
    script = (ROOT / "frontend" / "healthcheck.sh").read_text(encoding="utf-8")
    assert "http://127.0.0.1:5173/" in script
    assert "idn-system:8000" not in script
    assert health["start_period"] == "10s"
    # The public entry point is Nginx on 80/443.  Vite must remain loopback
    # only so the development port is not exposed directly to the Internet.
    assert frontend["ports"] == ["127.0.0.1:5173:5173"]


def test_compose_project_and_container_names_are_consistent():
    app = yaml.safe_load((ROOT / "docker-compose.app.yml").read_text(encoding="utf-8"))
    infra = yaml.safe_load((ROOT / "docker-compose.infra.yml").read_text(encoding="utf-8"))
    assert app["name"] == infra["name"] == "idn-system"
    expected = {
        "idn-system": "idn-backend",
        "idn-watcher": "idn-watcher",
        "idn-frontend": "idn-frontend",
        "onos": "idn-onos",
        "mininet": "idn-mininet",
    }
    services = {**infra["services"], **app["services"]}
    assert set(services) == set(expected)
    for service, container_name in expected.items():
        assert services[service]["container_name"] == container_name
        assert container_name.startswith("idn-")
    onos_health = " ".join(infra["services"]["onos"]["healthcheck"]["test"])
    assert "/tmp/idn-onos-ready" in onos_health
    startup = (ROOT / "docker" / "onos" / "onos-start-with-pipeconf.sh").read_text(encoding="utf-8")
    assert "trap shutdown_onos TERM INT EXIT" in startup
    assert 'touch "$READY_FILE"' in startup


def test_mininet_service_validation_does_not_force_an_unverified_socket_buffer():
    """A portable service probe must not inherit an oversized host buffer.

    ``probe_server._flow_args`` accepts an operator-provided
    ``MININET_IPERF_SOCKET_BUFFER_BYTES``.  The compose default must leave it
    unset, though: Mininet namespaces can have a much smaller SO_SNDBUF limit
    and iperf3 then fails immediately with "socket buffer size not set
    correctly" before the P4/TC validation traffic even starts.
    """
    infra = yaml.safe_load((ROOT / "docker-compose.infra.yml").read_text(encoding="utf-8"))
    environment = infra["services"]["mininet"]["environment"]
    assert not any(
        str(item).startswith("MININET_IPERF_SOCKET_BUFFER_BYTES=")
        for item in environment
    )


def test_mininet_image_does_not_copy_an_ignored_root_level_p4info_env():
    dockerfile = (ROOT / "docker" / "Dockerfile.mininet").read_text(encoding="utf-8")
    assert "COPY p4/build/ /app/p4/build/" in dockerfile
    assert "COPY p4/p4info.env /app/p4/p4info.env" not in dockerfile


def test_diagnostic_script_discovers_services_and_is_read_only():
    script = (ROOT / "scripts" / "diagnose_stack.sh").read_text(encoding="utf-8")
    assert "config --services" in script and 'ps --format json' in script and 'ps -q "$service"' in script
    forbidden = [r"docker\s+rm\b", r"docker\s+restart\b", r"compose[^\n]+\s(?:up|down|restart|stop|kill)\b", r"curl[^\n]+\s-X\s+(?:POST|PUT|PATCH|DELETE)"]
    assert not any(re.search(pattern, script, re.IGNORECASE) for pattern in forbidden)

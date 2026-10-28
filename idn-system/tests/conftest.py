"""Shared fixtures for all test files.

This module provides:
- LLM mocks (rule fallback, decorated responses)
- Test data factories (YANG policies, network snapshots, P4Info)
- Infrastructure helpers (env vars, FastAPI client, temp DB store)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.store import IntentStore, get_store, reset_store
from models.flow_table import (
    ActionParam,
    AtomicityMode,
    FlowTablePlan,
    MatchField,
    TableEntry,
    WriteRequest,
)
from models.network import DeviceInfo, HostInfo, LinkInfo, NetworkSnapshot
from models.yang_policy import (
    ActionRules,
    ActionType,
    MatchCriteria,
    NetworkPolicy,
    QoSParams,
    YANGPolicy,
)

# ── Paths ─────────────────────────────────────────────────────────────────────

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent
MININET_DIR = PROJECT_DIR.parent / "mininet"  # idn-system/../mininet/


# ═══════════════════════════════════════════════════════════════════════════════
# Topology fixtures (loaded from YAML — single source of truth)
# ═══════════════════════════════════════════════════════════════════════════════


def _load_topo_yaml(topo_name: str = "research_dc") -> dict[str, Any]:
    """Load a topology YAML as a raw dict (no topo_loader import needed)."""
    import yaml

    topo_path = MININET_DIR / "topologies" / f"{topo_name}.yaml"
    if not topo_path.is_file():
        pytest.skip(f"Topology YAML not found: {topo_path}")
    with open(topo_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="session")
def topo_yaml() -> dict[str, Any]:
    """Return the research_dc topology as a raw YAML dict."""
    return _load_topo_yaml("research_dc")


@pytest.fixture(scope="session")
def topo_switch_names(topo_yaml: dict[str, Any]) -> list[str]:
    """Return sorted switch names from the topology YAML."""
    return sorted(s["name"] for s in topo_yaml.get("switches", []))


@pytest.fixture(scope="session")
def topo_host_names(topo_yaml: dict[str, Any]) -> list[str]:
    """Return sorted host names from the topology YAML."""
    return sorted(h["name"] for h in topo_yaml.get("hosts", []))


@pytest.fixture(scope="session")
def topo_device_ids(topo_yaml: dict[str, Any]) -> list[str]:
    """Return sorted device:bmv2:N IDs from the topology YAML."""
    return sorted(
        f"device:bmv2:{s['device_id']}" for s in topo_yaml.get("switches", [])
    )


@pytest.fixture(scope="session")
def topo_switch_device_map(topo_yaml: dict[str, Any]) -> dict[str, str]:
    """Return {switch_name: device_uri} mapping."""
    return {
        s["name"]: f"device:bmv2:{s['device_id']}"
        for s in topo_yaml.get("switches", [])
    }


@pytest.fixture(scope="session")
def topo_host_ip_map(topo_yaml: dict[str, Any]) -> dict[str, str]:
    """Return {host_name: ip_cidr} mapping."""
    return {h["name"]: h["ip"] for h in topo_yaml.get("hosts", [])}


@pytest.fixture(scope="session")
def topo_links(topo_yaml: dict[str, Any]) -> list[dict[str, Any]]:
    """Return links from the topology YAML."""
    return topo_yaml.get("links", [])


@pytest.fixture(scope="session")
def topo_subnets(topo_yaml: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return subnets from the topology YAML."""
    return topo_yaml.get("subnets", {})


# ═══════════════════════════════════════════════════════════════════════════════
# LLM Mocks
# ═══════════════════════════════════════════════════════════════════════════════

class RuleFallbackLLM:
    """Force deterministic rule fallback — raises on invoke.

    Use this mock when the test should exercise ONLY the rule-based path.
    If the system tries to call the LLM, the test fails with RuntimeError,
    proving that the rule engine handled the request without LLM involvement.
    """

    async def ainvoke(self, messages, **kwargs):
        raise RuntimeError("LLM is disabled — expected rule engine to handle this request")


@pytest.fixture
def rule_fallback_llm() -> RuleFallbackLLM:
    """Return a RuleFallbackLLM that raises if the LLM is ever invoked."""
    return RuleFallbackLLM()


class _DecoratedFlowPlanLLM:
    """Simulate an LLM response with ``<think>…</think>`` wrapping and trailing text."""

    @staticmethod
    async def ainvoke(messages, **kwargs):
        from langchain_core.messages import AIMessage

        return AIMessage(
            content="""<think>select the suitable P4 table</think>
```json
{
  "table_entries": [{
    "table_name": "MyIngress.sla_enforcement",
    "match_fields": [
      {"field_name": "hdr.ipv4.srcAddr", "match_type": "ternary", "value": "192.168.10.0", "mask": 4294967040},
      {"field_name": "hdr.ipv4.dstAddr", "match_type": "ternary", "value": "10.0.0.0", "mask": 4278190080},
      {"field_name": "hdr.ipv4.protocol", "match_type": "ternary", "value": 17, "mask": 255},
      {"field_name": "hdr.udp.dstPort", "match_type": "ternary", "value": 5004, "mask": 65535}
    ],
    "action_name": "MyIngress.set_dscp",
    "action_params": [{"param_name": "dscp_value", "value": 46}],
    "priority": 101,
    "target_device_id": "device:bmv2:1"
  }]
}
```
tail text with unrelated object: {"ignored": true}"""
        )


@pytest.fixture
def decorated_flow_plan_llm() -> _DecoratedFlowPlanLLM:
    """Return a mock LLM that produces wrapped JSON with think-tags."""
    return _DecoratedFlowPlanLLM()


# ═══════════════════════════════════════════════════════════════════════════════
# Environment
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def mock_env() -> None:
    """Ensure every test runs in mock mode with deterministic LLM config."""
    os.environ["IDN_MOCK_MODE"] = "true"
    os.environ.setdefault("LLM_PROVIDER", "openai")
    os.environ.setdefault("LLM_MODEL", "gpt-4o")
    os.environ.setdefault("OPENAI_API_KEY", "test-key")
    # Container images resolve these under /app/config.  Tests run from the
    # source tree, so point the same readers at the checked-in files instead.
    os.environ.setdefault(
        "SERVICE_PROFILES_FILE",
        str(PROJECT_DIR / "config" / "service_profiles.json"),
    )
    os.environ.setdefault(
        "TOPOLOGY_PATHS_FILE",
        str(PROJECT_DIR / "config" / "topology_paths.json"),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test Data Factories
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def demo_p4info() -> dict[str, Any]:
    """Return a minimal but valid demo P4Info dict matching the P4 program."""
    return {
        "tables": [
            {
                "name": "MyIngress.ipv4_routing",
                "match_fields": [
                    {"name": "hdr.ipv4.dstAddr", "match_type": "LPM", "bitwidth": 32},
                ],
                "action_refs": [
                    "MyIngress.set_nhop",
                    "MyIngress.drop_packet",
                ],
                "size": 1024,
            },
            {
                "name": "MyIngress.dscp_marker",
                "match_fields": [
                    {"name": "hdr.ipv4.srcAddr", "match_type": "LPM", "bitwidth": 32},
                    {"name": "hdr.ipv4.dstAddr", "match_type": "TERNARY", "bitwidth": 32},
                ],
                "action_refs": ["MyIngress.mark_dscp"],
                "size": 1024,
            },
            {
                "name": "MyIngress.sla_enforcement",
                "match_fields": [
                    {"name": "hdr.ipv4.srcAddr", "match_type": "LPM", "bitwidth": 32},
                    {"name": "hdr.ipv4.dstAddr", "match_type": "LPM", "bitwidth": 32},
                ],
                "action_refs": ["MyIngress.apply_meter_policy", "MyIngress.mark_dscp"],
                "size": 1024,
            },
        ],
        "actions": [
            {"name": "MyIngress.set_nhop", "params": [
                {"name": "port", "bitwidth": 9},
                {"name": "srcMac", "bitwidth": 48},
                {"name": "dstMac", "bitwidth": 48},
            ]},
            {"name": "MyIngress.mark_dscp", "params": [
                {"name": "dscp_value", "bitwidth": 8},
            ]},
            {"name": "MyIngress.apply_meter_policy", "params": []},
            {"name": "MyIngress.drop_packet", "params": []},
        ],
        "meters": [
            {"name": "MyIngress.host_meter", "size": 4096, "unit": "PACKETS"},
        ],
    }


def _make_yang(
    policy_id: str = "POL-001",
    policy_name: str = "test-policy",
    action_type: ActionType = ActionType.SET_PRIORITY,
    source_ip: str = "192.168.10.0/24",
    destination_ip: str = "10.0.0.0/8",
    bandwidth: int = 100,
    dscp_value: int = 46,
    burst_size: int = 1024,
    protocol: str = "UDP",
    destination_port: int = 5004,
    raw_intent: str = "",
) -> YANGPolicy:
    """Create a standard YANG policy for testing."""
    return YANGPolicy(
        raw_intent=raw_intent,
        confidence_score=0.93,
        network_policy=NetworkPolicy(
            policy_id=policy_id,
            policy_name=policy_name,
            match=MatchCriteria(
                source_ip=source_ip,
                destination_ip=destination_ip,
                protocol=protocol,
                destination_port=destination_port,
            ),
            action=ActionRules(
                action_type=action_type,
                qos_params=QoSParams(
                    dscp_value=dscp_value,
                    bandwidth=bandwidth,
                    burst_size=burst_size,
                ),
            ),
        ),
    )


@pytest.fixture
def yang_policy() -> YANGPolicy:
    """Return a standard SET_PRIORITY YANG policy."""
    return _make_yang()


@pytest.fixture
def yang_policy_factory():
    """Return the factory function so tests can customise parameters."""
    return _make_yang


def _make_snapshot(
    snapshot_id: str = "SNAP-001",
) -> NetworkSnapshot:
    """Create a standard 2-device, 1-link network snapshot."""
    return NetworkSnapshot(
        snapshot_id=snapshot_id,
        devices=[
            DeviceInfo(device_id="device:bmv2:1", status="AVAILABLE"),
            DeviceInfo(device_id="device:bmv2:2", status="AVAILABLE"),
        ],
        links=[
            LinkInfo(
                link_id="s1-s2",
                src_device="device:bmv2:1",
                src_port="2",
                dst_device="device:bmv2:2",
                dst_port="1",
                bandwidth_mbps=1000,
                state="ACTIVE",
            ),
        ],
        hosts=[
            HostInfo(
                host_id="h_src",
                mac="00:00:00:00:00:01",
                ip_addresses=["192.168.10.10"],
                location_device="device:bmv2:1",
                location_port="1",
            ),
            HostInfo(
                host_id="h_dst",
                mac="00:00:00:00:00:02",
                ip_addresses=["10.0.0.10"],
                location_device="device:bmv2:2",
                location_port="2",
            ),
        ],
    )


@pytest.fixture
def network_snapshot() -> NetworkSnapshot:
    """Return a standard 2-device network snapshot."""
    return _make_snapshot()


@pytest.fixture
def network_snapshot_factory():
    """Return the factory function."""
    return _make_snapshot


# ═══════════════════════════════════════════════════════════════════════════════
# API / Store helpers
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def temp_store(tmp_path: Path) -> IntentStore:
    """Return an IntentStore backed by a temporary SQLite database."""
    db_path = tmp_path / "test.db"
    reset_store(db_path)
    return get_store()


@pytest.fixture
def app_client(tmp_path: Path) -> TestClient:
    """Return a FastAPI TestClient with a fresh temporary store."""
    reset_store(tmp_path / "app.db")
    return TestClient(create_app())


@pytest.fixture
def app_client_with_store(tmp_path: Path) -> tuple[TestClient, IntentStore]:
    """Return both TestClient and IntentStore for tests that need direct DB access."""
    store = reset_store(tmp_path / "app.db")
    client = TestClient(create_app())
    return client, store

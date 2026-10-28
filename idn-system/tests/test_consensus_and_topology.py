from __future__ import annotations

from api.topology import build_topology_summary_payload
from infra.trace import TraceBus
from models.agent_state import (
    ConflictReport,
    ConsensusReasonCode,
    ConsensusStatus,
    ExecutionStage,
    GlobalAgentState,
    StageStatus,
)
from models.intent import UserIntent
from models.network import DeviceInfo, DeviceStatus, HostInfo, LinkInfo, NetworkSnapshot, build_topology_summary
from orchestrator.nodes.base_node import BaseNode


class ConflictConsensusNode(BaseNode):
    agent_id = "policy_conflict_checker"
    stage = ExecutionStage.CHECK


def _state(report: ConflictReport, snapshot: NetworkSnapshot | None = None) -> GlobalAgentState:
    return GlobalAgentState(
        intent_id="INT-CONSENSUS-1",
        user_intent=UserIntent(text="保障视频带宽 10 Mbps"),
        conflict_report=report,
        network_snapshot=snapshot,
    )


def test_directed_16_and_logical_13_are_distinct_valid_counts():
    switches = [
        {"name": f"s{i}", "device_id": f"device:bmv2:{i}"}
        for i in range(1, 7)
    ]
    hosts = [{"name": f"h{i}"} for i in range(1, 6)]
    physical_pairs = [(1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 1), (2, 5), (3, 6)]
    mininet_links = [
        {"endpoints": [{"type": "switch", "node": f"s{a}"}, {"type": "switch", "node": f"s{b}"}]}
        for a, b in physical_pairs
    ] + [
        {"endpoints": [{"type": "host", "node": f"h{i}"}, {"type": "switch", "node": f"s{i}"}]}
        for i in range(1, 6)
    ]
    onos_devices = {"devices": [{"id": f"of:{i:016x}"} for i in range(1, 7)]}
    onos_links = {"links": []}
    for a, b in physical_pairs:
        onos_links["links"].extend([
            {"src": {"device": f"of:{a:016x}"}, "dst": {"device": f"of:{b:016x}"}},
            {"src": {"device": f"of:{b:016x}"}, "dst": {"device": f"of:{a:016x}"}},
        ])

    summary = build_topology_summary_payload(
        {"status": "ok", "switches": switches, "hosts": hosts, "links": mininet_links},
        onos_devices,
        onos_links,
        {"hosts": []},
    )

    assert summary == {
        "devices": 6,
        "directed_device_links": 16,
        "physical_device_links": 8,
        "host_attachment_links": 5,
        "logical_total_links": 13,
        "hosts": 5,
        "topology_sccs": 1,
    }


def test_network_summary_deduplicates_bidirectional_device_links():
    devices = [DeviceInfo(device_id=f"device:bmv2:{i}", status=DeviceStatus.AVAILABLE) for i in range(1, 3)]
    links = [
        LinkInfo(link_id="1-2", src_device=devices[0].device_id, src_port="1", dst_device=devices[1].device_id, dst_port="1"),
        LinkInfo(link_id="2-1", src_device=devices[1].device_id, src_port="1", dst_device=devices[0].device_id, dst_port="1"),
    ]
    hosts = [HostInfo(host_id="h1", mac="00:00:00:00:00:01", location_device=devices[0].device_id)]
    summary = build_topology_summary(devices=devices, directed_links=links, hosts=hosts)
    assert summary.directed_device_links == 2
    assert summary.physical_device_links == 1
    assert summary.host_attachment_links == 1
    assert summary.logical_total_links == 2


def test_blocking_consensus_has_actor_reason_detail_and_topology_evidence():
    topology = NetworkSnapshot(
        devices=[DeviceInfo(device_id="device:bmv2:1", status=DeviceStatus.UNAVAILABLE)],
    )
    topology.topology_summary = build_topology_summary(devices=topology.devices, directed_links=[], hosts=[])
    report = ConflictReport(
        has_conflict=True,
        blocking=True,
        conflict_type="TOPOLOGY_CONFLICT",
        severity="MEDIUM",
        explanation="device:bmv2:1 unavailable",
        conflict_items=[{
            "conflict_type": "TOPOLOGY_CONFLICT",
            "unavailable_devices": ["device:bmv2:1"],
            "explanation": "device:bmv2:1 unavailable",
        }],
        suggestions=["等待设备恢复后刷新拓扑并重试。"],
    )
    decision = ConflictConsensusNode()._build_consensus_decision(
        _state(report, topology), StageStatus.SUCCESS, "conflict check complete"
    )
    assert decision.status == ConsensusStatus.NEEDS_REPAIR
    assert decision.blocking is True
    assert decision.blocking_agent == "policy_conflict_checker"
    assert decision.reason_code == ConsensusReasonCode.DEVICE_UNAVAILABLE
    assert decision.reason_detail["unavailable_devices"] == ["device:bmv2:1"]
    assert decision.reason_detail["topology_summary"]["devices"] == 1
    assert decision.original_policy_preserved is True
    assert decision.retry_allowed is True


def test_same_scope_consensus_is_verify_or_update_not_repair():
    duplicate = ConflictReport(
        has_conflict=False,
        blocking=False,
        resolution="REUSE_EXISTING_POLICY",
        execution_mode="VERIFY_EXISTING",
        existing_policy_id="POL-10",
        traffic_scope_key="scope-1",
        explanation="reuse",
    )
    update = duplicate.model_copy(update={
        "resolution": "UPDATE_EXISTING_POLICY",
        "execution_mode": "MODIFY_QOS_RESOURCE",
        "changed_fields": ["bandwidth", "qos_mode"],
        "explanation": "update",
    })
    duplicate_decision = ConflictConsensusNode()._build_consensus_decision(
        _state(duplicate), StageStatus.SUCCESS, "ok"
    )
    update_decision = ConflictConsensusNode()._build_consensus_decision(
        _state(update), StageStatus.SUCCESS, "ok"
    )
    assert duplicate_decision.decision_code == "VERIFY_EXISTING"
    assert duplicate_decision.status == ConsensusStatus.ACCEPTED
    assert update_decision.decision_code == "UPDATE_EXISTING_POLICY"
    assert update_decision.status == ConsensusStatus.ACCEPTED
    assert update_decision.reason_code == ConsensusReasonCode.QOS_PARAMETER_UPDATE


def test_a2a_normalization_exposes_full_rejection_reason():
    payload = {
        "decision_code": "BLOCK_POLICY",
        "reason_code": "INSUFFICIENT_CAPACITY",
        "reason_detail": {"requested_bandwidth": 120, "available_bandwidth": 99},
        "blocking": True,
        "blocking_stage": "check",
        "blocking_agent": "policy_conflict_checker",
        "repair_actions": ["降低带宽需求"],
        "retry_allowed": True,
        "original_policy_preserved": True,
    }
    message = TraceBus.normalize_message({
        "message_type": "Consensus_Sync",
        "payload": payload,
    })
    assert message["reason_code"] == "INSUFFICIENT_CAPACITY"
    assert message["reason_detail"]["requested_bandwidth"] == 120
    assert message["blocking_agent"] == "policy_conflict_checker"
    assert message["repair_action"] == ["降低带宽需求"]
    assert message["original_policy_preserved"] is True

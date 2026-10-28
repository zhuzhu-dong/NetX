"""API-layer unit tests — QoS, healing, decision reports, A2A transport,
conversation persistence, watcher, and agent cards.

Covers: api.qos, api.healing, api.decision_report, api.a2a_trace,
        api.store, api.watcher, api.main (agent cards)
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from api import healing, qos
from api.a2a_trace import build_runtime_evidence, record_delegation, record_heartbeat
from api.conversation.session import _format_flow_stats_reply, _parse_flow_stats_query
from api.decision_report import build_decision_artifacts
from api.main import create_app
from api.routes import _health_rollup
from api.store import IntentStore, get_store, reset_store
from api.watcher import KpiWatcher, WatcherConfig
from api.worker import (
    _collect_compensatable_flows,
    _compensate_flows,
    _has_newly_created_dataplane_resources,
)
from persistence.policy_store import PolicyStore
from models.yang_policy import (
    ActionRules,
    ActionType,
    MatchCriteria,
    NetworkPolicy,
    QoSParams,
    YANGPolicy,
)


# ═══════════════════════════════════════════════════════════════════════════════
# QoS
# ═══════════════════════════════════════════════════════════════════════════════

def _qos_policy(action_type=ActionType.RATE_LIMIT, bandwidth=50):
    return YANGPolicy(
        network_policy=NetworkPolicy(
            policy_id="POL-QOS-1",
            policy_name="qos-test",
            match=MatchCriteria(source_ip="192.168.10.0/24", destination_ip="10.0.0.0/8"),
            action=ActionRules(
                action_type=action_type,
                qos_params=QoSParams(bandwidth=bandwidth, burst_size=512, dscp_value=46),
            ),
        )
    )


@pytest.mark.asyncio
async def test_apply_qos_policy_skips_non_qos_action():
    policy = _qos_policy(ActionType.DROP)
    result = await qos.apply_qos_policy(policy)
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_apply_qos_policy_skips_none_policy():
    result = await qos.apply_qos_policy(None)
    assert result["status"] == "skipped"


# ═══════════════════════════════════════════════════════════════════════════════
# Healing
# ═══════════════════════════════════════════════════════════════════════════════

def test_should_heal_on_qdisc_congestion():
    """qdisc congestion_detected → should attempt healing."""
    v = SimpleNamespace(passed=False, violations=[], telemetry=SimpleNamespace(
        raw_int_data={"raw": {"qdisc_probe": {"status": "ok", "congestion_detected": True}}}))
    assert healing.should_attempt_congestion_heal(v) is True


def test_should_heal_on_bandwidth_violation():
    """KPI keywords in violations → should attempt healing."""
    v = SimpleNamespace(passed=False, violations=["带宽不足: 实际10.0Mbps < 约束30.0Mbps"],
                         telemetry=SimpleNamespace(raw_int_data={}))
    assert healing.should_attempt_congestion_heal(v) is True


def test_should_not_heal_when_passed():
    """Passed verification → skip healing."""
    v = SimpleNamespace(passed=True, violations=["带宽不足"], telemetry=SimpleNamespace(raw_int_data={}))
    assert healing.should_attempt_congestion_heal(v) is False


# ═══════════════════════════════════════════════════════════════════════════════
# Decision Report
# ═══════════════════════════════════════════════════════════════════════════════

def test_decision_report_projects_policy_conflicts():
    _, report = build_decision_artifacts({
        "intent_id": "INT-CONFLICT-VIS",
        "current_status": "failed",
        "user_intent": {"text": "保障视频业务，同时限制低优先级流量"},
        "network_snapshot": {"devices": [], "links": [], "hosts": [], "flows": []},
        "yang_draft": {
            "network_policy": {
                "policy_id": "POL-new", "policy_name": "new-video-policy",
                "match": {"source_ip": "192.168.10.0/24", "destination_ip": "10.0.0.0/8", "protocol": "UDP"},
                "action": {"action_type": "SET_PRIORITY"},
            },
            "confidence_score": 0.92,
        },
        "conflict_report": {
            "has_conflict": True, "conflict_type": "QOS_CONFLICT", "severity": "HIGH",
            "explanation": "新策略与已有 QoS 策略冲突。",
            "conflict_items": [
                {"conflict_type": "QOS_CONFLICT", "severity": "HIGH",
                 "existing_policy_id": "POL-old", "existing_policy_name": "old-video-policy",
                 "explanation": "DSCP 与带宽目标不一致。"},
            ],
        },
    })
    assert report["policy_conflict"]["status"] == "blocked"
    assert len(report["policy_conflict"]["matrix"]) == 1
    assert "新策略与已有 QoS 策略冲突" in report["final_conclusion"]
    assert "原因：未知" not in report["final_conclusion"]


def test_policy_store_delete_by_intent_removes_rolled_back_policy(temp_store):
    temp_store.create(
        intent_id="INT-ROLLBACK-1",
        text="test rollback cleanup",
        priority="medium",
        user_id="tester",
    )
    policy_store = PolicyStore(temp_store)
    policy_store.save(_qos_policy(), "INT-ROLLBACK-1")

    assert policy_store.count() == 1
    assert policy_store.delete_by_intent("INT-ROLLBACK-1") == 1
    assert policy_store.count() == 0
    assert policy_store.delete_by_intent("INT-ROLLBACK-1") == 0


def test_collect_compensatable_flows_includes_retries_qos_and_healing():
    record = {
        "result": {
            "execution_result": {
                "installed_flows": [
                    {"device_id": "device:bmv2:1", "flow_id": "main-1"},
                    {"device_id": "device:bmv2:1", "flow_id": "main-1"},
                ],
                "qos_enforcement": {
                    "device_id": "device:bmv2:5",
                    "flow_ids": ["qos-1"],
                },
            },
            "decision_report": {
                "auto_healing": {
                    "actions": [{
                        "device_id": "device:bmv2:5",
                        "flow_ids": ["heal-1"],
                    }],
                },
            },
        },
    }

    assert _collect_compensatable_flows(record) == [
        {"device_id": "device:bmv2:1", "flow_id": "main-1"},
        {"device_id": "device:bmv2:5", "flow_id": "qos-1"},
        {"device_id": "device:bmv2:5", "flow_id": "heal-1"},
    ]


def test_health_rollup_requires_network_infrastructure_and_worker():
    assert _health_rollup("ok", "ok", "ok") == "healthy"
    assert _health_rollup("unavailable", "ok", "ok") == "unhealthy"
    assert _health_rollup("ok", "unavailable", "ok") == "unhealthy"
    assert _health_rollup("ok", "ok", "starting") == "starting"


@pytest.mark.asyncio
async def test_compensation_reconciles_policy_when_flows_are_already_absent(temp_store):
    intent_id = "INT-ROLLBACK-RECONCILE"
    temp_store.create(
        intent_id=intent_id,
        text="test rollback reconciliation",
        priority="medium",
        user_id="tester",
    )
    policy_store = PolicyStore(temp_store)
    policy_store.save(_qos_policy(), intent_id)

    result = await _compensate_flows(intent_id, temp_store)

    assert result["status"] == "ok"
    assert result["deleted"] == 0
    assert result["policies_deleted"] == 1
    assert policy_store.count() == 0


@pytest.mark.asyncio
async def test_compensation_counts_actual_deletes_closes_client_and_cleans_policy(
    temp_store, monkeypatch,
):
    intent_id = "INT-ROLLBACK-FLOWS"
    temp_store.create(
        intent_id=intent_id,
        text="test flow rollback",
        priority="medium",
        user_id="tester",
    )
    temp_store.update(intent_id, result={
        "execution_result": {
            "installed_flows": [
                {"device_id": "device:bmv2:1", "flow_id": "11"},
                {"device_id": "device:bmv2:1", "flow_id": "12"},
            ],
        },
    })
    policy_store = PolicyStore(temp_store)
    policy_store.save(_qos_policy(), intent_id)

    class FakeONOSClient:
        closed = False

        async def delete_flows(self, device_id, flow_ids):
            assert device_id == "device:bmv2:1"
            assert flow_ids == ["11", "12"]
            return {"flows_deleted": 2, "total": 2, "errors": None}

        async def close(self):
            FakeONOSClient.closed = True

    from mcp_servers import onos_server
    monkeypatch.setattr(onos_server, "ONOSClient", FakeONOSClient)

    result = await _compensate_flows(intent_id, temp_store)

    assert result["status"] == "ok"
    assert result["deleted"] == 2
    assert result["failed"] == 0
    assert result["policies_deleted"] == 1
    assert FakeONOSClient.closed is True
    assert policy_store.count() == 0


@pytest.mark.asyncio
async def test_post_execution_unsatisfiable_compensation_releases_new_qos_binding(
    temp_store, monkeypatch,
):
    """A failed new SLA must not leave its P4 rule, TC class or binding active."""
    intent_id = "INT-UNSAT-NEW-RESOURCE"
    policy = _qos_policy().model_dump(mode="json")
    policy_id = policy["network_policy"]["policy_id"]
    temp_store.create(
        intent_id=intent_id,
        text="post-execution unsatisfiable",
        priority="medium",
        user_id="tester",
    )
    temp_store.update(intent_id, result={
        "yang_policies": [policy],
        "policy_results": [{
            "policy_id": policy_id,
            "execution_result": {
                "operation": "CREATE",
                "executor_status": "CREATED",
                "data_plane_changed": True,
                "installed_flows": [{"device_id": "device:bmv2:1", "flow_id": "901"}],
            },
        }],
    })
    PolicyStore(temp_store).save(_qos_policy(), intent_id)

    class FakeONOSClient:
        async def delete_flows(self, device_id, flow_ids):
            assert (device_id, flow_ids) == ("device:bmv2:1", ["901"])
            return {"flows_deleted": 1, "total": 1, "errors": None}

        async def close(self):
            return None

    async def fake_clear_qos_policies(*, yang_policy):
        assert yang_policy["network_policy"]["policy_id"] == policy_id
        return {"status": "ok", "scope_key": "scope-under-test"}

    from api import qos
    from mcp_servers import onos_server

    monkeypatch.setattr(onos_server, "ONOSClient", FakeONOSClient)
    monkeypatch.setattr(qos, "clear_qos_policies", fake_clear_qos_policies)

    record = temp_store.get(intent_id)
    assert _has_newly_created_dataplane_resources(record) is True
    result = await _compensate_flows(intent_id, temp_store)

    assert result["status"] == "ok"
    assert result["deleted"] == 1
    assert result["qos_deleted"] == 1
    assert result["qos_failed"] == 0
    assert result["policies_deleted"] == 1
    assert PolicyStore(temp_store).count() == 0


@pytest.mark.asyncio
async def test_compensation_reuses_an_existing_withdrawal_writer_lease(
    temp_store, monkeypatch,
):
    """Withdrawal compensation must not request a nested fencing lease."""
    intent_id = "INT-WITHDRAW-LEASE-REUSE"
    temp_store.create(
        intent_id=intent_id,
        text="withdraw leased flow",
        priority="medium",
        user_id="tester",
    )
    temp_store.update(intent_id, result={
        "execution_result": {
            "installed_flows": [{"device_id": "device:bmv2:1", "flow_id": "71"}],
        },
    })

    class FakeONOSClient:
        async def delete_flows(self, device_id, flow_ids):
            assert (device_id, flow_ids) == ("device:bmv2:1", ["71"])
            return {"flows_deleted": 1, "total": 1, "errors": None}

        async def close(self):
            return None

    from infra.network_write_fencing import NetworkWriteFencing, write_context
    from mcp_servers import onos_server

    monkeypatch.setattr(onos_server, "ONOSClient", FakeONOSClient)
    lease = NetworkWriteFencing(temp_store).acquire(
        owner_type="INTENT_WITHDRAWAL",
        owner_id=intent_id,
        recovery_id=f"WITHDRAW-{intent_id}",
        ttl_seconds=120,
    )
    try:
        with write_context(
            owner_type=lease["owner_type"],
            owner_id=lease["owner_id"],
            fencing_token=int(lease["fencing_token"]),
        ):
            result = await _compensate_flows(
                intent_id,
                temp_store,
                acquire_lease=False,
            )
    finally:
        NetworkWriteFencing(temp_store).release(lease["lease_id"])

    assert result["status"] == "ok"
    assert result["deleted"] == 1
    assert result["failed"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# A2A Transport
# ═══════════════════════════════════════════════════════════════════════════════

def test_a2a_inprocess_transport_persists_metadata(tmp_path):
    store = reset_store(tmp_path / "app.db")
    intent_id = "INT-A2A-1"
    store.create(intent_id=intent_id, text="test", priority="medium", user_id="tester")
    state = {"intent_id": intent_id}

    task_id = record_delegation(state, source_agent="orchestrator",
                                target_agent="intent_planner", stage="think",
                                input_summary="parse user intent")
    events = store.list_events(intent_id)
    assert task_id
    message = events[0]["data"]
    assert message["transport"]["selected"] == "inprocess"


def test_a2a_transport_endpoint_reports_adapter(tmp_path):
    reset_store(tmp_path / "app.db")
    client = TestClient(create_app())
    response = client.get("/api/v1/a2a/transport")
    assert response.status_code == 200
    assert "selected" in response.json()


# ═══════════════════════════════════════════════════════════════════════════════
# Conversation Persistence (SQLite store)
# ═══════════════════════════════════════════════════════════════════════════════

def test_sqlite_store_retains_sessions_and_messages_after_reopen(tmp_path):
    database = tmp_path / "app.db"
    store = IntentStore(database)
    session = store.create_session("operator", "Video guarantee")
    store.create("INT-PERSIST-1", "保障视频业务带宽不低于100Mbps",
                 user_id="operator", session_id=session["session_id"])
    store.update("INT-PERSIST-1", status="success",
                 snapshot={"verification_passed": True},
                 result={"flow_table_plan": {"plan_id": "PLAN-1"}})
    store.add_message(session["session_id"], "assistant", "final_result",
                      "意图已执行。", intent_id="INT-PERSIST-1")

    reopened = IntentStore(database)
    record = reopened.get("INT-PERSIST-1")
    assert record["status"] == "success"
    assert record["snapshot"]["verification_passed"] is True


def test_read_only_flow_stats_query_is_not_pipeline_intent():
    query = _parse_flow_stats_query("查看 bmv2:1 的实时流表统计")
    assert query == {"kind": "flow_stats", "device_id": "device:bmv2:1"}
    assert _parse_flow_stats_query("下发 bmv2:1 的流表") is None


def test_flow_stats_reply_includes_runtime_flow_details():
    reply = _format_flow_stats_reply("device:bmv2:1", [{
        "state": "ADDED",
        "packets": 2,
        "bytes": 128,
        "priority": 10,
        "tableId": 37066206,
        "selector": {"criteria": [{
            "type": "PROTOCOL_INDEPENDENT",
            "matches": [{
                "field": "hdr.ipv4.dstAddr",
                "match": "lpm",
                "value": "c0a80a0a",
                "prefixLength": 32,
            }],
        }]},
        "treatment": {"instructions": [{
            "type": "PROTOCOL_INDEPENDENT",
            "subtype": "ACTION",
            "actionId": "MyIngress.set_nhop",
            "actionParams": {"port": "0003"},
        }]},
    }])

    assert "device:bmv2:1" in reply
    assert "ADDED" in reply
    assert "packets=2" in reply
    assert "192.168.10.10/32" in reply
    assert "MyIngress.set_nhop(port=3)" in reply


def test_chat_non_stream_returns_flow_stats_without_intent(tmp_path, monkeypatch):
    reset_store(tmp_path / "app.db")

    async def fake_get_flows(device_id=None):
        assert device_id == "device:bmv2:1"
        return {"flows": [{
            "state": "ADDED",
            "packets": 2,
            "bytes": 128,
            "priority": 10,
            "tableId": 37066206,
            "selector": {"criteria": [{
                "matches": [{
                    "field": "hdr.ipv4.dstAddr",
                    "match": "lpm",
                    "value": "c0a80a0a",
                    "prefixLength": 32,
                }],
            }]},
            "treatment": {"instructions": [{
                "actionId": "MyIngress.set_nhop",
                "actionParams": {"port": "0003"},
            }]},
        }]}

    from mcp_servers import onos_server

    monkeypatch.setattr(onos_server.onos_client, "get_flows", fake_get_flows)
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/chat/non-stream",
        headers={"X-API-Key": "demo-key-xxx"},
        json={"text": "查看 bmv2:1 的实时流表统计", "user_id": "operator"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent_detected"] is False
    assert payload["intent_id"] is None
    assert "device:bmv2:1 当前实时流表" in payload["reply"]
    assert "192.168.10.10/32" in payload["reply"]


def test_api_intent_submit_and_status_flow(tmp_path, monkeypatch):
    """Submit an intent via API → job is queued → status tracked in store."""
    monkeypatch.setenv("IDN_MOCK_MODE", "true")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "checkpoint.db"))
    reset_store(tmp_path / "app.db")

    client = TestClient(create_app())
    session_id = client.post(
        "/api/v1/sessions",
        headers={"X-API-Key": "demo-key-xxx"},
        json={"user_id": "operator", "title": "Test session"},
    ).json()["session_id"]
    response = client.post(
        "/api/v1/intents",
        headers={"X-API-Key": "demo-key-xxx"},
        json={
            "session_id": session_id, "user_id": "operator",
            "text": "保障科研大楼到数据中心的视频业务带宽不低于10Gbps",
        },
    )
    assert response.status_code == 202
    intent_id = response.json()["intent_id"]
    assert response.json()["job_status"] == "queued"

    # Verify the intent record exists
    record = client.get(f"/api/v1/intents/{intent_id}").json()
    assert record["intent_id"] == intent_id


def test_intent_details_is_one_aggregate_read_with_optional_sections(app_client_with_store):
    client, store = app_client_with_store
    store.create(
        intent_id="INT-DETAILS-1", text="inspect current intent", priority="medium", user_id="tester"
    )
    response = client.get("/api/v1/intents/INT-DETAILS-1/details")
    assert response.status_code == 200
    payload = response.json()
    assert payload["record"]["intent_id"] == "INT-DETAILS-1"
    assert payload["execution"] == {"intent_id": "INT-DETAILS-1", "current": None, "history": []}
    assert "evidence" in payload["capabilities"]["available_sections"]
    assert payload["a2a_trace_ref"].endswith("/INT-DETAILS-1/a2a-trace")


def test_http_errors_follow_client_contract(app_client):
    response = app_client.get("/api/v1/intents/does-not-exist/details", headers={"X-Trace-Id": "trace-contract"})
    assert response.status_code == 404
    error = response.json()["error"]
    assert set(error) == {"error_type", "message", "reason_code", "trace_id", "retryable"}
    assert error["trace_id"] == "trace-contract"


# ═══════════════════════════════════════════════════════════════════════════════
# Watcher
# ═══════════════════════════════════════════════════════════════════════════════

def _watcher_config(**overrides):
    values = {
        "api_base_url": "http://testserver/api/v1",
        "enabled": True, "light_interval_sec": 0.01, "deep_every_n": 3,
        "iperf_on_suspect": True, "debounce_samples": 3, "cooldown_sec": 60,
        "max_latency_ms": 10, "max_loss_rate": 0.01, "min_throughput_mbps": 20,
        "throughput_requires_iperf": True, "queue_depth_bytes_threshold": 65536,
        "queue_overlimits_delta_threshold": 10, "queue_drops_delta_threshold": 1,
    }
    values.update(overrides)
    return WatcherConfig(**values)


def test_watcher_queue_counter_deltas_trigger_bad_verdict(tmp_path):
    reset_store(tmp_path / "app.db")
    watcher = KpiWatcher(_watcher_config())
    watcher.state.last_kpi = {"queue_overlimits": 10, "queue_drops": 0}

    verdict = watcher.evaluate(
        {"queue_overlimits": 25, "queue_drops": 1},
        telemetry_source="onos_rest+mininet_qdisc",
    )
    assert verdict["bad"] is True
    assert "queue_overlimits_delta=15.0" in verdict["reason"]


def test_watcher_trigger_persists_incident(tmp_path):
    reset_store(tmp_path / "app.db")
    store = get_store()
    watcher = KpiWatcher(_watcher_config())
    watcher.state.active_incident_id = "INC-test"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["context"]["triggered_by"] == "kpi_watcher"
        intent_id = "INT-watch-1"
        store.create(intent_id=intent_id, text="watcher-triggered",
                     priority="critical", user_id="watcher", context=payload["context"])
        return httpx.Response(202, json={
            "session_id": "SES-test", "intent_id": intent_id,
            "job_id": "JOB-test", "job_status": "queued", "status": "pending",
        })

    async def _trigger():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await watcher.trigger_healing_intent(
                client,
                {"congestion_detected": True},
                {"bad": True, "reason": "test", "reasons": ["test"]},
            )

    intent_id = asyncio.run(_trigger())
    incident = store.get_watcher_incident("INC-test")
    assert incident["status"] == "triggered"
    assert incident["triggered_intent_id"] == intent_id

import pytest

from api import telemetry


class _FakeONOSClient:
    def __init__(self):
        self._flow_samples = [
            [{"state": "ADDED", "packets": 10, "bytes": 1000}],
            [{"state": "ADDED", "packets": 16, "bytes": 2500}],
        ]

    async def get_devices(self):
        return {"devices": [{"available": True}, {"available": False}]}

    async def get_flows(self):
        return {"flows": self._flow_samples.pop(0)}


@pytest.mark.asyncio
async def test_collect_current_telemetry_derives_throughput_from_onos_flow_counters(monkeypatch):
    client = _FakeONOSClient()
    times = iter([100.0, 102.0])
    monkeypatch.setattr(telemetry, "_last_sample", None)
    monkeypatch.setattr(telemetry, "_monotonic", lambda: next(times))

    first = await telemetry.collect_current_telemetry(client)
    second = await telemetry.collect_current_telemetry(client)

    assert first["status"] == "ok"
    assert first["devices"] == {"total": 2, "available": 1}
    assert first["flows"]["bytes"] == 1000
    assert second["flows"]["packets"] == 16
    assert second["kpi"]["throughput_mbps"] == 0.006
    assert second["flows"]["packet_rate_pps"] == 3.0


@pytest.mark.asyncio
async def test_collect_current_telemetry_exposes_tc_qdisc_congestion(monkeypatch):
    client = _FakeONOSClient()
    monkeypatch.setattr(telemetry, "_last_sample", None)
    monkeypatch.setattr(telemetry, "_monotonic", lambda: 100.0)

    async def fake_probe(path, params=None):
        if path == "/probe/ping":
            return {
                "status": "ok",
                "latency_ms": 4.0,
                "packet_loss_rate": 0.0,
                "jitter_ms": 0.2,
            }
        if path == "/probe/qdisc":
            return {
                "status": "ok",
                "queue_depth": 12,
                "queue_depth_bytes": 4096,
                "dropped_packets": 2,
                "overlimits": 9,
                "congestion_detected": True,
                "congested_interfaces": [
                    {"node": "s_agg_r", "interface": "s_agg_r-eth3", "overlimits": 9}
                ],
            }
        return None

    monkeypatch.setattr(telemetry, "_probe_get", fake_probe)

    result = await telemetry.collect_current_telemetry(
        client,
        include_probe=True,
        include_iperf=False,
    )

    assert result["kpi"]["queue_depth"] == 12
    assert result["kpi"]["queue_depth_bytes"] == 4096
    assert result["kpi"]["queue_drops"] == 2
    assert result["kpi"]["queue_overlimits"] == 9
    assert result["kpi"]["congestion_detected"] is True
    assert result["metrics"][3]["status"] == "warn"


@pytest.mark.asyncio
async def test_collect_current_telemetry_treats_overlimits_as_historical_counter(monkeypatch):
    client = _FakeONOSClient()
    monkeypatch.setattr(telemetry, "_last_sample", None)
    monkeypatch.setattr(telemetry, "_monotonic", lambda: 100.0)

    async def fake_probe(path, params=None):
        if path == "/probe/ping":
            return {
                "status": "ok",
                "latency_ms": 2.0,
                "packet_loss_rate": 0.0,
            }
        if path == "/probe/qdisc":
            return {
                "status": "ok",
                "queue_depth": 0,
                "queue_depth_bytes": 0,
                "dropped_packets": 0,
                "overlimits": 17800,
                "congestion_detected": True,
                "congested_interfaces": [
                    {"node": "h_research", "interface": "h_research-eth0", "overlimits": 17800}
                ],
            }
        return None

    monkeypatch.setattr(telemetry, "_probe_get", fake_probe)

    result = await telemetry.collect_current_telemetry(
        client,
        include_probe=True,
        include_iperf=False,
    )

    assert result["kpi"]["queue_overlimits"] == 17800
    assert result["kpi"]["congestion_detected"] is False
    assert result["raw"]["qdisc_probe"]["historical_congestion_detected"] is True
    assert result["metrics"][3]["status"] == "ok"


@pytest.mark.asyncio
async def test_collect_current_telemetry_exposes_p4_int_hops(monkeypatch):
    client = _FakeONOSClient()
    monkeypatch.setattr(telemetry, "_last_sample", None)
    monkeypatch.setattr(telemetry, "_monotonic", lambda: 100.0)
    monkeypatch.setenv("IDN_INT_PROBE_ENABLED", "true")

    async def fake_probe(path, params=None):
        if path == "/probe/ping":
            return {"status": "ok", "latency_ms": 1.0, "packet_loss_rate": 0.0}
        if path == "/probe/qdisc":
            return {"status": "ok", "queue_depth": 0, "queue_depth_bytes": 0}
        if path == "/probe/int":
            return {
                "status": "ok",
                "source": "p4_int_probe",
                "parser_version": "idn-int-v2",
                "schema_version": 2,
                "byte_order": "big",
                "hop_record_bytes": 26,
                "parsed_hop_count": 2,
                "payload_bytes": 58,
                "total_hop_latency_ms": 0.75,
                "max_queue_occupancy": 12,
                "hop_records": [
                    {
                        "hop_index": 0,
                        "switch_id_raw": 5,
                        "device_id": "device:bmv2:5",
                        "switch_name": "s_access_r",
                        "ingress_port": 1,
                        "egress_port": 3,
                        "ingress_timestamp_ns": 1000,
                        "egress_timestamp_ns": 301000,
                        "hop_latency_ns": 300000,
                        "queue_occupancy": 4,
                        "flags": 1,
                    },
                    {
                        "hop_index": 1,
                        "switch_id_raw": 3,
                        "device_id": "device:bmv2:3",
                        "switch_name": "s_agg_r",
                        "ingress_port": 2,
                        "egress_port": 3,
                        "ingress_timestamp_ns": 401000,
                        "egress_timestamp_ns": 851000,
                        "hop_latency_ns": 450000,
                        "queue_occupancy": 12,
                        "flags": 1,
                    },
                ],
            }
        return None

    monkeypatch.setattr(telemetry, "_probe_get", fake_probe)

    result = await telemetry.collect_current_telemetry(
        client,
        include_probe=True,
        include_iperf=False,
    )

    assert "p4_int" in result["source"]
    assert result["kpi"]["int_hop_count"] == 2
    assert result["kpi"]["int_path_latency_ms"] == 0.75
    assert result["kpi"]["int_max_queue_occupancy"] == 12
    assert result["metrics"][4]["id"] == "int_hops"
    assert result["raw"]["int_probe"]["hop_records"][1]["device_id"] == "device:bmv2:3"

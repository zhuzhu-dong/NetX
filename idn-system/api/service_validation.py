"""Business-scoped active validation and recent service-metric registry."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from api.service_profiles import (
    ValidationProfileMismatch,
    get_service_profile,
    get_traffic_scenario,
    profile_matches_policy,
)
from models.policy_update import effective_qos_mode, qos_values, traffic_scope_key
from models.service_profile import EvidenceStatus, KpiStatus, MeasurementStatus
from models.yang_policy import ActionType, YANGPolicy


_metrics_lock = asyncio.Lock()
_latest_metrics: dict[str, dict[str, Any]] = {}
_metrics_history: dict[str, list[dict[str, Any]]] = {}
_MAX_HISTORY = 50
_active_probe_lock = asyncio.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _active_best_effort_scope_ids(
    *, policy: YANGPolicy | None, request: dict[str, Any],
) -> set[str]:
    """Resolve declared active contenders without service-specific branches."""
    if policy is None or effective_qos_mode(policy) != "MIN_BANDWIDTH":
        return set()
    requested_ids = {
        str(item if isinstance(item, str) else item.get("service_id") or item.get("service_profile_id") or "")
        for item in (request.get("background_profiles") or [])
    }
    if not requested_ids:
        return set()
    from api.store import get_store
    from infra.ingress_admission_state import IngressAdmissionDesiredRepository

    desired = IngressAdmissionDesiredRepository(get_store()).active_for_scope(
        traffic_scope_key(policy).key
    )
    if desired is None:
        return set()
    return {
        str(scope["scope_id"])
        for scope in (desired.get("desired") or {}).get("eligible_best_effort_scopes") or []
        if str(scope.get("service_id") or "") in requested_ids
    }


async def _activate_validation_ingress_allocation(
    *, policy: YANGPolicy | None, request: dict[str, Any], target_demand_mbps: float,
) -> dict[str, Any]:
    """Materialize the declared competition allocation before its real probe.

    QoS egress readback alone cannot protect a flow if an active contender is
    unbounded upstream of BMv2.  This uses the same reconciler, lease, and
    fencing path as production allocation; it does not issue ad-hoc TC
    commands from the validator.
    """
    active_scopes = _active_best_effort_scope_ids(policy=policy, request=request)
    if policy is None or not active_scopes:
        return {"status": "NOT_APPLICABLE", "write_executed": False}
    from infra.network_reconciler import get_network_reconciler

    result = await get_network_reconciler().activate_ingress_runtime_allocation(
        target_demand_mbps=target_demand_mbps,
        active_best_effort_scope_ids=active_scopes,
        traffic_scope_key=traffic_scope_key(policy).key,
    )
    return {"status": "APPLIED", **result}


async def _restore_validation_ingress_allocation(
    *, policy: YANGPolicy | None, allocation: dict[str, Any],
) -> dict[str, Any]:
    """Restore idle borrowing after a bounded synthetic contention window."""
    if policy is None or allocation.get("status") != "APPLIED":
        return {"status": "NOT_APPLICABLE", "write_executed": False}
    from infra.network_reconciler import get_network_reconciler

    result = await get_network_reconciler().activate_ingress_runtime_allocation(
        target_demand_mbps=float(qos_values(policy).get("bandwidth") or 0),
        active_best_effort_scope_ids=set(),
        traffic_scope_key=traffic_scope_key(policy).key,
    )
    return {"status": "RESTORED_IDLE", **result}


def _active_policy_for_service(service_id: str) -> tuple[YANGPolicy, dict[str, Any]] | None:
    from persistence.policy_store import PolicyStore

    profile = get_service_profile(service_id)
    for item in PolicyStore().list_all():
        candidate = dict(item)
        store = dict(candidate.pop("_store", {}) or {})
        matched, _ = profile_matches_policy(profile, candidate)
        if not matched:
            continue
        policy = YANGPolicy(
            network_policy=candidate,
            service_id=profile.service_id,
            validation_profile_id=profile.service_id,
            service_profile_snapshot=profile.model_dump(mode="json"),
        )
        return policy, store
    return None


def active_policy_for_service(service_id: str) -> tuple[YANGPolicy, dict[str, Any]] | None:
    """Return the active, selector-matched policy used by formal execution."""
    return _active_policy_for_service(service_id)


def _empty_protocol_metrics(protocol: str) -> dict[str, Any]:
    if protocol == "UDP":
        return {
            "sent_mbps": None,
            "received_mbps": None,
            "sent_packets": None,
            "received_packets": None,
            "lost_packets": None,
            "total_datagrams": None,
            "loss_percent": None,
            "jitter_ms": None,
            "out_of_order": None,
        }
    if protocol == "TCP":
        return {
            "sender_mbps": None,
            "receiver_mbps": None,
            "retransmits": None,
            "mean_rtt_ms": None,
            "max_cwnd": None,
            "bytes_sent": None,
            "bytes_received": None,
        }
    return {
        "transmitted": None,
        "received": None,
        "loss_percent": None,
        "rtt_min_ms": None,
        "rtt_avg_ms": None,
        "rtt_max_ms": None,
        "rtt_mdev_ms": None,
    }


def no_sample_result(service_id: str) -> dict[str, Any]:
    profile = get_service_profile(service_id)
    return {
        "probe_id": None,
        "service_id": profile.service_id,
        "service_name": profile.service_name,
        "intent_id": None,
        "policy_id": None,
        "protocol": profile.protocol.value,
        "selector": profile.selector(),
        "start_time": None,
        "end_time": None,
        "duration_seconds": None,
        "status": MeasurementStatus.NO_SAMPLE.value,
        "measurement_status": MeasurementStatus.NO_SAMPLE.value,
        "evidence_status": EvidenceStatus.UNAVAILABLE.value,
        "kpi_status": KpiStatus.NOT_EVALUATED.value,
        "validation_run_id": None,
        "validation_started_at": None,
        "validation_ended_at": None,
        "measurement_source": None,
        "offered_load": {},
        "received_load": {},
        "protocol_metrics": _empty_protocol_metrics(profile.protocol.value),
        "background_results": [],
        "p4_counter_evidence": {"status": "NO_MATCHING_ENTRY", "matching_entry_count": 0},
        "tc_evidence": {"status": "NO_SAMPLE"},
        "path_evidence": {},
        "validation_result": {
            "passed": False,
            "status": MeasurementStatus.NO_SAMPLE.value,
            "verification_state": "NO_SAMPLE",
        },
        "errors": [],
    }


async def _remember(result: dict[str, Any]) -> None:
    service_id = str(result.get("service_id") or "")
    if not service_id:
        return
    snapshot = deepcopy(result)
    async with _metrics_lock:
        _latest_metrics[service_id] = snapshot
        history = _metrics_history.setdefault(service_id, [])
        history.append(snapshot)
        del history[:-_MAX_HISTORY]
    from api.store import get_store
    from persistence.telemetry_store import TelemetryStore

    await asyncio.to_thread(
        TelemetryStore(get_store()).record_service_metric,
        snapshot,
        traffic_role="VALIDATION",
    )


def latest_service_metrics(service_id: str | None = None) -> dict[str, Any]:
    if service_id:
        return deepcopy(_latest_metrics.get(service_id) or no_sample_result(service_id))
    from api.service_profiles import list_service_profiles

    return {
        profile.service_id: deepcopy(
            _latest_metrics.get(profile.service_id) or no_sample_result(profile.service_id)
        )
        for profile in list_service_profiles()
    }


def service_metric_history(service_id: str) -> list[dict[str, Any]]:
    get_service_profile(service_id)
    from api.store import get_store
    from persistence.telemetry_store import TelemetryStore

    persisted = TelemetryStore(get_store()).service_history(service_id, limit=_MAX_HISTORY)
    return [deepcopy(item["payload"]) for item in persisted]


async def _classifier_snapshot(
    profile: Any,
    *,
    minimum_packets: int | None = None,
    minimum_bytes: int | None = None,
) -> dict[str, Any]:
    from api.p4_evidence import read_p4_snapshot

    timeout = 8.0
    interval = 0.5
    deadline = asyncio.get_running_loop().time() + timeout
    last: dict[str, Any] = {}
    while True:
        last = await read_p4_snapshot(profile)
        if last.get("status") == "VERIFIED":
            if minimum_packets is None and minimum_bytes is None:
                return last
            if (
                int(last.get("packets") or 0) > int(minimum_packets or 0)
                or int(last.get("bytes") or 0) > int(minimum_bytes or 0)
            ):
                return last
        if last.get("status") not in {
            "VERIFIED", "NO_MATCHING_ENTRY", "SELECTOR_MISMATCH", "COUNTER_NOT_BOUND",
        }:
            return last
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return last
        await asyncio.sleep(min(interval, remaining))


async def _runtime_classifier_snapshot(
    policy: YANGPolicy,
    runtime: dict[str, Any],
    *,
    minimum_packets: int | None = None,
    minimum_bytes: int | None = None,
) -> dict[str, Any]:
    """Read the exact policy-owned direct counter from ONOS/P4Runtime.

    A QoS policy always owns a classifier entry even when an optional static
    ServiceProfile counter hint is absent.  Runtime binding is authoritative
    for dynamic services and independently allocated classifiers.
    """
    from api.qos import verify_p4_classifier

    try:
        value = await verify_p4_classifier(
            policy,
            runtime,
            require_counter_hit=minimum_packets is not None or minimum_bytes is not None,
            minimum_packets=minimum_packets,
            minimum_bytes=minimum_bytes,
        )
        return {
            **value,
            "status": "VERIFIED",
            "matching_entry_count": int(value.get("matching_entry_count") or 1),
            "table": value.get("table_name") or "MyIngress.sla_enforcement",
            "table_name": value.get("table_name") or "MyIngress.sla_enforcement",
            "counter": "MyIngress.qos_classifier_counter",
        }
    except Exception as exc:
        return {
            "status": "READ_FAILED", "matching_entry_count": 0,
            "packets": 0, "bytes": 0,
            "table": "MyIngress.sla_enforcement",
            "counter": "MyIngress.qos_classifier_counter",
            "error": str(exc),
        }


def _evidence_rollup(p4_status: str, tc_status: str, *, p4_required: bool, tc_required: bool) -> str:
    p4_ok = not p4_required or p4_status == "VERIFIED"
    tc_ok = not tc_required or tc_status in {"MEASURED", "VERIFIED"}
    required = int(p4_required) + int(tc_required)
    if required == 0:
        return EvidenceStatus.UNAVAILABLE.value
    if p4_ok and tc_ok:
        return EvidenceStatus.VERIFIED.value
    if (p4_required and p4_ok) or (tc_required and tc_ok):
        return EvidenceStatus.PARTIAL.value
    return EvidenceStatus.FAILED.value


def _kpi_evaluation(profile: Any, policy: YANGPolicy | None, result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("protocol_metrics") or {}
    protocol = profile.protocol.value
    observed = (
        metrics.get("received_mbps") if protocol == "UDP"
        else metrics.get("receiver_mbps") if protocol == "TCP"
        else None
    )
    qos = qos_values(policy) if policy is not None else {}
    mode = effective_qos_mode(policy) if policy is not None else ""
    bandwidth = qos.get("bandwidth")
    primary: dict[str, Any]
    if observed is None or bandwidth is None:
        primary = {
            "metric": "throughput", "target": bandwidth, "measured": observed,
            "unit": "Mbps", "status": "NOT_EVALUATED",
        }
    elif mode == "RATE_LIMIT":
        maximum = float(bandwidth) * 1.10
        primary = {
            "metric": "throughput", "operator": "<=", "target": maximum,
            "declared_target": float(bandwidth), "measured": float(observed),
            "unit": "Mbps", "status": "PASS" if float(observed) <= maximum else "FAIL",
        }
    else:
        minimum = float(bandwidth) * 0.95
        primary = {
            "metric": "throughput", "operator": ">=", "target": minimum,
            "declared_target": float(bandwidth), "measured": float(observed),
            "unit": "Mbps", "status": "PASS" if float(observed) >= minimum else "FAIL",
        }

    thresholds = profile.validation_thresholds or {}
    quality: dict[str, Any] = {}
    for metric_name, threshold_name, unit in (
        ("loss_percent", "maximum_loss_percent", "%"),
        ("jitter_ms", "maximum_jitter_ms", "ms"),
    ):
        measured = metrics.get(metric_name)
        target = thresholds.get(threshold_name)
        quality[metric_name.removesuffix("_percent").removesuffix("_ms")] = {
            "measured": measured,
            "maximum": target,
            "unit": unit,
            "status": (
                "NOT_EVALUATED" if measured is None or target is None
                else "PASS" if float(measured) <= float(target)
                else "WARN"
            ),
        }
    quality_degraded = any(item["status"] == "WARN" for item in quality.values())
    if primary["status"] == "FAIL":
        overall = "FAILED"
        kpi_status = KpiStatus.FAILED.value
    elif primary["status"] == "PASS" and quality_degraded:
        overall = "VERIFIED_WITH_WARNING"
        kpi_status = KpiStatus.DEGRADED.value
    elif primary["status"] == "PASS":
        overall = "VERIFIED"
        kpi_status = KpiStatus.VERIFIED.value
    else:
        overall = "NOT_EVALUATED"
        kpi_status = KpiStatus.NOT_EVALUATED.value
    return {
        "primary_sla": primary,
        "quality": quality,
        "quality_status": "DEGRADED" if quality_degraded else "PASS",
        "overall": overall,
        "kpi_status": kpi_status,
    }


async def run_service_validation(
    service_id: str,
    request: dict[str, Any] | None = None,
    *,
    scenario_id: str | None = None,
    yang_policy: YANGPolicy | dict[str, Any] | None = None,
    execution_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one exact ServiceProfile through Mininet and enrich P4/TC evidence."""
    from api._probe_session import get_probe_session
    from api.qos import p4_counter_evidence

    profile = get_service_profile(service_id)
    request = dict(request or {})
    validation_run_id = str(request.get("validation_run_id") or f"VR-{uuid4().hex[:16]}")
    validation_started_at = _utc_now()
    active = _active_policy_for_service(service_id) if yang_policy is None else None
    policy = (
        yang_policy if isinstance(yang_policy, YANGPolicy)
        else YANGPolicy(**yang_policy) if isinstance(yang_policy, dict)
        else active[0] if active else None
    )
    store = active[1] if active else {}
    runtime = dict(execution_result or store.get("runtime") or {})
    if policy is not None:
        matched, mismatches = profile_matches_policy(profile, policy)
        if not matched:
            raise ValidationProfileMismatch(
                f"active policy differs from {service_id}: {', '.join(mismatches)}"
            )

    payload = {
        **request,
        "service_id": profile.service_id,
        "validation_profile_id": profile.service_id,
        "intent_id": request.get("intent_id") or store.get("intent_id"),
        "policy_id": request.get("policy_id") or (
            policy.network_policy.policy_id if policy else None
        ),
        "validation_run_id": validation_run_id,
    }
    if policy is not None:
        payload.update({
            "traffic_scope_key": traffic_scope_key(policy).key,
            "qos_mode": effective_qos_mode(policy),
            "bandwidth_mbps": qos_values(policy).get("bandwidth"),
        })
    if scenario_id:
        scenario = get_traffic_scenario(scenario_id)
        if scenario.target_service_id != service_id:
            raise ValidationProfileMismatch(
                f"scenario {scenario_id} targets {scenario.target_service_id}, not {service_id}"
            )
        payload["scenario_id"] = scenario_id

    qos_reconciliation: dict[str, Any] | None = None
    if policy is not None and qos_values(policy).get("bandwidth"):
        from api.qos import apply_qos_policy, verify_qos_policy

        try:
            verified_qos = await verify_qos_policy(
                policy, selected_path=request.get("selected_path")
            )
        except Exception as exc:
            verified_qos = {
                "status": "MISMATCH",
                "config_applied": False,
                "error": str(exc),
            }
        if verified_qos.get("config_applied"):
            qos_reconciliation = {
                "status": "VERIFIED",
                "operation": "VERIFY",
                "readback": verified_qos,
            }
        else:
            restored_qos = await apply_qos_policy(
                policy,
                operation="APPLY",
                selected_path=request.get("selected_path"),
            )
            qos_reconciliation = {
                "status": "RESTORED" if restored_qos.get("config_applied") else "RESTORE_FAILED",
                "operation": "APPLY",
                "verify_before": verified_qos,
                "readback": restored_qos,
            }
            if not restored_qos.get("config_applied"):
                return {
                    **no_sample_result(service_id),
                    "status": MeasurementStatus.TELEMETRY_UNAVAILABLE.value,
                    "qos_reconciliation": qos_reconciliation,
                    "errors": [{
                        "code": "TC_QOS_RESTORE_FAILED",
                        "message": "active policy TC resource could not be restored before validation",
                    }],
                }

    runtime_classifier_required = bool(
        policy is not None
        and policy.network_policy.action.action_type in {
            ActionType.RATE_LIMIT,
            ActionType.SET_PRIORITY,
        }
        and (runtime.get("installed_flows") or runtime.get("delivered_rules"))
    )
    p4_required = bool(profile.p4_counter_binding or runtime_classifier_required)
    p4_reconciliation: dict[str, Any] = {"status": "NOT_APPLICABLE"}
    if policy is not None and p4_required:
        try:
            from api.qos import ensure_p4_classifier

            _verified, repaired_runtime, p4_reconciliation = await ensure_p4_classifier(
                policy, runtime
            )
            runtime = repaired_runtime
            if p4_reconciliation.get("status") == "RESTORED":
                from persistence.policy_store import PolicyStore

                PolicyStore().commit_staged(
                    traffic_scope_key(policy).key,
                    intent_id=str(payload.get("intent_id") or "SERVICE_VALIDATION_RECONCILE"),
                    runtime=runtime,
                )
        except Exception as exc:
            p4_reconciliation = {
                "status": "FAILED",
                "operation": "ONOS_REINSTALL_AND_POLL",
                "reason_code": "CLASSIFIER_RECONCILIATION_FAILED",
                "reason_detail": str(exc),
                "exception": {"type": type(exc).__name__, "detail": str(exc)},
            }

    if profile.protocol.value == "ICMP":
        before = {
            "status": "NOT_APPLICABLE", "matching_entry_count": 0,
            "packets": 0, "bytes": 0,
        }
    elif runtime_classifier_required and policy is not None:
        before = await _runtime_classifier_snapshot(policy, runtime)
    elif not profile.p4_counter_binding:
        before = {
            "status": "UNSUPPORTED", "matching_entry_count": 0,
            "packets": 0, "bytes": 0,
            "reason": "ServiceProfile has no bound direct counter",
        }
    else:
        before = await _classifier_snapshot(profile)
    before["validation_run_id"] = validation_run_id
    probe = await get_probe_session()
    endpoint = "/probe/traffic-scenario" if scenario_id else "/probe/service/validate"
    # The Mininet probe intentionally serializes heavyweight iperf runs.  Do
    # the same before opening the HTTP request so time spent waiting for a
    # previous probe does not consume this request's bounded timeout.
    ingress_allocation: dict[str, Any] = {"status": "NOT_APPLICABLE", "write_executed": False}
    ingress_restore: dict[str, Any] = {"status": "NOT_APPLICABLE", "write_executed": False}
    try:
        async with _active_probe_lock:
            ingress_allocation = await _activate_validation_ingress_allocation(
                policy=policy,
                request=payload,
                target_demand_mbps=float(payload.get("offered_rate_mbps") or 0),
            )
            result = await probe.post(endpoint, json_data=payload)
    finally:
        # A synthetic validation contender must not remain resident after the
        # bounded probe.  Restore the durable idle-borrowing allocation by
        # the same reconciler path even when the traffic command fails.
        ingress_restore = await _restore_validation_ingress_allocation(
            policy=policy,
            allocation=ingress_allocation,
        )
    if not isinstance(result, dict):
        result = {
            **no_sample_result(service_id),
            "status": MeasurementStatus.TELEMETRY_UNAVAILABLE.value,
            "errors": [{"code": "TELEMETRY_UNAVAILABLE", "message": "empty Mininet response"}],
        }
    result["service_name"] = profile.service_name
    result["profile_snapshot"] = profile.model_dump(mode="json")
    result["qos_reconciliation"] = qos_reconciliation or {"status": "NOT_APPLICABLE"}
    result["p4_reconciliation"] = p4_reconciliation
    result["ingress_admission_allocation"] = ingress_allocation
    result["ingress_admission_restore"] = ingress_restore
    result["validation_run_id"] = validation_run_id
    result["validation_started_at"] = validation_started_at
    result["measurement_status"] = str(result.get("status") or MeasurementStatus.NO_SAMPLE.value)
    # ONOS refreshes BMv2 direct counters asynchronously.  For the post-traffic
    # read, wait for a value strictly newer than the exact classifier snapshot;
    # otherwise a fast REST read can incorrectly turn real traffic into a
    # MATCHING_ENTRY_NO_TRAFFIC result.
    after = (
        await (
            _runtime_classifier_snapshot(
                policy,
                runtime,
                minimum_packets=int(before.get("packets") or 0),
                minimum_bytes=int(before.get("bytes") or 0),
            )
            if runtime_classifier_required and policy is not None
            else _classifier_snapshot(
                profile,
                minimum_packets=int(before.get("packets") or 0),
                minimum_bytes=int(before.get("bytes") or 0),
            )
        )
        if (
            result.get("status") == "MEASURED"
            and p4_required
        )
        else before
    )
    after["validation_run_id"] = validation_run_id
    counter = p4_counter_evidence(before, after)
    counter["validation_run_id"] = validation_run_id
    result["p4_counter_evidence"] = counter
    result["p4_classifier_evidence"] = {"before": before, "after": after}

    validation = dict(result.get("validation_result") or {})
    tc = result.get("tc_evidence") or {}
    tc["validation_run_id"] = validation_run_id
    result["tc_evidence"] = tc
    tc_required = bool(policy is not None and qos_values(policy).get("bandwidth"))
    evidence_status = _evidence_rollup(
        str(counter.get("status") or "UNAVAILABLE"),
        str(tc.get("status") or "UNAVAILABLE"),
        p4_required=p4_required,
        tc_required=tc_required,
    )
    result["evidence_status"] = evidence_status
    result["evidence_summary"] = {
        "status": evidence_status,
        "p4": counter.get("status"),
        "tc": "VERIFIED" if tc.get("status") == "MEASURED" else tc.get("status"),
        "validation_run_id": validation_run_id,
    }

    if result.get("status") == "MEASURED" and profile.protocol.value in {"UDP", "TCP"}:
        kpi = _kpi_evaluation(profile, policy, result)
        validation.update(kpi)
        validation["passed"] = kpi["primary_sla"]["status"] == "PASS"
        validation["verification_state"] = kpi["overall"]
        result["kpi_status"] = kpi["kpi_status"]
        result["overall_status"] = kpi["overall"]
    else:
        result["kpi_status"] = (
            KpiStatus.VERIFIED.value if validation.get("passed")
            else KpiStatus.FAILED.value if result.get("status") == "MEASURED"
            else KpiStatus.NOT_EVALUATED.value
        )
        result["overall_status"] = validation.get("verification_state") or result["kpi_status"]
    validation["measurement_status"] = result["measurement_status"]
    validation["evidence_status"] = evidence_status
    validation["validation_run_id"] = validation_run_id
    result["validation_result"] = validation

    metrics = result.get("protocol_metrics") or {}
    tc_delta = tc.get("delta") or {}
    if profile.protocol.value == "UDP":
        sender_packets = metrics.get("sent_packets")
        receiver_packets = metrics.get("received_packets")
        p4_packets = counter.get("delta_packets")
        tc_packets = tc_delta.get("sent_packets")
        tc_dropped = tc_delta.get("dropped_packets")
        result["packet_accounting"] = {
            "validation_run_id": validation_run_id,
            "sender_packets": sender_packets,
            "p4_classified_packets": p4_packets,
            "tc_class_packets": tc_packets,
            "receiver_packets": receiver_packets,
            "udp_lost_packets": metrics.get("lost_packets"),
            "tc_dropped_packets": tc_dropped,
            "tc_overlimits": tc_delta.get("overlimits"),
            "interface_counters": result.get("interface_counters") or {},
            "loss_attribution": (
                "NOT_HTB_DROP; inspect post-TC/BMv2/receiver scheduling and UDP socket capacity"
                if int(metrics.get("lost_packets") or 0) > 0 and int(tc_dropped or 0) == 0
                else "HTB_DROP_OBSERVED" if int(tc_dropped or 0) > 0
                else "NO_UDP_LOSS_OBSERVED"
            ),
        }

    validation_ended_at = _utc_now()
    result["validation_ended_at"] = validation_ended_at
    result["validation_context"] = {
        "validation_run_id": validation_run_id,
        "service_id": service_id,
        "intent_id": payload.get("intent_id"),
        "policy_id": payload.get("policy_id"),
        "started_at": validation_started_at,
        "ended_at": validation_ended_at,
    }
    result["policy_binding"] = {
        "status": str(store.get("status") or "ACTIVE") if policy else "NO_ACTIVE_POLICY",
        "traffic_scope_key": traffic_scope_key(policy).key if policy else None,
        "pending_intent_id": store.get("pending_intent_id") if policy else None,
    }
    await _remember(result)

    for background in result.get("background_results") or []:
        background_id = background.get("service_id")
        if not background_id:
            continue
        background_profile = get_service_profile(background_id)
        background_result = {
            **no_sample_result(background_id),
            "probe_id": result.get("probe_id"),
            "service_name": background_profile.service_name,
            "protocol": background.get("protocol"),
            "selector": background.get("selector") or background_profile.selector(),
            "start_time": result.get("start_time"),
            "end_time": result.get("end_time"),
            "duration_seconds": background.get("duration_seconds"),
            "status": "MEASURED",
            "measurement_source": background.get("measurement_source"),
            "offered_load": {"target_mbps": background.get("offered_rate_mbps")},
            "received_load": {},
            "protocol_metrics": background.get("protocol_metrics") or {},
            "validation_result": {
                "passed": True,
                "status": "MEASURED",
                "verification_state": "BACKGROUND_SERVICE_MEASURED",
            },
        }
        await _remember(background_result)
    return result


async def run_traffic_scenario(
    scenario_id: str,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scenario = get_traffic_scenario(scenario_id)
    return await run_service_validation(
        scenario.target_service_id,
        request,
        scenario_id=scenario_id,
    )


async def run_formal_assurance_batch(intent_id: str) -> dict[str, Any]:
    """Run the fixed five-run contention contract without repair/replanning.

    This is intentionally separate from ``run_service_validation`` because
    that legacy path may reconcile QoS/P4 before testing.  Formal assurance is
    evidence collection only: it reads existing state, drives the fixed probe,
    reads the exact same P4Runtime entry again, and registers the raw result.
    """
    precheck = await run_formal_assurance_precheck(intent_id)
    if not precheck["passed"]:
        raise ValueError("FORMAL_PRECHECK_FAILED:" + ",".join(precheck["validation_errors"]))

    from api._probe_session import get_probe_session
    from api.p4_evidence import read_p4_snapshot
    from mcp_servers.p4info_registry import p4info_registry
    from persistence.intent_domain_store import get_intent_domain_store
    from services.formal_assurance_evidence import FormalAssuranceEvidenceService

    domain = get_intent_domain_store(); runtime = domain.get_runtime(intent_id)
    if runtime is None: raise KeyError(intent_id)
    active = _active_policy_for_service("research_video")
    if active is None: raise ValueError("POLICY_LINEAGE_UNRESOLVED")
    policy, store = active
    desired_policy_id = ((runtime.desired_policy or {}).get("network_policy") or {}).get("policy_id")
    execution_runtime = dict(store.get("runtime") or {})
    binding = ((execution_runtime.get("qos_enforcement") or {}).get("resource_binding") or {})
    if desired_policy_id != policy.network_policy.policy_id:
        reused = (
            execution_runtime.get("existing_policy_id") == policy.network_policy.policy_id
            and execution_runtime.get("classifier_reused") is True
            and binding.get("policy_id") == desired_policy_id
            and bool(binding.get("binding_id"))
        )
        if not reused:
            raise ValueError("POLICY_LINEAGE_UNRESOLVED")
    profile = get_service_profile("research_video")
    trace_id = f"FORMAL-RUN-{uuid4().hex[:16]}"; runs: list[dict[str, Any]] = []
    fingerprint = __import__("hashlib").sha256(
        f"{p4info_registry.table_id('MyIngress.sla_enforcement')}:{p4info_registry.action_id('MyIngress.set_dscp')}:{p4info_registry.direct_counter_id('MyIngress.qos_classifier_counter')}".encode()
    ).hexdigest()
    probe = await get_probe_session()
    for index in range(1, 6):
        domain.record_event(intent_id, "VALIDATION_RUN_STARTED", actor="formal_validation_runner", reason="fixed contention run started", data={"sample_index":index,"trace_id":trace_id})
        before = await read_p4_snapshot(profile)
        payload = {"service_id":"research_video", "intent_id":intent_id, "policy_id":policy.network_policy.policy_id, "traffic_scope_key":traffic_scope_key(policy).key, "qos_mode":"MIN_BANDWIDTH", "bandwidth_mbps":30, "validation_run_id":f"VR-FORMAL-{uuid4().hex[:16]}", "offered_rate_mbps":40, "duration_seconds":35, "warmup_seconds":5, "packet_size_bytes":1200, "background_profiles":[{"service_id":"background_video","offered_rate_mbps":95,"duration_seconds":35,"warmup_seconds":5,"packet_size_bytes":1200,"start_offset_seconds":0}]}
        raw = await probe.post("/probe/service/validate", json_data=payload)
        after = await read_p4_snapshot(profile)
        errors: list[str] = []
        if before.get("status") != "VERIFIED": errors.append("P4_COUNTER_READ_BEFORE_FAILED")
        if after.get("status") != "VERIFIED": errors.append("P4_COUNTER_READ_AFTER_FAILED")
        bp, bb = int(before.get("packets") or 0), int(before.get("bytes") or 0); ap, ab = int(after.get("packets") or 0), int(after.get("bytes") or 0)
        if not errors and (ap < bp or ab < bb): errors.append("P4_COUNTER_RESET")
        if not errors and (ap == bp or ab == bb): errors.append("P4_COUNTER_NO_DELTA")
        p4 = {"status":"VERIFIED" if not errors else errors[0], "device_id":after.get("device_id") or before.get("device_id"), "grpc_endpoint":after.get("grpc_endpoint") or before.get("grpc_endpoint"), "table_name":"MyIngress.sla_enforcement", "table_id":p4info_registry.table_id("MyIngress.sla_enforcement"), "direct_counter_name":"MyIngress.qos_classifier_counter", "direct_counter_id":p4info_registry.direct_counter_id("MyIngress.qos_classifier_counter"), "entry_selector":after.get("selector") or before.get("selector"), "entry_selector_hash":__import__("hashlib").sha256(str(after.get("selector") or before.get("selector")).encode()).hexdigest(), "action_name":"MyIngress.set_dscp", "action_id":p4info_registry.action_id("MyIngress.set_dscp"), "action_parameters":{"dscp_value":46}, "before_packets":bp, "before_bytes":bb, "after_packets":ap, "after_bytes":ab, "delta_packets":max(0,ap-bp), "delta_bytes":max(0,ab-bb), "before_observed_at":before.get("read_at"), "after_observed_at":after.get("read_at"), "counter_observation_scope":"WARMUP_PLUS_MEASUREMENT", "p4info_fingerprint":fingerprint, "device_epoch_before":before.get("device_id"), "device_epoch_after":after.get("device_id"), "execution_generation":runtime.active_execution_id or "execution-unavailable", "readback_generation":runtime.active_execution_id or "readback-unavailable", "desired_policy_id":desired_policy_id, "executed_resource_owner_policy_id":policy.network_policy.policy_id, "resource_binding_id":binding.get("binding_id"), "read_status":{"before":before.get("status"),"after":after.get("status")}, "validation_errors":errors}
        raw = dict(raw or {}); raw["p4_counter_evidence"] = p4; raw["intent_id"] = intent_id; raw["policy_id"] = policy.network_policy.policy_id; raw["formal_assurance_generation"] = trace_id
        runs.append(raw)
        domain.record_event(intent_id, "P4_COUNTER_EVIDENCE_VERIFIED" if not errors else "P4_COUNTER_EVIDENCE_REJECTED", actor="formal_validation_runner", reason=p4["status"], data={"sample_index":index,"validation_run_id":raw.get("validation_run_id"),"trace_id":trace_id})
    status, batch = FormalAssuranceEvidenceService(domain).register_internal_runs(intent_id=intent_id, runs=runs, trace_id=trace_id)
    return {"status":status,"trace_id":trace_id,"evidence_batch":batch.model_dump(mode="json"),"runs":runs}


async def run_formal_assurance_precheck(intent_id: str) -> dict[str, Any]:
    """Run one non-formal contention probe and enforce same-run evidence.

    This creates neither an assurance generation nor an EvidenceBatch.  Its
    sole purpose is to prove that the fixed request reaches the probe and that
    both TC layers supply evidence sufficient for the five formal samples.
    """
    from api._probe_session import get_probe_session
    from api.p4_evidence import read_p4_snapshot
    from persistence.intent_domain_store import get_intent_domain_store
    from services.formal_assurance_evidence import FormalAssuranceEvidenceService

    domain = get_intent_domain_store()
    runtime = domain.get_runtime(intent_id)
    if runtime is None:
        raise KeyError(intent_id)
    active = _active_policy_for_service("research_video")
    if active is None:
        raise ValueError("POLICY_LINEAGE_UNRESOLVED")
    policy, store = active
    execution_runtime = dict(store.get("runtime") or {})
    binding = ((execution_runtime.get("qos_enforcement") or {}).get("resource_binding") or {})
    lineage = FormalAssuranceEvidenceService(domain)._lineage(intent_id)
    scope = traffic_scope_key(policy)
    run_id = f"VR-PRECHECK-{uuid4().hex[:16]}"
    payload = {
        "service_id": "research_video",
        "intent_id": intent_id,
        "revision": runtime.current_revision,
        "policy_id": policy.network_policy.policy_id,
        "traffic_scope_key": scope.key,
        "qos_mode": "MIN_BANDWIDTH",
        "bandwidth_mbps": 30,
        "requested_bandwidth_mbps": 30,
        "source_ip": "192.168.10.10/32",
        "destination_ip": "10.0.0.10/32",
        # The probe profile API canonicalizes protocol names, while the formal
        # traffic contract records the corresponding IPv4 protocol number.
        # Carry both explicitly; do not coerce the probe's wire field to an
        # incompatible integer representation.
        "protocol": "UDP",
        "ip_protocol": 17,
        "destination_port": 5004,
        "dscp": 46,
        "ingress_admission_plan_id": lineage["ingress_admission_plan_id"],
        "allocation_epoch": lineage["ingress_allocation_epoch"],
        "resource_binding_id": binding.get("binding_id"),
        "execution_generation": lineage["execution_generation"],
        "readback_generation": lineage["readback_generation"],
        "environment_epoch": lineage["environment_epoch"],
        "validation_run_id": run_id,
        "offered_rate_mbps": 40,
        "duration_seconds": 35,
        "warmup_seconds": 5,
        "packet_size_bytes": 1200,
        "background_profiles": [{
            "service_id": "background_video", "offered_rate_mbps": 95,
            "duration_seconds": 35, "warmup_seconds": 5,
            "packet_size_bytes": 1200, "start_offset_seconds": 0,
        }],
        "validation_mode": "PRECHECK_ONLY",
    }
    from infra.network_reconciler import get_network_reconciler
    allocation = await get_network_reconciler().activate_ingress_runtime_allocation(
        target_demand_mbps=float(payload["offered_rate_mbps"]),
        active_best_effort_scope_ids={"BE-ACTIVE-1"},
    )
    before = await read_p4_snapshot(get_service_profile("research_video"))
    raw = dict(await (await get_probe_session()).post("/probe/service/validate", json_data=payload) or {})
    after = await read_p4_snapshot(get_service_profile("research_video"))
    errors: list[str] = []
    if before.get("status") != "VERIFIED" or after.get("status") != "VERIFIED":
        errors.append("P4_COUNTER_READ_FAILED")
    elif int(after.get("packets") or 0) <= int(before.get("packets") or 0) or int(after.get("bytes") or 0) <= int(before.get("bytes") or 0):
        errors.append("P4_COUNTER_NO_DELTA")
    tc = raw.get("tc_evidence") or {}
    if tc.get("status") != "MEASURED":
        errors.append("TC_EGRESS_READBACK_UNVERIFIED")
    # The probe must echo the structured request after parsing it.  Comparing
    # a local Python object is not evidence that the remote probe received it.
    received = raw.get("probe_received_payload")
    propagation_fields = ("intent_id", "revision", "policy_id", "traffic_scope_key", "qos_mode", "requested_bandwidth_mbps", "source_ip", "destination_ip", "protocol", "ip_protocol", "destination_port", "dscp", "ingress_admission_plan_id", "allocation_epoch", "resource_binding_id", "execution_generation", "readback_generation", "environment_epoch")
    if not isinstance(received, dict):
        errors.append("TC_PROBE_REQUEST_ECHO_MISSING")
    elif any(received.get(field) != payload.get(field) for field in propagation_fields):
        errors.append("TC_SCOPE_KEY_MISMATCH")
    ingress = raw.get("ingress_admission_evidence")
    if not isinstance(ingress, dict) or not isinstance(ingress.get("before"), dict) or not isinstance(ingress.get("after"), dict):
        errors.append("TC_INGRESS_SAME_RUN_READBACK_UNAVAILABLE")
    elif int((ingress.get("delta") or {}).get("target_packets") or 0) <= 0:
        errors.append("TC_INGRESS_COUNTER_NO_DELTA")
    return {
        "mode": "PRECHECK_ONLY",
        "passed": not errors,
        "validation_errors": errors,
        "runner_input": payload,
        "constructed_probe_payload": payload,
        "probe_received_payload": received,
        "resolved_traffic_scope_key": scope.key,
        "p4_before": before,
        "p4_after": after,
        "tc_evidence": tc,
        "ingress_admission_evidence": ingress,
        "raw_probe_result": raw,
        "ingress_runtime_allocation": allocation,
    }


async def current_service_p4_evidence(service_id: str) -> dict[str, Any]:
    """Return live P4Runtime binding plus the latest same-run counter delta."""
    from api.p4_evidence import read_p4_snapshot

    profile = get_service_profile(service_id)
    active = _active_policy_for_service(service_id)
    if active and (active[1].get("runtime") or {}).get("installed_flows"):
        current = await _runtime_classifier_snapshot(active[0], active[1]["runtime"])
    else:
        current = await read_p4_snapshot(profile)
    latest = latest_service_metrics(service_id)
    latest_counter = latest.get("p4_counter_evidence") or {}
    same_binding = (
        latest_counter.get("device_id") == current.get("device_id")
        and latest_counter.get("table_id") == current.get("table_id")
        and latest_counter.get("counter_id") == current.get("counter_id")
    )
    counter = (
        {
            "before_packets": latest_counter.get("before_packets"),
            "after_packets": latest_counter.get("after_packets"),
            "delta_packets": latest_counter.get("delta_packets"),
            "before_bytes": latest_counter.get("before_bytes"),
            "after_bytes": latest_counter.get("after_bytes"),
            "delta_bytes": latest_counter.get("delta_bytes"),
            "validation_run_id": latest.get("validation_run_id"),
        }
        if same_binding and latest.get("validation_run_id") else {
            "before_packets": None,
            "after_packets": current.get("packets"),
            "delta_packets": None,
            "before_bytes": None,
            "after_bytes": current.get("bytes"),
            "delta_bytes": None,
            "validation_run_id": None,
        }
    )
    return {
        **current,
        "table": current.get("table_name"),
        "flow_state": "INSTALLED_IN_BMV2" if current.get("status") == "VERIFIED" else None,
        "counter": counter,
        "measurement_status": latest.get("measurement_status") or latest.get("status"),
        "evidence_status": latest.get("evidence_status"),
    }


async def verify_no_iperf_leaks() -> dict[str, Any]:
    from api._probe_session import get_probe_session

    probe = await get_probe_session()
    return await probe.get("/probe/processes") or {
        "status": "unavailable",
        "process_cleanup_verified": False,
    }

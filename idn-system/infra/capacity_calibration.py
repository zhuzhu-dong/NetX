"""Formal live capacity calibration for an active ingress desired generation."""
from __future__ import annotations

import hashlib
import json
import math
import os
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from api.store import IntentStore, get_store
from api.service_profiles import resolve_service_profile_for_policy
from infra.ingress_admission_state import IngressAdmissionDesiredRepository
from infra.network_write_fencing import NetworkWriteFencing, resource_write_scope, write_context
from infra.qos_resource_allocator import QoSResourceAllocator
from infra.stable_capacity import StableCapacityRepository, conservative_capacity_estimate
from models.policy_update import effective_qos_mode, qos_values
from models.resources import StableDataplaneCapacityRecord
from models.yang_policy import YANGPolicy
from persistence.policy_store import PolicyStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CapacityCalibrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CalibrationCandidate:
    """Non-durable authority for first-time capacity measurement.

    A candidate is created only after semantic resolution, policy modeling and
    resource resolution have completed.  It is deliberately not a Policy,
    Binding, reservation or ingress desired record: it authorizes one fenced
    measurement window and is discarded whether that measurement succeeds or
    fails.
    """

    intent_id: str
    traffic_scope_key: str
    staged_policy: YANGPolicy
    service_id: str | None
    bottleneck_egress: str
    requested_schedulable_mbps: float
    topology_id: str
    path_information: dict[str, Any]
    trace_id: str
    created_at: str


class ActiveIngressCapacityCalibrator:
    """Measure a current service path before refreshing its capacity evidence.

    This is intentionally not an arbitrary ``PUT stable capacity`` wrapper.
    It runs the existing Mininet receiver-side iperf probe repeatedly, retains
    all samples in the audit event, takes the conservative lower bound, and
    refuses to publish an evidence record that cannot support the current
    ingress scheduler contract.
    """

    def __init__(
        self,
        store: IntentStore | None = None,
        probe: Any = None,
        p4_reader: Any = None,
    ):
        self.store = store or get_store()
        self.probe = probe
        # Dependency injection is intentionally limited to the read-only P4
        # evidence call.  Production continues to use the direct reader; the
        # seam lets regression tests prove that an unavailable P4Runtime path
        # fails closed instead of silently manufacturing capacity evidence.
        self.p4_reader = p4_reader

    async def calibrate_candidate(
        self,
        candidate: CalibrationCandidate,
        *,
        samples: int = 5,
        duration_seconds: int = 3,
    ) -> dict[str, Any]:
        """Calibrate a first-admission candidate without materializing QoS state.

        This path exists specifically to break the fresh-install dependency
        cycle: a new guarantee cannot have an active Binding before it has
        passed admission, yet admission must be based on a real conservative
        measurement.  The candidate is held solely in memory/audit payloads;
        a StableDataplaneCapacityRecord is written only after all receiver-side
        samples support the requested schedulable capacity.
        """
        samples = max(3, min(int(samples), 10))
        duration_seconds = max(1, min(int(duration_seconds), 15))
        warmup_seconds = max(0, min(int(os.getenv("IDN_CAPACITY_CALIBRATION_WARMUP_SECONDS", "1")), 15))
        sample_interval_seconds = max(0, min(float(os.getenv("IDN_CAPACITY_CALIBRATION_SAMPLE_INTERVAL_SECONDS", "1")), 10.0))
        policy = candidate.staged_policy
        try:
            profile = resolve_service_profile_for_policy(policy)
        except Exception as exc:
            raise CapacityCalibrationError("CALIBRATION_SERVICE_PROFILE_UNRESOLVED") from exc

        from infra.resource_registry import get_resource_registry

        registry = get_resource_registry().snapshot()
        if str(registry.topology_name) != str(candidate.topology_id):
            raise CapacityCalibrationError("CALIBRATION_TOPOLOGY_CHANGED")
        resource = next(
            (item for item in registry.links if item.resource_id == candidate.bottleneck_egress),
            None,
        )
        if resource is None:
            raise CapacityCalibrationError("CALIBRATION_RESOURCE_NOT_CURRENT")
        required = float(candidate.requested_schedulable_mbps)
        if required <= 0:
            raise CapacityCalibrationError("CALIBRATION_SCHEDULABLE_CONTRACT_MISSING")
        offered = min(
            int(resource.capacity_mbps),
            max(int(math.ceil(required + 10)), int(math.ceil(required * 1.2))),
        )
        probe = self.probe
        if probe is None:
            from api._probe_session import get_probe_session

            probe = await get_probe_session()

        capacity_record_id = "CAP-" + hashlib.sha256(
            f"{candidate.traffic_scope_key}:{candidate.bottleneck_egress}".encode()
        ).hexdigest()[:16]
        observed: list[dict[str, Any]] = []
        delivered: list[float] = []
        fence = NetworkWriteFencing(self.store)
        lease = fence.acquire(
            owner_type="PROVISIONAL_CAPACITY_CALIBRATION",
            owner_id=candidate.intent_id,
            recovery_id=f"CAL-CANDIDATE-{candidate.intent_id}",
            resource_scope=resource_write_scope(candidate.bottleneck_egress),
            ttl_seconds=max(120, warmup_seconds + samples * (duration_seconds + 12) + int(sample_interval_seconds * max(samples - 1, 0))),
        )

        async def sample_evidence() -> dict[str, Any]:
            try:
                reader = self.p4_reader
                if reader is None:
                    from api.p4_evidence import read_p4_snapshot

                    reader = read_p4_snapshot
                p4 = await reader(profile)
                if str((p4 or {}).get("status") or "").upper() not in {
                    "VERIFIED",
                    "MATCHING_ENTRY_NO_TRAFFIC",
                    # A candidate has not installed a classifier.  An exact
                    # no-match is expected, but it still proves the direct
                    # P4Runtime read completed successfully.
                    "NO_MATCHING_ENTRY",
                }:
                    raise CapacityCalibrationError("CALIBRATION_P4_EVIDENCE_UNAVAILABLE")
            except Exception as exc:
                # A candidate has no classifier yet, but it must still prove
                # that P4Runtime readback is reachable for the exact tuple.
                # Treat a transport/read failure as a failed calibration;
                # otherwise a healthy iperf alone could create authority for
                # a dataplane whose P4 control path is unavailable.
                raise CapacityCalibrationError("CALIBRATION_P4_EVIDENCE_UNAVAILABLE") from exc
            try:
                tc = await probe.get(
                    "/policy/qos-readback", {"scope_key": candidate.traffic_scope_key}
                ) or {}
            except Exception as exc:
                tc = {"status": "unavailable", "error": str(exc)}
            try:
                udp = await probe.get(
                    "/probe/udp-kernel-stats", {"host": profile.dst_host}
                ) or {}
            except Exception as exc:
                udp = {"status": "unavailable", "error": str(exc)}
            return {"p4": p4, "tc": tc, "receiver_udp": udp}

        async def diagnostics() -> dict[str, Any]:
            """Collect best-effort runtime data; failures never change admission."""
            result: dict[str, Any] = {"status": "ok"}
            for name, path, query in (
                ("environment", "/diagnostics/environment", {}),
                ("processes", "/probe/processes", {"hosts": f"{profile.src_host},{profile.dst_host}"}),
                ("sender_udp", "/probe/udp-kernel-stats", {"host": profile.src_host}),
                ("receiver_udp", "/probe/udp-kernel-stats", {"host": profile.dst_host}),
            ):
                try:
                    result[name] = await probe.get(path, query) or {}
                except Exception as exc:  # Diagnostics are audit-only.
                    result[name] = {"status": "unavailable", "error": str(exc)}
            return result

        def probe_request(duration: int) -> dict[str, Any]:
            return {
                "src": profile.src_host,
                "dst": profile.dst_host,
                "protocol": profile.protocol.value,
                "port": profile.dst_port,
                "bandwidth_mbps": offered,
                "duration": duration,
                "dscp": profile.dscp,
                "packet_size_bytes": profile.packet_size_bytes,
            }

        def sample_record(sample: int, value: dict[str, Any], before: dict[str, Any], after: dict[str, Any], started: datetime, ended: datetime) -> dict[str, Any]:
            datagrams_sent = value.get("sent_packets") or value.get("packets")
            lost = value.get("lost_packets")
            return {
                "sample": sample,
                "started_at": started.isoformat(),
                "ended_at": ended.isoformat(),
                "wall_duration_seconds": round((ended - started).total_seconds(), 3),
                "status": value.get("status"),
                "offered_mbps": offered,
                "sender_mbps": value.get("sent_mbps"),
                "receiver_mbps": value.get("received_mbps") or value.get("throughput_mbps"),
                "packet_loss_percent": value.get("loss_percent"),
                "jitter_ms": value.get("jitter_ms"),
                "datagrams_sent": datagrams_sent,
                "datagrams_received": (
                    max(int(datagrams_sent) - int(lost), 0)
                    if datagrams_sent is not None and lost is not None else None
                ),
                "iperf_exit_status": value.get("iperf_exit_status"),
                "iperf_server_exit_status": value.get("iperf_server_exit_status"),
                "effective_probe_arguments": value.get("effective_arguments") or probe_request(duration_seconds),
                "socket_buffer_bytes": value.get("socket_buffer_bytes"),
                "probe": value,
                "evidence_before": before,
                "evidence_after": after,
            }

        def reject(reason: str, estimate: dict[str, Any] | None = None) -> None:
            self.store.add_audit_event(
                event_type="CANDIDATE_CAPACITY_CALIBRATION_REJECTED",
                resource_type="stable_capacity",
                resource_id=capacity_record_id,
                correlation={
                    "intent_id": candidate.intent_id,
                    "traffic_scope_key": candidate.traffic_scope_key,
                    "trace_id": candidate.trace_id,
                },
                expected={"minimum_schedulable_mbps": required, "sample_count": samples},
                observed={
                    "reason": reason,
                    "offered_mbps": offered,
                    "samples": observed,
                    "delivered_mbps": delivered,
                    "estimate": estimate,
                    "candidate_authority": "PROVISIONAL_ADMISSION_CANDIDATE",
                    "warmup_seconds": warmup_seconds,
                    "sample_interval_seconds": sample_interval_seconds,
                },
                action={"operation": "LIVE_RECEIVER_IPERF_CANDIDATE_CALIBRATION", "network_configuration_write": False},
                result={"status": "REJECTED"},
            )

        try:
            with write_context(
                owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                fencing_token=int(lease["fencing_token"]), resource_scope=lease["resource_scope"],
                trace_id=candidate.trace_id,
            ):
                authorization = fence.authorize_network_configuration_write(
                    owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                    fencing_token=int(lease["fencing_token"]), resource_scope=lease["resource_scope"],
                    operation="CANDIDATE_CAPACITY_CALIBRATION_PROBE",
                    trace_id=candidate.trace_id,
                )
                if not authorization["authorized"]:
                    raise CapacityCalibrationError(authorization["reason"])
                # Candidate calibration has no committed classifier, TC class,
                # binding or ingress plan to open.  It performs no dataplane
                # configuration write; fencing still serializes this real probe
                # with resident reconciliation and other calibration windows.
                fence.record_execution(authorization, write_executed=False)
                warmup: dict[str, Any] | None = None
                if warmup_seconds:
                    warmup_started = _now()
                    try:
                        warmup_value = await probe.get("/probe/iperf", probe_request(warmup_seconds)) or {}
                        warmup = {
                            "status": warmup_value.get("status"),
                            "started_at": warmup_started.isoformat(),
                            "ended_at": _now().isoformat(),
                            "duration_seconds": warmup_seconds,
                            "probe_request": probe_request(warmup_seconds),
                            "receiver_mbps": warmup_value.get("received_mbps") or warmup_value.get("throughput_mbps"),
                            "probe": warmup_value,
                            "counted_in_capacity_estimate": False,
                        }
                    except Exception as exc:
                        warmup = {
                            "status": "error", "error": str(exc), "duration_seconds": warmup_seconds,
                            "probe_request": probe_request(warmup_seconds), "counted_in_capacity_estimate": False,
                        }
                for sample in range(samples):
                    before = await sample_evidence()
                    diagnostic_before = await diagnostics()
                    started = _now()
                    value = await probe.get("/probe/iperf", probe_request(duration_seconds)) or {}
                    ended = _now()
                    after = await sample_evidence()
                    diagnostic_after = await diagnostics()
                    mbps = value.get("received_mbps") or value.get("throughput_mbps")
                    record = sample_record(sample + 1, value, before, after, started, ended)
                    record["diagnostics_before"] = diagnostic_before
                    record["diagnostics_after"] = diagnostic_after
                    if sample == 0 and warmup is not None:
                        record["warmup"] = warmup
                    observed.append(record)
                    if value.get("status") == "ok" and mbps is not None and float(mbps) > 0:
                        delivered.append(float(mbps))
                    if sample < samples - 1 and sample_interval_seconds:
                        await asyncio.sleep(sample_interval_seconds)
        except CapacityCalibrationError as exc:
            reject(str(exc))
            raise
        except Exception as exc:
            reject("CALIBRATION_PROBE_EXCEPTION")
            raise CapacityCalibrationError("CALIBRATION_PROBE_EXCEPTION") from exc
        finally:
            fence.release(lease["lease_id"])

        if len(delivered) != samples:
            reject("CALIBRATION_SAMPLE_INCOMPLETE")
            raise CapacityCalibrationError("CALIBRATION_SAMPLE_INCOMPLETE")
        estimate = conservative_capacity_estimate(delivered)
        safety = float(os.getenv("IDN_CAPACITY_CALIBRATION_SAFETY_MARGIN_MBPS", "5"))
        schedulable = max(0.0, float(estimate["lower_bound_mbps"]) - safety)
        if math.floor(schedulable) < required:
            reject(
                "CALIBRATION_DOES_NOT_SUPPORT_CANDIDATE",
                {**estimate, "safety_margin_mbps": safety, "schedulable_capacity_mbps": schedulable},
            )
            raise CapacityCalibrationError("CALIBRATION_DOES_NOT_SUPPORT_CANDIDATE")

        now = _now()
        record = StableDataplaneCapacityRecord(
            capacity_record_id=capacity_record_id,
            topology_id=registry.topology_name,
            environment_epoch="CAL-" + hashlib.sha256(
                (registry.topology_name + now.isoformat()).encode()
            ).hexdigest()[:16],
            controller_epoch="ONOS-live",
            path_signature=str(candidate.path_information.get("path_id") or candidate.bottleneck_egress),
            bottleneck_egress=candidate.bottleneck_egress,
            packet_profile=f"{profile.protocol.value.lower()}-{profile.packet_size_bytes}-live-probe",
            protocol=profile.protocol.value,
            packet_size_bytes=profile.packet_size_bytes,
            observation_mode="LIVE_RECEIVER_IPERF_CANDIDATE_CALIBRATION",
            physical_capacity_mbps=float(resource.capacity_mbps),
            stable_capacity_mbps=float(estimate["lower_bound_mbps"]),
            capacity_floor_mbps=float(estimate["lower_bound_mbps"]),
            p05_capacity_mbps=float(estimate["p05_capacity_mbps"]),
            sample_count=int(estimate["sample_count"]),
            confidence=str(estimate["confidence"]),
            measured_at=now.isoformat(),
            valid_until=(now + timedelta(seconds=max(60, int(os.getenv("IDN_CAPACITY_CALIBRATION_TTL_SECONDS", "3600"))))).isoformat(),
            source_experiment="formal-live-mininet-receiver-iperf-candidate",
            status="ACTIVE",
            safety_margin_mbps=safety,
        )
        StableCapacityRepository(self.store).put(record)
        result = {
            "status": "CALIBRATED",
            "authority_source": "PROVISIONAL_ADMISSION_CANDIDATE",
            "traffic_scope_key": candidate.traffic_scope_key,
            "capacity_record": record.model_dump(mode="json"),
            "schedulable_capacity_mbps": schedulable,
            "samples": observed,
            "required_schedulable_mbps": required,
        }
        self.store.add_audit_event(
            event_type="STABLE_CAPACITY_CALIBRATED_FROM_CANDIDATE_LIVE_PROBE",
            resource_type="stable_capacity",
            resource_id=record.capacity_record_id,
            correlation={
                "intent_id": candidate.intent_id,
                "traffic_scope_key": candidate.traffic_scope_key,
                "trace_id": candidate.trace_id,
            },
            expected={"minimum_schedulable_mbps": required, "sample_count": samples},
            observed=result,
            action={"operation": "LIVE_RECEIVER_IPERF_CANDIDATE_CALIBRATION", "network_configuration_write": False},
            result={"status": "CALIBRATED"},
        )
        return result

    async def calibrate(
        self,
        traffic_scope_key: str,
        *,
        samples: int = 5,
        duration_seconds: int = 3,
        required_schedulable_mbps: float | None = None,
    ) -> dict[str, Any]:
        samples = max(3, min(int(samples), 10))
        duration_seconds = max(1, min(int(duration_seconds), 15))
        ingress = IngressAdmissionDesiredRepository(self.store).active_for_scope(traffic_scope_key)
        binding = QoSResourceAllocator(self.store).get_active_for_scope(traffic_scope_key)
        policy_row = PolicyStore(self.store).get_by_scope(traffic_scope_key)
        if not binding or not policy_row:
            raise CapacityCalibrationError("CALIBRATION_AUTHORITY_MISSING")
        try:
            policy = YANGPolicy(**json.loads(policy_row["yang_json"]))
            profile = resolve_service_profile_for_policy(policy)
        except Exception as exc:
            raise CapacityCalibrationError("CALIBRATION_SERVICE_PROFILE_UNRESOLVED") from exc
        if ingress:
            if binding.binding_id != ingress.get("resource_binding_id"):
                raise CapacityCalibrationError("CALIBRATION_BINDING_LINEAGE_MISMATCH")
            desired = dict(ingress.get("desired") or {})
            current_contract = float(desired.get("schedulable_capacity_mbps") or 0)
            capacity_record_id = str(ingress.get("capacity_record_id") or "")
            authority_source = "INGRESS_ADMISSION_DESIRED"
        else:
            # Generic active scopes do not necessarily participate in the
            # research-only ingress scheduler.  Their committed Policy plus
            # their unique active Binding are nevertheless durable authority
            # for a *read-only evidence refresh*.  Requiring a foreign
            # ingress generation here made a valid generic scope impossible
            # to calibrate and therefore impossible to update safely.
            qos = qos_values(policy)
            if effective_qos_mode(policy) != "MIN_BANDWIDTH" or not qos.get("bandwidth"):
                raise CapacityCalibrationError("CALIBRATION_SCHEDULABLE_CONTRACT_MISSING")
            current_contract = float(qos["bandwidth"])
            capacity_record_id = "CAP-" + hashlib.sha256(
                f"{traffic_scope_key}:{binding.resource_key}".encode()
            ).hexdigest()[:16]
            authority_source = "COMMITTED_POLICY_PLUS_ACTIVE_BINDING"
        requested_contract = float(required_schedulable_mbps or current_contract)
        if requested_contract < current_contract:
            raise CapacityCalibrationError("CALIBRATION_REQUIRED_CAPACITY_BELOW_COMMITTED_CONTRACT")
        protected = requested_contract
        if protected <= 0 or not capacity_record_id:
            raise CapacityCalibrationError("CALIBRATION_SCHEDULABLE_CONTRACT_MISSING")
        from infra.resource_registry import get_resource_registry
        registry = get_resource_registry().snapshot()
        resource = next((item for item in registry.links if item.resource_id == binding.resource_key), None)
        if resource is None:
            raise CapacityCalibrationError("CALIBRATION_RESOURCE_NOT_CURRENT")
        offered = min(int(resource.capacity_mbps), max(int(math.ceil(protected + 10)), int(math.ceil(protected * 1.2))))
        if self.probe is None:
            from api._probe_session import get_probe_session
            probe = await get_probe_session()
        else:
            probe = self.probe
        observed: list[dict[str, Any]] = []
        delivered: list[float] = []

        async def sample_evidence() -> dict[str, Any]:
            """Collect read-only evidence bound to exactly one probe sample."""
            try:
                from api.p4_evidence import read_p4_snapshot
                p4 = await read_p4_snapshot(profile)
            except Exception as exc:
                p4 = {"status": "unavailable", "error": str(exc)}
            try:
                tc = await probe.get("/policy/qos-readback", {"scope_key": traffic_scope_key}) or {}
            except Exception as exc:
                tc = {"status": "unavailable", "error": str(exc)}
            try:
                udp = await probe.get("/probe/udp-kernel-stats", {"host": profile.dst_host}) or {}
            except Exception as exc:
                udp = {"status": "unavailable", "error": str(exc)}
            return {"p4": p4, "tc": tc, "receiver_udp": udp}
        # The active classifier shares the calibration tuple.  A real probe
        # would otherwise be capped by the very root capacity record being
        # refreshed, making a safety-margin refresh mathematically impossible.
        # Hold one formal fence across both temporary actuator writes and all
        # receiver measurements so no resident repair can interleave them.
        fence = NetworkWriteFencing(self.store)
        lease = fence.acquire(
            owner_type="CAPACITY_CALIBRATION", owner_id=traffic_scope_key,
            recovery_id=f"CAL-{traffic_scope_key}",
            resource_scope=resource_write_scope(binding.resource_key),
            ttl_seconds=max(120, samples * (duration_seconds + 12)),
        )
        entered_egress = False
        entered_ingress = False
        restore_egress: dict[str, Any] | None = None
        restore_ingress: dict[str, Any] | None = None
        window_evidence: dict[str, Any] = {
            "egress": {"status": "ABSENT", "write_executed": False},
            "ingress": {"status": "ABSENT", "write_executed": False},
        }
        try:
            with write_context(
                owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                fencing_token=int(lease["fencing_token"]), resource_scope=lease["resource_scope"],
                trace_id=f"CAL-{traffic_scope_key}",
            ):
                authorization = fence.authorize_network_configuration_write(
                    owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                    fencing_token=int(lease["fencing_token"]), resource_scope=lease["resource_scope"],
                    operation="CAPACITY_CALIBRATION_WINDOW_ENTER", trace_id=f"CAL-ENTER-{traffic_scope_key}",
                )
                if not authorization["authorized"]:
                    raise CapacityCalibrationError(authorization["reason"])
                # A Mininet/P4 restart has no runtime QoS object yet.  In that
                # state the path is already unconstrained, so measuring it is
                # both safe and necessary before formal recovery can recreate
                # the declared scheduler.  An installed scope requires the
                # temporary window above to avoid self-capping the probe.
                installed = await probe.get("/policy/qos", {"scope_key": traffic_scope_key}) or {}
                if installed.get("status") != "not_found":
                    entered_result = await probe.post("/policy/qos-calibration-window", {"scope_key": traffic_scope_key, "operation": "ENTER"}) or {}
                    window_evidence["egress"] = entered_result
                    if entered_result.get("status") != "ok":
                        raise CapacityCalibrationError("CALIBRATION_WINDOW_ENTER_FAILED")
                    entered_egress = True
                ingress_result: dict[str, Any] = {"status": "ok", "write_executed": False, "window": "ABSENT"}
                ingress_runtime: dict[str, Any] = {"status": "NOT_FOUND"}
                if ingress:
                    ingress_runtime = await probe.get("/policy/ingress-admission", {
                        "admission_plan_id": ingress["admission_plan_id"],
                    }) or {}
                if ingress and ingress_runtime.get("status") != "NOT_FOUND":
                    ingress_result = await probe.post("/policy/ingress-admission-calibration-window", {
                        "admission_plan_id": ingress["admission_plan_id"],
                        "traffic_scope_key": traffic_scope_key,
                        "operation": "ENTER", "ceiling_mbps": offered,
                    }) or {}
                window_evidence["ingress"] = ingress_result
                fence.record_execution(authorization, write_executed=bool(
                    entered_result.get("write_executed") if installed.get("status") != "not_found" else False
                ) or bool(ingress_result.get("write_executed")))
                if ingress_result.get("status") != "ok":
                    raise CapacityCalibrationError("CALIBRATION_INGRESS_WINDOW_ENTER_FAILED")
                entered_ingress = ingress_runtime.get("status") != "NOT_FOUND"
                for sample in range(samples):
                    before = await sample_evidence()
                    value = await probe.get("/probe/iperf", {
                        "src": profile.src_host, "dst": profile.dst_host,
                        "protocol": profile.protocol.value, "port": profile.dst_port,
                        "bandwidth_mbps": offered, "duration": duration_seconds,
                        # Capacity evidence must exercise the exact active
                        # classifier tuple.  Omitting a profile's DSCP silently
                        # sends traffic through the default TC class instead.
                        "dscp": profile.dscp,
                    }) or {}
                    after = await sample_evidence()
                    mbps = value.get("received_mbps") or value.get("throughput_mbps")
                    observed.append({
                        "sample": sample + 1, "status": value.get("status"), "received_mbps": mbps,
                        "probe": value, "evidence_before": before, "evidence_after": after,
                    })
                    if value.get("status") == "ok" and mbps is not None and float(mbps) > 0:
                        delivered.append(float(mbps))
        finally:
            # Release is deliberately in the outer finally: a failed restore
            # must surface as an error, but must never leave its resource
            # frozen until the long lease TTL expires.
            try:
                if entered_ingress:
                    with write_context(
                        owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                        fencing_token=int(lease["fencing_token"]), resource_scope=lease["resource_scope"],
                        trace_id=f"CAL-RESTORE-{traffic_scope_key}",
                    ):
                        authorization = fence.authorize_network_configuration_write(
                            owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                            fencing_token=int(lease["fencing_token"]), resource_scope=lease["resource_scope"],
                            operation="CAPACITY_CALIBRATION_WINDOW_EXIT", trace_id=f"CAL-EXIT-{traffic_scope_key}",
                        )
                        restore_ingress = await probe.post("/policy/ingress-admission-calibration-window", {
                            "admission_plan_id": ingress["admission_plan_id"],
                            "traffic_scope_key": traffic_scope_key, "operation": "EXIT",
                        }) or {}
                        window_evidence["ingress_restore"] = restore_ingress
                        if restore_ingress.get("status") != "ok":
                            raise CapacityCalibrationError("CALIBRATION_INGRESS_WINDOW_RESTORE_FAILED")
                        fence.record_execution(authorization, write_executed=bool(restore_ingress.get("write_executed")))
                if entered_egress:
                    with write_context(
                        owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                        fencing_token=int(lease["fencing_token"]), resource_scope=lease["resource_scope"],
                        trace_id=f"CAL-RESTORE-EGRESS-{traffic_scope_key}",
                    ):
                        authorization = fence.authorize_network_configuration_write(
                            owner_type=lease["owner_type"], owner_id=lease["owner_id"],
                            fencing_token=int(lease["fencing_token"]), resource_scope=lease["resource_scope"],
                            operation="CAPACITY_CALIBRATION_EGRESS_WINDOW_EXIT", trace_id=f"CAL-EXIT-EGRESS-{traffic_scope_key}",
                        )
                        restore_egress = await probe.post("/policy/qos-calibration-window", {"scope_key": traffic_scope_key, "operation": "EXIT"}) or {}
                        window_evidence["egress_restore"] = restore_egress
                        fence.record_execution(authorization, write_executed=bool(restore_egress.get("write_executed")))
                        if restore_egress.get("status") != "ok":
                            raise CapacityCalibrationError("CALIBRATION_WINDOW_RESTORE_FAILED")
            finally:
                fence.release(lease["lease_id"])
        def record_rejection(reason: str, estimate: dict[str, Any] | None = None) -> None:
            """Persist real failed measurements; never turn them into capacity evidence."""
            self.store.add_audit_event(
                event_type="STABLE_CAPACITY_CALIBRATION_REJECTED",
                resource_type="stable_capacity",
                resource_id=capacity_record_id,
                correlation={"traffic_scope_key": traffic_scope_key, "ingress_generation": (ingress or {}).get("generation_id")},
                expected={"minimum_schedulable_mbps": protected, "sample_count": samples},
                observed={
                    "reason": reason, "offered_mbps": offered, "samples": observed,
                    "delivered_mbps": delivered, "estimate": estimate,
                    "window": window_evidence,
                },
                action={"operation": "LIVE_RECEIVER_IPERF_CALIBRATION", "network_configuration_write": False},
                result={"status": "REJECTED"},
            )

        if len(delivered) != samples:
            record_rejection("CALIBRATION_SAMPLE_INCOMPLETE")
            raise CapacityCalibrationError("CALIBRATION_SAMPLE_INCOMPLETE")
        estimate = conservative_capacity_estimate(delivered)
        safety = float(os.getenv("IDN_CAPACITY_CALIBRATION_SAFETY_MARGIN_MBPS", "5"))
        schedulable = max(0.0, estimate["lower_bound_mbps"] - safety)
        if math.floor(schedulable) < protected:
            record_rejection("CALIBRATION_DOES_NOT_SUPPORT_CURRENT_INGRESS_CONTRACT", {
                **estimate, "safety_margin_mbps": safety, "schedulable_capacity_mbps": schedulable,
            })
            raise CapacityCalibrationError("CALIBRATION_DOES_NOT_SUPPORT_CURRENT_INGRESS_CONTRACT")
        existing = next((item for item in StableCapacityRepository(self.store).list(binding.resource_key)
                         if item.capacity_record_id == capacity_record_id), None)
        now = _now()
        record = StableDataplaneCapacityRecord(
            capacity_record_id=capacity_record_id, topology_id=registry.topology_name,
            environment_epoch="CAL-" + hashlib.sha256((registry.topology_name + now.isoformat()).encode()).hexdigest()[:16],
            controller_epoch=str((existing.controller_epoch if existing else None) or "ONOS-live"),
            path_signature=str((existing.path_signature if existing else None) or binding.resource_key),
            bottleneck_egress=binding.resource_key, packet_profile=f"{profile.protocol.value.lower()}-{profile.packet_size_bytes}-live-probe",
            protocol=profile.protocol.value, packet_size_bytes=profile.packet_size_bytes,
            observation_mode="LIVE_RECEIVER_IPERF_CALIBRATION", physical_capacity_mbps=float(resource.capacity_mbps),
            stable_capacity_mbps=float(estimate["lower_bound_mbps"]), capacity_floor_mbps=float(estimate["lower_bound_mbps"]),
            p05_capacity_mbps=float(estimate["p05_capacity_mbps"]), sample_count=int(estimate["sample_count"]),
            confidence=str(estimate["confidence"]), measured_at=now.isoformat(),
            valid_until=(now + timedelta(seconds=max(60, int(os.getenv("IDN_CAPACITY_CALIBRATION_TTL_SECONDS", "3600"))))).isoformat(),
            source_experiment="formal-live-mininet-receiver-iperf", status="ACTIVE", safety_margin_mbps=safety,
        )
        StableCapacityRepository(self.store).put(record)
        result = {"status": "CALIBRATED", "traffic_scope_key": traffic_scope_key,
                  "capacity_record": record.model_dump(mode="json"), "schedulable_capacity_mbps": schedulable,
                  "samples": observed, "protected_schedulable_contract_mbps": protected,
                  "qos_window_restored": bool(restore_egress) if entered_egress else True,
                  "ingress_window_restored": bool(restore_ingress) if entered_ingress else True,
                  "current_committed_contract_mbps": current_contract,
                  "required_schedulable_mbps": protected,
                  "authority_source": authority_source}
        self.store.add_audit_event(event_type="STABLE_CAPACITY_CALIBRATED_FROM_LIVE_PROBE", resource_type="stable_capacity", resource_id=record.capacity_record_id,
            correlation={"traffic_scope_key": traffic_scope_key, "ingress_generation": (ingress or {}).get("generation_id")},
            expected={"minimum_schedulable_mbps": protected, "sample_count": samples}, observed=result,
            action={"operation": "LIVE_RECEIVER_IPERF_CALIBRATION", "network_configuration_write": False}, result={"status": "CALIBRATED"})
        return result

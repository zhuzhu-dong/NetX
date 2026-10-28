"""Central ServiceProfile registry and selector/policy consistency checks."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from ipaddress import ip_interface, ip_network
from pathlib import Path
from typing import Any

from models.service_profile import ServiceProfile, TrafficScenario


class ServiceProfileError(ValueError):
    code = "SERVICE_PROFILE_INVALID"


class ValidationProfileMismatch(ServiceProfileError):
    code = "VALIDATION_PROFILE_MISMATCH"


def _config_path() -> Path:
    explicit = os.getenv("SERVICE_PROFILES_FILE")
    if explicit:
        return Path(explicit)
    return Path(__file__).resolve().parents[1] / "config" / "service_profiles.json"


def _normalize_prefix(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(ip_network(str(value), strict=False))


def _normalize_selector_prefix(value: Any) -> str | None:
    """Preserve a concrete host as /32 while retaining explicit subnet intent."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if "/" not in text:
            return f"{ip_interface(text).ip}/32"
        return str(ip_network(text, strict=False))
    except ValueError:
        return None


@lru_cache(maxsize=4)
def _load_registry(path: str, mtime_ns: int) -> dict[str, Any]:
    del mtime_ns
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    profiles = [ServiceProfile(**item) for item in raw.get("profiles") or []]
    scenarios = [TrafficScenario(**item) for item in raw.get("traffic_scenarios") or []]
    by_id = {item.service_id: item for item in profiles}
    if len(by_id) != len(profiles):
        raise ServiceProfileError("duplicate service_id in service profile registry")
    scenario_by_id = {item.scenario_id: item for item in scenarios}
    if len(scenario_by_id) != len(scenarios):
        raise ServiceProfileError("duplicate scenario_id in service profile registry")
    for profile in profiles:
        for dependency in profile.background_profile_ids:
            if dependency not in by_id:
                raise ServiceProfileError(
                    f"{profile.service_id} references missing background profile {dependency}"
                )
    for scenario in scenarios:
        if scenario.target_service_id not in by_id:
            raise ServiceProfileError(
                f"{scenario.scenario_id} references missing target {scenario.target_service_id}"
            )
        for flow in scenario.background_flows:
            if flow.service_profile_id not in by_id:
                raise ServiceProfileError(
                    f"{scenario.scenario_id} references missing background {flow.service_profile_id}"
                )
    return {
        "schema_version": str(raw.get("schema_version") or "1.0"),
        "application_defaults": dict(raw.get("application_defaults") or {}),
        "profiles": profiles,
        "by_id": by_id,
        "scenarios": scenarios,
        "scenario_by_id": scenario_by_id,
        "config_path": path,
    }


def application_default(name: str) -> dict[str, Any] | None:
    value = service_registry()["application_defaults"].get(str(name).lower())
    return dict(value) if isinstance(value, dict) else None


def service_registry() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        raise ServiceProfileError(f"service profile config is missing: {path}")
    return _load_registry(str(path), path.stat().st_mtime_ns)


def list_service_profiles(*, enabled_only: bool = True) -> list[ServiceProfile]:
    profiles = list(service_registry()["profiles"])
    return [item for item in profiles if item.enabled] if enabled_only else profiles


def get_service_profile(service_id: str) -> ServiceProfile:
    profile = service_registry()["by_id"].get(str(service_id))
    if profile is None:
        raise KeyError(f"unknown service_id: {service_id}")
    if not profile.enabled:
        raise ServiceProfileError(f"service profile is disabled: {service_id}")
    return profile


def list_traffic_scenarios() -> list[TrafficScenario]:
    return list(service_registry()["scenarios"])


def get_traffic_scenario(scenario_id: str) -> TrafficScenario:
    scenario = service_registry()["scenario_by_id"].get(str(scenario_id))
    if scenario is None:
        raise KeyError(f"unknown scenario_id: {scenario_id}")
    return scenario


def infer_service_id(
    text: str,
    *,
    source_ip: str | None = None,
    destination_ip: str | None = None,
    protocol: str | None = None,
    destination_port: int | None = None,
) -> str | None:
    """Backward-compatible convenience wrapper over ServiceSemanticResolver."""
    from agents.semantic_resolver import EndpointResolver, ServiceSemanticResolver
    from models.semantic_resolution import ResolutionStatus

    # ``ANY_REGISTERED_HOST`` is deliberately identity based.  This legacy
    # helper receives selector prefixes rather than host names, so normalize
    # registered addresses through the same endpoint registry used by the
    # conversational path.  Unknown addresses remain unresolved.
    endpoints = EndpointResolver()
    source = endpoints.resolve(source_ip)
    destination = endpoints.resolve(destination_ip)
    resolution = ServiceSemanticResolver().resolve(
        text,
        source_endpoint=source.endpoint_id,
        destination_endpoint=destination.endpoint_id,
        source_ip=_normalize_selector_prefix(source_ip),
        destination_ip=_normalize_selector_prefix(destination_ip),
        protocol=protocol,
        destination_port=destination_port,
    )
    return (
        resolution.selected_service_id
        if resolution.status == ResolutionStatus.RESOLVED
        else None
    )


def resolve_service_semantics(text: str, **kwargs: Any):
    """Return the full typed resolution, including candidates and provenance."""
    from agents.semantic_resolver import ServiceSemanticResolver

    return ServiceSemanticResolver().resolve(text, **kwargs)


def profile_matches_policy(profile: ServiceProfile, policy: Any) -> tuple[bool, list[str]]:
    network_policy = getattr(policy, "network_policy", policy)
    match = getattr(network_policy, "match", None)
    action = getattr(network_policy, "action", None)
    if isinstance(network_policy, dict):
        match = network_policy.get("match") or {}
        action = network_policy.get("action") or {}
    match_data = match.model_dump(mode="json") if hasattr(match, "model_dump") else dict(match or {})
    action_data = action.model_dump(mode="json") if hasattr(action, "model_dump") else dict(action or {})
    qos = action_data.get("qos_params") or {}
    actual_protocol = str(getattr(match_data.get("protocol"), "value", match_data.get("protocol") or "ANY")).upper()
    # Policies carry selector prefixes rather than conversational host names.
    # Resolve those prefixes through the topology registry before evaluating
    # ANY_REGISTERED_HOST.  An arbitrary raw address is not a registered host.
    from agents.semantic_resolver import EndpointResolver

    endpoint_resolver = EndpointResolver()
    source_resolution = endpoint_resolver.resolve(match_data.get("source_ip"))
    destination_resolution = endpoint_resolver.resolve(match_data.get("destination_ip"))
    checks = {
        "source_ip": profile.allows_source(
            endpoint_id=source_resolution.endpoint_id,
            selector_prefix=_normalize_selector_prefix(match_data.get("source_ip")),
        ),
        "destination_ip": profile.allows_destination(
            endpoint_id=destination_resolution.endpoint_id,
            selector_prefix=_normalize_selector_prefix(match_data.get("destination_ip")),
        ),
        "protocol": actual_protocol == profile.protocol.value,
        "source_port": match_data.get("source_port") == profile.src_port,
        "destination_port": match_data.get("destination_port") == profile.dst_port,
        # An omitted DSCP means "use this named service's declared default".
        # In particular, DSCP 0 is a real profile value, not an absent value;
        # rejecting an otherwise exact background-video selector here stranded
        # the workflow before capacity admission.
        "dscp": (qos.get("dscp_value") if qos else None) in (None, profile.dscp),
        "vlan_id": match_data.get("vlan_id") == profile.vlan_id,
    }
    mismatches = [name for name, passed in checks.items() if not passed]
    return not mismatches, mismatches


def resolve_service_profile_for_policy(policy: Any) -> ServiceProfile:
    explicit = getattr(policy, "service_id", None)
    if explicit is None and isinstance(policy, dict):
        explicit = policy.get("service_id")
    if explicit:
        profile = get_service_profile(str(explicit))
        matched, mismatches = profile_matches_policy(profile, policy)
        if not matched:
            raise ValidationProfileMismatch(
                f"service_id={profile.service_id} differs from policy selector: {', '.join(mismatches)}"
            )
        return profile

    matches: list[ServiceProfile] = []
    for profile in list_service_profiles():
        if profile.protocol.value == "ICMP":
            continue
        matched, _ = profile_matches_policy(profile, policy)
        if matched:
            matches.append(profile)
    if len(matches) != 1:
        raise ValidationProfileMismatch(
            "policy selector must resolve to exactly one ServiceProfile; "
            f"matched={[item.service_id for item in matches]}"
        )
    return matches[0]


def attach_service_profile(policy: Any, service_id: str | None = None) -> Any:
    profile = get_service_profile(service_id) if service_id else resolve_service_profile_for_policy(policy)
    matched, mismatches = profile_matches_policy(profile, policy)
    if not matched:
        raise ValidationProfileMismatch(
            f"cannot attach {profile.service_id}; selector mismatch: {', '.join(mismatches)}"
        )
    snapshot = profile.model_dump(mode="json")
    if hasattr(policy, "model_copy"):
        attached = policy.model_copy(deep=True)
        qos = attached.network_policy.action.qos_params
        if qos is not None and qos.dscp_value is None:
            qos.dscp_value = profile.dscp
        return attached.model_copy(update={
            "service_id": profile.service_id,
            "service_profile_snapshot": snapshot,
            "validation_profile_id": profile.service_id,
        })
    attached = dict(policy)
    network_policy = dict(attached.get("network_policy") or {})
    action = dict(network_policy.get("action") or {})
    qos = dict(action.get("qos_params") or {})
    if qos.get("dscp_value") is None:
        qos["dscp_value"] = profile.dscp
    action["qos_params"] = qos
    network_policy["action"] = action
    attached["network_policy"] = network_policy
    attached["service_id"] = profile.service_id
    attached["service_profile_snapshot"] = snapshot
    attached["validation_profile_id"] = profile.service_id
    return attached

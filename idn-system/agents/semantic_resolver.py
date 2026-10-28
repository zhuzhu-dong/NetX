"""Registry-driven endpoint and business-service semantic resolution."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from models.semantic_resolution import (
    ClarificationOption,
    EndpointPairResolution,
    EndpointResolution,
    FieldProvenance,
    ResolutionStatus,
    ServiceCandidate,
    ServiceResolution,
)
from models.service_profile import ServiceProfile


def _host_selector(value: str | None) -> str | None:
    if not value:
        return None
    try:
        address = ipaddress.ip_interface(str(value)).ip
    except ValueError:
        return None
    return f"{address}/32"


def _semantic_token_matches(token: str, text: str) -> bool:
    if not token:
        return False
    if re.fullmatch(r"[a-z0-9 _-]+", token):
        # ``\w`` treats CJK characters as word characters.  That made a
        # registered hostname immediately following a Chinese role marker
        # (for example ``源是h_ai``) fail its token-boundary check, while a
        # shorter alias could subsequently win.  Endpoint hostnames and
        # English aliases need boundaries only against ASCII identifier
        # characters; adjacent natural-language text is a valid boundary.
        return bool(
            re.search(rf"(?<![a-z0-9_-]){re.escape(token)}(?![a-z0-9_-])", text)
        )
    return token in text


class EndpointResolver:
    """Resolve concrete host references without widening them to interface CIDRs."""

    def __init__(self, profiles: Iterable[ServiceProfile] | None = None):
        if profiles is None:
            from api.service_profiles import list_service_profiles

            profiles = list_service_profiles()
        self._profiles = list(profiles)
        self._endpoints = self._load_endpoints()

    def _load_endpoints(self) -> dict[str, dict[str, Any]]:
        endpoints: dict[str, dict[str, Any]] = {}
        for profile in self._profiles:
            for hostname, value in (
                (profile.src_host, profile.src_ip),
                (profile.dst_host, profile.dst_ip),
            ):
                current = endpoints.setdefault(hostname.casefold(), {"hostname": hostname})
                current.setdefault("host_ip", str(ipaddress.ip_interface(value).ip))
                current.setdefault("selector_prefix", _host_selector(value))

        # IP and /32 references are first-class endpoint references too.  A
        # user who supplies a registered address must resolve to the same
        # durable endpoint identity as a user who supplies its host name.
        # This is deliberately a registry alias, never a closest-prefix
        # heuristic: unknown A/B placeholders and unregistered IPs remain
        # unresolved and therefore cannot acquire a traffic scope.
        for endpoint in list(endpoints.values()):
            for reference in (endpoint.get("host_ip"), endpoint.get("selector_prefix")):
                if reference:
                    endpoints.setdefault(str(reference).casefold(), dict(endpoint))

        topology_name = os.getenv("TOPO_NAME", "research_dc")
        explicit_topology = os.getenv("IDN_TOPOLOGY_FILE")
        roots = [Path(__file__).resolve().parents[2], Path("/workspace")]
        topo_path = (
            Path(explicit_topology)
            if explicit_topology
            else next(
                (
                    root / "mininet" / "topologies" / f"{topology_name}.yaml"
                    for root in roots
                    if (root / "mininet" / "topologies" / f"{topology_name}.yaml").exists()
                ),
                roots[0] / "mininet" / "topologies" / f"{topology_name}.yaml",
            )
        )
        try:
            import yaml

            topo = yaml.safe_load(topo_path.read_text(encoding="utf-8")) or {}
            subnets = topo.get("subnets") or {}
            for host in topo.get("hosts") or []:
                hostname = str(host.get("name") or "")
                if not hostname:
                    continue
                current = endpoints.setdefault(hostname.casefold(), {"hostname": hostname})
                interface = ipaddress.ip_interface(str(host.get("ip")))
                current.update(
                    host_ip=str(interface.ip),
                    selector_prefix=f"{interface.ip}/32",
                    interface_network=str(
                        (subnets.get(host.get("subnet")) or {}).get("network")
                        or interface.network
                    ),
                    mac=host.get("mac"),
                )
        except (OSError, ValueError, TypeError, ImportError):
            pass

        attachment_path = Path(__file__).resolve().parents[1] / "config" / "topology_paths.json"
        try:
            payload = json.loads(attachment_path.read_text(encoding="utf-8"))
            for hostname, attachment in (payload.get("host_attachment_map") or {}).items():
                current = endpoints.setdefault(hostname.casefold(), {"hostname": hostname})
                current["attached_device"] = attachment.get("device_id")
                current["attached_port"] = attachment.get("port")
        except (OSError, ValueError, TypeError):
            pass

        # Site names are business-facing references, but enforcement requires a
        # concrete endpoint selector.  Keep that mapping explicit in configuration
        # so neither the LLM nor a CIDR heuristic can invent a host.  Aliases are
        # materialized only when their configured endpoint exists in the registry.
        aliases_path = Path(__file__).resolve().parents[1] / "config" / "site_aliases.json"
        try:
            payload = json.loads(aliases_path.read_text(encoding="utf-8"))
            for site in payload.get("known_sites") or []:
                endpoint_id = str(site.get("endpoint_id") or "").strip().casefold()
                endpoint = endpoints.get(endpoint_id)
                if endpoint is None:
                    continue
                references = [site.get("name"), *(site.get("aliases") or [])]
                for reference in references:
                    key = str(reference or "").strip().casefold()
                    if key:
                        endpoints.setdefault(key, dict(endpoint))
        except (OSError, ValueError, TypeError):
            pass
        return endpoints

    def resolve(self, reference: str | None) -> EndpointResolution:
        key = str(reference or "").strip().casefold()
        data = self._endpoints.get(key)
        if data is None:
            return EndpointResolution(
                status=ResolutionStatus.NOT_FOUND,
                matched_reference=reference,
            )
        return EndpointResolution(
            status=ResolutionStatus.RESOLVED,
            endpoint_id=data["hostname"],
            matched_reference=reference,
            confidence=1.0,
            provenance={
                "endpoint_id": FieldProvenance.USER_EXPLICIT.value,
                "selector_prefix": FieldProvenance.ENDPOINT_REGISTRY.value,
            },
            **data,
        )

    def resolve_text(self, text: str) -> EndpointPairResolution:
        lowered = str(text or "").casefold()
        def _positions(key: str) -> list[int]:
            # Registered IPs must be whole address tokens: 192.168.10.10
            # is not an alias for 192.168.10.100.  Natural-language aliases
            # retain their existing semantic token matching behaviour.
            if re.fullmatch(r"[0-9a-f:.]+(?:/\d{1,3})?", key):
                return [m.start() for m in re.finditer(
                    rf"(?<![0-9a-f:.]){re.escape(key)}(?![0-9a-f:.])", lowered
                )]
            return [lowered.find(key)] if _semantic_token_matches(key, lowered) else []
        matches = [
            (position, -len(key), key)
            for key in self._endpoints
            for position in _positions(key)
        ]
        # Keep an explicit but unregistered address in the ordered endpoint
        # pair.  Previously it was omitted because it has no registry key;
        # the next registered address then shifted into the source position
        # (``192.168.99.99 to 10.0.0.10`` became source=h_dc).  An unknown
        # address is meaningful user input: it must remain NOT_FOUND so the
        # semantic gate can ask for the invalid endpoint and prevent policy
        # creation, rather than silently reassigning endpoint roles.
        for match in re.finditer(
            r"(?<![0-9a-f:.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,3})?(?![0-9a-f:.])",
            lowered,
        ):
            reference = match.group(0)
            if reference not in self._endpoints:
                matches.append((match.start(), -len(reference), reference))
        matches.sort()
        # A configured alias can be a substring of a concrete hostname (for
        # example ``research`` in ``h_research``).  Collapse those references by
        # endpoint identity, preferring the longest match at the same position,
        # before assigning source/destination by textual order.
        references: list[str] = []
        endpoint_ids: set[str] = set()
        for _, _, key in matches:
            endpoint = self._endpoints.get(key)
            endpoint_id = str(
                endpoint.get("hostname") if endpoint is not None else f"unregistered:{key}"
            ).casefold()
            if endpoint_id in endpoint_ids:
                continue
            endpoint_ids.add(endpoint_id)
            references.append(key)
        source_ref = references[0] if references else None
        destination_ref = references[1] if len(references) > 1 else None
        source = self.resolve(source_ref)
        destination = self.resolve(destination_ref)
        unresolved: list[str] = []
        if source.status != ResolutionStatus.RESOLVED:
            unresolved.append("source")
        if destination.status != ResolutionStatus.RESOLVED:
            unresolved.append("destination")
        return EndpointPairResolution(
            source=source,
            destination=destination,
            unresolved_fields=unresolved,
        )


class ServiceSemanticResolver:
    """Resolve ServiceProfiles using semantics plus hard applicability constraints."""

    def __init__(self, profiles: Iterable[ServiceProfile] | None = None):
        if profiles is None:
            from api.service_profiles import list_service_profiles

            profiles = list_service_profiles()
        self.profiles = [profile for profile in profiles if profile.enabled]

    def resolve(
        self,
        text: str,
        *,
        source_endpoint: str | None = None,
        destination_endpoint: str | None = None,
        source_ip: str | None = None,
        destination_ip: str | None = None,
        protocol: str | None = None,
        destination_port: int | None = None,
        tenant_id: str | None = None,
    ) -> ServiceResolution:
        lowered = str(text or "").casefold()
        protocol_name = str(protocol or "").upper()
        protocol_filter = protocol_name not in {"", "ANY"}
        port_filter = destination_port is not None
        explicit_protocol = bool(re.search(r"(?<![a-z])(tcp|udp|icmp)(?![a-z])", lowered))
        explicit_port = bool(
            re.search(
                r"(?:port|端口|dst_port|destination_port)\s*[:=]?\s*\d{1,5}|(?:tcp|udp)\s*[/ :]\s*\d{1,5}",
                lowered,
            )
        )
        explicit_service = next(
            (
                profile.service_id
                for profile in self.profiles
                if re.search(rf"(?<![\w-]){re.escape(profile.service_id.casefold())}(?![\w-])", lowered)
            ),
            None,
        )
        explicit_fields: dict[str, Any] = {}
        if explicit_service:
            explicit_fields["service_id"] = explicit_service
        if explicit_protocol:
            explicit_fields["protocol"] = protocol_name
        if explicit_port:
            explicit_fields["destination_port"] = destination_port
        if source_endpoint:
            explicit_fields["source_endpoint"] = source_endpoint
        if destination_endpoint:
            explicit_fields["destination_endpoint"] = destination_endpoint

        candidates: list[ServiceCandidate] = []
        matched_tokens: set[str] = set()
        for profile in self.profiles:
            semantic_values = [
                profile.display_name,
                profile.service_name,
                *profile.aliases,
                *profile.effective_semantic_tags,
            ]
            profile_semantics = []
            for value in semantic_values:
                token = str(value or "").casefold().strip()
                if _semantic_token_matches(token, lowered):
                    matched_tokens.add(token)
                    profile_semantics.append(token)
            conflicts: list[str] = []
            if explicit_service and profile.service_id != explicit_service:
                conflicts.append("service_id")
            if (source_endpoint or source_ip) and not profile.allows_source(
                endpoint_id=source_endpoint,
                selector_prefix=source_ip,
            ):
                conflicts.append("source_endpoint")
            if (destination_endpoint or destination_ip) and not profile.allows_destination(
                endpoint_id=destination_endpoint,
                selector_prefix=destination_ip,
            ):
                conflicts.append("destination_endpoint")
            if source_ip and not profile.allows_source(
                endpoint_id=source_endpoint,
                selector_prefix=source_ip,
            ):
                conflicts.append("source_ip")
            if destination_ip and not profile.allows_destination(
                endpoint_id=destination_endpoint,
                selector_prefix=destination_ip,
            ):
                conflicts.append("destination_ip")
            if protocol_filter and profile.protocol.value != protocol_name:
                conflicts.append("protocol")
            if port_filter and profile.dst_port != destination_port:
                conflicts.append("destination_port")
            if tenant_id and profile.tenant_id != tenant_id:
                conflicts.append("tenant_id")
            if conflicts:
                continue

            reasons: list[str] = []
            score = 0
            for token in profile_semantics:
                reasons.append(f"semantic:{token}")
                score = max(score, 70 + min(20, len(token)))
            if explicit_service:
                score = 100
                reasons.append("explicit service_id")
            if explicit_protocol and explicit_port:
                # Transport alone must not erase a more specific business
                # semantic match.  Several legitimate ServiceProfiles can
                # share UDP/5004, so making this a floor of 95 used to turn
                # an explicit name such as "research video" into a tie with
                # a generic ``video``/``udp`` tag after endpoint
                # applicability was generalized.  Keep the semantic score
                # and add transport evidence instead; transport-only input
                # still ties and therefore asks the user to clarify.
                score += 15
                reasons.append("explicit protocol+destination_port")
            elif explicit_protocol or explicit_port:
                score += 5
                reasons.append("explicit transport selector")
            if source_endpoint and destination_endpoint:
                score += 10
                reasons.append("endpoint pair compatible")
            elif source_endpoint or destination_endpoint:
                score += 5
                reasons.append("endpoint compatible")

            # Endpoints alone classify applicability, not the user's business intent.
            if not reasons or (
                not explicit_service
                and not explicit_protocol
                and not explicit_port
                and not any(reason.startswith("semantic:") for reason in reasons)
            ):
                continue
            candidates.append(
                ServiceCandidate(
                    service_id=profile.service_id,
                    display_name=profile.display_name or profile.service_name,
                    confidence=min(1.0, score / 100),
                    match_reason=reasons,
                    conflicting_fields=[],
                )
            )

        candidates.sort(key=lambda item: (-item.confidence, item.service_id))
        # A uniquely identified service is still useful semantic information
        # before the user has supplied a traffic endpoint pair.  It may supply
        # transport characteristics (protocol/port/DSCP), but it must *not*
        # supply its configured src/dst values as an intent selector.  Concrete
        # endpoint applicability is enforced later, once both user-provided
        # endpoints are registry-resolved.  This keeps "research_video" a
        # service type rather than an implicit h_research -> h_dc request.
        if not candidates:
            semantic_profiles: list[tuple[ServiceProfile, list[str], int]] = []
            for profile in self.profiles:
                values = [
                    profile.display_name,
                    profile.service_name,
                    *profile.aliases,
                    *profile.effective_semantic_tags,
                ]
                matched_values = [
                    str(value or "").casefold().strip()
                    for value in values
                    if str(value or "").strip()
                    and _semantic_token_matches(
                        str(value or "").casefold().strip(), lowered
                    )
                ]
                if matched_values:
                    conflicts: list[str] = []
                    if (source_endpoint or source_ip) and not profile.allows_source(
                        endpoint_id=source_endpoint,
                        selector_prefix=source_ip,
                    ):
                        conflicts.append("source_endpoint")
                    if (destination_endpoint or destination_ip) and not profile.allows_destination(
                        endpoint_id=destination_endpoint,
                        selector_prefix=destination_ip,
                    ):
                        conflicts.append("destination_endpoint")
                    if source_ip and not profile.allows_source(
                        endpoint_id=source_endpoint,
                        selector_prefix=source_ip,
                    ):
                        conflicts.append("source_ip")
                    if destination_ip and not profile.allows_destination(
                        endpoint_id=destination_endpoint,
                        selector_prefix=destination_ip,
                    ):
                        conflicts.append("destination_ip")
                    if protocol_filter and profile.protocol.value != protocol_name:
                        conflicts.append("protocol")
                    if port_filter and profile.dst_port != destination_port:
                        conflicts.append("destination_port")
                    semantic_profiles.append((profile, conflicts, max(map(len, matched_values))))
            common_protocols = {item.protocol.value for item, _, _ in semantic_profiles}
            inferred = (
                {"protocol": next(iter(common_protocols))}
                if len(common_protocols) == 1
                else {}
            )
            mismatch_candidates = [
                ServiceCandidate(
                    service_id=item.service_id,
                    display_name=item.display_name or item.service_name,
                    confidence=min(0.99, 0.7 + min(20, score) / 100),
                    match_reason=["semantic match"],
                    conflicting_fields=conflicts,
                )
                for item, conflicts, score in semantic_profiles
            ]
            if mismatch_candidates:
                best_score = max(item.confidence for item in mismatch_candidates)
                mismatch_candidates = [
                    item for item in mismatch_candidates if item.confidence == best_score
                ]
            return ServiceResolution(
                status=ResolutionStatus.NOT_FOUND,
                candidate_services=mismatch_candidates,
                confidence=(mismatch_candidates[0].confidence if len(mismatch_candidates) == 1 else 0.0),
                explicit_fields=explicit_fields,
                inferred_fields=inferred,
                field_provenance=(
                    {"protocol": FieldProvenance.SEMANTIC_RESOLUTION.value}
                    if inferred
                    else {}
                ),
                matched_semantics=sorted(matched_tokens),
                ambiguity_reason=(
                    "Service semantics are known but the explicit selector is not applicable."
                    if mismatch_candidates and any(item.conflicting_fields for item in mismatch_candidates)
                    else "No ServiceProfile satisfies the semantic and endpoint constraints."
                ),
            )
        best_confidence = candidates[0].confidence
        best = [item for item in candidates if item.confidence == best_confidence]
        if len(best) != 1:
            return ServiceResolution(
                status=ResolutionStatus.AMBIGUOUS,
                candidate_services=candidates,
                confidence=best_confidence,
                matched_semantics=sorted(matched_tokens),
                explicit_fields=explicit_fields,
                ambiguity_reason="Multiple ServiceProfiles remain equally applicable.",
                clarification_options=[
                    ClarificationOption(
                        value=item.service_id,
                        label=item.display_name,
                        description="；".join(item.match_reason),
                    )
                    for item in best
                ],
            )

        selected = best[0]
        profile = next(item for item in self.profiles if item.service_id == selected.service_id)
        provenance = {
            "service_id": FieldProvenance.SEMANTIC_RESOLUTION.value,
            "protocol": (
                FieldProvenance.USER_EXPLICIT.value
                if explicit_protocol
                else FieldProvenance.SERVICE_PROFILE.value
            ),
            "destination_port": (
                FieldProvenance.USER_EXPLICIT.value
                if explicit_port
                else FieldProvenance.SERVICE_PROFILE.value
            ),
            "dscp": FieldProvenance.SERVICE_PROFILE.value,
        }
        return ServiceResolution(
            status=ResolutionStatus.RESOLVED,
            selected_service_id=profile.service_id,
            candidate_services=candidates,
            confidence=selected.confidence,
            matched_semantics=sorted(matched_tokens),
            explicit_fields=explicit_fields,
            inferred_fields={
                "protocol": profile.protocol.value,
                "destination_port": profile.dst_port,
                "dscp": profile.dscp,
            },
            field_provenance=provenance,
        )

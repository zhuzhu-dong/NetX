"""Translate the legacy parser output into the versioned IntentSpec IR."""

from __future__ import annotations

import re
from typing import Any

from models.intent_domain import (
    ComparisonOperator,
    ConstraintType,
    IntentConstraint,
    IntentLifecycle,
    IntentObjective,
    IntentSpec,
    IntentUnderstandingResult,
    LLMRuntimeMode,
    ObjectiveType,
    PreferenceDimension,
    PreferenceRelation,
    PreferenceStrength,
    ValueOrigin,
)
from models.intent_plan import IntentPlan


class IntentUnderstandingAgent:
    agent_id = "intent_understanding"

    def understand(
        self,
        *,
        intent_id: str,
        text: str,
        legacy_plan: IntentPlan | dict[str, Any] | None,
        priority: str = "medium",
        context: dict[str, Any] | None = None,
        revision: int = 1,
    ) -> IntentSpec:
        context = dict(context or {})
        plan = (
            legacy_plan
            if isinstance(legacy_plan, IntentPlan)
            else IntentPlan.model_validate(legacy_plan) if legacy_plan else None
        )
        items = list(plan.sub_intents) if plan else []
        selector: dict[str, Any] = {}
        services: list[str] = []
        objectives: list[IntentObjective] = []
        formal_constraints: list[IntentConstraint] = []
        seen: dict[tuple[str, str, str | None], int] = {}

        def add(
            objective_type: ObjectiveType,
            value: Any,
            operator: ComparisonOperator,
            unit: str | None = None,
            *,
            explicit: bool = True,
            source_text: str | None = None,
            origin: ValueOrigin | None = None,
        ) -> None:
            key = (objective_type.value, str(value), unit)
            if key in seen:
                index = seen[key]
                existing = objectives[index]
                if explicit and not existing.explicit:
                    objectives[index] = existing.model_copy(
                        update={
                            "origin": origin or ValueOrigin.USER_EXPLICIT,
                            "explicit": True,
                            "source_text": source_text,
                        }
                    )
                return
            seen[key] = len(objectives)
            objectives.append(
                IntentObjective(
                    type=objective_type,
                    operator=operator,
                    value=value,
                    unit=unit,
                    origin=origin
                    or (
                        ValueOrigin.USER_EXPLICIT if explicit else ValueOrigin.INFERRED
                    ),
                    explicit=explicit,
                    source_text=source_text,
                )
            )

        for item in items:
            for key, value in (item.match or {}).items():
                if value not in (None, "", "any", "ANY"):
                    selector.setdefault(key, value)
            if item.service_id and item.service_id not in services:
                services.append(item.service_id)
            action = dict(item.action or {})
            action_type = str(
                action.get("action_type") or item.intent_type or ""
            ).upper()
            qos = dict(action.get("qos_params") or {})
            constraints = dict(item.constraints or {})
            bandwidth = self._number(
                qos.get("bandwidth")
                or constraints.get("bandwidth_mbps")
                or constraints.get("bandwidth")
            )
            bandwidth_explicit = bool(
                re.search(
                    r"(?:\d+(?:\.\d+)?\s*(?:mbps|m(?:bit)?/?s|兆)|(?:bandwidth|带宽|限速)[^0-9]{0,16}\d+)",
                    text,
                    flags=re.IGNORECASE,
                )
            )
            if action_type in {"RATE_LIMIT", "POLICE"} and bandwidth is not None:
                add(
                    ObjectiveType.MAX_BANDWIDTH,
                    bandwidth,
                    ComparisonOperator.LTE,
                    "Mbps",
                    explicit=bandwidth_explicit,
                    source_text=text if bandwidth_explicit else None,
                    origin=(
                        ValueOrigin.USER_EXPLICIT
                        if bandwidth_explicit
                        else ValueOrigin.SERVICE_PROFILE
                    ),
                )
            elif bandwidth is not None:
                add(
                    ObjectiveType.MIN_BANDWIDTH,
                    bandwidth,
                    ComparisonOperator.GTE,
                    "Mbps",
                    explicit=bandwidth_explicit,
                    source_text=text if bandwidth_explicit else None,
                    origin=(
                        ValueOrigin.USER_EXPLICIT
                        if bandwidth_explicit
                        else ValueOrigin.SERVICE_PROFILE
                    ),
                )

            metric_map = {
                "max_latency_ms": (ObjectiveType.MAX_LATENCY, "ms"),
                "latency_ms": (ObjectiveType.MAX_LATENCY, "ms"),
                "max_packet_loss_pct": (ObjectiveType.MAX_PACKET_LOSS, "%"),
                "packet_loss_pct": (ObjectiveType.MAX_PACKET_LOSS, "%"),
                "max_jitter_ms": (ObjectiveType.MAX_JITTER, "ms"),
                "jitter_ms": (ObjectiveType.MAX_JITTER, "ms"),
                "availability_pct": (ObjectiveType.AVAILABILITY, "%"),
            }
            for key, (kind, unit) in metric_map.items():
                value = self._number(constraints.get(key))
                if value is None:
                    continue
                operator = (
                    ComparisonOperator.GTE
                    if kind == ObjectiveType.AVAILABILITY
                    else ComparisonOperator.LTE
                )
                explicit = self._metric_is_explicit(text, kind)
                add(
                    kind,
                    value,
                    operator,
                    unit,
                    explicit=explicit,
                    source_text=text if explicit else None,
                    origin=(
                        ValueOrigin.USER_EXPLICIT
                        if explicit
                        else ValueOrigin.SERVICE_PROFILE
                    ),
                )
            constraint_map = {
                "avoid_links": ConstraintType.AVOID_LINK,
                "avoid_link": ConstraintType.AVOID_LINK,
                "avoid_devices": ConstraintType.AVOID_DEVICE,
                "avoid_device": ConstraintType.AVOID_DEVICE,
                "required_devices": ConstraintType.REQUIRE_DEVICE,
                "require_device": ConstraintType.REQUIRE_DEVICE,
                "prefer_path": ConstraintType.PREFER_PATH,
                "tenant_isolation": ConstraintType.TENANT_ISOLATION,
                "time_window": ConstraintType.TIME_WINDOW,
            }
            for key, constraint_kind in constraint_map.items():
                value = constraints.get(key)
                if value not in (None, "", [], {}):
                    formal_constraints.append(
                        IntentConstraint(
                            type=constraint_kind,
                            value=value,
                            hard=constraint_kind != ConstraintType.PREFER_PATH,
                            origin=ValueOrigin.USER_EXPLICIT,
                            source_text=text,
                        )
                    )
            if action_type == "DROP":
                add(
                    ObjectiveType.ISOLATION,
                    True,
                    ComparisonOperator.EQ,
                    explicit=True,
                    source_text=text,
                )
            elif action_type in {"SET_PRIORITY", "FORWARD", "RATE_LIMIT", "POLICE"}:
                add(
                    ObjectiveType.CONNECTIVITY,
                    True,
                    ComparisonOperator.EQ,
                    explicit=False,
                )
            # SubIntent.priority is an internal policy-ordering field and DSCP
            # may come from a ServiceProfile. Neither is a user objective
            # unless the natural-language request explicitly asks for it.
            priority_requested = bool(
                re.search(
                    r"(?:priority|优先级|优先保障|高优先)", text, flags=re.IGNORECASE
                )
            )
            priority_value = (
                item.priority
                if item.priority is not None
                else qos.get("priority", qos.get("dscp"))
            )
            if priority_requested and priority_value is not None:
                add(
                    ObjectiveType.PRIORITY,
                    priority_value,
                    ComparisonOperator.EQ,
                    explicit=True,
                    source_text=text,
                )

        # Preserve explicit metrics even when an older parser omitted them.
        self._extract_explicit_metrics(text, add)
        preferences = self._extract_preference_relations_v3(text)
        self_preference_error = self._has_self_preference(text)
        preference_only = bool(preferences)
        if preference_only:
            # ``priority`` in this syntax is a relation between named services,
            # not a request for a traffic priority/DSCP or connectivity SLA.
            # Retain any separately stated measurable objective, but discard
            # legacy parser artefacts created from SET_PRIORITY.
            objectives = [
                objective
                for objective in objectives
                if objective.explicit
                and objective.type not in {
                    ObjectiveType.CONNECTIVITY,
                    ObjectiveType.PRIORITY,
                }
            ]
            for relation in preferences:
                for service_id in (relation.higher_scope, relation.lower_scope):
                    if service_id not in services:
                        services.append(service_id)
        critic = dict(plan.critic_report or {}) if plan else {}
        llm_status = str(
            critic.get("llm_status") or ("SUCCESS" if plan else "NOT_ATTEMPTED")
        )
        runtime_mode_text = str(
            critic.get("llm_runtime_mode")
            or (
                "DETERMINISTIC_FALLBACK"
                if llm_status != "SUCCESS" and plan
                else "NOT_USED"
            )
        )
        runtime_mode = (
            LLMRuntimeMode(runtime_mode_text)
            if runtime_mode_text in {item.value for item in LLMRuntimeMode}
            else LLMRuntimeMode.NOT_USED
        )
        questions = list(plan.clarification_questions) if plan else []
        missing = list(critic.get("missing_slots") or [])
        for slot in missing:
            questions.append(f"Please provide the missing value for {slot}.")
        questions = list(
            dict.fromkeys(str(item) for item in questions if str(item).strip())
        )
        clarification = bool(questions or not objectives)
        source_ip = selector.get("source_ip") or selector.get("src_ip")
        destination_ip = selector.get("destination_ip") or selector.get("dst_ip")
        provenance = dict(plan.field_provenance or {}) if plan else {}
        semantic_resolution = dict(plan.semantic_resolution or {}) if plan else {}
        endpoint_resolution: dict[str, Any] = {}
        for item in items:
            constraints = dict(item.constraints or {})
            if constraints.get("endpoint_resolution"):
                endpoint_resolution = dict(constraints["endpoint_resolution"])
            provenance.update(dict(constraints.get("field_provenance") or {}))
        source_detail = dict(endpoint_resolution.get("source") or {})
        destination_detail = dict(endpoint_resolution.get("destination") or {})
        unresolved_fields: list[str] = []
        path_scope_required = False
        if preference_only:
            try:
                from api.service_profiles import get_service_profile

                path_relations = [
                    relation for relation in preferences
                    if relation.dimension == PreferenceDimension.PATH_PREFERENCE
                ]
                if path_relations:
                    path_scope_required = True
                    # A path preference has one declared service scope and
                    # role labels (for example PRIMARY/BACKUP).  Resolve the
                    # service through its registry profile.  Its configured
                    # endpoints are applicability metadata, not defaults for
                    # this user's traffic scope; PathConfig is consulted only
                    # after the user has supplied both concrete endpoints.
                    role_scopes = {
                        scope for relation in path_relations
                        for scope in (relation.higher_scope, relation.lower_scope)
                    }
                    profile_ids = [item for item in services if item not in role_scopes]
                    if not profile_ids:
                        from api.service_profiles import list_service_profiles

                        normalized_text = text.casefold()
                        matches: list[tuple[int, str]] = []
                        for candidate in list_service_profiles():
                            names = [
                                candidate.service_id,
                                candidate.service_name,
                                candidate.display_name,
                                *candidate.aliases,
                            ]
                            for name in names:
                                token = str(name or "").casefold().strip()
                                if token and token in normalized_text:
                                    matches.append((len(token), candidate.service_id))
                        if matches:
                            profile_ids = [max(matches)[1]]
                    profiles = [get_service_profile(item) for item in profile_ids]
                    if len(profiles) != 1:
                        raise ValueError("path preference requires exactly one registered service")
                else:
                    # A service preference has two canonical service ends.
                    for relation in preferences:
                        get_service_profile(relation.higher_scope)
                        get_service_profile(relation.lower_scope)
            except (ImportError, KeyError, ValueError):
                unresolved_fields.append("service")
        if not preference_only or path_scope_required:
            if not source_ip:
                unresolved_fields.append("source")
            if not destination_ip:
                unresolved_fields.append("destination")
            # Do not treat a proposed IP selector as endpoint resolution.
            # In particular, literal placeholders such as A/B must never be
            # allowed to borrow an unrelated active QoS binding simply because
            # a later parser/LLM proposed a familiar selector.
            qos_objective = any(
                objective.type in {ObjectiveType.MIN_BANDWIDTH, ObjectiveType.MAX_BANDWIDTH}
                for objective in objectives
            )
            if qos_objective:
                if str(source_detail.get("status") or "").upper() != "RESOLVED":
                    unresolved_fields.append("source")
                if str(destination_detail.get("status") or "").upper() != "RESOLVED":
                    unresolved_fields.append("destination")
            if not objectives and not preference_only:
                unresolved_fields.append("objective")
            if semantic_resolution.get("status") in {"AMBIGUOUS", "NOT_FOUND"} and semantic_resolution.get("matched_semantics"):
                unresolved_fields.append("service")
        clarification = bool(questions or unresolved_fields or (not objectives and not preference_only))
        if self_preference_error:
            clarification = False
            questions = []
            unresolved_fields = []
        return IntentSpec(
            intent_id=intent_id,
            original_text=text,
            normalized_text=" ".join(text.split()),
            intent_type=("PREFERENCE" if preference_only and not objectives else self._intent_type(objectives)),
            subject=dict(context.get("subject") or {}),
            source=(
                {
                    "endpoint_id": source_detail.get("endpoint_id"),
                    "hostname": source_detail.get("hostname"),
                    "host_ip": source_detail.get("host_ip"),
                    "interface_network": source_detail.get("interface_network"),
                    "selector_prefix": source_ip,
                    "ip": source_ip,
                }
                if source_ip
                else dict(context.get("source") or {})
            ),
            destination=(
                {
                    "endpoint_id": destination_detail.get("endpoint_id"),
                    "hostname": destination_detail.get("hostname"),
                    "host_ip": destination_detail.get("host_ip"),
                    "interface_network": destination_detail.get("interface_network"),
                    "selector_prefix": destination_ip,
                    "ip": destination_ip,
                }
                if destination_ip
                else dict(context.get("destination") or {})
            ),
            service=(
                {"service_ids": services}
                if services
                else dict(context.get("service") or {})
            ),
            selector=selector,
            objectives=objectives,
            constraints=formal_constraints,
            preferences=preferences,
            execution_capability=(
                "CONTROL_PLANE_SCHEDULING_PREFERENCE" if preference_only else None
            ),
            southbound_mutation_required=False if preference_only else None,
            preference_validation_error=("INVALID_SELF_PREFERENCE" if self_preference_error else None),
            priority=priority,
            scope=str(context.get("scope") or "network"),
            tenant_id=str(context.get("tenant_id") or "default"),
            validity=dict(context.get("validity") or {}),
            confidence=float(plan.confidence_score if plan else 0),
            assumptions=[
                "Connectivity is required by an enforcement action."
                for _ in [0]
                if any(
                    o.type == ObjectiveType.CONNECTIVITY and not o.explicit
                    for o in objectives
                )
            ],
            clarification_required=clarification,
            clarification_questions=questions,
            clarification_details=[
                item.model_dump(mode="json") for item in (plan.clarification_details if plan else [])
            ],
            explicit_fields=dict(semantic_resolution.get("explicit_fields") or {}),
            inferred_fields=dict(semantic_resolution.get("inferred_fields") or {}),
            unresolved_fields=list(dict.fromkeys(unresolved_fields)),
            semantic_scope_complete=(
                not preference_only
                and not unresolved_fields
                and bool(source_ip)
                and bool(destination_ip)
                and str(source_detail.get("status") or "").upper() == "RESOLVED"
                and str(destination_detail.get("status") or "").upper() == "RESOLVED"
            ),
            field_provenance=provenance,
            service_resolution=semantic_resolution,
            endpoint_resolution=endpoint_resolution,
            evidence=[
                {
                    "type": "DETERMINISTIC_RESOLUTION",
                    "service": semantic_resolution,
                    "endpoints": endpoint_resolution,
                }
            ],
            llm_status=llm_status,
            llm_runtime_mode=runtime_mode,
            llm_call_id=critic.get("llm_call_id"),
            llm_provider=critic.get("llm_provider"),
            llm_model=critic.get("llm_model"),
            llm_structured_output_valid=critic.get("structured_output_valid"),
            llm_validator_result=str(critic.get("validator_result") or "NOT_RUN"),
            fallback_used=bool(critic.get("fallback_used")),
            lifecycle=(
                IntentLifecycle.NEEDS_CLARIFICATION
                if clarification
                else IntentLifecycle.PARSED
            ),
            revision=revision,
        )

    def understand_result(self, **kwargs: Any) -> IntentUnderstandingResult:
        spec = self.understand(**kwargs)
        return IntentUnderstandingResult(
            intent_spec=spec,
            status=(
                "NEEDS_CLARIFICATION"
                if spec.clarification_required
                else "RESOLVED"
            ),
            confidence=spec.confidence,
            explicit_fields=spec.explicit_fields,
            inferred_fields=spec.inferred_fields,
            unresolved_fields=spec.unresolved_fields,
            service_resolution=spec.service_resolution,
            endpoint_resolution=spec.endpoint_resolution,
            clarification_questions=spec.clarification_details,
            llm_status=spec.llm_status,
            fallback_used=spec.fallback_used,
            llm_runtime_mode=spec.llm_runtime_mode,
            llm_call_id=spec.llm_call_id,
            llm_provider=spec.llm_provider,
            llm_model=spec.llm_model,
            llm_structured_output_valid=spec.llm_structured_output_valid,
            llm_validator_result=spec.llm_validator_result,
            evidence=spec.evidence,
        )

    def _extract_explicit_metrics(self, text: str, add) -> None:
        # ``带宽`` alone is a useful minimum-bandwidth cue for a guarantee,
        # but it is not one when the same phrase explicitly states a cap.
        # Without this guard, "带宽上限设置为20Mbps" produced mutually
        # contradictory MIN_BANDWIDTH and MAX_BANDWIDTH objectives even though
        # the IntentPlan correctly selected RATE_LIMIT.
        rate_limit_language = bool(re.search(
            r"(?:限速|不超过|最多|上限|封顶|cap\s*(?:at|to)|ceiling)",
            text,
            flags=re.IGNORECASE,
        ))
        patterns = [
            (
                r"(?:保|保障|保证|至少|不低于|带宽)[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(?:mbps|m(?![a-z])|兆)",
                ObjectiveType.MIN_BANDWIDTH,
                "Mbps",
                ComparisonOperator.GTE,
            ),
            (
                r"(?:限速|不超过|最多)[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(?:mbps|m(?![a-z])|兆)",
                ObjectiveType.MAX_BANDWIDTH,
                "Mbps",
                ComparisonOperator.LTE,
            ),
            (
                r"(?:latency|时延|延迟)[^0-9]{0,12}(?:不超过|低于|小于|<=?)?\s*(\d+(?:\.\d+)?)\s*ms",
                ObjectiveType.MAX_LATENCY,
                "ms",
                ComparisonOperator.LTE,
            ),
            (
                r"(?:jitter|抖动)[^0-9]{0,12}(?:不超过|低于|小于|<=?)?\s*(\d+(?:\.\d+)?)\s*ms",
                ObjectiveType.MAX_JITTER,
                "ms",
                ComparisonOperator.LTE,
            ),
            (
                r"(?:packet\s*loss|丢包率|丢包)[^0-9]{0,12}(?:不超过|低于|小于|<=?)?\s*(\d+(?:\.\d+)?)\s*%",
                ObjectiveType.MAX_PACKET_LOSS,
                "%",
                ComparisonOperator.LTE,
            ),
            (
                r"(?:availability|可用性)[^0-9]{0,12}(?:不少于|不低于|>=?)?\s*(\d+(?:\.\d+)?)\s*%",
                ObjectiveType.AVAILABILITY,
                "%",
                ComparisonOperator.GTE,
            ),
        ]
        for pattern, kind, unit, operator in patterns:
            if kind == ObjectiveType.MIN_BANDWIDTH and rate_limit_language:
                continue
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                add(
                    kind,
                    float(match.group(1)),
                    operator,
                    unit,
                    explicit=True,
                    source_text=match.group(0),
                )

    @staticmethod
    def _extract_relative_priority(text: str) -> list[dict[str, Any]]:
        patterns = [
            r"(?P<higher>[A-Za-z0-9_\-\u4e00-\u9fff]{1,32})\s*(?:的)?优先级\s*(?:高于|大于|超过)\s*(?P<lower>[A-Za-z0-9_\-\u4e00-\u9fff]{1,32})",
            r"prioriti[sz]e\s+(?P<higher>[A-Za-z0-9_\-]{1,32})\s+over\s+(?P<lower>[A-Za-z0-9_\-]{1,32})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return [
                    {
                        "type": "RELATIVE_PRIORITY",
                        "subject": match.group("higher"),
                        "operator": "GREATER_THAN",
                        "relative_to": match.group("lower"),
                        "origin": ValueOrigin.USER_EXPLICIT.value,
                        "explicit": True,
                        "source_text": match.group(0),
                    }
                ]
        return []

    @staticmethod
    def _extract_preference_relations(text: str) -> list[PreferenceRelation]:
        """Extract a typed relation without assigning an SLA or queue class.

        Service names are resolved through the configured service registry. A
        term that is not a service remains a normalized path role, so the
        parser has no topology or business-name special case.
        """
        patterns = (
            (PreferenceDimension.SERVICE_PRIORITY, [
                r"(?P<higher>[A-Za-z0-9_\\-\\u4e00-\\u9fff ]{1,48}?)(?:流量|业务)?\\s*(?:优先于|优先级高于|优先保障)\\s*(?P<lower>[A-Za-z0-9_\\-\\u4e00-\\u9fff ]{1,48}?)(?:流量|业务)?(?:[。；，,]|$)",
                r"(?:prefer|prioriti[sz]e)\\s+(?P<higher>[A-Za-z0-9_\\- ]{1,48}?)\\s+(?:over|above)\\s+(?P<lower>[A-Za-z0-9_\\- ]{1,48}?)(?:[.;,]|$)",
            ]),
            (PreferenceDimension.PATH_PREFERENCE, [
                r"(?:优先走|优先使用)\\s*(?P<higher>[A-Za-z0-9_\\-\\u4e00-\\u9fff ]{1,48}?)\\s*(?:而不是|优先于)\\s*(?P<lower>[A-Za-z0-9_\\-\\u4e00-\\u9fff ]{1,48}?)(?:[。；，,]|$)",
                r"(?:prefer|use)\\s+(?P<higher>[A-Za-z0-9_\\- ]{1,48}?)\\s+(?:path\\s+)?(?:instead of|over)\\s+(?P<lower>[A-Za-z0-9_\\- ]{1,48}?)(?:[.;,]|$)",
            ]),
        )
        for dimension, expressions in patterns:
            for expression in expressions:
                match = re.search(expression, text, flags=re.IGNORECASE)
                if not match:
                    continue
                higher = IntentUnderstandingAgent._preference_scope(match.group("higher"))
                lower = IntentUnderstandingAgent._preference_scope(match.group("lower"))
                if higher and lower and higher.casefold() != lower.casefold():
                    return [PreferenceRelation(
                        higher_scope=higher,
                        lower_scope=lower,
                        dimension=dimension,
                        strength=PreferenceStrength.SOFT,
                        origin=ValueOrigin.USER_EXPLICIT,
                        source_text=match.group(0).strip(),
                    )]
        return []

    @staticmethod
    def _extract_preference_relations_v2(text: str) -> list[PreferenceRelation]:
        """UTF-8-safe generic relation grammar used by the production path."""
        patterns = (
            (PreferenceDimension.SERVICE_PRIORITY, (
                r"(?P<higher>.+?)(?:优先级高于|优先于|优先保障)(?P<lower>.+?)(?:[。；，,]|$)",
                r"(?:prefer|prioriti[sz]e) (?P<higher>.+?) (?:over|above) (?P<lower>.+?)(?:[.;,]|$)",
            )),
            (PreferenceDimension.PATH_PREFERENCE, (
                r"(?:优先走|优先使用)(?P<higher>.+?)(?:而不是|优先于)(?P<lower>.+?)(?:[。；，,]|$)",
                r"(?:prefer|use) (?P<higher>.+?) (?:path )?(?:instead of|over) (?P<lower>.+?)(?:[.;,]|$)",
            )),
        )
        for dimension, expressions in patterns:
            for expression in expressions:
                match = re.search(expression, text, flags=re.IGNORECASE)
                if not match:
                    continue
                higher = IntentUnderstandingAgent._preference_scope(match.group("higher"))
                lower = IntentUnderstandingAgent._preference_scope(match.group("lower"))
                if higher and lower and higher.casefold() != lower.casefold():
                    return [PreferenceRelation(
                        higher_scope=higher,
                        lower_scope=lower,
                        dimension=dimension,
                        strength=PreferenceStrength.SOFT,
                        origin=ValueOrigin.USER_EXPLICIT,
                        source_text=match.group(0).strip(),
                    )]
        return []

    @staticmethod
    def _extract_preference_relations_v3(text: str) -> list[PreferenceRelation]:
        """Extract every English relation in a compound semantic request.

        Real LLM output is still authoritative for the underlying IntentPlan;
        this deterministic guard preserves each explicit relative relation so a
        compound request cannot silently lose one edge before DAG validation.
        """
        collected: list[PreferenceRelation] = []
        grammars = (
            (PreferenceDimension.SERVICE_PRIORITY, (
                r"(?:prefer|prioriti[sz]e) (?P<higher>.+?) (?:service )?(?:over|above) (?P<lower>.+?)(?: when |[.,;]|$)",
                r"(?P<higher>.+?) should (?:also )?have higher priority than (?P<lower>.+?)(?:[.,;]|$)",
            )),
            (PreferenceDimension.PATH_PREFERENCE, (
                r"(?:prefer|use) (?:the )?(?P<higher>[^ ]+) path .*?(?:instead of|and use (?:the )?)(?P<lower>[^ ]+) path",
            )),
        )
        seen: set[tuple[str, str, PreferenceDimension]] = set()
        for dimension, expressions in grammars:
            for expression in expressions:
                for match in re.finditer(expression, text, flags=re.IGNORECASE):
                    higher = IntentUnderstandingAgent._preference_scope(match.group("higher"))
                    lower = IntentUnderstandingAgent._preference_scope(match.group("lower"))
                    key = (higher.casefold(), lower.casefold(), dimension)
                    if not higher or not lower or higher.casefold() == lower.casefold() or key in seen:
                        continue
                    seen.add(key)
                    collected.append(PreferenceRelation(
                        higher_scope=higher,
                        lower_scope=lower,
                        dimension=dimension,
                        strength=PreferenceStrength.SOFT,
                        origin=ValueOrigin.USER_EXPLICIT,
                        source_text=match.group(0).strip(),
                    ))
        # Retain the existing Chinese grammar as a compatibility fallback.
        return collected or IntentUnderstandingAgent._extract_preference_relations_v2(text)

    @staticmethod
    def _preference_scope(value: str) -> str:
        cleaned = " ".join(str(value).strip().split())
        cleaned = re.sub(r"^(?:while|and)\s+", "", cleaned, flags=re.IGNORECASE)
        # Natural language frequently wraps registry aliases in an article and
        # the generic noun "service".  Normalize those grammatical wrappers
        # before consulting the registry; no business service IDs are encoded
        # here.
        cleaned = re.sub(r"^(?:the|a|an)\s+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+(?:service|traffic|flow)\s*$", "", cleaned, flags=re.IGNORECASE)
        if not cleaned:
            return ""
        try:
            from api.service_profiles import infer_service_id, list_service_profiles
            normalized = cleaned.casefold()
            for profile in list_service_profiles():
                names = [
                    profile.service_id,
                    profile.display_name,
                    profile.service_name,
                    *profile.aliases,
                ]
                if any(normalized == str(name or "").casefold().strip() for name in names):
                    return profile.service_id
            resolved = infer_service_id(cleaned)
            if resolved:
                return resolved
        except (ImportError, KeyError, ValueError):
            pass
        return re.sub(r"[^\w-]+", "_", cleaned.casefold()).strip("_")

    @staticmethod
    def _has_self_preference(text: str) -> bool:
        expressions = (
            r"(?:prefer|prioriti[sz]e) (?P<higher>.+?) (?:service )?(?:over|above) (?P<lower>.+?)(?: when |[.,;]|$)",
            r"(?P<higher>.+?) should (?:also )?have higher priority than (?P<lower>.+?)(?:[.,;]|$)",
        )
        for expression in expressions:
            for match in re.finditer(expression, text, flags=re.IGNORECASE):
                higher = IntentUnderstandingAgent._preference_scope(match.group("higher"))
                lower = IntentUnderstandingAgent._preference_scope(match.group("lower"))
                if higher and lower and higher.casefold() == lower.casefold():
                    return True
        return False

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        match = re.search(r"-?\d+(?:\.\d+)?", str(value))
        return float(match.group()) if match else None

    @staticmethod
    def _metric_is_explicit(text: str, kind: ObjectiveType) -> bool:
        tokens = {
            ObjectiveType.MAX_LATENCY: r"latency|时延|延迟",
            ObjectiveType.MAX_PACKET_LOSS: r"packet\s*loss|丢包",
            ObjectiveType.MAX_JITTER: r"jitter|抖动",
            ObjectiveType.AVAILABILITY: r"availability|可用性",
        }
        token = tokens.get(kind)
        return bool(
            token
            and re.search(rf"(?:{token})[^0-9]{{0,16}}\d+", text, flags=re.IGNORECASE)
        )

    @staticmethod
    def _intent_type(objectives: list[IntentObjective]) -> str:
        types = {item.type for item in objectives}
        if ObjectiveType.ISOLATION in types:
            return "ISOLATION"
        if types & {
            ObjectiveType.MIN_BANDWIDTH,
            ObjectiveType.MAX_BANDWIDTH,
            ObjectiveType.MAX_LATENCY,
            ObjectiveType.MAX_PACKET_LOSS,
            ObjectiveType.MAX_JITTER,
            ObjectiveType.PRIORITY,
        }:
            return "QOS"
        return "CONNECTIVITY"

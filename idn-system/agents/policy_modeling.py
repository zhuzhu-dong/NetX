"""
agents/policy_modeling.py

PolicyModelingAgent converts the JSON intent_plan into the abstract YANGPolicy.
This keeps "what the user wants" separate from "how the network policy is
represented" before any P4Runtime table entries are generated.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from agents.base_agent import BaseAgent
from models.intent_plan import IntentPlan, IntentPlanEnvelope
from models.network import NetworkSnapshot
from models.yang_policy import (
    ActionRules,
    ActionType,
    MatchCriteria,
    NetworkPolicy,
    PolicyMetadata,
    QoSParams,
    QoSMode,
    YANGPolicy,
)


class PolicyModelingAgent(BaseAgent):
    """JSON intent_plan -> YANGPolicy."""

    agent_id = "policy_modeling"
    description = "Convert intent plans into abstract YANG policies"

    async def model(
        self,
        intent_plan: IntentPlan | IntentPlanEnvelope | dict[str, Any],
        network_snapshot: Optional[NetworkSnapshot] = None,
    ) -> YANGPolicy:
        policies = await self.model_all(intent_plan, network_snapshot=network_snapshot)
        return policies[0]

    async def model_all(
        self,
        intent_plan: IntentPlan | IntentPlanEnvelope | dict[str, Any],
        network_snapshot: Optional[NetworkSnapshot] = None,
    ) -> list[YANGPolicy]:
        plan = self._coerce_plan(intent_plan)
        if (
            not plan.plan_id
            or plan.artifact_type != "INTENT_PLAN"
            or plan.clarification_questions
            or plan.clarification_details
        ):
            raise ValueError(
                "INTENT_NOT_PLANNABLE: policy modeling requires a complete formal IntentPlan"
            )
        if not plan.sub_intents:
            raise ValueError("IntentPlan must contain at least one sub_intent")

        return [self._model_sub_intent(plan, item) for item in plan.sub_intents]

    def _model_sub_intent(self, plan: IntentPlan, item) -> YANGPolicy:
        action_data = dict(item.action or {})
        qos_data = action_data.get("qos_params") or {}
        action_type = ActionType(action_data.get("action_type") or item.intent_type or ActionType.FORWARD.value)

        qos_params = None
        if action_type in {ActionType.SET_PRIORITY, ActionType.RATE_LIMIT} and qos_data:
            qos_data = dict(qos_data)
            from api.service_profiles import get_service_profile
            from models.semantic_resolution import FieldProvenance

            field_provenance = dict(item.constraints.get("field_provenance") or {})
            dscp_origin = field_provenance.get("dscp_value") or field_provenance.get("dscp")
            profile = get_service_profile(item.service_id) if item.service_id else None
            # Service-profile traffic classes are canonical unless the raw
            # request explicitly supplied a DSCP constraint.  In particular,
            # a provider's convenience default (often 46) cannot turn into a
            # selector mismatch for an otherwise matching profile.
            if profile is not None and dscp_origin != FieldProvenance.USER_EXPLICIT.value:
                qos_data["dscp_value"] = profile.dscp
            elif qos_data.get("dscp_value") is None:
                # A DSCP value is a service-classification property, not a
                # generic QoS default.  Resolve it from the same central
                # ServiceProfile registry used during understanding.
                from agents.semantic_resolver import ServiceSemanticResolver
                from models.semantic_resolution import ResolutionStatus

                selector = dict(item.match or {})
                resolution = ServiceSemanticResolver().resolve(
                    plan.raw_intent,
                    source_ip=selector.get("source_ip"),
                    destination_ip=selector.get("destination_ip"),
                    protocol=str(
                        getattr(
                            selector.get("protocol"),
                            "value",
                            selector.get("protocol") or "",
                        )
                    ),
                    destination_port=selector.get("destination_port"),
                )
                if resolution.status == ResolutionStatus.RESOLVED:
                    qos_data["dscp_value"] = get_service_profile(
                        resolution.selected_service_id
                    ).dscp
            qos_data.setdefault(
                "qos_mode",
                QoSMode.RATE_LIMIT.value
                if action_type == ActionType.RATE_LIMIT
                else QoSMode.MIN_BANDWIDTH.value,
            )
            qos_params = QoSParams(**qos_data)

        forward_params = None
        if action_type == ActionType.MIRROR:
            fwd_data = action_data.get("forward_params") or {}
            if fwd_data:
                forward_params = ForwardParams(**fwd_data)

        policy = NetworkPolicy(
            policy_name=self._policy_name(item.constraints.get("policy_name") or item.objective or action_type.value),
            match=MatchCriteria(**(item.match or {})),
            action=ActionRules(
                action_type=action_type,
                qos_params=qos_params,
                forward_params=forward_params,
            ),
            metadata=PolicyMetadata(
                priority=item.priority or 1000,
                description=item.objective or plan.raw_intent,
                created_by="PolicyModelingAgent",
            ),
        )

        modeled = YANGPolicy(
            network_policy=policy,
            source_plan_id=plan.plan_id,
            source_plan_status="PLAN_READY",
            raw_intent=plan.raw_intent,
            confidence_score=plan.confidence_score,
            parse_warnings=plan.parse_warnings,
            service_id=item.service_id,
            service_profile_snapshot=item.service_profile_snapshot,
            validation_profile_id=item.validation_profile_id,
        )
        from api.service_profiles import attach_service_profile, resolve_service_profile_for_policy

        if item.service_id:
            return attach_service_profile(modeled, item.service_id)
        try:
            profile = resolve_service_profile_for_policy(modeled)
        except ValueError:
            return modeled
        return attach_service_profile(modeled, profile.service_id)

    def _coerce_plan(self, payload: IntentPlan | IntentPlanEnvelope | dict[str, Any]) -> IntentPlan:
        if isinstance(payload, IntentPlan):
            return payload
        if isinstance(payload, IntentPlanEnvelope):
            return payload.intent_plan
        if "intent_plan" in payload:
            return IntentPlan(**payload["intent_plan"])
        return IntentPlan(**payload)

    def _policy_name(self, text: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
        return value[:80] or "modeled-policy"

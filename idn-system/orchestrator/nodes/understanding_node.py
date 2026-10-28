"""Pre-sensing semantic validation gate for natural-language intents."""

from __future__ import annotations

import inspect

from models.agent_state import ExecutionStage, GlobalAgentState, StageStatus
from models.intent import IntentStatus

from .base_node import BaseNode


class UnderstandingNode(BaseNode):
    """Build and validate the candidate interpretation before Sense/Planning."""

    agent_id = "intent_understanding"
    stage = ExecutionStage.UNDERSTANDING

    async def run(self, state: GlobalAgentState) -> GlobalAgentState:
        start = self._start_log(state)
        state.current_status = IntentStatus.THINKING
        try:
            from agents.intent_planner import IntentPlannerAgent

            planner = IntentPlannerAgent(llm=self.llm)
            kwargs = {
                "user_input": state.user_intent.text,
                "network_snapshot": state.network_snapshot,
                "context": state.user_intent.context,
            }
            parameters = inspect.signature(planner.plan).parameters
            if "intent_id" in parameters:
                kwargs["intent_id"] = state.intent_id
            if "revision" in parameters:
                kwargs["revision"] = 1
            if "on_token" in parameters:
                kwargs["on_token"] = self._token_callback(state.intent_id)
            candidate = await planner.plan(**kwargs)
            # A relative service/path preference is a scheduler relation, not
            # a disguised QoS request.  The real LLM has already participated
            # in ``candidate``; now use the typed IR to stop the legacy graph
            # before its generic policy modeler can turn SET_PRIORITY parser
            # artefacts into bandwidth, DSCP, P4, TC, or ONOS work.
            from agents.intent_understanding import IntentUnderstandingAgent

            typed_spec = IntentUnderstandingAgent().understand(
                intent_id=state.intent_id,
                text=state.user_intent.text,
                legacy_plan=candidate,
                priority=state.user_intent.priority.value,
                context=state.user_intent.context,
            )
            if typed_spec.preference_validation_error or (
                typed_spec.preferences and not typed_spec.objectives
            ):
                state.intent_plan = candidate
                state.needs_clarification = False
                state.clarification_question = None
                state.clarification_questions = []
                state.semantic_only_completed = True
                state.execution_status = "NOT_RUN_CONTROL_PLANE_PREFERENCE"
                state.execution_readback_status = "PENDING_CONTROL_PLANE_READBACK"
                state.current_status = IntentStatus.SUCCESS
                return self._end_log(
                    state,
                    start,
                    StageStatus.SUCCESS,
                    "relative preference resolved or rejected; stop before generic policy modelling and southbound compilation",
                )
            missing = self._missing_slots(candidate)
            if candidate.clarification_questions or missing:
                details = [
                    item.model_copy(update={"intent_id": state.intent_id})
                    for item in candidate.clarification_details
                ]
                candidate = candidate.model_copy(
                    update={
                        "plan_id": None,
                        "artifact_type": "CANDIDATE_INTERPRETATION",
                        "clarification_details": details,
                    }
                )
                state.intent_plan = candidate
                state.yang_draft = None
                state.yang_policies = []
                state.flow_table_plan = None
                state.needs_clarification = True
                state.clarification_question = (
                    candidate.clarification_questions[0]
                    if candidate.clarification_questions
                    else "请补充完成该网络目标所需的业务信息。"
                )
                state.clarification_questions = [
                    item.model_dump(mode="json")
                    for item in details
                ]
                state.current_status = IntentStatus.PENDING_CLARIFICATION
                self._push_progress(
                    state,
                    "我需要确认一个业务信息；后续网络感知、规划和执行尚未开始。",
                    "clarification_required",
                )
                return self._end_log(
                    state,
                    start,
                    StageStatus.SUCCESS,
                    "语义理解需要澄清，流水线在网络感知与规划前暂停",
                )

            state.intent_plan = candidate
            state.needs_clarification = False
            state.clarification_question = None
            state.clarification_questions = []
            return self._end_log(
                state,
                start,
                StageStatus.SUCCESS,
                f"语义理解完成: service={candidate.sub_intents[0].service_id if candidate.sub_intents else None}, "
                f"confidence={candidate.confidence_score:.2f}",
            )
        except Exception as exc:
            state.error_message = f"意图语义理解失败: {exc}"
            return self._end_log(
                state,
                start,
                StageStatus.FAILED,
                "意图语义理解失败",
                error=str(exc),
            )

    def _token_callback(self, intent_id: str):
        sequence = 0

        def on_token(token: str) -> None:
            nonlocal sequence
            try:
                from api.events import get_event_manager

                get_event_manager().push(
                    intent_id,
                    "thinking_token",
                    {
                        "stage": "understanding",
                        "agent_id": self.agent_id,
                        "seq": sequence,
                        "token": token,
                    },
                )
                sequence += 1
            except Exception:
                pass

        return on_token

    @staticmethod
    def _missing_slots(candidate) -> list[str]:
        values: list[str] = []
        critic = getattr(candidate, "critic_report", {}) or {}
        values.extend(critic.get("missing_slots") or [])
        for item in getattr(candidate, "sub_intents", []) or []:
            slot = (getattr(item, "constraints", {}) or {}).get("slot_filling") or {}
            values.extend(slot.get("missing_slots") or [])
        return list(dict.fromkeys(str(value) for value in values if str(value).strip()))

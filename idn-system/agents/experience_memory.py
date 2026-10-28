"""Long-term case memory for intent execution and self-healing outcomes."""
from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from models.yang_policy import YANGPolicy


class ExperienceMemoryAgent:
    """Store and retrieve successful or failed repair cases."""

    agent_id = "experience_memory"
    description = (
        "Stores historical intent and self-healing cases, then retrieves similar "
        "cases by keyword, optional vector search, or hybrid reranking."
    )

    _VALID_RETRIEVAL_MODES = {"keyword", "vector", "hybrid"}

    def __init__(self, store: Any | None = None):
        self.store = store if store is not None else self._current_store()

    def available(self) -> bool:
        return self.store is not None

    def record_case(self, case: dict[str, Any]) -> dict[str, Any] | None:
        if not self.store:
            return None
        case_id = case.get("case_id") or f"CASE-{uuid.uuid4().hex[:12].upper()}"
        payload = {
            **case,
            "case_id": case_id,
            "created_at": case.get("created_at") or datetime.now(timezone.utc).isoformat(),
        }
        search_text = str(payload.get("search_text") or self._case_text(payload))
        payload["search_text"] = search_text
        payload["embedding_model"] = payload.get("embedding_model") or self._embedding_model()
        payload["embedding"] = payload.get("embedding") or self._embed_text(search_text)
        return self.store.upsert_experience_case(payload)

    def record_from_verification(
        self,
        *,
        intent_id: str,
        yang_policy: YANGPolicy,
        verification_report: dict[str, Any],
    ) -> dict[str, Any] | None:
        case = self.case_from_verification(
            intent_id=intent_id,
            yang_policy=yang_policy,
            verification_report=verification_report,
        )
        if not case:
            return None
        return self.record_case(case)

    def retrieve_similar(
        self,
        *,
        intent_type: str | None = None,
        symptoms: list[str] | None = None,
        evidence: list[str] | None = None,
        root_cause: str | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        if not self.store:
            return []
        query_text = self._query_text(
            intent_type=intent_type,
            symptoms=symptoms or [],
            evidence=evidence or [],
            root_cause=root_cause,
        )
        query_tokens = self._tokens([query_text])
        if not query_tokens:
            return []

        mode = self._retrieval_mode()
        query_embedding = self._embed_text(query_text) if mode in {"vector", "hybrid"} else []
        keyword_weight = self._float_env("IDN_EXPERIENCE_KEYWORD_WEIGHT", 0.55)
        vector_weight = self._float_env("IDN_EXPERIENCE_VECTOR_WEIGHT", 0.45)
        weight_sum = keyword_weight + vector_weight
        if weight_sum <= 0:
            keyword_weight, vector_weight = 0.55, 0.45
            weight_sum = 1.0

        scored = []
        for case in self.store.list_experience_cases(limit=200):
            case_text = str(case.get("search_text") or self._case_text(case))
            case_tokens = self._tokens([case_text])
            if not case_tokens:
                continue
            keyword_score = self._keyword_score(query_tokens, case_tokens)
            vector_score = 0.0
            if mode in {"vector", "hybrid"}:
                vector_score = self._cosine(
                    query_embedding,
                    self._case_embedding(case, case_text),
                )
            if mode == "vector":
                base_score = vector_score
            elif mode == "hybrid":
                base_score = (
                    keyword_score * keyword_weight + vector_score * vector_weight
                ) / weight_sum
            else:
                base_score = keyword_score

            rerank_score = self._rerank_score(
                base_score=base_score,
                case=case,
                intent_type=intent_type,
                root_cause=root_cause,
            )
            if rerank_score <= 0:
                continue
            retrieval = {
                "mode": mode,
                "keyword_score": round(keyword_score, 3),
                "vector_score": round(vector_score, 3),
                "base_score": round(base_score, 3),
                "rerank_score": round(rerank_score, 3),
                "embedding_model": case.get("embedding_model") or self._embedding_model(),
            }
            scored.append(
                {
                    **case,
                    "similarity": retrieval["rerank_score"],
                    "retrieval": retrieval,
                }
            )
        scored.sort(key=lambda item: (item["similarity"], bool(item.get("success"))), reverse=True)
        return scored[:limit]

    def summarize_cases(self, cases: list[dict[str, Any]]) -> dict[str, Any]:
        if not cases:
            return {
                "similar_cases": [],
                "success_rate": None,
                "recommended_actions": [],
                "lesson": None,
            }
        successes = [case for case in cases if case.get("success")]
        actions: list[str] = []
        for case in successes or cases:
            actions.extend(str(item) for item in case.get("repair_action") or [] if item)
        return {
            "similar_cases": [
                {
                    "case_id": case.get("case_id"),
                    "similarity": case.get("similarity"),
                    "root_cause": case.get("root_cause"),
                    "success": case.get("success"),
                    "repair_action": case.get("repair_action") or [],
                    "lesson": case.get("lesson"),
                    "retrieval": case.get("retrieval"),
                }
                for case in cases
            ],
            "success_rate": round(len(successes) / len(cases), 3),
            "recommended_actions": self._dedupe(actions)[:5],
            "lesson": (successes or cases)[0].get("lesson"),
        }

    def case_from_verification(
        self,
        *,
        intent_id: str,
        yang_policy: YANGPolicy,
        verification_report: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not verification_report:
            return None
        root_report = self._root_report(verification_report)
        auto_healing = verification_report.get("auto_healing") or {}
        comparison = verification_report.get("healing_comparison") or auto_healing.get("comparison") or {}
        before = (
            (verification_report.get("pre_heal_verification") or {}).get("kpi")
            or verification_report.get("before_kpi")
            or (verification_report.get("pre_execution_verification") or {}).get("kpi")
            or comparison.get("before_kpi")
            or verification_report.get("kpi")
            or {}
        )
        after = (
            verification_report.get("after_kpi")
            or comparison.get("after_kpi")
            or verification_report.get("kpi")
            or {}
        )
        evidence = self._evidence(root_report, verification_report)
        actions = [
            item.get("action")
            for item in auto_healing.get("actions") or []
            if item.get("action")
        ]
        recommended = root_report.get("recommended_action")
        if recommended and not actions:
            actions = [recommended]
        success = bool(verification_report.get("passed") or verification_report.get("intent_achieved"))
        root_cause = root_report.get("primary_cause") or verification_report.get("root_cause")
        intent_type = self._intent_type(yang_policy)
        if not evidence:
            evidence = [
                "verification: intent_achieved"
                if success
                else "verification: intent_not_achieved_without_root_cause_evidence"
            ]
        if not actions and success:
            actions = ["install_compiled_policy"]
        return {
            "case_id": f"CASE-{intent_id}-{uuid.uuid4().hex[:6].upper()}",
            "intent_id": intent_id,
            "intent_type": intent_type,
            "symptom": (
                root_cause
                or "; ".join(verification_report.get("violations") or [])
                or ("intent_achieved" if success else "intent_not_achieved")
            ),
            "evidence": evidence,
            "root_cause": root_cause,
            "repair_action": actions,
            "before_kpi": before,
            "after_kpi": after,
            "success": success,
            "lesson": self._lesson(root_cause, actions, success),
        }

    def _current_store(self) -> Any | None:
        try:
            from api.store import get_current_store

            return get_current_store()
        except Exception as exc:
            logger.error("[ExperienceMemory] store access failed: %s", exc)
            return None

    def _root_report(self, verification_report: dict[str, Any]) -> dict[str, Any]:
        direct = verification_report.get("root_cause_report")
        if isinstance(direct, dict) and direct:
            return direct
        pre = verification_report.get("pre_heal_verification") or {}
        nested = pre.get("root_cause_report")
        return nested if isinstance(nested, dict) else {}

    def _evidence(
        self,
        root_report: dict[str, Any],
        verification_report: dict[str, Any],
    ) -> list[str]:
        evidence = []
        for item in root_report.get("evidence") or []:
            if isinstance(item, dict):
                observation = item.get("observation")
                source = item.get("source")
                if observation:
                    evidence.append(f"{source}: {observation}" if source else str(observation))
            elif item:
                evidence.append(str(item))
        evidence.extend(str(item) for item in verification_report.get("violations") or [] if item)
        return self._dedupe(evidence)[:10]

    def _intent_type(self, yang_policy: YANGPolicy) -> str:
        policy = yang_policy.network_policy
        name = policy.policy_name or ""
        action = policy.action.action_type.value
        if "video" in name.lower():
            return "video_service_priority" if action == "SET_PRIORITY" else "video_service"
        return action

    def _lesson(self, root_cause: str | None, actions: list[str], success: bool) -> str:
        action_text = " + ".join(actions) if actions else "reobserve_and_request_human_review"
        if success and root_cause == "primary_path_congestion":
            return (
                "When primary path congestion is observed and a backup path is reachable, "
                f"prefer {action_text}."
            )
        if success:
            return f"{root_cause or 'similar_symptom'} was repaired by {action_text}."
        return (
            f"{root_cause or 'similar_symptom'} was not fully repaired by {action_text}; "
            "the next ReAct turn should verify more conservatively."
        )

    def _query_text(
        self,
        *,
        intent_type: str | None,
        symptoms: list[str],
        evidence: list[str],
        root_cause: str | None,
    ) -> str:
        return " ".join(
            str(item or "")
            for item in [intent_type, root_cause, *symptoms, *evidence]
            if item
        )

    def _case_text(self, case: dict[str, Any]) -> str:
        parts: list[str] = [
            str(case.get("intent_type") or ""),
            str(case.get("symptom") or ""),
            str(case.get("root_cause") or ""),
            str(case.get("lesson") or ""),
        ]
        parts.extend(str(item) for item in case.get("evidence") or [] if item)
        parts.extend(str(item) for item in case.get("repair_action") or [] if item)
        for section in ("before_kpi", "after_kpi"):
            kpi = case.get(section) or {}
            if isinstance(kpi, dict):
                parts.extend(f"{section}.{key}:{value}" for key, value in sorted(kpi.items()))
        return " ".join(part for part in parts if part)

    def _retrieval_mode(self) -> str:
        mode = os.getenv("IDN_EXPERIENCE_RETRIEVAL_MODE", "keyword").strip().lower()
        return mode if mode in self._VALID_RETRIEVAL_MODES else "keyword"

    def _embedding_model(self) -> str:
        return f"local_hash_embedding_v1:{self._vector_dim()}"

    def _vector_dim(self) -> int:
        return max(16, min(512, self._int_env("IDN_EXPERIENCE_VECTOR_DIM", 64)))

    def _case_embedding(self, case: dict[str, Any], case_text: str) -> list[float]:
        embedding = self._coerce_vector(case.get("embedding"))
        if len(embedding) == self._vector_dim():
            return embedding
        return self._embed_text(case_text)

    def _embed_text(self, text: str) -> list[float]:
        dim = self._vector_dim()
        vector = [0.0] * dim
        features = sorted(self._tokens([text]) | set(self._char_ngrams(text)))
        if not features:
            return vector
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8", errors="ignore")).digest()
            index = int.from_bytes(digest[:4], "big") % dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 1.0 + (digest[5] % 11) / 20.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0:
            return vector
        return [round(value / norm, 6) for value in vector]

    def _coerce_vector(self, value: Any) -> list[float]:
        if not isinstance(value, list):
            return []
        vector = []
        for item in value:
            try:
                vector.append(float(item))
            except (TypeError, ValueError):
                return []
        return vector

    def _cosine(self, left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm <= 0 or right_norm <= 0:
            return 0.0
        value = dot / (left_norm * right_norm)
        return max(0.0, min(1.0, value))

    def _tokens(self, values: list[Any]) -> set[str]:
        text = " ".join(str(value or "") for value in values)
        tokens = set(re.findall(r"[A-Za-z0-9_:\-.]+", text.lower()))
        compact = re.sub(r"\s+", "", text.lower())
        non_ascii = "".join(ch for ch in compact if ord(ch) > 127)
        for size in (2, 3):
            for index in range(max(0, len(non_ascii) - size + 1)):
                tokens.add(non_ascii[index : index + size])
        return tokens

    def _char_ngrams(self, text: str) -> list[str]:
        normalized = re.sub(r"\s+", "", str(text).lower())
        if not normalized:
            return []
        grams: list[str] = []
        for size in (2, 3, 4):
            for index in range(max(0, len(normalized) - size + 1)):
                grams.append(normalized[index : index + size])
                if len(grams) >= 256:
                    return grams
        return grams

    def _keyword_score(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        jaccard = self._jaccard(left, right)
        query_coverage = len(left & right) / len(left)
        return max(0.0, min(1.0, jaccard * 0.65 + query_coverage * 0.35))

    def _rerank_score(
        self,
        *,
        base_score: float,
        case: dict[str, Any],
        intent_type: str | None,
        root_cause: str | None,
    ) -> float:
        score = base_score
        if root_cause and case.get("root_cause") == root_cause:
            score += self._float_env("IDN_EXPERIENCE_RERANK_ROOT_CAUSE_BONUS", 0.18)
        if intent_type and case.get("intent_type") == intent_type:
            score += self._float_env("IDN_EXPERIENCE_RERANK_INTENT_BONUS", 0.08)
        if case.get("success"):
            score += self._float_env("IDN_EXPERIENCE_RERANK_SUCCESS_BONUS", 0.03)
        return max(0.0, min(1.0, score))

    def _jaccard(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        value = len(left & right) / len(left | right)
        return 0.0 if math.isnan(value) else value

    def _dedupe(self, values: list[str]) -> list[str]:
        result = []
        seen = set()
        for value in values:
            key = str(value)
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(key)
        return result

    def _int_env(self, name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    def _float_env(self, name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

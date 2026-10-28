"""Strict output contracts for the LLM decision layer."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class PlanningRankItem(BaseModel):
    candidate_id: str
    rank: int = Field(ge=1)
    reason: str = ""


class PlanningLLMSelection(BaseModel):
    selected_candidate_id: str
    ranking: list[PlanningRankItem] = Field(min_length=1)
    rationale: str
    tradeoffs: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def selected_must_be_ranked(self) -> "PlanningLLMSelection":
        if self.selected_candidate_id not in {
            item.candidate_id for item in self.ranking
        }:
            raise ValueError("selected candidate is missing from ranking")
        return self


class RootCauseRankItem(BaseModel):
    cause: str
    rank: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    reason: str
    evidence_fact_ids: list[str] = Field(default_factory=list)


class RootCauseLLMSelection(BaseModel):
    selected_cause: str
    ranking: list[RootCauseRankItem] = Field(min_length=1)
    explanation: str
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def selected_must_be_ranked(self) -> "RootCauseLLMSelection":
        if self.selected_cause not in {item.cause for item in self.ranking}:
            raise ValueError("selected cause is missing from ranking")
        return self

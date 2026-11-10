"""Structured outputs for TRIAD research stages."""
from typing import Dict, List, Literal

from pydantic import BaseModel, Field


class ResearchProposalSchema(BaseModel):
    hypothesis: str
    rationale: str
    novelty: str
    assumptions: List[str] = Field(default_factory=list)
    falsifiable_prediction: str
    proposed_experiment: str
    evidence_needed: List[str] = Field(default_factory=list)
    failure_modes: List[str] = Field(default_factory=list)
    kill_criteria: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    freeform_insight: str


class ResearchCritiqueSchema(BaseModel):
    strongest_point: str
    weakest_assumption: str
    logical_flaw: str
    hidden_risk: str
    falsification_method: str
    experiment_flaw: str
    useful_combination: str
    self_critique: str
    target_observations: Dict[str, str] = Field(default_factory=dict)


class ResearchSynthesisSchema(BaseModel):
    selection_type: Literal["A", "B", "C", "A+B", "A+C", "B+C", "A+B+C", "DISCARD_ALL", "NEW_D"]
    title: str
    synthesized_hypothesis: str
    rationale: str
    source_domains: List[str] = Field(default_factory=list)
    critique_lessons: List[str] = Field(default_factory=list)
    falsifiable_prediction: str
    proposed_experiment: str
    kill_criteria: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class ResearchInterpretationSchema(BaseModel):
    hypothesis_assessment: Literal["supported", "rejected", "inconclusive"]
    reasoning: str
    experiment_flaw: str
    unexpected_observations: List[str] = Field(default_factory=list)
    hidden_confounders: List[str] = Field(default_factory=list)
    next_experiment: str
    abandon_direction: bool
    new_possibilities: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class InterpretationCritiqueSchema(BaseModel):
    strongest_interpretation: str
    weakest_inference: str
    disagreements: List[str] = Field(default_factory=list)
    hidden_confounders: List[str] = Field(default_factory=list)
    evidence_quality: str
    recommended_next_experiment: str
    self_correction: str


class ResearchMemorySchema(BaseModel):
    hypothesis: str
    why_tried: str
    result: Literal["supported", "rejected", "inconclusive"]
    lesson: str
    rejected_ideas: List[str] = Field(default_factory=list)
    negative_evidence: List[str] = Field(default_factory=list)
    do_not_repeat: List[str] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)
    next_direction: str
    milestone_complete: bool = False
    no_progress: bool = False

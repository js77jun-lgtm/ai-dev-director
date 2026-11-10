from typing import List, Literal
from pydantic import BaseModel, Field

class ReviewResultSchema(BaseModel):
    """Result report produced by CODEX REVIEWER."""
    verdict: Literal["pass", "changes_required"] = Field(
        ...,
        description="Review verdict: pass or changes_required"
    )
    p0: List[str] = Field(
        default_factory=list,
        description="Blocker bugs, regressions, or severe security/architectural flaws"
    )
    p1: List[str] = Field(
        default_factory=list,
        description="High-priority bugs, missing critical tests, or scope concerns"
    )
    p2: List[str] = Field(
        default_factory=list,
        description="Minor improvements, style nitpicks, or suggestions"
    )
    summary: str = Field(..., description="Summary of the code review")

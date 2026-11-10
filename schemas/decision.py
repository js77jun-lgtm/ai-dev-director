from typing import List, Literal
from pydantic import BaseModel, Field

class DecisionSchema(BaseModel):
    """Final decision produced by HEAD based on evidence."""
    decision: Literal["PASS", "HUMAN_REQUIRED", "FAIL"] = Field(
        ...,
        description="Final run verdict: PASS, HUMAN_REQUIRED, or FAIL"
    )
    rationale: str = Field(..., description="Explanation of why this decision was reached")
    next_steps: List[str] = Field(
        default_factory=list,
        description="Recommended next steps for the user or developers"
    )

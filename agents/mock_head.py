from typing import Dict, Any
from agents.head import BaseHead
from schemas.order import OrderSchema
from schemas.builder_result import BuilderResultSchema
from schemas.review_result import ReviewResultSchema
from schemas.decision import DecisionSchema

class MockHead(BaseHead):
    """Deterministic Mock HEAD for testing and offline verification."""
    provider: str = "mock"
    agent_family: str = "mock"

    def __init__(self, model: str = "mock-head"):
        self.model = model
        self.provider = "mock"
        self.agent_family = "mock"

    def create_order(self, goal: str, project_context: Dict[str, Any]) -> OrderSchema:
        return OrderSchema(
            goal=goal,
            allowed_paths=["src/", "tests/", "app/"],
            protected_paths=[".git/", "config.yaml", "secret", "data/"],
            acceptance_criteria=[
                "Implement the requested goal cleanly",
                "Pass all automated unit tests",
                "Follow project style"
            ],
            max_files_changed=5,
            max_diff_lines=200,
            context="Mock Head generated order"
        )

    def decide(
        self,
        order: OrderSchema,
        builder_result: BuilderResultSchema,
        verification: Dict[str, Any],
        review_result: ReviewResultSchema,
        cwd: Any = None,
        **kwargs
    ) -> DecisionSchema:
        v_passed = verification.get("passed", False)
        b_passed = (builder_result.status == "COMPLETED")
        r_passed = (review_result.verdict == "pass")

        if not v_passed or not b_passed:
            return DecisionSchema(
                decision="FAIL",
                rationale="Automated verification or builder execution failed.",
                next_steps=["Inspect error logs", "Fix compilation/test issues"]
            )
        
        if len(review_result.p0) > 0:
            return DecisionSchema(
                decision="FAIL",
                rationale=f"Review found critical P0 blocker: {', '.join(review_result.p0)}",
                next_steps=["Resolve critical blocker"]
            )

        if len(review_result.p1) > 0:
            return DecisionSchema(
                decision="HUMAN_REQUIRED",
                rationale=f"Review found P1 issue requiring human judgment: {', '.join(review_result.p1)}",
                next_steps=["Human review of P1 items before proceeding"]
            )

        return DecisionSchema(
            decision="PASS",
            rationale="All acceptance criteria satisfied, tests passed, and review approved.",
            next_steps=["Ready for human merge/review"]
        )

    def proxy_decide(self, question: str, evidence: Dict[str, Any], cwd: Any = None) -> Dict[str, Any]:
        return {"decision": "APPROVE", "reason": "mock proxy approval"}

    def plan_next(self, mission_goal: str, history: list, project_context: Dict[str, Any], cwd: Any = None) -> Dict[str, Any]:
        last = history[-1] if history else {}
        if last.get("final_state") == "PASS":
            return {"status": "DONE", "summary": "mock mission complete", "reason": "last step passed", "order": None}
        return {"status": "CONTINUE", "summary": "mock retry", "reason": "last step did not pass",
                "order": self.create_order(mission_goal, project_context).model_dump()}

    def chat(self, message: str, project_context: Dict[str, Any], history: list) -> Dict[str, Any]:
        return {
            "type": "DEV_TASK",
            "message": "mock generated order",
            "order": self.create_order("mock order from chat", project_context).model_dump()
        }

    def get_provenance(self, exit_code: int = 0):
        from schemas.provenance import AgentProvenanceRecord
        from datetime import datetime
        import sys
        return AgentProvenanceRecord(
            agent="HEAD",
            provider="mock",
            model=self.model,
            executable=sys.executable,
            invocation_command="mock_head.create_order()",
            version="0.1.0",
            exit_code=exit_code,
            timestamp=datetime.utcnow().isoformat()
        )

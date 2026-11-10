from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from schemas.order import OrderSchema
from schemas.builder_result import BuilderResultSchema
from schemas.review_result import ReviewResultSchema
from schemas.decision import DecisionSchema

from schemas.provenance import AgentProvenanceRecord

class BaseHead(ABC):
    """
    Abstract base class for HEAD agent.
    HEAD has read-only access to project context and evidence.
    Forbidden: file write, shell execution, git manipulation.
    """
    provider: str = ""

    @abstractmethod
    def create_order(self, goal: str, project_context: Dict[str, Any]) -> OrderSchema:
        """Analyzes user goal and project context to produce a strict OrderSchema."""
        pass

    @abstractmethod
    def decide(
        self,
        order: OrderSchema,
        builder_result: BuilderResultSchema,
        verification: Dict[str, Any],
        review_result: ReviewResultSchema,
        cwd: Optional[str] = None,
        **kwargs
    ) -> DecisionSchema:
        """Reviews all evidence and issues a final verdict: PASS, HUMAN_REQUIRED, or FAIL."""
        pass

    @abstractmethod
    def get_provenance(self, exit_code: int = 0) -> AgentProvenanceRecord:
        """Returns provenance record proving execution environment without credentials."""
        pass

    @abstractmethod
    def chat(self, message: str, project_context: Dict[str, Any], history: list) -> Dict[str, Any]:
        """Process chat message and return {"type": "QUESTION"|"DEV_TASK", "message": "...", "order": {...}}"""
        pass

    # ------------------------------------------------------------------ user-proxy capabilities
    # Concrete CLI heads provide _invoke_cli/_extract_json; these prompts are shared by all providers.

    PROXY_POLICY = (
        "You act as the USER'S PROXY for an autonomous development system. The user delegated every decision to "
        "you EXCEPT these, which must be ESCALATED to the user:\n"
        "1. spending money: payments, purchases, paid services/subscriptions, paid API keys;\n"
        "2. real-money trading: submitting orders on a live/real broker or exchange account "
        "(demo accounts, backtests and simulations are fine);\n"
        "3. creating accounts or handling real credentials/secrets;\n"
        "4. irreversible actions outside the isolated git worktree: production deploys, deleting user data, "
        "sending messages or emails to people.\n"
        "All code work happens in an isolated worktree that is never merged automatically, so it is reversible. "
        "Negated mentions (e.g. 'no live orders are placed') are NOT exceptions.\n"
    )

    ORDER_SCHEMA_TEXT = (
        '{"goal": string, "allowed_paths": string[], "protected_paths": string[], "acceptance_criteria": string[], '
        '"required_outputs": string[], "reference_paths": string[], "allowed_deletions": string[], '
        '"max_files_changed": integer, "max_diff_lines": integer, "context": string, '
        '"risk_level": "LOW"|"MEDIUM"|"HIGH", "requires_human_approval": false}. '
        'allowed_deletions entries are repo-relative file paths or symbols as "def:name" / "class:name" '
        '(not pytest node ids).'
    )

    def proxy_decide(self, question: str, evidence: Dict[str, Any], cwd: Optional[str] = None) -> Dict[str, Any]:
        """{'decision': 'APPROVE'|'ESCALATE', 'reason': str} on the user's behalf."""
        import json
        prompt = (self.PROXY_POLICY + "\nQUESTION:\n" + question + "\n\nEVIDENCE:\n"
                  + json.dumps(evidence, ensure_ascii=False, default=str)[:30000]
                  + '\n\nRespond ONLY with JSON: {"decision": "APPROVE" | "ESCALATE", "reason": "one sentence"}')
        return self._extract_json(self._invoke_cli(prompt, cwd=cwd))

    def plan_next(self, mission_goal: str, history: list, project_context: Dict[str, Any],
                  cwd: Optional[str] = None) -> Dict[str, Any]:
        """Goal-oriented step planning. Returns {'status': 'DONE'|'CONTINUE'|'BLOCKED', 'summary', 'reason',
        'order': ORDER|None}."""
        import json
        prompt = (
            "You are HEAD, driving a multi-step development MISSION to completion on the user's behalf.\n"
            + self.PROXY_POLICY +
            "\nEach step is one ORDER executed by a Builder in an isolated worktree, then tested by the Director, "
            "reviewed by an independent Reviewer and judged by you. A new step starts from the files of the latest "
            "PASSED step, so build incrementally on what exists.\n\n"
            "Decide the next move:\n"
            "- DONE: the mission goal's deliverables exist and the step evidence shows they are tested and working.\n"
            "- CONTINUE: give the next ORDER - one focused, testable milestone that moves toward the goal. If the "
            "previous step failed, change the approach or narrow the scope; never resubmit an identical ORDER.\n"
            "- BLOCKED: only if the next move needs one of the escalation categories above.\n"
            "If the previous step FAILED but its code is worth keeping (e.g. tests pass and only review findings "
            "remain), set \"build_on_last_attempt\": true so the next step continues from that attempt's files instead "
            "of starting over; the next ORDER should then fix the outstanding findings.\n"
            "ORDER rules: repo-relative paths only (e.g. 'src/**', 'tests/test_x.py'); never .git/.agents/GEMINI.md; "
            "include behavior tests in required_outputs; keep max_files_changed <= 15 and max_diff_lines <= 1500; "
            "reference_paths ONLY for absolute paths of external documents the user supplied (repo files are always "
            "given to the Builder; never list them there); "
            "risk_level HIGH only for the escalation categories, an ambitious goal is MEDIUM.\n"
            "Before you answer, check your ORDER against itself: every file in required_outputs must be inside "
            "allowed_paths and must NOT match protected_paths (a wildcard such as data/*.json also protects a new "
            "data/x.json).\n\n"
            + ("YOUR PREVIOUS ORDER WAS REJECTED:\n" + str(project_context["previous_plan_rejected"]) + "\n\n"
               if project_context.get("previous_plan_rejected") else "")
            + "MISSION GOAL:\n" + mission_goal + "\n\n"
            "STEPS SO FAR:\n" + json.dumps(history, ensure_ascii=False, default=str)[:40000] + "\n\n"
            "CURRENT PROJECT FILES:\n" + json.dumps(project_context, ensure_ascii=False, default=str)[:12000] + "\n\n"
            'Respond ONLY with JSON: {"status": "DONE"|"CONTINUE"|"BLOCKED", "summary": "what is done and what '
            'remains, in Korean", "reason": "why", "build_on_last_attempt": false, "order": '
            + self.ORDER_SCHEMA_TEXT + ' or null}'
        )
        return self._extract_json(self._invoke_cli(prompt, cwd=cwd))

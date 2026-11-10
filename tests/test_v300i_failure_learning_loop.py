"""V3.0-H failure-learning loop correction.

Reviewer changes_required, when it comes from an ordinary in-scope implementation defect (no P0, no
Scope Guard/protected-path/provider/busy/baseline halt), is a research failure - not an immediate
HUMAN_REQUIRED. It is preserved as evidence and routed to the existing Result Interpretation ->
Cross Critique -> Research Memory -> bounded Next Cycle chain, exactly like the pre-existing
deterministic-verifier-failure path already does. Every genuine safety/infra halt (Scope Guard,
protected path, P0 blocker, provider/config failure) keeps escalating to HUMAN_REQUIRED exactly as
before - this file proves both directions.
"""
import json
from pathlib import Path

from agents.mock_head import MockHead
from agents.codex_reviewer import CodexReviewer
from core.orchestrator import Orchestrator
from core.research_experiment import TriadExperimentOrchestrator
from core.research_state import (
    INTERPRETATION_CROSS_STAGE,
    INTERPRETATION_INDEPENDENT_STAGE,
    RESEARCH_MEMORY_STAGE,
    ResearchState,
)
from core.state_machine import State
from schemas.review_result import ReviewResultSchema

from tests.test_v300e_experiment_pipeline import SYNTHESIS, _at_planning, _order


class FixedVerdictReviewer:
    """Deterministic reviewer stub: always returns the same verdict, regardless of the real diff."""

    def __init__(self, verdict, p0=None, p1=None, p2=None, summary="stub review", provider="codex"):
        self.verdict = verdict
        self.p0 = p0 or []
        self.p1 = p1 or []
        self.p2 = p2 or []
        self.summary = summary
        self.provider = provider
        self.agent_family = provider
        self.last_execution_info = {"exit_code": 0}

    def review(self, order, git_diff, verification, project_context=None, worktree_path=None):
        return ReviewResultSchema(verdict=self.verdict, p0=self.p0, p1=self.p1, p2=self.p2, summary=self.summary)


def _factory_with_reviewer(orchestrator, created, reviewer):
    config = {
        "head": {"provider": "mock"}, "reviewer": {"provider": "mock"},
        "verification": {"test_command": "python -m pytest tests/ -q"},
    }

    def factory(project_evidence, baseline_evidence):
        normal = TriadExperimentOrchestrator(
            config=config,
            head_agent=MockHead(), reviewer_agent=reviewer,
            greenfield_project_evidence=project_evidence if baseline_evidence else None,
            greenfield_baseline_evidence=baseline_evidence,
        )
        created.append(normal)
        return normal

    orchestrator.experiment_factory = factory


def _passing_builder(builder, order, worktree):
    builder.pre_tool_write_file("src/app.py", "def value():\n    return 2\n", worktree, order)
    builder.pre_tool_write_file(
        "tests/test_app.py", "from src.app import value\n\ndef test_value():\n    assert value() == 2\n",
        worktree, order,
    )
    return ["src/app.py", "tests/test_app.py"]


def _rogue_builder(builder, order, worktree):
    (Path(worktree) / "outside.py").write_text("unsafe", encoding="utf-8")
    return ["outside.py"]


def _protected_path_builder(builder, order, worktree):
    (Path(worktree) / ".git" / "director_rogue_hook").write_text("unsafe", encoding="utf-8")
    return [".git/director_rogue_hook"]


def _to_result_interpretation(tmp_path, reviewer, builder_fn=_passing_builder):
    _, orchestrator, session_id, cycle_id, _ = _at_planning(tmp_path, greenfield=True)
    orchestrator.plan_experiment(session_id)
    created = []
    _factory_with_reviewer(orchestrator, created, reviewer)
    result = orchestrator.execute_experiment(session_id, builder_fn=builder_fn)
    return orchestrator, session_id, cycle_id, result


INTERPRETATION_PAYLOAD = {
    "hypothesis_assessment": "rejected",
    "reasoning": "Reviewer found a real in-scope edge-case defect.",
    "experiment_flaw": "Canonicalization was context-dependent.",
    "unexpected_observations": ["Decimal.normalize() depends on ambient context"],
    "hidden_confounders": [],
    "next_experiment": "Use a context-independent canonical representation.",
    "abandon_direction": False,
    "new_possibilities": [],
    "confidence": 0.6,
}

CRITIQUE_PAYLOAD = {
    "strongest_interpretation": "The defect is real and in-scope.",
    "weakest_inference": "Confidence in root cause is only moderate.",
    "disagreements": [],
    "hidden_confounders": [],
    "evidence_quality": "Reviewer diff and verifier output are concrete.",
    "recommended_next_experiment": "Re-implement canonicalization without ambient context.",
    "self_correction": "None needed.",
}


def _memory_payload(no_progress=False, milestone_complete=False):
    return {
        "hypothesis": SYNTHESIS["synthesized_hypothesis"],
        "why_tried": "Smallest falsifiable experiment for the synthesized hypothesis.",
        "result": "rejected",
        "lesson": "canonicalization must not depend on ambient interpreter/library context",
        "rejected_ideas": ["context-dependent Decimal.normalize() based canonicalization"],
        "negative_evidence": ["Decimal.normalize() output depends on the active Decimal context"],
        "do_not_repeat": ["Rely on context-dependent Decimal normalization"],
        "open_questions": [],
        "next_direction": "Implement an explicit context-independent canonical representation.",
        "milestone_complete": milestone_complete,
        "no_progress": no_progress,
    }


def _stage_stub(payloads):
    def call(domain, prompt, stage, cwd):
        payload = payloads[stage]
        payload = payload(domain) if callable(payload) else payload
        return {
            "output": json.dumps(payload), "executable": "stub", "version": "test",
            "invocation": "stub", "exit_code": 0, "duration_sec": 0.01,
            "authentication_mode": "mock", "api_key_used": False,
            "observed_fields": ["exit_code", "duration_sec"],
            "declared_fields": ["authentication_mode", "api_key_used"],
        }
    return call


def _run_full_interpretation_chain(orchestrator, session_id, memory_no_progress=False, memory_milestone=False):
    orchestrator.provider_call = _stage_stub({
        INTERPRETATION_INDEPENDENT_STAGE: INTERPRETATION_PAYLOAD,
        INTERPRETATION_CROSS_STAGE: CRITIQUE_PAYLOAD,
        RESEARCH_MEMORY_STAGE: _memory_payload(memory_no_progress, memory_milestone),
    })
    interp = orchestrator.run_result_interpretation(session_id)
    assert interp["interpretation_substage"] == INTERPRETATION_CROSS_STAGE
    cross = orchestrator.run_interpretation_cross_critique(session_id)
    assert cross["interpretation_substage"] == RESEARCH_MEMORY_STAGE
    return orchestrator.consolidate_research_memory(session_id)


# TEST A -----------------------------------------------------------------------------------------

def test_a_reviewer_changes_required_ordinary_defect_becomes_research_failure(tmp_path):
    reviewer = FixedVerdictReviewer("changes_required", p1=["off-by-one in a boundary case"])
    orchestrator, session_id, cycle_id, result = _to_result_interpretation(tmp_path, reviewer)
    assert result["current_stage"] == ResearchState.RESULT_INTERPRETATION
    assert result["human_required"] is False
    experiment = result["experiment"]
    assert experiment["status"] == "FAILED"
    assert experiment["normal_final_state"] == State.HUMAN_REQUIRED
    assert experiment["result"]["review"]["verdict"] == "changes_required"
    assert experiment["result"]["review"]["p1"] == ["off-by-one in a boundary case"]
    assert experiment["result"]["review"]["p0"] == []


# TEST B -----------------------------------------------------------------------------------------

def test_b_three_independent_interpretations_of_the_failed_experiment(tmp_path):
    reviewer = FixedVerdictReviewer("changes_required", p1=["off-by-one in a boundary case"])
    orchestrator, session_id, cycle_id, _ = _to_result_interpretation(tmp_path, reviewer)
    orchestrator.provider_call = _stage_stub({INTERPRETATION_INDEPENDENT_STAGE: INTERPRETATION_PAYLOAD})
    interp = orchestrator.run_result_interpretation(session_id)
    assert interp["interpretation_substage"] == INTERPRETATION_CROSS_STAGE
    rows = orchestrator.rsm.get_stage_outputs(cycle_id, INTERPRETATION_INDEPENDENT_STAGE)
    assert {row["domain"] for row in rows} == {"OPENAI", "ANTHROPIC", "GOOGLE"}
    for row in rows:
        assert row["payload"]["hypothesis_assessment"] == "rejected"


# TEST C -----------------------------------------------------------------------------------------

def test_c_cross_critique_blocked_until_all_three_interpretations_exist(tmp_path):
    reviewer = FixedVerdictReviewer("changes_required", p1=["off-by-one in a boundary case"])
    orchestrator, session_id, cycle_id, _ = _to_result_interpretation(tmp_path, reviewer)
    # No independent interpretation has been run yet - cross critique must refuse, not silently proceed.
    result = orchestrator.run_interpretation_cross_critique(session_id)
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert result["human_required"] is True
    assert orchestrator.rsm.get_stage_outputs(cycle_id, INTERPRETATION_CROSS_STAGE) == []


# TEST D -----------------------------------------------------------------------------------------

def test_d_reviewer_findings_land_in_research_memory(tmp_path):
    reviewer = FixedVerdictReviewer("changes_required", p1=["Decimal.normalize() is context-dependent"])
    orchestrator, session_id, cycle_id, _ = _to_result_interpretation(tmp_path, reviewer)
    memory_result = _run_full_interpretation_chain(orchestrator, session_id)
    assert not memory_result["human_required"]
    memory_rows = orchestrator.rsm.list_research_memory(session_id)
    assert len(memory_rows) == 1
    payload = memory_rows[0]["payload"]
    assert payload["result"] == "rejected"
    assert "ambient" in payload["lesson"]
    assert payload["negative_evidence"] and "Decimal.normalize()" in payload["negative_evidence"][0]
    # The experiment evidence that fed the memory prompt still carries the real Reviewer finding verbatim.
    experiment = orchestrator.rsm.get_experiment(cycle_id)
    assert "Decimal.normalize() is context-dependent" in experiment["result"]["review"]["p1"]


# TEST E -----------------------------------------------------------------------------------------

def test_e_learned_constraint_reflected_in_next_cycle_prompt(tmp_path):
    reviewer = FixedVerdictReviewer("changes_required", p1=["Decimal.normalize() is context-dependent"])
    orchestrator, session_id, cycle_id, _ = _to_result_interpretation(tmp_path, reviewer)
    memory_result = _run_full_interpretation_chain(orchestrator, session_id)
    assert memory_result["current_stage"] == ResearchState.NEXT_CYCLE
    started = orchestrator.start_next_cycle(session_id)
    assert not started["human_required"]
    next_cycle = orchestrator.rsm.list_cycles(session_id)[-1]
    prompt_rows = orchestrator.rsm.get_prompt_artifacts(next_cycle["cycle_id"], ResearchState.INDEPENDENT_RESEARCH)
    assert prompt_rows
    prompt_text = Path(prompt_rows[0]["artifact_path"]).read_text(encoding="utf-8")
    assert "context-independent canonical representation" in prompt_text


# TEST F -----------------------------------------------------------------------------------------

def test_f_cycle_2_starts_in_the_same_research_session(tmp_path):
    reviewer = FixedVerdictReviewer("changes_required", p1=["Decimal.normalize() is context-dependent"])
    orchestrator, session_id, cycle_id, _ = _to_result_interpretation(tmp_path, reviewer)
    _run_full_interpretation_chain(orchestrator, session_id)
    orchestrator.start_next_cycle(session_id)
    cycles = orchestrator.rsm.list_cycles(session_id)
    assert len(cycles) == 2
    assert all(c["session_id"] == session_id for c in cycles)
    assert cycles[0]["cycle_number"] == 1 and cycles[1]["cycle_number"] == 2
    assert cycles[1]["stage"] == ResearchState.INDEPENDENT_RESEARCH


# TEST G -----------------------------------------------------------------------------------------

def test_g_cycle_2_receives_compact_memory_only_not_raw_cycle_1_history(tmp_path):
    reviewer = FixedVerdictReviewer("changes_required", p1=["Decimal.normalize() is context-dependent"])
    orchestrator, session_id, cycle_id, _ = _to_result_interpretation(tmp_path, reviewer)
    _run_full_interpretation_chain(orchestrator, session_id)
    orchestrator.start_next_cycle(session_id)
    next_cycle = orchestrator.rsm.list_cycles(session_id)[-1]
    prompt_rows = orchestrator.rsm.get_prompt_artifacts(next_cycle["cycle_id"], ResearchState.INDEPENDENT_RESEARCH)
    envelope = json.loads(Path(prompt_rows[0]["artifact_path"]).read_text(encoding="utf-8"))
    facts = envelope["prompt"]["sources"]["RESEARCH_MEMORY_FACTS"]
    assert len(facts) == 1
    fact = facts[0]
    assert set(fact) == {"hypothesis", "result", "lesson", "do_not_repeat", "open_questions",
                          "next_direction", "evidence_sha256"}
    serialized = json.dumps(envelope, ensure_ascii=False)
    # Raw cycle-1 artifacts (reviewer verdict, P1 finding text, provenance hashes) must never leak into
    # cycle 2's prompt - only the compact memory fact (hypothesis/lesson/next_direction/...) may appear.
    assert "changes_required" not in serialized
    assert "Decimal.normalize() is context-dependent" not in serialized
    assert "prompt_sha256" not in json.dumps(fact)
    assert "p0" not in fact and "p1" not in fact and "review" not in fact


# TEST H -----------------------------------------------------------------------------------------

def test_h_scope_guard_violation_never_reaches_result_interpretation(tmp_path):
    reviewer = FixedVerdictReviewer("pass")
    _, session_id, cycle_id, result = _to_result_interpretation(tmp_path, reviewer, builder_fn=_rogue_builder)
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert result["human_required"] is True
    assert result["experiment"]["result"]["scope_violations"] > 0
    assert result["current_stage"] != ResearchState.RESULT_INTERPRETATION


# TEST I -----------------------------------------------------------------------------------------

def test_i_protected_path_violation_never_reaches_result_interpretation(tmp_path):
    reviewer = FixedVerdictReviewer("pass")
    _, session_id, cycle_id, result = _to_result_interpretation(
        tmp_path, reviewer, builder_fn=_protected_path_builder
    )
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert result["human_required"] is True
    assert result["current_stage"] != ResearchState.RESULT_INTERPRETATION


# TEST J -----------------------------------------------------------------------------------------

def test_j_reviewer_config_error_stays_human_required_not_research_failure(tmp_path):
    reviewer = CodexReviewer(provider="openai", executable="/definitely/does/not/exist/codex-binary")
    _, session_id, cycle_id, result = _to_result_interpretation(tmp_path, reviewer)
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert result["human_required"] is True
    experiment = result["experiment"]
    assert any("CONFIG_ERROR" in item for item in experiment["result"]["review"]["p0"])


# TEST K -----------------------------------------------------------------------------------------

def test_k_cycle_budget_exhausted_is_a_bounded_stop_not_next_cycle(tmp_path):
    reviewer = FixedVerdictReviewer("changes_required", p1=["Decimal.normalize() is context-dependent"])
    orchestrator, session_id, cycle_id, _ = _to_result_interpretation(tmp_path, reviewer)
    with orchestrator.rsm._get_conn() as conn:
        conn.execute("UPDATE research_sessions SET cycle_budget = 1 WHERE session_id = ?", (session_id,))
    memory_result = _run_full_interpretation_chain(orchestrator, session_id)
    assert memory_result["current_stage"] == ResearchState.RESEARCH_STOPPED
    assert memory_result["status"] == "RESEARCH_STOPPED"


# TEST L -----------------------------------------------------------------------------------------

def test_l_no_progress_is_a_bounded_stop(tmp_path):
    reviewer = FixedVerdictReviewer("changes_required", p1=["Decimal.normalize() is context-dependent"])
    orchestrator, session_id, cycle_id, _ = _to_result_interpretation(tmp_path, reviewer)
    memory_result = _run_full_interpretation_chain(orchestrator, session_id, memory_no_progress=True)
    assert memory_result["current_stage"] == ResearchState.RESEARCH_STOPPED
    assert orchestrator.rsm.get_session(session_id)["stopped_reason"] == "NO_PROGRESS"


# TEST M -----------------------------------------------------------------------------------------

def test_m_changes_required_verdict_is_never_rewritten_to_pass(tmp_path):
    reviewer = FixedVerdictReviewer("changes_required", p1=["Decimal.normalize() is context-dependent"])
    orchestrator, session_id, cycle_id, result = _to_result_interpretation(tmp_path, reviewer)
    experiment = orchestrator.rsm.get_experiment(cycle_id)
    assert experiment["result"]["review"]["verdict"] == "changes_required"
    assert experiment["status"] == "FAILED"
    assert experiment["normal_final_state"] != State.PASS


# TEST N -----------------------------------------------------------------------------------------

def test_n_technical_failure_provenance_and_reviewer_findings_both_preserved(tmp_path):
    reviewer = FixedVerdictReviewer(
        "changes_required", p1=["Decimal.normalize() is context-dependent"], p2=["minor style nit"]
    )
    orchestrator, session_id, cycle_id, result = _to_result_interpretation(tmp_path, reviewer)
    experiment = orchestrator.rsm.get_experiment(cycle_id)
    r = experiment["result"]
    assert r["builder"]["status"] == "COMPLETED"
    assert r["verification"]["passed"] is True
    assert r["review"]["p1"] == ["Decimal.normalize() is context-dependent"]
    assert r["review"]["p2"] == ["minor style nit"]
    assert r.get("provenance") is not None


# TEST O -----------------------------------------------------------------------------------------

def test_o_normal_decision_and_pass_gate_logic_untouched_by_construction():
    assert TriadExperimentOrchestrator.apply_final_pass_gate is Orchestrator.apply_final_pass_gate
    assert TriadExperimentOrchestrator.run_decision_with_failover is Orchestrator.run_decision_with_failover
    assert TriadExperimentOrchestrator.run_reviewer_with_failover is Orchestrator.run_reviewer_with_failover
    assert TriadExperimentOrchestrator._finalize is Orchestrator._finalize

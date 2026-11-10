import json
from pathlib import Path

import pytest

from core.research_orchestrator import ResearchOrchestrator
from core.provider_manager import ProviderStatus
from core.research_prompts import (
    IndependentResearchContaminationError,
    build_independent_interpretation_prompt,
)
from core.research_state import (
    INTERPRETATION_CROSS_STAGE,
    INTERPRETATION_INDEPENDENT_STAGE,
    RESEARCH_MEMORY_STAGE,
    ResearchState,
)


SYNTHESIS = {
    "selection_type": "NEW_D", "title": "Evidence experiment",
    "synthesized_hypothesis": "A tiny deterministic function produces the expected value.",
    "rationale": "Direct test", "source_domains": ["OPENAI", "ANTHROPIC", "GOOGLE"],
    "critique_lessons": ["Measure it"], "falsifiable_prediction": "The test returns 2",
    "proposed_experiment": "Build and test it", "kill_criteria": ["test fails"],
    "risks": ["confound"], "confidence": 0.7,
}
ORDER = {
    "goal": "test hypothesis", "allowed_paths": ["src/", "tests/"],
    "protected_paths": [".git/"], "acceptance_criteria": ["tests"],
    "max_files_changed": 3, "max_diff_lines": 100, "allowed_deletions": [],
    "context": "research", "risk_level": "LOW", "requires_human_approval": False,
}


def _interpretation(domain):
    return {
        "hypothesis_assessment": "inconclusive", "reasoning": f"{domain} evidence reading",
        "experiment_flaw": "sample is small", "unexpected_observations": ["timing variance"],
        "hidden_confounders": ["environment"], "next_experiment": "repeat with another fixture",
        "abandon_direction": False, "new_possibilities": ["simpler invariant"], "confidence": 0.55,
    }


def _critique(domain):
    return {
        "strongest_interpretation": "it distinguishes fact from inference",
        "weakest_inference": f"{domain} overweights one observation",
        "disagreements": ["failure cause"], "hidden_confounders": ["runner state"],
        "evidence_quality": "limited but reproducible",
        "recommended_next_experiment": "repeat deterministically", "self_correction": "lower confidence",
    }


def _memory(**overrides):
    payload = {
        "hypothesis": SYNTHESIS["synthesized_hypothesis"], "why_tried": "directly falsifiable",
        "result": "inconclusive", "lesson": "one run is insufficient",
        "rejected_ideas": ["assume success from build completion"],
        "negative_evidence": ["post verification did not establish the prediction"],
        "do_not_repeat": ["same fixture without new controls"],
        "open_questions": ["does environment affect the value?"],
        "next_direction": "add an independent deterministic control", "milestone_complete": False,
        "no_progress": False,
    }
    payload.update(overrides)
    return payload


class Calls:
    def __init__(self, memory=None, fail_domain=None):
        self.items = []
        self.memory = memory or _memory()
        self.fail_domain = fail_domain

    def __call__(self, domain, prompt, stage, cwd):
        data = json.loads(prompt)
        self.items.append((domain, stage, data))
        if domain == self.fail_domain and stage == INTERPRETATION_INDEPENDENT_STAGE:
            raise RuntimeError("forced interpretation failure")
        if stage == INTERPRETATION_INDEPENDENT_STAGE:
            assert "interpretations" not in data
            assert "peer_interpretation" not in data
            output = _interpretation(domain)
        elif stage == INTERPRETATION_CROSS_STAGE:
            assert set(data["interpretations"]) in (
                {"OPENAI", "ANTHROPIC", "GOOGLE"}, {"OPENAI", "GOOGLE"}
            )
            output = _critique(domain)
        elif stage == RESEARCH_MEMORY_STAGE:
            output = self.memory
        else:
            raise AssertionError(f"unexpected stage {stage}")
        return {
            "output": json.dumps(output), "executable": f"stub-{domain.lower()}", "version": "test",
            "invocation": "stub", "exit_code": 0, "duration_sec": 0.01,
            "authentication_mode": "mock", "api_key_used": False,
            "observed_fields": ["exit_code", "duration_sec"],
            "declared_fields": ["authentication_mode", "api_key_used"],
        }


def _ready(tmp_path: Path, calls: Calls, cycle_budget=8):
    project = tmp_path / "project"
    project.mkdir()
    orchestrator = ResearchOrchestrator(
        artifact_root=tmp_path / "research", provider_call=calls
    )
    session = orchestrator.start_session(str(project), "Moonshot", cycle_budget=cycle_budget)
    cycle = orchestrator.rsm.list_cycles(session["session_id"])[-1]
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.CROSS_CRITIQUE, "test")
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.SYNTHESIS, "test")
    encoded = json.dumps(SYNTHESIS)
    orchestrator.rsm.record_stage_output(
        cycle["cycle_id"], ResearchState.SYNTHESIS, "OPENAI", "synthesis.json", "a" * 64,
        len(encoded), len(encoded.encode()), SYNTHESIS,
    )
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.EXPERIMENT_PLANNING, "test")
    orchestrator.rsm.record_experiment_plan(
        cycle["cycle_id"], ORDER, "order.json", "b" * 64, "EXISTING", "REQUIRED"
    )
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.EXPERIMENT_BUILDING, "test")
    result = {
        "run_id": "NORMAL-1", "final_state": "FAIL", "scope_violations": 0,
        "builder": {"status": "COMPLETED"},
        "verification": {"passed": False, "summary": "Test: FAIL"},
    }
    orchestrator.rsm.record_experiment_result(
        cycle["cycle_id"], "NORMAL-1", "FAILED", "FAIL", str(project / "worktree"), result
    )
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.EXPERIMENT_VERIFYING, "test")
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.EXPERIMENT_REVIEW, "test")
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.RESULT_INTERPRETATION, "test")
    return orchestrator, session["session_id"], cycle["cycle_id"]


def test_independent_then_cross_then_memory_uses_seven_calls(tmp_path):
    calls = Calls()
    orchestrator, session_id, cycle_id = _ready(tmp_path, calls)
    first = orchestrator.run_result_interpretation(session_id)
    assert first["interpretation_substage"] == INTERPRETATION_CROSS_STAGE
    assert len(orchestrator.rsm.get_stage_outputs(cycle_id, INTERPRETATION_INDEPENDENT_STAGE)) == 3
    second = orchestrator.run_interpretation_cross_critique(session_id)
    assert second["interpretation_substage"] == RESEARCH_MEMORY_STAGE
    final = orchestrator.consolidate_research_memory(session_id)
    assert final["current_stage"] == ResearchState.NEXT_CYCLE
    assert len(calls.items) == 7
    assert [stage for _, stage, _ in calls.items].count(INTERPRETATION_INDEPENDENT_STAGE) == 3
    assert [stage for _, stage, _ in calls.items].count(INTERPRETATION_CROSS_STAGE) == 3
    assert [stage for _, stage, _ in calls.items].count(RESEARCH_MEMORY_STAGE) == 1
    memory = final["research_memory"][0]
    assert memory["payload"]["negative_evidence"]
    assert memory["payload"]["do_not_repeat"]
    assert memory["source_evidence"]["interpretation_sha256"].keys() == {"OPENAI", "ANTHROPIC", "GOOGLE"}
    assert len(orchestrator.rsm.get_call_provenance(cycle_id)) == 7


def test_one_independent_failure_discloses_nothing_to_cross_stage(tmp_path):
    calls = Calls(fail_domain="GOOGLE")
    orchestrator, session_id, cycle_id = _ready(tmp_path, calls)
    result = orchestrator.run_result_interpretation(session_id)
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert len(calls.items) == 3
    assert not any(stage == INTERPRETATION_CROSS_STAGE for _, stage, _ in calls.items)


def test_cross_critique_before_complete_interpretations_calls_no_provider(tmp_path):
    calls = Calls()
    orchestrator, session_id, _ = _ready(tmp_path, calls)
    result = orchestrator.run_interpretation_cross_critique(session_id)
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert calls.items == []


def test_independent_interpretation_structurally_rejects_peer_material():
    with pytest.raises(IndependentResearchContaminationError):
        build_independent_interpretation_prompt(
            "OPENAI", "goal", SYNTHESIS,
            {"result": {"interpretations": {"GOOGLE": {"reasoning": "peer"}}}},
        )


def test_no_progress_is_persisted_then_stops_research(tmp_path):
    calls = Calls(memory=_memory(no_progress=True, next_direction="none defensible"))
    orchestrator, session_id, _ = _ready(tmp_path, calls)
    orchestrator.run_result_interpretation(session_id)
    orchestrator.run_interpretation_cross_critique(session_id)
    result = orchestrator.consolidate_research_memory(session_id)
    assert result["current_stage"] == ResearchState.RESEARCH_STOPPED
    assert result["status"] == "RESEARCH_STOPPED"
    assert result["research_memory"][0]["payload"]["no_progress"] is True


def test_milestone_complete_is_terminal_and_evidence_backed(tmp_path):
    calls = Calls(memory=_memory(milestone_complete=True, next_direction="deliver result"))
    orchestrator, session_id, _ = _ready(tmp_path, calls)
    orchestrator.run_result_interpretation(session_id)
    orchestrator.run_interpretation_cross_critique(session_id)
    result = orchestrator.consolidate_research_memory(session_id)
    assert result["current_stage"] == ResearchState.MILESTONE_COMPLETE
    assert result["status"] == "MILESTONE_COMPLETE"


def test_cycle_budget_stops_after_memory_without_open_ended_retry(tmp_path):
    calls = Calls()
    orchestrator, session_id, _ = _ready(tmp_path, calls, cycle_budget=1)
    orchestrator.run_result_interpretation(session_id)
    orchestrator.run_interpretation_cross_critique(session_id)
    result = orchestrator.consolidate_research_memory(session_id)
    assert result["current_stage"] == ResearchState.RESEARCH_STOPPED
    assert orchestrator.rsm.get_session(session_id)["stopped_reason"] == "CYCLE_BUDGET_EXHAUSTED"
    assert orchestrator.rsm.get_session(session_id)["cycles_completed"] == 1


def test_memory_retrieval_returns_compact_relevant_facts_not_raw_history(tmp_path):
    calls = Calls()
    orchestrator, session_id, _ = _ready(tmp_path, calls)
    orchestrator.run_result_interpretation(session_id)
    orchestrator.run_interpretation_cross_critique(session_id)
    orchestrator.consolidate_research_memory(session_id)
    facts = orchestrator.retrieve_research_memory_facts(session_id, "environment value", limit=2)
    assert len(facts) == 1
    assert set(facts[0]) == {
        "hypothesis", "result", "lesson", "do_not_repeat", "open_questions",
        "next_direction", "evidence_sha256",
    }
    assert "source_evidence" not in facts[0] and "interpretations" not in facts[0]


def test_repeated_hypothesis_is_blocked_before_new_planner_call(tmp_path):
    calls = Calls()
    orchestrator, session_id, cycle_id = _ready(tmp_path, calls)
    hypothesis_hash = orchestrator._hypothesis_sha256(SYNTHESIS["synthesized_hypothesis"])
    orchestrator.rsm.record_research_memory(
        session_id, cycle_id, hypothesis_hash, _memory(), {"seed": "prior evidence"}
    )
    # Move back is deliberately impossible; create a second cycle at planning to model the later cycle.
    second_id = f"{session_id}-C2"
    orchestrator.rsm.create_cycle(second_id, session_id, 2)
    orchestrator.rsm.register_participants(second_id, {d: "COMPLETE" for d in ("OPENAI", "ANTHROPIC", "GOOGLE")})
    orchestrator.rsm.transition_cycle(second_id, ResearchState.INDEPENDENT_RESEARCH, "test")
    orchestrator.rsm.transition_cycle(second_id, ResearchState.CROSS_CRITIQUE, "test")
    orchestrator.rsm.transition_cycle(second_id, ResearchState.SYNTHESIS, "test")
    orchestrator.rsm.record_stage_output(
        second_id, ResearchState.SYNTHESIS, "OPENAI", "synthesis2.json", "c" * 64, 1, 1, SYNTHESIS
    )
    orchestrator.rsm.transition_cycle(second_id, ResearchState.EXPERIMENT_PLANNING, "test")
    result = orchestrator.plan_experiment(session_id)
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert "REPEATED_HYPOTHESIS" in result["message"]
    assert calls.items == []


def test_interpretation_idempotency_does_not_repeat_provider_calls(tmp_path):
    calls = Calls()
    orchestrator, session_id, _ = _ready(tmp_path, calls)
    orchestrator.run_result_interpretation(session_id)
    orchestrator.run_result_interpretation(session_id)
    orchestrator.run_interpretation_cross_critique(session_id)
    orchestrator.run_interpretation_cross_critique(session_id)
    assert len(calls.items) == 6


def test_next_cycle_rechecks_readiness_and_injects_compact_memory(tmp_path):
    calls = Calls()
    orchestrator, session_id, _ = _ready(tmp_path, calls)
    orchestrator.run_result_interpretation(session_id)
    orchestrator.run_interpretation_cross_critique(session_id)
    orchestrator.consolidate_research_memory(session_id)
    result = orchestrator.start_next_cycle(session_id)
    assert result["current_cycle"] == 2
    assert result["current_stage"] == ResearchState.INDEPENDENT_RESEARCH
    assert len(result["prompt_artifacts"]) == 3
    envelope = json.loads(Path(result["prompt_artifacts"][0]["artifact_path"]).read_text(encoding="utf-8"))
    facts = envelope["prompt"]["sources"]["RESEARCH_MEMORY_FACTS"]
    assert len(facts) == 1 and facts[0]["do_not_repeat"]
    assert "source_evidence" not in facts[0]
    assert len(calls.items) == 7, "starting a cycle prepares prompts but performs no inference"


def test_next_cycle_provider_quota_block_prevents_inference(tmp_path):
    calls = Calls()
    orchestrator, session_id, _ = _ready(tmp_path, calls)
    orchestrator.run_result_interpretation(session_id)
    orchestrator.run_interpretation_cross_critique(session_id)
    orchestrator.consolidate_research_memory(session_id)
    before = len(calls.items)
    orchestrator.pm.set_status("ANTHROPIC", ProviderStatus.QUOTA_EXHAUSTED, "test")
    orchestrator.pm.set_status("GOOGLE", ProviderStatus.QUOTA_EXHAUSTED, "test")
    result = orchestrator.start_next_cycle(session_id)
    assert result["human_required"] is True
    assert result["current_stage"] == ResearchState.RESEARCH_INTAKE
    assert result["prompt_artifacts"] == []
    assert len(calls.items) == before


def test_user_stop_after_completed_cycle_is_idempotent_and_not_double_counted(tmp_path):
    calls = Calls()
    orchestrator, session_id, _ = _ready(tmp_path, calls)
    orchestrator.run_result_interpretation(session_id)
    orchestrator.run_interpretation_cross_critique(session_id)
    orchestrator.consolidate_research_memory(session_id)
    assert orchestrator.rsm.get_session(session_id)["cycles_completed"] == 1
    first = orchestrator.stop_session(session_id, "USER_STOP")
    second = orchestrator.stop_session(session_id, "USER_STOP")
    assert first["status"] == second["status"] == "RESEARCH_STOPPED"
    assert orchestrator.rsm.get_session(session_id)["cycles_completed"] == 1
    assert len(calls.items) == 7

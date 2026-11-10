import json
import subprocess
from pathlib import Path

from agents.codex_reviewer import CodexReviewer
from agents.mock_head import MockHead
from core.orchestrator import Orchestrator
from core.research_experiment import TriadExperimentOrchestrator
from core.research_orchestrator import ResearchOrchestrator
from core.research_state import ResearchState
from core.state_machine import State


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, greenfield: bool) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "triad-test@example.invalid")
    _git(repo, "config", "user.name", "TRIAD Test")
    if not greenfield:
        (repo / "src").mkdir()
        (repo / "tests").mkdir()
        (repo / "src" / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        (repo / "tests" / "test_app.py").write_text(
            "from src.app import value\n\ndef test_value():\n    assert value() == 1\n", encoding="utf-8"
        )
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "existing project")
    else:
        _git(repo, "commit", "--allow-empty", "-m", "initial empty project")
    return repo


SYNTHESIS = {
    "selection_type": "NEW_D",
    "title": "Tiny falsifiable experiment",
    "synthesized_hypothesis": "A deterministic value function can be implemented with one focused test.",
    "rationale": "Small and directly measurable.",
    "source_domains": ["OPENAI", "ANTHROPIC", "GOOGLE"],
    "critique_lessons": ["Keep the experiment minimal."],
    "falsifiable_prediction": "The new test passes only when the function returns 2.",
    "proposed_experiment": "Implement next_value and a unit test.",
    "kill_criteria": ["Post-build test fails."],
    "risks": ["Incorrect path scope."],
    "confidence": 0.7,
}


def _order(goal="Implement a tiny experiment"):
    return {
        "goal": goal,
        "allowed_paths": ["src/", "tests/"],
        "protected_paths": [".git/", "config.yaml", "data/"],
        "acceptance_criteria": ["A deterministic post-build unit test passes"],
        "max_files_changed": 4,
        "max_diff_lines": 120,
        "allowed_deletions": [],
        "context": "TRIAD synthesized experiment",
        "risk_level": "LOW",
        "requires_human_approval": False,
    }


def _planner(payload, calls):
    def call(**kwargs):
        calls.append(kwargs)
        assert kwargs["stage"] == ResearchState.EXPERIMENT_PLANNING
        return {
            "output": json.dumps(payload), "executable": "stub-codex", "version": "test",
            "invocation": "stub", "exit_code": 0, "duration_sec": 0.01,
            "authentication_mode": "mock", "api_key_used": False,
            "observed_fields": ["exit_code", "duration_sec"],
            "declared_fields": ["authentication_mode", "api_key_used"],
        }
    return call


def _at_planning(tmp_path: Path, *, greenfield: bool, order_payload=None):
    repo = _repo(tmp_path, greenfield)
    calls = []
    orchestrator = ResearchOrchestrator(
        artifact_root=tmp_path / "artifacts",
        provider_call=_planner(order_payload or _order(), calls),
    )
    started = orchestrator.start_session(
        str(repo), "research a tiny deterministic tool", new_project=greenfield
    )
    cycle = orchestrator.rsm.list_cycles(started["session_id"])[-1]
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.CROSS_CRITIQUE, "test")
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.SYNTHESIS, "test")
    orchestrator.rsm.record_stage_output(
        cycle["cycle_id"], ResearchState.SYNTHESIS, "OPENAI", "synthesis.json", "a" * 64,
        len(json.dumps(SYNTHESIS)), len(json.dumps(SYNTHESIS).encode()), SYNTHESIS,
    )
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.EXPERIMENT_PLANNING, "test")
    return repo, orchestrator, started["session_id"], cycle["cycle_id"], calls


def _factory(orchestrator: ResearchOrchestrator, created: list):
    config = {
        "head": {"provider": "mock"}, "reviewer": {"provider": "mock"},
        "verification": {"test_command": "python -m pytest tests/ -q"},
    }

    def factory(project_evidence, baseline_evidence):
        normal = TriadExperimentOrchestrator(
            config=config,
            head_agent=MockHead(), reviewer_agent=CodexReviewer(provider="mock"),
            greenfield_project_evidence=project_evidence if baseline_evidence else None,
            greenfield_baseline_evidence=baseline_evidence,
        )
        created.append(normal)
        return normal

    orchestrator.experiment_factory = factory


def _greenfield_builder(builder, order, worktree):
    builder.pre_tool_write_file("src/app.py", "def value():\n    return 2\n", worktree, order)
    builder.pre_tool_write_file(
        "tests/test_app.py", "from src.app import value\n\ndef test_value():\n    assert value() == 2\n",
        worktree, order,
    )
    return ["src/app.py", "tests/test_app.py"]


def _existing_builder(builder, order, worktree):
    builder.pre_tool_write_file(
        "src/app.py", "def value():\n    return 1\n\ndef next_value():\n    return 2\n", worktree, order
    )
    builder.pre_tool_write_file(
        "tests/test_next.py", "from src.app import next_value\n\ndef test_next():\n    assert next_value() == 2\n",
        worktree, order,
    )
    return ["src/app.py", "tests/test_next.py"]


def test_greenfield_experiment_uses_na_baseline_and_requires_post_build_pass(tmp_path):
    repo, orchestrator, session_id, cycle_id, calls = _at_planning(tmp_path, greenfield=True)
    planned = orchestrator.plan_experiment(session_id)
    assert planned["experiment"]["status"] == "PLANNED"
    created = []
    _factory(orchestrator, created)
    result = orchestrator.execute_experiment(session_id, builder_fn=_greenfield_builder)
    assert result["current_stage"] == ResearchState.RESULT_INTERPRETATION
    assert result["experiment"]["normal_final_state"] == State.PASS
    normal = created[0]
    run_id = result["experiment"]["normal_run_id"]
    baseline = normal._load_json(run_id, "baseline_verification.json")
    final = normal._load_json(run_id, "final_evidence.json")
    assert baseline["baseline_state"] == "NOT_APPLICABLE_GREENFIELD"
    assert baseline["passed"] is None and baseline["test_counts"] is None
    assert final["baseline"] == "NOT_APPLICABLE_GREENFIELD"
    assert final["verification"] == "PASS" and final["regression"] == "PASS"
    assert not (repo / "src").exists(), "main checkout must remain untouched"
    assert len(calls) == 1


def test_existing_project_delegates_to_normal_baseline(tmp_path):
    repo, orchestrator, session_id, cycle_id, _ = _at_planning(tmp_path, greenfield=False)
    orchestrator.plan_experiment(session_id)
    created = []
    _factory(orchestrator, created)
    result = orchestrator.execute_experiment(session_id, builder_fn=_existing_builder)
    assert result["current_stage"] == ResearchState.RESULT_INTERPRETATION
    normal = created[0]
    baseline = normal._load_json(result["experiment"]["normal_run_id"], "baseline_verification.json")
    assert baseline["passed"] is True
    assert baseline.get("baseline_state") != "NOT_APPLICABLE_GREENFIELD"
    assert baseline["test_counts"]["passed"] == 1
    assert "next_value" not in (repo / "src" / "app.py").read_text(encoding="utf-8")


def test_greenfield_without_post_build_tests_cannot_pass(tmp_path):
    _, orchestrator, session_id, _, _ = _at_planning(tmp_path, greenfield=True)
    orchestrator.plan_experiment(session_id)
    created = []
    _factory(orchestrator, created)

    def no_tests(builder, order, worktree):
        builder.pre_tool_write_file("src/app.py", "def value():\n    return 2\n", worktree, order)
        return ["src/app.py"]

    result = orchestrator.execute_experiment(session_id, builder_fn=no_tests)
    assert result["current_stage"] == ResearchState.RESULT_INTERPRETATION
    assert result["experiment"]["normal_final_state"] == State.FAIL
    assert result["experiment"]["result"]["verification"]["passed"] is False
    assert "성공으로 처리하지 않았습니다" in result["message"]


def test_invalid_order_is_blocked_before_normal_pipeline_or_builder(tmp_path):
    bad = _order()
    bad["allowed_paths"] = ["../outside"]
    _, orchestrator, session_id, _, calls = _at_planning(tmp_path, greenfield=True, order_payload=bad)
    planned = orchestrator.plan_experiment(session_id)
    assert planned["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert planned["human_required"] is True
    assert orchestrator.rsm.list_cycles(session_id)[-1]["stage"] == ResearchState.HUMAN_REQUIRED
    assert len(calls) == 1


def test_scope_violation_in_normal_pipeline_fails_research_cycle(tmp_path):
    _, orchestrator, session_id, _, _ = _at_planning(tmp_path, greenfield=False)
    orchestrator.plan_experiment(session_id)
    created = []
    _factory(orchestrator, created)

    def rogue(builder, order, worktree):
        (Path(worktree) / "outside.py").write_text("unsafe", encoding="utf-8")
        return ["outside.py"]

    result = orchestrator.execute_experiment(session_id, builder_fn=rogue)
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert result["experiment"]["normal_final_state"] == State.HUMAN_REQUIRED
    assert result["experiment"]["result"]["scope_violations"] > 0


def test_missing_synthesis_blocks_planner_without_provider_call(tmp_path):
    repo = _repo(tmp_path, greenfield=True)
    calls = []
    orchestrator = ResearchOrchestrator(
        artifact_root=tmp_path / "artifacts", provider_call=_planner(_order(), calls)
    )
    started = orchestrator.start_session(str(repo), "new tool", new_project=True)
    cycle = orchestrator.rsm.list_cycles(started["session_id"])[-1]
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.CROSS_CRITIQUE, "test")
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.SYNTHESIS, "test")
    orchestrator.rsm.transition_cycle(cycle["cycle_id"], ResearchState.EXPERIMENT_PLANNING, "test")
    result = orchestrator.plan_experiment(started["session_id"])
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert calls == []


def test_planning_is_idempotent_and_does_not_consume_second_call(tmp_path):
    _, orchestrator, session_id, _, calls = _at_planning(tmp_path, greenfield=False)
    first = orchestrator.plan_experiment(session_id)
    second = orchestrator.plan_experiment(session_id)
    assert first["experiment"]["order_sha256"] == second["experiment"]["order_sha256"]
    assert len(calls) == 1


def test_adapter_inherits_all_normal_safety_stage_implementations():
    assert TriadExperimentOrchestrator.run_builder_stage is Orchestrator.run_builder_stage
    assert TriadExperimentOrchestrator.validate_post_build is Orchestrator.validate_post_build
    assert TriadExperimentOrchestrator.run_reviewer_with_failover is Orchestrator.run_reviewer_with_failover
    assert TriadExperimentOrchestrator.run_decision_with_failover is Orchestrator.run_decision_with_failover
    assert TriadExperimentOrchestrator.prepare_order_for_execution is Orchestrator.prepare_order_for_execution

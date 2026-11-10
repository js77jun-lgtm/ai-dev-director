"""V3.0-H opt-in real subscription-CLI E2E.

Run explicitly with:
    python -m pytest -p no:cacheprovider -q -m integration_cli tests/test_v300h_real_three_provider_e2e.py

The default regression deselects this file. No API key or SDK path is used.

V3.0-H's acceptance bar is the failure-learning loop, not a lucky first-cycle PASS: real Independent
Research -> Cross Critique -> Synthesis -> Experiment (real Builder/Verifier/Reviewer) -> Result
Interpretation -> Result Cross Critique -> Research Memory -> bounded Next Cycle, with every cycle run
for real by Codex + Claude + Antigravity in one Research Session, until MILESTONE_COMPLETE or a bounded
RESEARCH_STOPPED (cycle budget / no-progress) is reached. A Reviewer changes_required verdict on an
ordinary in-scope defect is expected, real research data - it must route to Result Interpretation, never
be silently forced to PASS and never immediately halt the whole session to HUMAN_REQUIRED. Any genuine
safety/infra halt (Scope Guard, protected path, P0 blocker, provider/config failure, quota/auth) must
still stop the session to HUMAN_REQUIRED immediately, exactly as before.
"""
import hashlib
import json
from pathlib import Path

import pytest

from core.db_path import SOURCE_ROOT
from core.env_utils import run_capture
from core.git_manager import GitManager
from core.greenfield import BASELINE_NOT_APPLICABLE_GREENFIELD
from core.research_orchestrator import ResearchOrchestrator
from core.research_runner import TriadResearchRunner
from core.research_state import (
    INTERPRETATION_CROSS_STAGE,
    INTERPRETATION_INDEPENDENT_STAGE,
    RESEARCH_MEMORY_STAGE,
    ResearchState,
)
from core.state_machine import StateMachine


pytestmark = pytest.mark.integration_cli

MAX_REAL_CYCLES = 2  # must equal the cycle_budget passed to start_session below


def _git(project: Path, *args):
    result = run_capture(["git", *args], cwd=project, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _make_empty_repo(project: Path):
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.name", "AI DEV DIRECTOR V3 E2E")
    _git(project, "config", "user.email", "v3-e2e@localhost")
    _git(project, "commit", "--allow-empty", "-m", "empty greenfield baseline")
    assert not [p for p in project.iterdir() if p.name != ".git"]


def _prompt_payload(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_v300h_real_three_provider_greenfield_cycle(tmp_path):
    project = tmp_path / "v3h_empty_project"
    _make_empty_repo(project)
    artifact_root = SOURCE_ROOT / "research" / "v3h-e2e"
    runner = TriadResearchRunner(timeout=600, no_output_timeout=240, heartbeat_interval=30)
    config = {
        "head": {"provider": "openai", "executable": None},
        "builder": {"provider": "antigravity", "model": "antigravity-cli", "executable": None},
        "reviewer": {"provider": "codex", "model": "codex-cli", "executable": None},
        "timeouts": {"head": 600, "builder": 900, "reviewer": 600, "no_progress": 240, "heartbeat": 30},
        "safety": {"max_files_changed": 12, "max_diff_lines": 700,
                   "approved_commands": ["python", "pytest", "git"]},
        "verification": {"build_command": None, "test_command": "python -m pytest -q", "lint_command": None},
    }
    orch = ResearchOrchestrator(artifact_root=artifact_root, research_runner=runner, experiment_config=config)
    challenge = (
        "새 빈 프로젝트에서 작은 오프라인 Python 연구 도구를 만들어라. JSON Lines 이벤트 스트림의 "
        "의미상 중복을 결정론적으로 식별하되, 어떤 정규화와 판정 전략이 가장 설명 가능하고 안전한지 "
        "세 연구자가 독립적으로 제안하고 실제 실험으로 검증한다. 구현 알고리즘은 미리 지정하지 않는다. "
        "외부 네트워크와 외부 패키지를 사용하지 않고 src/와 tests/ 안에서만 구현하며, pytest 경계 테스트로 "
        "정확성과 결정론을 증명하고 파일 삭제는 하지 않는다."
    )
    summary = orch.start_session(str(project), challenge, cycle_budget=MAX_REAL_CYCLES, new_project=True)
    sid = summary["session_id"]
    assert summary["readiness"] == "TRIAD_READY" and summary["human_required"] is False
    assert summary["project_type"] == "GREENFIELD"

    trajectory = []
    final_stage = None
    cycles_run = 0

    while True:
        cycles_run += 1
        assert cycles_run <= MAX_REAL_CYCLES, "must never exceed the configured cycle budget"

        summary = orch.run_through_synthesis(sid)
        assert not summary["human_required"], f"cycle {cycles_run} research/critique/synthesis: {summary}"
        assert summary["current_stage"] == ResearchState.EXPERIMENT_PLANNING

        plan = orch.plan_experiment(sid)
        assert not plan["human_required"] and plan["experiment"]["status"] == "PLANNED"

        exp_summary = orch.execute_experiment(sid)
        assert not exp_summary["human_required"], (
            f"cycle {cycles_run} experiment stopped at an unsafe HUMAN_REQUIRED (Scope Guard/protected-path/"
            f"provider/P0 halt, not an ordinary reviewable defect): {exp_summary}"
        )
        assert exp_summary["current_stage"] == ResearchState.RESULT_INTERPRETATION

        interp = orch.run_result_interpretation(sid)
        assert not interp["human_required"] and interp["interpretation_substage"] == INTERPRETATION_CROSS_STAGE
        cross = orch.run_interpretation_cross_critique(sid)
        assert not cross["human_required"] and cross["interpretation_substage"] == RESEARCH_MEMORY_STAGE
        mem = orch.consolidate_research_memory(sid)
        assert not mem["human_required"]

        cycle = orch.rsm.list_cycles(sid)[-1]
        cycle_id = cycle["cycle_id"]
        experiment = orch.rsm.get_experiment(cycle_id)
        result = experiment["result"]

        proposals = orch.rsm.get_stage_outputs(cycle_id, ResearchState.INDEPENDENT_RESEARCH)
        proposal_hashes = {row["domain"]: row["output_sha256"] for row in proposals}
        assert set(proposal_hashes) == {"OPENAI", "ANTHROPIC", "GOOGLE"}
        assert len(set(proposal_hashes.values())) == 3

        prompt_rows = orch.rsm.get_prompt_artifacts(cycle_id, ResearchState.INDEPENDENT_RESEARCH)
        assert len(prompt_rows) == 3
        for row in prompt_rows:
            prompt = _prompt_payload(row["artifact_path"])
            assert prompt["contamination_guard"] == "PASS"
            serialized = json.dumps(prompt, ensure_ascii=False, sort_keys=True)
            assert "peer_proposals" not in serialized and "other_proposals" not in serialized

        # STRICT: a Reviewer changes_required verdict must survive verbatim - never rewritten toward "pass".
        review = result.get("review") or {}
        if experiment["normal_final_state"] != "PASS":
            assert review.get("verdict") == "changes_required"
            assert experiment["status"] == "FAILED"
            assert not review.get("p0"), "a P0 finding must never be routed as an ordinary research failure"

        research_provenance = orch.rsm.get_call_provenance(cycle_id)
        successful = [p for p in research_provenance if p["outcome"] == "SUCCESS"]
        assert {p["domain"] for p in successful if p["stage"] == ResearchState.INDEPENDENT_RESEARCH} == {
            "OPENAI", "ANTHROPIC", "GOOGLE"
        }
        assert {p["domain"] for p in successful if p["stage"] == INTERPRETATION_INDEPENDENT_STAGE} == {
            "OPENAI", "ANTHROPIC", "GOOGLE"
        }
        for record in successful:
            assert record["executable"] and Path(record["executable"]).exists()
            assert record["version"] and record["version"] != "UNKNOWN"
            assert record["exit_code"] == 0 and record["duration_sec"] is not None
            assert record["authentication_mode"] == "subscription" and record["api_key_used"] == 0

        normal_provenance = (result.get("provenance") or {}).get("records") or []
        assert any(r.get("agent") == "BUILDER" and r.get("exit_code") == 0 for r in normal_provenance)
        assert all(r.get("api_key_used") is False for r in normal_provenance)
        assert all(r.get("authentication_mode") == "subscription" for r in normal_provenance)

        memory_rows = orch.rsm.list_research_memory(sid)
        memory_payload = memory_rows[-1]["payload"] if memory_rows else None

        trajectory.append({
            "cycle_number": cycle["cycle_number"], "cycle_id": cycle_id,
            "hypothesis_sha256": {row["domain"]: row["output_sha256"] for row in proposals},
            "normal_run_id": experiment.get("normal_run_id"),
            "normal_final_state": experiment["normal_final_state"],
            "experiment_status": experiment["status"],
            "reviewer_verdict": review.get("verdict"), "reviewer_p0": review.get("p0"),
            "reviewer_p1": review.get("p1"), "reviewer_p2": review.get("p2"),
            "verification_passed": (result.get("verification") or {}).get("passed"),
            "research_provenance": research_provenance, "normal_provenance": normal_provenance,
            "research_memory": memory_payload,
            "worktree_path": experiment.get("worktree_path"),
        })

        final_stage = mem["current_stage"]
        if final_stage == ResearchState.NEXT_CYCLE:
            started = orch.start_next_cycle(sid)
            assert not started["human_required"], f"start_next_cycle hit HUMAN_REQUIRED: {started}"
            continue
        break

    # The session must land on a deliberate bounded terminal state, never silently stop mid-cycle.
    assert final_stage in (ResearchState.MILESTONE_COMPLETE, ResearchState.RESEARCH_STOPPED), final_stage

    # If a second cycle ran, its independent-research prompts must carry only the compact memory fact from
    # cycle 1 (hypothesis/lesson/next_direction/...), never cycle 1's raw reviewer findings or provenance.
    if len(trajectory) > 1:
        cycle2_prompt_rows = orch.rsm.get_prompt_artifacts(trajectory[1]["cycle_id"], ResearchState.INDEPENDENT_RESEARCH)
        for row in cycle2_prompt_rows:
            prompt = _prompt_payload(row["artifact_path"])
            facts = prompt["prompt"]["sources"]["RESEARCH_MEMORY_FACTS"]
            assert len(facts) >= 1
            for fact in facts:
                assert set(fact) == {"hypothesis", "result", "lesson", "do_not_repeat", "open_questions",
                                      "next_direction", "evidence_sha256"}
            serialized = json.dumps(prompt, ensure_ascii=False)
            for p1_item in (trajectory[0]["reviewer_p1"] or []):
                assert p1_item not in serialized, "cycle 1's raw reviewer finding text leaked into cycle 2's prompt"

    baseline = orch.rsm.get_baseline_evidence(trajectory[0]["cycle_id"])
    assert baseline["baseline_state"] == BASELINE_NOT_APPLICABLE_GREENFIELD
    assert baseline["evidence"]["passed"] is None and baseline["evidence"]["test_counts"] is None

    evidence = {
        "schema_version": "3.0-H-multicycle", "session_id": sid, "cycle_budget": MAX_REAL_CYCLES,
        "cycles_run": len(trajectory), "challenge": challenge, "project_type": summary["project_type"],
        "greenfield_baseline": baseline, "contamination_count": 0,
        "final_research_stage": final_stage, "trajectory": trajectory,
    }
    artifact_root.mkdir(parents=True, exist_ok=True)
    evidence_path = artifact_root / "latest_evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    assert hashlib.sha256(evidence_path.read_bytes()).hexdigest()

    for cycle_evidence in trajectory:
        worktree_path = cycle_evidence.get("worktree_path")
        if not worktree_path:
            continue
        normal_run = cycle_evidence.get("normal_run_id")
        branch = (StateMachine().get_run(normal_run) or {}).get("branch_name")
        GitManager.remove_worktree(str(project), worktree_path, branch_name=branch, force=True)

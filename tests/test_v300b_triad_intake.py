"""V3.0-B: TRIAD session intake, readiness authorization, and independent-prompt isolation."""
import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from core.db_path import get_db_path
from core.process_runner import ProcessTreeRunner
from core.research_orchestrator import ResearchOrchestrator
from core.research_prompts import (
    IndependentResearchContaminationError,
    build_independent_research_prompt,
)
from core.research_state import ResearchState, ResearchStateMachine
from core.triad_provider import TRIAD_BLOCKED, TRIAD_DEGRADED, TRIAD_READY


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "model.py").write_text("VALUE = 1\n", encoding="utf-8")
    return project


def _orchestrator(tmp_path, pm):
    return ResearchOrchestrator(
        state_machine=ResearchStateMachine(), provider_manager=pm, artifact_root=tmp_path / "research"
    )


def test_1_ready_intake_persists_minimum_session_and_three_separate_prompts(tmp_path, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    before = len(ProcessTreeRunner.blocked_real_cli_calls)
    result = _orchestrator(tmp_path, pm).start_session(
        str(_project(tmp_path)), "Find a falsifiable architecture improvement", cycle_budget=4
    )

    assert result["mode"] == "TRIAD"
    assert result["readiness"] == TRIAD_READY
    assert result["participant_count"] == 3
    assert result["current_cycle"] == 1
    assert result["current_stage"] == ResearchState.INDEPENDENT_RESEARCH
    assert result["cycle_budget"] == 4
    assert result["execution_authorized"] is True and result["human_required"] is False
    assert result["user_challenge"] == "Find a falsifiable architecture improvement"
    assert result["created_at"]
    assert len(ProcessTreeRunner.blocked_real_cli_calls) == before  # prompt preparation never attempted a CLI

    artifacts = result["prompt_artifacts"]
    assert {a["provider_domain"] for a in artifacts} == {"OPENAI", "ANTHROPIC", "GOOGLE"}
    assert len({a["artifact_path"] for a in artifacts}) == 3
    assert all(Path(a["artifact_path"]).is_file() for a in artifacts)
    assert all(a["contamination_guard"] == "PASS" and len(a["prompt_sha256"]) == 64 for a in artifacts)

    session = ResearchStateMachine().get_session(result["session_id"])
    assert session["mode"] == "TRIAD" and session["current_cycle"] == 1
    assert session["current_stage"] == ResearchState.INDEPENDENT_RESEARCH


def test_2_degraded_intake_requires_explicit_human_decision_and_runs_nothing(tmp_path, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="UNAVAILABLE", GOOGLE="HEALTHY")
    result = _orchestrator(tmp_path, pm).start_session(str(_project(tmp_path)), "Moonshot")

    assert result["readiness"] == TRIAD_DEGRADED
    assert result["participant_count"] == 2
    assert result["degraded"] is True and result["degraded_approved"] is False
    assert result["missing_domains"] == ["ANTHROPIC"]
    assert result["human_required"] is True and result["execution_authorized"] is False
    assert result["current_stage"] == ResearchState.RESEARCH_INTAKE
    assert result["prompt_artifacts"] == []
    assert "현재 2/3 AI만 사용 가능합니다" in result["message"]


def test_3_explicit_degraded_approval_stays_truthfully_two_provider(tmp_path, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="UNAVAILABLE", GOOGLE="HEALTHY")
    orch = _orchestrator(tmp_path, pm)
    pending = orch.start_session(str(_project(tmp_path)), "Moonshot")
    approved = orch.approve_degraded_session(pending["session_id"], approved_by="tester")

    assert approved["readiness"] == TRIAD_DEGRADED
    assert approved["degraded"] is True and approved["degraded_approved"] is True
    assert approved["participant_count"] == 2 and approved["execution_authorized"] is True
    assert approved["current_stage"] == ResearchState.INDEPENDENT_RESEARCH
    assert {a["provider_domain"] for a in approved["prompt_artifacts"]} == {"OPENAI", "GOOGLE"}
    assert "2/3" in approved["message"]
    persisted = ResearchStateMachine().get_session(pending["session_id"])
    intake = json.loads(persisted["intake_evidence"])
    assert intake["state"] == TRIAD_DEGRADED and intake["degraded_approved_by"] == "tester"


def test_4_start_with_explicit_degraded_permission_is_recorded_not_silent(tmp_path, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="UNAVAILABLE")
    result = _orchestrator(tmp_path, pm).start_session(
        str(_project(tmp_path)), "Moonshot", allow_degraded=True
    )
    assert result["readiness"] == TRIAD_DEGRADED
    assert result["degraded"] is True and result["degraded_approved"] is True
    assert result["participant_count"] == 2
    assert result["missing_domains"] == ["GOOGLE"]
    assert result["execution_authorized"] is True


def test_5_blocked_intake_never_prepares_prompts_or_starts_research(tmp_path, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="UNAVAILABLE", GOOGLE="AUTH_FAILED")
    result = _orchestrator(tmp_path, pm).start_session(str(_project(tmp_path)), "Moonshot", allow_degraded=True)
    assert result["readiness"] == TRIAD_BLOCKED
    assert result["human_required"] is True and result["execution_authorized"] is False
    assert result["current_stage"] == ResearchState.RESEARCH_INTAKE
    assert result["prompt_artifacts"] == []
    assert result["participant_count"] == 1


def test_6_independent_prompt_api_structurally_excludes_peer_outputs():
    signature = inspect.signature(build_independent_research_prompt)
    assert not ({"proposal", "proposals", "peer_proposals", "critiques"} & set(signature.parameters))
    envelope = build_independent_research_prompt(
        "OPENAI", "challenge", {"project_path": "C:/project"},
        verified_evidence={"tests": {"passed": 3}},
    )
    assert envelope["source_manifest"] == [
        "USER_CHALLENGE", "PROJECT_CONTEXT", "VERIFIED_EVIDENCE", "RESEARCH_MEMORY_FACTS"
    ]
    assert "peer-secret-from-claude" not in envelope["prompt_text"]
    with pytest.raises(IndependentResearchContaminationError):
        build_independent_research_prompt(
            "OPENAI", "challenge", {}, verified_evidence={"peer_proposals": {"CLAUDE": "secret"}}
        )
    with pytest.raises(IndependentResearchContaminationError):
        build_independent_research_prompt(
            "GOOGLE", "challenge", {}, research_memory_facts=[{"critique": "not allowed yet"}]
        )


def test_7_prompt_db_metadata_matches_exact_file_payload(tmp_path, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    orch = _orchestrator(tmp_path, pm)
    result = orch.start_session(str(_project(tmp_path)), "한국어 문자를 포함한 연구 목표")
    cycle_id = ResearchStateMachine().list_cycles(result["session_id"])[0]["cycle_id"]
    rows = ResearchStateMachine().get_prompt_artifacts(cycle_id, ResearchState.INDEPENDENT_RESEARCH)
    assert len(rows) == 3
    for row in rows:
        payload = json.loads(Path(row["artifact_path"]).read_text(encoding="utf-8"))
        assert row["prompt_sha256"] == payload["prompt_sha256"]
        assert row["prompt_chars"] == payload["prompt_chars"]
        assert row["prompt_bytes"] == payload["prompt_bytes"]
        assert payload["prompt_bytes"] >= payload["prompt_chars"]


def test_8_cycle_budget_is_positive_bounded_and_current_stage_is_synchronized(tmp_path, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    project = _project(tmp_path)
    orch = _orchestrator(tmp_path, pm)
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            orch.start_session(str(project), "Moonshot", cycle_budget=bad)
    result = orch.start_session(str(project), "Moonshot", cycle_budget=1)
    rsm = ResearchStateMachine()
    session = rsm.get_session(result["session_id"])
    cycle = rsm.list_cycles(result["session_id"])[0]
    rsm.transition_cycle(cycle["cycle_id"], ResearchState.CROSS_CRITIQUE)
    assert rsm.get_session(result["session_id"])["current_stage"] == ResearchState.CROSS_CRITIQUE
    with pytest.raises(ValueError, match="cycle budget exceeded"):
        rsm.create_cycle(f"{result['session_id']}-C2", result["session_id"], 2)


def test_9_intake_never_mutates_normal_provider_current_role(tmp_path, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    pm.resolve_roles()
    with sqlite3.connect(str(get_db_path())) as conn:
        before = conn.execute("SELECT provider,current_role FROM provider_health ORDER BY provider").fetchall()
    _orchestrator(tmp_path, pm).start_session(str(_project(tmp_path)), "Moonshot")
    with sqlite3.connect(str(get_db_path())) as conn:
        after = conn.execute("SELECT provider,current_role FROM provider_health ORDER BY provider").fetchall()
    assert before == after


def test_10_session_ids_have_full_uuid_entropy(tmp_path, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    orch = _orchestrator(tmp_path, pm)
    ids = {orch.generate_session_id() for _ in range(10_000)}
    assert len(ids) == 10_000
    assert all(len(value.rsplit("-", 1)[-1]) == 32 for value in ids)

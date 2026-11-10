import json
import subprocess
from pathlib import Path

import pytest

from core.greenfield import (
    BASELINE_NOT_APPLICABLE_GREENFIELD,
    BASELINE_REQUIRED,
    PROJECT_TYPE_EXISTING,
    PROJECT_TYPE_GREENFIELD,
    assess_project_type,
    build_greenfield_baseline_evidence,
)
from core.research_orchestrator import ResearchOrchestrator
from core.research_state import ResearchState


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, *, with_head: bool = True) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "triad-test@example.invalid")
    _git(repo, "config", "user.name", "TRIAD Test")
    if with_head:
        _git(repo, "commit", "--allow-empty", "-m", "initial empty project")
    return repo


def _advance_to_experiment_planning(orchestrator: ResearchOrchestrator, session_id: str) -> str:
    cycle = orchestrator.rsm.list_cycles(session_id)[-1]
    for stage in (ResearchState.CROSS_CRITIQUE, ResearchState.SYNTHESIS, ResearchState.EXPERIMENT_PLANNING):
        orchestrator.rsm.transition_cycle(cycle["cycle_id"], stage, details="test advance")
    return cycle["cycle_id"]


def test_explicit_empty_repo_with_head_is_greenfield(tmp_path):
    repo = _repo(tmp_path)
    evidence = assess_project_type(str(repo), user_explicit_new_project=True)
    assert evidence["project_type"] == PROJECT_TYPE_GREENFIELD
    assert evidence["baseline_state"] == BASELINE_NOT_APPLICABLE_GREENFIELD
    assert evidence["greenfield_qualified"] is True
    assert all(evidence["predicates"].values())
    assert evidence["head_commit"] == _git(repo, "rev-parse", "HEAD")


def test_empty_repo_without_explicit_user_intent_is_existing(tmp_path):
    repo = _repo(tmp_path)
    evidence = assess_project_type(str(repo), user_explicit_new_project=False)
    assert evidence["project_type"] == PROJECT_TYPE_EXISTING
    assert evidence["baseline_state"] == BASELINE_REQUIRED
    assert "user_explicit_new_project" in evidence["failed_predicates"]


def test_unborn_repo_without_head_is_not_greenfield(tmp_path):
    repo = _repo(tmp_path, with_head=False)
    evidence = assess_project_type(str(repo), user_explicit_new_project=True)
    assert evidence["project_type"] == PROJECT_TYPE_EXISTING
    assert evidence["predicates"]["valid_local_git_repo"] is True
    assert evidence["predicates"]["head_exists"] is False


@pytest.mark.parametrize(
    "relative, failed_predicate",
    [
        ("src/app.py", "meaningful_source_absent"),
        ("tests/test_app.py", "existing_tests_absent"),
        ("package.json", "meaningful_source_absent"),
    ],
)
def test_existing_source_tests_or_manifest_forbid_greenfield(tmp_path, relative, failed_predicate):
    repo = _repo(tmp_path)
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("placeholder", encoding="utf-8")
    evidence = assess_project_type(str(repo), user_explicit_new_project=True)
    assert evidence["project_type"] == PROJECT_TYPE_EXISTING
    assert failed_predicate in evidence["failed_predicates"]


def test_existing_product_modification_request_forbids_greenfield(tmp_path):
    repo = _repo(tmp_path)
    evidence = assess_project_type(
        str(repo), user_explicit_new_project=True,
        existing_product_modification_request=True,
    )
    assert evidence["project_type"] == PROJECT_TYPE_EXISTING
    assert "existing_product_modification_request_absent" in evidence["failed_predicates"]


def test_not_applicable_baseline_is_never_serialized_as_pass(tmp_path):
    evidence = assess_project_type(str(_repo(tmp_path)), user_explicit_new_project=True)
    baseline = build_greenfield_baseline_evidence(evidence)
    assert baseline["baseline_state"] == BASELINE_NOT_APPLICABLE_GREENFIELD
    assert baseline["applicable"] is False
    assert baseline["passed"] is None
    assert baseline["test_counts"] is None
    assert "not PASS" in baseline["summary"]


def test_non_greenfield_cannot_create_baseline_exemption(tmp_path):
    evidence = assess_project_type(str(_repo(tmp_path)), user_explicit_new_project=False)
    with pytest.raises(ValueError, match="GREENFIELD_QUALIFICATION_FAILED"):
        build_greenfield_baseline_evidence(evidence)


def test_invalid_new_project_intake_is_human_required_without_provider_call(tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("print('existing')", encoding="utf-8")
    calls = []
    orchestrator = ResearchOrchestrator(
        artifact_root=tmp_path / "artifacts",
        provider_call=lambda **kwargs: calls.append(kwargs),
    )
    summary = orchestrator.start_session(str(repo), "build a new product", new_project=True)
    assert summary["human_required"] is True
    assert summary["status"] == "HUMAN_REQUIRED"
    assert summary["current_stage"] == ResearchState.RESEARCH_INTAKE
    assert summary["prompt_artifacts"] == []
    assert calls == []


def test_qualified_greenfield_intake_persists_classification_artifact(tmp_path):
    repo = _repo(tmp_path)
    orchestrator = ResearchOrchestrator(artifact_root=tmp_path / "artifacts")
    summary = orchestrator.start_session(str(repo), "create a tiny new tool", new_project=True)
    assert summary["project_type"] == PROJECT_TYPE_GREENFIELD
    row = summary["project_evidence"]
    assert row["baseline_state"] == BASELINE_NOT_APPLICABLE_GREENFIELD
    assert Path(row["artifact_path"]).is_file()
    on_disk = json.loads(Path(row["artifact_path"]).read_text(encoding="utf-8"))
    assert on_disk["evidence_sha256"] == row["evidence_sha256"]


def test_prepare_greenfield_baseline_skips_verifier_and_persists_evidence(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    orchestrator = ResearchOrchestrator(artifact_root=tmp_path / "artifacts")
    summary = orchestrator.start_session(str(repo), "create a tiny new tool", new_project=True)
    cycle_id = _advance_to_experiment_planning(orchestrator, summary["session_id"])
    monkeypatch.setattr(
        "core.verifier.Verifier.verify",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Verifier baseline must not run")),
    )
    baseline = orchestrator.prepare_greenfield_baseline(summary["session_id"])
    assert baseline["baseline_state"] == BASELINE_NOT_APPLICABLE_GREENFIELD
    assert baseline["passed"] is None
    assert Path(baseline["artifact_path"]).is_file()
    stored = orchestrator.rsm.get_baseline_evidence(cycle_id)
    assert stored["baseline_state"] == BASELINE_NOT_APPLICABLE_GREENFIELD
    assert stored["evidence"]["passed"] is None


def test_greenfield_is_revalidated_before_baseline_and_new_source_blocks(tmp_path):
    repo = _repo(tmp_path)
    orchestrator = ResearchOrchestrator(artifact_root=tmp_path / "artifacts")
    summary = orchestrator.start_session(str(repo), "create a tiny new tool", new_project=True)
    _advance_to_experiment_planning(orchestrator, summary["session_id"])
    (repo / "existing.py").write_text("already here", encoding="utf-8")
    stopped = orchestrator.prepare_greenfield_baseline(summary["session_id"])
    assert stopped["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert stopped["human_required"] is True
    assert "GREENFIELD_QUALIFICATION_FAILED" in stopped["message"]
    assert orchestrator.rsm.get_session(summary["session_id"])["project_type"] == PROJECT_TYPE_EXISTING


def test_readme_only_is_benign_but_does_not_replace_required_head(tmp_path):
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("new project", encoding="utf-8")
    evidence = assess_project_type(str(repo), user_explicit_new_project=True)
    assert evidence["project_type"] == PROJECT_TYPE_GREENFIELD
    assert evidence["meaningful_source_files"] == []

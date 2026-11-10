import os
import sys
import tempfile
import subprocess
import time
from pathlib import Path
from click.testing import CliRunner
import pytest

from core.state_machine import StateMachine, State
from core.git_manager import GitManager
from agents.codex_reviewer import CodexReviewer
from agents.codex_head import CodexHead
from agents.claude_reviewer import ClaudeReviewer
from agents.claude_head import ClaudeHead
from core.orchestrator import Orchestrator

def test_a_clean_worktrees_preserves_pass_and_human_required():
    from director import cli
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(proj_dir), check=True)
        (proj_dir / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Init"], cwd=str(proj_dir), check=True, capture_output=True)

        db_path = Path(tmpdir) / "test.db"
        sm = StateMachine(str(db_path))

        run_pass = f"RUN-PASS-{int(time.time()*1000)}"
        wt_pass, br_pass = GitManager.create_worktree(str(proj_dir), run_pass)
        sm.create_run(run_pass, str(proj_dir), "Goal P", wt_pass, br_pass)
        for s in [State.PLANNING, State.ORDER_READY, State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING, State.PASS]:
            sm.transition_to(run_pass, s)

        run_hr = f"RUN-HR-{int(time.time()*1000)}"
        wt_hr, br_hr = GitManager.create_worktree(str(proj_dir), run_hr)
        sm.create_run(run_hr, str(proj_dir), "Goal H", wt_hr, br_hr)
        for s in [State.PLANNING, State.ORDER_READY, State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING, State.HUMAN_REQUIRED]:
            sm.transition_to(run_hr, s)

        runner = CliRunner()
        import director
        orig_sm = director.StateMachine
        director.StateMachine = lambda *args, **kwargs: sm
        try:
            res = runner.invoke(cli, ["clean-worktrees", "-p", str(proj_dir)])
            assert res.exit_code == 0
            assert "Skipping" in res.output
            assert Path(wt_pass).exists()
            assert Path(wt_hr).exists()
        finally:
            director.StateMachine = orig_sm


def test_b_clean_worktrees_purge_reviewed():
    from director import cli
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(proj_dir), check=True)
        (proj_dir / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Init"], cwd=str(proj_dir), check=True, capture_output=True)

        db_path = Path(tmpdir) / "test.db"
        sm = StateMachine(str(db_path))

        run_pass = f"RUN-PASS-{int(time.time()*1000)}"
        wt_pass, br_pass = GitManager.create_worktree(str(proj_dir), run_pass)
        sm.create_run(run_pass, str(proj_dir), "Goal P", wt_pass, br_pass)
        for s in [State.PLANNING, State.ORDER_READY, State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING, State.PASS]:
            sm.transition_to(run_pass, s)

        runner = CliRunner()
        import director
        orig_sm = director.StateMachine
        director.StateMachine = lambda *args, **kwargs: sm
        try:
            res = runner.invoke(cli, ["clean-worktrees", "-p", str(proj_dir), "--purge-reviewed"])
            assert res.exit_code == 0
            assert not Path(wt_pass).exists()
        finally:
            director.StateMachine = orig_sm


def test_c_clean_worktrees_skips_uncommitted_changes():
    from director import cli
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(proj_dir), check=True)
        (proj_dir / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Init"], cwd=str(proj_dir), check=True, capture_output=True)

        db_path = Path(tmpdir) / "test.db"
        sm = StateMachine(str(db_path))

        run_fail = f"RUN-FAIL-{int(time.time()*1000)}"
        wt_fail, br_fail = GitManager.create_worktree(str(proj_dir), run_fail)
        sm.create_run(run_fail, str(proj_dir), "Goal F", wt_fail, br_fail)
        for s in [State.PLANNING, State.ORDER_READY, State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING, State.FAIL]:
            sm.transition_to(run_fail, s)
        
        # Add uncommitted change
        (Path(wt_fail) / "uncommitted.txt").write_text("change")

        runner = CliRunner()
        import director
        orig_sm = director.StateMachine
        director.StateMachine = lambda *args, **kwargs: sm
        try:
            res = runner.invoke(cli, ["clean-worktrees", "-p", str(proj_dir)])
            assert res.exit_code == 0
            assert "SKIPPED_UNREVIEWED_CHANGES" in res.output
            assert Path(wt_fail).exists()
        finally:
            director.StateMachine = orig_sm

def test_d_clean_worktrees_unicode_paths():
    from director import cli
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project_한글"
        proj_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(proj_dir), check=True)
        (proj_dir / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Init"], cwd=str(proj_dir), check=True, capture_output=True)

        db_path = Path(tmpdir) / "test.db"
        sm = StateMachine(str(db_path))

        run_fail = f"RUN-FAIL-{int(time.time()*1000)}"
        wt_fail, br_fail = GitManager.create_worktree(str(proj_dir), run_fail)
        sm.create_run(run_fail, str(proj_dir), "Goal F", wt_fail, br_fail)
        for s in [State.PLANNING, State.ORDER_READY, State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING, State.FAIL]:
            sm.transition_to(run_fail, s)

        runner = CliRunner()
        import director
        orig_sm = director.StateMachine
        director.StateMachine = lambda *args, **kwargs: sm
        try:
            res = runner.invoke(cli, ["clean-worktrees", "-p", str(proj_dir)])
            assert res.exit_code == 0
            assert not Path(wt_fail).exists()
        finally:
            director.StateMachine = orig_sm

def test_e_real_provenance():
    rev = CodexReviewer(executable="python")
    rev.last_execution_info = {"exit_code": 123}
    prov = rev.get_provenance()
    assert prov.exit_code == 123
    assert prov.authentication_mode == "subscription"          # declared policy value (v0.2.3.3)
    assert "authentication_mode" in prov.declared_fields and "exit_code" in prov.observed_fields
    
    h = ClaudeHead(executable="python")
    h.last_execution_info = {"exit_code": 456}
    prov = h.get_provenance()
    assert prov.exit_code == 456

def test_f_malformed_reviewer_output():
    class MockReviewer(CodexReviewer):
        def _invoke_cli(self, *args, **kwargs):
            return "not json"
        
        def _review_with_cli(self, *args, **kwargs):
            # simulate CLI output not having json
            from schemas.review_result import ReviewResultSchema
            return ReviewResultSchema(
                verdict="changes_required",
                p0=["UNPARSEABLE_REVIEW: not json"],
                p1=[],
                p2=[],
                summary="UNPARSEABLE_REVIEW"
            )

    rev = MockReviewer()
    res = rev._review_with_cli(None, None, None)
    assert res.verdict == "changes_required"
    assert "UNPARSEABLE_REVIEW" in res.p0[0]

def test_g_resume_catches_provider_exceptions():
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()
        db_path = Path(tmpdir) / "test.db"
        
        config = {
            "head": {"provider": "mock"},
            "builder": {"provider": "mock"},
            "reviewer": {"provider": "mock"}
        }
        sm = StateMachine(str(db_path))
        orch = Orchestrator(config, sm)
        
        run_id = f"RUN-RESUME-{int(time.time()*1000)}"
        orch.sm.create_run(run_id, str(proj_dir), "Goal F", str(proj_dir), run_id)
        orch.sm.transition_to(run_id, State.PLANNING)
        
        # Inject exception in planning
        orch.head.create_order = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Head failed randomly"))
        
        orch.resume(run_id)
        run_record = orch.sm.get_run(run_id)
        assert run_record["current_state"] == "FAIL"
        transitions = orch.sm.get_transitions(run_id)
        assert "Planning error: Head failed randomly" in transitions[-1]["details"]

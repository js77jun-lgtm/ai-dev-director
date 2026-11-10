import os
import sys
import json
import time
import psutil
import tempfile
import subprocess
import pytest
from pathlib import Path
from datetime import datetime

from core.state_machine import StateMachine, State
from core.provider_manager import ProviderManager, ProviderStatus, FailoverMode, ErrorCategory
from core.orchestrator import Orchestrator
from core.process_runner import ProcessTreeRunner, ProcessResult
from core.verifier import Verifier
from core.safety import SafetyPolicy
from schemas.order import OrderSchema
from schemas.builder_result import BuilderResultSchema
from schemas.review_result import ReviewResultSchema
from schemas.decision import DecisionSchema
from schemas.provenance import AgentProvenanceRecord
from agents.head import BaseHead
from agents.codex_head import CodexHead
from agents.claude_head import ClaudeHead
from agents.codex_reviewer import CodexReviewer
from agents.claude_reviewer import ClaudeReviewer
from agents.antigravity_builder import AntigravityBuilder


# =====================================================================
# TEST A: SAME-PROVIDER INDEPENDENCE GUARD
# =====================================================================

def test_a_same_provider_guard_blocks_auto_pass():
    """
    Audit Item 1: When HEAD and REVIEWER share the same provider (e.g. Claude HEAD + Claude REVIEWER),
    final automated PASS is strictly forbidden. It must demote to HUMAN_REQUIRED.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(proj_dir), check=True)

        src = proj_dir / "src"
        src.mkdir()
        (src / "calc.py").write_text("def add(a, b): return a + b\n")
        tests = proj_dir / "tests"
        tests.mkdir()
        (tests / "test_calc.py").write_text("from src.calc import add\ndef test_add(): assert add(1, 2) == 3\n")
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(proj_dir), check=True, capture_output=True)

        # Mock Claude Head with explicit provider="anthropic"
        class MockClaudeHead(BaseHead):
            provider = "anthropic"
            def create_order(self, goal, context, **kwargs):
                return OrderSchema(
                    goal=goal,
                    allowed_paths=["src/", "tests/"],
                    protected_paths=[".git/"],
                    acceptance_criteria=["Tests pass"],
                    max_files_changed=5,
                    max_diff_lines=100
                )
            def decide(self, order, b_res, verif, r_res, **kwargs):
                return DecisionSchema(decision="PASS", rationale="All looks good", next_steps=[])
            def chat(self, user_msg, context=None):
                return {"type": "QUESTION", "response": "mock"}
            def get_provenance(self, exit_code=0):
                return AgentProvenanceRecord(
                    agent="HEAD",
                    provider="anthropic",
                    runtime="claude-code-cli",
                    model="claude-code-cli",
                    executable="claude",
                    invocation_command="claude",
                    version="2.1.220",
                    exit_code=exit_code,
                    duration_sec=1.0,
                    authentication="subscription",
                    api_key_used=False,
                    timestamp=datetime.utcnow().isoformat()
                )

        # Mock Claude Reviewer with explicit provider="anthropic"
        class MockClaudeReviewer:
            provider = "anthropic"
            def review(self, order, diff, verif, context=None, worktree_path=None):
                return ReviewResultSchema(
                    verdict="pass",
                    p0=[],
                    p1=[],
                    p2=[],
                    summary="All clean, passing tests."
                )
            def chat(self, user_msg, context=None):
                return {"type": "QUESTION", "response": "mock"}
            def get_provenance(self, exit_code=0):
                return AgentProvenanceRecord(
                    agent="REVIEWER",
                    provider="anthropic",
                    runtime="claude-code-cli",
                    model="claude-code-cli",
                    executable="claude",
                    invocation_command="claude",
                    version="2.1.220",
                    exit_code=exit_code,
                    duration_sec=1.0,
                    authentication="subscription",
                    api_key_used=False,
                    timestamp=datetime.utcnow().isoformat()
                )

        cfg = {
            "head": {"provider": "claude"},
            "builder": {"provider": "antigravity"},
            "reviewer": {"provider": "claude"},
            "verification": {"test_command": "python -m pytest tests/test_calc.py"}
        }

        orch = Orchestrator(
            config=cfg,
            head_agent=MockClaudeHead(),
            reviewer_agent=MockClaudeReviewer()
        )

        def builder_fn(builder, order, wt_path):
            p = Path(wt_path) / "src" / "calc.py"
            p.write_text("def add(a, b): return a + b\ndef sub(a, b): return a - b\n")
            t = Path(wt_path) / "tests" / "test_calc.py"
            t.write_text("from src.calc import add, sub\ndef test_add(): assert add(1, 2) == 3\ndef test_sub(): assert sub(3, 1) == 2\n")
            return ["src/calc.py", "tests/test_calc.py"]

        summary = orch.run(str(proj_dir), "Add subtract", builder_fn=builder_fn)

        # Head preference was PASS, reviewer verdict was PASS, but both are 'anthropic'
        # Orchestrator Same-Provider Independence Guard MUST demote to HUMAN_REQUIRED
        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert summary["decision"]["decision"] == "HUMAN_REQUIRED"
        assert "Same-Provider Independence Guard" in summary["decision"]["rationale"]


# =====================================================================
# TEST B: REVIEWER FAIL-CLOSED ON EMPTY STDOUT
# =====================================================================

def test_b_reviewer_empty_stdout_fail_closed(monkeypatch):
    """
    Audit Item 2: Reviewer must never produce PASS when stdout is empty.
    Must return verdict='changes_required' with 'UNPARSEABLE_REVIEW' in p0.
    """
    order = OrderSchema(
        goal="Test empty",
        allowed_paths=["src/"],
        protected_paths=[".git/"],
        acceptance_criteria=["Tests pass"]
    )

    empty_res = ProcessResult(
        exit_code=0,
        stdout="",
        stderr="",
        timed_out=False,
        duration_sec=0.1,
        pid=123
    )

    monkeypatch.setattr(ProcessTreeRunner, "run", lambda *args, **kwargs: empty_res)

    # Test ClaudeReviewer
    cr = ClaudeReviewer()
    cr.executable = "dummy_claude"
    res_claude = cr._review_with_cli(order, "", {})
    assert res_claude.verdict == "changes_required"
    assert any("UNPARSEABLE_REVIEW" in err for err in res_claude.p0)

    # Test CodexReviewer
    cx = CodexReviewer()
    cx.executable = "dummy_codex"
    res_codex = cx._review_with_cli(order, "", {})
    assert res_codex.verdict == "changes_required"
    assert any("UNPARSEABLE_REVIEW" in err for err in res_codex.p0)


# =====================================================================
# TEST C: REVIEWER FAIL-CLOSED ON MALFORMED JSON (NO PASS HEURISTICS)
# =====================================================================

def test_c_reviewer_malformed_json_fail_closed(monkeypatch):
    """
    Audit Item 2: Reviewer must delete natural language PASS guessing heuristics.
    Malformed/non-JSON response must strictly fail-closed with 'changes_required'.
    """
    order = OrderSchema(
        goal="Test malformed",
        allowed_paths=["src/"],
        protected_paths=[".git/"],
        acceptance_criteria=["Tests pass"]
    )

    natural_language_good = "The changes look wonderful! No bugs found, clean implementation, all tests pass."

    cr = ClaudeReviewer()
    with pytest.raises(ValueError):
        cr._extract_json(natural_language_good)

    malformed_res = ProcessResult(
        exit_code=0,
        stdout=natural_language_good,
        stderr="",
        timed_out=False,
        duration_sec=0.1,
        pid=123
    )
    monkeypatch.setattr(ProcessTreeRunner, "run", lambda *args, **kwargs: malformed_res)

    res = cr._review_with_cli(order, "", {})
    assert res.verdict == "changes_required"
    assert any("UNPARSEABLE_REVIEW" in err for err in res.p0)

    cx = CodexReviewer()
    with pytest.raises(ValueError):
        cx._extract_json(natural_language_good)

    res_cx = cx._review_with_cli(order, "", {})
    assert res_cx.verdict == "changes_required"
    assert any("UNPARSEABLE_REVIEW" in err for err in res_cx.p0)


# =====================================================================
# TEST D: PROVIDER ERROR CLASSIFICATION & IDENTITY
# =====================================================================

def test_d_provider_error_classification_and_domain_mapping():
    """
    Audit Item 5: Error classification into QUOTA, AUTH, TIMEOUT, CONFIG_ERROR, etc.
    CONFIG_ERROR does not mark provider down.
    Claude failure records ANTHROPIC provider status, not OPENAI.
    """
    pm = ProviderManager()
    assert pm.classify_error("usage limit exceeded, purchase more credits") == ErrorCategory.QUOTA
    assert pm.classify_error("Authentication failed: invalid token") == ErrorCategory.AUTH
    assert pm.classify_error("Process timed out after 300 seconds") == ErrorCategory.TIMEOUT
    assert pm.classify_error("Not inside a trusted directory: .git not found") == ErrorCategory.CONFIG_ERROR

    # CONFIG_ERROR does NOT mark provider down
    status = pm.parse_cli_error("OPENAI", "Not inside a trusted directory")
    assert status == ProviderStatus.HEALTHY
    assert pm.get_status("OPENAI") == ProviderStatus.HEALTHY

    # Quota error DOES mark provider as QUOTA_EXHAUSTED
    status_q = pm.parse_cli_error("OPENAI", "Rate limit: usage limit reached")
    assert status_q == ProviderStatus.QUOTA_EXHAUSTED
    assert pm.get_status("OPENAI") == ProviderStatus.QUOTA_EXHAUSTED

    # In Orchestrator, Claude failure must map to ANTHROPIC
    orch = Orchestrator(config={})
    claude_head = ClaudeHead()
    assert orch._get_agent_domain(claude_head) == "ANTHROPIC"
    codex_head = CodexHead()
    assert orch._get_agent_domain(codex_head) == "OPENAI"
    builder = AntigravityBuilder()
    assert orch._get_agent_domain(builder) == "GOOGLE"


# =====================================================================
# TEST E: TWO PROVIDERS DOWN HALTS WITH HUMAN_REQUIRED IN ALL STAGES
# =====================================================================

def test_e_two_providers_down_halts_human_required():
    """
    Audit Item 4: If 2 providers are down in any stage, FailoverMode is STOP
    and Orchestrator halts with HUMAN_REQUIRED.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(proj_dir), check=True)

        (proj_dir / "src").mkdir()
        (proj_dir / "src" / "app.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(proj_dir), check=True, capture_output=True)

        # Stage 1: Planning with 2 providers already down
        orch = Orchestrator(config={"verification": {}})
        orch.pm.set_status("OPENAI", ProviderStatus.QUOTA_EXHAUSTED, "Quota exceeded")
        orch.pm.set_status("ANTHROPIC", ProviderStatus.UNAVAILABLE, "Claude offline")

        summary = orch.run(str(proj_dir), "Test goal")
        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert summary["failover_mode"] == FailoverMode.STOP


# =====================================================================
# TEST F: VERIFIER HANGING CHILD PROCESS FULL TREE KILL
# =====================================================================

def test_f_verifier_hanging_child_tree_kill():
    """
    Audit Item 10: Verifier uses ProcessTreeRunner (shell=False) and on timeout
    terminates the entire process tree (parent + child), leaving zero orphan processes.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        pid_file = tmp_path / "child.pid"

        # Python script that spawns a long-sleeping child process and writes child PID
        script = tmp_path / "hang_tree.py"
        script.write_text(f"""
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(r"{pid_file.as_posix()}").write_text(str(child.pid))
child.wait()
""", encoding="utf-8")

        # Run via Verifier with 2.0s timeout
        start = time.time()
        res = Verifier._execute_cmd(f"python {script}", tmp_path, timeout=2.0)
        elapsed = time.time() - start

        # Must time out in ~2 seconds (not 60 seconds!)
        assert elapsed < 10.0, f"Verifier failed to enforce timeout: took {elapsed:.1f}s"
        assert res["passed"] is False
        assert res["exit_code"] == -1
        assert "timed out after 2.0 seconds" in res["output"]

        # Ensure child process was terminated by taskkill tree kill
        if pid_file.exists():
            child_pid = int(pid_file.read_text().strip())
            time.sleep(0.5)
            assert not psutil.pid_exists(child_pid), f"Child process {child_pid} leaked after timeout!"


# =====================================================================
# TEST G: RESUME SAFETY GATE PARITY
# =====================================================================

def test_g1_resume_enforces_baseline_regression_gate():
    """
    Audit Item 8 & 11: resume() must enforce baseline test regression gate.
    If post test count < baseline test count, resume() must transition to HUMAN_REQUIRED.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(proj_dir), check=True)

        src = proj_dir / "src"
        src.mkdir()
        (src / "app.py").write_text("def a(): pass\n")
        tests = proj_dir / "tests"
        tests.mkdir()
        (tests / "test_app.py").write_text("def test_1(): pass\ndef test_2(): pass\n")
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(proj_dir), check=True, capture_output=True)

        run_id = f"RUN-TEST-REGRESS-{int(time.time()*1000)}"
        sm = StateMachine()
        sm.create_run(run_id, str(proj_dir), "Goal")

        from core.git_manager import GitManager
        wt_path, branch = GitManager.create_worktree(str(proj_dir), run_id)
        sm.update_run_worktree(run_id, wt_path, branch)

        # Transition cleanly along valid state path
        sm.transition_to(run_id, State.PLANNING)
        sm.transition_to(run_id, State.ORDER_READY)
        sm.transition_to(run_id, State.BUILDING)
        sm.transition_to(run_id, State.VERIFYING)

        orch = Orchestrator(config={
            "verification": {"test_command": "python -m pytest tests/"}
        }, state_machine=sm)

        # Baseline had 2 passed tests
        orch._save_json(run_id, "baseline_verification.json", {
            "passed": True,
            "test_counts": {"passed": 2, "failed": 0, "total": 2}
        })
        orch._save_json(run_id, "order.json", OrderSchema(
            goal="Goal",
            allowed_paths=["src/", "tests/"],
            protected_paths=[".git/"],
            acceptance_criteria=["Tests pass"],
            allowed_deletions=["tests/test_app.py"] # Even with allowed_deletions, regression gate must hold!
        ))

        # In worktree, delete 1 test so only 1 passes
        (Path(wt_path) / "tests" / "test_app.py").write_text("def test_1(): pass\n")

        summary = orch.resume(run_id)
        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert summary["scope_violations"] > 0


def test_g2_resume_enforces_same_provider_guard():
    """
    Audit Item 8 & 1: resume() must enforce the Same-Provider Independence Guard
    at DECIDING stage.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(proj_dir), check=True)

        (proj_dir / "src").mkdir()
        (proj_dir / "src" / "app.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(proj_dir), check=True, capture_output=True)

        run_id = f"RUN-TEST-SAMEPROV-{int(time.time()*1000)}"
        sm = StateMachine()
        sm.create_run(run_id, str(proj_dir), "Goal")

        from core.git_manager import GitManager
        wt_path, branch = GitManager.create_worktree(str(proj_dir), run_id)
        sm.update_run_worktree(run_id, wt_path, branch)

        # Transition cleanly along valid state path
        sm.transition_to(run_id, State.PLANNING)
        sm.transition_to(run_id, State.ORDER_READY)
        sm.transition_to(run_id, State.BUILDING)
        sm.transition_to(run_id, State.VERIFYING)
        sm.transition_to(run_id, State.REVIEWING)
        sm.transition_to(run_id, State.DECIDING)

        # Mock Claude Head & Reviewer (both anthropic)
        class MockClaudeHead(BaseHead):
            provider = "anthropic"
            def create_order(self, goal, context=None, **kwargs):
                return OrderSchema(goal=goal, allowed_paths=["src/"])
            def decide(self, *args, **kwargs):
                return DecisionSchema(decision="PASS", rationale="Approved by Claude", next_steps=[])
            def chat(self, user_msg, context=None):
                return {"type": "QUESTION", "response": "mock"}
            def get_provenance(self, exit_code=0):
                return AgentProvenanceRecord(
                    agent="HEAD",
                    provider="anthropic",
                    runtime="claude-code-cli",
                    model="claude-code-cli",
                    executable="claude",
                    invocation_command="claude",
                    version="2.1.220",
                    exit_code=exit_code,
                    duration_sec=1.0,
                    authentication="subscription",
                    api_key_used=False,
                    timestamp=datetime.utcnow().isoformat()
                )

        class MockClaudeReviewer:
            provider = "anthropic"

        orch = Orchestrator(
            config={},
            state_machine=sm,
            head_agent=MockClaudeHead(),
            reviewer_agent=MockClaudeReviewer()
        )

        orch._save_json(run_id, "order.json", OrderSchema(goal="Goal", allowed_paths=["src/"]))
        orch._save_json(run_id, "builder.json", {"status": "COMPLETED", "files_modified": [], "commands_run": [], "summary": "Built successfully"})
        orch._save_json(run_id, "baseline_verification.json", {"passed": True, "test_counts": {"passed": 0}})   # v0.2.3.5: late-state resume needs evidence
        orch._save_json(run_id, "verification.json", {"passed": True, "test_counts": {"passed": 0}})
        orch._save_json(run_id, "review.json", {"verdict": "pass", "p0": [], "p1": [], "p2": [], "summary": "Looks good"})

        summary = orch.resume(run_id)
        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert "Same-Provider Independence Guard" in summary["decision"]["rationale"]

import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
import pytest
from pathlib import Path
from datetime import datetime

from core.state_machine import StateMachine, State, TERMINAL_STATES
from core.provider_manager import ProviderManager, ProviderStatus, FailoverMode, ErrorCategory
from core.orchestrator import Orchestrator
from core.verifier import Verifier
from core.git_manager import GitManager
from schemas.order import OrderSchema
from schemas.builder_result import BuilderResultSchema
from schemas.review_result import ReviewResultSchema
from schemas.decision import DecisionSchema
from schemas.provenance import AgentProvenanceRecord
from agents.head import BaseHead
from agents.mock_head import MockHead


# =====================================================================
# TEST 1: NORMAL topology (GPT Head + Antigravity Builder + Codex Reviewer) -> PASS
# =====================================================================

def test_1_normal_topology_gpt_antigravity_codex_allows_pass():
    """
    TEST 1: Normal topology (GPT Head + Antigravity Builder + Codex Reviewer)
    must NOT be blocked from PASS simply because both Head and Reviewer are openai provider.
    Review independence is judged on runtime/role.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(proj_dir), check=True)

        (proj_dir / "src").mkdir()
        (proj_dir / "src" / "calc.py").write_text("def add(a, b): return a + b\n")
        (proj_dir / "tests").mkdir()
        (proj_dir / "tests" / "test_calc.py").write_text("from src.calc import add\ndef test_add(): assert add(1, 2) == 3\n")
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=str(proj_dir), check=True, capture_output=True)

        class MockGPTHead(BaseHead):
            provider = "openai"
            agent_family = "gpt"
            def create_order(self, goal, context, **kwargs):
                return OrderSchema(
                    goal=goal,
                    allowed_paths=["src/", "tests/"],
                    protected_paths=[".git/"],
                    acceptance_criteria=["Tests pass"],
                    max_files_changed=5,
                    max_diff_lines=100
                )
            def decide(self, *args, **kwargs):
                return DecisionSchema(decision="PASS", rationale="All tests pass and no regression", next_steps=[])
            def chat(self, user_msg, context=None):
                return {"type": "QUESTION", "response": "mock"}
            def get_provenance(self, exit_code=0):
                return AgentProvenanceRecord(
                    agent="HEAD", provider="openai", runtime="codex-cli", model="gpt-4o",
                    executable="codex", invocation_command="codex", version="1.0.0",
                    exit_code=exit_code, timestamp=datetime.utcnow().isoformat()
                )

        class MockCodexReviewer:
            provider = "openai"
            agent_family = "codex"
            def review(self, order, diff, verif, context=None, worktree_path=None):
                return ReviewResultSchema(verdict="pass", p0=[], p1=[], p2=[], summary="Clean review.")
            def chat(self, user_msg, context=None):
                return {"type": "QUESTION", "response": "mock"}
            def get_provenance(self, exit_code=0):
                return AgentProvenanceRecord(
                    agent="REVIEWER", provider="openai", runtime="codex-cli", model="codex-cli",
                    executable="codex", invocation_command="codex", version="1.0.0",
                    exit_code=exit_code, timestamp=datetime.utcnow().isoformat()
                )

        cfg = {
            "head": {"provider": "openai"},
            "builder": {"provider": "antigravity"},
            "reviewer": {"provider": "codex"},
            "verification": {"test_command": "python -m pytest tests/test_calc.py"}
        }

        orch = Orchestrator(
            config=cfg,
            head_agent=MockGPTHead(),
            reviewer_agent=MockCodexReviewer()
        )

        def custom_builder(builder, order, wt_path):
            (Path(wt_path) / "src" / "calc.py").write_text("def add(a, b): return a + b\ndef sub(a, b): return a - b\n")
            (Path(wt_path) / "tests" / "test_calc.py").write_text("from src.calc import add, sub\ndef test_add(): assert add(1, 2) == 3\ndef test_sub(): assert sub(3, 1) == 2\n")
            return ["src/calc.py", "tests/test_calc.py"]

        summary = orch.run(str(proj_dir), "Add sub", builder_fn=custom_builder)

        assert summary["failover_mode"] == FailoverMode.NORMAL
        assert summary["final_state"] == State.PASS
        assert summary["decision"]["decision"] == "PASS"


# =====================================================================
# TEST 2: DEGRADED failover (Claude Head + Claude Reviewer) -> HUMAN_REQUIRED
# =====================================================================

def test_2_degraded_failover_same_family_claude_demoted_to_human_required():
    """
    TEST 2: When DEGRADED failover assigns Claude to both HEAD and REVIEWER,
    automated PASS is forbidden -> demote to HUMAN_REQUIRED.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(proj_dir), check=True)

        (proj_dir / "src").mkdir()
        (proj_dir / "src" / "calc.py").write_text("def add(a, b): return a + b\n")
        (proj_dir / "tests").mkdir()
        (proj_dir / "tests" / "test_calc.py").write_text("from src.calc import add\ndef test_add(): assert add(1, 2) == 3\n")
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=str(proj_dir), check=True, capture_output=True)

        class MockClaudeHead(BaseHead):
            provider = "anthropic"
            agent_family = "claude"
            def create_order(self, goal, context, **kwargs):
                return OrderSchema(
                    goal=goal,
                    allowed_paths=["src/", "tests/"],
                    protected_paths=[".git/"],
                    acceptance_criteria=["Tests pass"],
                    max_files_changed=5,
                    max_diff_lines=100
                )
            def decide(self, *args, **kwargs):
                return DecisionSchema(decision="PASS", rationale="Claude head approves", next_steps=[])
            def chat(self, user_msg, context=None):
                return {"type": "QUESTION", "response": "mock"}
            def get_provenance(self, exit_code=0):
                return AgentProvenanceRecord(
                    agent="HEAD", provider="anthropic", runtime="claude-code-cli", model="claude-code",
                    executable="claude", invocation_command="claude", version="1.0.0",
                    exit_code=exit_code, timestamp=datetime.utcnow().isoformat()
                )

        class MockClaudeReviewer:
            provider = "anthropic"
            agent_family = "claude"
            def review(self, order, diff, verif, context=None, worktree_path=None):
                return ReviewResultSchema(verdict="pass", p0=[], p1=[], p2=[], summary="Claude review passed.")
            def chat(self, user_msg, context=None):
                return {"type": "QUESTION", "response": "mock"}
            def get_provenance(self, exit_code=0):
                return AgentProvenanceRecord(
                    agent="REVIEWER", provider="anthropic", runtime="claude-code-cli", model="claude-code",
                    executable="claude", invocation_command="claude", version="1.0.0",
                    exit_code=exit_code, timestamp=datetime.utcnow().isoformat()
                )

        cfg = {
            "head": {"provider": "anthropic"},
            "builder": {"provider": "antigravity"},
            "reviewer": {"provider": "anthropic"},
            "verification": {"test_command": "python -m pytest tests/test_calc.py"}
        }

        orch = Orchestrator(
            config=cfg,
            head_agent=MockClaudeHead(),
            reviewer_agent=MockClaudeReviewer()
        )

        def custom_builder(builder, order, wt_path):
            (Path(wt_path) / "src" / "calc.py").write_text("def add(a, b): return a + b\ndef sub(a, b): return a - b\n")
            (Path(wt_path) / "tests" / "test_calc.py").write_text("from src.calc import add, sub\ndef test_add(): assert add(1, 2) == 3\ndef test_sub(): assert sub(3, 1) == 2\n")
            return ["src/calc.py", "tests/test_calc.py"]

        summary = orch.run(str(proj_dir), "Add sub", builder_fn=custom_builder)

        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert "Same-Provider Independence Guard" in summary["decision"]["rationale"]


# =====================================================================
# TEST 3: Review critique text does NOT trigger provider down
# =====================================================================

def test_3_reviewer_critique_text_does_not_trigger_provider_down():
    """
    TEST 3: Reviewer critique text mentioning 'timeout handling is missing',
    'auth validation bug exists', 'rate limit handling should be added'
    must NOT be classified as provider failure or mark provider down.
    """
    pm = ProviderManager()
    assert pm.get_status("OPENAI") == ProviderStatus.HEALTHY

    critiques = [
        "timeout handling is missing in auth middleware",
        "auth validation bug exists in session handler",
        "rate limit handling should be added to api endpoints"
    ]

    for c in critiques:
        cat = pm.classify_error(c)
        assert cat in (ErrorCategory.UNKNOWN, ErrorCategory.EXECUTION_ERROR), f"Wrongly classified '{c}' as {cat}"
        status = pm.parse_cli_error("OPENAI", c)
        assert status == ProviderStatus.HEALTHY
        assert pm.get_status("OPENAI") == ProviderStatus.HEALTHY


# =====================================================================
# TEST 4: Real quota signal -> QUOTA_EXHAUSTED
# =====================================================================

def test_4_reviewer_cli_quota_exhausted():
    """
    TEST 4: Real quota signal in CLI stderr triggers QUOTA_EXHAUSTED.
    """
    pm = ProviderManager()
    status = pm.parse_cli_error("OPENAI", "Error code 429: usage limit exceeded, please purchase more credits")
    assert status == ProviderStatus.QUOTA_EXHAUSTED
    assert pm.get_status("OPENAI") == ProviderStatus.QUOTA_EXHAUSTED

    status2 = pm.parse_cli_error("ANTHROPIC", "Your quota exhausted for the current billing period")
    assert status2 == ProviderStatus.QUOTA_EXHAUSTED
    assert pm.get_status("ANTHROPIC") == ProviderStatus.QUOTA_EXHAUSTED


# =====================================================================
# TEST 5: CONFIG_ERROR halts with HUMAN_REQUIRED without failover
# =====================================================================

def test_5_trusted_directory_config_error_halts_without_failover():
    """
    TEST 5: 'Not inside a trusted directory' is classified as CONFIG_ERROR,
    keeps provider HEALTHY, and halts run with HUMAN_REQUIRED without failover.
    """
    pm = ProviderManager()
    err = "Not inside a trusted directory: .git directory not found"
    cat = pm.classify_error(err)
    assert cat == ErrorCategory.CONFIG_ERROR

    status = pm.parse_cli_error("OPENAI", err)
    assert status == ProviderStatus.HEALTHY
    assert pm.get_status("OPENAI") == ProviderStatus.HEALTHY

    orch = Orchestrator(config={})
    run_id = f"RUN-TEST-CONF-{int(time.time()*1000)}"
    orch.sm.create_run(run_id, "E:/fake/path", "Test goal")
    orch.sm.transition_to(run_id, State.PLANNING)

    import logging
    logger = logging.getLogger("test_logger")
    stop_summary = orch._handle_provider_failure("OPENAI", err, run_id, "PLANNING", logger, [])

    assert stop_summary is not None
    assert stop_summary["final_state"] == State.HUMAN_REQUIRED
    transitions = orch.sm.get_transitions(run_id)
    assert any("CONFIG_ERROR" in (t.get("details") or "") for t in transitions)
    assert pm.get_status("OPENAI") == ProviderStatus.HEALTHY


# =====================================================================
# TEST 6: pytest collection scope (only tests/ collected)
# =====================================================================

def test_6_pytest_collection_scope():
    """
    TEST 6: pytest --collect-only must collect ONLY files under tests/,
    excluding smoke_project, demo_project, scratch, etc.
    """
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        check=True
    )

    collected_lines = [line.strip() for line in res.stdout.splitlines() if "::" in line]
    assert len(collected_lines) >= 30, f"Expected at least 30 tests, got {len(collected_lines)}"

    for line in collected_lines:
        test_file = line.split("::")[0]
        norm = Path(test_file).as_posix()
        assert norm.startswith("tests/"), f"Collected test outside tests/: {line}"
        assert "smoke_project" not in norm
        assert "demo_project" not in norm
        assert "scratch" not in norm


# =====================================================================
# TEST 7: clean-worktrees real cleanup
# =====================================================================

def test_7_clean_worktrees_removes_terminal_preserves_active_and_branch():
    """
    TEST 7: clean-worktrees removes terminal worktrees, preserves active worktrees,
    preserves git branches, and supports --dry-run.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(proj_dir), check=True)

        (proj_dir / "README.md").write_text("# Test Repo\n")
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Init"], cwd=str(proj_dir), check=True, capture_output=True)

        db_path = Path(tmpdir) / "test.db"
        sm = StateMachine(str(db_path))

        # Create Run 1: Terminal state (PASS)
        run1 = f"RUN-TERM-{int(time.time()*1000)}"
        wt1_path, br1 = GitManager.create_worktree(str(proj_dir), run1)
        sm.create_run(run1, str(proj_dir), "Goal 1", wt1_path, br1)
        sm.transition_to(run1, State.PLANNING)
        sm.transition_to(run1, State.ORDER_READY)
        sm.transition_to(run1, State.BUILDING)
        sm.transition_to(run1, State.VERIFYING)
        sm.transition_to(run1, State.REVIEWING)
        sm.transition_to(run1, State.DECIDING)
        sm.transition_to(run1, State.PASS)

        # Create Run 2: Active state (BUILDING)
        run2 = f"RUN-ACTV-{int(time.time()*1000)}"
        wt2_path, br2 = GitManager.create_worktree(str(proj_dir), run2)
        sm.create_run(run2, str(proj_dir), "Goal 2", wt2_path, br2)
        sm.transition_to(run2, State.PLANNING)
        sm.transition_to(run2, State.ORDER_READY)
        sm.transition_to(run2, State.BUILDING)

        assert Path(wt1_path).exists()
        assert Path(wt2_path).exists()

        from click.testing import CliRunner
        import director

        runner = CliRunner()
        orig_sm = director.StateMachine
        director.StateMachine = lambda *args, **kwargs: sm
        try:
            # 1. Test --dry-run
            res_dry = runner.invoke(director.cli, ["clean-worktrees", "-p", str(proj_dir), "--dry-run", "--purge-reviewed"])
            assert res_dry.exit_code == 0
            assert "Would remove worktree" in res_dry.output
            assert Path(wt1_path).exists()
            assert Path(wt2_path).exists()

            # 2. Test actual clean
            res_clean = runner.invoke(director.cli, ["clean-worktrees", "-p", str(proj_dir), "--purge-reviewed"])
            assert res_clean.exit_code == 0
            assert "Removing worktree" in res_clean.output
            assert "Cleaned 1 terminal worktree" in res_clean.output

            # Terminal worktree deleted
            assert not Path(wt1_path).exists(), f"Terminal worktree {wt1_path} was not deleted"
            # Active worktree preserved
            assert Path(wt2_path).exists(), f"Active worktree {wt2_path} was incorrectly deleted"

            # Branches preserved
            branches = subprocess.run(["git", "branch", "--list"], cwd=str(proj_dir), capture_output=True, text=True).stdout
            assert br1 in branches, f"Branch {br1} should be preserved for audit"
            assert br2 in branches, f"Branch {br2} should be preserved"
        finally:
            director.StateMachine = orig_sm


# =====================================================================
# TEST 8: Verifier sanitizes API keys from child process env
# =====================================================================

def test_8_verifier_sanitizes_api_keys():
    """
    TEST 8: Verifier child process must never receive forbidden API keys.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        wt = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=str(wt), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(wt), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(wt), check=True, capture_output=True)
        (wt / "init.txt").write_text("init\n")
        subprocess.run(["git", "add", "."], cwd=str(wt), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(wt), check=True, capture_output=True)

        test_script = (
            "import os, sys\n"
            "forbidden = ['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY']\n"
            "leaked = [k for k in forbidden if k in os.environ]\n"
            "if leaked:\n"
            "    print(f'LEAKED: {leaked}')\n"
            "    sys.exit(1)\n"
            "print('ENV_CLEAN')\n"
            "sys.exit(0)\n"
        )
        (wt / "check_env.py").write_text(test_script, encoding="utf-8")

        os.environ["OPENAI_API_KEY"] = "sk-forbidden-openai"
        os.environ["ANTHROPIC_API_KEY"] = "sk-forbidden-anthropic"
        os.environ["GEMINI_API_KEY"] = "sk-forbidden-gemini"
        os.environ["GOOGLE_API_KEY"] = "sk-forbidden-google"

        try:
            cmd = f"{sys.executable} check_env.py"
            verif = Verifier.verify(str(wt), test_cmd=cmd)
            assert verif["passed"] is True, f"Verifier failed: {verif}"
            assert "ENV_CLEAN" in verif["test"]["output"]
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("GOOGLE_API_KEY", None)


# =====================================================================
# TEST 9: Verifier nested quotes command execution
# =====================================================================

def test_9_verifier_nested_quote_execution():
    """
    TEST 9: Verifier test_command with nested quotes like python -c \"print('tests ok')\"
    executes cleanly with passed=True and correct output.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        wt = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=str(wt), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(wt), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(wt), check=True, capture_output=True)
        (wt / "init.txt").write_text("init\n")
        subprocess.run(["git", "add", "."], cwd=str(wt), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(wt), check=True, capture_output=True)

        cmd = f'{sys.executable} -c "print(\'tests ok\')"'
        verif = Verifier.verify(str(wt), test_cmd=cmd)
        assert verif["passed"] is True, f"Verifier failed: {verif}"
        assert "tests ok" in verif["test"]["output"].strip()


# =====================================================================
# TEST 10: resume() parity & terminal state handling
# =====================================================================

def test_10_resume_parity_and_terminal_handling():
    """
    TEST 10: resume() on terminal states returns RESUME_NOT_ALLOWED without raising exception.
    Enforces baseline regression gate and Same-Provider Independence Guard on resume.
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

        db_path = Path(tmpdir) / "test.db"
        sm = StateMachine(str(db_path))

        # Part 1: Terminal states return RESUME_NOT_ALLOWED cleanly
        for term_state in [State.PASS, State.FAIL, State.HUMAN_REQUIRED, State.TIMEOUT]:
            run_id = f"RUN-TERM-{term_state}-{int(time.time()*1000)}"
            sm.create_run(run_id, str(proj_dir), "Goal")
            sm.transition_to(run_id, State.PLANNING)
            if term_state == State.PASS:
                sm.transition_to(run_id, State.ORDER_READY)
                sm.transition_to(run_id, State.BUILDING)
                sm.transition_to(run_id, State.VERIFYING)
                sm.transition_to(run_id, State.REVIEWING)
                sm.transition_to(run_id, State.DECIDING)
                sm.transition_to(run_id, State.PASS)
            else:
                sm.transition_to(run_id, term_state)

            orch = Orchestrator(config={}, state_machine=sm)
            res = orch.resume(run_id)
            assert res["status"] == "RESUME_NOT_ALLOWED"
            assert res["final_state"] == term_state
            assert "Cannot resume run" in res["error"]

        # Part 2: Resume enforces baseline regression gate
        run_reg = f"RUN-REG-{int(time.time()*1000)}"
        wt_path, br = GitManager.create_worktree(str(proj_dir), run_reg)
        sm.create_run(run_reg, str(proj_dir), "Goal", wt_path, br)
        sm.transition_to(run_reg, State.PLANNING)
        sm.transition_to(run_reg, State.ORDER_READY)
        sm.transition_to(run_reg, State.BUILDING)
        sm.transition_to(run_reg, State.VERIFYING)

        orch_reg = Orchestrator(config={}, state_machine=sm)
        orch_reg._save_json(run_reg, "order.json", OrderSchema(goal="Goal", allowed_paths=["src/"]))
        orch_reg._save_json(run_reg, "baseline_verification.json", {"passed": True, "test_counts": {"passed": 5}})
        res_reg = orch_reg.resume(run_reg)
        assert res_reg["final_state"] == State.HUMAN_REQUIRED
        transitions = sm.get_transitions(run_reg)
        assert any("Baseline test count decreased" in (t.get("details") or "") for t in transitions)

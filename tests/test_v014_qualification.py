import os
import json
import subprocess
import tempfile
import pytest
from pathlib import Path

from core.provider_manager import ProviderManager, ProviderStatus, FailoverMode
from core.orchestrator import Orchestrator
from core.state_machine import State
from core.safety import ScopeViolationError
from schemas.order import OrderSchema
from agents.antigravity_builder import AntigravityBuilder
from agents.mock_head import MockHead


# =====================================================================
# TEST A: PERMISSION ESCAPE TESTS
# =====================================================================

def test_a1_scoped_permission_rules_no_wildcards():
    """Verify that permission generation strictly denies by default with NO open wildcards."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wt = Path(tmpdir)
        order = OrderSchema(
            goal="Add feature",
            allowed_paths=["src/", "tests/"],
            protected_paths=[".git/", "config.yaml"],
            acceptance_criteria=["Tests pass"],
            max_files_changed=5,
            max_diff_lines=100
        )
        builder = AntigravityBuilder()
        perms = builder._setup_scoped_permissions(wt, order)

        # 1. No open wildcards in allow lists
        for item in perms["allow_writes"] + perms["allow_commands"]:
            assert item != "write_file(*)", "write_file(*) must be strictly forbidden"
            assert item != "command(*)", "command(*) must be strictly forbidden"
            assert item != "unsandboxed(*)", "unsandboxed(*) must be strictly forbidden"
            assert item != "read_file(*)", "read_file(*) must be strictly forbidden"

        # 2. Check explicit deny rules
        assert any(".git" in d for d in perms["deny_writes"])
        assert any("git push" in c for c in perms["deny_commands"])
        assert any("rm -rf" in c for c in perms["deny_commands"])


def test_a2_permission_guard_hook_blocks_escapes():
    """Verify that the permission_guard.py hook script actively intercepts and denies violations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wt = Path(tmpdir)
        order = OrderSchema(
            goal="Test guard",
            allowed_paths=["src/"],
            protected_paths=[".git/"],
            acceptance_criteria=["Pass"],
            max_files_changed=5,
            max_diff_lines=100
        )
        builder = AntigravityBuilder()
        builder._setup_scoped_permissions(wt, order)

        guard_script = wt / ".agents" / "permission_guard.py"
        assert guard_script.exists()

        def run_guard(tool_name: str, args: dict) -> dict:
            payload = json.dumps({"toolCall": {"name": tool_name, "args": args}})
            res = subprocess.run(
                ["python", str(guard_script)],
                input=payload,
                text=True,
                capture_output=True,
                timeout=5
            )
            assert res.returncode == 0
            return json.loads(res.stdout.strip())

        # Escape 1: Outside worktree write
        r1 = run_guard("write_to_file", {"TargetFile": "C:/temp/escape.txt"})
        assert r1["decision"] == "deny"
        assert "outside allowed worktree" in r1["reason"]

        # Escape 2: .git directory modification
        r2 = run_guard("write_to_file", {"TargetFile": str(wt / ".git" / "config")})
        assert r2["decision"] == "deny"

        # Escape 3: Forbidden command git push
        r3 = run_guard("run_command", {"CommandLine": "git push origin master"})
        assert r3["decision"] == "deny"
        assert "forbidden" in r3["reason"]

        # Escape 4: Forbidden destructive command rm -rf
        r4 = run_guard("run_command", {"CommandLine": "rm -rf /"})
        assert r4["decision"] == "deny"

        # Legitimate write inside worktree allowed_paths
        r5 = run_guard("write_to_file", {"TargetFile": str(wt / "src" / "calc.py")})
        assert r5["decision"] == "allow"


def test_a3_pre_tool_validation_blocks_unauthorized_actions():
    """Verify that builder pre-tool checks raise ScopeViolationError for escapes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wt = Path(tmpdir)
        order = OrderSchema(
            goal="Test pre-tool",
            allowed_paths=["src/"],
            protected_paths=[".git/"],
            acceptance_criteria=["Pass"],
            max_files_changed=5,
            max_diff_lines=100
        )
        builder = AntigravityBuilder(approved_commands=["python -m pytest", "git status"])

        # Write outside allowed_paths -> blocked
        with pytest.raises(ScopeViolationError):
            builder.pre_tool_write_file("outside.txt", "data", str(wt), order)

        # Write into protected path -> blocked
        with pytest.raises(ScopeViolationError):
            builder.pre_tool_write_file(".git/config", "data", str(wt), order)

        # Unapproved command -> blocked
        with pytest.raises(ScopeViolationError):
            builder.pre_tool_run_command("git push", str(wt), order)


# =====================================================================
# TEST B: SINGLE PROVIDER FAILURE & FAILOVER TESTS
# =====================================================================

def test_b1_single_provider_failure_resolves_degraded():
    """Verify that OpenAI quota exhaustion triggers DEGRADED mode and fails over to Claude."""
    pm = ProviderManager()
    pm.set_status("OPENAI", ProviderStatus.QUOTA_EXHAUSTED, "Monthly limit exceeded")
    pm.set_status("ANTHROPIC", ProviderStatus.HEALTHY)
    pm.set_status("GOOGLE", ProviderStatus.HEALTHY)

    assigned = pm.resolve_roles(requested_head="openai", requested_builder="antigravity", requested_reviewer="codex")

    assert assigned["can_proceed"] is True
    assert assigned["mode"] == FailoverMode.DEGRADED
    assert assigned["head_provider"] == "claude"
    assert assigned["reviewer_provider"] == "claude"
    assert assigned["builder_provider"] == "antigravity"


def test_b2_orchestrator_degraded_failover_execution():
    """Verify Orchestrator transitions to DEGRADED mode and executes without infinite wait."""
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()

        # Initialize clean git repository
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(proj_dir), check=True)

        (proj_dir / "src").mkdir()
        (proj_dir / "tests").mkdir()
        (proj_dir / "src" / "calc.py").write_text("def add(a, b): return a + b\n")
        (proj_dir / "tests" / "test_calc.py").write_text("from src.calc import add\ndef test_add(): assert add(1, 2) == 3\n")

        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(proj_dir), check=True, capture_output=True)

        # Mock Head that simulates an OpenAI quota failure initially, but Claude backup succeeds
        class MockFailingHead(MockHead):
            def __init__(self):
                super().__init__()
                self.provider = "openai"

            def create_order(self, goal, context):
                raise RuntimeError("usage limit exceeded on OpenAI")

        class MockClaudeHead(MockHead):
            def __init__(self):
                super().__init__()
                self.provider = "claude"

        class MockClaudeReviewer:
            def __init__(self, **kwargs):
                self.provider = "claude"

            def review(self, order, git_diff, verification, project_context=None, worktree_path=None):
                from schemas.review_result import ReviewResultSchema
                return ReviewResultSchema(verdict="pass", p0=[], p1=[], p2=[], summary="Claude review passed.")

            def get_provenance(self, exit_code=0):
                from datetime import datetime
                from schemas.provenance import AgentProvenanceRecord
                return AgentProvenanceRecord(
                    agent="REVIEWER",
                    provider="anthropic",
                    runtime="claude-cli",
                    model="claude-code",
                    executable="claude",
                    invocation_command="claude",
                    version="2.1.4",
                    exit_code=exit_code,
                    duration_sec=1.0,
                    authentication="subscription",
                    api_key_used=False,
                    timestamp=datetime.utcnow().isoformat()
                )

        cfg = {
            "head": {"provider": "openai"},
            "builder": {"provider": "antigravity"},
            "reviewer": {"provider": "codex"},
            "verification": {"test_command": "python -m pytest tests/test_calc.py"}
        }
        orch = Orchestrator(config=cfg, head_agent=MockFailingHead())
        # Provide MockClaude agents upon failover
        orch._init_head = lambda prov=None: MockClaudeHead() if prov in ("claude", "anthropic") else MockFailingHead()
        orch._init_reviewer = lambda prov=None: MockClaudeReviewer() if prov in ("claude", "anthropic") else orch._init_reviewer(prov)

        # Custom builder function to simulate successful Antigravity modification
        def custom_builder(builder, order, wt_path):
            p = Path(wt_path) / "src" / "calc.py"
            p.write_text("def add(a, b): return a + b\ndef sub(a, b): return a - b\n")
            t = Path(wt_path) / "tests" / "test_calc.py"
            t.write_text("from src.calc import add, sub\ndef test_add(): assert add(1, 2) == 3\ndef test_sub(): assert sub(3, 1) == 2\n")
            return ["src/calc.py", "tests/test_calc.py"]

        summary = orch.run(str(proj_dir), "Add subtract function", builder_fn=custom_builder)

        # Verify failover was triggered and Same-Provider Guard demoted to HUMAN_REQUIRED
        assert summary["failover_mode"] == FailoverMode.DEGRADED
        assert summary["head_label"] == "CLAUDE"
        assert summary["reviewer_label"] == "CLAUDE"
        assert summary["final_state"] == State.HUMAN_REQUIRED


# =====================================================================
# TEST C: TWO PROVIDER FAILURE - AUTONOMOUS EXECUTION BLOCKED
# =====================================================================

def test_c1_two_providers_down_blocks_execution():
    """Verify that if both OpenAI and Anthropic are down, FailoverMode is STOP and can_proceed is False."""
    pm = ProviderManager()
    pm.set_status("OPENAI", ProviderStatus.UNAVAILABLE, "Codex missing or down")
    pm.set_status("ANTHROPIC", ProviderStatus.UNAVAILABLE, "Claude missing or down")
    pm.set_status("GOOGLE", ProviderStatus.HEALTHY)

    assigned = pm.resolve_roles(requested_head="openai", requested_builder="antigravity", requested_reviewer="codex")

    assert assigned["can_proceed"] is False
    assert assigned["mode"] == FailoverMode.STOP
    assert "Minimum 2 healthy providers required" in assigned["reason"]


def test_c2_orchestrator_two_providers_down_aborts_immediately():
    """Verify that Orchestrator immediately transitions to HUMAN_REQUIRED without running Antigravity alone."""
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "project"
        proj_dir.mkdir()

        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(proj_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(proj_dir), check=True)

        (proj_dir / "src").mkdir()
        (proj_dir / "src" / "app.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(proj_dir), check=True, capture_output=True)

        cfg = {
            "head": {"provider": "openai"},
            "builder": {"provider": "antigravity"},
            "reviewer": {"provider": "codex"},
            "verification": {"test_command": "python -m pytest"}
        }
        orch = Orchestrator(config=cfg)
        # Mark both OpenAI and Anthropic as unavailable
        orch.pm.set_status("OPENAI", ProviderStatus.UNAVAILABLE, "Down")
        orch.pm.set_status("ANTHROPIC", ProviderStatus.UNAVAILABLE, "Down")
        orch.pm.set_status("GOOGLE", ProviderStatus.HEALTHY)

        builder_called = []
        def probe_builder(builder, order, wt_path):
            builder_called.append(True)
            return []

        summary = orch.run(str(proj_dir), "Should not run", builder_fn=probe_builder)

        # Assertions: Antigravity must never run alone
        assert len(builder_called) == 0, "Antigravity must NEVER run autonomously alone!"
        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert summary["files_changed"] == 0
        assert summary["scope_violations"] > 0

import os
import sys
import time
import tempfile
import psutil
import pytest
from pathlib import Path

from core.process_runner import ProcessTreeRunner
from core.provider_manager import ProviderManager, ProviderStatus, FailoverMode
from core.orchestrator import Orchestrator
from core.state_machine import StateMachine, State
from agents.mock_head import MockHead

def test_hang_protection_tree_kill_and_failover():
    """
    Validates end-to-end Process Tree Kill & Failover under CLI hang conditions:
    1. A mock hanging CLI spawns a child (60s sleep) and parent sleeps 60s.
    2. ProcessTreeRunner executes with timeout=3s.
    3. Entire process tree is killed (parent and child dead, elapsed < 10s).
    4. Provider status transitions to TIMEOUT.
    5. ProviderManager resolves failover to DEGRADED mode.
    6. When second provider also fails, transitions to HUMAN_REQUIRED.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        hang_script = Path(tmpdir) / "hang_cli.py"
        hang_script.write_text(
            "import sys, time, subprocess\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], shell=False)\n"
            "print(f'CHILD_PID:{child.pid}', flush=True)\n"
            "time.sleep(60)\n",
            encoding="utf-8"
        )

        start_time = time.time()
        res = ProcessTreeRunner.run(
            argv=[sys.executable, str(hang_script)],
            timeout=3.0,
            heartbeat_interval=1.0
        )
        elapsed = time.time() - start_time
        parent_pid = res.pid

        child_pid = None
        for line in res.stdout.splitlines():
            if line.startswith("CHILD_PID:"):
                child_pid = int(line.split(":")[1].strip())
                break

        assert child_pid is not None
        time.sleep(0.5)

        parent_alive = psutil.pid_exists(parent_pid)
        child_alive = psutil.pid_exists(child_pid)

        assert res.timed_out is True
        assert elapsed < 10.0, f"Elapsed should be < 10s, got {elapsed}"
        assert parent_alive is False, f"Parent PID {parent_pid} still alive"
        assert child_alive is False, f"Child PID {child_pid} still alive"

        # Test ProviderManager status and Failover resolution
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS provider_health (provider TEXT PRIMARY KEY, cli TEXT, version TEXT, status TEXT NOT NULL, last_success DATETIME, last_failure DATETIME, failure_reason TEXT, current_role TEXT, last_latency_sec REAL, consecutive_failures INTEGER DEFAULT 0, usage_percent REAL, remaining REAL, reset_at DATETIME, last_checked DATETIME)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS provider_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL, usage_percent REAL, latency REAL, success INTEGER NOT NULL)''')
            conn.commit()
            conn.close()
            
            pm = ProviderManager(timeouts={"head": 3, "builder": 3, "reviewer": 3}, db_path=str(db_path))
            status = pm.parse_cli_error("OPENAI", f"TIMEOUT: {res.timeout_reason}")
            assert status == ProviderStatus.TIMEOUT
            assert pm.get_status("OPENAI") == ProviderStatus.TIMEOUT

            # With 1 provider down (OpenAI), mode must be DEGRADED
            assigned = pm.resolve_roles(requested_head="openai", requested_builder="antigravity", requested_reviewer="codex")
            assert assigned["mode"] == FailoverMode.DEGRADED
        assert assigned["head_provider"] == "claude"
        assert assigned["reviewer_provider"] == "claude"
        assert assigned["can_proceed"] is True

        # When Anthropic also encounters timeout
        status2 = pm.parse_cli_error("ANTHROPIC", "TIMEOUT: Claude Code CLI timed out after 3.0s")
        assert status2 == ProviderStatus.TIMEOUT
        assert pm.get_status("ANTHROPIC") == ProviderStatus.TIMEOUT

        # With 2 providers down, mode must be STOP (can_proceed == False) -> HUMAN_REQUIRED
        assigned2 = pm.resolve_roles(requested_head="openai", requested_builder="antigravity", requested_reviewer="codex")
        assert assigned2["mode"] == FailoverMode.STOP
        assert assigned2["can_proceed"] is False
        assert "Multiple providers down" in assigned2["reason"]

def test_orchestrator_hang_triggers_degraded_and_timeout_state():
    """
    Tests Orchestrator handling of agent timeouts during planning and execution.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir) / "test_proj"
        proj_dir.mkdir()
        src_dir = proj_dir / "src"
        src_dir.mkdir()
        (src_dir / "app.py").write_text("def hello(): return 'world'\n", encoding="utf-8")
        
        import subprocess
        subprocess.run(["git", "init"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=str(proj_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(proj_dir), check=True, capture_output=True)

        config = {
            "head": {"provider": "openai", "model": "gpt-4o"},
            "builder": {"provider": "antigravity", "model": "antigravity-cli"},
            "reviewer": {"provider": "codex", "model": "codex-cli"},
            "timeouts": {"head": 3, "builder": 6, "reviewer": 3, "no_progress": 2, "heartbeat": 1},
            "verification": {"build_command": None, "test_command": "python -c \"print('tests ok')\"", "lint_command": None}
        }

        db_path = Path(tmpdir) / "test.db"
        sm = StateMachine(str(db_path))

        class HangingHead(MockHead):
            def __init__(self):
                super().__init__()
                self.provider = "openai"
                self.executable = sys.executable

            def get_version(self):
                return "1.0.0"

            def create_order(self, goal, context):
                raise TimeoutError("TIMEOUT: ProcessTreeRunner hard limit exceeded after 3s")

        class MockClaudeHead(MockHead):
            def __init__(self):
                super().__init__()
                self.provider = "claude"
                self.executable = sys.executable

            def get_version(self):
                return "1.0.0"

        class MockClaudeReviewer:
            def __init__(self, **kwargs):
                self.provider = "anthropic"
                self.executable = sys.executable

            def get_version(self):
                return "1.0.0"

            def review(self, order, git_diff, verification, project_context=None, worktree_path=None):
                from schemas.review_result import ReviewResultSchema
                return ReviewResultSchema(verdict="pass", p0=[], p1=[], p2=[], summary="Claude review passed.")

            def get_provenance(self, exit_code=0):
                from datetime import datetime
                from schemas.provenance import AgentProvenanceRecord
                return AgentProvenanceRecord(
                    agent="REVIEWER",
                    provider="anthropic",
                    runtime="claude-code-cli",
                    model="claude-code-cli",
                    executable=sys.executable,
                    invocation_command="claude",
                    version="1.0.0",
                    exit_code=exit_code,
                    duration_sec=1.0,
                    authentication="subscription",
                    api_key_used=False,
                    timestamp=datetime.utcnow().isoformat()
                )

        hanging_head = HangingHead()
        orchestrator = Orchestrator(config=config, state_machine=sm, head_agent=hanging_head)
        orchestrator.pm = ProviderManager(timeouts=config["timeouts"], db_path=str(db_path))
        orchestrator._init_head = lambda prov=None: MockClaudeHead() if prov in ("claude", "anthropic") else hanging_head
        orchestrator._init_reviewer = lambda prov=None: MockClaudeReviewer() if prov in ("claude", "anthropic") else MockClaudeReviewer()

        def custom_builder(builder, order, wt_path):
            (Path(wt_path) / "src" / "app.py").write_text("def hello(): return 'world 2'\n", encoding="utf-8")
            return ["src/app.py"]

        summary = orchestrator.run(
            project_path=str(proj_dir),
            goal="Add feature",
            run_id="RUN-HANG-TEST-001",
            builder_fn=custom_builder
        )

        assert orchestrator.pm.get_status("OPENAI") == ProviderStatus.TIMEOUT
        assert summary["failover_mode"] == FailoverMode.DEGRADED
        assert summary["final_state"] in (State.PASS, State.HUMAN_REQUIRED, State.TIMEOUT)

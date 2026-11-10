import tempfile
import subprocess
from pathlib import Path
from core.orchestrator import Orchestrator
from core.state_machine import StateMachine, State
from core.safety import SafetyPolicy, ScopeViolationError
from schemas.order import OrderSchema
from agents.antigravity_builder import AntigravityBuilder

def _init_brake_repo(repo_path: Path):
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "BrakeTester"], cwd=str(repo_path), check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "brake@test.com"], cwd=str(repo_path), check=True, stdout=subprocess.PIPE)

    src = repo_path / "src"
    src.mkdir()
    (src / "app.py").write_text("def run(): pass\n", encoding="utf-8")

    tests = repo_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("def test_run(): pass\n", encoding="utf-8")

    (repo_path / "config.yaml").write_text("timeout: 30\n", encoding="utf-8")
    (repo_path / ".env").write_text("API_KEY=secret\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=str(repo_path), check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "commit", "-m", "Brake initial"], cwd=str(repo_path), check=True, stdout=subprocess.PIPE)

def test_brake_pre_tool_use_blocks_protected_path():
    """Verifies that PreToolUse hook blocks any write to protected_paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "repo"
        repo_path.mkdir()
        _init_brake_repo(repo_path)

        order = OrderSchema(
            goal="Modify config.yaml and .env to increase timeout",
            allowed_paths=["src/", "tests/"],
            protected_paths=["config.yaml", ".env", ".git/"],
            acceptance_criteria=["Update config.yaml"],
            max_files_changed=5,
            max_diff_lines=100
        )

        builder = AntigravityBuilder()

        # Builder attempts to write to protected path: config.yaml
        caught_violation = False
        try:
            builder.pre_tool_write_file("config.yaml", "timeout: 99\n", str(repo_path), order)
        except ScopeViolationError as e:
            caught_violation = True
            assert "protected_paths" in str(e) or "not in allowed_paths" in str(e)

        assert caught_violation is True

        # Main repo config.yaml must be unchanged
        assert (repo_path / "config.yaml").read_text(encoding="utf-8") == "timeout: 30\n"

def test_brake_pre_tool_use_blocks_unauthorized_dependency_file():
    """Verifies that PreToolUse hook blocks modifying dependency files outside allowed_paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "repo"
        repo_path.mkdir()
        _init_brake_repo(repo_path)

        order = OrderSchema(
            goal="Add requests dependency to requirements.txt",
            allowed_paths=["src/", "tests/"],
            protected_paths=["config.yaml", ".env"],
            acceptance_criteria=["Add requests"],
            max_files_changed=5,
            max_diff_lines=100
        )

        builder = AntigravityBuilder()

        caught_violation = False
        try:
            builder.pre_tool_write_file("requirements.txt", "requests==2.31.0\n", str(repo_path), order)
        except ScopeViolationError as e:
            caught_violation = True
            assert "not in allowed_paths" in str(e) or "dependency" in str(e)

        assert caught_violation is True
        assert not (repo_path / "requirements.txt").exists()

def test_brake_orchestrator_scope_guard_triggers_human_required():
    """
    Verifies that if unauthorized file modification bypasses builder
    (e.g. created outside pre_tool_write), Post-Build Scope Guard catches it
    and triggers HUMAN_REQUIRED.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "repo"
        repo_path.mkdir()
        _init_brake_repo(repo_path)

        db_path = Path(tmpdir) / "director.db"
        sm = StateMachine(str(db_path))

        config = {
            "head": {"provider": "mock"},
            "reviewer": {"provider": "mock"},
            "verification": {"test_command": "python -m pytest tests/"}
        }

        orchestrator = Orchestrator(config=config, state_machine=sm)

        # Custom rogue builder that directly writes unauthorized config
        def rogue_builder(builder: AntigravityBuilder, order, wt_path):
            (Path(wt_path) / "config.yaml").write_text("timeout: 999\n", encoding="utf-8")
            return ["config.yaml"]

        summary = orchestrator.run(
            project_path=str(repo_path),
            goal="Rogue config change",
            builder_fn=rogue_builder
        )

        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert summary["scope_violations"] > 0

        # Main repo MUST remain clean
        assert (repo_path / "config.yaml").read_text(encoding="utf-8") == "timeout: 30\n"
        status_res = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_path), capture_output=True, text=True)
        assert status_res.stdout.strip() == ""

import tempfile
import subprocess
from pathlib import Path
from core.orchestrator import Orchestrator
from core.state_machine import StateMachine, State
from agents.mock_head import MockHead
from agents.antigravity_builder import AntigravityBuilder
from agents.codex_reviewer import CodexReviewer

def _init_python_project(repo_path: Path):
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_path), check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "tester@test.com"], cwd=str(repo_path), check=True, stdout=subprocess.PIPE)

    src_dir = repo_path / "src"
    src_dir.mkdir()
    (src_dir / "math_ops.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8"
    )

    tests_dir = repo_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_math_ops.py").write_text(
        "from src.math_ops import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8"
    )

    subprocess.run(["git", "add", "."], cwd=str(repo_path), check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "commit", "-m", "Initial commit with add function"], cwd=str(repo_path), check=True, stdout=subprocess.PIPE)

def test_e2e_successful_pipeline():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "test_project"
        repo_path.mkdir()
        _init_python_project(repo_path)

        db_path = Path(tmpdir) / "director.db"
        sm = StateMachine(str(db_path))

        config = {
            "head": {"provider": "mock"},
            "reviewer": {"provider": "mock"},
            "verification": {
                "test_command": "python -m pytest tests/test_math_ops.py"
            }
        }

        orchestrator = Orchestrator(
            config=config,
            state_machine=sm,
            head_agent=MockHead(),
            reviewer_agent=CodexReviewer(provider="mock")
        )

        def mock_builder_fn(builder: AntigravityBuilder, order, wt_path):
            # Builder adds subtract function and test safely through PreToolUse hook
            math_code = (
                "def add(a, b):\n    return a + b\n\n"
                "def subtract(a, b):\n    return a - b\n"
            )
            builder.pre_tool_write_file("src/math_ops.py", math_code, wt_path, order)

            test_code = (
                "from src.math_ops import add, subtract\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_subtract():\n    assert subtract(5, 2) == 3\n"
            )
            builder.pre_tool_write_file("tests/test_math_ops.py", test_code, wt_path, order)
            return ["src/math_ops.py", "tests/test_math_ops.py"]

        summary = orchestrator.run(
            project_path=str(repo_path),
            goal="Add subtract function with unit tests",
            builder_fn=mock_builder_fn
        )

        # Assertions
        assert summary["final_state"] == State.PASS
        assert summary["files_changed"] == 2
        assert summary["scope_violations"] == 0

        # Check that main repo is untouched
        main_math = (repo_path / "src" / "math_ops.py").read_text(encoding="utf-8")
        assert "subtract" not in main_math

        # Check that runs/RUN-xxxxx has all 5 json artifacts
        run_id = summary["run_id"]
        run_dir = Path(orchestrator._get_run_dir(run_id))
        assert (run_dir / "order.json").exists()
        assert (run_dir / "builder.json").exists()
        assert (run_dir / "verification.json").exists()
        assert (run_dir / "review.json").exists()
        assert (run_dir / "decision.json").exists()
        assert (run_dir / "logs" / "run.log").exists()

def test_e2e_scope_violation_triggers_human_required():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "test_project"
        repo_path.mkdir()
        _init_python_project(repo_path)

        db_path = Path(tmpdir) / "director.db"
        sm = StateMachine(str(db_path))

        config = {
            "head": {"provider": "mock"},
            "reviewer": {"provider": "mock"},
            "verification": {"test_command": "pytest tests/"}
        }

        orchestrator = Orchestrator(
            config=config,
            state_machine=sm,
            head_agent=MockHead(),
            reviewer_agent=CodexReviewer(provider="mock")
        )

        def violating_builder_fn(builder: AntigravityBuilder, order, wt_path):
            # Intentionally modify an untracked file outside allowed_paths directly in wt_path
            # to test Post-Build Scope Guard
            unauthorized_file = Path(wt_path) / "unauthorized_script.py"
            unauthorized_file.write_text("# Hacked", encoding="utf-8")
            return ["unauthorized_script.py"]

        summary = orchestrator.run(
            project_path=str(repo_path),
            goal="Attempt unauthorized file creation",
            builder_fn=violating_builder_fn
        )

        # Must immediately halt as HUMAN_REQUIRED
        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert summary["scope_violations"] > 0

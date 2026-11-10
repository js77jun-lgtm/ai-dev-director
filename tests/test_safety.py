import tempfile
from pathlib import Path
from core.safety import SafetyPolicy
from schemas.order import OrderSchema

def test_safety_pre_tool_file_write():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        allowed_paths = ["src/", "tests/"]
        protected_paths = [".git/", "config.yaml"]

        # 1. Allowed file write
        ok, reason = SafetyPolicy.check_pre_tool_file_write(
            file_path="src/main.py",
            workspace_root=str(ws),
            allowed_paths=allowed_paths,
            protected_paths=protected_paths
        )
        assert ok is True
        assert reason is None

        # 2. File write outside allowed paths
        ok, reason = SafetyPolicy.check_pre_tool_file_write(
            file_path="scripts/deploy.sh",
            workspace_root=str(ws),
            allowed_paths=allowed_paths,
            protected_paths=protected_paths
        )
        assert ok is False
        assert "not in allowed_paths" in reason

        # 3. File write in protected path
        ok, reason = SafetyPolicy.check_pre_tool_file_write(
            file_path="config.yaml",
            workspace_root=str(ws),
            allowed_paths=["config.yaml", "src/"],
            protected_paths=protected_paths
        )
        assert ok is False
        assert "protected_paths" in reason

        # 4. Attempt write outside workspace root
        ok, reason = SafetyPolicy.check_pre_tool_file_write(
            file_path="../outside.txt",
            workspace_root=str(ws / "nested"),
            allowed_paths=["../outside.txt"],
            protected_paths=[]
        )
        assert ok is False
        assert "outside workspace" in reason

def test_safety_pre_tool_commands():
    # Forbidden git commands
    ok, reason = SafetyPolicy.check_pre_tool_command("git push origin main")
    assert ok is False
    assert "Forbidden" in reason

    ok, reason = SafetyPolicy.check_pre_tool_command("git reset --hard HEAD~1")
    assert ok is False

    # Forbidden bypass flag
    ok, reason = SafetyPolicy.check_pre_tool_command("antigravity run --bypass-permissions")
    assert ok is False

    # Allowed build command
    ok, reason = SafetyPolicy.check_pre_tool_command("pytest tests/", approved_commands=["pytest", "python"])
    assert ok is True

    # Unapproved command
    ok, reason = SafetyPolicy.check_pre_tool_command("docker run ubuntu", approved_commands=["pytest", "python"])
    assert ok is False

def test_safety_post_build_scope_guard():
    order = OrderSchema(
        goal="Test Goal",
        allowed_paths=["src/"],
        protected_paths=[".git/", "secret.key"],
        acceptance_criteria=["Clean code"],
        max_files_changed=2,
        max_diff_lines=50
    )

    # Valid diff
    numstat_ok = [(10, 5, "src/calculator.py")]
    valid, violations = SafetyPolicy.validate_scope_post_build(order, numstat_ok, [])
    assert valid is True
    assert len(violations) == 0

    # Exceeding max files
    numstat_exceed_files = [
        (5, 0, "src/a.py"),
        (5, 0, "src/b.py"),
        (5, 0, "src/c.py")
    ]
    valid, violations = SafetyPolicy.validate_scope_post_build(order, numstat_exceed_files, [])
    assert valid is False
    assert any("Max files changed exceeded" in v for v in violations)

    # Exceeding max diff lines
    numstat_exceed_lines = [(40, 20, "src/calculator.py")]
    valid, violations = SafetyPolicy.validate_scope_post_build(order, numstat_exceed_lines, [])
    assert valid is False
    assert any("Max diff lines exceeded" in v for v in violations)

    # Touching dependency file
    numstat_dep = [(1, 0, "src/calculator.py"), (2, 0, "requirements.txt")]
    valid, violations = SafetyPolicy.validate_scope_post_build(order, numstat_dep, [])
    assert valid is False
    assert any("Dependency configuration modified" in v for v in violations)

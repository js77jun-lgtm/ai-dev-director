"""V3.0-H headless Antigravity permission and no-op truthfulness regressions."""
import json
import subprocess
import sys
from pathlib import Path

from agents import antigravity_builder as ab
from agents.antigravity_builder import AntigravityBuilder
from core.process_runner import ProcessResult
from schemas.order import OrderSchema


def _git_project(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)


def test_windows_headless_builder_denies_all_terminal_and_uses_glob_free_paths(tmp_path):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    scoped = AntigravityBuilder(executable=sys.executable)._setup_scoped_permissions(
        wt, OrderSchema(goal="g", allowed_paths=["src/**"], protected_paths=["docs/**"])
    )
    assert scoped["allow_commands"] == []
    assert "command(*)" in scoped["deny_commands"]
    assert "unsandboxed(*)" in scoped["deny_commands"]
    assert not any("*" in rule for rule in scoped["allow_writes"] + scoped["deny_writes"])
    assert any(rule.endswith("/src)") for rule in scoped["allow_writes"])
    assert any(rule.endswith("/docs)") for rule in scoped["deny_writes"])


def test_exit_zero_without_task_changes_is_failed_and_settings_restore(
    isolate_provider_db, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = {"toolPermission": "request-review", "permissions": {"allow": [], "deny": []}}
    settings.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    observed = {}

    def fake_run(argv, **kwargs):
        active = json.loads(settings.read_text(encoding="utf-8"))
        if "--input-format" in argv:
            observed["argv"] = argv
            observed["stdin_data"] = kwargs.get("stdin_data")
        observed["allow"] = active["permissions"]["allow"]
        observed["deny"] = active["permissions"]["deny"]
        return ProcessResult(
            exit_code=0,
            stdout=json.dumps({"event": "result", "result": {
                "status": "SUCCESS", "response": "tool confirmation was soft-denied"
            }}) + "\n",
            stderr="",
            timed_out=False,
            duration_sec=0.1,
            pid=1,
        )

    monkeypatch.setattr(ab.ProcessTreeRunner, "run", staticmethod(fake_run))
    wt = tmp_path / "repo"
    _git_project(wt)
    result = AntigravityBuilder(executable=sys.executable).run(
        OrderSchema(goal="create implementation", context="Preserve integer millisecond precision", allowed_paths=["src/", "tests/"]), str(wt)
    )

    assert result.status == "FAILED"
    assert result.error.startswith("BUILDER_NO_CHANGES")
    assert "soft-denied" in result.error
    assert "-p" not in observed["argv"]
    assert observed["argv"][1:5] == [
        "--input-format", "stream-json", "--output-format", "stream-json"
    ]
    message = json.loads(observed["stdin_data"])
    assert message["event"] == "user"
    prompt = message["message"]["content"][0]["text"]
    assert "create implementation" in prompt
    assert "Preserve integer millisecond precision" in prompt
    assert "HARD CHANGE CAPS" in prompt and "QUALITY GATE BEFORE FINAL RESPONSE" in prompt
    assert not any(rule.startswith(("command(", "unsandboxed(")) for rule in observed["allow"])
    assert "command(*)" in observed["deny"]
    assert "unsandboxed(*)" in observed["deny"]
    file_rules = [rule for rule in observed["allow"] if rule.startswith(("read_file(", "write_file("))]
    assert file_rules and all(":/" in rule for rule in file_rules)
    assert not any("write_file(src" in rule or "write_file(tests" in rule for rule in file_rules)
    assert json.loads(settings.read_text(encoding="utf-8")) == original
    assert not settings.with_name(settings.name + ".director-original").exists()

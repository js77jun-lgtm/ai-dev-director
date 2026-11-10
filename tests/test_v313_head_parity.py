"""v3.1.3 — HEAD GPT(Codex) ↔ Claude 전환 대칭성.

2026-10-30 감사에서 두 HEAD 클래스는 프롬프트·시그니처·타임아웃이 동일했고, 유일한 차이는 CodexHead가
프로젝트를 고르지 않은 대화(cwd=None)에서도 프로세스 cwd가 git 저장소이길 요구한 것이었다.
이제 cwd가 없으면 ClaudeHead처럼 그대로 CLI를 호출한다. 프로젝트가 주어지면 git 검사는 그대로다.
"""
import os
from pathlib import Path

import pytest

from agents.codex_head import CodexHead
from agents.claude_head import ClaudeHead
from core.process_runner import ProcessResult


class _Capture:
    def __init__(self):
        self.calls = []

    def run(self, argv, cwd=None, env=None, timeout=300, no_output_timeout=None, heartbeat_interval=30, on_heartbeat=None, stdin_data=None):
        self.calls.append({"argv": argv, "cwd": cwd, "timeout": timeout, "no_output_timeout": no_output_timeout, "stdin": stdin_data})
        return ProcessResult(exit_code=0, stdout='{"type":"QUESTION","message":"ok"}', stderr="", timed_out=False, duration_sec=0.1, pid=1)


@pytest.fixture
def capture(monkeypatch, tmp_path):
    cap = _Capture()
    monkeypatch.setattr("agents.codex_head.ProcessTreeRunner", cap)
    monkeypatch.setattr("agents.claude_head.ProcessTreeRunner", cap)
    fake = tmp_path / "codex.exe"
    fake.write_text("")
    monkeypatch.setattr(CodexHead, "get_version", lambda self: "test")
    monkeypatch.setattr(ClaudeHead, "get_version", lambda self: "test")
    return cap, fake


def test_codex_head_without_project_does_not_require_git(capture, tmp_path, monkeypatch):
    cap, fake = capture
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)  # process cwd is not a git repository
    head = CodexHead(executable=str(fake), timeout=123, no_progress_timeout=45)
    out = head._invoke_cli("hello", cwd=None)
    assert "QUESTION" in out
    assert cap.calls[0]["cwd"] == str(plain.resolve()) and cap.calls[0]["stdin"] == "hello"
    assert cap.calls[0]["timeout"] == 123 and cap.calls[0]["no_output_timeout"] == 45


def test_codex_head_with_project_still_requires_git(capture, tmp_path):
    cap, fake = capture
    plain = tmp_path / "project-without-git"
    plain.mkdir()
    head = CodexHead(executable=str(fake))
    with pytest.raises(RuntimeError, match="CONFIG_ERROR"):
        head._invoke_cli("hello", cwd=str(plain))
    assert not cap.calls


def test_both_heads_forward_the_same_timeouts(capture, tmp_path):
    cap, fake = capture
    for cls in (CodexHead, ClaudeHead):
        cls(executable=str(fake), timeout=300, no_progress_timeout=180, heartbeat_interval=30)._invoke_cli("x", cwd=None)
    a, b = cap.calls[-2], cap.calls[-1]
    assert (a["timeout"], a["no_output_timeout"]) == (b["timeout"], b["no_output_timeout"]) == (300, 180)

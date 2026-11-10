"""v3.1.8 macOS/Linux compatibility (unverified on real macOS; these tests simulate the platform branches on any OS).

- kill_process_tree must not call `taskkill` off Windows (psutil walks the tree instead).
- ResultManager._open must use `open` on macOS and `xdg-open` elsewhere, and refuse cleanly when the file manager is absent.
- resolve_cli must consider the POSIX install folders after PATH.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import core.process_runner as pr
import core.result_manager as rm
import core.env_utils as eu


def test_kill_process_tree_skips_taskkill_off_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(pr.os, "name", "posix")
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    monkeypatch.setattr(pr.psutil, "pid_exists", lambda pid: False)
    assert pr.ProcessTreeRunner.kill_process_tree(424242) is True
    assert calls == []


def test_kill_process_tree_uses_taskkill_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(pr.os, "name", "nt")
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    alive = {"n": 1}

    def pid_exists(pid):
        alive["n"] -= 1
        return alive["n"] >= 0
    monkeypatch.setattr(pr.psutil, "pid_exists", pid_exists)
    pr.ProcessTreeRunner.kill_process_tree(424242)
    assert calls and calls[0][:2] == ["taskkill", "/PID"]


@pytest.mark.parametrize("os_name,platform,expected", [
    ("nt", "win32", "explorer.exe"),
    ("posix", "darwin", "open"),
    ("posix", "linux", "xdg-open"),
])
def test_open_folder_picks_platform_file_manager(monkeypatch, tmp_path, os_name, platform, expected):
    argv_seen = []
    monkeypatch.setattr(rm.os, "name", os_name)
    monkeypatch.setattr(rm.sys, "platform", platform)
    monkeypatch.setattr(rm.subprocess, "Popen", lambda argv, *a, **k: argv_seen.append(argv))
    out = rm.ResultManager._open(str(tmp_path))
    assert out["status"] == "ok"
    assert argv_seen == [[expected, str(tmp_path)]]


def test_open_folder_refuses_when_file_manager_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(rm.os, "name", "posix")
    monkeypatch.setattr(rm.sys, "platform", "linux")

    def boom(argv, *a, **k):
        raise FileNotFoundError("xdg-open")
    monkeypatch.setattr(rm.subprocess, "Popen", boom)
    out = rm.ResultManager._open(str(tmp_path))
    assert out["status"] == "refused" and "xdg-open" in out["reason"]


def test_resolve_cli_finds_posix_local_bin(monkeypatch, tmp_path):
    home = tmp_path / "home"
    binary = home / ".local" / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(eu.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(eu.shutil, "which", lambda name: None)
    monkeypatch.setenv("DIRECTOR_TESTING", "1")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    found = eu.resolve_cli("codex")
    assert found and Path(found) == binary.resolve()


def test_shell_launchers_ship_and_parse():
    root = Path(__file__).resolve().parent.parent
    for name in ("start-companion.sh", "stop-companion.sh"):
        text = (root / name).read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env sh")
        assert "companion_server.py" in text

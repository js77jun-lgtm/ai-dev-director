"""v3.1.6 — 작업 정지 (2026-11-03): a stop request really stops a running mission.

2026-11-02 23:53 the operator wrote '즉시 작업 중단' in HEAD chat; HEAD agreed in words and the mission kept building,
and resume-on-start continued it after the next restart. Now: STOPPED survives the running thread's saves, the
mission loop exits, resume skips it, the chat handler treats a short 'stop' message as a command.
"""
import json
from pathlib import Path
import pytest

from core.mission import MissionStore, MissionRunner, stop_request, missions_to_resume
import dashboard_server as d


def _mission(tmp_path, status="RUNNING"):
    store = MissionStore(tmp_path)
    m = {"mission_id": "MSN-TEST-1", "project_path": str(tmp_path / "proj"), "goal": "g", "status": status,
         "steps": [{"run_id": None}], "max_steps": 3, "max_consecutive_failures": 3, "owner_pid": None}
    store.save(m, force=True)
    return store, m


def test_stopped_survives_running_saves(tmp_path):
    store, m = _mission(tmp_path)
    stop_request("MSN-TEST-1", "test", store=store)
    assert store.load("MSN-TEST-1")["status"] == "STOPPED"
    m["status"] = "RUNNING"            # the thread's stale in-memory copy
    store.save(m)
    assert store.load("MSN-TEST-1")["status"] == "STOPPED"
    assert "정지" in store.load("MSN-TEST-1")["blocked_reason"]


def test_reopen_clears_stop(tmp_path):
    store, _ = _mission(tmp_path)
    stop_request("MSN-TEST-1", store=store)
    runner = MissionRunner(lambda: None, store=store)
    assert runner.reopen("MSN-TEST-1")["status"] == "RUNNING"
    assert store.load("MSN-TEST-1")["status"] == "RUNNING"


def test_run_loop_exits_after_stop(tmp_path):
    store, _ = _mission(tmp_path)
    runner = MissionRunner(lambda: None, store=store)
    calls = {"n": 0}
    def fake_advance(mission):
        calls["n"] += 1
        stop_request("MSN-TEST-1", "mid-step", store=store)      # the operator presses 작업 정지 while the step runs
        mission["status"] = "RUNNING"
        store.save(mission)
        return mission
    runner._advance = fake_advance
    out = runner.run("MSN-TEST-1")
    assert calls["n"] == 1 and out["status"] == "STOPPED" and out["owner_pid"] is None


def test_resume_skips_stopped(tmp_path):
    store, _ = _mission(tmp_path)
    assert [m["mission_id"] for m in missions_to_resume(store)] == ["MSN-TEST-1"]
    stop_request("MSN-TEST-1", store=store)
    assert missions_to_resume(store) == []


@pytest.mark.parametrize("msg,expected", [
    ("즉시 작업 중단", True), ("멈춰", True), ("stop", True), ("지금 하는 거 다 정지해", True), ("그만해", True),
    ("중단하지 마", False), ("멈추지 말고 계속 진행해", False), ("", False),
    ("41단계의 저장 로직이 중단되는 버그를 고치고 42단계 테스트 환경을 수정하라 ... 길게 이어지는 개발 지시 메시지 본문입니다 ... 정지 조건도 넣어라", False),
])
def test_stop_intent(msg, expected):
    assert d.is_stop_request(msg) is expected


def test_stop_missions_idle_when_nothing_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "MissionStore", lambda: MissionStore(tmp_path))
    monkeypatch.setattr(d, "active_session_id", lambda: "s")
    monkeypatch.setattr(d, "invalidate_status_cache", lambda: None)
    assert d.stop_missions("x")["status"] == "idle"

"""
v0.2.3.2 FINAL E2E BLOCKER PATCH — tests A..I (J = whole suite).
"""
import os
import sys
import json
import time
import socket
import threading
import subprocess
import tempfile
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import psutil

import dashboard_server
from dashboard_server import (DashboardServer, DashboardAlreadyRunning, is_request_origin_allowed,
                              handle_chat_message, GLOBAL_SESSION_ID)
from core.provider_manager import ProviderManager, ProviderStatus, FailoverMode
from core.state_machine import StateMachine, State
from core.chat_manager import ChatManager
from core.orchestrator import Orchestrator
from core.file_lock import FileLock, LockBusy
from core.db_path import get_lock_dir
from schemas.order import OrderSchema
from tests.test_v0231_chat_stabilization import make_git_project, inline_threads, HeadStub, _order


def _pm_with_probe_ok(monkeypatch):
    pm = ProviderManager()
    monkeypatch.setattr("core.provider_manager.resolve_cli", lambda name: sys.executable)
    monkeypatch.setattr(ProviderManager, "_get_cli_version", lambda self, b, arg="--version": "cli 1.0.0")
    return pm


def _age_failure(pm, domain, seconds):
    old = (datetime.utcnow() - timedelta(seconds=seconds)).isoformat()
    with pm._get_conn() as conn:
        # simulate elapsed time: the failure itself and its trial cooldown both lie in the past
        conn.execute("UPDATE provider_health SET last_failure=?, next_trial_at=? WHERE provider=?", (old, old, domain))
        conn.commit()


# ------------------------------------------------------------------ TEST A: probe success -> RECOVERING, not HEALTHY

def test_a_quota_exhausted_probe_success_is_recovering_not_healthy(isolate_provider_db, monkeypatch):
    pm = _pm_with_probe_ok(monkeypatch)
    pm.set_status("OPENAI", ProviderStatus.QUOTA_EXHAUSTED, "You've hit your usage limit")

    pm.check_health()                      # fresh failure: bounded, stays down
    assert pm.get_status("OPENAI") == ProviderStatus.QUOTA_EXHAUSTED

    _age_failure(pm, "OPENAI", pm.RECHECK_AFTER_SEC + 60)
    pm.check_health()
    assert pm.get_status("OPENAI") == ProviderStatus.RECOVERING
    row = pm._health_row("OPENAI")
    assert "unverified" in (row.get("failure_reason") or "")

    # forced recheck also yields RECOVERING at most, and repeated probes never escalate to HEALTHY
    pm.set_status("ANTHROPIC", ProviderStatus.TIMEOUT, "hung")
    pm.check_health(force=True)
    assert pm.get_status("ANTHROPIC") == ProviderStatus.RECOVERING
    pm.check_health(force=True)
    pm.check_health()
    assert pm.get_status("ANTHROPIC") == ProviderStatus.RECOVERING

    # failover: RECOVERING without a claimed trial does not count as healthy
    pm.set_status("GOOGLE", ProviderStatus.HEALTHY)
    mode, down = pm.get_failover_mode()
    assert mode == FailoverMode.STOP
    assert any("RECOVERING" in d for d in down)


# ------------------------------------------------------------------ TEST B: RECOVERING + real CLI success -> HEALTHY

def test_b_recovering_real_success_promotes_to_healthy(isolate_provider_db, monkeypatch):
    pm = _pm_with_probe_ok(monkeypatch)
    pm.set_status("OPENAI", ProviderStatus.QUOTA_EXHAUSTED, "limit")
    _age_failure(pm, "OPENAI", pm.RECHECK_AFTER_SEC + 60)
    pm.check_health()
    assert pm.get_status("OPENAI") == ProviderStatus.RECOVERING

    # a single trial claim makes the provider usable for one attempt
    assert pm.claim_trial("OPENAI") is True
    assert "OPENAI" in pm.usable_domains()
    # a second process (fresh manager) cannot claim a concurrent trial
    pm2 = ProviderManager()
    assert pm2.claim_trial("OPENAI") is False
    assert "OPENAI" not in pm2.usable_domains()

    # the orchestrator hook promotes only on a real exit-0 execution
    orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}})
    orch.pm = pm

    class RealLookingHead:
        provider = "openai"
        last_execution_info = {"exit_code": 0, "execution_duration_sec": 2.0}

    orch._record_provider_success(RealLookingHead())
    assert pm.get_status("OPENAI") == ProviderStatus.HEALTHY
    assert pm._health_row("OPENAI").get("trial_started_at") is None

    # mocks / non-zero exits never promote
    pm.set_status("ANTHROPIC", ProviderStatus.RECOVERING, "probe ok")

    class NonZero:
        provider = "anthropic"
        last_execution_info = {"exit_code": 1}

    orch._record_provider_success(NonZero())
    assert pm.get_status("ANTHROPIC") == ProviderStatus.RECOVERING

    class Mock:
        provider = "mock"
        last_execution_info = {"exit_code": 0}

    orch._record_provider_success(Mock())
    assert pm.get_status("ANTHROPIC") == ProviderStatus.RECOVERING


# ------------------------------------------------------------------ TEST C: RECOVERING + quota failure -> QUOTA_EXHAUSTED

def test_c_recovering_trial_quota_failure_returns_to_quota_exhausted(isolate_provider_db, monkeypatch):
    pm = _pm_with_probe_ok(monkeypatch)
    pm.set_status("OPENAI", ProviderStatus.RECOVERING, "probe ok")
    assert pm.claim_trial("OPENAI") is True

    status = pm.parse_cli_error("OPENAI", "ERROR: You've hit your usage limit. try again at 11:13 PM")
    assert status == ProviderStatus.QUOTA_EXHAUSTED
    assert pm.get_status("OPENAI") == ProviderStatus.QUOTA_EXHAUSTED
    assert "OPENAI" not in pm.active_trials
    row = pm._health_row("OPENAI")
    assert row.get("trial_started_at") is None
    assert row.get("reset_at") is not None                     # reset time captured from the CLI message

    # bounded: the trial cooldown blocks an immediate second trial after re-probing
    pm.set_status("OPENAI", ProviderStatus.RECOVERING, "probe ok")
    with pm._get_conn() as conn:
        conn.execute("UPDATE provider_health SET trial_started_at=? WHERE provider='OPENAI'",
                     (datetime.utcnow().isoformat(),))
        conn.commit()
    pm.active_trials.discard("OPENAI")
    assert pm.claim_trial("OPENAI") is False

    # auth failure path
    pm.set_status("ANTHROPIC", ProviderStatus.RECOVERING, "probe ok")
    assert pm.parse_cli_error("ANTHROPIC", "authentication failed: not logged in") == ProviderStatus.AUTH_FAILED


def test_c2_dashboard_chat_records_real_outcome(isolate_provider_db, monkeypatch):
    """A RECOVERING HEAD provider gets one trial via chat; success promotes, failure records the real state."""
    StateMachine(); cm = ChatManager()
    pm = _pm_with_probe_ok(monkeypatch)
    pm.set_status("OPENAI", ProviderStatus.RECOVERING, "probe ok")
    pm.set_status("ANTHROPIC", ProviderStatus.HEALTHY)
    pm.set_status("GOOGLE", ProviderStatus.HEALTHY)
    monkeypatch.setattr(ProviderManager, "check_health", lambda self, force=False: self.status_map.copy())
    dashboard_server._ACTIVE_PROJECTS.clear()

    head = HeadStub(_order(), exit_code=0)
    head.chat = lambda msg, ctx, hist: {"type": "QUESTION", "message": "fine"}
    monkeypatch.setattr(Orchestrator, "_init_head", lambda self, p=None: head)

    handle_chat_message("hello?")
    assert ProviderManager().get_status("OPENAI") == ProviderStatus.HEALTHY
    msgs = [m["content"] for m in cm.get_messages(GLOBAL_SESSION_ID)]
    assert any(c.startswith("RECOVERY TRIAL: OPENAI") for c in msgs)
    assert msgs[-1] == "fine"

    # failure path: quota error during the trial -> QUOTA_EXHAUSTED again (no fake HEALTHY)
    pm.set_status("OPENAI", ProviderStatus.RECOVERING, "probe ok")

    def boom(msg, ctx, hist):
        raise RuntimeError("Codex CLI execution failed (exit 1): You've hit your usage limit")
    head.chat = boom
    handle_chat_message("again?")
    assert ProviderManager().get_status("OPENAI") == ProviderStatus.QUOTA_EXHAUSTED


# ------------------------------------------------------------------ TEST D: run_id unique 1000 / 10000

def test_d_run_id_unique_same_second(isolate_provider_db):
    orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}})
    ids = [orch.generate_run_id() for _ in range(1000)]
    assert len(set(ids)) == 1000
    assert all(len(i.split("-")[2]) == 32 for i in ids)     # v0.2.3.3: full uuid4 hex (128-bit)
    ids10k = [orch.generate_run_id() for _ in range(10000)]
    assert len(set(ids10k)) == 10000


# ------------------------------------------------------------------ TEST E: second dashboard instance rejected

def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


def _listeners_on(port):
    return [c for c in psutil.net_connections(kind="tcp")
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port]


def test_e_dashboard_single_instance(isolate_provider_db):
    port = _free_port()
    first = DashboardServer(("127.0.0.1", port), dashboard_server.DashboardHandler)
    t = threading.Thread(target=first.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.2)
        with pytest.raises(DashboardAlreadyRunning):
            DashboardServer(("127.0.0.1", port), dashboard_server.DashboardHandler)
        # run() also refuses with the same error (no second LISTEN)
        with pytest.raises(DashboardAlreadyRunning):
            dashboard_server.run(port=port)
        listeners = _listeners_on(port)
        assert len(listeners) == 1, listeners
        assert listeners[0].laddr.ip == "127.0.0.1"
        assert DashboardServer.allow_reuse_address is False
    finally:
        first.shutdown(); first.server_close()


# ------------------------------------------------------------------ TEST F: cross-process Antigravity lock

CHILD_HOLDER = r'''
import sys, time, json
sys.path.insert(0, sys.argv[3])
from core.file_lock import FileLock
lock = FileLock(sys.argv[1], note="child")
ok = lock.try_acquire()
print("ACQUIRED" if ok else "BUSY", flush=True)
if ok:
    time.sleep(float(sys.argv[2]))
    lock.release()
    print("RELEASED", flush=True)
'''


def test_f_two_processes_compete_for_antigravity_lock(isolate_provider_db, tmp_path):
    lock_path = get_lock_dir() / "antigravity.lock"
    root = str(Path(__file__).resolve().parent.parent)
    script = tmp_path / "holder.py"
    script.write_text(CHILD_HOLDER, encoding="utf-8")

    child = subprocess.Popen([sys.executable, str(script), str(lock_path), "3", root],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    first_line = child.stdout.readline().strip()
    assert first_line == "ACQUIRED"

    # this process (second contender) must NOT get the lock while the child holds it
    mine = FileLock(lock_path, note="parent")
    assert mine.try_acquire() is False
    owner = mine.read_owner()
    assert owner and owner["pid"] == child.pid

    # builder surfaces BUILDER_BUSY instead of touching the global settings file
    from agents.antigravity_builder import AntigravityBuilder
    b = AntigravityBuilder(executable=sys.executable)
    res = b.run(OrderSchema(goal="g", allowed_paths=["src/"]), str(tmp_path))
    assert res.status == "FAILED" and res.error.startswith("BUILDER_BUSY")
    assert str(child.pid) in res.error

    out, _ = child.communicate(timeout=15)
    assert "RELEASED" in out
    assert mine.try_acquire() is True     # free again after the holder exits normally
    mine.release()
    assert not lock_path.exists()


def test_f2_orchestrator_builder_busy_ends_human_required(isolate_provider_db, tmp_path, monkeypatch):
    from agents.mock_head import MockHead
    from agents.codex_reviewer import CodexReviewer
    from agents.antigravity_builder import AntigravityBuilder
    lock_path = get_lock_dir() / "antigravity.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": os.getpid(), "created_at": time.time(), "note": "other run"}), encoding="utf-8")
    try:
        builder = AntigravityBuilder(executable=sys.executable)
        orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                            head_agent=MockHead(), reviewer_agent=CodexReviewer(provider="mock"), builder_agent=builder)
        with make_git_project() as proj:
            summary = orch.run(proj, "g")
        assert summary["final_state"] == State.HUMAN_REQUIRED
        details = [t["details"] for t in orch.sm.get_transitions(summary["run_id"])]
        assert any("BUILDER_BUSY" in (d or "") for d in details)
    finally:
        lock_path.unlink(missing_ok=True)


# ------------------------------------------------------------------ TEST G: stale lock recovery

def test_g_stale_lock_recovery(isolate_provider_db, tmp_path):
    lock_path = tmp_path / "locks" / "antigravity.lock"
    lock_path.parent.mkdir(parents=True)

    # 1) owner PID no longer exists -> stale -> recovered
    dead_pid = 4_000_000_000 % 60000 + 60000
    while psutil.pid_exists(dead_pid):
        dead_pid += 1
    lock_path.write_text(json.dumps({"pid": dead_pid, "created_at": time.time(), "note": "crashed"}), encoding="utf-8")
    lk = FileLock(lock_path, note="me")
    assert lk.is_stale() is True
    assert lk.try_acquire() is True
    assert lk.read_owner()["pid"] == os.getpid()
    lk.release()

    # 2) live owner but very old lock -> NOT stale (a live PID is never overridden by age)
    lock_path.write_text(json.dumps({"pid": os.getpid(), "token": "t-old", "created_at": time.time() - 100000, "note": "long build"}), encoding="utf-8")
    old = time.time() - 100000
    os.utime(lock_path, (old, old))
    lk2 = FileLock(lock_path, max_age_sec=10, note="me")
    assert lk2.is_stale() is False
    assert lk2.try_acquire() is False
    assert lock_path.exists() and json.loads(lock_path.read_text())["token"] == "t-old"
    lock_path.unlink()

    # 3) live owner, fresh lock -> NOT stale -> refused (context manager raises LockBusy)
    lock_path.write_text(json.dumps({"pid": os.getpid(), "created_at": time.time(), "note": "active"}), encoding="utf-8")
    lk3 = FileLock(lock_path, note="me")
    assert lk3.is_stale() is False
    assert lk3.try_acquire() is False
    with pytest.raises(LockBusy):
        with FileLock(lock_path):
            pass
    lock_path.unlink()

    # 4) exception inside the critical section still releases
    with pytest.raises(ValueError):
        with FileLock(lock_path):
            raise ValueError("boom")
    assert not lock_path.exists()


# ------------------------------------------------------------------ TEST H / I: CSRF

def test_h_i_origin_check_unit():
    port = 8080
    ok, _ = is_request_origin_allowed({"Host": "127.0.0.1:8080"}, port); assert ok
    ok, _ = is_request_origin_allowed({"Host": "localhost:8080", "Origin": "http://localhost:8080"}, port); assert ok
    ok, _ = is_request_origin_allowed({"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080"}, port); assert ok
    ok, r = is_request_origin_allowed({"Host": "127.0.0.1:8080", "Origin": "http://evil.example"}, port); assert not ok and "Origin" in r
    ok, r = is_request_origin_allowed({"Host": "127.0.0.1:8080", "Origin": "null"}, port); assert not ok
    ok, r = is_request_origin_allowed({"Host": "172.30.1.9:8080"}, port); assert not ok and "Host" in r
    ok, r = is_request_origin_allowed({}, port); assert not ok
    ok, r = is_request_origin_allowed({"Host": "127.0.0.1:9999"}, port); assert not ok


def test_h_i_origin_check_live_server(isolate_provider_db, monkeypatch):
    StateMachine(); ChatManager(); ProviderManager()
    calls = []
    monkeypatch.setattr(dashboard_server, "handle_chat_message", lambda msg: calls.append(msg))
    monkeypatch.setattr(dashboard_server, "set_project", lambda path: calls.append(("project", path)))
    monkeypatch.setattr(dashboard_server, "refresh_providers", lambda: calls.append("refresh"))
    port = _free_port()
    httpd = DashboardServer(("127.0.0.1", port), dashboard_server.DashboardHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True); t.start()

    def post(path, headers):
        data = urllib.parse.urlencode({"message": "hi", "path": "x"}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    urllib.request.install_opener(urllib.request.build_opener(NoRedirect))
    try:
        # TEST H: foreign Origin -> 403 on every state-changing endpoint, handlers never invoked
        for path in ("/chat", "/project", "/providers/refresh"):
            assert post(path, {"Origin": "http://evil.example"}) == 403
        assert post("/chat", {"Host": "evil.example:80"}) == 403
        assert calls == []
        # TEST I: localhost requests (no Origin, and matching Origin) -> accepted (303 redirect)
        assert post("/chat", {}) == 303
        assert post("/chat", {"Origin": f"http://127.0.0.1:{port}"}) == 303
        assert post("/project", {"Origin": f"http://localhost:{port}", "Host": f"localhost:{port}"}) == 303
        assert calls == ["hi", "hi", ("project", "x")]
    finally:
        urllib.request.install_opener(urllib.request.build_opener())
        httpd.shutdown(); httpd.server_close()

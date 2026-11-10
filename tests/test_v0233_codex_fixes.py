"""
v0.2.3.3 CODEX AUDIT BLOCKER FIX — tests A..N (O = whole suite).
"""
import os
import sys
import json
import time
import threading
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import psutil

import dashboard_server
from dashboard_server import handle_chat_message, GLOBAL_SESSION_ID
from core.safety import SafetyPolicy, ScopeViolationError
from core.orchestrator import Orchestrator
from core.state_machine import StateMachine, State
from core.chat_manager import ChatManager
from core.provider_manager import ProviderManager, ProviderStatus, FailoverMode
from core.file_lock import FileLock
from core.db_path import get_lock_dir
from core import env_utils
from schemas.order import OrderSchema
from schemas.review_result import ReviewResultSchema
from schemas.decision import DecisionSchema
from agents.mock_head import MockHead
from agents.codex_reviewer import CodexReviewer
from agents.antigravity_builder import AntigravityBuilder
from tests.test_v0231_chat_stabilization import make_git_project, inline_threads, HeadStub, _order


# ------------------------------------------------------------------ helpers

def _mock_orch(**cfg_over):
    cfg = {"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}}
    cfg.update(cfg_over)
    return Orchestrator(config=cfg, head_agent=MockHead(), reviewer_agent=CodexReviewer(provider="mock"))


def _run_attack(order, proj, sentinel, builder_calls):
    """Runs an ORDER through the real orchestrator with a builder that would clobber the sentinel."""
    def rogue(builder, o, wt):
        builder_calls.append(wt)
        sentinel.write_text("HACKED", encoding="utf-8")
        return []
    orch = _mock_orch()
    return orch.run(proj, order.goal, builder_fn=rogue, order=order)


# ------------------------------------------------------------------ TEST A: '../outside' fail-closed, Builder 0

@pytest.mark.parametrize("bad", ["../outside", "../../outside", "src/../../outside", "..\\outside", "./../x"])
def test_a_parent_traversal_fail_closed(isolate_provider_db, tmp_path, bad):
    sentinel = tmp_path / "outside_sentinel.txt"
    sentinel.write_text("ORIGINAL", encoding="utf-8")
    calls = []
    with make_git_project() as proj:
        order = OrderSchema(goal="attack", allowed_paths=[bad], protected_paths=[".git/"])
        summary = _run_attack(order, proj, sentinel, calls)
    assert summary["final_state"] == State.HUMAN_REQUIRED
    assert calls == []                                   # Builder never executed
    assert sentinel.read_text(encoding="utf-8") == "ORIGINAL"
    assert any("ORDER_PATH_VIOLATION" in (t["details"] or "") for t in StateMachine().get_transitions(summary["run_id"]))

    # protected_paths / allowed_deletions are validated too
    assert SafetyPolicy.validate_order_paths(OrderSchema(goal="g", allowed_paths=["src/"], protected_paths=["../x"]))
    assert SafetyPolicy.validate_order_paths(OrderSchema(goal="g", allowed_paths=["src/"], allowed_deletions=["../y"]))
    assert SafetyPolicy.validate_order_paths(OrderSchema(goal="g", allowed_paths=["src/"], allowed_deletions=["def:old_func"])) == []
    # entry policy (chat) also refuses
    assert any("INVALID_PATH" in r for r in SafetyPolicy.assess_order_for_auto_run(order))


# ------------------------------------------------------------------ TEST B: absolute / UNC / home paths fail-closed

@pytest.mark.parametrize("bad", [r"C:\temp", "C:/temp", "/tmp/foo", r"\\server\share", "//server/share", "~/x", r"D:\proj\src"])
def test_b_absolute_and_unc_paths_fail_closed(isolate_provider_db, tmp_path, bad):
    ok, reason = SafetyPolicy.is_repo_relative_path(bad)
    assert ok is False and reason
    sentinel = tmp_path / "abs_sentinel.txt"
    sentinel.write_text("ORIGINAL", encoding="utf-8")
    calls = []
    with make_git_project() as proj:
        order = OrderSchema(goal="attack", allowed_paths=[bad])
        summary = _run_attack(order, proj, sentinel, calls)
    assert summary["final_state"] == State.HUMAN_REQUIRED
    assert calls == [] and sentinel.read_text(encoding="utf-8") == "ORIGINAL"
    # good paths are still fine
    for good in ("src/", "tests/test_calc.py", "src/pkg/mod.py", "./src/x.py"):
        assert SafetyPolicy.is_repo_relative_path(good)[0] is True


# ------------------------------------------------------------------ TEST C: symlink / junction escape fail-closed

def _make_link(link: Path, target: Path) -> bool:
    """Creates a symlink (or a directory junction on Windows) pointing outside; returns False if unsupported."""
    try:
        os.symlink(str(target), str(link), target_is_directory=target.is_dir())
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt" and target.is_dir():
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        return r.returncode == 0
    return False


def test_c_symlink_escape_fail_closed(isolate_provider_db, tmp_path):
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("ORIGINAL", encoding="utf-8")

    with make_git_project() as proj:
        link = Path(proj) / "src" / "escape"
        if not _make_link(link, outside):
            pytest.skip("symlink/junction creation not permitted in this environment")

        # 1) pre-tool write through the link is refused (canonical containment)
        builder = AntigravityBuilder()
        order = OrderSchema(goal="g", allowed_paths=["src/"], protected_paths=[".git/"])
        with pytest.raises(ScopeViolationError):
            builder.pre_tool_write_file("src/escape/sentinel.txt", "HACKED", proj, order)
        assert sentinel.read_text(encoding="utf-8") == "ORIGINAL"
        assert SafetyPolicy.is_within_root(proj, Path(proj) / "src" / "escape" / "sentinel.txt") is False

        # 2) an ORDER whose allowed path resolves outside the checkout is refused before the Builder runs.
        #    (a junction is not part of the git tree, so the orchestrator is pointed at this checkout as its
        #    worktree to exercise the canonical containment check against a real escape)
        o2 = OrderSchema(goal="attack", allowed_paths=["src/escape/"])
        assert any("resolves outside worktree" in v for v in SafetyPolicy.validate_order_paths(o2, worktree_path=proj))
        from core import git_manager as gm
        import core.orchestrator as orch_mod
        real_create = gm.GitManager.create_worktree
        try:
            gm.GitManager.create_worktree = classmethod(lambda cls, project_path, run_id, base_ref="HEAD": (str(Path(project_path).resolve()), run_id))
            calls = []
            summary = _run_attack(o2, proj, sentinel, calls)
        finally:
            gm.GitManager.create_worktree = real_create
        assert summary["final_state"] == State.HUMAN_REQUIRED and calls == []
        assert sentinel.read_text(encoding="utf-8") == "ORIGINAL"
        assert any("ORDER_PATH_VIOLATION" in (t["details"] or "") for t in StateMachine().get_transitions(summary["run_id"]))

        # 3) post-build scope guard flags a changed path that escapes (defence in depth)
        wt = Path(proj)
        ok, viol = SafetyPolicy.validate_scope_post_build(
            OrderSchema(goal="g", allowed_paths=["src/"]), numstat=[(1, 0, "src/escape/sentinel.txt")],
            untracked_files=[], worktree_path=str(wt))
        assert ok is False and any("escapes worktree" in v for v in viol)

        # 4) hook guard: canonical containment (write via the link is denied even though the string starts with the worktree)
        builder._setup_scoped_permissions(wt, order)
        guard = wt / ".agents" / "permission_guard.py"
        payload = json.dumps({"toolCall": {"name": "write_to_file", "args": {"TargetFile": str(wt / "src" / "escape" / "sentinel.txt")}}})
        r = subprocess.run([sys.executable, str(guard)], input=payload, text=True, capture_output=True, timeout=10)
        assert json.loads(r.stdout.strip())["decision"] == "deny"
        payload_ok = json.dumps({"toolCall": {"name": "write_to_file", "args": {"TargetFile": str(wt / "src" / "ok.py")}}})
        r2 = subprocess.run([sys.executable, str(guard)], input=payload_ok, text=True, capture_output=True, timeout=10)
        assert json.loads(r2.stdout.strip())["decision"] == "allow"


# ------------------------------------------------------------------ TEST D: permission settings write failure -> Builder 0

def test_d_permission_setup_failure_blocks_builder(isolate_provider_db, tmp_path, monkeypatch):
    from core.process_runner import ProcessResult
    agy_calls = []

    def fake_run(argv, **kw):
        agy_calls.append(argv)
        return ProcessResult(exit_code=0, stdout='{"event":"result","result":{"status":"SUCCESS","response":"done"}}\n', stderr="", timed_out=False, duration_sec=0.1, pid=1)
    from agents import antigravity_builder as ab
    monkeypatch.setattr(ab.ProcessTreeRunner, "run", staticmethod(fake_run))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    # make the scoped settings write fail
    real_replace = os.replace
    def failing_replace(src, dst):
        if str(dst).endswith("settings.json") and str(src).endswith(".director-tmp"):
            raise PermissionError("simulated: cannot write settings.json")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", failing_replace)

    with make_git_project() as proj:
        builder = AntigravityBuilder(executable=sys.executable)
        res = builder.run(OrderSchema(goal="g", allowed_paths=["src/"]), proj)
    assert res.status == "FAILED" and res.error.startswith("PERMISSION_SETUP_FAILED")
    assert [a for a in agy_calls if "--sandbox" in a] == []   # Antigravity never launched (version probes excluded)
    assert not (tmp_path / ".gemini" / "antigravity-cli" / "settings.json.director-original").exists()
    assert ab._AGY_GLOBAL_LOCK.locked() is False

    # orchestrator maps it to HUMAN_REQUIRED
    monkeypatch.setattr(os, "replace", failing_replace)
    orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                        head_agent=MockHead(), reviewer_agent=CodexReviewer(provider="mock"),
                        builder_agent=AntigravityBuilder(executable=sys.executable))
    with make_git_project() as proj:
        summary = orch.run(proj, "g")
    assert summary["final_state"] == State.HUMAN_REQUIRED
    assert any("PERMISSION_SETUP_FAILED" in (t["details"] or "") for t in orch.sm.get_transitions(summary["run_id"]))
    assert [a for a in agy_calls if "--sandbox" in a] == []


def test_d2_settings_snapshot_restore_and_leftover_detection(isolate_provider_db, tmp_path, monkeypatch):
    from core.process_runner import ProcessResult
    from agents import antigravity_builder as ab
    seen = {}

    def fake_run(argv, **kw):
        sf = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
        seen["during"] = json.loads(sf.read_text(encoding="utf-8"))
        seen["backup_exists"] = (tmp_path / ".gemini" / "antigravity-cli" / "settings.json.director-original").exists()
        return ProcessResult(exit_code=0, stdout='{"event":"result","result":{"status":"SUCCESS","response":"done"}}\n', stderr="", timed_out=False, duration_sec=0.1, pid=1)
    monkeypatch.setattr(ab.ProcessTreeRunner, "run", staticmethod(fake_run))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    sf = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
    sf.parent.mkdir(parents=True)
    original = {"permissions": {"allow": ["command(git status*)"], "deny": []}, "user_pref": 1}
    sf.write_text(json.dumps(original), encoding="utf-8")

    with make_git_project() as proj:
        res = AntigravityBuilder(executable=sys.executable).run(OrderSchema(goal="g", allowed_paths=["src/"]), proj)
    assert res.status == "FAILED" and res.error.startswith("BUILDER_NO_CHANGES")
    assert "_director_scoped" in seen["during"] and seen["backup_exists"]
    restored = json.loads(sf.read_text(encoding="utf-8"))
    assert restored["user_pref"] == 1 and "_director_scoped" not in restored
    assert restored["permissions"]["allow"] == ["command(git status*)"]
    assert not sf.with_name(sf.name + ".director-original").exists()

    # leftover scoped settings from a crashed run are never mistaken for the original
    sf.write_text(json.dumps({"permissions": {"allow": ["write_file(/x/*)"], "deny": []}, "_director_scoped": {"pid": 1}}), encoding="utf-8")
    sf.with_name(sf.name + ".director-original").write_text(json.dumps(original), encoding="utf-8")
    with make_git_project() as proj:
        res2 = AntigravityBuilder(executable=sys.executable).run(OrderSchema(goal="g", allowed_paths=["src/"]), proj)
    assert res2.status == "FAILED" and res2.error.startswith("BUILDER_NO_CHANGES")
    assert json.loads(sf.read_text(encoding="utf-8"))["user_pref"] == 1


# ------------------------------------------------------------------ TEST E: reviewer changes_required + P2 only -> no PASS

def test_e_reviewer_verdict_gate(isolate_provider_db):
    class PassHead(MockHead):
        def decide(self, *a, **k):
            return DecisionSchema(decision="PASS", rationale="head says pass", next_steps=[])

    class P2OnlyReviewer:
        provider = "mock"
        def review(self, *a, **k):
            return ReviewResultSchema(verdict="changes_required", p0=[], p1=[], p2=["issue"], summary="minor")

    orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                        head_agent=PassHead(), reviewer_agent=P2OnlyReviewer())
    with make_git_project() as proj:
        def b(builder, o, wt):
            (Path(wt) / "src" / "new.py").write_text("y = 2\n", encoding="utf-8"); return ["src/new.py"]
        summary = orch.run(proj, "g", builder_fn=b, order=OrderSchema(goal="g", allowed_paths=["src/"]))
    assert summary["final_state"] != State.PASS
    assert summary["final_state"] == State.HUMAN_REQUIRED
    assert "not 'pass'" in summary["decision"]["rationale"]

    # malformed verdicts are rejected by the schema itself
    with pytest.raises(Exception):
        ReviewResultSchema(verdict="unknown", p0=[], p1=[], p2=[], summary="")
    with pytest.raises(Exception):
        ReviewResultSchema(verdict="", p0=[], p1=[], p2=[], summary="")


# ------------------------------------------------------------------ TEST F: reviewer exception -> terminal state

def test_f_reviewer_exception_terminal(isolate_provider_db):
    class Boom:
        provider = "mock"
        def review(self, *a, **k):
            raise RuntimeError("reviewer crashed hard")

    class PassHead(MockHead):
        def decide(self, order, b, v, r, **k):
            return DecisionSchema(decision="PASS", rationale="head ignores reviewer", next_steps=[])

    orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                        head_agent=PassHead(), reviewer_agent=Boom())
    with make_git_project() as proj:
        def b(builder, o, wt):
            (Path(wt) / "src" / "new.py").write_text("y = 2\n", encoding="utf-8"); return ["src/new.py"]
        summary = orch.run(proj, "g", builder_fn=b, order=OrderSchema(goal="g", allowed_paths=["src/"]))
    from core.state_machine import TERMINAL_STATES
    assert summary["final_state"] in TERMINAL_STATES and summary["final_state"] != State.PASS
    actions = [r["action"] for r in orch.sm._get_conn().execute("SELECT action FROM audit_logs WHERE run_id=?", (summary["run_id"],)).fetchall()]
    assert "REVIEWER_EXCEPTION" in actions
    assert orch.sm.get_run(summary["run_id"])["current_state"] != State.REVIEWING


# ------------------------------------------------------------------ TEST G: two processes claim the same trial -> exactly one wins

CLAIMER = r'''
import sys, os
sys.path.insert(0, sys.argv[1])
os.environ["DIRECTOR_TESTING"] = "1"; os.environ["TEST_DB_PATH"] = sys.argv[2]
from core.provider_manager import ProviderManager
import time
pm = ProviderManager()
t0 = float(sys.argv[3])
while time.time() < t0: time.sleep(0.001)
print("WON" if pm.claim_trial("OPENAI") else "LOST", flush=True)
'''


def test_g_atomic_trial_claim_two_processes(isolate_provider_db, tmp_path):
    pm = ProviderManager()
    pm.set_status("OPENAI", ProviderStatus.RECOVERING, "probe ok")
    root = str(Path(__file__).resolve().parent.parent)
    script = tmp_path / "claimer.py"
    script.write_text(CLAIMER, encoding="utf-8")
    db = os.environ["TEST_DB_PATH"]
    wins = 0
    for _round in range(3):
        with pm._get_conn() as conn:
            conn.execute("UPDATE provider_health SET trial_started_at=NULL, next_trial_at=NULL WHERE provider='OPENAI'")
            conn.commit()
        t0 = str(time.time() + 1.5)
        procs = [subprocess.Popen([sys.executable, str(script), root, db, t0], stdout=subprocess.PIPE, text=True) for _ in range(4)]
        results = [p.communicate(timeout=30)[0].strip() for p in procs]
        assert results.count("WON") == 1, results
        wins += 1
    assert wins == 3


# ------------------------------------------------------------------ TEST H: failed trial then Refresh -> cooldown kept

def test_h_refresh_does_not_bypass_cooldown(isolate_provider_db, monkeypatch):
    pm = ProviderManager()
    monkeypatch.setattr("core.provider_manager.resolve_cli", lambda name: sys.executable)
    monkeypatch.setattr(ProviderManager, "_get_cli_version", lambda self, b, arg="--version": "cli 1.0.0")
    pm.set_status("OPENAI", ProviderStatus.RECOVERING, "probe ok")
    assert pm.claim_trial("OPENAI") is True
    # the trial fails on quota
    pm.parse_cli_error("OPENAI", "You've hit your usage limit")
    assert pm.get_status("OPENAI") == ProviderStatus.QUOTA_EXHAUSTED
    row = pm._health_row("OPENAI")
    assert row["next_trial_at"] and datetime.fromisoformat(row["next_trial_at"]) > datetime.utcnow()

    # Refresh providers (force) may re-probe: status can only reach RECOVERING, trial stays on cooldown
    dashboard_server.refresh_providers()
    pm2 = ProviderManager()
    assert pm2.get_status("OPENAI") == ProviderStatus.RECOVERING
    assert pm2.trial_available("OPENAI") is False
    assert pm2.claim_trial("OPENAI") is False
    # repeated refresh/claim loops never consume a second real call
    for _ in range(5):
        dashboard_server.refresh_providers()
        assert ProviderManager().claim_trial("OPENAI") is False
    # cooldown expiry (simulated) re-enables exactly one trial
    past = (datetime.utcnow() - timedelta(seconds=1)).isoformat()
    with pm._get_conn() as conn:
        conn.execute("UPDATE provider_health SET next_trial_at=? WHERE provider='OPENAI'", (past,)); conn.commit()
    pm3 = ProviderManager()
    assert pm3.claim_trial("OPENAI") is True
    assert ProviderManager().claim_trial("OPENAI") is False


# ------------------------------------------------------------------ TEST I/J/K: agy lock policy

def test_i_live_pid_old_lock_never_stale(tmp_path):
    lock_path = tmp_path / "antigravity.lock"
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock_path.write_text(json.dumps({"pid": holder.pid, "token": "live-old", "created_at": time.time() - 10**6, "note": "x"}), encoding="utf-8")
        old = time.time() - 10**6
        os.utime(lock_path, (old, old))
        lk = FileLock(lock_path, max_age_sec=1, note="me")
        assert lk.is_stale() is False
        assert lk.try_acquire() is False
        assert json.loads(lock_path.read_text())["token"] == "live-old"
    finally:
        holder.kill(); holder.wait()


def test_j_dead_pid_stale_lock_recovered(tmp_path):
    lock_path = tmp_path / "antigravity.lock"
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    dead_pid = dead.pid
    deadline = time.time() + 5
    while psutil.pid_exists(dead_pid) and time.time() < deadline:
        time.sleep(0.05)
    if psutil.pid_exists(dead_pid):
        pytest.skip("pid still registered by the OS")
    lock_path.write_text(json.dumps({"pid": dead_pid, "token": "dead", "created_at": time.time(), "note": "crashed"}), encoding="utf-8")
    lk = FileLock(lock_path, note="me")
    assert lk.is_stale() is True
    assert lk.try_acquire() is True
    owner = lk.read_owner()
    assert owner["pid"] == os.getpid() and owner["token"] == lk.token
    lk.release()
    assert not lock_path.exists()


def test_k_lock_token_replaced_during_recovery_not_deleted(tmp_path, monkeypatch):
    lock_path = tmp_path / "antigravity.lock"
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    if psutil.pid_exists(dead.pid):
        pytest.skip("pid still registered by the OS")
    lock_path.write_text(json.dumps({"pid": dead.pid, "token": "stale-token", "created_at": time.time(), "note": "crashed"}), encoding="utf-8")
    lk = FileLock(lock_path, note="me")

    # simulate a race: between the staleness check and the unlink another live process replaces the lock
    real_is_stale = lk.is_stale
    def racy_is_stale():
        result = real_is_stale()
        lock_path.write_text(json.dumps({"pid": os.getpid(), "token": "fresh-token", "created_at": time.time(), "note": "other"}), encoding="utf-8")
        return result
    monkeypatch.setattr(lk, "is_stale", racy_is_stale)
    assert lk.try_acquire() is False
    assert json.loads(lock_path.read_text())["token"] == "fresh-token"     # the new lock survived

    # release never deletes a lock carrying another token
    lk2 = FileLock(lock_path, note="me2")
    lk2.acquired = True; lk2.token = "not-mine"
    lk2.release()
    assert lock_path.exists()


# ------------------------------------------------------------------ TEST L: resolve_cli PermissionError

def test_l_resolve_cli_permission_error_no_crash(monkeypatch):
    monkeypatch.setattr(env_utils.shutil, "which", lambda name: None)
    monkeypatch.setattr("core.provider_manager.resolve_cli", env_utils.resolve_cli)   # real resolver (conftest stubs it)
    real_exists = Path.exists
    def exploding_exists(self):
        if "npm" in str(self) or "agy" in str(self):
            raise PermissionError(5, "Access is denied")
        return real_exists(self)
    monkeypatch.setattr(Path, "exists", exploding_exists)
    for name in ("claude", "codex", "agy"):
        assert env_utils.resolve_cli(name) is None              # unavailable, not a crash
    # and the ProviderManager health check reports UNAVAILABLE instead of raising
    pm = ProviderManager(db_path=str(Path(tempfile.mkdtemp()) / "t.db"))
    statuses = pm.check_health(force=True)
    assert all(s == ProviderStatus.UNAVAILABLE for s in statuses.values())
    # partial failure: first candidate explodes, second exists -> second is used
    monkeypatch.undo()
    monkeypatch.setattr(env_utils.shutil, "which", lambda name: None)
    calls = {"n": 0}
    def flaky_exists(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(5, "Access is denied")
        return True
    monkeypatch.setattr(Path, "exists", flaky_exists)
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)
    assert env_utils.resolve_cli("codex") is not None


# ------------------------------------------------------------------ TEST M: truthfulness for other statuses

@pytest.mark.parametrize("status", [ProviderStatus.QUOTA_LOW, ProviderStatus.DEGRADED, ProviderStatus.CONFIG_ERROR, ProviderStatus.UNKNOWN, ProviderStatus.RECOVERING])
def test_m_version_success_never_promotes_non_healthy(isolate_provider_db, monkeypatch, status):
    pm = ProviderManager()
    monkeypatch.setattr("core.provider_manager.resolve_cli", lambda name: sys.executable)
    monkeypatch.setattr(ProviderManager, "_get_cli_version", lambda self, b, arg="--version": "cli 1.0.0")
    with pm._get_conn() as conn:
        conn.execute("UPDATE provider_health SET status=? WHERE provider='OPENAI'", (status.value,)); conn.commit()
    pm._sync_from_db()
    pm.check_health()
    assert pm.get_status("OPENAI") == status
    pm.check_health(force=True)
    assert pm.get_status("OPENAI") == status
    # only a real successful call promotes
    pm.record_success("OPENAI")
    assert pm.get_status("OPENAI") == ProviderStatus.HEALTHY


# ------------------------------------------------------------------ TEST N: run_id 10000 unique + collision retry

def test_n_run_id_unique_and_collision_retry(isolate_provider_db, monkeypatch):
    orch = _mock_orch()
    ids = [orch.generate_run_id() for _ in range(10000)]
    assert len(set(ids)) == 10000
    assert all(len(i.split("-")[2]) == 32 for i in ids)

    # forced collision: the first generated id already exists -> retry allocates a fresh one
    existing = orch.generate_run_id()
    orch.sm.create_run(existing, "p", "g")
    seq = iter([existing, existing, orch.generate_run_id()])
    monkeypatch.setattr(orch, "generate_run_id", lambda: next(seq))
    rid = orch._create_run_with_retry(None, "p", "g")
    assert rid != existing and orch.sm.get_run(rid) is not None
    # an explicit run_id that collides is NOT silently replaced
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        orch._create_run_with_retry(existing, "p", "g")

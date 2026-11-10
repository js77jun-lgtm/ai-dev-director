"""
v0.2.4.6 RESULT CENTER — view / test results / open worktree / apply & verify (backup + rollback) / discard /
pending results. No merge, commit, push, stash or reset; every target path is canonically re-checked.
"""
import os
import json
import subprocess
import threading
from pathlib import Path

import pytest

import dashboard_server
from dashboard_server import render_html
from core.result_manager import ResultManager, RESULT_APPLIED, RESULT_DISCARDED, RESULT_APPLY_FAILED, RESULT_VERIFY_FAILED
from core.state_machine import StateMachine, State
from core.safety import SafetyPolicy
from schemas.order import OrderSchema
from tests.test_v0231_chat_stabilization import make_git_project
from tests.test_v0234_resume_parity import _mock_orch
from tests.test_v0238_dashboard_ux import env, server, _get_json, _post  # noqa: F401
from tests.test_v0233_codex_fixes import _make_link


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()


HDR = "import sys, os; sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))"


def _builder(b, o, w):
    (Path(w) / "src" / "n.py").write_text("def n():\n    return 1\n", encoding="utf-8")
    (Path(w) / "tests" / "test_n.py").write_text(HDR + "\nfrom n import n\ndef test_n():\n    assert n() == 1\n", encoding="utf-8")
    (Path(w) / "src" / "calc.py").write_text("def add(a, b): return a + b\ndef mul(a, b): return a * b\n", encoding="utf-8")
    return ["src/n.py", "tests/test_n.py", "src/calc.py"]


def _pass_run(proj, builder=_builder, test_cmd="python -m pytest -q tests/"):
    (Path(proj) / "tests").mkdir(exist_ok=True)
    (Path(proj) / "src" / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    (Path(proj) / "tests" / "test_calc.py").write_text(HDR + "\nfrom calc import add\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(proj), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=str(proj), check=True, capture_output=True)
    orch = _mock_orch(verification={"test_command": test_cmd})
    summary = orch.run(str(proj), "add n", builder_fn=builder, order=OrderSchema(goal="add n", allowed_paths=["src/", "tests/"], protected_paths=[".git/"]))
    assert summary["final_state"] == "PASS", summary
    return summary["run_id"], orch


@pytest.fixture
def rm(env):
    from core.result_manager import _RECENT_APPLY_REQUESTS, _APPLY_INFLIGHT
    _RECENT_APPLY_REQUESTS.clear(); _APPLY_INFLIGHT.clear()
    return ResultManager(config={"verification": {"test_command": "python -m pytest -q tests/"}})


# ------------------------------------------------------------------ A/B/C: result ready, view changes, test results

def test_abc_result_ready_view_and_tests(rm, server):
    with make_git_project() as proj:
        run_id, orch = _pass_run(proj)
        s = rm.summary(run_id, with_diffs=True)
        assert s["is_pass"] and s["result"]["state"] == "READY" and s["worktree_exists"]
        assert sorted((c["type"], c["path"]) for c in s["changed_files"]) == [("A", "src/n.py"), ("A", "tests/test_n.py"), ("M", "src/calc.py")]
        assert s["counts"] == {"A": 2, "M": 1, "D": 0} and s["control_plane"] == []
        assert "def mul" in s["file_diffs"]["src/calc.py"] and ("new file" in s["file_diffs"]["src/n.py"] and "def n" in s["file_diffs"]["src/n.py"])
        t = s["tests"]
        assert t["baseline"]["passed"] == 1 and t["post"]["passed"] == 2 and t["regression"] == "PASS" and t["canonical"] == "PASS" and t["reviewer"] == "pass" and t["final_gate"] == "PASS"
        st = _get_json(server, "/status")[1]
        assert st["result"]["is_pass"] and st["result"]["changed_count"] == 3 and st["results"][0]["run_id"] == run_id
        html = render_html()
        assert "RESULT READY" in html and "Changed files: 3" in html and "RESULTS (1)" in html and "RUN PASS" in html
        for bid in ("result-view-btn", "result-tests-btn", "result-open-btn", "result-apply-btn", "result-discard-btn", "chat-view-result-btn"):
            assert f'id="{bid}"' in html
        detail = _get_json(server, f"/result?run_id={run_id}")[1]
        assert detail["file_diffs"] and detail["preconditions"]["ok"] is True and "manifest" not in detail["preconditions"]


# ------------------------------------------------------------------ D: open worktree uses only the canonical recorded path

def test_d_open_worktree_canonical_only(rm, monkeypatch):
    with make_git_project() as proj:
        run_id, orch = _pass_run(proj)
        calls = []
        monkeypatch.setattr("core.result_manager.subprocess.Popen", lambda argv, **k: calls.append(argv))
        monkeypatch.setattr("core.result_manager.os.name", "nt")
        assert rm.open_worktree(run_id)["status"] == "ok"
        assert calls == [["explorer.exe", str(Path(orch.sm.get_run(run_id)["worktree_path"]).resolve())]]
        # tampered worktree path outside .director_worktrees -> refused, explorer not called
        with orch.sm._get_conn() as c:
            c.execute("UPDATE runs SET worktree_path=? WHERE run_id=?", (str(Path(proj).resolve()), run_id)); c.commit()
        assert rm.open_worktree(run_id)["status"] == "refused" and len(calls) == 1
        assert rm.open_project(run_id)["status"] == "ok" and calls[-1] == ["explorer.exe", str(Path(proj).resolve())]


# ------------------------------------------------------------------ E/N: clean main + same HEAD -> apply, verify, no commit

def test_en_apply_and_verify(rm, server):
    with make_git_project() as proj:
        run_id, orch = _pass_run(proj)
        head_before = _git(["rev-parse", "HEAD"], proj)
        code, j = _post(server, "/result/apply", {"run_id": run_id, "request_id": "ap-1"})
        assert code == 200 and j["status"] == "applied", j
        assert sorted(j["files"]) == ["src/calc.py", "src/n.py", "tests/test_n.py"] and j["verification"]["passed"] == 2
        assert (Path(proj) / "src" / "n.py").exists() and "def mul" in (Path(proj) / "src" / "calc.py").read_text()
        assert _git(["rev-parse", "HEAD"], proj) == head_before                                    # no commit
        assert _git(["log", "--oneline"], proj).count("\n") == _git(["log", "--oneline"], proj).count("\n")
        status = sorted(l.strip() for l in _git(["status", "--porcelain"], proj).splitlines() if "__pycache__" not in l and ".pytest_cache" not in l)
        assert status == ["?? src/n.py", "?? tests/test_n.py", "M src/calc.py"]
        assert rm.result_state(run_id)["state"] == RESULT_APPLIED
        acts = [a["action"] for a in orch.sm.get_audit_logs(run_id)]
        assert acts.count("RESULT_APPLY_STARTED") == 1 and acts.count("RESULT_APPLY_COMPLETED") == 1
        rec = json.loads([a for a in orch.sm.get_audit_logs(run_id) if a["action"] == "RESULT_APPLY_COMPLETED"][0]["payload"])
        assert rec["applied_by"] == "HUMAN" and rec["request_id"] == "ap-1" and rec["source_commit"] == head_before and set(rec["hashes"]) == set(j["files"])
        assert (rm.run_dir(run_id) / "result_manifest.json").exists() and (rm.run_dir(run_id) / "apply_backup" / "backup_manifest.json").exists()
        st = _get_json(server, "/status")[1]
        assert st["result"]["result"]["state"] == "APPLIED" and st["results"] == []
        html = render_html()
        assert "RESULT APPLIED" in html and "RESULT: APPLIED" in html and 'id="result-openproj-btn"' in html
        # second apply refused
        assert _post(server, "/result/apply", {"run_id": run_id, "request_id": "ap-2"})[1]["status"] == "blocked"


# ------------------------------------------------------------------ F/G/H: preconditions block without touching main

def test_f_main_head_changed_blocks(rm):
    with make_git_project() as proj:
        run_id, orch = _pass_run(proj)
        (Path(proj) / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(proj), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "moved on"], cwd=str(proj), check=True, capture_output=True)
        r = rm.apply(run_id, request_id="f")
        assert r["status"] == "blocked" and any("Main project changed" in b for b in r["blocks"])
        assert not (Path(proj) / "src" / "n.py").exists() and rm.result_state(run_id)["state"] == "APPLY_BLOCKED"


def test_g_dirty_tracked_blocks(rm):
    with make_git_project() as proj:
        run_id, orch = _pass_run(proj)
        (Path(proj) / "src" / "calc.py").write_text("def add(a, b): return a + b  # local edit\n", encoding="utf-8")
        r = rm.apply(run_id, request_id="g")
        assert r["status"] == "blocked" and any("not clean" in b for b in r["blocks"])
        assert "local edit" in (Path(proj) / "src" / "calc.py").read_text() and not (Path(proj) / "src" / "n.py").exists()


def test_h_untracked_collision_blocks(rm):
    with make_git_project() as proj:
        run_id, orch = _pass_run(proj)
        (Path(proj) / "src" / "n.py").write_text("USER FILE\n", encoding="utf-8")
        r = rm.apply(run_id, request_id="h")
        assert r["status"] == "blocked" and any("collides" in b for b in r["blocks"])
        assert (Path(proj) / "src" / "n.py").read_text() == "USER FILE\n"


# ------------------------------------------------------------------ I/J: control plane / escape in result -> blocked

def test_i_control_plane_in_result_blocks(rm, server):
    with make_git_project() as proj:
        run_id, orch = _pass_run(proj)
        wt = Path(orch.sm.get_run(run_id)["worktree_path"])
        (wt / ".agents").mkdir(exist_ok=True); (wt / ".agents" / "hooks.json").write_text("{}", encoding="utf-8")
        s = rm.summary(run_id)
        assert any(p.startswith(".agents") for p in s["control_plane"])
        r = rm.apply(run_id, request_id="i")
        assert r["status"] == "blocked" and any("Control-plane" in b or "differs" in b for b in r["blocks"])
        assert not (Path(proj) / ".agents").exists() and not (Path(proj) / "src" / "n.py").exists()
        assert "APPLY BLOCKED" in render_html()


def test_j_target_escape_blocks(rm, tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    with make_git_project() as proj:
        run_id, orch = _pass_run(proj)
        # replace main's src/ with a junction to an outside directory -> target parent escapes
        import shutil
        shutil.move(str(Path(proj) / "src"), str(outside / "src_real"))
        if not _make_link(Path(proj) / "src", outside / "src_real"):
            pytest.skip("junction unsupported")
        r = rm.apply(run_id, request_id="j")
        assert r["status"] == "blocked" and any("escapes" in b or "not clean" in b or "differs" in b for b in r["blocks"]), r
        assert not (outside / "src_real" / "n.py").exists()


# ------------------------------------------------------------------ K: 5 rapid applies -> 1

def test_k_rapid_apply_once(rm, server):
    with make_git_project() as proj:
        run_id, orch = _pass_run(proj)
        results = []
        def fire(rid):
            results.append(_post(server, "/result/apply", {"run_id": run_id, "request_id": rid}))
        ts = [threading.Thread(target=fire, args=(r,)) for r in ("same", "same", "same", "o1", "o2")]
        for t in ts: t.start()
        for t in ts: t.join(timeout=120)
        statuses = sorted(r[1]["status"] for r in results)
        assert statuses.count("applied") == 1, statuses
        acts = [a["action"] for a in orch.sm.get_audit_logs(run_id)]
        assert acts.count("RESULT_APPLY_COMPLETED") == 1 and acts.count("RESULT_APPLY_STARTED") == 1


# ------------------------------------------------------------------ L/M: rollback on write failure / verify failure

def test_l_write_failure_rolls_back(rm, monkeypatch):
    with make_git_project() as proj:
        run_id, orch = _pass_run(proj)
        original = (Path(proj) / "src" / "calc.py").read_bytes()
        import core.result_manager as R
        real_replace = R.os.replace
        state = {"n": 0}
        def flaky(src, dst):
            state["n"] += 1
            if state["n"] == 2:
                raise OSError("disk full")
            return real_replace(src, dst)
        monkeypatch.setattr(R.os, "replace", flaky)
        r = rm.apply(run_id, request_id="l")
        assert r["status"] == "rolled_back" and r["result_state"] == RESULT_APPLY_FAILED and r["rollback_errors"] == []
        assert (Path(proj) / "src" / "calc.py").read_bytes() == original
        assert not (Path(proj) / "src" / "n.py").exists() and not (Path(proj) / "tests" / "test_n.py").exists()
        assert _git(["status", "--porcelain", "--untracked-files=no"], proj) == ""
        assert "RESULT_APPLY_ROLLED_BACK" in [a["action"] for a in orch.sm.get_audit_logs(run_id)]


def test_m_verify_failure_rolls_back(rm):
    with make_git_project() as proj:
        run_id, orch = _pass_run(proj)
        original = (Path(proj) / "src" / "calc.py").read_bytes()
        r = rm.apply(run_id, request_id="m", test_cmd="python -c \"import sys; sys.exit(1)\"")
        assert r["status"] == "rolled_back" and r["result_state"] == RESULT_VERIFY_FAILED
        assert (Path(proj) / "src" / "calc.py").read_bytes() == original and not (Path(proj) / "src" / "n.py").exists()
        assert _git(["status", "--porcelain", "--untracked-files=no"], proj) == ""
        assert rm.result_state(run_id)["state"] == RESULT_VERIFY_FAILED
        # a later clean apply still works
        assert rm.apply(run_id, request_id="m2")["status"] == "applied"


# ------------------------------------------------------------------ O: discard

def test_o_discard(rm, server):
    with make_git_project() as proj:
        run_id, orch = _pass_run(proj)
        run_id2, _ = _pass_run(proj) if False else (None, None)
        wt = Path(orch.sm.get_run(run_id)["worktree_path"])
        # a second worktree that must survive
        other_wt = Path(proj) / ".director_worktrees" / "OTHER"
        subprocess.run(["git", "worktree", "add", "-q", str(other_wt), "-b", "OTHER"], cwd=str(proj), check=True, capture_output=True)
        head = _git(["rev-parse", "HEAD"], proj)
        code, j = _post(server, "/result/discard", {"run_id": run_id, "request_id": "d1"})
        assert code == 200 and j["status"] == "discarded" and j["removed"] is True
        assert not wt.exists() and other_wt.exists()
        assert _git(["rev-parse", "HEAD"], proj) == head and _git(["status", "--porcelain", "--untracked-files=no"], proj) == ""
        assert not (Path(proj) / "src" / "n.py").exists()
        d = rm.run_dir(run_id)
        for f in ("final_evidence.json", "verification.json", "provenance.json", "order.json", "discard_evidence.json", "result_state.json"):
            assert (d / f).exists(), f
        ev = json.loads((d / "discard_evidence.json").read_text(encoding="utf-8"))
        assert any(c["path"] == "src/n.py" for c in ev["changed_files"])
        assert orch.sm.get_run(run_id)["current_state"] == "PASS" and rm.result_state(run_id)["state"] == RESULT_DISCARDED
        assert "RESULT_DISCARDED" in [a["action"] for a in orch.sm.get_audit_logs(run_id)]
        st = _get_json(server, "/status")[1]
        assert st["results"] == [] and "RESULT DISCARDED" in render_html()
        assert rm.apply(run_id, request_id="after-discard")["status"] == "blocked"


# ------------------------------------------------------------------ P: pending results independent of newest run

def test_p_pending_results(rm, server):
    with make_git_project() as p1, make_git_project() as p2, make_git_project() as p3:
        ids = [_pass_run(p)[0] for p in (p1, p2, p3)]
        sm = StateMachine(); sm.create_run("RUN-NEWER", str(p1), "g"); sm.transition_to("RUN-NEWER", State.PLANNING); sm.transition_to("RUN-NEWER", State.HUMAN_REQUIRED, details="x")
        st = _get_json(server, "/status")[1]
        assert st["run"]["run_id"] == "RUN-NEWER" and st["result"]["is_pass"] is False
        assert sorted(r["run_id"] for r in st["results"]) == sorted(ids) and all(r["changed_count"] == 3 for r in st["results"])
        assert "RESULTS (3)" in render_html()
        detail = _get_json(server, f"/result?run_id={ids[1]}")[1]
        assert detail["is_pass"] and detail["changed_count"] == 3 and _get_json(server, "/status")[1]["run"]["run_id"] == "RUN-NEWER"
        assert _post(server, "/result/discard", {"run_id": ids[0], "request_id": "pd"})[1]["status"] == "discarded"
        assert _post(server, "/result/apply", {"run_id": ids[2], "request_id": "pa"})[1]["status"] == "applied"
        assert [r["run_id"] for r in _get_json(server, "/status")[1]["results"]] == [ids[1]]


def test_q_no_git_mutation_commands_in_result_manager():
    src = Path("core/result_manager.py").read_text(encoding="utf-8")
    for forbidden in ("shell=True", '"merge"', '"commit"', '"push"', '"stash"', '"reset"', "--hard"):
        assert forbidden not in src, forbidden
    # v3.1.8: the folder is opened with the platform file manager (explorer.exe / open / xdg-open), still argv-only
    assert '["worktree", "remove", "--force"' in src and '"explorer.exe"' in src and '"xdg-open"' in src and "Popen(argv)" in src

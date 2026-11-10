"""
v0.2.4.4 HUMAN ACTIONS PHASE 2 — post-build approvable HUMAN_REQUIRED (narrow per-file waiver, builder never
re-run) and Pending Human Actions list independent of the newest run. Safety is never widened.
"""
import json
import time
import threading
import subprocess
from pathlib import Path

import pytest

import dashboard_server
from dashboard_server import render_html, pending_actions, approval_info, GLOBAL_SESSION_ID, _record_human_required
from core.orchestrator import Orchestrator
from core.state_machine import StateMachine, State
from core.safety import SafetyPolicy
from schemas.order import OrderSchema
from tests.test_v0231_chat_stabilization import make_git_project
from tests.test_v0238_dashboard_ux import env, server, _get_json, _post  # noqa: F401
from tests.test_v0243_approval import (approval_env, ApprovingOrch, BUILDER_CALLS, _mock_orch, _wait_done, _halted_run)  # noqa: F401
from tests.test_v0233_codex_fixes import _make_link


def _dep_builder(b, o, w):
    """Builder that really modifies the approved sensitive file (requirements.txt) + the allowed source file."""
    BUILDER_CALLS.append(w)
    (Path(w) / "requirements.txt").write_text("pytest>=8.0\n", encoding="utf-8")
    (Path(w) / "src" / "n.py").write_text("z=1\n", encoding="utf-8")
    return ["requirements.txt", "src/n.py"]


def _two_dep_builder(b, o, w):
    BUILDER_CALLS.append(w)
    (Path(w) / "requirements.txt").write_text("pytest>=8.0\n", encoding="utf-8")
    (Path(w) / "pyproject.toml").write_text("[tool.x]\n", encoding="utf-8")
    return ["requirements.txt", "pyproject.toml"]


def _run_to_post_build_halt(server, proj, builder=_dep_builder, allowed=("src/", "requirements.txt", "pyproject.toml")):
    """Entry halt -> human approve -> builder modifies requirements.txt -> post-build HUMAN_REQUIRED."""
    order = OrderSchema(goal="add dep", allowed_paths=list(allowed), protected_paths=[".git/"])
    orch = _mock_orch()
    reasons = SafetyPolicy.assess_order_for_auto_run(order)
    run_id = _record_human_required(orch, str(proj), "add dep", "APPROVAL_REQUIRED: " + "; ".join(reasons), order)
    dashboard_server._new_orchestrator = lambda: ApprovingOrch(builder)
    code, j = _post(server, "/run/approve", {"run_id": run_id, "request_id": "entry-" + run_id[-6:]})
    assert code == 200 and j["status"] == "accepted"
    state = _wait_done(orch, run_id)
    return run_id, orch, state


# ------------------------------------------------------------------ classification (A1)

def test_1_classification_post_build_gates():
    cls = SafetyPolicy.classify_post_build_violations([
        "Dependency configuration modified: requirements.txt",
        "Dependency configuration modified: pyproject.toml",
        "File modified outside allowed_paths: README.md",
        "Path escapes worktree (symlink/junction or traversal): src/escape/x.py",
        "Protected path modified: .env",
        "Permission configuration modified: .github/workflows/ci.yml",
    ])
    assert [i["file"] for i in cls["approvable"]] == ["requirements.txt", "pyproject.toml"]
    assert len(cls["blocking"]) == 4
    remaining, waived = SafetyPolicy.apply_waivers(
        ["Dependency configuration modified: requirements.txt", "Dependency configuration modified: pyproject.toml", "File modified outside allowed_paths: x"],
        [{"gate": "DEPENDENCY_CONFIGURATION_MODIFIED", "file": "requirements.txt"}])
    assert waived == ["Dependency configuration modified: requirements.txt"]
    assert remaining == ["Dependency configuration modified: pyproject.toml", "File modified outside allowed_paths: x"]
    # a waiver for a non-approvable gate is ignored
    remaining2, waived2 = SafetyPolicy.apply_waivers(["Path escapes worktree (symlink/junction or traversal): a"], [{"gate": "PATH_ESCAPE", "file": "a"}])
    assert waived2 == [] and remaining2


# ------------------------------------------------------------------ 1/2/3: entry approve -> builder -> post-build halt with actions

def test_2_3_post_build_halt_shows_actions(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        run_id, orch, state = _run_to_post_build_halt(server, proj)
        assert state == "HUMAN_REQUIRED" and len(BUILDER_CALLS) == 1
        last = orch.sm.get_transitions(run_id)[-1]["details"]
        assert last == "Scope violation: Dependency configuration modified: requirements.txt"
        st = orch.approval_state(run_id)
        assert st["stage"] == "POST_BUILD" and st["approvable"] is True and st["builder_ran"] is True
        assert [i["file"] for i in st["items"]] == ["requirements.txt"] and st["blocking"] == []
        payload = _get_json(server, "/status")[1]
        ap = payload["approval"]
        assert ap["required"] is True and ap["stage"] == "POST_BUILD" and ap["stage_label"] == "POST-BUILD / VERIFYING" and ap["builder"] == "COMPLETED"
        assert ap["items"][0]["title"] == "Dependency configuration modified" and ap["items"][0]["file"] == "requirements.txt"
        html = render_html()
        assert "POST-BUILD / VERIFYING" in html and 'id="approve-btn" class="approve" style="">' in html and "HUMAN APPROVAL REQUIRED" in html
        ins = _get_json(server, f"/run/inspect?run_id={run_id}")[1]
        assert ins["approval"]["stage"] == "POST_BUILD" and "requirements.txt" in ins["changed_files"]
        assert "requirements.txt" in ins["file_diffs"] and "pytest>=8.0" in ins["file_diffs"]["requirements.txt"]
        assert ins["builder"]["status"] == "COMPLETED" and ins["stages_executed"][-2:] == ["BUILDING", "VERIFYING"]   # scope guard halts inside VERIFYING
        assert ins["stages_upcoming"] == ["VERIFYING", "REVIEWING", "DECIDING", "FINAL_GATE"]
        assert ins["scope_checks"] == [last]


# ------------------------------------------------------------------ 4: post-build approve -> same run, builder not re-run, PASS

def test_4_post_build_approve_resumes_without_builder(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        run_id, orch, state = _run_to_post_build_halt(server, proj)
        assert state == "HUMAN_REQUIRED"
        calls_before = len(BUILDER_CALLS)
        code, j = _post(server, "/run/approve", {"run_id": run_id, "request_id": "post-" + run_id[-6:]})
        assert code == 200 and j["status"] == "accepted"
        assert _wait_done(orch, run_id) == "PASS"
        assert len(BUILDER_CALLS) == calls_before                                  # builder NOT re-run
        approvals = [json.loads(a["payload"]) for a in orch.sm.get_audit_logs(run_id) if a["action"] == "HUMAN_APPROVAL"]
        assert [a["approval_stage"] for a in approvals] == ["ENTRY", "POST_BUILD"]  # two separate records
        assert approvals[1]["approved_files"] == ["requirements.txt"] and approvals[1]["approved_gates"] == ["DEPENDENCY_CONFIGURATION_MODIFIED"]
        assert approvals[1]["request_id"].startswith("post-") and approvals[1]["approver"] == "HUMAN"
        trans = [(t["from_state"], t["to_state"]) for t in orch.sm.get_transitions(run_id)]
        assert trans.count(("HUMAN_REQUIRED", "ORDER_READY")) == 1 and trans.count(("HUMAN_REQUIRED", "VERIFYING")) == 1
        assert trans.count(("ORDER_READY", "BUILDING")) == 1 and trans[-1] == ("DECIDING", "PASS")
        acts = [a["action"] for a in orch.sm.get_audit_logs(run_id)]
        assert acts.count("BUILDER_EXECUTION") == 1 and "SCOPE_WAIVER_APPLIED" in acts and "CODE_REVIEW" in acts and "FINAL_DECISION" in acts
        waivers = (orch._load_json(run_id, "approvals.json") or {}).get("post_build")
        assert waivers == [dict(waivers[0])] and waivers[0]["file"] == "requirements.txt"
        assert len([r for r in orch.sm.list_runs() if r["project_path"] == str(proj)]) == 1     # same run, no new run
        # main repo untouched
        assert subprocess.run(["git", "status", "--short", "--", "requirements.txt", "src"], cwd=str(proj), capture_output=True, text=True).stdout.strip() == ""


# ------------------------------------------------------------------ 5: narrow waiver, other sensitive file still blocks

def test_5_waiver_is_per_file(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        run_id, orch, state = _run_to_post_build_halt(server, proj, builder=_two_dep_builder)
        assert state == "HUMAN_REQUIRED"
        st = orch.approval_state(run_id)
        assert sorted(i["file"] for i in st["items"]) == ["pyproject.toml", "requirements.txt"]
        # simulate approving ONLY requirements.txt (narrow waiver written directly), then resume: pyproject.toml still blocks
        orch._save_json(run_id, "approvals.json", {"post_build": [{"gate": "DEPENDENCY_CONFIGURATION_MODIFIED", "file": "requirements.txt"}]})
        orch.sm.log_audit(run_id, "HUMAN", "HUMAN_APPROVAL", json.dumps({"approval_stage": "POST_BUILD", "approved_files": ["requirements.txt"]}))
        assert orch.sm.reopen_after_human_approval(run_id, "HUMAN_APPROVED(POST_BUILD) partial", target=State.VERIFYING)
        summary = orch.resume(run_id)
        assert summary["final_state"] == "HUMAN_REQUIRED"
        last = orch.sm.get_transitions(run_id)[-1]["details"]
        assert last == "Scope violation: Dependency configuration modified: pyproject.toml"
        assert len(BUILDER_CALLS) == 1


# ------------------------------------------------------------------ 6/7: canonical escape and regression are never approvable

def test_6_canonical_escape_not_approvable(approval_env, server, monkeypatch, tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    def escape_builder(b, o, w):
        BUILDER_CALLS.append(w)
        if not _make_link(Path(w) / "src" / "escape", outside):
            pytest.skip("junction unsupported")
        (outside / "x.py").write_text("x=1\n", encoding="utf-8")
        (Path(w) / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        return ["src/escape/x.py", "requirements.txt"]
    with make_git_project() as proj:
        run_id, orch, state = _run_to_post_build_halt(server, proj, builder=escape_builder)
        assert state == "HUMAN_REQUIRED"
        last = orch.sm.get_transitions(run_id)[-1]["details"]
        assert "escapes worktree" in last or "outside allowed_paths" in last
        st = orch.approval_state(run_id)
        assert st["approvable"] is False and st["blocking"]
        assert _get_json(server, "/status")[1]["approval"]["required"] is False
        assert 'id="approve-btn" class="approve" style="display:none;"' in render_html()
        code, j = _post(server, "/run/approve", {"run_id": run_id, "request_id": "esc"})
        assert code == 400 and len(BUILDER_CALLS) == 1


def test_7_regression_not_approvable(approval_env, server, monkeypatch):
    def regress_builder(b, o, w):
        BUILDER_CALLS.append(w)
        (Path(w) / "tests" / "test_calc.py").write_text("import pytest\ndef test_a():\n    assert True\ndef test_b():\n    pytest.skip('regressed')\n", encoding="utf-8")
        return ["tests/test_calc.py"]
    with make_git_project() as proj:
        order = OrderSchema(goal="g", allowed_paths=["tests/", "requirements.txt"], protected_paths=[".git/"])
        orch = _mock_orch()
        run_id = _record_human_required(orch, str(proj), "g", "APPROVAL_REQUIRED: SENSITIVE_FILE_IN_SCOPE (requirements.txt)", order)
        (Path(proj) / "tests").mkdir(exist_ok=True)
        (Path(proj) / "tests" / "test_calc.py").write_text("def test_a():\n    assert True\ndef test_b():\n    assert True\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(proj), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "two tests"], cwd=str(proj), check=True, capture_output=True)
        dashboard_server._new_orchestrator = lambda: ApprovingOrch(regress_builder)
        cfg_orch = ApprovingOrch(regress_builder); cfg_orch.o.config["verification"] = {"test_command": "python -m pytest -q tests/"}
        dashboard_server._new_orchestrator = lambda: cfg_orch
        code, j = _post(server, "/run/approve", {"run_id": run_id, "request_id": "reg"})
        assert code == 200
        assert _wait_done(orch, run_id, timeout=120) == "HUMAN_REQUIRED"
        last = orch.sm.get_transitions(run_id)[-1]["details"]
        assert "decreased" in last or "Unauthorized deletion" in last      # regression gate (or deletion protection): never approvable
        assert orch.approval_state(run_id)["approvable"] is False
        assert _get_json(server, "/status")[1]["approval"]["required"] is False


# ------------------------------------------------------------------ 8: post-build reject

def test_8_post_build_reject_preserves_worktree(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        run_id, orch, state = _run_to_post_build_halt(server, proj)
        wt = orch.sm.get_run(run_id)["worktree_path"]
        code, j = _post(server, "/run/reject", {"run_id": run_id})
        assert code == 200 and j["status"] == "ok"
        assert orch.sm.get_run(run_id)["current_state"] == "HUMAN_REQUIRED"
        assert Path(wt).exists() and (Path(wt) / "requirements.txt").read_text() == "pytest>=8.0\n"    # worktree preserved
        assert len(BUILDER_CALLS) == 1
        assert subprocess.run(["git", "status", "--short"], cwd=str(proj), capture_output=True, text=True).stdout.strip() == ""
        rej = [json.loads(a["payload"]) for a in orch.sm.get_audit_logs(run_id) if a["action"] == "HUMAN_REJECTED"]
        assert len(rej) == 1 and rej[0]["stage"] == "POST_BUILD"
        assert orch.approval_state(run_id)["approvable"] is False
        assert all(p["run_id"] != run_id for p in pending_actions())


# ------------------------------------------------------------------ 9: concurrent post-build approve

def test_9_concurrent_post_build_approve(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        run_id, orch, state = _run_to_post_build_halt(server, proj)
        results = []
        def fire(rid):
            results.append(_post(server, "/run/approve", {"run_id": run_id, "request_id": rid}))
        ts = [threading.Thread(target=fire, args=(f"c{i}",)) for i in range(5)]
        for t in ts: t.start()
        for t in ts: t.join(timeout=30)
        assert sorted(r[1]["status"] for r in results).count("accepted") == 1
        assert _wait_done(orch, run_id) == "PASS"
        acts = [a["action"] for a in orch.sm.get_audit_logs(run_id)]
        assert acts.count("HUMAN_APPROVAL") == 2 and acts.count("BUILDER_EXECUTION") == 1     # entry + one post-build
        assert [(t["from_state"], t["to_state"]) for t in orch.sm.get_transitions(run_id)].count(("HUMAN_REQUIRED", "VERIFYING")) == 1


# ------------------------------------------------------------------ 10/11: pending actions independent of newest run

def test_10_11_pending_actions_list(approval_env, server, monkeypatch):
    with make_git_project() as p1, make_git_project() as p2, make_git_project() as p3:
        ids = [_halted_run(p)[0] for p in (p1, p2, p3)]
        monkeypatch.setattr(dashboard_server, "_new_orchestrator", lambda: ApprovingOrch(lambda b, o, w: (BUILDER_CALLS.append(w), (Path(w) / "src" / "n.py").write_text("z=1\n"), ["src/n.py"])[2]))
        # a newer, unrelated run becomes the "current run"
        sm = StateMachine(); sm.create_run("RUN-NEWEST", str(p1), "g"); sm.transition_to("RUN-NEWEST", State.PLANNING); sm.transition_to("RUN-NEWEST", State.ORDER_READY)
        sm.transition_to("RUN-NEWEST", State.BUILDING); sm.transition_to("RUN-NEWEST", State.VERIFYING); sm.transition_to("RUN-NEWEST", State.REVIEWING)
        sm.transition_to("RUN-NEWEST", State.DECIDING); sm.transition_to("RUN-NEWEST", State.PASS)
        st = _get_json(server, "/status")[1]
        assert st["run"]["run_id"] == "RUN-NEWEST" and st["approval"]["required"] is False
        pend = st["pending"]
        assert sorted(p["run_id"] for p in pend) == sorted(ids) and all(p["status"] == "PENDING" and p["stage"] == "ENTRY" for p in pend)
        html = render_html()
        assert "PENDING ACTIONS (3)" in html and all(i[:26] in html for i in ids)
        # per-run approval info for a selected pending run (no current-run change)
        ap = _get_json(server, f"/run/approval?run_id={ids[1]}")[1]
        assert ap["required"] is True and ap["run_id"] == ids[1]
        assert _get_json(server, "/status")[1]["run"]["run_id"] == "RUN-NEWEST"
        # reject one, approve another -> list shrinks, entry stays for the third
        assert _post(server, "/run/reject", {"run_id": ids[0]})[1]["status"] == "ok"
        assert _post(server, "/run/approve", {"run_id": ids[1], "request_id": "pa-1"})[1]["status"] == "accepted"
        orch = _mock_orch(); _wait_done(orch, ids[1])
        pend2 = _get_json(server, "/status")[1]["pending"]
        assert [p["run_id"] for p in pend2] == [ids[2]]
        assert "PENDING ACTIONS (1)" in render_html()


def test_12_pending_panel_is_dom_update_only():
    src = dashboard_server.DASHBOARD_JS
    assert "renderPending" in src and "lastPendingKey" in src and "innerHTML" not in src and "location.reload" not in src
    assert "selectedRunId" in src and "/run/approval?run_id=" in src

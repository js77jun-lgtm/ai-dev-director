"""
v0.2.4.3 HUMAN_REQUIRED APPROVAL UX — explicit dashboard Approve & Resume / Reject; approval lifts only the
entry APPROVAL_REQUIRED halt, every runtime gate stays; one resume per run; Builder never runs otherwise.
"""
import json
import time
import threading
from pathlib import Path

import pytest

import dashboard_server
from dashboard_server import (render_html, parse_approval_reason, approval_info, inspect_run, approve_run, reject_run,
                              GLOBAL_SESSION_ID, _record_human_required)
from core.orchestrator import Orchestrator
from core.state_machine import StateMachine, State
from core.provider_manager import ProviderManager
from core.safety import SafetyPolicy
from schemas.order import OrderSchema
from schemas.decision import DecisionSchema
from agents.mock_head import MockHead
from agents.codex_reviewer import CodexReviewer
from tests.test_v0231_chat_stabilization import make_git_project
from tests.test_v0238_dashboard_ux import env, server, _get_json, _post  # noqa: F401


class PassHead(MockHead):
    def decide(self, *a, **k):
        return DecisionSchema(decision="PASS", rationale="ok", next_steps=[])


BUILDER_CALLS = []


def _mock_orch():
    return Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                        head_agent=PassHead(), reviewer_agent=CodexReviewer(provider="mock"))


def _good_builder(b, o, w):
    BUILDER_CALLS.append(w)
    (Path(w) / "src" / "n.py").write_text("z=1\n", encoding="utf-8")
    return ["src/n.py"]


def _rogue_builder(b, o, w):
    BUILDER_CALLS.append(w)
    (Path(w) / "README.md").write_text("outside scope\n", encoding="utf-8")     # not in allowed_paths -> Scope Guard
    return ["README.md"]


class ApprovingOrch:
    """Wraps the mock orchestrator so approve_and_resume runs a deterministic builder (no CLI)."""
    def __init__(self, builder):
        self.o = _mock_orch(); self.builder = builder
    def __getattr__(self, n):
        return getattr(self.o, n)
    def approve_and_resume(self, run_id, approver="HUMAN", note="", **kw):
        return self.o.approve_and_resume(run_id, approver=approver, note=note, builder_fn=self.builder, **kw)


@pytest.fixture
def approval_env(env, monkeypatch):
    BUILDER_CALLS.clear()
    monkeypatch.setattr(ProviderManager, "check_health", lambda self, force=False: self.status_map.copy())
    with dashboard_server._APPROVAL_LOCK:
        dashboard_server._APPROVAL_INFLIGHT.clear(); dashboard_server._RECENT_APPROVAL_REQUESTS.clear()
    dashboard_server._ACTIVE_PROJECTS.clear()
    return env


def _wait_done(orch, run_id, timeout=60):
    """Terminal state AND the background approve/resume thread finished (so temp dirs can be removed)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = orch.sm.get_run(run_id)["current_state"]
        with dashboard_server._APPROVAL_LOCK:
            inflight = run_id in dashboard_server._APPROVAL_INFLIGHT
        if state in ("PASS", "FAIL", "HUMAN_REQUIRED", "TIMEOUT") and not inflight:
            return state
        time.sleep(0.2)
    return orch.sm.get_run(run_id)["current_state"]


def _halted_run(proj, allowed=("src/", "requirements.txt")):
    order = OrderSchema(goal="add n", allowed_paths=list(allowed), protected_paths=[".git/"])
    reasons = SafetyPolicy.assess_order_for_auto_run(order)
    assert any(r.startswith("SENSITIVE_FILE_IN_SCOPE (requirements.txt)") for r in reasons)      # policy unchanged
    orch = _mock_orch()
    run_id = _record_human_required(orch, str(proj), "add n", "APPROVAL_REQUIRED: " + "; ".join(reasons), order)
    return run_id, orch


# ------------------------------------------------------------------ A/B/C: halt, Builder 0, buttons shown

def test_abc_sensitive_file_halts_before_builder_and_shows_actions(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        run_id, orch = _halted_run(proj)
        assert orch.sm.get_run(run_id)["current_state"] == "HUMAN_REQUIRED"
        assert BUILDER_CALLS == [] and orch._load_json(run_id, "builder.json") is None and orch.sm.get_run(run_id)["worktree_path"] is None
        monkeypatch.setattr(dashboard_server, "_new_orchestrator", lambda: ApprovingOrch(_good_builder))
        st = _get_json(server, "/status")[1]
        ap = st["approval"]
        assert ap["required"] is True and ap["headline"] == "HUMAN APPROVAL REQUIRED" and ap["next"] == "Approval required before continuing"
        assert ap["items"][0] == {"code": "SENSITIVE_FILE_IN_SCOPE", "file": "requirements.txt", "title": "Sensitive file in scope",
                                  "why": "Dependency/configuration file modification requires user approval.", "approvable": True}
        html = render_html()
        for bid in ("approve-btn", "reject-btn", "inspect-btn", "chat-approve-btn", "chat-reject-btn", "alert-approve-btn", "alert-reject-btn"):
            assert f'id="{bid}"' in html
        assert 'id="approve-btn" class="approve" style="">' in html      # visible (label is localized)
        assert "HUMAN APPROVAL REQUIRED" in html and "Approval required before continuing" in html and "requirements.txt" in html
        # Inspect is read-only and says the builder never ran
        code, ins = _get_json(server, f"/run/inspect?run_id={run_id}")
        assert ins["builder_ran"] is False and ins["related_files"] == ["requirements.txt"] and "requirements.txt" in ins["order"]["allowed_paths"]
        assert ins["stages_executed"] == [] and ins["approval_items"][0]["code"] == "SENSITIVE_FILE_IN_SCOPE"
        assert orch.sm.get_run(run_id)["current_state"] == "HUMAN_REQUIRED" and BUILDER_CALLS == []


# ------------------------------------------------------------------ D: approve -> audit -> SAME run resumes -> builder runs

def test_d_approve_records_audit_and_resumes_same_run(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        run_id, orch = _halted_run(proj)
        monkeypatch.setattr(dashboard_server, "_new_orchestrator", lambda: ApprovingOrch(_good_builder))
        code, j = _post(server, "/run/approve", {"run_id": run_id, "request_id": "ap-1"})
        assert code == 200 and j["status"] == "accepted"
        _wait_done(orch, run_id)
        row = orch.sm.get_run(run_id)
        assert row["current_state"] == "PASS", row
        assert len(BUILDER_CALLS) == 1 and row["worktree_path"] and run_id in row["worktree_path"]
        audits = orch.sm.get_audit_logs(run_id)
        ap = [a for a in audits if a["action"] == "HUMAN_APPROVAL"]
        assert len(ap) == 1
        rec = json.loads(ap[0]["payload"])
        assert rec["approver"] == "HUMAN" and rec["run_id"] == run_id and rec["timestamp"] and "requirements.txt" in rec["approved_scope"]
        assert rec["approval_reason"].startswith("APPROVAL_REQUIRED: SENSITIVE_FILE_IN_SCOPE")
        trans = [(t["from_state"], t["to_state"]) for t in orch.sm.get_transitions(run_id)]
        assert ("HUMAN_REQUIRED", "ORDER_READY") in trans and trans[-1][1] == "PASS"
        assert len([r for r in orch.sm.list_runs() if r["run_id"] != run_id and r["project_path"] == str(proj)]) == 0   # no new run
        st = _get_json(server, "/status")[1]
        assert st["approval"]["required"] is False and st["run"]["state"] == "PASS"


# ------------------------------------------------------------------ E: reject -> builder 0, terminal

def test_e_reject_keeps_terminal_and_never_builds(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        run_id, orch = _halted_run(proj)
        monkeypatch.setattr(dashboard_server, "_new_orchestrator", lambda: ApprovingOrch(_good_builder))
        code, j = _post(server, "/run/reject", {"run_id": run_id})
        assert code == 200 and j["status"] == "ok"
        assert orch.sm.get_run(run_id)["current_state"] == "HUMAN_REQUIRED"
        assert [a["action"] for a in orch.sm.get_audit_logs(run_id)].count("HUMAN_REJECTED") == 1
        assert BUILDER_CALLS == []
        st = _get_json(server, "/status")[1]
        assert st["approval"]["required"] is False and st["approval"]["rejected"] is True and st["approval"]["headline"] == "REJECTED BY HUMAN"
        # approving after a reject is refused
        code, j2 = _post(server, "/run/approve", {"run_id": run_id, "request_id": "ap-after-reject"})
        assert code == 400 and j2["status"] == "not_applicable" and BUILDER_CALLS == []
        assert 'id="approve-btn" class="approve" style="display:none;"' in render_html()


# ------------------------------------------------------------------ F: approval never bypasses other gates

def test_f_other_safety_violation_still_blocks_after_approval(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        run_id, orch = _halted_run(proj)
        monkeypatch.setattr(dashboard_server, "_new_orchestrator", lambda: ApprovingOrch(_rogue_builder))
        code, j = _post(server, "/run/approve", {"run_id": run_id, "request_id": "ap-f"})
        assert code == 200
        _wait_done(orch, run_id)
        row = orch.sm.get_run(run_id)
        assert row["current_state"] == "HUMAN_REQUIRED" and len(BUILDER_CALLS) == 1
        last = orch.sm.get_transitions(run_id)[-1]["details"]
        assert "outside allowed_paths" in last or "SCOPE" in last.upper()
        # the second HUMAN_REQUIRED is a runtime gate, not an approval question: no Approve button
        st = _get_json(server, "/status")[1]
        assert st["approval"]["required"] is False
        assert orch.approval_state(run_id)["approvable"] is False


def test_f2_non_approvable_reasons_are_not_approvable(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        orch = _mock_orch()
        run_id = _record_human_required(orch, str(proj), "g", "PROJECT_SELECTION_REQUIRED: not a git repository", OrderSchema(goal="g", allowed_paths=["src/"]))
        monkeypatch.setattr(dashboard_server, "_new_orchestrator", lambda: ApprovingOrch(_good_builder))
        assert _get_json(server, "/status")[1]["approval"]["required"] is False
        code, j = _post(server, "/run/approve", {"run_id": run_id, "request_id": "ap-x"})
        assert code == 400 and BUILDER_CALLS == [] and orch.sm.get_run(run_id)["current_state"] == "HUMAN_REQUIRED"
        assert parse_approval_reason("APPROVAL_REQUIRED: INVALID_PATH (protected_paths: ..)")["approvable"] is False
    # gate-produced HUMAN_REQUIRED (no APPROVAL_REQUIRED prefix) is never approvable either
    assert approval_info("RUN-NONE", "HUMAN_REQUIRED", "FINAL_GATE: PASS refused")["required"] is False


# ------------------------------------------------------------------ G: duplicate Approve -> one approval / one resume

def test_g_duplicate_approve_single_resume(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        run_id, orch = _halted_run(proj)
        slow_calls = []
        def slow_builder(b, o, w):
            slow_calls.append(w); time.sleep(1.0)
            return _good_builder(b, o, w)
        monkeypatch.setattr(dashboard_server, "_new_orchestrator", lambda: ApprovingOrch(slow_builder))
        results = []
        def fire(rid):
            results.append(_post(server, "/run/approve", {"run_id": run_id, "request_id": rid}))
        ts = [threading.Thread(target=fire, args=(rid,)) for rid in ("same", "same", "same", "other-1", "other-2")]
        for t in ts: t.start()
        for t in ts: t.join(timeout=30)
        statuses = sorted(r[1]["status"] for r in results)
        assert statuses.count("accepted") == 1, statuses
        assert _wait_done(orch, run_id) == "PASS"
        assert len(slow_calls) == 1 and [a["action"] for a in orch.sm.get_audit_logs(run_id)].count("HUMAN_APPROVAL") == 1
        assert [(t["from_state"], t["to_state"]) for t in orch.sm.get_transitions(run_id)].count(("HUMAN_REQUIRED", "ORDER_READY")) == 1
        # direct double reopen is also atomic
        assert orch.sm.reopen_after_human_approval(run_id, "x") is False


# ------------------------------------------------------------------ H: polling keeps button state (server side stable)

def test_h_button_state_stable_across_polls(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        run_id, orch = _halted_run(proj)
        monkeypatch.setattr(dashboard_server, "_new_orchestrator", lambda: ApprovingOrch(_good_builder))
        snaps = [_get_json(server, "/status")[1]["approval"] for _ in range(3)]
        assert all(s["required"] is True and s["run_id"] == run_id for s in snaps) and snaps[0] == snaps[1] == snaps[2]
        html = render_html()
        assert "lastApprovalKey" in html and "approvalBusy" in html      # JS: no rebuild while unchanged, disabled while a click is in flight
        # chat text never approves anything
        dashboard_server.set_project(str(proj))
        assert _get_json(server, "/status")[1]["approval"]["required"] is True and BUILDER_CALLS == []


# ------------------------------------------------------------------ page script must parse (a broken script silently kills polling)

def test_i_dashboard_script_parses():
    import shutil, subprocess, tempfile, os
    node = shutil.which("node")
    src = dashboard_server.DASHBOARD_JS
    for i, line in enumerate(src.split("\n"), 1):          # no raw newline inside a single-quoted JS string
        assert line.count("'") % 2 == 0, f"unbalanced quote on JS line {i}: {line[:80]}"
    if node:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(src); path = f.name
        try:
            r = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=30)
            assert r.returncode == 0, r.stderr
        finally:
            os.unlink(path)

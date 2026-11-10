"""
v0.2.4.5 APPROVAL POLICY + CONTROL-PLANE SAFETY HOTFIX — ENTRY approval is a default-deny allowlist;
Director control-plane files are hard-protected at every layer and always detected post-build.
"""
import json
import subprocess
from pathlib import Path

import pytest

import dashboard_server
from dashboard_server import render_html, parse_approval_reason, _record_human_required
from core.safety import SafetyPolicy
from core.state_machine import State
from schemas.order import OrderSchema
from agents.antigravity_builder import AntigravityBuilder
from tests.test_v0231_chat_stabilization import make_git_project
from tests.test_v0238_dashboard_ux import env, server, _get_json, _post  # noqa: F401
from tests.test_v0243_approval import approval_env, ApprovingOrch, BUILDER_CALLS, _mock_orch, _wait_done  # noqa: F401
from tests.test_v0244_human_actions import _run_to_post_build_halt, _dep_builder


def _entry_halt(server, proj, reason, allowed=("src/",)):
    order = OrderSchema(goal="g", allowed_paths=list(allowed), protected_paths=[".git/"])
    orch = _mock_orch()
    run_id = _record_human_required(orch, str(proj), "g", reason, order)
    dashboard_server._new_orchestrator = lambda: ApprovingOrch(_dep_builder)
    return run_id, orch


def _assert_non_approvable(server, orch, run_id, code):
    st = orch.approval_state(run_id)
    assert st["approvable"] is False and any(b["gate"] == code for b in st["blocking"]) and st["items"] == [] or st["approvable"] is False
    assert _get_json(server, "/status")[1]["approval"]["required"] is False
    html = render_html()
    assert 'id="approve-btn" class="approve" style="display:none;"' in html
    calls_before = len(BUILDER_CALLS)
    resp_code, j = _post(server, "/run/approve", {"run_id": run_id, "request_id": "x-" + code})
    assert resp_code == 400 and j["status"] == "not_applicable"
    assert len(BUILDER_CALLS) == calls_before
    assert [a["action"] for a in orch.sm.get_audit_logs(run_id)].count("HUMAN_APPROVAL") == 0
    assert orch.sm.get_run(run_id)["current_state"] == "HUMAN_REQUIRED"
    assert all(p["run_id"] != run_id for p in dashboard_server.pending_actions())


# ------------------------------------------------------------------ 1-4: ENTRY default deny

@pytest.mark.parametrize("reason,code", [
    ("APPROVAL_REQUIRED: PROTECTED_PATH_IN_SCOPE (src/secret.py)", "PROTECTED_PATH_IN_SCOPE"),
    ("APPROVAL_REQUIRED: PROTECTED_PATH_INSIDE_ALLOWED (src/keys under src/)", "PROTECTED_PATH_INSIDE_ALLOWED"),
    ("APPROVAL_REQUIRED: GIT_INTERNALS_IN_SCOPE (.git/hooks/pre-commit)", "GIT_INTERNALS_IN_SCOPE"),
    ("APPROVAL_REQUIRED: SOME_FUTURE_CODE (whatever)", "SOME_FUTURE_CODE"),
    ("APPROVAL_REQUIRED: DESTRUCTIVE_CHANGE (allowed_deletions present)", "DESTRUCTIVE_CHANGE"),
    ("APPROVAL_REQUIRED: CONTROL_PLANE_IN_SCOPE (.agents/hooks.json)", "CONTROL_PLANE_IN_SCOPE"),
    ("APPROVAL_REQUIRED: SENSITIVE_FILE_IN_SCOPE (requirements.txt); GIT_INTERNALS_IN_SCOPE (.git/x)", "GIT_INTERNALS_IN_SCOPE"),
])
def test_1_4_entry_non_approvable_codes(approval_env, server, monkeypatch, reason, code):
    with make_git_project() as proj:
        run_id, orch = _entry_halt(server, proj, reason)
        _assert_non_approvable(server, orch, run_id, code)
        assert parse_approval_reason(reason)["approvable"] is False


def test_entry_allowlist_is_explicit():
    assert SafetyPolicy.APPROVABLE_ENTRY == {"SENSITIVE_FILE_IN_SCOPE", "AMBIGUOUS_SCOPE", "HIGH_RISK",
                                            "EXPLICIT_HUMAN_APPROVAL_REQUESTED", "USER_DECISION_REQUIRED"}
    for code in ("AMBIGUOUS_SCOPE", "HIGH_RISK", "EXPLICIT_HUMAN_APPROVAL_REQUESTED", "SENSITIVE_FILE_IN_SCOPE",
                 "USER_DECISION_REQUIRED"):
        assert parse_approval_reason(f"APPROVAL_REQUIRED: {code} (x)")["approvable"] is True


# ------------------------------------------------------------------ 5: control plane in ORDER scope -> entry halt, non-approvable

@pytest.mark.parametrize("path", [".agents/hooks.json", ".agents/", "GEMINI.md", ".director_worktrees/x", "sub/.agents/hooks.json"])
def test_5_control_plane_in_order_scope_halts_at_entry(approval_env, server, path):
    order = OrderSchema(goal="g", allowed_paths=["src/", path], protected_paths=[".git/"])
    reasons = SafetyPolicy.assess_order_for_auto_run(order)
    assert any(r.startswith("CONTROL_PLANE_IN_SCOPE") for r in reasons), reasons
    with make_git_project() as proj:
        orch = _mock_orch()
        run_id = _record_human_required(orch, str(proj), "g", "APPROVAL_REQUIRED: " + "; ".join(reasons), order)
        dashboard_server._new_orchestrator = lambda: ApprovingOrch(_dep_builder)
        _assert_non_approvable(server, orch, run_id, "CONTROL_PLANE_IN_SCOPE")


def test_5b_git_in_order_scope_still_git_internals():
    reasons = SafetyPolicy.assess_order_for_auto_run(OrderSchema(goal="g", allowed_paths=[".git/config"]))
    assert any(r.startswith("GIT_INTERNALS_IN_SCOPE") for r in reasons)


# ------------------------------------------------------------------ 6-8: builder modifies control plane -> detected post-build, non-approvable

@pytest.mark.parametrize("rel,content", [
    (".agents/hooks.json", '{"hooks": {}}'),
    ("GEMINI.md", "# weakened rules\n"),
    (".director_worktrees/evil.txt", "x\n"),
    (".agents/permission_guard.py", "print('allow')\n"),
])
def test_6_8_control_plane_change_detected_post_build(approval_env, server, monkeypatch, rel, content):
    def cp_builder(b, o, w):
        BUILDER_CALLS.append(w)
        p = Path(w) / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content, encoding="utf-8")
        (Path(w) / "src" / "n.py").write_text("z=1\n", encoding="utf-8")
        return ["src/n.py", rel]
    with make_git_project() as proj:
        order = OrderSchema(goal="g", allowed_paths=["src/", "requirements.txt"], protected_paths=[".git/"])
        orch = _mock_orch()
        run_id = _record_human_required(orch, str(proj), "g", "APPROVAL_REQUIRED: SENSITIVE_FILE_IN_SCOPE (requirements.txt)", order)
        dashboard_server._new_orchestrator = lambda: ApprovingOrch(cp_builder)
        assert _post(server, "/run/approve", {"run_id": run_id, "request_id": "cp-" + rel})[1]["status"] == "accepted"
        assert _wait_done(orch, run_id) == "HUMAN_REQUIRED"
        last = orch.sm.get_transitions(run_id)[-1]["details"]
        assert "Control plane modified: " + rel.rstrip("/") in last, last
        st = orch.approval_state(run_id)
        assert st["stage"] == "POST_BUILD" and st["approvable"] is False and any("Control plane modified" in b for b in st["blocking"])
        assert _get_json(server, "/status")[1]["approval"]["required"] is False
        # a waiver can never cover it
        remaining, waived = SafetyPolicy.apply_waivers([f"Control plane modified: {rel}"], [{"gate": "DEPENDENCY_CONFIGURATION_MODIFIED", "file": rel}])
        assert waived == [] and remaining
        assert len(BUILDER_CALLS) == 1


def test_6b_scope_guard_direct_control_plane_and_runtime_artifacts(tmp_path):
    wt = tmp_path / "wt"; (wt / "src").mkdir(parents=True)
    order = OrderSchema(goal="g", allowed_paths=["src/"])
    ok, v = SafetyPolicy.validate_scope_post_build(order, [(1, 0, "src/a.py"), (2, 0, ".agents/hooks.json")], ["GEMINI.md", "src/__pycache__/x.pyc", ".pytest_cache/v/1"], "", [], worktree_path=str(wt))
    assert not ok and sorted(x for x in v if x.startswith("Control plane")) == ["Control plane modified: .agents/hooks.json", "Control plane modified: GEMINI.md"]
    assert not any("__pycache__" in x or ".pytest_cache" in x for x in v)           # runtime artifacts still excluded
    ok2, v2 = SafetyPolicy.validate_scope_post_build(order, [(1, 0, "src/a.py")], [], "", [".director_worktrees/RUN-1/x"], worktree_path=str(wt))
    assert not ok2 and "Control plane modified: .director_worktrees/RUN-1/x" in v2


# ------------------------------------------------------------------ 9: pre-write / hook / settings hard deny

def test_9_pre_write_and_hook_deny_control_plane(tmp_path):
    wt = tmp_path / "wt"; (wt / "src").mkdir(parents=True)
    order = OrderSchema(goal="g", allowed_paths=["src/", ".agents/", "GEMINI.md", ".git/"], protected_paths=[])
    for rel in (".git/config", ".agents/hooks.json", "GEMINI.md", ".director_worktrees/a/b", "src/.agents/x"):
        ok, why = SafetyPolicy.check_pre_tool_file_write(rel, str(wt), order.allowed_paths, order.protected_paths)
        assert not ok and ("control-plane" in why or "Forbidden" in why), (rel, why)
    ok, why = SafetyPolicy.check_pre_tool_file_write("src/ok.py", str(wt), order.allowed_paths, order.protected_paths)
    assert ok, why
    # settings and hook are generated from the same constant
    b = AntigravityBuilder(executable="python")
    scoped = b._setup_scoped_permissions(wt, order)
    for pat in SafetyPolicy.CONTROL_PLANE_PATTERNS:
        target = pat.replace("/**", "").replace("/*", "")
        assert f"write_file({target})" in scoped["deny_writes"]
    src = Path("agents/antigravity_builder.py").read_text(encoding="utf-8")
    assert "SafetyPolicy.CONTROL_PLANE_PATTERNS" in src and "control_dirs" in src and "Director control-plane path is never writable" in src
    for pat in SafetyPolicy.CONTROL_PLANE_PATTERNS:
        target = pat.replace("/**", "").replace("/*", "")
        assert f"write_file({target})" in AntigravityBuilder.SAFE_DEFAULT_SETTINGS["permissions"]["deny"]
    assert SafetyPolicy.is_control_plane_path(".git/x") and SafetyPolicy.is_control_plane_path("GEMINI.md") and not SafetyPolicy.is_control_plane_path("docs/GEMINI.md")


def test_9b_generated_hook_denies_control_plane(tmp_path):
    """Runs the actual generated permission-guard hook script against control-plane targets."""
    wt = tmp_path / "wt"; (wt / "src").mkdir(parents=True)
    b = AntigravityBuilder(executable="python")
    order = OrderSchema(goal="g", allowed_paths=["src/"])
    b._setup_scoped_permissions(wt, order)
    guard = wt / ".agents" / "permission_guard.py"
    import sys
    def run(target):
        payload = json.dumps({"toolCall": {"name": "write_to_file", "args": {"TargetFile": target}}})
        r = subprocess.run([sys.executable, str(guard)], input=payload, capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout.strip().splitlines()[-1])
    for t in (".agents/hooks.json", "GEMINI.md", ".git/config", ".director_worktrees/x", str(wt / ".agents" / "permission_guard.py")):
        assert run(t)["decision"] == "deny", t
    assert run("src/ok.py")["decision"] == "allow"


# ------------------------------------------------------------------ 10-12: requirements.txt flow intact, waiver narrow

def test_10_12_requirements_two_stage_still_works_and_waiver_narrow(approval_env, server, monkeypatch):
    with make_git_project() as proj:
        run_id, orch, state = _run_to_post_build_halt(server, proj)
        assert state == "HUMAN_REQUIRED" and orch.approval_state(run_id)["stage"] == "POST_BUILD"
        assert _post(server, "/run/approve", {"run_id": run_id, "request_id": "p2"})[1]["status"] == "accepted"
        assert _wait_done(orch, run_id) == "PASS" and len(BUILDER_CALLS) == 1
        assert [json.loads(a["payload"])["approval_stage"] for a in orch.sm.get_audit_logs(run_id) if a["action"] == "HUMAN_APPROVAL"] == ["ENTRY", "POST_BUILD"]
    remaining, waived = SafetyPolicy.apply_waivers(
        ["Dependency configuration modified: requirements.txt", "Dependency configuration modified: pyproject.toml", "Control plane modified: .agents/hooks.json", "Protected path modified: .env"],
        [{"gate": "DEPENDENCY_CONFIGURATION_MODIFIED", "file": "requirements.txt"}])
    assert waived == ["Dependency configuration modified: requirements.txt"] and len(remaining) == 3


# ------------------------------------------------------------------ builder-side snapshot (real CLI path, before cleanup)

def test_9c_builder_snapshot_detects_control_plane_edit(tmp_path):
    wt = tmp_path / "wt"; (wt / "src").mkdir(parents=True)
    b = AntigravityBuilder(executable="python")
    order = OrderSchema(goal="g", allowed_paths=["src/"])
    b._setup_scoped_permissions(wt, order)
    before = b._control_plane_snapshot(wt)
    assert any(k.startswith(".agents/") for k in before) and "GEMINI.md" in before
    assert b._control_plane_snapshot(wt) == before                        # deterministic, no drift by itself
    (wt / ".agents" / "hooks.json").write_text("{}", encoding="utf-8")
    (wt / ".director_worktrees").mkdir(); (wt / ".director_worktrees" / "x").write_text("x", encoding="utf-8")
    after = b._control_plane_snapshot(wt)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    assert changed == [".agents/hooks.json", ".director_worktrees/x"]
    src = Path("agents/antigravity_builder.py").read_text(encoding="utf-8")
    assert "CONTROL_PLANE_MODIFIED" in src and src.index("cp_after = self._control_plane_snapshot(wt)") < src.index("# Clean up temporary scoped permission rules")

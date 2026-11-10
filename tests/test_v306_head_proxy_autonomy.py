"""
v3.0.6 HEAD PROXY AUTONOMY + GOAL MISSIONS (user decision 2026-09-29): HEAD decides on the user's behalf; the run
only stops for money / credentials / irreversible external actions or genuine security violations. Everything else is
narrowed, retried, failed over or fed back into repair, and a request runs as a multi-milestone mission.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from agents.mock_head import MockHead
from core import autopilot
from core.mission import MissionRunner, MissionStore, missions_to_resume
from core.safety import SafetyPolicy
from core.state_machine import State
from schemas.builder_result import BuilderResultSchema
from schemas.decision import DecisionSchema
from schemas.order import OrderSchema
from schemas.review_result import ReviewResultSchema
from tests.test_v0234_resume_parity import _mock_orch

pytestmark = pytest.mark.head_proxy

PYTEST_CMD = "python -m pytest tests/"


# ------------------------------------------------------------------ helpers

def proxy_orch(max_repairs=2, **autonomy):
    auto = {"enabled": True, "max_repairs": max_repairs, "head_proxy": True, "builder_attempts": 3}
    auto.update(autonomy)
    return _mock_orch(bootstrap={"enabled": True}, autonomy=auto, verification={"test_command": PYTEST_CMD})


def quiet(monkeypatch, orch):
    monkeypatch.setattr(orch, "_claim_trial_for", lambda *a: None)
    monkeypatch.setattr(orch, "_provider_ready", lambda *a: True)
    monkeypatch.setattr(orch, "_record_provider_success", lambda *a: None)


def write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def good_build(calls, files=None):
    files = files or {"feature.py": "def double(x):\n    return x * 2\n",
                      "tests/test_feature.py": "from feature import double\n\ndef test_double():\n    assert double(4) == 8\n"}

    def build(order, worktree_path, builder_fn=None):
        calls.append(order.context or "")
        for rel, text in files.items():
            write(worktree_path, rel, text)
        return BuilderResultSchema(status="COMPLETED", files_modified=list(files), summary="built")
    return build


def review_green(monkeypatch, orch):
    def review(rid, o, v, *a):
        r = ReviewResultSchema(verdict="pass" if v["passed"] else "changes_required",
                               p1=[] if v["passed"] else ["tests fail"], summary="review")
        orch._save_json(rid, "review.json", r)          # like the real reviewer stage (resume reads it)
        return r, None
    monkeypatch.setattr(orch, "run_reviewer_with_failover", review)


def decide_by_tests(monkeypatch, orch):
    monkeypatch.setattr(orch, "run_decision_with_failover", lambda rid, o, b, v, r, *a: (DecisionSchema(
        decision="PASS" if v["passed"] and r.verdict == "pass" else "FAIL", rationale="evidence",
        next_steps=["fix: " + v.get("summary", "")]), None))


ORDER = dict(goal="double", allowed_paths=["feature.py", "tests/**"], context="original")


def git_project(path, files):
    path.mkdir(parents=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    for rel, text in files.items():
        write(path, rel, text)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, env=env)
    return str(path)


class ProxyHead:
    provider = "mock"

    def __init__(self, decision="APPROVE", raises=False):
        self.decision, self.raises, self.questions = decision, raises, []

    def proxy_decide(self, question, evidence, cwd=None):
        self.questions.append(question)
        if self.raises:
            raise RuntimeError("HEAD CLI unreachable")
        return {"decision": self.decision, "reason": "proxy says " + self.decision}


# ------------------------------------------------------------------ ENTRY gate

def test_entry_gate_narrows_scope_and_approves_high_risk_via_head(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    order = OrderSchema(goal="backtest a new strategy", risk_level="HIGH",
                        allowed_paths=[str(proj / "src"), ".git/hooks", "tests/**", "../outside"],
                        protected_paths=["../AITOS/**", ".env", "tests/**"])
    head = ProxyHead("APPROVE")
    gate = autopilot.entry_gate(order, str(proj), head)
    assert gate["escalate"] is None
    assert gate["order"].allowed_paths == ["src"]          # absolute->relative; .git, outside, self-protected dropped
    assert "../AITOS/**" not in gate["order"].protected_paths and ".env" in gate["order"].protected_paths
    assert head.questions and "HIGH_RISK" in head.questions[0]
    assert gate["notes"]


def test_entry_gate_escalates_money_decision_as_approvable_user_decision(tmp_path):
    order = OrderSchema(goal="subscribe to the paid data plan", risk_level="HIGH", allowed_paths=["src/**"])
    gate = autopilot.entry_gate(order, str(tmp_path), ProxyHead("ESCALATE"))
    assert gate["escalate"].startswith("APPROVAL_REQUIRED: USER_DECISION_REQUIRED")
    from dashboard_server import parse_approval_reason
    assert parse_approval_reason(gate["escalate"])["approvable"] is True


@pytest.mark.parametrize("goal,escalates", [("Build a backtest report generator", False),
                                            ("Set up Stripe payment for the ebook", True)])
def test_entry_gate_policy_fallback_when_head_unreachable(tmp_path, goal, escalates):
    order = OrderSchema(goal=goal, risk_level="HIGH", allowed_paths=["src/**"], protected_paths=["credentials/**"])
    gate = autopilot.entry_gate(order, str(tmp_path), ProxyHead(raises=True))
    assert (gate["escalate"] is not None) is escalates      # protected 'credentials/**' alone never escalates
    assert any("policy fallback" in n for n in gate["notes"])


def test_whole_repo_scope_is_spelled_as_glob_and_control_plane_stays_denied(tmp_path):
    order, notes = autopilot.sanitize_order(OrderSchema(goal="g", allowed_paths=["."]), str(tmp_path))
    assert order.allowed_paths == ["**"] and notes
    assert SafetyPolicy.is_path_allowed("src/deep/x.py", order.allowed_paths)
    for rel in (".git/config", ".agents/hooks.json", "GEMINI.md"):
        ok, _ = SafetyPolicy.check_pre_tool_file_write(rel, str(tmp_path), order.allowed_paths, [])
        assert not ok


def test_empty_scope_is_derived_from_required_outputs(tmp_path):
    order = OrderSchema(goal="g", allowed_paths=[], required_outputs=["src/tool.py", "tests/test_tool.py"])
    gate = autopilot.entry_gate(order, str(tmp_path), ProxyHead())
    assert gate["escalate"] is None and gate["order"].allowed_paths == ["src/tool.py", "tests/test_tool.py"]


# ------------------------------------------------------------------ classification helpers

def test_post_build_triage_fails_closed_on_unknown():
    t = autopilot.split_post_build(["Max diff lines exceeded: 1029 > 800", "Unauthorized file deletion: a.py",
                                    "Control plane modified: GEMINI.md", "Something new: x"])
    assert t["waivable"] == ["Max diff lines exceeded: 1029 > 800"]
    assert t["repairable"] == ["Unauthorized file deletion: a.py"]
    assert t["serious"] == ["Control plane modified: GEMINI.md", "Something new: x"]


def test_money_code_scan_reads_added_lines_only():
    diff = ("+++ b/src/live.py\n+    mt5.order_send(req)\n"
            "+++ b/src/old.py\n-    mt5.order_send(req)\n"
            "+++ b/src/pay.py\n+import stripe\n")
    assert autopilot.money_code_findings(diff, {"src/new.py": "x = create_market_buy_order('BTC')\n"}) == [
        "Live money code: src/live.py", "Live money code: src/new.py", "Live money code: src/pay.py"]
    assert SafetyPolicy.classify_post_build_violations(["Live money code: src/live.py"])["approvable"]


@pytest.mark.parametrize("diff,flagged", [
    ("-def foo(a):\n+def foo(a, b):\n", False),        # edited signature: still exists
    ("-class Old:\n+class New:\n", True),              # really removed
    ("-async def run():\n+def run():\n", False),
])
def test_symbol_deletion_detection_ignores_edits(diff, flagged):
    ok, v = SafetyPolicy.validate_scope_post_build(OrderSchema(goal="g", allowed_paths=["**"]), [(1, 1, "a.py")], [], diff)
    assert any("Unauthorized deletion" in x for x in v) is flagged


# ------------------------------------------------------------------ Builder resilience

def test_builder_retries_with_concrete_feedback(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    calls, good = [], None
    good = good_build(calls)

    def flaky(order, worktree_path, builder_fn=None):
        if "BUILDER RETRY NOTICE" not in (order.context or ""):
            calls.append(order.context)
            raise ValueError("Unterminated string starting at: line 25 column 18 (char 31122)")
        return good(order, worktree_path)
    monkeypatch.setattr(orch.builder, "run", flaky)
    review_green(monkeypatch, orch)
    decide_by_tests(monkeypatch, orch)
    out = orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER))
    assert out["final_state"] == State.PASS
    assert "not complete valid JSON" in calls[1]
    assert len(orch._load_json(out["run_id"], "builder_attempts.json")) == 1


def test_builder_moves_to_another_provider_on_the_last_attempt(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    seen, calls = [], []

    def broken(order, worktree_path, builder_fn=None):
        seen.append("primary")
        raise RuntimeError("GOOGLE_STREAM_EMPTY_RESPONSE")
    monkeypatch.setattr(orch.builder, "run", broken)

    class Backup:
        provider = "anthropic"
        run = staticmethod(good_build(calls))
    swaps = []

    def swap(role, run_id, logger, tried, stage, why):
        swaps.append((role, stage))
        orch.builder = Backup()
        return True
    monkeypatch.setattr(orch, "_swap_agent", swap)
    review_green(monkeypatch, orch)
    decide_by_tests(monkeypatch, orch)
    out = orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER))
    assert out["final_state"] == State.PASS
    assert seen == ["primary", "primary"] and swaps == [("builder", "BUILDING")] and len(calls) == 1


# ------------------------------------------------------------------ HEAD proxy decisions

def test_non_serious_head_escalation_becomes_a_repair(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    calls = []
    monkeypatch.setattr(orch.builder, "run", good_build(calls))
    review_green(monkeypatch, orch)
    decisions = iter([DecisionSchema(decision="HUMAN_REQUIRED", rationale="Unclear whether to use JSON or CSV output",
                                     next_steps=["Use JSON output"]),
                      DecisionSchema(decision="PASS", rationale="done", next_steps=[])])
    monkeypatch.setattr(orch, "run_decision_with_failover", lambda *a: (next(decisions), None))
    out = orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER))
    assert out["final_state"] == State.PASS and len(calls) == 2
    assert "Use JSON output" in calls[1]
    assert "HEAD_ESCALATION_RESOLVED_BY_PROXY" in [a["action"] for a in orch.sm.get_audit_logs(out["run_id"])]


def test_serious_head_escalation_still_stops(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    calls = []
    monkeypatch.setattr(orch.builder, "run", good_build(calls))
    review_green(monkeypatch, orch)
    monkeypatch.setattr(orch, "run_decision_with_failover", lambda *a: (DecisionSchema(
        decision="HUMAN_REQUIRED", rationale="USER_DECISION: this requires buying a paid API key", next_steps=[]), None))
    out = orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER))
    assert out["final_state"] == State.HUMAN_REQUIRED and len(calls) == 1


def test_unresolved_review_after_budget_is_fail_not_human(tmp_path, monkeypatch):
    orch = proxy_orch(max_repairs=2)
    quiet(monkeypatch, orch)
    calls = []
    monkeypatch.setattr(orch.builder, "run", good_build(calls))
    monkeypatch.setattr(orch, "run_reviewer_with_failover", lambda *a: (ReviewResultSchema(
        verdict="changes_required", p1=["still a real defect"], summary="still not happy"), None))
    monkeypatch.setattr(orch, "run_decision_with_failover", lambda *a: (DecisionSchema(
        decision="PASS", rationale="looks fine", next_steps=[]), None))
    out = orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER))
    assert out["final_state"] == State.FAIL and len(calls) == 3


def test_same_family_head_and_reviewer_pass_with_recorded_note(monkeypatch):
    orch = proxy_orch()

    class A:
        provider = "claude"
    orch.head, orch.reviewer = A(), A()
    d = DecisionSchema(decision="PASS", rationale="green", next_steps=[])
    import logging
    state = orch.apply_final_pass_gate(d, ReviewResultSchema(verdict="pass", summary="ok"), logging.getLogger("t"))
    assert state == State.PASS and "REDUCED_INDEPENDENCE" in d.rationale


# ------------------------------------------------------------------ Scope Guard re-routing

def test_money_code_stops_before_tests_run(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    calls = []
    files = {"feature.py": "import MetaTrader5 as mt5\n\ndef buy(req):\n    return mt5.order_send(req)\n",
             "tests/test_feature.py": "def test_x():\n    assert True\n"}
    monkeypatch.setattr(orch.builder, "run", good_build(calls, files))
    review_green(monkeypatch, orch)
    decide_by_tests(monkeypatch, orch)
    out = orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER))
    assert out["final_state"] == State.HUMAN_REQUIRED
    assert orch._load_json(out["run_id"], "verification.json") is None       # code never executed
    st = orch.approval_state(out["run_id"])
    assert st["stage"] == "POST_BUILD" and st["approvable"] is True


def test_cap_overrun_is_waived_by_head_proxy(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    calls = []
    monkeypatch.setattr(orch.builder, "run", good_build(calls))
    review_green(monkeypatch, orch)
    decide_by_tests(monkeypatch, orch)
    out = orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER, max_files_changed=1))
    assert out["final_state"] == State.PASS
    assert "HEAD_PROXY_WAIVER" in [a["action"] for a in orch.sm.get_audit_logs(out["run_id"])]


def test_deleted_function_goes_back_to_the_builder(tmp_path, monkeypatch):
    proj = git_project(tmp_path / "proj", {"keep.py": "def keep():\n    return 1\n",
                                           "tests/test_keep.py": "from keep import keep\n\ndef test_keep():\n    assert keep() == 1\n"})
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    calls = []

    def build(order, worktree_path, builder_fn=None):
        calls.append(order.context or "")
        if len(calls) == 1:
            write(worktree_path, "keep.py", "def other():\n    return 2\n")
        else:
            write(worktree_path, "keep.py", "def keep():\n    return 1\n\ndef other():\n    return 2\n")
        write(worktree_path, "tests/test_other.py", "from keep import other\n\ndef test_other():\n    assert other() == 2\n")
        return BuilderResultSchema(status="COMPLETED", files_modified=["keep.py"], summary="b")
    monkeypatch.setattr(orch.builder, "run", build)
    review_green(monkeypatch, orch)
    decide_by_tests(monkeypatch, orch)
    out = orch.run(proj, "add other", order=OrderSchema(goal="add other", allowed_paths=["keep.py", "tests/**"]))
    assert out["final_state"] == State.PASS and len(calls) == 2
    assert "SCOPE_REPAIR_REQUIRED" in calls[1]


def test_preexisting_baseline_failure_does_not_block(tmp_path, monkeypatch):
    proj = git_project(tmp_path / "proj", {"tests/test_old.py": "def test_old():\n    assert False\n"})
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    calls = []
    monkeypatch.setattr(orch.builder, "run", good_build(calls))
    review_green(monkeypatch, orch)
    decide_by_tests(monkeypatch, orch)
    out = orch.run(proj, "double", order=OrderSchema(**ORDER))
    assert out["final_state"] == State.PASS
    ev = orch._load_json(out["run_id"], "final_evidence.json")
    assert ev["baseline"] == "PREEXISTING_FAILURES"
    assert orch._load_json(out["run_id"], "verification.json")["preexisting_failures"] == ["tests/test_old.py::test_old"]


# ------------------------------------------------------------------ reviewer / HEAD provider resilience

def test_reviewer_is_retried_then_replaced(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    calls = []
    monkeypatch.setattr(orch.builder, "run", good_build(calls))
    attempts = []

    def crash(*a, **k):
        attempts.append(1)
        raise RuntimeError("reviewer crashed")
    monkeypatch.setattr(orch.reviewer, "review", crash)

    class Backup:
        provider = "anthropic"

        def review(self, *a, **k):
            return ReviewResultSchema(verdict="pass", summary="ok")

    def swap(role, run_id, logger, tried, stage, why):
        orch.reviewer = Backup()
        return True
    monkeypatch.setattr(orch, "_swap_agent", swap)
    monkeypatch.setattr(orch, "_reviewer_provenance", lambda *a: None)
    decide_by_tests(monkeypatch, orch)
    out = orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER))
    assert out["final_state"] == State.PASS and len(attempts) == 2


def test_unreachable_head_falls_back_to_evidence_decision(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    monkeypatch.setattr(orch.builder, "run", good_build([]))
    review_green(monkeypatch, orch)
    monkeypatch.setattr(orch.head, "decide", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("usage limit reached")))
    monkeypatch.setattr(orch, "_swap_agent", lambda *a: False)
    out = orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER))
    assert out["final_state"] == State.PASS
    assert "DECISION_FALLBACK" in [a["action"] for a in orch.sm.get_audit_logs(out["run_id"])]


def test_role_assignment_replaces_unusable_provider_visibly(monkeypatch):
    orch = proxy_orch()
    orch.sm.create_run("RUN-ROLES", "x", "g")
    monkeypatch.setattr(orch.pm, "usable_domains", lambda candidates=None: ["ANTHROPIC", "GOOGLE"])
    import logging
    a = orch._autonomous_role_assignment("claude", "antigravity", "openai", "RUN-ROLES", logging.getLogger("t"))
    assert a["can_proceed"] and a["reviewer_provider"] in ("claude", "antigravity") and a["head_provider"] == "claude"
    assert "FAILOVER" in [x["action"] for x in orch.sm.get_audit_logs("RUN-ROLES")]
    monkeypatch.setattr(orch.pm, "usable_domains", lambda candidates=None: [])
    assert orch._autonomous_role_assignment("claude", "antigravity", "openai", "RUN-ROLES", logging.getLogger("t"))["can_proceed"] is False


# ------------------------------------------------------------------ missions

STEP2 = {"goal": "quad", "allowed_paths": ["feature2.py", "tests/**"], "acceptance_criteria": ["quad"],
         "required_outputs": ["feature2.py"], "risk_level": "MEDIUM"}
STEP2_FILES = {"feature2.py": "from feature import double\n\ndef quad(x):\n    return double(double(x))\n",
               "tests/test_feature2.py": "from feature2 import quad\n\ndef test_quad():\n    assert quad(1) == 4\n"}


def mission_factory(monkeypatch, plans, builds):
    def factory():
        orch = proxy_orch()
        quiet(monkeypatch, orch)

        def build(order, worktree_path, builder_fn=None):
            files = STEP2_FILES if order.goal == "quad" else None
            return good_build(builds, files)(order, worktree_path)
        monkeypatch.setattr(orch.builder, "run", build)
        review_green(monkeypatch, orch)
        decide_by_tests(monkeypatch, orch)
        return orch
    monkeypatch.setattr(MockHead, "plan_next", lambda self, goal, history, ctx, cwd=None: plans.pop(0))
    return factory


def test_mission_builds_milestones_on_each_other_until_done(tmp_path, monkeypatch):
    builds, messages = [], []
    plans = [{"status": "CONTINUE", "summary": "double done", "reason": "next", "order": STEP2},
             {"status": "DONE", "summary": "double and quad work", "reason": "goal met", "order": None}]
    runner = MissionRunner(mission_factory(monkeypatch, plans, builds), MissionStore(tmp_path / "missions"),
                           notify=lambda text, run_id=None, role="SYSTEM": messages.append((role, text)))
    m = runner.create(str(tmp_path / "proj"), "double then quad", OrderSchema(**ORDER), config={"autonomy": {"max_steps": 4}})
    done = runner.run(m["mission_id"])
    assert done["status"] == "DONE" and len(done["steps"]) == 2
    assert [s["result"]["final_state"] for s in done["steps"]] == ["PASS", "PASS"]
    step2 = Path(done["steps"][1]["result"]["worktree_path"])
    assert (step2 / "feature.py").exists() and (step2 / "feature2.py").exists()   # built on step 1
    assert done["base_worktree"] == str(step2) or Path(done["base_worktree"]) == step2
    assert any(role == "HEAD" and "미션 완료" in text for role, text in messages)
    # v3.2: the finished deliverable is applied to the project (autonomy.auto_apply defaults to on_pass)
    assert (tmp_path / "proj" / "feature.py").exists() and (tmp_path / "proj" / "feature2.py").exists()
    assert done["applied"]["status"] == "applied" and "적용했습니다" in messages[-1][1]


def test_mission_blocks_only_on_a_delegated_exception(tmp_path, monkeypatch):
    builds = []
    plans = [{"status": "CONTINUE", "summary": "", "reason": "",
              "order": dict(STEP2, goal="connect the live account", risk_level="HIGH")}]
    factory = mission_factory(monkeypatch, plans, builds)
    monkeypatch.setattr(MockHead, "proxy_decide", lambda self, q, e, cwd=None: {"decision": "ESCALATE", "reason": "live account"})
    runner = MissionRunner(factory, MissionStore(tmp_path / "missions"))
    m = runner.create(str(tmp_path / "proj"), "g", OrderSchema(**ORDER), config={})
    done = runner.run(m["mission_id"])
    assert done["status"] == "BLOCKED" and "USER_DECISION_REQUIRED" in done["blocked_reason"]
    orch = factory()
    st = orch.approval_state(done["steps"][-1]["run_id"])
    assert st["approvable"] is True and st["stage"] == "ENTRY"


def test_interrupted_mission_resumes_after_restart(tmp_path, monkeypatch):
    builds = []
    plans = [{"status": "DONE", "summary": "ok", "reason": "", "order": None}]
    factory = mission_factory(monkeypatch, plans, builds)
    store = MissionStore(tmp_path / "missions")
    runner = MissionRunner(factory, store)
    m = runner.create(str(tmp_path / "proj"), "g", OrderSchema(**ORDER), config={})
    # crash after the run id was reserved but before the pipeline started, owned by a process that is gone
    rid = factory().reserve_run_id(str(tmp_path / "proj"), "double")
    m["steps"][0]["run_id"], m["owner_pid"] = rid, 999999
    store.save(m)
    assert [x["mission_id"] for x in missions_to_resume(store)] == [m["mission_id"]]
    done = runner.run(m["mission_id"])
    assert done["status"] == "DONE" and done["steps"][0]["run_id"] == rid and len(builds) == 1
    assert missions_to_resume(store) == []


# ------------------------------------------------------------------ dashboard wiring

def test_chat_request_starts_a_mission_without_human_approval(monkeypatch):
    import dashboard_server
    from tests.test_v0231_chat_stabilization import chat_env, make_git_project, HeadStub, _install_head, _order  # noqa: F401
    from core.chat_manager import ChatManager
    from core.provider_manager import ProviderManager
    monkeypatch.setattr(ProviderManager, "check_health", lambda self, force=False: self.status_map.copy())
    dashboard_server._ACTIVE_PROJECTS.clear()
    started = []
    monkeypatch.setattr(dashboard_server, "_run_mission_thread", lambda sid, mid, key, reopen=False: started.append(mid))
    with make_git_project() as proj:
        # HIGH risk + absolute path + whole-repo scope: all used to stop for a human
        _install_head(monkeypatch, HeadStub(_order(risk_level="HIGH", allowed_paths=[str(Path(proj) / "src"), "."])))
        ChatManager().set_project(dashboard_server.active_session_id(), proj)
        dashboard_server.handle_chat_message("build the backtest report")
        missions = MissionStore().all()
    assert len(missions) == 1 and started == [missions[0]["mission_id"]]
    first = missions[0]["steps"][0]["order"]
    assert first["allowed_paths"] == ["src", "**"] and missions[0]["goal"] == "build the backtest report"
    texts = [m["content"] for m in ChatManager().get_messages(dashboard_server.active_session_id())]
    assert not any(t.startswith("HUMAN_REQUIRED") for t in texts) and any("미션 시작" in t for t in texts)
    dashboard_server._ACTIVE_PROJECTS.clear()


def test_server_start_resumes_orphaned_missions(tmp_path, monkeypatch):
    import dashboard_server
    dashboard_server._ACTIVE_PROJECTS.clear()
    store = MissionStore()
    runner = MissionRunner(lambda: None, store)
    m = runner.create(str(tmp_path / "proj"), "g", OrderSchema(**ORDER), config={})
    m["owner_pid"] = 999999
    store.save(m)
    done = runner.create(str(tmp_path / "other"), "g", OrderSchema(**ORDER), config={})
    done["status"] = "DONE"
    store.save(done)
    started = []
    monkeypatch.setattr(dashboard_server, "_run_mission_thread", lambda sid, mid, key, reopen=False: started.append(mid))
    assert dashboard_server.resume_interrupted_missions() == [m["mission_id"]] == started
    dashboard_server._ACTIVE_PROJECTS.clear()


def test_approved_blocked_step_continues_the_mission_on_previous_work(tmp_path, monkeypatch):
    builds = []
    plans = [{"status": "CONTINUE", "summary": "", "reason": "", "order": dict(STEP2, risk_level="HIGH")},
             {"status": "DONE", "summary": "ok", "reason": "", "order": None}]
    factory = mission_factory(monkeypatch, plans, builds)
    monkeypatch.setattr(MockHead, "proxy_decide", lambda self, q, e, cwd=None: {"decision": "ESCALATE", "reason": "needs user"})
    runner = MissionRunner(factory, MissionStore(tmp_path / "missions"))
    m = runner.create(str(tmp_path / "proj"), "g", OrderSchema(**ORDER), config={})
    blocked = runner.run(m["mission_id"])
    assert blocked["status"] == "BLOCKED"
    rid = blocked["steps"][-1]["run_id"]
    approver = factory()
    assert approver.approve_and_resume(rid)["final_state"] == State.PASS          # the user's dashboard click
    assert (Path(approver.sm.get_run(rid)["worktree_path"]) / "feature.py").exists()   # built on milestone 1
    runner.reopen(m["mission_id"])
    done = runner.run(m["mission_id"])
    assert done["status"] == "DONE"
    assert [s["result"]["final_state"] for s in done["steps"]] == ["PASS", "PASS"]


def _review_sequence(monkeypatch, orch, verdicts):
    seq = iter(verdicts)

    def review(rid, o, v, *a):
        verdict = next(seq, "pass")
        r = ReviewResultSchema(verdict=verdict, p2=["polish"] if verdict != "pass" else [], summary="r")
        orch._save_json(rid, "review.json", r)
        return r, None
    monkeypatch.setattr(orch, "run_reviewer_with_failover", review)


def test_files_written_by_the_test_run_are_not_builder_changes(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    calls = []
    files = {"feature.py": "def double(x):\n    return x * 2\n",
             "tests/test_feature.py": "import json, pathlib\nfrom feature import double\n\n"
                                      "def test_double():\n    pathlib.Path('ledger.json').write_text(json.dumps({'v': double(2)}))\n"
                                      "    assert double(4) == 8\n"}
    monkeypatch.setattr(orch.builder, "run", good_build(calls, files))
    _review_sequence(monkeypatch, orch, ["changes_required", "pass"])     # forces a second scope check
    decide_by_tests(monkeypatch, orch)
    out = orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER))
    assert out["final_state"] == State.PASS and len(calls) == 2
    run_dir = orch._get_run_dir(out["run_id"])
    assert (run_dir / "verification_output" / "ledger.json").exists()          # preserved as evidence
    wt = Path(orch.sm.get_run(out["run_id"])["worktree_path"])
    assert not (wt / "ledger.json").exists()                                   # not presented as a deliverable
    assert "ledger.json" not in (orch._load_json(out["run_id"], "review.json") or {}).get("summary", "")


def test_test_run_writing_into_the_control_plane_is_still_stopped(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    calls = []
    files = {"feature.py": "def double(x):\n    return x * 2\n",
             "tests/test_feature.py": "import pathlib\nfrom feature import double\n\n"
                                      "def test_double():\n    pathlib.Path('.agents').mkdir(exist_ok=True)\n"
                                      "    pathlib.Path('.agents/hooks.json').write_text('{}')\n    assert double(4) == 8\n"}
    monkeypatch.setattr(orch.builder, "run", good_build(calls, files))
    _review_sequence(monkeypatch, orch, ["changes_required", "pass"])
    decide_by_tests(monkeypatch, orch)
    out = orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER))
    assert out["final_state"] == State.HUMAN_REQUIRED
    assert "Control plane modified" in orch.sm.get_transitions(out["run_id"])[-1]["details"]


def test_non_exception_halt_makes_the_mission_replan_not_block(tmp_path, monkeypatch):
    builds = []
    plans = [{"status": "CONTINUE", "summary": "retry core", "reason": "", "order": dict(ORDER)},
             {"status": "DONE", "summary": "ok", "reason": "", "order": None}]
    factory = mission_factory(monkeypatch, plans, builds)
    runner = MissionRunner(factory, MissionStore(tmp_path / "missions"))
    m = runner.create(str(tmp_path / "proj"), "g", OrderSchema(**ORDER), config={})
    orch = factory()
    rid = orch._create_run_with_retry(None, str(tmp_path / "proj"), "double")
    orch.sm.transition_to(rid, State.HUMAN_REQUIRED, details="EVIDENCE_MISSING: VERIFICATION_FAILED")
    m["steps"][0]["run_id"] = rid
    m["steps"][0]["result"] = runner._evidence(orch, rid)
    runner.store.save(m)
    done = runner.run(m["mission_id"])
    assert done["status"] == "DONE"
    assert done["steps"][0]["result"]["treated_as"].startswith("FAIL")
    assert done["steps"][1]["result"]["final_state"] == "PASS"


def _mission_with_head_stop(tmp_path, monkeypatch, detail, verdict):
    builds, reviews = [], []
    plans = [{"status": "CONTINUE", "summary": "fix fixture too", "reason": "", "order": dict(ORDER)},
             {"status": "DONE", "summary": "ok", "reason": "", "order": None}]
    factory = mission_factory(monkeypatch, plans, builds)
    monkeypatch.setattr(MockHead, "proxy_decide",
                        lambda self, q, e, cwd=None: reviews.append(e["stop_detail"]) or {"decision": verdict, "reason": "r"})
    runner = MissionRunner(factory, MissionStore(tmp_path / "missions"))
    m = runner.create(str(tmp_path / "proj"), "g", OrderSchema(**ORDER), config={})
    orch = factory()
    rid = orch._create_run_with_retry(None, str(tmp_path / "proj"), "double")
    orch.sm.transition_to(rid, State.HUMAN_REQUIRED, details=detail)
    m["steps"][0]["run_id"] = rid
    m["steps"][0]["result"] = runner._evidence(orch, rid)
    runner.store.save(m)
    return runner.run(m["mission_id"]), reviews


@pytest.mark.head_proxy
def test_head_user_decision_on_an_order_conflict_is_replanned(tmp_path, monkeypatch):
    detail = ("Final decision: USER_DECISION: This order cannot be met as written: protected tests share a 1x1 PNG "
              "fixture with a corrupt CRC")
    done, reviews = _mission_with_head_stop(tmp_path, monkeypatch, detail, "APPROVE")
    assert reviews and done["status"] == "DONE"
    assert "not a delegated exception" in done["steps"][0]["result"]["treated_as"]
    assert done["steps"][0]["user_decision_review"] == "APPROVE"


@pytest.mark.head_proxy
def test_head_user_decision_blocks_when_the_proxy_escalates_or_money_is_involved(tmp_path, monkeypatch):
    done, reviews = _mission_with_head_stop(tmp_path, monkeypatch, "Final decision: USER_DECISION: unclear", "ESCALATE")
    assert done["status"] == "BLOCKED" and reviews
    done, reviews = _mission_with_head_stop(tmp_path / "b", monkeypatch,
                                            "Final decision: USER_DECISION: this needs a paid plan", "APPROVE")
    assert done["status"] == "BLOCKED" and not reviews                 # money: never second-guessed


def test_resumed_run_with_failed_verification_continues_to_repair(tmp_path, monkeypatch):
    class Crash(BaseException):
        pass
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    wrong = {"feature.py": "def double(x):\n    return x * 3\n",
             "tests/test_feature.py": "from feature import double\n\ndef test_double():\n    assert double(4) == 8\n"}
    monkeypatch.setattr(orch.builder, "run", good_build([], wrong))
    monkeypatch.setattr(orch, "run_reviewer_with_failover", lambda *a: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):                                      # process dies during review (e.g. reboot)
        orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER))
    rid = orch.sm.list_runs()[0]["run_id"]
    assert orch.sm.get_run(rid)["current_state"] == State.REVIEWING
    orch2 = proxy_orch()
    quiet(monkeypatch, orch2)
    calls = []
    monkeypatch.setattr(orch2.builder, "run", good_build(calls))
    review_green(monkeypatch, orch2)
    decide_by_tests(monkeypatch, orch2)
    out = orch2.resume(rid)
    assert out["final_state"] == State.PASS and len(calls) == 1      # failed tests -> review -> HEAD repair -> PASS


def test_repo_relative_reference_paths_are_dropped_absolute_kept(tmp_path):
    doc = tmp_path / "spec.md"
    doc.write_text("spec", encoding="utf-8")
    order, notes = autopilot.sanitize_order(
        OrderSchema(goal="g", allowed_paths=["ledger/**"], reference_paths=["ledger/core.py", str(doc)]), str(tmp_path))
    assert order.reference_paths == [str(doc)] and any("reference_paths" in n for n in notes)


def test_follow_up_mission_continues_from_the_last_deliverable(tmp_path):
    from core.mission import latest_deliverable
    store = MissionStore(tmp_path / "missions")
    runner = MissionRunner(lambda: None, store)
    old = runner.create(str(tmp_path / "proj"), "first", OrderSchema(**ORDER), config={})
    deliverable = tmp_path / "proj" / ".director_worktrees" / "RUN-OLD"
    deliverable.mkdir(parents=True)
    old.update(status="DONE", base_worktree=str(deliverable))
    store.save(old)
    assert latest_deliverable(str(tmp_path / "proj"), store) == str(deliverable)
    new = runner.create(str(tmp_path / "proj"), "now add export", OrderSchema(**ORDER), config={})
    assert new["base_worktree"] == new["continued_from"] == str(deliverable)
    fresh = runner.create(str(tmp_path / "other"), "x", OrderSchema(**ORDER), config={})
    assert fresh["base_worktree"] is None


def test_verification_does_not_inherit_the_directors_io_encoding(tmp_path, monkeypatch):
    from core.verifier import Verifier
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    monkeypatch.setenv("PYTHONUTF8", "1")
    (tmp_path / "probe.py").write_text("import os\nprint(os.environ.get('PYTHONIOENCODING'), os.environ.get('PYTHONUTF8'))\n")
    res = Verifier._execute_cmd(["python", "probe.py"], tmp_path)
    assert res["output"].strip().endswith("None None")


# ------------------------------------------------------------------ MQL5 projects

def test_project_verification_override_applies_only_to_that_project(tmp_path):
    from core.bootstrap import configure_runtime
    proj = tmp_path / "ea_project"
    proj.mkdir()
    cfg = {"verification": {"build_command": None, "test_command": "python -m pytest tests/"},
           "projects": {str(proj): {"verification": {"build_command": ["python", "compile.py"], "test_command": "python -m pytest -q"}}}}
    got = configure_runtime(cfg, str(proj))["verification"]
    assert got == {"build_command": ["python", "compile.py"], "test_command": "python -m pytest -q"}
    assert configure_runtime(cfg, str(tmp_path))["verification"]["build_command"] is None
    assert cfg["verification"]["build_command"] is None                     # original config not mutated


def test_mql5_sources_carry_forward_between_milestones(tmp_path):
    from core.bootstrap import snapshot, git
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "ea").mkdir()
    (proj / "ea" / "Bot.mq5").write_text("int OnInit(){return 0;}\n", encoding="utf-8")
    (proj / "ea" / "Lib.mqh").write_text("// lib\n", encoding="utf-8")
    ref = snapshot(proj)
    assert {"ea/Bot.mq5", "ea/Lib.mqh"} <= set(git(proj, "ls-tree", "-r", "--name-only", ref).splitlines())


def test_ea_order_code_is_compiled_not_stopped_but_python_order_code_still_stops():
    diff = ("+++ b/ea/SampleEA.mq5\n+   trade.OrderSend(req, res);\n+   OrderSend(req, res);\n"
            "+++ b/backend/live.py\n+    mt5.order_send(req)\n")
    assert autopilot.money_code_findings(diff) == ["Live money code: backend/live.py"]


def test_head_can_continue_from_a_failed_but_useful_attempt(tmp_path, monkeypatch):
    builds = []
    wrong = {"feature.py": "def double(x):\n    return x * 2\n",
             "tests/test_feature.py": "from feature import double\n\ndef test_double():\n    assert double(4) == 8\n"}
    plans = [{"status": "CONTINUE", "summary": "keep it", "reason": "tests pass, review nit", "build_on_last_attempt": True,
              "order": STEP2},
             {"status": "DONE", "summary": "ok", "reason": "", "order": None}]

    def factory():
        orch = proxy_orch(max_repairs=0)
        quiet(monkeypatch, orch)

        def build(order, worktree_path, builder_fn=None):
            return good_build(builds, STEP2_FILES if order.goal == "quad" else wrong)(order, worktree_path)
        monkeypatch.setattr(orch.builder, "run", build)
        verdicts = iter(["changes_required"]) if not builds else iter([])
        monkeypatch.setattr(orch, "run_reviewer_with_failover", lambda rid, o, v, *a: (
            orch._save_json(rid, "review.json", ReviewResultSchema(verdict=next(verdicts, "pass"), summary="r")) or
            ReviewResultSchema(verdict="changes_required" if o.goal == "double" else "pass", summary="r"), None))
        decide_by_tests(monkeypatch, orch)
        return orch
    monkeypatch.setattr(MockHead, "plan_next", lambda self, goal, history, ctx, cwd=None: plans.pop(0))
    runner = MissionRunner(factory, MissionStore(tmp_path / "missions"))
    m = runner.create(str(tmp_path / "proj"), "g", OrderSchema(**ORDER), config={})
    done = runner.run(m["mission_id"])
    assert done["status"] == "DONE"
    assert [s["result"]["final_state"] for s in done["steps"]] == ["FAIL", "PASS"]
    attempt = done["steps"][0]["result"]["worktree_path"]
    assert done["steps"][1]["builds_on_failed_attempt"] == attempt
    assert (Path(done["steps"][1]["result"]["worktree_path"]) / "feature.py").exists()   # step 1's code carried over


@pytest.mark.parametrize("entry,path", [
    ("backend/vision_supervisor.py (constructor degrade, safe audit writes)", "backend/vision_supervisor.py"),
    ("tests/test_x.py - new tests for timeouts", "tests/test_x.py"),
    ("src/plain.py", "src/plain.py"),
    ("../escape.py (note)", None),                                        # never a usable deliverable
    ("docs/SPEC.md with the EA publication-order rules", "docs/SPEC.md"),
    ("pytest summary line and git diff --numstat report", None),          # prose, not a file
    ("behavior tests for the truncated-PNG path, in backend/tests/x.py", None),
])
def test_annotated_required_outputs_are_read_as_paths(entry, path):
    assert autopilot.required_output_path(entry) == path


def test_annotated_required_output_does_not_fail_verification(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    monkeypatch.setattr(orch.builder, "run", good_build([]))
    review_green(monkeypatch, orch)
    decide_by_tests(monkeypatch, orch)
    order = OrderSchema(**ORDER, required_outputs=["feature.py (double implementation)", "tests/test_feature.py - tests"])
    out = orch.run(str(tmp_path / "proj"), "double", order=order)
    assert out["final_state"] == State.PASS
    assert orch._load_json(out["run_id"], "verification.json")["required_outputs_missing"] == []


def test_failures_head_keeps_building_on_do_not_end_the_mission(tmp_path, monkeypatch):
    builds = []
    wrong = {"feature.py": "def double(x):\n    return x * 3\n",
             "tests/test_feature.py": "from feature import double\n\ndef test_double():\n    assert double(4) == 8\n"}
    keep = {"status": "CONTINUE", "summary": "", "reason": "progress", "build_on_last_attempt": True, "order": dict(ORDER)}
    plans = [dict(keep), dict(keep), dict(keep), {"status": "DONE", "summary": "ok", "reason": "", "order": None}]

    def factory():
        orch = proxy_orch(max_repairs=0)
        quiet(monkeypatch, orch)
        monkeypatch.setattr(orch.builder, "run", lambda o, w, builder_fn=None: good_build(builds, wrong if len(builds) < 3 else None)(o, w))
        review_green(monkeypatch, orch)
        decide_by_tests(monkeypatch, orch)
        return orch
    monkeypatch.setattr(MockHead, "plan_next", lambda self, goal, history, ctx, cwd=None: plans.pop(0))
    runner = MissionRunner(factory, MissionStore(tmp_path / "missions"))
    m = runner.create(str(tmp_path / "proj"), "g", OrderSchema(**ORDER), config={"autonomy": {"max_steps": 5}})
    done = runner.run(m["mission_id"])
    assert [s["result"]["final_state"] for s in done["steps"]] == ["FAIL", "FAIL", "FAIL", "PASS"]
    assert done["status"] == "DONE"


def test_only_minor_review_findings_pass_with_recorded_follow_ups(tmp_path, monkeypatch):
    import logging
    orch = proxy_orch()
    d = DecisionSchema(decision="PASS", rationale="code correct", next_steps=[])
    state = orch.apply_final_pass_gate(d, ReviewResultSchema(verdict="changes_required", p2=["doc wording"], summary="s"),
                                       logging.getLogger("t"))
    assert state == State.PASS and "P2 FOLLOW-UPS: doc wording" in d.rationale
    d2 = DecisionSchema(decision="PASS", rationale="x", next_steps=[])
    assert orch.apply_final_pass_gate(d2, ReviewResultSchema(verdict="changes_required", p1=["real bug"], summary="s"),
                                      logging.getLogger("t")) == State.FAIL


def test_empty_proposal_is_only_accepted_in_a_correction_round(tmp_path, monkeypatch):
    import sys
    from agents.proposal_builder import ProposalBuilder
    from core.research_runner import TriadResearchRunner
    monkeypatch.setattr(TriadResearchRunner, "invoke", lambda *a: dict(output=json.dumps({"files": [], "summary": "already fixed"}),
                                                                       invocation="t", exit_code=0, duration_sec=0))
    builder = ProposalBuilder(executable=sys.executable)
    repair = OrderSchema(goal="g", allowed_paths=["src/**"], context='x\nHEAD REPAIR ORDER: {"round": 1}')
    res = builder.run(repair, str(tmp_path))
    assert res.status == "COMPLETED" and res.files_modified == [] and "already fixed" in res.summary
    with pytest.raises(ValueError):
        builder.run(OrderSchema(goal="g", allowed_paths=["src/**"]), str(tmp_path))


def test_provider_that_failed_twice_this_run_is_not_rotated_back(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    orch.sm.create_run("RUN-HIST", str(tmp_path), "g")
    orch._save_json("RUN-HIST", "builder_attempts.json", [
        {"attempt": 1, "provider": "GOOGLE", "category": "UNKNOWN", "error": "GOOGLE_STREAM_EMPTY_RESPONSE"},
        {"attempt": 2, "provider": "GOOGLE", "category": "UNKNOWN", "error": "GOOGLE_STREAM_EMPTY_RESPONSE"}])
    swaps = []

    class Other:
        provider = "openai"
        run = staticmethod(good_build([]))

    def swap(role, run_id, logger, tried, stage, why):
        swaps.append(set(tried))
        orch.builder = Other()
        return True
    monkeypatch.setattr(orch, "_swap_agent", swap)
    import logging
    (tmp_path / "tests").mkdir()
    res, stop = orch._run_builder_autonomous("RUN-HIST", OrderSchema(**ORDER), str(tmp_path), logging.getLogger("t"), [])
    assert stop is None and res.status == "COMPLETED"
    assert swaps and "GOOGLE" in swaps[0]                                   # swapped away before the first attempt


def test_files_the_builder_must_edit_are_always_in_its_context(tmp_path, monkeypatch):
    import sys
    from agents.proposal_builder import ProposalBuilder
    from core.research_runner import TriadResearchRunner
    for i in range(4):                                                         # 4 x 60 KB of unrelated sources
        write(tmp_path, f"backend/big_{i}.py", "x = 1  # " + "a" * 60_000 + "\n")
    write(tmp_path, "docs/SPEC.md", "UNIQUE-SPEC-MARKER\n")
    write(tmp_path, "ea/Bot.mq5", "// UNIQUE-EA-MARKER\n")
    seen = {}

    def invoke(self, domain, prompt, cwd, stage):
        seen["prompt"] = prompt
        return dict(output=json.dumps({"files": [{"path": "docs/SPEC.md", "content": "new\n"}]}),
                    invocation="t", exit_code=0, duration_sec=0)
    monkeypatch.setattr(TriadResearchRunner, "invoke", invoke)
    builder = ProposalBuilder(executable=sys.executable)
    builder.run(OrderSchema(goal="g", allowed_paths=["docs/SPEC.md", "ea/**"], required_outputs=["docs/SPEC.md (fix)"]), str(tmp_path))
    assert "UNIQUE-SPEC-MARKER" in seen["prompt"] and "UNIQUE-EA-MARKER" in seen["prompt"]
    assert builder.last_execution_info["context_files"][:2] == ["docs/SPEC.md", "ea/Bot.mq5"]


def test_proposal_followed_by_commentary_is_still_read():
    from agents.proposal_builder import ProposalBuilder
    parsed, repairs = ProposalBuilder.parse_proposal('{"files": [{"path": "a.py", "content": "x=1"}]}\n\nDone - notes follow {')
    assert parsed["files"][0]["path"] == "a.py" and repairs
    with pytest.raises(json.JSONDecodeError):
        ProposalBuilder.parse_proposal('{"files": [}')


def test_minor_findings_with_head_pass_spend_no_correction_round(tmp_path, monkeypatch):
    orch = proxy_orch()
    quiet(monkeypatch, orch)
    calls = []
    monkeypatch.setattr(orch.builder, "run", good_build(calls))
    monkeypatch.setattr(orch, "run_reviewer_with_failover", lambda *a: (ReviewResultSchema(
        verdict="changes_required", p2=["doc wording"], summary="minor"), None))
    monkeypatch.setattr(orch, "run_decision_with_failover", lambda *a: (DecisionSchema(
        decision="PASS", rationale="done", next_steps=["doc wording"]), None))
    out = orch.run(str(tmp_path / "proj"), "double", order=OrderSchema(**ORDER))
    assert out["final_state"] == State.PASS and len(calls) == 1
    assert "P2 FOLLOW-UPS" in orch._load_json(out["run_id"], "decision.json")["rationale"]


def test_pytest_node_ids_in_allowed_deletions_become_symbols(tmp_path):
    from core.autopilot import sanitize_order, symbol_deletion
    assert symbol_deletion("backend/tests/test_img.py::test_passes_through[png]") == "def:test_passes_through"
    assert symbol_deletion("tests/test_a.py::TestResize") == "class:TestResize"
    assert symbol_deletion("backend/old.py") == "backend/old.py"
    order = OrderSchema(goal="g", allowed_paths=["backend/**"],
                        allowed_deletions=["backend/tests/test_img.py::test_signature_filler_passes", "def:keep"])
    fixed, notes = sanitize_order(order, str(tmp_path))
    assert fixed.allowed_deletions == ["def:test_signature_filler_passes", "def:keep"]
    assert any("node ids rewritten" in n for n in notes)


@pytest.mark.head_proxy
def test_operator_replan_sends_a_security_stop_back_to_planning_without_approving_it(tmp_path, monkeypatch):
    builds = []
    plans = [{"status": "CONTINUE", "summary": "redo", "reason": "", "order": dict(ORDER)},
             {"status": "DONE", "summary": "ok", "reason": "", "order": None}]
    factory = mission_factory(monkeypatch, plans, builds)
    runner = MissionRunner(factory, MissionStore(tmp_path / "missions"))
    m = runner.create(str(tmp_path / "proj"), "g", OrderSchema(**ORDER), config={})
    orch = factory()
    rid = orch._create_run_with_retry(None, str(tmp_path / "proj"), "double")
    orch.sm.transition_to(rid, State.HUMAN_REQUIRED, details="Scope violation: Path escapes worktree: docs/x.md")
    m["steps"][0]["run_id"] = rid
    m["steps"][0]["result"] = runner._evidence(orch, rid)
    runner.store.save(m)
    assert runner.run(m["mission_id"])["status"] == "BLOCKED"            # a security stop never passes on its own
    m = runner.store.load(m["mission_id"])
    m["steps"][0]["operator_replan"] = "false positive fixed"
    runner.store.save(m)
    runner.reopen(m["mission_id"])
    done = runner.run(m["mission_id"])
    assert done["status"] == "DONE" and "operator re-plan" in done["steps"][0]["result"]["treated_as"]
    assert orch.sm.get_run(rid)["current_state"] == State.HUMAN_REQUIRED    # the stopped run itself is not approved

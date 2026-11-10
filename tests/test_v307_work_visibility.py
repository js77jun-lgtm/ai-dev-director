"""
v3.0.7 WORK VISIBILITY: every AI CLI call is recorded in the work ledger (role, AI, stage, CLI time, tokens) at the
single choke point, and /ops/activity shows missions step by step, who is working right now and per-role workload.
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core import usage
from core.process_runner import ProcessTreeRunner


def result(stdout="", stderr="", exit_code=0, duration=2.5, timed_out=False):
    return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code, duration_sec=duration, timed_out=timed_out)


def test_claude_reported_tokens_are_exact():
    envelope = json.dumps({"result": "ok", "usage": {"input_tokens": 100, "cache_read_input_tokens": 900,
                                                      "cache_creation_input_tokens": 50, "output_tokens": 40}})
    usage.set_context(role="HEAD", stage="DECIDING", run_id="RUN-A", mission_id="MSN-A")
    rid = usage.call_started([r"C:\x\claude.exe", "--print"], "prompt")
    usage.call_finished(rid, [r"C:\x\claude.exe"], "prompt", result(envelope))
    call = usage.recent_calls(1)[0]
    assert call["provider"] == "ANTHROPIC" and call["role"] == "HEAD" and call["tokens"] == 1090 and call["exact"]
    assert call["mission_id"] == "MSN-A" and call["run_id"] == "RUN-A"


def test_codex_total_and_estimates_for_silent_clis():
    usage.set_context(role="REVIEWER", stage="REVIEWING", run_id=None, mission_id=None)
    rid = usage.call_started(["codex.exe", "exec"], "x" * 400)
    usage.call_finished(rid, ["codex.exe"], "x" * 400, result("verdict...", "tokens used\n12,345"))
    assert usage.recent_calls(1)[0]["tokens"] == 12345
    usage.set_context(role="BUILDER")
    rid = usage.call_started(["agy.exe"], "a" * 400 + "가" * 150)
    usage.call_finished(rid, ["agy.exe"], "a" * 400 + "가" * 150, result("b" * 40))
    call = usage.recent_calls(1)[0]
    assert call["provider"] == "GOOGLE" and not call["exact"] and call["tokens"] == (101 + 100) + 11


def test_antigravity_output_is_estimated_from_the_answer_not_the_stream():
    stream = "\n".join([json.dumps({"event": "delta", "text": "x" * 4000})] * 10
                       + [json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "y" * 400}})])
    usage.set_context(role="BUILDER")
    rid = usage.call_started(["agy.exe"], "p" * 40)
    usage.call_finished(rid, ["agy.exe"], "p" * 40, result(stream))
    assert usage.recent_calls(1)[0]["tokens"] == (11) + (101)


def test_only_ai_clis_are_recorded():
    assert usage.call_started(["python", "-m", "pytest"], "") is None
    assert usage.call_started([r"C:\stub\director-stub-cli.cmd"], "") is None


def test_aggregate_per_role_and_provider_and_running_now():
    usage.set_context(role="BUILDER", stage="BUILDING", run_id="RUN-B", mission_id="MSN-B")
    done = usage.call_started(["agy.exe"], "p")
    usage.call_finished(done, ["agy.exe"], "p", result("o", duration=60))
    failed = usage.call_started(["claude.exe"], "p")
    usage.call_finished(failed, ["claude.exe"], "p", result("", exit_code=1, duration=5))
    live = usage.call_started(["codex.exe"], "p")                      # still running
    with usage._conn() as conn:                                          # a RUNNING row from a process that died long ago
        conn.execute("INSERT INTO ai_usage (started_at, status, provider, role) VALUES (?, 'RUNNING', 'GOOGLE', 'BUILDER')",
                     ((datetime.utcnow() - timedelta(hours=2)).isoformat(),))
    agg = usage.summary("MSN-B")["mission"]
    b = agg["by_role"]["BUILDER"]
    assert b["calls"] == 2 and b["failed"] == 1 and b["seconds"] == 65.0 and b["providers"] == {"GOOGLE": 1, "ANTHROPIC": 1}
    now = usage.running_now()
    assert [r["provider"] for r in now] == ["OPENAI"] and now[0]["role"] == "BUILDER" and now[0]["mission_id"] == "MSN-B"
    assert live is not None


def test_process_runner_records_around_the_real_call(monkeypatch):
    monkeypatch.setattr(ProcessTreeRunner, "_guarded", staticmethod(lambda argv: False))
    monkeypatch.setattr(ProcessTreeRunner, "_run_process", classmethod(lambda cls, argv, **kw: result("done", duration=7)))
    usage.set_context(role="REVIEWER", stage="REVIEWING", run_id="RUN-C", mission_id=None)
    out = ProcessTreeRunner.run(["codex.exe", "exec", "-"], stdin_data="review this")
    assert out.stdout == "done"
    call = usage.recent_calls(1)[0]
    assert call["provider"] == "OPENAI" and call["role"] == "REVIEWER" and call["seconds"] == 7 and call["status"] == "OK"


def test_ledger_failure_never_breaks_the_call(monkeypatch):
    monkeypatch.setattr(ProcessTreeRunner, "_guarded", staticmethod(lambda argv: False))
    monkeypatch.setattr(ProcessTreeRunner, "_run_process", classmethod(lambda cls, argv, **kw: result("fine")))
    monkeypatch.setattr(usage, "call_started", lambda *a: (_ for _ in ()).throw(RuntimeError("db locked")))
    assert ProcessTreeRunner.run(["claude.exe"], stdin_data="x").stdout == "fine"


def test_state_transitions_tag_the_calls_that_follow():
    from core.state_machine import StateMachine, State
    sm = StateMachine()
    sm.create_run("RUN-CTX", "p", "g")
    sm.transition_to("RUN-CTX", State.PLANNING)
    assert usage.context()["run_id"] == "RUN-CTX" and usage.context()["stage"] == State.PLANNING


def test_activity_payload_shows_missions_and_workload(tmp_path):
    import dashboard_server
    from core.mission import MissionRunner, MissionStore
    from schemas.order import OrderSchema
    store = MissionStore()
    runner = MissionRunner(lambda: None, store)
    m = runner.create(str(tmp_path / "proj"), "build a thing", OrderSchema(goal="first", allowed_paths=["src/**"]), config={})
    m["steps"][0]["result"] = {"final_state": "PASS", "tests": {"passed": 5, "failed": 0, "total": 5}, "review_verdict": "pass",
                               "repair_rounds": 1, "stop_detail": ""}
    m["status"] = "DONE"
    store.save(m)
    usage.set_context(role="BUILDER", stage="BUILDING", run_id=None, mission_id=m["mission_id"])
    rid = usage.call_started(["agy.exe"], "p")
    usage.call_finished(rid, ["agy.exe"], "p", result("o", duration=30))
    p = dashboard_server.build_activity_payload()
    view = p["missions"][0]
    assert p["focus_mission_id"] == m["mission_id"] and view["passed_steps"] == 1
    assert view["steps"][0]["tests"]["total"] == 5 and view["steps"][0]["repairs"] == 1
    assert p["usage"]["mission"]["by_role"]["BUILDER"]["seconds"] == 30.0
    assert any(c["provider"] == "GOOGLE" for c in p["recent_calls"])


def test_urls_in_an_order_are_not_mistaken_for_document_paths(tmp_path):
    from core.builder_context import referenced_documents
    from schemas.order import OrderSchema
    doc = tmp_path / "spec.md"
    doc.write_text("SPEC", encoding="utf-8")
    order = OrderSchema(goal="Call LM Studio at http://127.0.0.1:1234/v1 with model qwen; then update docs/SPEC.md. "
                             f"Read {doc} for details.", allowed_paths=["src/**"])
    assert referenced_documents(order) == {str(doc): "SPEC"}
    missing = OrderSchema(goal="See E:\\no\\such\\file.md please", allowed_paths=["src/**"])
    with pytest.raises(ValueError, match="REFERENCE_INPUT_UNAVAILABLE"):
        referenced_documents(missing)


def test_version_probes_are_not_counted_as_ai_work():
    assert usage.call_started(["claude.exe", "--version"], None) is None
    assert usage.call_started(["codex.exe", "--version"], "") is None


from tests.test_v0238_dashboard_ux import env, server, _get_json  # noqa: E402,F401


def test_activity_endpoint_serves_json(env, server):
    code, payload = _get_json(server, "/ops/activity")
    assert code == 200 and "working_now" in payload and "usage" in payload and "missions" in payload


def test_calls_of_a_stopped_director_process_are_not_shown_as_working(monkeypatch):
    usage.set_context(role="HEAD", stage="MISSION_PLANNING", run_id=None, mission_id="MSN-DEAD")
    usage.call_started(["claude.exe"], "plan")
    with usage._conn() as conn:
        conn.execute("UPDATE ai_usage SET pid=999999 WHERE mission_id='MSN-DEAD'")
    monkeypatch.setattr(usage, "_pid_alive", lambda pid: int(pid) != 999999)
    assert not [r for r in usage.running_now() if r["mission_id"] == "MSN-DEAD"]
    assert usage.recent_calls(1)[0]["status"] == "INTERRUPTED"

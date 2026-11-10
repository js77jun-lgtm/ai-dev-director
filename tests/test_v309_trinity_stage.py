"""
v3.0.9 TRINITY STAGE: the 8081 console opens on a live visual stage (WebGL particles driven by the work ledger).
Server side this adds a fixed static allowlist, a read-only mission replay endpoint (/ops/replay) fed by the ledger,
and keeps the strict CSP: the page may not rely on inline scripts or styles, or on any external host.
"""
import re
from pathlib import Path
from types import SimpleNamespace

from core import usage
from tests.test_v0238_dashboard_ux import env, server, _get_json  # noqa: F401
from tests.test_v301_companion_http import http_server, request  # noqa: F401

CONSOLE = Path(__file__).resolve().parents[1] / "console"


def result(duration=10.0, exit_code=0):
    return SimpleNamespace(stdout="ok", stderr="", exit_code=exit_code, duration_sec=duration, timed_out=False)


def record(role, cli, mission_id, run_id, stage, duration=10.0, exit_code=0):
    usage.set_context(role=role, stage=stage, run_id=run_id, mission_id=mission_id)
    rid = usage.call_started([cli], "prompt")
    usage.call_finished(rid, [cli], "prompt", result(duration, exit_code))


def test_stage_files_are_served_with_the_strict_csp(http_server):
    for path, marker in (("/", "trinity-canvas"), ("/stage.js", "TrinityStage"), ("/trinity.js", "window.Trinity"),
                         ("/stage.css", ".trinity")):
        code, body = request(http_server, "GET", path)
        assert code == 200 and marker.encode() in body, path
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", http_server.server_address[1], timeout=3)
    conn.request("GET", "/trinity.js")
    resp = conn.getresponse(); resp.read()
    assert "default-src 'self'" in resp.getheader("Content-Security-Policy")
    conn.close()


def test_only_the_allowlisted_console_files_are_served(http_server):
    for path in ("/stage.js.bak", "/../config.yaml", "/console/stage.js", "/companion.json", "/data/solomon-bridge.token"):
        assert request(http_server, "GET", path)[0] == 404, path


def test_stage_page_needs_no_inline_code_or_external_host():
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")
    assert "AI DEV 작업실" in html                                   # the workshop iframe is still there
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)            # CSP default-src 'self': no inline script
    assert "style=" not in html and "<style" not in html              # ... and no inline style
    for name in ("index.html", "stage.js", "trinity.js", "stage.css"):
        text = (CONSOLE / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://(?!127\.0\.0\.1)", text), name  # nothing is loaded from another host


def test_stage_only_calls_known_read_endpoints_and_the_chat_post():
    js = (CONSOLE / "stage.js").read_text(encoding="utf-8")
    called = set(re.findall(r"(?:getJSON|fetch)\('(/[a-z/_]+)", js))
    assert called == {"/ops/activity", "/status", "/ops/replay", "/chat/messages", "/chat", "/ops/roles", "/ops/mission/decide"}
    assert js.count("method:'POST'") == 3                             # writes: chat to HEAD, a role change, a delivery decision
    assert "'X-Solomon-Bridge':roleToken" in js                        # the role change carries the local token


def test_mission_calls_are_time_ordered_and_skip_running_rows():
    record("HEAD", "claude.exe", "MSN-R", "RUN-1", "PLANNING", 5)
    record("BUILDER", "agy.exe", "MSN-R", "RUN-1", "BUILDING", 60)
    record("REVIEWER", "codex.exe", "MSN-OTHER", "RUN-9", "REVIEWING", 30)
    usage.set_context(role="REVIEWER", stage="REVIEWING", run_id="RUN-1", mission_id="MSN-R")
    usage.call_started(["codex.exe"], "still running")
    calls = usage.mission_calls("MSN-R")
    assert [(c["role"], c["provider"], c["stage"]) for c in calls] == [("HEAD", "ANTHROPIC", "PLANNING"),
                                                                       ("BUILDER", "GOOGLE", "BUILDING")]
    assert calls[1]["seconds"] == 60 and calls[1]["run_id"] == "RUN-1"


def test_replay_endpoint_returns_the_latest_finished_mission_with_calls(env, server, tmp_path):
    from core.mission import MissionRunner, MissionStore
    from schemas.order import OrderSchema
    store = MissionStore()
    runner = MissionRunner(lambda: None, store)

    def mission(goal, status, calls):
        m = runner.create(str(tmp_path / "proj"), goal, OrderSchema(goal="first", allowed_paths=["src/**"]), config={})
        m["steps"][0]["run_id"] = "RUN-" + goal
        m["steps"][0]["result"] = {"final_state": "PASS", "tests": {"passed": 3, "failed": 0, "total": 3}}
        m["status"] = status
        store.save(m)
        for role, cli in calls:
            record(role, cli, m["mission_id"], "RUN-" + goal, "BUILDING")
        return m["mission_id"]

    done = mission("finished", "DONE", [("HEAD", "claude.exe"), ("BUILDER", "agy.exe")])
    live = mission("running", "RUNNING", [("HEAD", "claude.exe")])
    code, payload = _get_json(server, "/ops/replay")
    assert code == 200 and payload["mission_id"] == done               # a running mission is never "replayed"
    assert [c["role"] for c in payload["calls"]] == ["HEAD", "BUILDER"]
    assert payload["steps"][0]["state"] == "PASS" and payload["steps"][0]["tests"]["total"] == 3
    code, payload = _get_json(server, "/ops/replay?mission_id=" + live)
    assert payload["mission_id"] == live and len(payload["calls"]) == 1  # ... unless asked for by id
    assert _get_json(server, "/ops/replay?mission_id=MSN-NOPE")[1] == {"mission_id": None, "calls": [], "steps": []}


def test_folder_picker_needs_the_local_token_and_returns_the_chosen_path(http_server, monkeypatch):
    import json
    import companion_server
    monkeypatch.setattr(companion_server, "pick_folder", lambda initial="": {"path": "E:/work/프로젝트"})
    assert request(http_server, "POST", "/ops/pick-folder")[0] == 403                    # no token: no dialog
    code, body = request(http_server, "POST", "/ops/pick-folder", headers={"X-Solomon-Bridge": "test-token"})
    assert code == 200 and json.loads(body)["path"] == "E:/work/프로젝트"
    monkeypatch.setattr(companion_server, "pick_folder", lambda initial="": {"error": "busy"})
    assert request(http_server, "POST", "/ops/pick-folder", headers={"X-Solomon-Bridge": "test-token"})[0] == 409


def test_only_one_folder_dialog_at_a_time_and_cancel_is_an_empty_path(monkeypatch):
    import companion_server
    from types import SimpleNamespace as NS
    seen = {}

    def fake_run(argv, capture_output, timeout):
        seen["argv"] = argv
        assert "error" in companion_server.pick_folder()                                   # a second dialog is refused
        return NS(returncode=0, stdout=b'{"path": ""}')
    monkeypatch.setattr(companion_server.subprocess, "run", fake_run)
    assert companion_server.pick_folder("E:/start") == {"path": ""}
    assert seen["argv"][1].endswith("pick_folder.py") and seen["argv"][2] == "E:/start"

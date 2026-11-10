"""
v0.2.4.2 VISUAL COMMAND CENTER — node activity derived from real run state + recorded roles only,
pipeline visual classes, reduced-motion support, provider cards never clipped.
"""
import sqlite3

import pytest

import dashboard_server
from dashboard_server import render_html, _node_activity, DASHBOARD_CSS, GLOBAL_SESSION_ID
from core.state_machine import StateMachine, State
from core.db_path import get_db_path
from tests.test_v0238_dashboard_ux import env, server, _get_json  # noqa: F401


def _roles(openai="HEAD, REVIEWER", anthropic="BACKUP", google="BUILDER"):
    return {"OPENAI": {"role": openai}, "ANTHROPIC": {"role": anthropic}, "GOOGLE": {"role": google}}


def test_activity_idle_has_no_active_node():
    a = _node_activity("NONE", _roles())
    assert a["director"] is False and a["stage"] is None
    assert not any(n["active"] for n in a["nodes"].values())
    assert a["nodes"]["ANTHROPIC"]["backup"] is True
    for terminal in ("PASS", "FAIL", "HUMAN_REQUIRED", "TIMEOUT"):
        assert not any(n["active"] for n in _node_activity(terminal, _roles())["nodes"].values())


@pytest.mark.parametrize("state,expected", [
    ("PLANNING", "OPENAI"), ("ORDER_READY", "OPENAI"), ("BUILDING", "GOOGLE"), ("REVIEWING", "OPENAI"), ("DECIDING", "OPENAI"),
])
def test_activity_follows_stage_and_roles(state, expected):
    a = _node_activity(state, _roles())
    assert a["director"] is True
    assert [d for d, n in a["nodes"].items() if n["active"]] == [expected]


def test_activity_verify_stage_is_director_only():
    a = _node_activity("VERIFYING", _roles())
    assert a["director"] is True and a["stage"] == "VERIFY" and not any(n["active"] for n in a["nodes"].values())


def test_activity_claude_failover_uses_recorded_roles():
    a = _node_activity("REVIEWING", _roles(openai="NONE", anthropic="HEAD, REVIEWER"))
    assert a["nodes"]["ANTHROPIC"]["active"] is True and a["nodes"]["OPENAI"]["active"] is False
    assert a["nodes"]["ANTHROPIC"]["backup"] is False
    a2 = _node_activity("PLANNING", _roles(openai="NONE", anthropic="HEAD, REVIEWER"))
    assert a2["nodes"]["ANTHROPIC"]["active"] is True


def _run(sm, rid, chain, details="x"):
    sm.create_run(rid, "C:/p", "g")
    for stt in chain:
        sm.transition_to(rid, stt, details=details)


def test_status_and_page_reflect_building_and_pass(server, env):
    with sqlite3.connect(str(get_db_path())) as c:
        c.execute("UPDATE provider_health SET current_role='HEAD, REVIEWER' WHERE provider='OPENAI'")
        c.execute("UPDATE provider_health SET current_role='BUILDER' WHERE provider='GOOGLE'")
        c.execute("UPDATE provider_health SET current_role='BACKUP' WHERE provider='ANTHROPIC'"); c.commit()
    sm = StateMachine()
    _run(sm, "RUN-CC-BUILD", [State.PLANNING, State.ORDER_READY, State.BUILDING])
    st = _get_json(server, "/status")[1]
    assert st["activity"]["director"] is True and st["activity"]["stage"] == "BUILDER"
    assert st["activity"]["nodes"]["GOOGLE"]["active"] is True and st["activity"]["nodes"]["OPENAI"]["active"] is False
    html = render_html()
    assert 'id="node-GOOGLE"' in html and 'class="node active healthy" id="node-GOOGLE"' in html
    assert 'id="director-core" class="active"' in html and "ORCHESTRATING \u00b7 BUILDER" in html
    assert 'id="node-GOOGLE-ind" data-raw="ACTIVE"' in html and 'class="tap active" id="tap-GOOGLE"' in html
    # PASS: no node active, director idle, pipeline badge data available
    sm.transition_to("RUN-CC-BUILD", State.VERIFYING); sm.transition_to("RUN-CC-BUILD", State.REVIEWING)
    sm.transition_to("RUN-CC-BUILD", State.DECIDING); sm.transition_to("RUN-CC-BUILD", State.PASS, details="ok")
    st = _get_json(server, "/status")[1]
    assert st["activity"]["director"] is False and not any(n["active"] for n in st["activity"]["nodes"].values())
    html = render_html()
    assert 'class="node healthy" id="node-GOOGLE"' in html and 'id="director-core" class=""' in html and 'id="core-stage" data-raw="IDLE"' in html
    assert "PASS \u00b7" in html


def test_page_reflects_human_required_warning(server, env):
    sm = StateMachine()
    _run(sm, "RUN-CC-HR", [State.PLANNING, State.ORDER_READY, State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING, State.HUMAN_REQUIRED], details="FINAL_GATE: refused")
    st = _get_json(server, "/status")[1]
    assert st["run"]["state"] == "HUMAN_REQUIRED" and st["run"]["last_stage"] == "DECIDING" and st["run"]["human_required"] == "YES"
    html = render_html()
    assert 'class="alert a-red"' in html and "HUMAN REQUIRED \u2014 FINAL_GATE: refused" in html
    assert 'class="badge b-red" data-raw="HUMAN REQUIRED"' in html and "renderPipeline" in html and "'warn'" in html   # JS marks the stopped stage as warning


def test_visual_language_and_reduced_motion():
    css = DASHBOARD_CSS
    assert "@media (prefers-reduced-motion: reduce)" in css and "animation: none !important" in css
    assert "@keyframes pulse" in css and "@keyframes signal" in css
    node_block = css.split(".node {")[1].split("}")[0]
    assert "overflow: hidden" not in node_block and "min-height: 0" in node_block
    assert ".stage.warn" in css and ".stage.failed" in css and ".stage.active" in css and ".stage.done" in css


def test_compact_events_have_labels(env):
    env.add_message(GLOBAL_SESSION_ID, "SYSTEM", "Starting background run RUN-1...")
    env.add_message(GLOBAL_SESSION_ID, "SYSTEM", "Run RUN-1 finished: PASS")
    env.add_message(GLOBAL_SESSION_ID, "SYSTEM", "RECOVERY TRIAL: OPENAI is RECOVERING; this request is its single verification call")
    env.add_message(GLOBAL_SESSION_ID, "SYSTEM", "Run RUN-2 finished: HUMAN_REQUIRED")
    env.add_message(GLOBAL_SESSION_ID, "USER", "hello")
    html = render_html()
    for label in ("RUN STARTED", "RUN PASS", "RECOVERY TRIAL", "HUMAN REQUIRED"):
        assert f">{label}</span>" in html
    assert "<pre>hello</pre>" in html and "<pre>Starting" not in html
    assert "expanded" in html                                            # click-to-expand keeps the original text reachable

"""
v0.2.3.7 DASHBOARD MODE TRUTHFULNESS — the page's SYSTEM MODE and the chat/orchestrator gate share one
read-only policy; RECOVERING is never shown as HEALTHY; render claims/probes nothing.
"""
import re
import sqlite3
from datetime import datetime, timedelta

import pytest

import dashboard_server
from dashboard_server import render_html
from core.state_machine import StateMachine
from core.chat_manager import ChatManager
from core.provider_manager import ProviderManager, ProviderStatus, FailoverMode
from core.process_runner import ProcessTreeRunner
from core.db_path import get_db_path


def _set(dom, status, next_trial_at=None, trial_started_at=None):
    with sqlite3.connect(str(get_db_path())) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(provider_health)")]
        for col in ("trial_started_at", "next_trial_at"):
            if col not in cols:
                c.execute(f"ALTER TABLE provider_health ADD COLUMN {col} TEXT")
        c.execute("UPDATE provider_health SET status=?, next_trial_at=?, trial_started_at=? WHERE provider=?",
                  (status, next_trial_at, trial_started_at, dom))
        c.commit()


def _rows():
    with sqlite3.connect(str(get_db_path())) as c:
        c.row_factory = sqlite3.Row
        return {r["provider"]: dict(r) for r in c.execute("SELECT * FROM provider_health")}


def _chat_gate_mode():
    """The exact execution-path computation (dashboard_server.handle_chat_message lines: mark(only=[HEAD]) + gate)."""
    pm = ProviderManager()
    pm.mark_trial_candidates(only=["OPENAI"])
    return pm.get_failover_mode()[0]


def _mode_in_html(html):
    m = re.search(r'<span class="status-([A-Z]+)" id="system-mode" data-raw="([A-Z]+)">', html)
    assert m and m.group(1) == m.group(2)
    return m.group(2)


def _status_cell(html, dom):
    sec = html.split(f'id="node-{dom}"')[1]
    return re.search(r'<td class="status-([A-Z_]+)" id="prov-[A-Z]+-status" data-raw="([A-Z_]+)">', sec).group(2)


@pytest.fixture
def page_db(isolate_provider_db):
    StateMachine(); ChatManager()
    if hasattr(dashboard_server, "_ensure_schema"):
        dashboard_server._ensure_schema()
    return isolate_provider_db


# ------------------------------------------------------------------ A: RECOVERING head with trial available -> DEGRADED, same as chat gate

def test_a_recovering_head_trial_available_is_degraded(page_db):
    _set("OPENAI", "RECOVERING"); _set("ANTHROPIC", "RECOVERING"); _set("GOOGLE", "HEALTHY")
    preview = ProviderManager().get_effective_mode_for_next_run(only=["OPENAI"])
    assert preview["mode"] == FailoverMode.DEGRADED
    assert preview["trial_candidates"] == ["OPENAI"]
    assert _chat_gate_mode() == FailoverMode.DEGRADED                 # page == execution gate
    html = render_html()
    assert _mode_in_html(html) == "DEGRADED"
    assert "Recovery trial: OPENAI available" in html
    assert _status_cell(html, "OPENAI") == "RECOVERING"               # never shown as HEALTHY
    assert _status_cell(html, "ANTHROPIC") == "RECOVERING"
    assert _status_cell(html, "GOOGLE") == "HEALTHY"
    assert len(re.findall(r'<td class="status-HEALTHY" id="prov-[A-Z]+-status" data-raw="HEALTHY">', html)) == 1


# ------------------------------------------------------------------ B: cooldown active + ANTHROPIC unavailable -> STOP

def test_b_cooldown_active_is_stop(page_db):
    future = (datetime.utcnow() + timedelta(seconds=500)).isoformat()
    _set("OPENAI", "RECOVERING", next_trial_at=future); _set("ANTHROPIC", "UNAVAILABLE"); _set("GOOGLE", "HEALTHY")
    preview = ProviderManager().get_effective_mode_for_next_run(only=["OPENAI"])
    assert preview["mode"] == FailoverMode.STOP and preview["trial_candidates"] == []
    assert _chat_gate_mode() == FailoverMode.STOP
    html = render_html()
    assert _mode_in_html(html) == "STOP"
    assert "OPENAI unavailable" in html and "OPENAI available" not in html
    assert _status_cell(html, "OPENAI") == "RECOVERING"
    # in-flight trial owned by another process (trial_started_at fresh) is equally unavailable
    _set("OPENAI", "RECOVERING", trial_started_at=datetime.utcnow().isoformat())
    assert ProviderManager().get_effective_mode_for_next_run(only=["OPENAI"])["mode"] == FailoverMode.STOP
    assert _mode_in_html(render_html()) == "STOP"
    # RECOVERING provider that is not the HEAD is not a chat candidate either (policy parity)
    _set("OPENAI", "UNAVAILABLE"); _set("ANTHROPIC", "RECOVERING"); _set("GOOGLE", "HEALTHY")
    assert ProviderManager().get_effective_mode_for_next_run(only=["OPENAI"])["mode"] == FailoverMode.STOP
    assert _chat_gate_mode() == FailoverMode.STOP
    assert _mode_in_html(render_html()) == "STOP"


# ------------------------------------------------------------------ C: all HEALTHY -> NORMAL

def test_c_all_healthy_is_normal(page_db):
    for d in ("OPENAI", "ANTHROPIC", "GOOGLE"):
        _set(d, "HEALTHY")
    preview = ProviderManager().get_effective_mode_for_next_run(only=["OPENAI"])
    assert preview["mode"] == FailoverMode.NORMAL and preview["trial_candidates"] == []
    assert _chat_gate_mode() == FailoverMode.NORMAL
    html = render_html()
    assert _mode_in_html(html) == "NORMAL"
    assert re.search(r'<div id="recovery-trial"[^>]*display:none;"></div>', html)   # visible line empty/hidden


# ------------------------------------------------------------------ D: render is read-only (no status change, no claim, no CLI)

def test_d_render_is_read_only(page_db, monkeypatch):
    _set("OPENAI", "RECOVERING"); _set("ANTHROPIC", "RECOVERING"); _set("GOOGLE", "HEALTHY")
    before = _rows()

    def no_cli(*a, **k):
        raise AssertionError("render must not launch any process")
    monkeypatch.setattr(ProcessTreeRunner, "run", classmethod(lambda cls, *a, **k: no_cli()))
    monkeypatch.setattr(ProviderManager, "claim_trial", lambda self, d: (_ for _ in ()).throw(AssertionError("render must not claim a trial")))
    monkeypatch.setattr(ProviderManager, "check_health", lambda self, force=False: (_ for _ in ()).throw(AssertionError("render must not probe")))
    monkeypatch.setattr(ProviderManager, "set_status", lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("render must not change status")))

    html = render_html()
    assert _mode_in_html(html) == "DEGRADED"
    assert _rows() == before                                          # DB untouched
    pm = ProviderManager()
    pm.get_effective_mode_for_next_run(only=["OPENAI"])
    assert pm.trial_candidates == set() and pm.active_trials == set()  # preview leaves planning state alone
    assert _rows() == before


# ------------------------------------------------------------------ single policy: no duplicated mode logic

def test_e_single_mode_policy_source():
    import inspect
    src = inspect.getsource(dashboard_server)
    assert "healthy_count" not in src
    assert "get_effective_mode_for_next_run" in src
    assert ProviderManager.domain_for_provider("codex") == "OPENAI"
    assert ProviderManager.domain_for_provider("claude") == "ANTHROPIC"
    assert ProviderManager.domain_for_provider("antigravity") == "GOOGLE"
    from core.orchestrator import Orchestrator
    assert "domain_for_provider" in inspect.getsource(Orchestrator._get_agent_domain)

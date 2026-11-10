"""V3.0-G: Korean, read-only TRIAD Research Center (no real provider calls)."""
import json
import sqlite3
import threading
import urllib.request

import pytest

import dashboard_server
from core.db_path import get_db_path
from core.research_state import ResearchState, ResearchStateMachine
from core.state_machine import StateMachine


@pytest.fixture
def dashboard():
    StateMachine()
    httpd = dashboard_server.DashboardServer(("127.0.0.1", 0), dashboard_server.DashboardHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield port
    httpd.shutdown()
    httpd.server_close()


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.status, response.read().decode("utf-8")


def _seed_research():
    sm = ResearchStateMachine()
    sm.create_session("RS-G-1", "C:/greenfield", "한국어 Moonshot: 작은 검증 도구", cycle_budget=4)
    sm.create_cycle("RC-G-1", "RS-G-1", 1)
    sm.transition_cycle("RC-G-1", ResearchState.INDEPENDENT_RESEARCH)
    proposal = {
        "hypothesis": "결정론적 검사로 위험을 줄일 수 있다", "rationale": "작은 입력으로 검증 가능",
        "proposed_experiment": "검사기 프로토타입", "confidence": 0.72, "freeform_insight": "격리 우선",
    }
    for domain in ("OPENAI", "ANTHROPIC", "GOOGLE"):
        sm.record_stage_output("RC-G-1", ResearchState.INDEPENDENT_RESEARCH, domain,
                               f"research/{domain}.json", domain.lower() * 8, 10, 10, proposal)
    sm.transition_cycle("RC-G-1", ResearchState.CROSS_CRITIQUE)
    sm.record_stage_output("RC-G-1", ResearchState.CROSS_CRITIQUE, "OPENAI", "research/critique.json",
                           "c" * 64, 10, 10, {"weakest_assumption": "입력 다양성이 부족하다"})
    sm.transition_cycle("RC-G-1", ResearchState.SYNTHESIS)
    sm.record_stage_output("RC-G-1", ResearchState.SYNTHESIS, "OPENAI", "research/synthesis.json",
                           "s" * 64, 10, 10, {"title": "격리 검사", "synthesized_hypothesis": "격리된 결정론적 검사를 먼저 실행한다"})
    sm.transition_cycle("RC-G-1", ResearchState.EXPERIMENT_PLANNING)
    sm.record_experiment_plan("RC-G-1", {"goal": "검사기 프로토타입", "allowed_paths": ["src"]},
                              "research/order.json", "o" * 64, "GREENFIELD", "NOT_APPLICABLE_GREENFIELD")
    sm.record_experiment_result("RC-G-1", "RUN-G-1", "COMPLETE", "PASS", "C:/worktree",
                                {"post_verification": "PASS", "regression": "PASS"})
    sm.record_baseline_evidence("RC-G-1", {"baseline_state": "NOT_APPLICABLE_GREENFIELD"},
                                "research/baseline.json", "b" * 64)
    sm.record_research_memory("RS-G-1", "RC-G-1", "h" * 64,
                              {"hypothesis": "격리 검사", "result": "supported", "lesson": "작은 검증부터 시작한다",
                               "rejected_ideas": ["전역 상태 공유"], "next_direction": "입력 경계를 넓혀 재검증"},
                              {"experiment": "RUN-G-1"})
    sm.record_call_provenance({
        "session_id": "RS-G-1", "cycle_id": "RC-G-1", "stage": "SYNTHESIS", "domain": "OPENAI",
        "research_role": "RESEARCHER", "executable": "codex", "version": "test", "exit_code": 0,
        "duration_sec": 0.1, "observed_fields": ["executable", "version", "exit_code", "duration_sec"],
        "declared_fields": ["authentication_mode", "api_key_policy"], "outcome": "PASS",
    })
    return sm


def test_g1_old_database_without_research_tables_is_not_migrated(isolate_provider_db):
    before = sqlite3.connect(str(get_db_path())).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'research_%'"
    ).fetchall()
    payload = dashboard_server.build_research_status_payload()
    after = sqlite3.connect(str(get_db_path())).execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'research_%'"
    ).fetchall()
    assert before == after == []
    assert payload["available"] is False and payload["active"] is False


def test_g2_status_and_research_endpoint_expose_korean_research_center(dashboard):
    _seed_research()
    dashboard_server.invalidate_status_cache()
    code, raw = _get(dashboard, "/status")
    assert code == 200
    research = json.loads(raw)["research"]
    assert research["session_id"] == "RS-G-1"
    assert research["moonshot"].startswith("한국어 Moonshot")
    assert research["cycle_number"] == 1 and research["cycle_budget"] == 4
    assert len(research["proposals"]) == 3
    assert research["synthesis"]["synthesized_hypothesis"].startswith("격리된")
    assert research["experiment"]["normal_final_state"] == "PASS"
    assert research["evidence"]["baseline_state"] == "NOT_APPLICABLE_GREENFIELD"
    assert research["rejected_ideas"] == ["전역 상태 공유"]
    assert research["what_we_learned"] == "작은 검증부터 시작한다"
    assert research["provenance"][0]["observed_fields"] == ["executable", "version", "exit_code", "duration_sec"]
    code, raw = _get(dashboard, "/research?session_id=RS-G-1")
    assert code == 200 and json.loads(raw)["next_step"] == "입력 경계를 넓혀 재검증"


def test_g3_research_gets_are_read_only(dashboard):
    _seed_research()
    db = str(get_db_path())
    tables = ("research_sessions", "research_cycles", "research_stage_outputs", "research_call_provenance",
              "research_experiments", "research_baseline_evidence", "research_memory")
    with sqlite3.connect(db) as conn:
        before = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    _get(dashboard, "/research")
    _get(dashboard, "/status")
    dashboard_server.render_html()
    with sqlite3.connect(db) as conn:
        after = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    assert after == before


def test_g4_malformed_persisted_json_fails_closed_for_display_only():
    _seed_research()
    with sqlite3.connect(str(get_db_path())) as conn:
        conn.execute("UPDATE research_stage_outputs SET payload_json='not-json' WHERE stage='SYNTHESIS'")
        conn.execute("UPDATE research_call_provenance SET observed_fields='not-json'")
        conn.commit()
    research = dashboard_server.build_research_status_payload("RS-G-1")
    assert research["synthesis"] == {}
    assert research["provenance"][0]["observed_fields"] == []
    assert research["active"] is True


def test_g5_html_separates_normal_and_triad_without_new_execution_post():
    StateMachine()
    _seed_research()
    html = dashboard_server.render_html()
    for marker in (
        'id="normal-mode-btn"', 'id="triad-mode-btn"', 'id="triad-research-center"',
        "TRIAD RESEARCH CENTER", "3개 독립 제안", "교차 비평", "합성 가설", "기각된 아이디어",
        "배운 점", "다음 단계", 'id="research-raw"', 'id="main"', 'id="chat-form"',
    ):
        assert marker in html
    assert "setDashboardView" in html and "renderResearch" in html
    assert 'action="/research"' not in html
    assert "innerHTML" not in html

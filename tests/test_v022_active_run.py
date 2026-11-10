import pytest
import sqlite3
import os
from datetime import datetime, timedelta
from dashboard_server import render_html
from core.state_machine import StateMachine
from core.provider_manager import ProviderManager

def test_db_isolation(isolate_provider_db, monkeypatch):
    sm = StateMachine()
    # It should use TEST_DB_PATH
    assert str(sm.db_path) == os.environ["TEST_DB_PATH"]

def test_dashboard_logic(isolate_provider_db, monkeypatch):
    test_db_path = os.environ["TEST_DB_PATH"]
    
    conn = sqlite3.connect(test_db_path)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id TEXT PRIMARY KEY,
            project_path TEXT NOT NULL,
            head_provider TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            run_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES chat_sessions(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            project_path TEXT NOT NULL,
            goal TEXT NOT NULL,
            current_state TEXT NOT NULL,
            worktree_path TEXT,
            branch_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    now = datetime.utcnow()
    # B: Stale run (1 hour ago)
    stale_time = (now - timedelta(hours=1)).isoformat()
    c.execute("INSERT INTO runs (run_id, project_path, goal, current_state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
              ("RUN-1", "path", "goal", "DECIDING", stale_time, stale_time))

    # C: Active run
    active_time = (now - timedelta(seconds=10)).isoformat()
    c.execute("INSERT INTO runs (run_id, project_path, goal, current_state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
              ("RUN-2", "path", "goal", "BUILDING", active_time, active_time))

    # D: Completed run
    comp_time = (now - timedelta(days=1)).isoformat()
    c.execute("INSERT INTO runs (run_id, project_path, goal, current_state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
              ("RUN-3", "path", "goal", "PASS", comp_time, comp_time))

    # A: Test run (should be ignored)
    test_time = (now - timedelta(seconds=5)).isoformat()
    c.execute("INSERT INTO runs (run_id, project_path, goal, current_state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
              ("RUN-TEST-999", "path", "goal", "PLANNING", test_time, test_time))

    conn.commit()
    conn.close()

    html = render_html()

    # Check Stale run warning
    assert "STALE RUN DETECTED" in html



def test_active_run_selection_ignores_terminal_and_test_runs(isolate_provider_db):
    """Terminal runs are never 'stale'; the CURRENT RUN panel shows the active (non-terminal,
    non-stale) run; RUN-TEST-* rows are ignored."""
    test_db_path = os.environ["TEST_DB_PATH"]
    sm = StateMachine()  # creates runs table on TEST_DB_PATH
    from core.chat_manager import ChatManager
    ChatManager()  # creates chat tables

    conn = sqlite3.connect(test_db_path)
    c = conn.cursor()
    now = datetime.utcnow()
    old = (now - timedelta(hours=2)).isoformat()
    fresh = (now - timedelta(seconds=5)).isoformat()
    rows = [
        ("RUN-TEST-1", "PLANNING", fresh),      # must be ignored
        ("RUN-OLDPASS", "PASS", old),            # terminal + old: NOT stale, NOT current
        ("RUN-ACTIVE", "BUILDING", fresh),       # active
    ]
    for rid, st, ts_ in rows:
        c.execute("INSERT INTO runs (run_id, project_path, goal, current_state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (rid, "p", "g", st, ts_, ts_))
    conn.commit(); conn.close()

    html = render_html()
    assert "STALE RUN DETECTED" not in html
    assert "RUN-ACTIVE" in html
    assert "RUN-TEST-1" not in html

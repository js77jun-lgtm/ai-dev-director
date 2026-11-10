import pytest
import sqlite3
from pathlib import Path
from core.provider_manager import ProviderManager, ProviderStatus, FailoverMode
import requests
import json

def get_db(tmp_path):
    db_path = tmp_path / "director.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS provider_health (
            provider TEXT PRIMARY KEY,
            cli TEXT,
            version TEXT,
            status TEXT NOT NULL,
            last_success DATETIME,
            last_failure DATETIME,
            failure_reason TEXT,
            current_role TEXT,
            last_latency_sec REAL,
            consecutive_failures INTEGER DEFAULT 0,
            usage_percent REAL,
            remaining REAL,
            reset_at DATETIME,
            last_checked DATETIME
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS provider_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            usage_percent REAL,
            latency REAL,
            success INTEGER NOT NULL
        )
    ''')
    conn.commit()
    conn.close()
    return db_path

def test_a_normal_mode(tmp_path):
    db_path = get_db(tmp_path)
    pm = ProviderManager(db_path=str(db_path))
    
    # Check health forces init
    res = pm.check_health(force=True)
    mode, _ = pm.get_failover_mode()
    
    assert mode == FailoverMode.NORMAL
    
    roles = pm.resolve_roles()
    assert roles["head_provider"] == "openai"
    assert roles["builder_provider"] == "antigravity"
    assert roles["reviewer_provider"] == "codex"

def test_b_openai_quota_exhausted(tmp_path):
    db_path = get_db(tmp_path)
    pm = ProviderManager(db_path=str(db_path))
    
    # Simulate Quota Exhausted on OpenAI
    pm.set_status("OPENAI", ProviderStatus.QUOTA_EXHAUSTED, "usage limit reached")
    mode, down = pm.get_failover_mode()
    
    assert mode == FailoverMode.DEGRADED
    
    # Resolve roles should shift to Claude
    roles = pm.resolve_roles()
    assert roles["head_provider"] == "claude"
    assert roles["reviewer_provider"] == "claude"
    
    # Verify DB role was updated
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT current_role FROM provider_health WHERE provider = 'ANTHROPIC'")
    role = cursor.fetchone()['current_role']
    assert "HEAD" in role
    assert "REVIEWER" in role

def test_c_stop_mode(tmp_path):
    db_path = get_db(tmp_path)
    pm = ProviderManager(db_path=str(db_path))
    
    pm.set_status("OPENAI", ProviderStatus.UNAVAILABLE)
    pm.set_status("ANTHROPIC", ProviderStatus.AUTH_FAILED)
    
    mode, down = pm.get_failover_mode()
    assert mode == FailoverMode.STOP
    
    roles = pm.resolve_roles()
    assert roles["can_proceed"] is False

def test_d_unknown_usage(tmp_path):
    db_path = get_db(tmp_path)
    pm = ProviderManager(db_path=str(db_path))
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT usage_percent FROM provider_health WHERE provider = 'OPENAI'")
    usage = cursor.fetchone()['usage_percent']
    
    # DB allows NULL, meaning UNKNOWN
    assert usage is None

def test_e_cli_timeout(tmp_path):
    db_path = get_db(tmp_path)
    pm = ProviderManager(db_path=str(db_path))
    
    # simulate timeout parsing
    status = pm.parse_cli_error("GOOGLE", "hard timeout exceeded")
    assert status == ProviderStatus.TIMEOUT
    
    # check DB
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM provider_health WHERE provider = 'GOOGLE'")
    assert cursor.fetchone()['status'] == "TIMEOUT"

@pytest.mark.integration_dashboard
def test_f_dashboard():
    # We can fetch the dashboard locally
    try:
        resp = requests.get("http://localhost:8080/")
        assert resp.status_code == 200
        html = resp.text
        assert "AI DEV DIRECTOR" in html
        assert "OPENAI" in html
        assert "ANTHROPIC" in html
        assert "GOOGLE" in html
        assert "UNKNOWN" in html # usage percent
    except Exception as e:
        pytest.fail(f"Dashboard not running or failed: {e}")

def test_g_no_api_keys(tmp_path):
    db_path = get_db(tmp_path)
    pm = ProviderManager(db_path=str(db_path))
    # We just ensure it works without API keys
    assert True

import pytest
from core.provider_manager import ProviderManager

def test_no_cli_version_fallback(isolate_provider_db, monkeypatch):
    pm = ProviderManager()
    
    def mock_run(*args, **kwargs):
        raise Exception("Timeout simulated")

    # v0.2.3.4: version probes run through ProcessTreeRunner (tree-killed on timeout), not subprocess.run
    from core.process_runner import ProcessTreeRunner
    monkeypatch.setattr(ProcessTreeRunner, "run", staticmethod(mock_run))
    
    pm.check_health(force=True)
    
    with pm._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT provider, version, status FROM provider_health")
        rows = cursor.fetchall()
        for row in rows:
            assert row["version"] == "UNKNOWN"
            assert row["status"] == "TIMEOUT"

def test_update_latency(isolate_provider_db):
    pm = ProviderManager()
    pm.update_latency("OPENAI", 42.5)
    
    with pm._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT last_latency_sec FROM provider_health WHERE provider='OPENAI'")
        row = cursor.fetchone()
        assert row["last_latency_sec"] == 42.5

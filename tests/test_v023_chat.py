import pytest
import sqlite3
import os
import json
from pathlib import Path
from dashboard_server import render_html, handle_chat_message, GLOBAL_SESSION_ID
from core.chat_manager import ChatManager
from core.provider_manager import ProviderManager
from core.state_machine import StateMachine

@pytest.fixture
def chat_setup(isolate_provider_db):
    cm = ChatManager()
    pm = ProviderManager()
    
    # Healthy providers by default
    with pm._get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO provider_health (provider, status) VALUES ('OPENAI', 'HEALTHY')")
        conn.execute("INSERT OR REPLACE INTO provider_health (provider, status) VALUES ('ANTHROPIC', 'HEALTHY')")
        conn.execute("INSERT OR REPLACE INTO provider_health (provider, status) VALUES ('GOOGLE', 'HEALTHY')")
        conn.commit()
    
    return cm, pm

class MockHeadQuestion:
    provider = "OPENAI"
    def chat(self, msg, ctx, hist):
        return {"type": "QUESTION", "message": "I am a question answer"}
        
class MockHeadTask:
    provider = "OPENAI"
    def chat(self, msg, ctx, hist):
        return {
            "type": "DEV_TASK",
            "message": "I will do this",
            "order": {
                "goal": "do something",
                "allowed_paths": ["src/"],
                "protected_paths": [],
                "acceptance_criteria": [],
                "allowed_deletions": [],
                "max_files_changed": 1,
                "max_diff_lines": 10,
                "context": ""
            }
        }
        
class MockHeadMalformed:
    provider = "OPENAI"
    def chat(self, msg, ctx, hist):
        return {"type": "DEV_TASK"} # missing order

def test_a_question(chat_setup, monkeypatch):
    cm, pm = chat_setup
    
    import core.orchestrator
    monkeypatch.setattr(core.orchestrator.Orchestrator, "_init_head", lambda s, p: MockHeadQuestion())
    monkeypatch.setattr(core.orchestrator.Orchestrator, "run", lambda *a, **kw: None)
    
    # Mocking self.head property after initialization
    def mock_init(self, *args, **kwargs):
        self.head = MockHeadQuestion()
        self.config = {}
    
    monkeypatch.setattr(core.orchestrator.Orchestrator, "__init__", mock_init)
    
    handle_chat_message("what is this?")
    msgs = cm.get_messages(GLOBAL_SESSION_ID)
    
    assert msgs[-1]["role"] == "HEAD"
    assert msgs[-1]["content"] == "I am a question answer"

def test_b_dev_task(chat_setup, monkeypatch):
    cm, pm = chat_setup
    
    run_called = False
    def mock_run(self, project_path, goal, run_id=None, builder_fn=None, order=None, **kwargs):
        nonlocal run_called
        run_called = True
        assert order.goal == "do something"
        
    import core.orchestrator
    def mock_init(self, *args, **kwargs):
        self.head = MockHeadTask()
        self.config = {}
        self.sm = StateMachine()
        self.pm = ProviderManager()
        
    monkeypatch.setattr(core.orchestrator.Orchestrator, "__init__", mock_init)
    monkeypatch.setattr(core.orchestrator.Orchestrator, "run", mock_run)
    from tests.test_v0231_chat_stabilization import inline_threads
    inline_threads(monkeypatch)

    # v0.2.3.1: a project must be explicitly selected before any DEV_TASK can run
    from tests.test_v0231_chat_stabilization import make_git_project
    with make_git_project() as proj:
        cm.set_project(GLOBAL_SESSION_ID, proj)
        handle_chat_message("add a function")

    msgs = cm.get_messages("main_session")
    for m in msgs:
        print("MSG:", m)
    assert run_called

def test_c_malformed_order(chat_setup, monkeypatch):
    cm, pm = chat_setup
    
    import core.orchestrator
    def mock_init(self, *args, **kwargs):
        self.head = MockHeadMalformed()
        self.config = {}
        self.sm = StateMachine()
    monkeypatch.setattr(core.orchestrator.Orchestrator, "__init__", mock_init)
    
    handle_chat_message("add a function")
    
    msgs = cm.get_messages(GLOBAL_SESSION_ID)
    assert msgs[-1]["role"] == "SYSTEM"
    assert "missing order JSON" in msgs[-1]["content"]

def test_d_system_mode_stop(chat_setup, monkeypatch):
    cm, pm = chat_setup
    with pm._get_conn() as conn:
        conn.execute("UPDATE provider_health SET status='UNAVAILABLE'")
        conn.commit()
        
    handle_chat_message("do something")
    msgs = cm.get_messages(GLOBAL_SESSION_ID)
    assert msgs[-1]["role"] == "SYSTEM"
    assert "SYSTEM STOP" in msgs[-1]["content"]

def test_e_real_init_passes_config_and_project_context(chat_setup, monkeypatch):
    """handle_chat_message must construct a real Orchestrator (config supplied) and pass a
    dict project_context (not a bare string) to head.chat, and run() on the session project."""
    cm, pm = chat_setup
    seen = {}

    class CapturingHead:
        provider = "openai"
        def chat(self, msg, ctx, hist):
            seen["ctx"] = ctx
            return {"type": "DEV_TASK", "message": "ok", "order": {"goal": "g", "allowed_paths": ["src/"]}}

    import core.orchestrator
    monkeypatch.setattr(core.orchestrator.Orchestrator, "_init_head", lambda self, p=None: CapturingHead())
    def mock_run(self, project_path, goal, run_id=None, builder_fn=None, order=None, **kwargs):
        seen["project_path"] = project_path
        seen["order"] = order
    monkeypatch.setattr(core.orchestrator.Orchestrator, "run", mock_run)
    from tests.test_v0231_chat_stabilization import inline_threads
    inline_threads(monkeypatch)

    from tests.test_v0231_chat_stabilization import make_git_project
    with make_git_project() as proj:
        cm.set_project(GLOBAL_SESSION_ID, proj)
        handle_chat_message("add a helper")

    assert isinstance(seen["ctx"], dict)
    assert "project_path" in seen["ctx"] and "existing_files" in seen["ctx"]
    assert seen["order"].goal == "g"
    session = cm.get_session(GLOBAL_SESSION_ID)
    assert seen["project_path"] == session["project_path"]


def test_h_protected_paths_safety(isolate_provider_db):
    """An order supplied via chat (Orchestrator.run(order=...)) must still pass through the
    post-build Scope Guard: a rogue write to a protected path ends in HUMAN_REQUIRED."""
    import subprocess, tempfile
    from pathlib import Path
    from core.orchestrator import Orchestrator
    from core.state_machine import State
    from agents.mock_head import MockHead
    from agents.codex_reviewer import CodexReviewer
    from schemas.order import OrderSchema

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), check=True)
        (repo / "src").mkdir()
        (repo / "src" / "app.py").write_text("def run(): pass\n", encoding="utf-8")
        (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

        orch = Orchestrator(
            config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
            head_agent=MockHead(),
            reviewer_agent=CodexReviewer(provider="mock")
        )
        order = OrderSchema(goal="chat order", allowed_paths=["src/"], protected_paths=[".env"], max_files_changed=5, max_diff_lines=100)

        def rogue_builder(builder, o, wt_path):
            (Path(wt_path) / ".env").write_text("SECRET=hacked\n", encoding="utf-8")
            return [".env"]

        summary = orch.run(str(repo), order.goal, builder_fn=rogue_builder, order=order)

        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert summary["scope_violations"] > 0
        assert (repo / ".env").read_text(encoding="utf-8") == "SECRET=1\n"
        audit_actions = [r["action"] for r in orch.sm._get_conn().execute(
            "SELECT action FROM audit_logs WHERE run_id = ?", (summary["run_id"],)).fetchall()]
        assert "PROVIDED_ORDER" in audit_actions
        assert "SCOPE_VIOLATION" in audit_actions

def test_f_reload_history(chat_setup):
    cm, pm = chat_setup
    from core.state_machine import StateMachine
    sm = StateMachine() # Initializes tables
    cm.create_session(GLOBAL_SESSION_ID, "proj", "OPENAI")
    cm.add_message(GLOBAL_SESSION_ID, "USER", "hi")
    
    html = render_html()
    assert "hi" in html
    assert "msg-USER" in html

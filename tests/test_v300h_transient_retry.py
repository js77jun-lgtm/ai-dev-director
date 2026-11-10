"""V3.0-H hardening: bounded transient retry for TRIAD research-stage provider calls.

Scope (see docs/V3_TRIAD_RESEARCH_PLAN.md section 8/9): retry lives ONLY inside
core.research_runner.TriadResearchRunner, applies ONLY to transport/runtime-level failures with positive
evidence of being transient (a genuine timeout, or Antigravity's stream-json mode reporting terminal
SUCCESS with no parseable/empty result), MAX_ATTEMPTS=2 (initial + 1 retry), never substitutes a provider,
never feeds a bad attempt's output into the retry, and every attempt is preserved in
research_call_provenance. Quota/auth/provider-unavailable/config/safety/schema/experiment-result failures
are excluded by construction and still fail closed on the first attempt.

Tests 1-4, 9: TriadResearchRunner-level, ProcessTreeRunner.run monkeypatched for determinism.
Tests 5-6: structural proof the safety/reviewer pipeline never routes through the retrying runner at all.
Tests 7-8: ResearchOrchestrator-level, provider_call stub, proving isolation and full provenance survive a
retry that happened inside a lower-level runner (simulated here at the result-shape boundary).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.process_runner import ProcessResult, ProcessTreeRunner
from core.research_runner import ResearchProviderCallError, TriadResearchRunner


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    return repo


def _google_stream(status: str, response: str = "") -> str:
    return json.dumps({"event": "result", "result": {"status": status, "response": response}}) + "\n"


# ---------------------------------------------------------------------------------- 1-4, 9: runner-level

def test_1_empty_response_then_valid_response_succeeds_on_retry(tmp_path, monkeypatch):
    """empty response -> 1 retry -> normal response -> stage succeeds."""
    repo = _git_repo(tmp_path)
    calls = {"n": 0}

    def fake_run(argv, **kwargs):
        if "--version" in argv:
            return ProcessResult(exit_code=0, stdout="test-1", stderr="", timed_out=False, duration_sec=0.05, pid=1)
        calls["n"] += 1
        if calls["n"] == 1:
            return ProcessResult(exit_code=0, stdout=_google_stream("SUCCESS", ""), stderr="", timed_out=False,
                                  duration_sec=0.5, pid=1)
        return ProcessResult(exit_code=0, stdout=_google_stream("SUCCESS", "a real proposal"), stderr="",
                              timed_out=False, duration_sec=0.5, pid=1)

    monkeypatch.setattr(ProcessTreeRunner, "run", staticmethod(fake_run))
    runner = TriadResearchRunner(executables={"GOOGLE": sys.executable})
    result = runner.invoke("GOOGLE", "the original prompt", str(repo), "INDEPENDENT_RESEARCH")
    assert result["output"] == "a real proposal"
    assert result["attempt_number"] == 2
    assert calls["n"] == 2
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["outcome"] == "FAILED"
    assert result["attempts"][0]["error"] == "GOOGLE_STREAM_EMPTY_RESPONSE"
    assert result["attempts"][0]["retryable"] is True


def test_2_empty_response_on_both_attempts_is_human_required(tmp_path, monkeypatch):
    """empty response -> retry also empty -> HUMAN_REQUIRED (raised, no third attempt)."""
    repo = _git_repo(tmp_path)
    calls = {"n": 0}

    def fake_run(argv, **kwargs):
        calls["n"] += 1
        return ProcessResult(exit_code=0, stdout=_google_stream("SUCCESS", ""), stderr="", timed_out=False,
                              duration_sec=0.5, pid=1)

    monkeypatch.setattr(ProcessTreeRunner, "run", staticmethod(fake_run))
    runner = TriadResearchRunner(executables={"GOOGLE": sys.executable})
    with pytest.raises(ResearchProviderCallError) as excinfo:
        runner.invoke("GOOGLE", "the original prompt", str(repo), "INDEPENDENT_RESEARCH")
    assert calls["n"] == 2   # exactly MAX_ATTEMPTS, never more (no infinite retry)
    assert "GOOGLE_STREAM_EMPTY_RESPONSE" in str(excinfo.value)
    attempts = excinfo.value.attempts
    assert len(attempts) == 2 and all(a["outcome"] == "FAILED" for a in attempts)
    assert attempts[0]["attempt_number"] == 1 and attempts[1]["attempt_number"] == 2
    assert attempts[1]["retry_reason"] == attempts[0]["error"]


def test_3_quota_error_is_never_retried(tmp_path, monkeypatch):
    """quota error -> retry 0 -> HUMAN_REQUIRED on the first attempt."""
    repo = _git_repo(tmp_path)
    calls = {"n": 0}

    def fake_run(argv, **kwargs):
        calls["n"] += 1
        return ProcessResult(exit_code=1, stdout="", stderr="Error: usage limit reached for this session",
                              timed_out=False, duration_sec=0.2, pid=1)

    monkeypatch.setattr(ProcessTreeRunner, "run", staticmethod(fake_run))
    runner = TriadResearchRunner(executables={"OPENAI": sys.executable})
    with pytest.raises(ResearchProviderCallError) as excinfo:
        runner.invoke("OPENAI", "prompt", str(repo), "INDEPENDENT_RESEARCH")
    assert calls["n"] == 1   # no retry attempted
    assert len(excinfo.value.attempts) == 1
    assert excinfo.value.attempts[0]["retryable"] is False


def test_4_auth_error_is_never_retried(tmp_path, monkeypatch):
    """auth failure -> retry 0."""
    repo = _git_repo(tmp_path)
    calls = {"n": 0}

    def fake_run(argv, **kwargs):
        calls["n"] += 1
        return ProcessResult(exit_code=1, stdout="", stderr="Error: authentication failed, please log in again",
                              timed_out=False, duration_sec=0.2, pid=1)

    monkeypatch.setattr(ProcessTreeRunner, "run", staticmethod(fake_run))
    runner = TriadResearchRunner(executables={"ANTHROPIC": sys.executable})
    with pytest.raises(ResearchProviderCallError):
        runner.invoke("ANTHROPIC", "prompt", str(repo), "INDEPENDENT_RESEARCH")
    assert calls["n"] == 1


def test_4b_genuine_timeout_is_retried_and_provider_unavailable_is_not(tmp_path, monkeypatch):
    """Positive control (timeout retried) alongside a negative control (explicitly-named non-retryable
    category) in one test to pin both ends of the classifier."""
    repo = _git_repo(tmp_path)
    calls = {"n": 0}

    def fake_timeout(argv, **kwargs):
        if "--version" in argv:
            return ProcessResult(exit_code=0, stdout="test-1", stderr="", timed_out=False, duration_sec=0.05, pid=1)
        calls["n"] += 1
        if calls["n"] == 1:
            return ProcessResult(exit_code=None, stdout="", stderr="", timed_out=True, duration_sec=300.0,
                                  pid=1, timeout_reason="hard timeout exceeded")
        return ProcessResult(exit_code=0, stdout=json.dumps({"result": "ok"}), stderr="", timed_out=False,
                              duration_sec=0.5, pid=1)

    monkeypatch.setattr(ProcessTreeRunner, "run", staticmethod(fake_timeout))
    runner = TriadResearchRunner(executables={"ANTHROPIC": sys.executable})
    result = runner.invoke("ANTHROPIC", "prompt", str(repo), "SYNTHESIS")
    assert result["attempt_number"] == 2 and calls["n"] == 2   # timeout is retried once, then succeeds

    calls2 = {"n": 0}

    def fake_unavailable(argv, **kwargs):
        calls2["n"] += 1
        return ProcessResult(exit_code=1, stdout="", stderr="503 Service Unavailable", timed_out=False,
                              duration_sec=0.2, pid=1)

    monkeypatch.setattr(ProcessTreeRunner, "run", staticmethod(fake_unavailable))
    runner2 = TriadResearchRunner(executables={"OPENAI": sys.executable})
    with pytest.raises(ResearchProviderCallError):
        runner2.invoke("OPENAI", "prompt", str(repo), "SYNTHESIS")
    assert calls2["n"] == 1   # "provider unavailable/blocked" is on the never-retry list, not just TIMEOUT


def test_9_retry_never_bypasses_the_real_cli_guard_in_default_regression(tmp_path):
    """Every attempt - including a retry - must still funnel through the REAL ProcessTreeRunner.run() (not
    mocked in this one test), so the existing REAL_CLI_CALL_DURING_UNIT_TEST guard (DIRECTOR_TESTING=1)
    blocks it exactly as before; the retry loop introduces no second code path that could reach a real
    subscription CLI in a unit test."""
    repo = _git_repo(tmp_path)
    # a real file whose basename ("codex") matches REAL_CLI_BASENAMES, so the guard's argv[0] check fires -
    # unlike sys.executable, this exercises the actual, unmocked ProcessTreeRunner.run guard path.
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("", encoding="utf-8")
    before = len(ProcessTreeRunner.blocked_real_cli_calls)
    runner = TriadResearchRunner(executables={"OPENAI": str(fake_codex)})
    with pytest.raises(Exception):
        runner.invoke("OPENAI", "prompt", str(repo), "INDEPENDENT_RESEARCH")
    blocked = ProcessTreeRunner.blocked_real_cli_calls[before:]
    assert len(blocked) == 1   # the guard fired on the very first attempt; no attempt ever got past it -
    # MAX_ATTEMPTS=2 exists for genuinely transient failures, but "refusing to launch a real CLI under
    # DIRECTOR_TESTING=1" is not retryable by construction (RealCliCallDuringUnitTest, not
    # ResearchProviderCallError, so _is_retryable is never even consulted).
    del ProcessTreeRunner.blocked_real_cli_calls[before:]   # this test provoked it deliberately


# ---------------------------------------------------------------------------------- 5-6: structural scope proof

def test_5_6_retry_module_is_never_imported_by_the_safety_or_reviewer_pipeline():
    """safety gate failure -> retry 0; Reviewer changes_required -> retry 0. Both are true by construction:
    Scope Guard (core/safety.py), the NORMAL Builder/Verifier/Reviewer/Decision pipeline
    (core/orchestrator.py), and the Reviewer agents never import core.research_runner at all - the bounded
    retry added in this round exists ONLY inside the six TRIAD research-prompt call sites and cannot affect
    an experiment's Scope Guard, Verifier, or Reviewer outcome."""
    for path in ("core/safety.py", "core/orchestrator.py", "agents/codex_reviewer.py",
                 "agents/claude_reviewer.py", "agents/antigravity_builder.py"):
        text = Path(path).read_text(encoding="utf-8")
        assert "research_runner" not in text and "TriadResearchRunner" not in text, path


# ---------------------------------------------------------------------------------- 7-8: orchestrator-level

def test_7_8_independent_research_isolation_and_full_attempt_provenance_survive_a_retry(tmp_path, provider_state):
    """independent-research contamination guard still holds when one domain's call needed a retry, and
    every attempt (failed + final) is preserved in research_call_provenance with the right attempt_number/
    retry_reason - proven at the orchestrator boundary using a result shaped exactly like
    TriadResearchRunner.invoke() would hand back after a real retry (attempt classification itself is
    covered by tests 1-4 above)."""
    from tests.test_v300c_research_pipeline import _project, _proposal
    from core.research_orchestrator import ResearchOrchestrator
    from core.research_state import ResearchState, ResearchStateMachine

    seen_prompts = {}

    def retried_google_call(domain, prompt, stage, cwd):
        seen_prompts.setdefault(domain, []).append(prompt)
        if domain == "GOOGLE" and stage == ResearchState.INDEPENDENT_RESEARCH:
            attempts = [{
                "attempt_number": 1, "outcome": "FAILED", "error": "GOOGLE_STREAM_EMPTY_RESPONSE",
                "retryable": True, "retry_reason": None, "executable": "stub-google", "version": "test-1",
                "invocation": "stub", "started_at": "2026-01-01T00:00:00", "ended_at": "2026-01-01T00:00:01",
                "duration_sec": 0.5, "exit_code": 0,
            }]
            return {
                "output": json.dumps(_proposal("GOOGLE")), "executable": "stub-google", "version": "test-1",
                "invocation": "stub", "started_at": "2026-01-01T00:00:02", "ended_at": "2026-01-01T00:00:03",
                "duration_sec": 0.5, "exit_code": 0, "token_usage": "UNKNOWN",
                "authentication_mode": "subscription", "api_key_used": False,
                "observed_fields": ["executable", "version", "exit_code", "duration_sec"],
                "declared_fields": ["authentication_mode", "api_key_used"],
                "attempts": attempts, "attempt_number": 2,
            }
        return {
            "output": json.dumps(_proposal(domain)), "executable": f"stub-{domain.lower()}", "version": "test-1",
            "invocation": "stub", "started_at": "2026-01-01T00:00:00", "ended_at": "2026-01-01T00:00:01",
            "duration_sec": 0.5, "exit_code": 0, "token_usage": "UNKNOWN",
            "authentication_mode": "subscription", "api_key_used": False,
            "observed_fields": ["executable", "version", "exit_code", "duration_sec"],
            "declared_fields": ["authentication_mode", "api_key_used"], "attempts": [], "attempt_number": 1,
        }

    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    rsm = ResearchStateMachine()
    orch = ResearchOrchestrator(state_machine=rsm, provider_manager=pm, artifact_root=tmp_path / "research",
                                provider_call=retried_google_call)
    session = orch.start_session(str(_project(tmp_path)), "Find a new falsifiable mechanism")
    result = orch.run_independent_research(session["session_id"])
    assert result["current_stage"] == ResearchState.CROSS_CRITIQUE and not result["human_required"]

    # 7: contamination guard holds for the retried domain exactly like the other two - the prompt text every
    # domain receives (including GOOGLE, whose call needed a retry) contains no peer-proposal material, and
    # GOOGLE's own retry resent the identical prompt rather than rebuilding it with any other domain's output.
    assert set(seen_prompts) == {"OPENAI", "ANTHROPIC", "GOOGLE"}
    for domain, prompts_seen in seen_prompts.items():
        assert len(prompts_seen) == 1   # provider_call is invoked once per domain; a real retry happens
        # inside TriadResearchRunner.invoke() (tests 1-4) and is reported back via "attempts", not by
        # calling this stub twice - so a single prompt reaching this stub, resent unchanged on retry, is
        # exactly what an orchestrator caller observes either way.
        payload = json.loads(prompts_seen[0])
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        assert "peer_proposal" not in serialized and "other_proposal" not in serialized
        assert "peer_proposals" not in serialized and "other_proposals" not in serialized

    # 8: full attempt provenance persisted for the retried domain
    cycle_id = rsm.list_cycles(session["session_id"])[0]["cycle_id"]
    rows = rsm.get_call_provenance(cycle_id, ResearchState.INDEPENDENT_RESEARCH)
    google_rows = sorted([r for r in rows if r["domain"] == "GOOGLE"], key=lambda r: r["attempt_number"])
    assert len(google_rows) == 2
    assert google_rows[0]["attempt_number"] == 1 and google_rows[0]["outcome"] == "FAILED"
    assert google_rows[0]["error"] == "GOOGLE_STREAM_EMPTY_RESPONSE"
    assert google_rows[1]["attempt_number"] == 2 and google_rows[1]["outcome"] == "SUCCESS"
    assert google_rows[1]["retry_reason"] == "GOOGLE_STREAM_EMPTY_RESPONSE"
    # the other two domains needed no retry: exactly one SUCCESS row each, no FAILED rows
    for domain in ("OPENAI", "ANTHROPIC"):
        drows = [r for r in rows if r["domain"] == domain]
        assert len(drows) == 1 and drows[0]["attempt_number"] == 1 and drows[0]["outcome"] == "SUCCESS"

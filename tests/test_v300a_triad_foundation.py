"""
V3.0-A - TRIAD RESEARCH MODE milestone A: Research Session/Cycle data model + provider readiness.

Scope (see docs/V3_TRIAD_RESEARCH_PLAN.md): no CLI calls, no experiment pipeline, no dashboard UI yet.
These tests only cover: (B) a TRIAD session/cycle registers all three providers as participants when all
three are usable, (N) a missing provider is reflected truthfully (never silently treated as a 3-provider
TRIAD success), plus the underlying state-model mechanics (schema, transitions, isolation from NORMAL mode
and from provider_health.current_role, bounded-autonomy cycle budget).

Regression test "A" (NORMAL mode unaffected) is the full existing pytest suite, run separately - this file
adds nothing to core.state_machine, core.orchestrator, or any NORMAL-mode code path.
"""
import sqlite3

import pytest

from core.db_path import get_db_path
from core.research_state import (
    DEFAULT_CYCLE_BUDGET,
    PARTICIPANT_STATUS_ACTIVE,
    PARTICIPANT_STATUS_UNAVAILABLE,
    InvalidResearchTransitionError,
    ResearchState,
    ResearchStateMachine,
    SESSION_STATUS_ACTIVE,
    TERMINAL_CYCLE_STAGES,
)
from core.triad_provider import TRIAD_BLOCKED, TRIAD_DEGRADED, TRIAD_READY, assess_triad_readiness


# ---------------------------------------------------------------------------------- schema / isolation

def test_1_tables_created_in_same_isolated_db_as_normal_mode(isolate_provider_db):
    """research_* tables live in the exact same (test-isolated) DB file StateMachine uses - not a
    separate file, not the production data/director.db (isolate_provider_db already asserts that globally;
    this test additionally proves ResearchStateMachine resolves to the identical path)."""
    from core.state_machine import StateMachine
    sm = StateMachine()
    rsm = ResearchStateMachine()
    assert rsm.db_path == sm.db_path == get_db_path()
    with sqlite3.connect(str(get_db_path())) as c:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"research_sessions", "research_cycles", "research_cycle_transitions", "research_participants"} <= names
    # and NORMAL mode's own tables are untouched / still present alongside
    assert {"runs", "state_transitions", "provider_health"} <= names


def test_2_research_state_machine_never_touches_provider_health_current_role(isolate_provider_db, provider_state):
    """Registering TRIAD participants must not write provider_health.current_role - that column is
    NORMAL mode's (ProviderManager.resolve_roles()). A concurrent NORMAL run's role display must survive
    a TRIAD cycle untouched."""
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    pm.resolve_roles()  # NORMAL mode assigns roles first, as it would in a real concurrent scenario
    with sqlite3.connect(str(get_db_path())) as c:
        before = {r[0]: r[1] for r in c.execute("SELECT provider, current_role FROM provider_health")}
    rsm = ResearchStateMachine()
    rsm.create_session("R-TEST-1", "C:/proj", "beat the market")
    cid = "R-TEST-1-C1"
    rsm.create_cycle(cid, "R-TEST-1", 1)
    rsm.register_participants(cid, {"OPENAI": "ACTIVE", "ANTHROPIC": "ACTIVE", "GOOGLE": "ACTIVE"})
    with sqlite3.connect(str(get_db_path())) as c:
        after = {r[0]: r[1] for r in c.execute("SELECT provider, current_role FROM provider_health")}
    assert before == after   # byte-for-byte unchanged


# ---------------------------------------------------------------------------------- provider readiness (B, N)

def test_3_triad_ready_when_all_three_usable(isolate_provider_db, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    r = assess_triad_readiness(pm)
    assert r["state"] == TRIAD_READY
    assert r["usable"] == ["ANTHROPIC", "GOOGLE", "OPENAI"]
    assert r["missing"] == []
    assert r["all_three_usable"] is True


def test_4_triad_degraded_when_exactly_one_provider_unavailable(isolate_provider_db, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="UNAVAILABLE", GOOGLE="HEALTHY")
    r = assess_triad_readiness(pm)
    assert r["state"] == TRIAD_DEGRADED
    assert r["missing"] == ["ANTHROPIC"]
    assert r["all_three_usable"] is False


def test_5_triad_blocked_when_two_or_more_unavailable(isolate_provider_db, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="UNAVAILABLE", GOOGLE="AUTH_FAILED")
    r = assess_triad_readiness(pm)
    assert r["state"] == TRIAD_BLOCKED
    assert set(r["missing"]) == {"ANTHROPIC", "GOOGLE"}


def test_6_degraded_session_never_silently_looks_like_a_full_triad_success(isolate_provider_db, provider_state):
    """PHASE 3 requirement: a 2-provider run must never be indistinguishable from a real 3-provider TRIAD
    session in the persisted evidence. register_participants() records the true per-domain status, and a
    caller building a session summary can see UNAVAILABLE explicitly rather than inferring 3-for-3."""
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="UNAVAILABLE", GOOGLE="HEALTHY")
    readiness = assess_triad_readiness(pm)
    assert readiness["state"] == TRIAD_DEGRADED

    rsm = ResearchStateMachine()
    rsm.create_session("R-TEST-2", "C:/proj", "beat the market")
    cid = "R-TEST-2-C1"
    rsm.create_cycle(cid, "R-TEST-2", 1)
    statuses = {d: (PARTICIPANT_STATUS_ACTIVE if d in readiness["usable"] else PARTICIPANT_STATUS_UNAVAILABLE)
                for d in ("OPENAI", "ANTHROPIC", "GOOGLE")}
    rsm.register_participants(cid, statuses)
    participants = {p["domain"]: p["status"] for p in rsm.get_participants(cid)}
    assert participants == {"OPENAI": "ACTIVE", "ANTHROPIC": "UNAVAILABLE", "GOOGLE": "ACTIVE"}
    active_count = sum(1 for s in participants.values() if s == PARTICIPANT_STATUS_ACTIVE)
    assert active_count == 2   # never reported/countable as 3


def test_7_triad_session_registers_all_three_as_active_participants_when_ready(isolate_provider_db, provider_state):
    """Test B: TRIAD session start -> three providers all registered as real, active participants."""
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    readiness = assess_triad_readiness(pm)
    assert readiness["state"] == TRIAD_READY

    rsm = ResearchStateMachine()
    rsm.create_session("R-TEST-3", "C:/proj", "beat the market", project_type="EXISTING", cycle_budget=5)
    session = rsm.get_session("R-TEST-3")
    assert session["status"] == SESSION_STATUS_ACTIVE and session["cycle_budget"] == 5 and session["cycles_completed"] == 0

    cid = "R-TEST-3-C1"
    rsm.create_cycle(cid, "R-TEST-3", 1)
    cycle = rsm.get_cycle(cid)
    assert cycle["stage"] == ResearchState.RESEARCH_INTAKE

    rsm.register_participants(cid, {d: PARTICIPANT_STATUS_ACTIVE for d in readiness["usable"]})
    participants = rsm.get_participants(cid)
    assert len(participants) == 3
    assert all(p["role"] == "RESEARCHER" and p["status"] == PARTICIPANT_STATUS_ACTIVE for p in participants)
    assert {p["domain"] for p in participants} == {"OPENAI", "ANTHROPIC", "GOOGLE"}


# ---------------------------------------------------------------------------------- cycle transitions

def test_8_full_happy_path_transition_sequence():
    rsm = ResearchStateMachine()
    rsm.create_session("R-TEST-4", "C:/proj", "moonshot")
    cid = "R-TEST-4-C1"
    rsm.create_cycle(cid, "R-TEST-4", 1)
    happy_path = [
        ResearchState.INDEPENDENT_RESEARCH, ResearchState.CROSS_CRITIQUE, ResearchState.SYNTHESIS,
        ResearchState.EXPERIMENT_PLANNING, ResearchState.EXPERIMENT_BUILDING, ResearchState.EXPERIMENT_VERIFYING,
        ResearchState.EXPERIMENT_REVIEW, ResearchState.RESULT_INTERPRETATION, ResearchState.MILESTONE_COMPLETE,
    ]
    for stage in happy_path:
        rsm.transition_cycle(cid, stage, details=f"reached {stage}")
    cycle = rsm.get_cycle(cid)
    assert cycle["stage"] == ResearchState.MILESTONE_COMPLETE
    transitions = rsm.get_cycle_transitions(cid)
    assert [t["to_stage"] for t in transitions] == [ResearchState.RESEARCH_INTAKE] + happy_path
    session = rsm.get_session("R-TEST-4")
    assert session["cycles_completed"] == 1   # incremented on reaching a terminal stage


def test_9_invalid_transition_rejected():
    rsm = ResearchStateMachine()
    rsm.create_session("R-TEST-5", "C:/proj", "moonshot")
    cid = "R-TEST-5-C1"
    rsm.create_cycle(cid, "R-TEST-5", 1)
    with pytest.raises(InvalidResearchTransitionError):
        rsm.transition_cycle(cid, ResearchState.SYNTHESIS)   # cannot skip INDEPENDENT_RESEARCH/CROSS_CRITIQUE
    assert rsm.get_cycle(cid)["stage"] == ResearchState.RESEARCH_INTAKE   # unchanged


def test_10_terminal_escape_to_human_required_from_any_non_terminal_stage():
    rsm = ResearchStateMachine()
    rsm.create_session("R-TEST-6", "C:/proj", "moonshot")
    cid = "R-TEST-6-C1"
    rsm.create_cycle(cid, "R-TEST-6", 1)
    rsm.transition_cycle(cid, ResearchState.INDEPENDENT_RESEARCH)
    rsm.transition_cycle(cid, ResearchState.CROSS_CRITIQUE)
    rsm.transition_cycle(cid, ResearchState.HUMAN_REQUIRED, details="ambiguous synthesis, needs a human call")
    assert rsm.get_cycle(cid)["stage"] == ResearchState.HUMAN_REQUIRED
    assert rsm.get_session("R-TEST-6")["cycles_completed"] == 1


def test_11_cannot_transition_out_of_a_terminal_stage():
    rsm = ResearchStateMachine()
    rsm.create_session("R-TEST-7", "C:/proj", "moonshot")
    cid = "R-TEST-7-C1"
    rsm.create_cycle(cid, "R-TEST-7", 1)
    rsm.transition_cycle(cid, ResearchState.RESEARCH_STOPPED, details="user stopped")
    with pytest.raises(InvalidResearchTransitionError):
        rsm.transition_cycle(cid, ResearchState.INDEPENDENT_RESEARCH)


def test_12_cycle_budget_bounded_autonomy(isolate_provider_db):
    """PHASE 17: research cycles are bounded, not infinite."""
    rsm = ResearchStateMachine()
    rsm.create_session("R-TEST-8", "C:/proj", "moonshot", cycle_budget=2)
    assert rsm.cycle_budget_exhausted("R-TEST-8") is False
    for n in (1, 2):
        cid = f"R-TEST-8-C{n}"
        rsm.create_cycle(cid, "R-TEST-8", n)
        rsm.transition_cycle(cid, ResearchState.RESEARCH_STOPPED, details="test")
    assert rsm.cycle_budget_exhausted("R-TEST-8") is True


def test_13_default_cycle_budget_applied_when_not_specified():
    rsm = ResearchStateMachine()
    rsm.create_session("R-TEST-9", "C:/proj", "moonshot")
    assert rsm.get_session("R-TEST-9")["cycle_budget"] == DEFAULT_CYCLE_BUDGET


def test_14_terminal_cycle_stages_constant_matches_transition_graph():
    """Every stage listed in TERMINAL_CYCLE_STAGES must genuinely have no forward transitions defined."""
    from core.research_state import VALID_RESEARCH_TRANSITIONS
    for stage in TERMINAL_CYCLE_STAGES:
        assert VALID_RESEARCH_TRANSITIONS.get(stage, []) == []

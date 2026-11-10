import tempfile
import pytest
from pathlib import Path
from core.state_machine import StateMachine, State, InvalidStateTransitionError

def test_state_machine_valid_progression():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        sm = StateMachine(str(db_path))

        run_id = "RUN-TEST-001"
        sm.create_run(run_id, "/dummy/project", "Test Goal")

        run = sm.get_run(run_id)
        assert run["current_state"] == State.IDLE

        # Normal sequence
        sequence = [
            State.PLANNING,
            State.ORDER_READY,
            State.BUILDING,
            State.VERIFYING,
            State.REVIEWING,
            State.DECIDING,
            State.PASS
        ]

        for next_state in sequence:
            sm.transition_to(run_id, next_state)
            assert sm.get_run(run_id)["current_state"] == next_state

        transitions = sm.get_transitions(run_id)
        # Initial create + 7 transitions = 8 rows
        assert len(transitions) == 8

def test_state_machine_invalid_transition():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        sm = StateMachine(str(db_path))

        run_id = "RUN-TEST-002"
        sm.create_run(run_id, "/dummy/project", "Test Goal")

        # Jumping directly from IDLE to BUILDING must raise InvalidStateTransitionError
        with pytest.raises(InvalidStateTransitionError):
            sm.transition_to(run_id, State.BUILDING)

def test_state_machine_fail_and_human_required():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        sm = StateMachine(str(db_path))

        # Test FAIL from BUILDING
        run_fail = "RUN-FAIL-001"
        sm.create_run(run_fail, "/dummy/project", "Test Fail")
        sm.transition_to(run_fail, State.PLANNING)
        sm.transition_to(run_fail, State.ORDER_READY)
        sm.transition_to(run_fail, State.BUILDING)
        sm.transition_to(run_fail, State.FAIL, details="Builder error")
        assert sm.get_run(run_fail)["current_state"] == State.FAIL

        # Cannot transition once terminal
        with pytest.raises(InvalidStateTransitionError):
            sm.transition_to(run_fail, State.VERIFYING)

        # Test HUMAN_REQUIRED from VERIFYING
        run_hr = "RUN-HR-001"
        sm.create_run(run_hr, "/dummy/project", "Test HR")
        sm.transition_to(run_hr, State.PLANNING)
        sm.transition_to(run_hr, State.ORDER_READY)
        sm.transition_to(run_hr, State.BUILDING)
        sm.transition_to(run_hr, State.VERIFYING)
        sm.transition_to(run_hr, State.HUMAN_REQUIRED, details="Scope violation")
        assert sm.get_run(run_hr)["current_state"] == State.HUMAN_REQUIRED

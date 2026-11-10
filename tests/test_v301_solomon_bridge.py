import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
import pytest
from core.solomon_bridge import SolomonBridge
from schemas.order import OrderSchema


@pytest.fixture
def bridge(tmp_path):
    project = tmp_path / 'project'; project.mkdir(); (project / '.git').mkdir()
    db_path = tmp_path / 'bridge.db'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE runs(run_id TEXT,goal TEXT,current_state TEXT,project_path TEXT,updated_at TEXT)')
    calls = []
    sm = SimpleNamespace(log_audit=lambda *a: None)
    orch = SimpleNamespace(sm=sm, reserve_run_id=lambda *a: 'RUN-BRIDGE-1',
                          run=lambda *a, **kw: calls.append(kw))
    d = SimpleNamespace(resolve_db_path=lambda: db_path,
        validate_project_path=lambda p: (True, str(Path(p).resolve())),
        _new_orchestrator=lambda: orch, _project_key=lambda p: p,
        _RUN_LOCK=threading.Lock(), _ACTIVE_PROJECTS={}, _project_busy=lambda *a: False,
        _record_human_required=lambda *a: 'RUN-APPROVAL', invalidate_status_cache=lambda: None)
    b = SolomonBridge(d, [project])
    p = {'request_id': 'request-12345', 'project_path': str(project),
         'order': OrderSchema(goal='Add feature', allowed_paths=['src/helper.py'], acceptance_criteria=['Tests pass']).model_dump()}
    return b, p, calls


def test_durable_idempotency_and_orchestrator_only(bridge):
    b, p, calls = bridge
    result = b.submit(p)
    assert result['status'] == 'submitted'
    assert b.submit(p)['status'] == 'duplicate'
    assert len(calls) == 1
    assert calls[0]['order'].allowed_paths == ['src/helper.py']
    assert SolomonBridge(b.d, b.allowed).submit(p)['run_id'] == result['run_id']


def test_same_id_changed_payload_rejected(bridge):
    b, p, _ = bridge; b.submit(p)
    p['order']['goal'] = 'Different'
    with pytest.raises(ValueError, match='request_id'):
        b.submit(p)


def test_unregistered_project_rejected(bridge):
    b, p, calls = bridge; p['project_path'] += '-outside'
    with pytest.raises(ValueError, match='허용'):
        b.submit(p)
    assert not calls


@pytest.mark.parametrize('change', [
    {'allowed_paths': []}, {'risk_level': 'HIGH'}, {'requires_human_approval': True},
    {'allowed_paths': ['../outside.py']}, {'allowed_paths': ['.agents/hooks.json']},
    {'allowed_deletions': ['src/helper.py']},
])
def test_unsafe_orders_stop_at_existing_approval_gate(bridge, change):
    b, p, calls = bridge; p['order'].update(change)
    result = b.submit(p)
    assert result['status'] == 'awaiting_approval'
    assert not calls


def test_invalid_limits_rejected(bridge):
    b, p, calls = bridge; p['order']['max_diff_lines'] = 9999
    with pytest.raises(ValueError): b.submit(p)
    assert not calls


def test_missing_acceptance_rejected(bridge):
    b, p, _ = bridge; p['order']['acceptance_criteria'] = []
    with pytest.raises(ValueError): b.submit(p)


def test_busy_is_durable_and_not_replayed(bridge):
    b, p, calls = bridge; b.d._project_busy = lambda *a: True
    with pytest.raises(ValueError, match='진행'): b.submit(p)
    assert b.submit(p)['status'] == 'duplicate'
    with b.connect() as db:
        assert db.execute('SELECT error FROM solomon_orders').fetchone()[0]
    assert not calls

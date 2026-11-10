"""v3.1.5 — 시스템 중지 (2026-11-02): POST /ops/shutdown stops the companion; token + local origin required."""
import http.client
import json
import threading
from types import SimpleNamespace
import pytest
from companion_server import CompanionHandler
from dashboard_server import DashboardServer


@pytest.fixture
def http_server():
    server = DashboardServer(('127.0.0.1', 0), CompanionHandler)
    server.bridge_token = 'test-token'
    server.stopping = False
    server.bridge = SimpleNamespace(submit=lambda p: {'status': 'submitted'}, list=lambda: [], allowed=set())
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield server, thread
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def request(server, method='POST', path='/ops/shutdown', headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=3)
    conn.request(method, path, headers=headers or {})
    response = conn.getresponse(); data = response.read(); code = response.status
    conn.close(); return code, data


def test_shutdown_requires_token(http_server):
    server, thread = http_server
    assert request(server)[0] == 403
    assert request(server, headers={'X-Solomon-Bridge': 'test-token', 'Origin': 'https://evil.example'})[0] == 403
    assert server.stopping is False and thread.is_alive()


def test_shutdown_stops_the_server(http_server):
    server, thread = http_server
    code, body = request(server, headers={'X-Solomon-Bridge': 'test-token'})
    assert code == 200 and json.loads(body)['stopping'] is True
    thread.join(timeout=5)
    assert not thread.is_alive() and server.stopping is True


def test_console_has_stop_button(http_server):
    server, _ = http_server
    code, body = request(server, 'GET', '/')
    assert code == 200 and 'id="btn-shutdown"'.encode() in body

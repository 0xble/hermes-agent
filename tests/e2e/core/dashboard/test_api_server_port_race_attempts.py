"""The api-server port-race retry judges each attempt by its own log output.

All attempts append to one gateway log. Before this, a genuine collision on the first port left
"already in use" in the shared tail, so every later attempt read it immediately and reported a
lost race on a port it never tried.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.e2e.core.dashboard import _issue_helpers as helpers


def _server(tmp_path, monkeypatch, *, ready: bool):
    log = tmp_path / "gateway.log"
    log.write_text("ERROR api_server: Port 47639 already in use\n", encoding="utf-8")
    server = object.__new__(helpers.GatewayApiServer)
    server.log_path = log
    server.key = "k"
    server.base = "http://127.0.0.1:1"
    server.proc = SimpleNamespace(pid=4242, returncode=None, poll=lambda: None)
    server._attempt_offset = log.stat().st_size
    body = {"pid": 4242} if ready else {}
    monkeypatch.setattr(helpers, "http_json", lambda *a, **k: (200, body))
    return server, log


def test_an_earlier_attempts_collision_does_not_fail_a_clean_attempt(tmp_path, monkeypatch):
    server, _ = _server(tmp_path, monkeypatch, ready=True)
    assert server._await_ready(timeout=5) is True


def test_this_attempts_own_collision_still_reports_a_lost_race(tmp_path, monkeypatch):
    server, log = _server(tmp_path, monkeypatch, ready=False)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("ERROR gateway.run: api_server: Port 51234 already in use\n")
    assert server._await_ready(timeout=5) is False


def test_an_attempt_that_never_becomes_ready_still_times_out(tmp_path, monkeypatch):
    server, _ = _server(tmp_path, monkeypatch, ready=False)
    with pytest.raises(AssertionError, match="never became ready"):
        server._await_ready(timeout=0.3)

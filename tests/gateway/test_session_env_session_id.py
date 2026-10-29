"""Regression: a gateway turn served by a CACHED agent still carries its session identity.

Live on 2026-10-07 (release d07f664d): every ``mcp__relay__send`` from a turn that reused the
session's cached agent failed ``caller_identity_required`` (11 of 11), while every turn that built
a fresh agent succeeded (11 of 11). ``_set_session_env`` bound every session var except
``session_id``, which ``set_session_vars`` then bound to ``""``; only agent construction
(``_publish_session_id``) ever filled it in, so a reused agent's turn had no caller and
``caller_identity_meta()`` returned None.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway import session_context
from gateway.config import Platform
from gateway.session import SessionContext, SessionSource


@pytest.fixture
def clean_session_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(session_context, "_session_context_engaged", False)
    session_context.reset_session_vars()
    yield tmp_path
    session_context.reset_session_vars()


def _runner():
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    return runner


def _context(session_id="20261007_075652_72673a47"):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="2027045491", chat_type="dm",
                           thread_id="286686", user_id="1", profile="default")
    return SessionContext(source=source, connected_platforms=[], home_channels={},
                          session_key="agent:main:telegram:dm:2027045491:286686", session_id=session_id)


def test_turn_binding_publishes_the_session_id_without_building_an_agent(clean_session_vars):
    tokens = _runner()._set_session_env(_context())
    try:
        assert session_context.bound_session_env("HERMES_SESSION_ID") == "20261007_075652_72673a47"
    finally:
        session_context.clear_session_vars(tokens)


def test_cached_agent_turn_sends_mcp_caller_identity(clean_session_vars, monkeypatch):
    """The exact relay failure: no agent construction this turn, so only the turn binding exists."""
    from tools import mcp_tool_caller
    monkeypatch.setattr(mcp_tool_caller, "_topic_session_id", lambda sid, _db: sid)
    os_env_other = "some-other-session"
    monkeypatch.setenv("HERMES_SESSION_ID", os_env_other)  # last-writer-wins mirror must not leak in

    tokens = _runner()._set_session_env(_context())
    try:
        meta = mcp_tool_caller.caller_identity_meta()
    finally:
        session_context.clear_session_vars(tokens)

    assert meta == {"hermes/caller": {"profile": "default", "session_id": "20261007_075652_72673a47",
                                      "topic_session_id": "20261007_075652_72673a47"}}


def test_context_without_a_session_still_binds_empty(clean_session_vars):
    """No session entry (pre-resolution paths) keeps today's fail-closed empty binding."""
    tokens = _runner()._set_session_env(_context(session_id=""))
    try:
        assert session_context.bound_session_env("HERMES_SESSION_ID") == ""
    finally:
        session_context.clear_session_vars(tokens)

"""Session controls in a multiplexed gateway: each profile's records stay in that profile's store."""
from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def two_homes(tmp_path, monkeypatch):
    from hermes_cli import goals
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_state_registry import acquire, release_or_close

    default_home, secondary_home = tmp_path / "default", tmp_path / "secondary"
    dbs = {}
    for name, home in (("default", default_home), ("secondary", secondary_home)):
        home.mkdir()
        db = acquire(home / "state.db")
        for sid in ("requester", "target"):
            db.create_session(sid, "telegram", profile_name=name, chat_id=f"{name}-{sid}",
                              chat_type="private", session_key=f"agent:{name}:telegram:dm:{sid}")
        dbs[name] = (home, db)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    goals._DB_CACHE.clear()
    for _name, (home, db) in dbs.items():
        goals._DB_CACHE[str(home)] = db

    @contextlib.contextmanager
    def scope(home):
        token = set_hermes_home_override(str(home))
        try:
            yield
        finally:
            reset_hermes_home_override(token)

    yield dbs, scope
    goals._DB_CACHE.clear()
    for _name, (_home, db) in dbs.items():
        release_or_close(db)


@contextlib.asynccontextmanager
async def _async_scope(home):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


@pytest.mark.asyncio
async def test_drain_visits_every_served_profile_store_in_its_own_scope(two_homes, monkeypatch):
    from gateway import run
    from gateway.run_session_controls import GatewaySessionControlsMixin
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import pending_outbox, request_control
    from hermes_constants import get_hermes_home

    dbs, scope = two_homes
    secondary_home = dbs["secondary"][0]
    with scope(secondary_home):
        GoalManager("target").set("secondary objective")
        secondary_request = request_control("goal", "clear", "target", requester_sid="requester")
    assert pending_outbox() == []  # the ambient (default) store holds nothing

    seen = []

    class Runner(GatewaySessionControlsMixin):
        config = SimpleNamespace(multiplex_profiles=True)

        async def _run_in_executor_with_context(self, func, *args):
            return func(*args)

        async def _warm_goals_session_db(self, _label):
            return None

        async def _drain_session_control_record(self, record, _session_controls):
            seen.append((str(get_hermes_home()), record["id"]))

    monkeypatch.setattr(run, "_multiplex_profile_homes",
                        lambda _cfg: [("default", None), ("secondary", secondary_home)])
    monkeypatch.setattr(run, "_async_profile_runtime_scope", _async_scope)
    await Runner()._drain_session_controls()
    assert seen == [(str(secondary_home), secondary_request["id"])]


@pytest.mark.asyncio
async def test_button_resolves_in_the_profile_that_owns_the_chat(two_homes):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import _load_record, request_control
    from plugins.platforms.telegram.adapter import TelegramAdapter

    dbs, scope = two_homes
    secondary_home = dbs["secondary"][0]
    with scope(secondary_home):
        GoalManager("target").set("secondary objective")
        request = request_control("goal", "pause", "target", requester_sid="requester")

    adapter = object.__new__(TelegramAdapter)
    adapter._callback_authorized = AsyncMock(return_value=True)
    adapter._edit_html_quiet = AsyncMock()
    adapter.build_source = lambda **kw: SimpleNamespace(**kw)
    adapter._normalize_chat_type = staticmethod(lambda chat_type, is_forum=False: chat_type or "dm")
    adapter.gateway_runner = SimpleNamespace(
        _async_profile_scope_for_source=lambda source: _async_scope(secondary_home))
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=99, first_name="Brian"), answer=AsyncMock(),
        message=SimpleNamespace(text="Approval needed\nChange: Goal Pause"),
    )
    cb = {"chat_id": "secondary-target", "chat_type": "private", "thread_id": None, "user_name": "Brian"}
    await adapter._handle_session_control_callback(query, f"ctl:a:{request['id']}", cb)

    assert "Goal paused" in query.answer.await_args.kwargs["text"]
    with scope(secondary_home):
        assert _load_record(request["id"])["status"] == "applied"
        assert load_goal("target").status == "paused"


@pytest.mark.parametrize("action,expected", [("pause", "Loop paused"), ("resume", "Loop resumed"), ("stop", "Loop stopped")])
def test_session_control_loop_outcome_uses_loop_label(action, expected):
    from plugins.platforms.telegram.session_control_cards import session_control_outcome

    label, detail = session_control_outcome(
        {"status": "applied", "kind": "loop", "action": action}, "Brian")
    assert label == f"✓ {expected} by Brian"
    assert detail == ""

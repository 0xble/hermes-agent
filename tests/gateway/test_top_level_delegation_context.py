"""Gateway dispatch owns a new turn without consuming its inline caller's identity."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent.delegation_context import (
    DELEGATED_CHILD_ENV_MARKER,
    delegated_child_context,
    delegated_child_parent_session_id,
    delegated_child_root_session_id,
    is_delegated_child_context,
    is_delegated_child_process_context,
)
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import clear_session_vars, get_session_env, set_session_vars
from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


def _identity():
    return (
        is_delegated_child_context(),
        delegated_child_parent_session_id(),
        delegated_child_root_session_id(),
        get_session_env("HERMES_SESSION_ID"),
        get_session_env("HERMES_SESSION_PROFILE"),
        get_hermes_home(),
    )


def _runner(monkeypatch, dispatch):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    runner._draining = False
    runner._external_drain_active = False
    runner._is_user_authorized_for_source = lambda _source: True
    runner._admit_bot_message_for_source = lambda _source: True
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, _source: event)
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._hm_dispatch_idle_commands = AsyncMock(return_value=(False, None))
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._hm_try_passive_fyi_relay = AsyncMock(return_value=False)
    runner._mark_attended_user_activity = AsyncMock(return_value=False)
    runner._run_post_turn_hooks = AsyncMock()
    runner._clear_durable_active_turn = AsyncMock(return_value=True)
    runner._handle_message_with_agent = dispatch
    runner._get_executor = lambda: None
    # Avoid unrelated persistence and telemetry, not the admission or dispatch boundary.
    runner._persist_active_agents = lambda: None
    monkeypatch.setattr(
        "gateway.run_inbound_consumer.run_post_admission_hook",
        AsyncMock(return_value=(False, None)),
    )
    return runner


def _event(internal=True):
    return MessageEvent(
        text="continue the requested work",
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat",
            chat_type="dm",
            user_id="user",
            profile="routed",
        ),
        internal=internal,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True], ids=["user", "internal"])
@pytest.mark.parametrize(
    "process_marker", ["", "fenced-board"], ids=["in-process", "descendant-process"]
)
async def test_admitted_turn_owns_new_child_lineage(monkeypatch, tmp_path, internal, process_marker):
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, process_marker)
    if process_marker:
        monkeypatch.setenv("HERMES_DELEGATED_PARENT_SESSION_ID", "process-parent")
    else:
        monkeypatch.delenv("HERMES_DELEGATED_PARENT_SESSION_ID", raising=False)
    observed = {}
    dispatched = asyncio.Event()
    release = asyncio.Event()
    routed_home = tmp_path / "routed-home"

    async def dispatch(event, source, key, _generation):
        context = SessionContext(source, [], {}, session_key=key, session_id="target")
        with runner._session_env_scope(context):
            observed["turn"] = await runner._run_in_executor_with_context(
                lambda: (_identity(), is_delegated_child_process_context()),
            )
            with delegated_child_context("new-child"):
                observed["child"] = _identity()
                with delegated_child_context("new-grandchild"):
                    observed["grandchild"] = _identity()
            observed["after_children"] = _identity()
            dispatched.set()
            await release.wait()
        return "handled"

    runner = _runner(monkeypatch, dispatch)
    home_token = set_hermes_home_override(routed_home)
    session_tokens = set_session_vars(session_id="caller", profile="caller-profile")
    try:
        with delegated_child_context("origin", parent_session_id="topic"):
            with delegated_child_context("origin-nested"):
                before = _identity()
                turn = asyncio.create_task(runner._handle_message(_event(internal)))
                try:
                    await asyncio.wait_for(dispatched.wait(), timeout=5)
                    assert _identity() == before == (
                        True, "origin", "topic", "origin-nested", "caller-profile", routed_home,
                    )
                finally:
                    release.set()
                    await turn
        assert turn.result() == "handled"
        turn_identity = (
            False, "process-parent" if process_marker else "", "", "target", "routed", routed_home,
        )
        assert observed["turn"] == (turn_identity, bool(process_marker))
        assert observed["child"] == (True, "target", "target", "new-child", "routed", routed_home)
        assert observed["grandchild"] == (
            True, "new-child", "target", "new-grandchild", "routed", routed_home,
        )
        assert observed["after_children"] == turn_identity
        assert is_delegated_child_process_context() == bool(process_marker)
    finally:
        clear_session_vars(session_tokens)
        reset_hermes_home_override(home_token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [None, RuntimeError, asyncio.CancelledError],
    ids=["success", "error", "cancel"],
)
async def test_inline_dispatch_restores_callers_identity(monkeypatch, tmp_path, failure):
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_PARENT_SESSION_ID", raising=False)
    observed = []
    caller_home = tmp_path / "caller-home"
    routed_home = tmp_path / "routed-home"

    async def dispatch(event, source, key, _generation):
        context = SessionContext(source, [], {}, session_key=key, session_id="target")
        home_token = set_hermes_home_override(routed_home)
        try:
            with runner._session_env_scope(context):
                observed.append(await runner._run_in_executor_with_context(_identity))
                if failure is not None:
                    raise failure()
        finally:
            reset_hermes_home_override(home_token)
        return "handled"

    runner = _runner(monkeypatch, dispatch)
    home_token = set_hermes_home_override(caller_home)
    session_tokens = set_session_vars(session_id="caller", profile="caller-profile")
    try:
        with delegated_child_context("origin", parent_session_id="topic"):
            with delegated_child_context("origin-nested"):
                before = _identity()
                if failure is None:
                    assert await runner._handle_message(_event()) == "handled"
                else:
                    with pytest.raises(failure):
                        await runner._handle_message(_event())
                assert _identity() == before == (
                    True, "origin", "topic", "origin-nested", "caller-profile", caller_home,
                )
        assert observed == [(False, "", "", "target", "routed", routed_home)]
    finally:
        clear_session_vars(session_tokens)
        reset_hermes_home_override(home_token)

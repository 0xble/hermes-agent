"""Gateway binds user-requested goal releases to the authenticated event body."""

import asyncio
import json

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals
from tools.goal_authority import goal_authorization_task, goal_user_request_scope
from tools.goal_tool import set_goal_tool


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    yield
    goals._DB_CACHE.clear()


def runner_and_source():
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")})
    runner.adapters = {}
    runner._model = "test/model"
    runner._base_url = None
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="private", user_name="User")
    return runner, source


def resume_user_stop(session_id, *, user_requested):
    return json.loads(set_goal_tool(
        action="resume", session_id=session_id, turn_id="turn",
        goal_control_revision=goals.get_goal_control_revision(session_id),
        user_requested=user_requested,
    ))


@pytest.mark.asyncio
async def test_real_reply_renderer_binds_user_requested_resume_to_event_body():
    runner, source = runner_and_source()
    request = "Please continue working on the parser."
    event = MessageEvent(
        text=request, source=source, reply_to_message_id="42",
        reply_to_text="## Parser work\n1. Fix malformed records.",
    )
    rendered = await runner._prepare_inbound_message_text(event=event, source=source, history=[])
    assert rendered.startswith("[Replying to:")
    goals.GoalManager("reply").set("Fix parser")
    goals.GoalManager("reply").pause(user_requested=True)

    async def inner(message, *args, **kwargs):
        return await runner._run_in_executor_with_context(
            lambda: resume_user_stop("reply", user_requested=True)
        )

    runner._run_agent_inner = inner
    result = await runner._run_agent(rendered, "", [], source, "reply", goal_user_text=event.text)
    assert result["success"] is True
    state = goals.load_goal("reply")
    assert state is not None and state.status == "active"
    assert goal_authorization_task("reply", "local") == "local"


@pytest.mark.asyncio
async def test_authenticated_nonempty_body_releases_user_stop_without_magic_phrase():
    runner, source = runner_and_source()
    goals.GoalManager("quote").set("Fix parser")
    goals.GoalManager("quote").pause(user_requested=True)

    async def inner(message, *args, **kwargs):
        return resume_user_stop("quote", user_requested=True)

    runner._run_agent_inner = inner
    result = await runner._run_agent(
        '[Replying to: "Please continue working."]\n\nYes, continue.',
        "", [], source, "quote", goal_user_text="Yes, continue.",
    )
    assert result["success"] is True
    state = goals.load_goal("quote")
    assert state is not None and state.status == "active"


@pytest.mark.asyncio
async def test_internal_run_cannot_inherit_parent_user_requested_authority():
    runner, source = runner_and_source()
    request = "Please continue working."
    goals.GoalManager("internal").set("Fix parser")
    goals.GoalManager("internal").pause(user_requested=True)

    async def inner(message, *args, **kwargs):
        return resume_user_stop("internal", user_requested=True)

    runner._run_agent_inner = inner
    with goal_user_request_scope("internal", request):
        result = await runner._run_agent(request, "", [], source, "internal")
        assert result["error_code"] == "user_direction_required"
        assert goal_authorization_task("internal", None) == request
    assert goal_authorization_task("internal", "local") == "local"


@pytest.mark.asyncio
async def test_concurrent_sessions_and_exception_cleanup():
    runner, source = runner_and_source()
    entered = asyncio.Event()

    async def inner(message, context, history, source, session_id, **kwargs):
        if session_id == "one":
            entered.set()
            await asyncio.sleep(0)
        else:
            await entered.wait()
        assert goal_authorization_task(session_id, None) == message
        assert goal_authorization_task("other", "must not fall back") == ""
        if session_id == "one":
            raise ValueError("test error")
        return message

    runner._run_agent_inner = inner
    results = await asyncio.gather(
        runner._run_agent("first", "", [], source, "one", goal_user_text="first"),
        runner._run_agent("second", "", [], source, "two", goal_user_text="second"),
        return_exceptions=True,
    )
    assert isinstance(results[0], ValueError)
    assert results[1] == "second"
    assert goal_authorization_task("one", "local") == "local"

"""Regression coverage for exactly-once gateway goal bookkeeping."""

from __future__ import annotations

import sys
import threading
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


class _NoopAgent:
    def __init__(self, *args, **kwargs):
        self.tools = []
        self.model = kwargs.get("model", "test-model")
        self.provider = kwargs.get("provider", "test-provider")
        self.session_id = kwargs.get("session_id", "goal-session")
        self.context_compressor = None
        self.is_interrupted = False

    def run_conversation(
        self,
        user_message,
        conversation_history=None,
        task_id=None,
        persist_user_message=None,
        persist_user_timestamp=None,
    ):
        return {
            "final_response": "Partial progress.",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _setup_runner(monkeypatch, tmp_path):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _NoopAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    (tmp_path / "config.yaml").write_text(
        "agent:\n  model: test-model\n", encoding="utf-8"
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_hermes_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {"agent": {"model": "test-model"}},
    )
    monkeypatch.setattr(
        gateway_run, "_resolve_gateway_model", lambda config=None: "test-model"
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_adapter_for_source",
        lambda self, source: None,
    )

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(
        tools_config,
        "_get_platform_tools",
        lambda user_config, platform_key: {"core"},
    )

    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None, multiplex_profiles=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(
        session_id="goal-session"
    )
    runner.session_store.load_transcript.return_value = []
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    runner._gateway_loop = None
    return runner


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )


def _setup_handler_runner(monkeypatch, tmp_path):
    """Build the smallest real handler harness needed for return-boundary QA."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._begin_session_run_generation = lambda _key: 1
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id="goal-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_platform_message_id.return_value = False
    async def _run_agent_with_goal_state(**kwargs):
        kwargs["goal_post_turn_state"]["handled"] = True
        return {
            "final_response": "Partial progress.",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }

    runner._run_agent = AsyncMock(side_effect=_run_agent_with_goal_state)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner


@pytest.mark.asyncio
async def test_completed_gateway_response_is_goal_judged_once(monkeypatch, tmp_path):
    """The inner drain hook and outer hook must not judge the same response."""
    runner = _setup_runner(monkeypatch, tmp_path)
    source = _source()
    session_entry = SimpleNamespace(session_id="goal-session")
    runner._post_turn_goal_continuation = AsyncMock()
    runner._post_turn_loop_completion = AsyncMock()
    goal_post_turn_state = {}
    result = await runner._run_agent(
        message="Keep implementing the standing goal.",
        context_prompt="",
        history=[],
        source=source,
        session_id=session_entry.session_id,
        session_key="agent:main:telegram:dm:12345",
        goal_session_entry=session_entry,
        goal_post_turn_state=goal_post_turn_state,
    )

    assert runner._post_turn_goal_continuation.await_count == 1
    assert goal_post_turn_state == {"handled": True}

    # _handle_message_with_agent returns only text (or None after streaming),
    # so the event is the sole bridge that can carry the inner hook's marker
    # into the outer post-turn hook.
    event = SimpleNamespace(
        metadata={},
        _streamed_final_response=result["final_response"],
        _goal_post_turn_complete=goal_post_turn_state["handled"],
    )
    await runner._run_post_turn_hooks(
        agent_result=None,
        source=source,
        is_internal=False,
        event=event,
    )

    assert runner._post_turn_goal_continuation.await_count == 1
    runner._post_turn_goal_continuation.assert_awaited_once_with(
        session_entry=session_entry,
        source=source,
        final_response="Partial progress.",
        session_key="agent:main:telegram:dm:12345",
        enqueue_continuation=True,
        emit_status_notice=True,
    )


@pytest.mark.asyncio
async def test_handler_preserves_inner_goal_marker_for_outer_hook(monkeypatch, tmp_path):
    """One logical response must invoke goal continuation exactly once end to end."""
    runner = _setup_handler_runner(monkeypatch, tmp_path)
    source = _source()
    event = MessageEvent(text="Continue the goal.", source=source, message_id="m1")
    runner._post_turn_goal_continuation = AsyncMock()
    runner._post_turn_loop_completion = AsyncMock()
    goal_entry = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id="goal-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )

    async def _run_agent_after_inner_goal_hook(**kwargs):
        # Model the inner hook that already judged this response.  The optional
        # state holder is the fixed bridge; its absence exercises the old
        # raw-result/finalized-result boundary without requiring the new API.
        await runner._post_turn_goal_continuation(
            session_entry=goal_entry,
            source=source,
            final_response="Partial progress.",
        )
        goal_state = kwargs.get("goal_post_turn_state")
        if goal_state is not None:
            goal_state["handled"] = True
        return {
            "final_response": "Partial progress.",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "_goal_post_turn_complete": True,
        }

    runner._run_agent = AsyncMock(side_effect=_run_agent_after_inner_goal_hook)

    response = await runner._handle_message_with_agent(
        event,
        source,
        "agent:main:telegram:dm:12345",
        1,
    )
    assert response == "Partial progress."

    await runner._run_post_turn_hooks(
        agent_result=response,
        source=source,
        is_internal=False,
        event=event,
    )

    assert runner._post_turn_goal_continuation.await_count == 1
    assert not hasattr(event, "_goal_post_turn_complete")

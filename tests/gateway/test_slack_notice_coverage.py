"""Producer classification survives native handlers, drainers and transport boundaries."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import cli
import gateway.run as gateway_run
from hermes_cli.config_effective import load_user_config_effective

from agent.status_output import StatusOutputMixin
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, EphemeralReply, SendResult, as_command_reply
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, SessionStore, build_session_key


@pytest.fixture(autouse=True)
def _telegram_approval_surface(monkeypatch):
    """No shipped adapter renders Approve/Deny cards; these tests drive the retained request
    machinery as if the Telegram source still had one."""
    monkeypatch.setattr("hermes_cli.session_controls._APPROVABLE_SOURCES", frozenset({"telegram"}))


@pytest.fixture
def home(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli import goals

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    token = set_hermes_home_override(tmp_path)
    goals._DB_CACHE.clear()
    from hermes_state_registry import acquire
    goals._DB_CACHE[str(tmp_path)] = acquire(tmp_path / "state.db")
    # Pin the starting progress mode: /verbose cycles from it, and tier defaults differ by platform.
    (tmp_path / "config.yaml").write_text('display:\n  tool_progress_command: true\n  tool_progress: "off"\n')
    yield tmp_path
    from hermes_state_registry import release_or_close
    for db in goals._DB_CACHE.values():
        release_or_close(db)
    goals._DB_CACHE.clear()
    reset_hermes_home_override(token)


def _runner(home):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SessionStore(home / "sessions", runner.config)
    runner._busy_input_mode = "interrupt"
    runner._busy_text_mode = "interrupt"
    runner._verbose = False
    runner._show_reasoning = False
    runner._running_agents = {}
    runner._background_tasks = set()
    return runner


async def _close(runner):
    tasks = list(runner._background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    pool = getattr(runner, "_executor", None)
    if pool is not None:
        pool.shutdown(wait=True)
    runner.session_store.close_all_db_handles()


class _TextAdapter(BasePlatformAdapter):
    """Non-Slack transport recorder, all command delivery stays in the base adapter."""
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content="", **kwargs):
        self.sent.append((content, kwargs))
        return SendResult(success=True, message_id="sent")

    async def get_chat_info(self, chat_id):
        return {}


def _adapter(platform):
    if platform == Platform.SLACK:
        from plugins.platforms.slack.adapter import SlackAdapter
        adapter = SlackAdapter(PlatformConfig(enabled=True, token="test", extra={"auto_thread": True}))
        adapter._web_client = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ok": True, "ts": "sent"}),
            chat_update=AsyncMock(return_value={"ok": True, "ts": "sent"}),
        )
        adapter._app = SimpleNamespace(client=adapter._web_client)
        adapter._running = True
        adapter._get_client = MagicMock(return_value=adapter._web_client)
        adapter._bot_user_id = "BOT"
        return adapter
    adapter = _TextAdapter(PlatformConfig(enabled=True, token="test"), platform)
    adapter.sent = []
    return adapter


class _BoundaryAgent(StatusOutputMixin):
    def __init__(self, callback):
        self.model, self.provider, self.api_mode = "default", "nous", "chat_completions"
        self.status_callback = callback
        self.log_prefix = ""
        self._vprint = MagicMock()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.SLACK, Platform.TELEGRAM])
@pytest.mark.parametrize("suppressed", [False, True])
async def test_cached_attended_unavailable_is_one_context_diagnostic(home, monkeypatch, platform, suppressed):
    from gateway.attended_model import AttendedResolution
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from gateway.warning_notifications import DiagnosticText
    from hermes_cli.auth import AuthError

    runner, adapter = _runner(home), _adapter(platform)
    source = _event("", platform).source
    config = {"model": {"default": "default", "attended": {"model": "opus", "provider": "anthropic"}},
              "display": {"suppress_warning_notifications": suppressed}}
    decision = AttendedResolution("opus", "anthropic", "medium", "attended", True)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda *_a: "default")
    monkeypatch.setattr(gateway_run, "_get_channel_override", lambda *_a, **_kw: None)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {
        "provider": "nous", "api_mode": "chat_completions", "api_key": "test", "base_url": "https://test.invalid"})
    provider_resolution = MagicMock(side_effect=AuthError("attended credentials unavailable"))
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs_for_provider", provider_resolution)
    monkeypatch.setattr(runner, "_attended_resolution_for_session", lambda **_kw: decision)
    monkeypatch.setattr(runner, "_rehydrate_session_model_override", lambda _key: None)
    monkeypatch.setattr(runner, "_expire_attended_temporary_picks", lambda *_a: None)
    monkeypatch.setattr(runner, "_session_model_override", lambda _key: None)
    runner.adapters = {platform: adapter}
    arrived = asyncio.Event()
    original_send = adapter.send

    async def observe(*args, **kwargs):
        result = await original_send(*args, **kwargs)
        arrived.set()
        return result

    adapter.send = observe
    ctx = TurnContext(source=source, session_key="cached-session", user_config=config,
        _run_still_current=lambda: True, _status_adapter=adapter, _status_chat_id="C1",
        _status_thread_metadata={"thread_id": "123.0"}, _loop_for_step=asyncio.get_running_loop())
    turn = TurnRunner(runner, ctx)
    statuses = []

    def callback(kind, message):
        statuses.append((kind, message))
        turn._status_callback_sync(kind, message)

    agent = _BoundaryAgent(callback)
    # Another session's pending notice must survive this request boundary untouched.
    runner._commit_session_agent_runtime("other-session", ("other", {}, "other notice", False))
    try:
        await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
        await asyncio.to_thread(turn._apply_attended_before_api_call, agent)
        assert (agent.model, agent.provider, agent.api_mode) == ("default", "nous", "chat_completions")
        provider_resolution.assert_called_once_with("anthropic", target_model="opus")
        assert len(statuses) == 1
        kind, notice = statuses[0]
        assert kind == "lifecycle" and isinstance(notice, DiagnosticText)
        expected = "⚠️ Attended model unavailable; staying on default."
        assert notice == expected
        assert runner._consume_pre_agent_fallback_notice("cached-session") is None
        assert runner._consume_pre_agent_fallback_notice("other-session") == "other notice"
        if suppressed:
            assert not arrived.is_set()
            assert (adapter._web_client.chat_postMessage.call_count if platform == Platform.SLACK else len(adapter.sent)) == 0
        else:
            await asyncio.wait_for(arrived.wait(), 5)
            _assert_sent(adapter, platform == Platform.SLACK, expected)
            if platform == Platform.SLACK:
                payload = adapter._web_client.chat_postMessage.call_args.kwargs
                assert payload["text"] == expected
                assert payload["blocks"][0]["elements"][0]["text"] == expected
                assert adapter._web_client.chat_postMessage.call_count == 1
                adapter._web_client.chat_update.assert_not_awaited()
            else:
                assert len(adapter.sent) == 1
                assert adapter.sent[0] == (expected, {"metadata": {"thread_id": "123.0"}})
    finally:
        await _close(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Credentials unavailable. Configure a provider.",
    "Login required. Run /login.", "Model access denied. Choose another model."])
async def test_cached_action_needed_diagnostics_remain_full(home, message):
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    runner, adapter = _runner(home), _adapter(Platform.SLACK)
    runner.adapters = {Platform.SLACK: adapter}
    source = _event("").source
    arrived = asyncio.Event()
    original_send = adapter.send

    async def observe(*args, **kwargs):
        result = await original_send(*args, **kwargs)
        arrived.set()
        return result

    adapter.send = observe
    ctx = TurnContext(source=source, session_key="cached-session", user_config={},
        _run_still_current=lambda: True, _status_adapter=adapter, _status_chat_id="C1",
        _status_thread_metadata={"thread_id": "123.0"}, _loop_for_step=asyncio.get_running_loop())
    turn = TurnRunner(runner, ctx)
    agent = _BoundaryAgent(turn._status_callback_sync)
    runner._commit_session_agent_runtime("cached-session", ("default", {}, message, False))
    try:
        notice = runner._consume_pre_agent_fallback_notice("cached-session")
        assert notice == message
        assert getattr(notice, "notice_style", None) is None
        await asyncio.to_thread(agent._emit_diagnostic_status, notice)
        await asyncio.wait_for(arrived.wait(), 5)
        _assert_sent(adapter, False, message)
        assert runner._consume_pre_agent_fallback_notice("cached-session") is None
    finally:
        await _close(runner)


def _event(text, platform=Platform.SLACK):
    return MessageEvent(text=text, message_id="123.4", source=SessionSource(
        platform=platform, chat_id="C1", user_id="U1", chat_type="dm", thread_id="123.0"))


def _assert_sent(adapter, context, text=None):
    if adapter.platform == Platform.SLACK:
        payload = adapter._web_client.chat_postMessage.call_args.kwargs
        assert bool(payload.get("blocks") and payload["blocks"][0]["type"] == "context") is context
        assert payload["channel"] == "C1"
        assert payload["thread_ts"] == "123.0"
        if text is not None and not context:
            assert payload["text"] == adapter.format_message(str(text))
    else:
        content, kwargs = adapter.sent[-1]
        assert "notice_style" not in (kwargs.get("metadata") or {})
        assert kwargs["metadata"]["thread_id"] == "123.0"
        if text is not None:
            assert content == text


async def _command(runner, adapter, event, *, lane="inline"):
    async def dispatch(incoming):
        handled, result = await runner._hm_dispatch_canonical_command(
            incoming, incoming.source, build_session_key(incoming.source), incoming.get_command())
        assert handled
        dispatch.result = as_command_reply(result)
        return dispatch.result
    adapter.set_message_handler(dispatch)
    if lane == "inline":
        await adapter._dispatch_inline_reply(event)
    else:
        await adapter._process_message_background(event, build_session_key(event.source))
    return dispatch.result


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.SLACK, Platform.TELEGRAM])
@pytest.mark.parametrize("lane", ["inline", "background"])
@pytest.mark.parametrize("command,key,value", [
    ("/verbose", "tool_progress", "new"),
    ("/busy queue", "busy_input_mode", "queue"),
    ("/footer", "footer", True),
])
async def test_saved_settings_keep_style_and_ephemeral_contract(home, platform, lane, command, key, value):
    runner, adapter = _runner(home), _adapter(platform)
    try:
        result = await _command(runner, adapter, _event(command, platform), lane=lane)
        assert (isinstance(result, EphemeralReply)) is command.startswith("/busy")
        _assert_sent(adapter, platform == Platform.SLACK, result)
        config = load_user_config_effective(home / "config.yaml")
        saved = config["display"][key] if key == "busy_input_mode" else config["display"]["runtime_footer"]["enabled"] if key == "footer" else config["display"]["platforms"][platform.value][key]
        assert saved == value
    finally:
        await _close(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/busy status", "/busy nonsense", "/footer status", "/footer nonsense"])
async def test_settings_status_and_usage_are_full(home, command):
    runner, adapter = _runner(home), _adapter(Platform.SLACK)
    try:
        event = _event(command)
        # The legacy footer argument route reads ``message`` rather than MessageEvent.text.
        # Native no-argument toggling is exercised above without this compatibility field.
        if command.startswith("/footer"):
            event.message = event.text
        result = await _command(runner, adapter, event)
        _assert_sent(adapter, False, result)
    finally:
        await _close(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/verbose on", "/busy queue", "/footer off"])
async def test_failed_settings_saves_are_full(home, command):
    (home / "config.yaml").unlink()
    (home / "config.yaml").mkdir()  # real persistence failure, not a fabricated save receipt
    runner, adapter = _runner(home), _adapter(Platform.SLACK)
    try:
        event = _event(command)
        # The legacy footer argument route reads ``message`` rather than MessageEvent.text.
        # Native no-argument toggling is exercised above without this compatibility field.
        if command.startswith("/footer"):
            event.message = event.text
        result = await _command(runner, adapter, event)
        _assert_sent(adapter, False, result)
    finally:
        await _close(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("command,context", [
    ("/subgoal new criterion", True), ("/subgoal remove 1", True), ("/subgoal clear", True),
    ("/subgoal", False), ("/subgoal remove 99", False), ("/subgoal remove nope", False),
    ("/heartbeat every 10m check the build", True), ("/heartbeat resume", True),
    ("/heartbeat clear", True), ("/heartbeat status", False), ("/heartbeat pause", False),
    ("/heartbeat nonsense", False), ("/heartbeat every 1s check", False),
])
@pytest.mark.parametrize("platform", [Platform.SLACK, Platform.TELEGRAM])
async def test_autonomy_commands_from_real_managers_to_transport(home, command, context, platform):
    from hermes_cli.goals import GoalManager
    from hermes_cli.heartbeat import HeartbeatManager
    runner, adapter = _runner(home), _adapter(platform)
    event = _event(command, platform)
    try:
        entry = await runner.async_session_store.get_or_create_session(event.source)
        goal = GoalManager(entry.session_id)
        goal.set("watch the build")
        goal.add_subgoal("first criterion")
        heartbeat = HeartbeatManager(entry.session_id)
        heartbeat.set("check the build", 600)
        if command == "/heartbeat resume":
            heartbeat.pause()
        result = await _command(runner, adapter, event)
        _assert_sent(adapter, context and platform == Platform.SLACK, result)
        fresh = GoalManager(entry.session_id)
        if command == "/subgoal new criterion":
            assert fresh.state.subgoals[-1] == "new criterion"
        elif command in {"/subgoal remove 1", "/subgoal clear"}:
            assert fresh.state.subgoals == []
        elif command in {"/heartbeat resume", "/heartbeat every 10m check the build"}:
            assert HeartbeatManager(entry.session_id).state.status == "active"
    finally:
        await _close(runner)


@pytest.mark.asyncio
async def test_bare_control_usage_hint_intentionally_remains_full(home):
    from hermes_cli.goals import GoalManager
    from hermes_cli.goal_command import dispatch_goal_command
    mgr = GoalManager("goal")
    mgr.set("watch the build")
    mgr.pause("waiting")
    result = dispatch_goal_command(mgr, "resume ignored words", authorize_gate=lambda: None)
    assert "ignored" in result.output and "/goal -- <text>" in result.output
    assert getattr(result.output, "notice_style", None) is None
    adapter = _adapter(Platform.SLACK)
    await adapter._send_with_retry("C1", as_command_reply(result.output), metadata={"thread_id": "123.0"})
    _assert_sent(adapter, False, result.output)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,action", [("goal", "replace"), ("goal", "resume"), ("goal", "clear"), ("goal", "pause"),
                                          ("loop", "resume"), ("loop", "stop"), ("loop", "pause")])
@pytest.mark.parametrize("platform", [Platform.SLACK, Platform.TELEGRAM])
async def test_applied_control_outbox_to_adapter(home, kind, action, platform):
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager
    from hermes_cli import session_controls
    from gateway.run_session_controls import GatewaySessionControlsMixin
    runner, adapter = _runner(home), _adapter(platform)
    source = _event("", platform).source
    try:
        db = __import__("hermes_cli.goals", fromlist=["_get_session_db"])._get_session_db()
        db.create_session("requester", platform.value, profile_name="default", chat_id="R1")
        db.create_session("target", platform.value, profile_name="default", chat_id="C1")
        manager = GoalManager("target") if kind == "goal" else LoopManager("target")
        if kind == "goal":
            manager.set("watch the build")
        else:
            manager.set("check build", interval_seconds=600)
        if action == "resume":
            manager.pause("waiting") if kind == "goal" else manager.pause()
        quote = f"Please {action} the target {kind} now"
        if action == "replace":
            quote = "Please replace the target goal with watch the deployment"
        db.append_message("requester", "user", quote)
        applied = session_controls.apply_control(kind, action, "target", requester_sid="requester", user_quote=quote,
            payload={"goal": "watch the deployment"} if action == "replace" else None)
        assert applied.get("status") == "applied", applied
        record = applied["record"]
        text = GatewaySessionControlsMixin._control_notice(record)
        updated = await runner._drain_target_notice(record, session_controls, source, adapter)
        assert updated["target_notice_sent"] is True
        _assert_sent(adapter, action != "pause" and platform == Platform.SLACK, text)
        await runner._drain_target_notice(updated, session_controls, source, adapter)
        count = adapter._web_client.chat_postMessage.call_count if platform == Platform.SLACK else len(adapter.sent)
        assert count == 1  # no delivery or outbox semantics changed by styling
    finally:
        await _close(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace", "resume"])
async def test_autonomous_control_replay_uses_native_routing_and_outbox(home, action):
    from hermes_cli import session_controls
    from hermes_cli.goals import GoalManager, _get_session_db
    (home / "config.yaml").write_text("goals:\n  autonomy: true\n")
    runner, adapter = _runner(home), _adapter(Platform.SLACK)
    runner.adapters = {Platform.SLACK: adapter}
    source = _event("").source
    try:
        entry = await runner.async_session_store.get_or_create_session(source)
        manager = GoalManager(entry.session_id)
        manager.set("watch the build")
        if action == "resume":
            manager.pause("waiting")
        db = _get_session_db()
        # A routed session can precede its first model turn. Seed that turn's normal state row.
        if db.get_session(entry.session_id) is None:
            db.create_session(entry.session_id, "slack", profile_name="default", chat_id="C1")
        db.create_session("requester", "slack", profile_name="default", chat_id="R1")
        result = session_controls.apply_control("goal", action, entry.session_id, requester_sid="requester",
            payload={"goal": "watch the deployment"} if action == "replace" else None)
        assert result.get("status") == "applied", result
        record = result["record"]
        # Replay after the continuation was admitted, but before the target notice was posted.
        session_controls.mark_outbox(record["id"], "continuation_enqueued", True)
        await runner._drain_session_controls_in_scope()
        _assert_sent(adapter, True)
        settled = session_controls._load_record(record["id"])
        assert settled["target_notice_sent"] and settled["outbox_done"]
        await runner._drain_session_controls_in_scope()
        assert adapter._web_client.chat_postMessage.call_count == 1
    finally:
        await _close(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["denied", "expired", "failed"])
async def test_control_failure_outcomes_remain_full(home, status):
    from hermes_cli import session_controls
    from hermes_cli.goals import GoalManager, _get_session_db
    runner, adapter = _runner(home), _adapter(Platform.SLACK)
    try:
        db = _get_session_db()
        db.create_session("requester", "telegram", profile_name="default", chat_id="R1")
        db.create_session("target", "telegram", profile_name="default", chat_id="C1")
        GoalManager("target").set("watch the build")
        request = session_controls.request_control("goal", "resume", "target", requester_sid="requester")
        assert "id" in request, request
        if status == "denied":
            record = session_controls.resolve_request(request["id"], "deny", "U1")
        elif status == "expired":
            pending = session_controls._load_record(request["id"])
            pending["expires_at"] = 0
            session_controls._save_record(pending)
            record = session_controls.expire_request(request["id"])
        else:
            record = session_controls.fail_request(request["id"], "cannot apply")
        assert record["status"] == status
        await runner._drain_target_notice(record, session_controls, _event("").source, adapter)
        _assert_sent(adapter, False, runner._control_notice(record))
    finally:
        await _close(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.SLACK, Platform.TELEGRAM])
@pytest.mark.parametrize("mode,children,compression", [
    ("queue", False, False), ("steer", False, False), ("steer", True, False),
    ("redirect", False, False), ("interrupt", False, False),
    ("queue", True, False), ("queue", False, True),
])
@pytest.mark.parametrize("hint_seen", [False, True])
async def test_busy_ack_composition_and_send_metadata(home, platform, mode, children, compression, hint_seen):
    from agent.onboarding import BUSY_INPUT_FLAG, mark_seen
    if hint_seen:
        mark_seen(home / "config.yaml", BUSY_INPUT_FLAG)
    runner, adapter = _runner(home), _adapter(platform)
    adapter.send = AsyncMock(wraps=adapter.send)  # inspect, without replacing native formatting/delivery
    event = _event("follow up", platform)
    agent = SimpleNamespace(_active_children=[object()] if children else [])
    try:
        message = runner._compose_busy_ack_message(event, 200, None, agent,
            is_steer_mode=mode == "steer", is_queue_mode=mode == "queue", is_redirect_mode=mode == "redirect",
            demoted_for_subagents=children, demoted_for_compression=compression)
        await runner._send_busy_ack_reply(event, adapter, message)
        assert event._gateway_busy_ack_sent is True
        sent = adapter.send.await_args.kwargs
        assert sent["metadata"]["thread_id"] == "123.0"
        assert (sent["metadata"].get("notice_style") == "context") is (platform == Platform.SLACK)
        _assert_sent(adapter, platform == Platform.SLACK, message)
        if not hint_seen:
            from agent.onboarding import busy_input_hint_gateway
            assert busy_input_hint_gateway(mode) in message
    finally:
        await _close(runner)


@pytest.mark.asyncio
async def test_busy_helper_does_not_shrink_action_or_failure(home):
    runner, adapter = _runner(home), _adapter(Platform.SLACK)
    try:
        for text in ["Steering failed. Please retry.", "Approval required.", "Choose a running session."]:
            await runner._send_busy_reply(_event("follow up"), adapter, text)
            _assert_sent(adapter, False, text)
    finally:
        await _close(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.SLACK, Platform.TELEGRAM])
@pytest.mark.parametrize("producer", ["unmarked_info", "unmarked_success", "real_usage", "real_restored", "real_spent", "real_depleted"])
async def test_credit_notice_callback_uses_only_producer_marker(home, platform, producer):
    from agent.credits_tracker import AgentNotice, CreditsState, evaluate_credits_notices, new_credits_latch
    from gateway.run import render_notice_line
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    if producer.startswith("unmarked"):
        notice = AgentNotice("Unmarked credit information", level=producer.split("_")[1], key="credits.unmarked")
    else:
        state = CreditsState(denominator_kind="subscription_cap", subscription_limit_micros=100_000,
                             subscription_limit_usd="0.10", subscription_micros=50_000)
        latch = new_credits_latch()
        latch["seen_below_90"] = True
        if producer == "real_restored":
            latch["active"].add("credits.depleted")
        elif producer == "real_spent":
            state.subscription_micros = 0
            state.purchased_micros = 50_000
            state.purchased_usd = "0.05"
            latch["seen_grant_unspent"] = True
        elif producer == "real_depleted":
            state.paid_access = False
        notices, _clears = evaluate_credits_notices(state, latch)
        expected = {"real_usage": "credits.usage", "real_restored": "credits.restored",
                    "real_spent": "credits.grant_spent", "real_depleted": "credits.depleted"}[producer]
        notice = next(item for item in notices if item.key == expected)
    runner, adapter = _runner(home), _adapter(platform)
    event = _event("", platform)
    runner.adapters = {platform: adapter}
    arrived = asyncio.Event()
    original = adapter.send
    async def observe(*args, **kwargs):
        result = await original(*args, **kwargs)
        arrived.set()
        return result
    adapter.send = observe
    ctx = TurnContext(source=event.source, user_config={}, _run_still_current=lambda: True,
        _status_adapter=adapter, _loop_for_step=asyncio.get_running_loop())
    turn = TurnRunner(runner, ctx)
    try:
        await asyncio.to_thread(turn._notice_callback_sync, notice)
        await asyncio.wait_for(arrived.wait(), 5)
        context = producer in {"real_usage", "real_restored", "real_spent"}
        _assert_sent(adapter, context and platform == Platform.SLACK, render_notice_line(notice))
    finally:
        await _close(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_response", [False, True])
async def test_busy_saved_receipt_keeps_native_slash_reply_private(home, monkeypatch, failed_response):
    import time
    from plugins.platforms.slack import adapter as slack_module
    runner, adapter = _runner(home), _adapter(Platform.SLACK)
    adapter._web_client.chat_postEphemeral = AsyncMock(return_value={"ok": True, "ts": "private"})
    response = SimpleNamespace(status=503 if failed_response else 200,
                               content=SimpleNamespace(read=AsyncMock(return_value=b"unavailable")))
    post = MagicMock()
    post.return_value.__aenter__ = AsyncMock(return_value=response)
    post.return_value.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock(post=post)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(slack_module.aiohttp, "ClientSession", factory)
    adapter._slash_command_contexts[("C1", "U1")] = {
        "response_url": "https://slack.invalid/response", "user_id": "U1", "ts": time.monotonic()}
    token = slack_module._slash_user_id.set("U1")
    try:
        result = await _command(runner, adapter, _event("/busy queue"))
        assert isinstance(result, EphemeralReply)
        payload = post.call_args.kwargs["json"]
        assert payload["response_type"] == "ephemeral"
        assert payload["replace_original"] is True
        assert payload["blocks"][0]["type"] == "context"
        if failed_response:
            fallback = adapter._web_client.chat_postEphemeral.await_args.kwargs
            assert fallback["user"] == "U1" and fallback["channel"] == "C1"
            assert fallback["blocks"][0]["type"] == "context"
        else:
            adapter._web_client.chat_postEphemeral.assert_not_awaited()
        adapter._web_client.chat_postMessage.assert_not_awaited()
        text, ttl = adapter._unwrap_ephemeral(EphemeralReply(result, ttl_seconds=37))
        assert text == result and text.notice_style == "context" and ttl == 37
    finally:
        slack_module._slash_user_id.reset(token)
        await _close(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.SLACK, Platform.TELEGRAM])
@pytest.mark.parametrize("event", ["unwritable", "recovered"])
async def test_cron_store_notice_scoped_home_language_and_opt_out(home, monkeypatch, platform, event):
    from gateway.config import HomeChannel
    from gateway.cron_store_notices import send_cron_store_notice
    from cron.store_health import StoreDegraded, store_key
    from gateway.run import _async_profile_runtime_scope
    from agent.i18n import t

    profile = home / "profiles" / "company"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("display:\n  language: ru\n")
    monkeypatch.setattr("hermes_constants.get_routing_process_hermes_home", lambda: home)
    runner, adapter = _runner(home), _adapter(platform)
    other = _adapter(platform)
    cfg = PlatformConfig(enabled=True, token="test", home_channel=HomeChannel(platform, "C1", "Home", thread_id="123.0"))
    runner.config.platforms = {platform: cfg}
    runner.adapters = {platform: other}
    runner._profile_configs = {"company": GatewayConfig(platforms={platform: cfg})}
    runner._profile_adapters = {"company": {platform: adapter}}
    runner._served_profile_homes = {"company": profile}
    record = StoreDegraded(store_key(profile), 1, "ENOSPC: disk full")
    try:
        await send_cron_store_notice(runner, event, record)
        async with _async_profile_runtime_scope(profile):
            expected = t(f"gateway.cron_store.{event}", **record.notice_fields())
        _assert_sent(adapter, event == "recovered" and platform == Platform.SLACK, expected)
        assert (other._web_client.chat_postMessage.call_count if platform == Platform.SLACK else len(other.sent)) == 0
        before = adapter._web_client.chat_postMessage.call_count if platform == Platform.SLACK else len(adapter.sent)
        (profile / "config.yaml").write_text("display:\n  language: ru\n  suppress_warning_notifications: true\n")
        await send_cron_store_notice(runner, event, record)
        after = adapter._web_client.chat_postMessage.call_count if platform == Platform.SLACK else len(adapter.sent)
        assert after == before
    finally:
        await _close(runner)

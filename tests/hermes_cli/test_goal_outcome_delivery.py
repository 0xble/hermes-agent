"""Normal terminal delivery must commit goal-stop prose, not send a toast."""
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from hermes_cli import goals
from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin


@pytest.fixture
def outcome(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    manager = goals.GoalManager("outcome-delivery", default_max_turns=1)
    manager.set("Publish release notes")
    db = goals._get_session_db()
    db.create_session("outcome-delivery", source="cli")
    messages = [{"role": "user", "content": "Earlier"},
                {"role": "assistant", "content": "Drafted notes."},
                {"role": "user", "content": "Publish release notes"},
                {"role": "assistant", "content": "Drafted notes."}]
    db.replace_messages("outcome-delivery", messages)
    agent = SimpleNamespace(session_id="outcome-delivery", _session_db=db,
                            _session_messages=messages, _interrupt_requested=False)
    calls = []
    def llm(**kwargs):
        calls.append(kwargs)
        prompt = kwargs["messages"][0]["content"]
        text = ('{"already_explained": false, "supplement": "Publication remains. The turn budget is exhausted; resume after review."}'
                if "terminal assistant reply" in prompt else
                '{"verdict": "continue", "reason": "Publication remains"}')
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
    monkeypatch.setattr("agent.auxiliary_client.call_llm", llm)
    (tmp_path / "config.yaml").write_text("goals:\n  auto_notices: false\n")
    yield manager, agent, messages, calls
    goals._DB_CACHE.clear()


@pytest.mark.parametrize("change", ["pause", "clear", "replace", "edit", "revision", "interrupt", "error_status", "unchanged"])
def test_prepared_continuation_rechecks_late_authority(outcome, monkeypatch, change):
    from hermes_cli.goal_outcomes import prepare_goal_turn
    from tui_gateway import server as prompt_turn
    manager, agent, messages, calls = outcome
    manager.state.max_turns = 5
    manager._save()
    result = {"messages": messages, "final_response": "Drafted notes.", "completed": True}
    prepare_goal_turn(manager, agent, result)
    assert result["_goal_decision"]["should_continue"]
    if change == "pause":
        manager.pause("user requested")
    elif change == "clear":
        manager.clear()
    elif change == "replace":
        manager.set("Do different work")
    elif change == "edit":
        manager.state.contract.constraints = "Never publish"
        manager._save()
    elif change == "revision":
        goals.advance_goal_control_revision(agent.session_id)
    elif change == "interrupt":
        result["interrupted"] = True
    monkeypatch.setattr(prompt_turn, "_emit", lambda *a, **kw: None)
    status = "error" if change == "error_status" else "complete"
    prompt = prompt_turn._goal_followup_after_turn(
        "sid", {"session_key": agent.session_id, "agent": agent}, result, status, result["final_response"])
    assert bool(prompt) is (change == "unchanged")


def test_exception_outcome_retains_actual_failure_detail(outcome, monkeypatch):
    from hermes_cli.goal_outcomes import prepare_goal_turn
    manager, agent, messages, _calls = outcome
    def unavailable(*args, **kwargs):
        raise TimeoutError("classifier unavailable")
    monkeypatch.setattr(goals, "_call_goal_judge_llm", unavailable)
    result = {"messages": messages, "final_response": "", "failed": True,
              "error": "Provider authentication expired", "turn_exit_reason": "exception"}
    prepare_goal_turn(manager, agent, result)
    assert "Provider authentication expired" in result["final_response"]
    assert manager.state.status == "paused"
    assert agent._session_db.get_messages(agent.session_id)[-1]["content"] == result["final_response"]


@pytest.mark.parametrize("failure", [None, "nonempty", "empty"])
def test_cli_settle_commits_normal_stop_reply(outcome, failure):
    manager, agent, messages, calls = outcome
    result = {"messages": messages, "final_response": "Drafted notes.", "completed": True}
    if failure:
        result.update(failed=True, error="provider unavailable", completed=False)
        if failure == "empty":
            result["final_response"] = ""
            messages.pop()
    ui = MagicMock()
    ui.agent, ui.session_id = agent, agent.session_id
    ui._prompt_start_time = None
    ui.conversation_history = messages
    ui._get_goal_manager.return_value = manager
    ui._should_exit = False
    turn = SimpleNamespace(result=result, use_streaming_tts=False)
    CLIChatTurnMixin._chat_settle_turn(ui, turn)
    assert "resume after review" in result["final_response"]
    assert goals.GoalManager(agent.session_id).state.status == "paused"
    stored = agent._session_db.get_messages(agent.session_id)
    assert stored[-1]["content"] == result["final_response"] == messages[-1]["content"]
    assert stored[1]["content"] == "Drafted notes."
    assert len(stored) == 4
    assert all(not call.get("tools") for call in calls)
    ui._chat_resolve_interrupt.return_value = (None, False)
    ui._voice_tts = False
    assert CLIChatTurnMixin._chat_render_turn(ui, turn, None, None) == result["final_response"]
    ui._chat_print_response_panel.assert_called_once_with(turn, stored[-1]["content"])
    from hermes_state import SessionDB
    reloaded = SessionDB(agent._session_db.db_path)
    try:
        assert reloaded.get_messages(agent.session_id)[-1]["content"] == result["final_response"]
    finally:
        reloaded.close()


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("failure", [None, "nonempty", "empty", "exception", "auth", "setup",
                                     "exception_secret", "auth_secret", "setup_secret"])
def test_gateway_seals_and_persists_the_prepared_reply(outcome, streamed, failure, monkeypatch):
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from gateway.config import Platform
    from gateway.session import SessionSource
    manager, agent, messages, calls = outcome
    agent.model = "test"
    agent.tools = []
    secret = "sk-test-credential-1234567890abcdefghijklmnopqrstuvwxyz"
    error = f"provider unavailable, Authorization: Bearer {secret}" if str(failure).endswith("_secret") else "provider unavailable"
    failure_kind = str(failure).removesuffix("_secret")
    def run(*a, **kw):
        if failure_kind == "exception":
            raise RuntimeError(error)
        return {"messages": messages, "final_response": "" if failure == "empty" else "Drafted notes.",
                "completed": not bool(failure), "failed": bool(failure),
                "error": "provider unavailable" if failure else None}
    agent.run_conversation = run
    runner = MagicMock()
    runner.config = SimpleNamespace(streaming=None)
    runner._provider_routing = {}
    runner._agent_cache_lock = None
    runner._agent_cache = {}
    runner._session_db = None
    runner._prefill_messages = None
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner.session_store._entries = {}
    runner._get_system_prompt_for_channel.return_value = None
    runner._resolve_session_agent_runtime.return_value = ("test", {})
    runner._resolve_session_reasoning_config.return_value = None
    runner._resolve_session_service_tier.return_value = None
    runner._resolve_turn_agent_config.return_value = {"model": "test", "runtime": {}}
    runner._agent_config_signature.return_value = ("test",)
    runner._extract_cache_busting_config.return_value = {}
    runner._refresh_fallback_model.return_value = None
    runner._consume_pending_native_image_paths.return_value = []
    runner._consume_pending_turn_sidecar_notes.return_value = []
    runner._is_telegram_topic_lane.return_value = False
    runner._is_discord_auto_thread_lane.return_value = False
    runner._is_relay_discord_channel_lane.return_value = False
    ctx = TurnContext(source=SessionSource(platform=Platform.TELEGRAM, chat_id="test", user_id="test"),
        message="Publish release notes", history=messages[:2], session_id=agent.session_id,
        session_key="route", user_config={}, AIAgent=lambda **kw: agent,
        resolve_display_setting=lambda *a: False, _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False))
    turn = TurnRunner(runner, ctx)
    if failure_kind == "auth":
        runner._resolve_session_agent_runtime.side_effect = RuntimeError(error)
    if failure_kind == "setup":
        monkeypatch.setattr(turn, "_combined_ephemeral_prompt", lambda: (_ for _ in ()).throw(RuntimeError(error)))
    sealed = []
    consumer = SimpleNamespace(finish=lambda text=None: sealed.append(text)) if streamed else None
    monkeypatch.setattr(turn, "_setup_stream_consumer", lambda *a: (consumer, None, None, None, False))
    result = turn.run_sync()
    assert "resume after review" in result["final_response"]
    assert agent._session_db.get_messages(agent.session_id)[-1]["content"] == result["final_response"]
    if str(failure).endswith("_secret"):
        import json
        assert secret not in json.dumps(result)
        assert secret not in json.dumps(agent._session_db.get_messages(agent.session_id))
        assert secret not in json.dumps(calls)
        assert secret not in json.dumps(sealed)
        assert "provider unavailable" in result["final_response"]
        assert goals.GoalManager(agent.session_id).state.status == "paused"
    if streamed and failure_kind in {"auth", "setup"}:
        assert sealed == []  # failure precedes stream creation; ordinary final send owns it
    elif streamed:
        assert sealed == [result["final_response"]]
        import asyncio
        from gateway.run_turn import GatewayTurnMixin
        consumer.final_content_delivered = True
        consumer.delivered_final_matches = lambda text: text == sealed[0]
        ctx.stream_consumer_holder[0] = consumer
        runner._run_agent_stream_confirmed_final_delivery.return_value = True
        asyncio.run(GatewayTurnMixin._run_agent_mark_streamed_delivery(runner, result, ctx))
        assert result.get("already_sent") is True
    assert len(agent._session_db.get_messages(agent.session_id)) == 4


@pytest.mark.parametrize("failure", [None, "nonempty", "empty", "exception"])
def test_tui_submit_delivers_and_commits_the_prepared_reply(outcome, monkeypatch, tmp_path, failure):
    import threading
    from tui_gateway import server
    manager, agent, messages, calls = outcome
    def run(*a, **kw):
        if failure == "exception":
            raise RuntimeError("provider unavailable")
        return {"messages": messages, "final_response": "" if failure == "empty" else "Drafted notes.",
                "completed": not bool(failure), "failed": bool(failure),
                "error": "provider unavailable" if failure else None}
    agent.run_conversation = run
    agent.clear_interrupt = lambda: None
    class InlineThread:
        def __init__(self, target=None, **kw): self.target = target
        def start(self): self.target()
        def is_alive(self): return False
    monkeypatch.setattr(server.threading, "Thread", InlineThread)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_wire_callbacks", lambda *a: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *a: None)
    monkeypatch.setattr(server, "_session_cwd", lambda *a: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_get_usage", lambda *a: {})
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda name, sid, payload=None: emitted.append((name, payload)))
    session = dict(agent=agent, session_key=agent.session_id, history=messages[:2],
        history_lock=threading.Lock(), history_version=0, running=True,
        attached_images=[], image_counter=0, cols=80, slash_worker=None,
        show_reasoning=False, tool_progress_mode="all", inflight_turn=None)
    server._run_prompt_submit("rid", "sid", session, "Publish release notes")
    completed = [p for name, p in emitted if name == "message.complete"]
    assert len(completed) == 1
    text = completed[0]["text"]
    assert "resume after review" in text
    assert session["history"][-1]["content"] == text
    assert agent._session_db.get_messages(agent.session_id)[-1]["content"] == text
    assert not [p for name, p in emitted if name == "status.update" and p.get("kind") == "goal"]

@pytest.mark.parametrize("mode", ["complete", "partial", "malformed", "timeout", "cancel", "revision"])
def test_cli_outcome_semantics_and_late_control(outcome, monkeypatch, mode):
    from hermes_cli.goal_outcomes import prepare_goal_turn, consume_goal_decision
    manager, agent, messages, calls = outcome
    current = "Notes are drafted; publication remains. The goal turn budget is exhausted. Please resume after review."
    messages[-1]["content"] = current
    result = {"messages": messages, "final_response": current, "completed": True}
    original = __import__("agent.auxiliary_client", fromlist=["call_llm"]).call_llm
    def llm(**kw):
        if "terminal assistant reply" not in kw["messages"][0]["content"]:
            return original(**kw)
        calls.append(kw)
        if mode == "timeout": raise TimeoutError()
        if mode == "cancel": agent._interrupt_requested = True
        if mode == "revision": goals.advance_goal_control_revision(agent.session_id)
        raw = {"complete": '{"already_explained":true}',
               "partial": '{"already_explained":false,"supplement":"Review before resuming."}',
               "malformed": 'not json'}.get(mode, '{"already_explained":false,"supplement":"New explanation."}')
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
    monkeypatch.setattr("agent.auxiliary_client.call_llm", llm)
    prepare_goal_turn(manager, agent, result)
    if mode in {"complete", "cancel", "revision"}:
        assert result["final_response"] == current
    elif mode == "partial":
        assert result["final_response"] == current + "\n\nReview before resuming."
    else:
        assert "could not verify further completion" in result["final_response"]
        assert "turn budget exhausted" in result["final_response"]
    assert not manager.is_active()
    count = len(calls)
    prepare_goal_turn(manager, agent, result)
    consume_goal_decision(result)
    assert consume_goal_decision(result) == {}
    assert len(calls) == count


@pytest.mark.parametrize("flags", [{"interrupted": True}, {"turn_exit_reason": "interrupted_by_user", "failed": True}, {"compression_deferred": True},
                                   {"compression_exhausted": True}, {}, {"waiting": True}])
def test_intentional_empty_wait_and_compression_do_not_become_failures(outcome, flags):
    from hermes_cli.goal_outcomes import prepare_goal_turn
    manager, agent, messages, calls = outcome
    if flags.get("waiting"):
        manager._apply_wait_directive({"seconds": 60}, "waiting for completion")
    result = {"messages": messages, "final_response": "", **flags}
    prepare_goal_turn(manager, agent, result)
    assert result["final_response"] == ""
    assert manager.is_active()
    assert not calls


@pytest.mark.asyncio
async def test_suppressed_inner_drain_and_outer_replay_never_rejudge_or_resume(outcome):
    from gateway.run_goals import GatewayGoalsMixin
    from hermes_cli.goal_outcomes import prepare_goal_turn
    manager, agent, messages, calls = outcome
    result = {"messages": messages, "final_response": "Drafted notes.", "completed": True}
    prepare_goal_turn(manager, agent, result)
    class Runner(GatewayGoalsMixin):
        def __init__(self): self.notices = []; self.enqueued = []
        async def _post_turn_manager(self, *a): return manager
        async def _run_in_executor_with_context(self, fn): return fn()
        async def _defer_goal_status_notice_after_delivery(self, source, text): self.notices.append(text)
        def _enqueue_goal_continuation(self, **kw): self.enqueued.append(kw)
    runner = Runner()
    count = len(calls)
    for suppressed in (True, False, False):
        await runner._post_turn_goal_continuation(session_entry=SimpleNamespace(session_id=agent.session_id),
            source=SimpleNamespace(), final_response=result["final_response"], session_key="route",
            enqueue_continuation=True, emit_status_notice=not suppressed, agent_result=result)
    assert "resume after review" in result["final_response"]
    assert len(calls) == count
    assert runner.notices == runner.enqueued == []


def test_output_auxiliary_wall_deadline_has_no_late_side_effects(outcome, monkeypatch):
    import threading
    import time
    from hermes_cli.goal_outcomes import prepare_goal_turn
    manager, agent, messages, calls = outcome
    release = threading.Event()
    entered = threading.Event()
    original = __import__("agent.auxiliary_client", fromlist=["call_llm"]).call_llm
    def llm(**kw):
        if "terminal assistant reply" not in kw["messages"][0]["content"]:
            return original(**kw)
        entered.set()
        release.wait(2)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=
            '{"already_explained":false,"supplement":"LATE OUTPUT MUST NOT APPEAR"}'))])
    monkeypatch.setattr("agent.auxiliary_client.call_llm", llm)
    monkeypatch.setattr(goals, "_goal_judge_timeout", lambda: 0.1)
    result = {"messages": messages, "final_response": "Drafted notes.", "completed": True}
    start = time.monotonic()
    try:
        prepare_goal_turn(manager, agent, result)
        assert time.monotonic() - start < 1
        assert entered.is_set()
        assert "could not verify further completion" in result["final_response"]
        assert "LATE OUTPUT" not in result["final_response"]
        assert not manager.is_active()
    finally:
        release.set()


def test_current_tool_call_tail_and_prior_turn_survive(outcome):
    from hermes_cli.goal_outcomes import prepare_goal_turn
    manager, agent, messages, calls = outcome
    messages[-1] = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-one", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]}
    messages.append({"role": "tool", "tool_call_id": "call-one", "content": "draft generated"})
    result = {"messages": messages, "final_response": "", "failed": True, "error": "provider stopped"}
    prepare_goal_turn(manager, agent, result)
    stored = agent._session_db.get_messages(agent.session_id)
    assert len(stored) == 6
    assert stored[1]["content"] == "Drafted notes."
    assert stored[3]["tool_calls"][0]["id"] == "call-one"
    assert stored[4]["content"] == "draft generated"
    assert stored[-1]["content"] == result["final_response"]


@pytest.mark.parametrize("with_tools", [False, True])
def test_cli_worker_exception_has_normal_persisted_outcome(outcome, with_tools):
    manager, agent, messages, calls = outcome
    agent._session_messages = messages[:2]
    agent._session_db.replace_messages(agent.session_id, messages[:2])
    def fail(**kw):
        if with_tools:
            agent._session_messages = messages[:3] + [
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "failed-turn-tool", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "failed-turn-tool", "content": "draft generated"}]
            agent._session_db.replace_messages(agent.session_id, agent._session_messages)
        raise RuntimeError("provider went away")
    agent.run_conversation = fail
    ui = MagicMock()
    ui.agent, ui.session_id = agent, agent.session_id
    ui._prompt_start_time = None
    ui._should_exit = False
    ui._get_goal_manager.return_value = manager
    ui.conversation_history = messages[:-1]
    ui._pending_model_switch_note = None
    ui._pending_skills_reload_note = None
    ui._pending_turn_reasoning_config = None
    ui._pending_moa_config = None
    ui._pending_one_turn_model_restore = None
    turn = SimpleNamespace(voice_prefix="", stream_callback=None, result=None,
                           use_streaming_tts=False)
    CLIChatTurnMixin._chat_run_agent(ui, turn, "Publish release notes")
    assert turn.result["failed"]
    CLIChatTurnMixin._chat_settle_turn(ui, turn)
    assert "resume after review" in turn.result["final_response"]
    stored = agent._session_db.get_messages(agent.session_id)
    assert stored[1]["content"] == "Drafted notes."
    assert stored[-1]["content"] == turn.result["final_response"]
    assert len(stored) == (6 if with_tools else 4)
    assert stored[2]["content"] == "Publish release notes"
    if with_tools:
        assert stored[3]["tool_calls"][0]["id"] == "failed-turn-tool"
        assert stored[4]["content"] == "draft generated"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, True, False])
async def test_automatic_transition_policy_preserves_default(outcome, monkeypatch, configured):
    from gateway.run_goals import GatewayGoalsMixin
    from hermes_cli.goal_outcomes import prepare_goal_turn
    manager, agent, messages, calls = outcome
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {
        "goals": {} if configured is None else {"auto_notices": configured}})
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"verdict":"done","reason":"Published"}'))]))
    result = {"messages": messages, "final_response": "Published.", "completed": True}
    prepare_goal_turn(manager, agent, result)
    assert result["_goal_decision"]["verdict"] == "done"
    expected_notice = result["_goal_decision"]["message"]
    assert expected_notice
    class Runner(GatewayGoalsMixin):
        def __init__(self): self.notices = []
        async def _post_turn_manager(self, *a): return manager
        async def _run_in_executor_with_context(self, fn): return fn()
        # The continuation hook now runs at the successful delivery receipt.
        async def _send_goal_status_notice(self, source, message): self.notices.append(message)
    runner = Runner()
    await runner._post_turn_goal_continuation(session_entry=SimpleNamespace(session_id=agent.session_id),
        source=SimpleNamespace(), final_response=result["final_response"], agent_result=result)
    assert runner.notices == ([] if configured is False else [expected_notice])

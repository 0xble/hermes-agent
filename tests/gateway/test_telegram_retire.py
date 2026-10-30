"""Retirement never uses callback timing as deletion authority."""
import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource
from gateway import telegram_retire as retire


@pytest.fixture
def setup_retire(monkeypatch):
    monkeypatch.setattr("hermes_platform.resolver.locate_command", lambda _: SimpleNamespace(command=()))
    meta = {}
    db = SimpleNamespace(
        get_meta=AsyncMock(side_effect=lambda key: meta.get(key)),
        set_meta=AsyncMock(side_effect=lambda key, value: meta.__setitem__(key, value)),
        get_telegram_topic_owner_for_thread=AsyncMock(return_value={"owner_key": "monitor"}),
        set_telegram_topic_owner_state=AsyncMock(), delete_telegram_topic_binding=AsyncMock(),
        unbind_gateway_session_peer=AsyncMock(return_value=True),
        list_meta_prefix=AsyncMock(side_effect=lambda prefix: list(meta.items())),
    )
    bot = SimpleNamespace(delete_forum_topic=AsyncMock(return_value=True),
                          send_message=AsyncMock(side_effect=RuntimeError("Bad Request: message thread not found")))
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(True, message_id="10")), _bot=bot,
                              _session_tasks={}, cancel_session_processing=AsyncMock())
    runner = SimpleNamespace(_session_db=db, session_store=SimpleNamespace(
        peek_session_id=MagicMock(return_value="session-1"), unbind_session=MagicMock(return_value=True)),
        _session_key_for_source=lambda source: "topic-key", _is_session_running=MagicMock(return_value=False),
        _delivery_adapter_for=lambda source: adapter, _running_agents={})
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="100", thread_id="42", chat_type="dm", user_id="100")
    monkeypatch.setattr("gateway.telegram_retire_work.prepare", AsyncMock(return_value=True))
    return runner, source, adapter, meta


@pytest.fixture
def runtime(setup_retire):
    runner, source, adapter, _ = setup_retire
    runner.source, runner.adapter, runner.bot = source, adapter, adapter._bot
    runner._async_profile_scope_for_source = lambda _: nullcontext()
    return runner


@pytest.mark.asyncio
async def test_delivery_persist_delete_probe_unbind_order(setup_retire):
    runner, source, adapter, meta = setup_retire
    order = []
    async def close(*args, **kwargs):
        order.append("close")
        assert kwargs["metadata"] == {"thread_id": "42"}
        assert not adapter._bot.delete_forum_topic.called
        return SendResult(True, message_id="10")
    async def delete(**kwargs):
        state = json.loads(meta[retire.state_key(source)])
        assert state["closing_receipt"]["message_id"] == "10"
        assert state["closing_receipt"]["thread_id"] == "42"
        order.append("delete")
        return True
    adapter.send.side_effect = close
    adapter._bot.delete_forum_topic.side_effect = delete
    await retire.request(runner, source)
    await retire.advance(runner, source)
    assert order == ["close", "delete"]
    assert (await retire.load(runner, source))["phase"] == "done"
    runner._session_db.delete_telegram_topic_binding.assert_awaited_once_with(chat_id="100", thread_id="42", profile_name="default")
    runner.session_store.unbind_session.assert_called_once_with("topic-key", "session-1")
    runner._session_db.set_telegram_topic_owner_state.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [SendResult(False, error="failed"), SendResult(True),
    SendResult(True, message_id="10", deferred=True), SendResult(True, message_id="10", held=True),
    SendResult(False, message_id="10", continuation_message_ids=("9",)),
    SendResult(True, message_id="10", continuation_message_ids=("9",)),
    SendResult(True, message_id="10", raw_response={"partial_overflow": True}),
    SendResult(True, message_id="10", raw_response={"thread_fallback": True}),
    SendResult(True, message_id="10", raw_response={"requested_thread_id": 99}),
    SendResult(True, message_id="10", raw_response={"message_ids": ["9", "10"]})])
async def test_nonconfirmed_send_never_deletes(setup_retire, result):
    runner, source, adapter, _ = setup_retire
    adapter.send.return_value = result
    await retire.request(runner, source)
    await retire.advance(runner, source)
    adapter._bot.delete_forum_topic.assert_not_awaited()
    runner._session_db.delete_telegram_topic_binding.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["retry", "cancel"])
async def test_failed_retirement_releases_admission_and_explains_recovery(runtime, monkeypatch, control):
    monkeypatch.setattr("gateway.telegram_retire_work.prepare", AsyncMock(side_effect=RuntimeError("handoff failed")))
    await retire.request(runtime, runtime.source)
    await retire.advance(runtime, runtime.source)
    failed = await retire.load(runtime, runtime.source)
    assert failed is not None and failed["phase"] == "failed"
    # Failure stops automatic deletion, but cannot consume later human messages.
    assert await retire.intercept(runtime, MessageEvent(text="keep working here", source=runtime.source)) is None
    notice = runtime.adapter.send.await_args.args[1]
    assert "retry it automatically" in notice and "Ask here to cancel it." in notice and "/retire" not in notice
    count = runtime.adapter.send.await_count
    await retire.advance(runtime, runtime.source)
    assert runtime.adapter.send.await_count == count
    runtime.bot.delete_forum_topic.assert_not_awaited()
    await retire.request(runtime, runtime.source, cancel=control == "cancel")
    state = await retire.load(runtime, runtime.source)
    assert state is not None and state["generation"] != failed["generation"]
    assert state["phase"] == ("pending" if control == "retry" else "cancelled")


@pytest.mark.asyncio
async def test_cancel_during_closing_invalidates_delete(setup_retire):
    runner, source, adapter, _ = setup_retire
    async def close(*args, **kwargs):
        await retire.request(runner, source, cancel=True)
        return SendResult(True, message_id="10")
    adapter.send.side_effect = close
    await retire.request(runner, source)
    await retire.advance(runner, source)
    adapter._bot.delete_forum_topic.assert_not_awaited()
    assert (await retire.load(runner, source))["phase"] == "cancelled"


@pytest.mark.asyncio
async def test_active_turn_waits_and_reinventories_after_turn(setup_retire, monkeypatch):
    runner, source, adapter, _ = setup_retire
    prepare = AsyncMock(return_value=True)
    monkeypatch.setattr("gateway.telegram_retire_work.prepare", prepare)
    runner._is_session_running.return_value = True
    await retire.request(runner, source)
    await retire.advance(runner, source)
    prepare.assert_not_awaited()
    runner._is_session_running.return_value = False
    await retire.advance(runner, source)
    prepare.assert_awaited_once()
    adapter._bot.delete_forum_topic.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_close_keeps_topic_and_durable_unresolved_attempt(setup_retire):
    runner, source, adapter, _ = setup_retire
    adapter.send.side_effect = asyncio.CancelledError
    await retire.request(runner, source)
    with pytest.raises(asyncio.CancelledError):
        await retire.advance(runner, source)
    adapter._bot.delete_forum_topic.assert_not_awaited()
    state = await retire.load(runner, source)
    assert state["closing_attempt"] and not state.get("closing_receipt")
    adapter.send.side_effect = None
    await retire.advance(runner, source)
    adapter._bot.delete_forum_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_failure_keeps_binding_and_posts_failure(setup_retire, monkeypatch):
    runner, source, adapter, _ = setup_retire
    adapter._bot.delete_forum_topic.side_effect = RuntimeError("not enough rights")
    adapter._bot.send_message.side_effect = None
    adapter._bot.get_me = AsyncMock(return_value=SimpleNamespace(username="HermesBot", id=900))
    monkeypatch.setattr("gateway.telegram_retire_work.cli", AsyncMock(side_effect=RuntimeError("not enough rights")))
    await retire.request(runner, source)
    await retire.advance(runner, source)
    runner._session_db.delete_telegram_topic_binding.assert_not_awaited()
    assert "not enough rights" in adapter.send.call_args.args[1]


@pytest.mark.asyncio
async def test_restart_reconciles_delete_without_repeating_it(setup_retire):
    runner, source, adapter, _ = setup_retire
    await retire.request(runner, source)
    state = await retire.load(runner, source)
    state.update(phase="deleting", closing_receipt={"message_id": "10", "generation": state["generation"], "chat_id": "100", "thread_id": "42"})
    await retire.save(runner, source, state)
    await retire.advance(runner, source)
    adapter._bot.delete_forum_topic.assert_not_awaited()
    assert (await retire.load(runner, source))["phase"] == "done"


@pytest.mark.asyncio
async def test_now_interrupts_first_preserves_pending_obligations(setup_retire):
    runner, source, adapter, _ = setup_retire
    agent = runner._running_agents["topic-key"] = MagicMock()
    await retire.request(runner, source, now=True)
    agent.interrupt.assert_called_once()
    adapter.cancel_session_processing.assert_awaited_once_with("topic-key", discard_pending=False)
    adapter._bot.delete_forum_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_question_replies_do_not_bypass_retirement_and_preparation_is_fenced(setup_retire):
    runner, source, _adapter, _ = setup_retire
    await retire.request(runner, source)
    state = await retire.load(runner, source)
    state["questions"] = [{"id": "goal", "suggestion": "Work"}]
    await retire.save(runner, source, state)
    event = SimpleNamespace(source=source, allow_gateway_control=True, internal=False,
                            get_command=lambda: None, text="1 Work")
    assert await retire.intercept(runner, event) == retire.NOTICE
    event.text = "start a new project"
    assert await retire.intercept(runner, event) == retire.NOTICE
    # Core owns no /retire command, so a plugin's /retire is new work like any other.
    event.get_command = lambda: "retire"
    assert await retire.intercept(runner, event) == retire.NOTICE
    wake = MessageEvent(text="prepare", source=source, internal=True,
                        metadata={"retirement_generation": state["generation"]})
    assert await retire.intercept(runner, wake) is None
    await retire.request(runner, source, cancel=True)
    assert await retire.intercept(runner, wake) == ""


@pytest.mark.asyncio
async def test_complete_split_receipt_authorizes_delete(runtime):
    runtime.adapter.send.return_value = SendResult(
        success=True, message_id="50", raw_response={"message_ids": ["50", "51"], "requested_thread_id": "42"})
    await retire.request(runtime, runtime.source)
    await retire.advance(runtime, runtime.source)
    runtime.bot.delete_forum_topic.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_preparation_never_sends_closing_or_deletes(runtime, monkeypatch):
    monkeypatch.setattr("gateway.telegram_retire_work.prepare", AsyncMock(return_value=False))
    await retire.request(runtime, runtime.source)
    await retire.advance(runtime, runtime.source)
    runtime.adapter.send.assert_not_awaited()
    runtime.bot.delete_forum_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_generation_prevents_stale_save(runtime):
    await retire.request(runtime, runtime.source)
    prior = await retire.load(runtime, runtime.source)
    await retire.request(runtime, runtime.source, cancel=True)
    with pytest.raises(retire.RetirementSuperseded):
        await retire.save(runtime, runtime.source, prior)
    assert (await retire.load(runtime, runtime.source))["phase"] == "cancelled"


@pytest.mark.asyncio
async def test_internal_completion_can_settle_and_slash_retire_is_fenced(runtime):
    await retire.request(runtime, runtime.source)
    completion = MessageEvent(text="owned completion", source=runtime.source, internal=True)
    assert await retire.intercept(runtime, completion) is None
    command = MessageEvent(text="/retire@HermesBot cancel", source=runtime.source)
    assert await retire.intercept(runtime, command) == retire.NOTICE


@pytest.mark.asyncio
async def test_ambiguous_delete_probes_before_any_fallback(runtime, monkeypatch):
    runtime.bot.delete_forum_topic.side_effect = TimeoutError("response lost")
    probe = AsyncMock(side_effect=[True, True])
    monkeypatch.setattr(retire, "_probe_deleted", probe)
    cli = AsyncMock()
    monkeypatch.setattr("gateway.telegram_retire_work.cli", cli)
    await retire.request(runtime, runtime.source)
    await retire.advance(runtime, runtime.source)
    cli.assert_not_awaited()
    runtime.bot.delete_forum_topic.assert_awaited_once()
    runtime.session_store.unbind_session.assert_called_once_with("topic-key", "session-1")


@pytest.mark.asyncio
async def test_exact_mtproto_probe_uses_bot_dialog_not_user_chat(runtime, monkeypatch):
    from gateway import telegram_retire_work
    monkeypatch.setattr("hermes_platform.resolver.locate_command", lambda _: SimpleNamespace(command=("telegram",)))
    runtime.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="HermesBot", id=900))
    command = AsyncMock(return_value=[])
    monkeypatch.setattr(telegram_retire_work, "cli", command)
    assert await retire._probe_deleted(runtime.bot, runtime.source, runner=runtime)
    assert command.await_args.args[-3:] == ("@HermesBot", "--id", "42")
    runtime.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_unresolved_delete_can_recover_without_housekeeping_spam(runtime, monkeypatch, cancel):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def scope(_source):
        yield

    runtime._async_profile_scope_for_source = scope
    runtime.bot.delete_forum_topic.side_effect = RuntimeError("not enough rights")
    runtime.bot.send_message.side_effect = None  # exact topic still exists
    runtime.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="HermesBot"))
    monkeypatch.setattr("gateway.telegram_retire_work.cli", AsyncMock(side_effect=RuntimeError("CLI unavailable")))
    await retire.request(runtime, runtime.source)
    await retire.advance(runtime, runtime.source)
    failed = await retire.load(runtime, runtime.source)
    assert failed["phase"] == "delete_unresolved"
    assert "Ask here to retry or cancel it." in runtime.adapter.send.await_args.args[1]
    # Only an explicit request recovers it, so the user can reach the agent's retire tool again.
    assert await retire.intercept(runtime, MessageEvent(text="cancel the retirement", source=runtime.source)) is None
    counts = (runtime.bot.send_message.await_count, runtime.adapter.send.await_count)
    await retire.resume_all(runtime)
    await retire.advance(runtime, runtime.source)
    assert (runtime.bot.send_message.await_count, runtime.adapter.send.await_count) == counts

    # A new command first reconciles the previous dispatch, never blindly repeats it.
    await retire.request(runtime, runtime.source, cancel=cancel)
    recovered = await retire.load(runtime, runtime.source)
    assert recovered["generation"] != failed["generation"]
    if cancel:
        assert recovered["phase"] == "cancelled"
        assert await retire.intercept(runtime, MessageEvent(text="new work", source=runtime.source)) is None
        await retire.advance(runtime, runtime.source)
        assert runtime.bot.delete_forum_topic.await_count == 1
    else:
        assert recovered["phase"] == "pending" and not recovered.get("closing_receipt")
        runtime.bot.delete_forum_topic.side_effect = None
        runtime.bot.send_message.side_effect = RuntimeError("message thread not found")
        await retire.advance(runtime, runtime.source)
        assert (await retire.load(runtime, runtime.source))["phase"] == "done"
        assert runtime.bot.delete_forum_topic.await_count == 2
        from gateway.telegram_retire_prepare import closing_note
        assert [c.args[1] for c in runtime.adapter.send.await_args_list].count(closing_note(recovered)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_unresolved_delete_already_gone_only_unbinds(runtime, cancel):
    await retire.request(runtime, runtime.source)
    state = await retire.load(runtime, runtime.source)
    state.update(phase="delete_unresolved", closing_receipt={"generation": state["generation"],
                 "message_id": "10", "chat_id": "100", "thread_id": "42"})
    await retire.save(runtime, runtime.source, state)
    result = await retire.request(runtime, runtime.source, cancel=cancel)
    assert result["status"] == "done"
    runtime.bot.delete_forum_topic.assert_not_awaited()
    runtime.adapter.send.assert_not_awaited()
    assert (await retire.load(runtime, runtime.source))["phase"] == "done"


@pytest.mark.asyncio
async def test_unresolved_delete_unknown_probe_never_retries_or_cancels(runtime):
    await retire.request(runtime, runtime.source)
    state = await retire.load(runtime, runtime.source)
    state["phase"] = "delete_unresolved"
    await retire.save(runtime, runtime.source, state)
    runtime.bot.send_message.side_effect = TimeoutError("unknown topic state")
    for cancel in (False, True):
        with pytest.raises(Exception, match="unknown topic state"):
            await retire.request(runtime, runtime.source, cancel=cancel)
    assert await retire.load(runtime, runtime.source) == state
    runtime.bot.delete_forum_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_housekeeping_reconciles_confirmed_deleted_topic_without_sending(runtime, monkeypatch):
    monkeypatch.setattr("hermes_platform.resolver.locate_command", lambda _: SimpleNamespace(command=("telegram",)))
    runtime.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="HermesBot", id=900))
    lookup = AsyncMock(side_effect=[[{"id": 42}], []])
    monkeypatch.setattr("gateway.telegram_retire_work.cli", lookup)
    await retire.request(runtime, runtime.source)
    await retire.advance(runtime, runtime.source)
    unresolved = await retire.load(runtime, runtime.source)
    assert unresolved is not None
    assert unresolved["phase"] == "delete_unresolved"
    sends = runtime.adapter.send.await_count
    deletes = runtime.bot.delete_forum_topic.await_count
    runtime.session_store.unbind_session.assert_not_called()

    await retire.resume_all(runtime)

    settled = await retire.load(runtime, runtime.source)
    assert settled is not None
    assert settled["phase"] == "done"
    assert settled["generation"] == unresolved["generation"]
    assert settled["closing_receipt"] == unresolved["closing_receipt"]
    assert runtime.adapter.send.await_count == sends
    assert runtime.bot.delete_forum_topic.await_count == deletes == 1
    runtime.bot.send_message.assert_not_awaited()
    runtime.session_store.unbind_session.assert_called_once_with("topic-key", "session-1")
    runtime._session_db.delete_telegram_topic_binding.assert_awaited_once_with(
        chat_id="100", thread_id="42", profile_name="default")
    assert lookup.await_count == 2
    assert lookup.await_args is not None
    assert lookup.await_args.args[-3:] == ("@HermesBot", "--id", "42")


async def _saved_unresolved_retirement(runtime):
    await retire.request(runtime, runtime.source)
    state = await retire.load(runtime, runtime.source)
    assert state is not None
    state.update(phase="delete_unresolved", closing_receipt={
        "generation": state["generation"], "message_id": "10", "chat_id": "100", "thread_id": "42"})
    await retire.save(runtime, runtime.source, state)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["present", "incomplete", "unknown", "no_cli", "disconnected"])
async def test_passive_reconciliation_keeps_unproven_state_without_sends(runtime, monkeypatch, outcome):
    state = await _saved_unresolved_retirement(runtime)
    command = () if outcome == "no_cli" else ("telegram",)
    monkeypatch.setattr("hermes_platform.resolver.locate_command", lambda _: SimpleNamespace(command=command))
    runtime.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="HermesBot", id=900))
    lookup = AsyncMock(return_value=[{"id": 42}])
    if outcome == "incomplete":
        lookup.return_value = {"topics": []}
    elif outcome == "unknown":
        lookup.side_effect = TimeoutError("lookup unresolved")
    elif outcome == "disconnected":
        runtime.adapter._bot = None
    monkeypatch.setattr("gateway.telegram_retire_work.cli", lookup)

    await retire.resume_all(runtime)
    await retire.resume_all(runtime)

    assert await retire.load(runtime, runtime.source) == state
    runtime.adapter.send.assert_not_awaited()
    runtime.bot.send_message.assert_not_awaited()
    runtime.bot.delete_forum_topic.assert_not_awaited()
    runtime._session_db.delete_telegram_topic_binding.assert_not_awaited()
    runtime.session_store.unbind_session.assert_not_called()
    if outcome in {"no_cli", "disconnected"}:
        lookup.assert_not_awaited()
    else:
        assert lookup.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("generation", "other-generation"), ("message_id", None),
    ("chat_id", "101"), ("thread_id", "43"), (None, None),
])
async def test_passive_reconciliation_requires_exact_closing_receipt(runtime, monkeypatch, field, value):
    state = await _saved_unresolved_retirement(runtime)
    if field is None:
        state.pop("closing_receipt")
    else:
        state["closing_receipt"][field] = value
    await retire.save(runtime, runtime.source, state)
    monkeypatch.setattr("hermes_platform.resolver.locate_command", lambda _: SimpleNamespace(command=("telegram",)))
    lookup = AsyncMock(return_value=[])
    monkeypatch.setattr("gateway.telegram_retire_work.cli", lookup)

    await retire.resume_all(runtime)

    assert await retire.load(runtime, runtime.source) == state
    lookup.assert_not_awaited()
    runtime.adapter.send.assert_not_awaited()
    runtime.bot.send_message.assert_not_awaited()
    runtime.bot.delete_forum_topic.assert_not_awaited()
    runtime._session_db.delete_telegram_topic_binding.assert_not_awaited()
    runtime.session_store.unbind_session.assert_not_called()


@pytest.mark.asyncio
async def test_passive_reconciliation_does_not_unbind_superseded_generation(runtime, monkeypatch):
    state = await _saved_unresolved_retirement(runtime)
    replacement = {**state, "phase": "cancelled", "generation": "new-generation"}
    monkeypatch.setattr("hermes_platform.resolver.locate_command", lambda _: SimpleNamespace(command=("telegram",)))
    runtime.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="HermesBot", id=900))

    async def supersede(*args):
        await retire.save(runtime, runtime.source, replacement, force=True)
        return []

    monkeypatch.setattr("gateway.telegram_retire_work.cli", AsyncMock(side_effect=supersede))

    await retire.resume_all(runtime)

    assert await retire.load(runtime, runtime.source) == replacement
    runtime.adapter.send.assert_not_awaited()
    runtime.bot.send_message.assert_not_awaited()
    runtime.bot.delete_forum_topic.assert_not_awaited()
    runtime._session_db.delete_telegram_topic_binding.assert_not_awaited()
    runtime.session_store.unbind_session.assert_not_called()


_FLOOD = ('relay failed: {"error":{"code":"telegram","message":"telegram: exit status 6: {\\"error\\":{\\"code\\":'
          '\\"rate_limited\\",\\"message\\":\\"rpc error code 420: FLOOD_WAIT (13)\\",\\"exit_code\\":6,'
          '\\"retryable\\":true,\\"retry_after_seconds\\":13}}\\n","exit_code":1}}')


def test_transient_delay_reads_flood_wait_and_ignores_permanent_errors():
    assert retire._transient_delay(RuntimeError(_FLOOD)) == 18
    assert retire._transient_delay(asyncio.TimeoutError()) == 30
    assert retire._transient_delay(RuntimeError("Missing exact-topic confirmed closing receipt")) is None


@pytest.mark.asyncio
async def test_flood_wait_during_preparation_retries_instead_of_failing(runtime, monkeypatch):
    prepare = AsyncMock(side_effect=[RuntimeError(_FLOOD), True])
    monkeypatch.setattr("gateway.telegram_retire_work.prepare", prepare)
    retries = []
    monkeypatch.setattr(retire, "_schedule_retry", lambda runner, source, delay: retries.append(delay))
    await retire.request(runtime, runtime.source)
    await retire.advance(runtime, runtime.source)
    state = await retire.load(runtime, runtime.source)
    assert state["phase"] == "pending" and state["transient_retries"] == 1
    assert retries == [18]
    runtime.adapter.send.assert_not_awaited()  # no failure notice, no closing note yet
    await retire.advance(runtime, runtime.source)
    assert (await retire.load(runtime, runtime.source))["phase"] == "done"
    runtime.bot.delete_forum_topic.assert_awaited_once()


@pytest.mark.asyncio
async def test_transient_errors_stop_retrying_after_the_limit(runtime, monkeypatch):
    monkeypatch.setattr("gateway.telegram_retire_work.prepare", AsyncMock(side_effect=RuntimeError(_FLOOD)))
    monkeypatch.setattr(retire, "_schedule_retry", lambda runner, source, delay: None)
    await retire.request(runtime, runtime.source)
    for _ in range(retire._TRANSIENT_RETRY_LIMIT + 1):
        await retire.advance(runtime, runtime.source)
    state = await retire.load(runtime, runtime.source)
    assert state["phase"] == "failed" and state["transient_retries"] == retire._TRANSIENT_RETRY_LIMIT
    assert "Topic retirement failed" in runtime.adapter.send.await_args.args[1]


@pytest.mark.asyncio
async def test_failed_retirement_retries_itself_with_saved_decisions(runtime, monkeypatch):
    """A since-cleared fault must not strand a requested retirement until someone asks again."""
    clock = [1000.0]
    monkeypatch.setattr(retire.time, "time", lambda: clock[0])
    prepare = AsyncMock(side_effect=[RuntimeError("handoff failed"), RuntimeError("handoff failed"), True])
    monkeypatch.setattr("gateway.telegram_retire_work.prepare", prepare)
    await retire.request(runtime, runtime.source, target="Owner Topic", reason="merged", context="summary")
    await retire.advance(runtime, runtime.source)
    first = await retire.load(runtime, runtime.source)
    assert first["phase"] == "failed" and runtime.adapter.send.await_count == 1
    await retire.advance(runtime, runtime.source)  # inside the backoff: nothing happens
    assert (await retire.load(runtime, runtime.source))["generation"] == first["generation"]
    clock[0] += retire._AUTO_RETRY_BASE_SECONDS
    await retire.advance(runtime, runtime.source)  # queues the retry with the saved decisions
    retried = await retire.load(runtime, runtime.source)
    assert retried["phase"] == "pending" and retried["generation"] != first["generation"]
    assert (retried["target"], retried["reason"], retried["context"]) == ("Owner Topic", "merged", "summary")
    assert retried["auto_retries"] == 1
    await retire.advance(runtime, runtime.source)  # fails again, quietly
    assert (await retire.load(runtime, runtime.source))["phase"] == "failed"
    assert runtime.adapter.send.await_count == 1
    clock[0] += 2 * retire._AUTO_RETRY_BASE_SECONDS
    await retire.advance(runtime, runtime.source)
    await retire.advance(runtime, runtime.source)
    assert (await retire.load(runtime, runtime.source))["phase"] == "done"
    runtime.bot.delete_forum_topic.assert_awaited_once()


@pytest.mark.asyncio
async def test_automatic_retries_stop_at_the_limit_and_say_so(runtime, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(retire.time, "time", lambda: clock[0])
    monkeypatch.setattr("gateway.telegram_retire_work.prepare", AsyncMock(side_effect=RuntimeError("still broken")))
    await retire.request(runtime, runtime.source)
    await retire.advance(runtime, runtime.source)
    for _ in range(retire._AUTO_RETRY_LIMIT + 2):
        clock[0] += retire._AUTO_RETRY_MAX_SECONDS
        await retire.advance(runtime, runtime.source)  # queue the retry
        await retire.advance(runtime, runtime.source)  # run it
    state = await retire.load(runtime, runtime.source)
    assert state["phase"] == "failed" and state["auto_retries"] == retire._AUTO_RETRY_LIMIT
    notices = [call.args[1] for call in runtime.adapter.send.await_args_list]
    assert len(notices) == 2 and "5 automatic retries also failed" in notices[-1]
    runtime.bot.delete_forum_topic.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_retirement_is_never_retried(runtime, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(retire.time, "time", lambda: clock[0])
    monkeypatch.setattr("gateway.telegram_retire_work.prepare", AsyncMock(side_effect=RuntimeError("handoff failed")))
    await retire.request(runtime, runtime.source)
    await retire.advance(runtime, runtime.source)
    await retire.request(runtime, runtime.source, cancel=True)
    clock[0] += retire._AUTO_RETRY_MAX_SECONDS
    await retire.advance(runtime, runtime.source)
    assert (await retire.load(runtime, runtime.source))["phase"] == "cancelled"

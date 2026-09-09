"""Real gateway projection/transport boundaries, without live Telegram traffic."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.delegation_cards import DelegationCards, render_card
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionSource
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext


def test_render_card_is_one_native_markdown_blockquote_with_task_first_rows():
    card = {
        "started_at": 0,
        "rows": {"A": {"thread_ref": "A", "task_label": "Repair restart receipt", "role": "Orchestrator",
                         "state": "running", "last_tool": "read_file"}},
    }

    rendered = render_card(card, now=0)

    lines = rendered.splitlines()
    assert lines[0] == "> 🧵 **Delegating · 0 min**"
    assert lines[1] == "> **A. Repair restart receipt** · Orchestrator"
    assert lines[2].startswith("> ↳ Last tool: ")
    assert all(line.startswith("> ") for line in lines)


@pytest.mark.asyncio
async def test_telegram_card_send_and_edit_keep_blockquote_and_bold_entities():
    """Cards use the normal MarkdownV2 formatter on both transport operations."""
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={"rich_messages": False}))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    adapter._bot.edit_message_text = AsyncMock(return_value=SimpleNamespace(message_id=7))
    adapter._bot.send_chat_action = AsyncMock()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    content = "> 🧵 **Delegating · 0 min**\n> **A. Repair receipt** · Worker\n> ↳ Started · awaiting activity"

    sent = await adapter.send_delegation_card(source, content)
    edited = await adapter.edit_message("42", "7", content, finalize=True)

    assert sent.success and edited.success
    send_kwargs = adapter._bot.send_message.call_args.kwargs
    edit_kwargs = adapter._bot.edit_message_text.call_args.kwargs
    assert send_kwargs["parse_mode"] == edit_kwargs["parse_mode"]
    assert send_kwargs["text"].startswith("> 🧵 *Delegating · 0 min*")
    assert "> *A\. Repair receipt* · Worker" in send_kwargs["text"]
    assert edit_kwargs["text"] == send_kwargs["text"]


@pytest.mark.asyncio
async def test_card_outlives_turn_and_requires_parent_delivery(tmp_path):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    cards = runner._delegation_cards = DelegationCards(runner, home=tmp_path, interval=0)
    owner = dict(profile=str(tmp_path), session_id="session", session_key="route", chat_id="42", thread_id="8")
    data = dict(parent_task_id="a" * 32, thread_ref="A", task_label="Fix restart warning", role="Worker", owner=owner, background=True)
    ctx = TurnContext(source=source, session_id="session", session_key="route", run_generation=1,
        tool_progress_enabled=True, progress_mode="all", _run_still_current=lambda: False)
    relay = TurnRunner(runner, ctx)
    tasks = []
    relay._schedule = lambda coro, *_: tasks.append(asyncio.create_task(coro))
    relay.progress_callback("subagent.start", **data)
    await asyncio.gather(*tasks)
    await asyncio.gather(*list(cards.pending.values()))
    assert adapter.send_delegation_card.await_count == 1
    assert adapter.send_delegation_card.call_args.args[1].startswith("> 🧵 **Delegating · ")
    interim = MessageEvent(text="Working", source=source)
    assert cards.receipt(interim, "route", 1) == {}
    relay.progress_callback("subagent.tool", "terminal", preview="SECRET", args={"secret": "raw"}, **data)
    await asyncio.gather(*tasks)
    await asyncio.gather(*list(cards.pending.values()))
    assert "Last tool:" in adapter.edit_message.call_args.args[2]
    assert all(line.startswith("> ") for line in adapter.edit_message.call_args.args[2].splitlines())
    assert adapter.edit_message.call_args.kwargs == {"finalize": True}
    assert "SECRET" not in adapter.edit_message.call_args.args[2]
    relay.progress_callback("subagent.complete", status="completed", **data)
    await asyncio.gather(*tasks)
    await asyncio.gather(*list(cards.pending.values()))
    assert "Returned · awaiting parent" in render_card(cards.cards["a" * 32])
    adapter.delete_message.assert_not_awaited()
    final_event = MessageEvent(text="Returned", source=source, internal=True, metadata={
        "delegation_parent_task_id": "a" * 32, "delegation_owner": owner, "delegation_thread_refs": ["A"]})
    final_event._delegation_card_receipt = cards.receipt(final_event, "route", 2)
    transport = SimpleNamespace(name="test")
    transport._send_final_text = BasePlatformAdapter._send_final_text.__get__(transport)
    # Upstream moved the ledger bracket out of _send_final_text into send_final_ledgered; bind the
    # real method too so this still exercises the production path rather than stubbing it out.
    transport.send_final_ledgered = BasePlatformAdapter.send_final_ledgered.__get__(transport)
    transport._finalize_delivery_obligation = AsyncMock(return_value=None)
    transport.gateway_runner = runner
    transport._final_delivery_adapter = lambda _: transport
    transport.name = "test"
    transport._record_delivery_obligation = AsyncMock(return_value=None)
    transport._send_with_retry = AsyncMock(return_value=SendResult(success=False))
    await transport._send_final_text(final_event, "route", "Done", {}, False, 0, lambda _: None)
    adapter.delete_message.assert_not_awaited()
    transport._send_with_retry.return_value = SendResult(success=True, message_id="final")
    await transport._send_final_text(final_event, "route", "Done", {}, False, 0, lambda _: None)
    adapter.delete_message.assert_awaited_once_with("42", "1")
    await cards.observe(source, "route", "session", 1, "subagent.start", None, data)
    assert not cards.pending


@pytest.mark.asyncio
async def test_persisted_telegram_card_resolves_live_adapter_and_cleans_up(tmp_path):
    """Persisted JSON must return to the enum-keyed gateway adapter resolver."""
    class Runner(GatewayAuthorizationMixin):
        adapters: dict
        _primary_profile_name: str

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    adapter = SimpleNamespace(
        send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="card")),
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True),
    )
    runner = Runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._primary_profile_name = "default"
    owner = dict(profile="default", session_id="session", session_key="route", chat_id="42", thread_id="8")
    data = dict(parent_task_id="e" * 32, thread_ref="A", owner=owner, background=True)

    cards = DelegationCards(runner, home=tmp_path, interval=0)
    await cards.observe(source, "route", "session", 1, "subagent.start", None, data)
    await asyncio.gather(*list(cards.pending.values()))

    restored = DelegationCards(runner, home=tmp_path, interval=0)
    assert restored.cards["e" * 32]["source"]["platform"] == "telegram"
    await restored.reconcile()
    await asyncio.gather(*list(restored.pending.values()))
    assert restored._source(restored.cards["e" * 32]).platform is Platform.TELEGRAM
    assert runner._adapter_for_source(restored._source(restored.cards["e" * 32])) is adapter
    adapter.send_delegation_card.assert_awaited_once()
    assert adapter.send_delegation_card.call_args.args[0].platform is Platform.TELEGRAM

    event = MessageEvent(text="Returned", source=source, internal=True, metadata={
        "delegation_parent_task_id": "e" * 32, "delegation_owner": owner, "delegation_thread_refs": ["A"]})
    await restored.delivered(restored.receipt(event, "route", 2))
    adapter.delete_message.assert_awaited_once_with("42", "card")


def test_invalid_persisted_card_source_fails_closed_with_one_safe_diagnostic(tmp_path, caplog):
    cards = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: pytest.fail("must not resolve")), home=tmp_path)
    card = {"source": {"platform": "not-a-platform", "chat_id": "private-chat-id"}}

    assert cards._adapter(card) is None
    assert cards._adapter(card) is None

    messages = [record.getMessage() for record in caplog.records if "Delegation card" in record.getMessage()]
    assert messages == ["Delegation card source platform is invalid; skipping delivery"]
    assert "private-chat-id" not in caplog.text


@pytest.mark.asyncio
async def test_grouping_isolation_generation_and_recovery(tmp_path):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    first = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    live = [first]
    runner = SimpleNamespace(_adapter_for_source=lambda _: live[0])
    cards = DelegationCards(runner, home=tmp_path, interval=0)
    owner = dict(profile=str(tmp_path), session_id="s", session_key="r", chat_id="42", thread_id="8")
    data = dict(parent_task_id="b" * 32, thread_ref="A", owner=owner, background=True)
    await cards.observe(source, "r", "s", 1, "subagent.start", None, data)
    await asyncio.gather(*list(cards.pending.values()))
    wrong = {**data, "owner": {**owner, "thread_id": "9"}, "thread_ref": "B"}
    await cards.observe(source, "r", "s", 1, "subagent.start", None, wrong)
    assert list(cards.cards["b" * 32]["rows"]) == ["A"]
    replacement = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="2")),
        edit_message=AsyncMock(return_value=SendResult(success=False, error="Message to edit not found")),
        delete_message=AsyncMock(return_value=True))
    live[0] = replacement
    await cards.observe(source, "r", "s", 1, "subagent.tool", "read_file", data)
    await asyncio.gather(*list(cards.pending.values()))
    replacement.send_delegation_card.assert_awaited_once()
    await cards.observe(source, "r", "s", 1, "subagent.tool", "terminal", data)
    await asyncio.gather(*list(cards.pending.values()))
    assert replacement.send_delegation_card.await_count == 1
    event = MessageEvent(text="Result", source=source, internal=True, metadata={
        "delegation_parent_task_id": "b" * 32, "delegation_owner": owner, "delegation_thread_refs": ["A"]})
    proof = cards.receipt(event, "r", 2)
    await cards.observe(source, "r", "s", 2, "subagent.start", None, {**data, "thread_ref": "B"})
    await cards.delivered(proof)
    replacement.delete_message.assert_not_awaited()
    await asyncio.gather(*list(cards.pending.values()))
    restarted = DelegationCards(runner, home=tmp_path, interval=0)
    assert all(r["state"] == "unknown" for r in restarted.cards["b" * 32]["rows"].values())
    assert "gateway restarted" in render_card(restarted.cards["b" * 32])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "log"])
async def test_display_modes_do_not_schedule_cards(mode):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    ctx = TurnContext(source=source, tool_progress_enabled=False, progress_mode=mode,
                      _run_still_current=lambda: False)
    runner = SimpleNamespace()
    relay = TurnRunner(runner, ctx)
    relay.progress_callback("subagent.start", parent_task_id="a" * 32)
    assert not hasattr(runner, "_delegation_cards")


@pytest.mark.asyncio
async def test_silent_terminal_delivery_and_unchanged_suppression(tmp_path):
    from gateway.run_turn import GatewayTurnMixin
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter, _should_send_voice_reply=lambda *a, **k: False)
    cards = runner._delegation_cards = DelegationCards(runner, home=tmp_path, interval=0)
    data = dict(parent_task_id="c" * 32, thread_ref="A", background=False,
        owner=dict(profile=str(tmp_path), session_id="s", session_key="r", chat_id="42", thread_id=""))
    await cards.observe(source, "r", "s", 1, "subagent.start", None, data)
    await asyncio.gather(*list(cards.pending.values()))
    await cards.observe(source, "r", "s", 1, "subagent.tool", "terminal", data)
    await asyncio.gather(*list(cards.pending.values()))
    before = adapter.edit_message.await_count
    await cards.observe(source, "r", "s", 1, "subagent.tool", "terminal", data)
    await asyncio.gather(*list(cards.pending.values()))
    assert adapter.edit_message.await_count == before
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, {**data, "status": "failed"})
    await asyncio.gather(*list(cards.pending.values()))
    assert "Failed · awaiting parent" in render_card(cards.cards["c" * 32])
    event = MessageEvent(source=source, text="")
    await GatewayTurnMixin._hmwa_deliver_turn_response(runner, event, source,
        SimpleNamespace(session_id="s"), "r", 1, {}, [], "[SILENT]", "", True)
    adapter.delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_never_retries_ambiguous_send_and_recovers_delete(tmp_path):
    import json
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(side_effect=TimeoutError),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=False))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    cards = DelegationCards(runner, home=tmp_path, interval=0)
    data = dict(parent_task_id="d" * 32, thread_ref="A", background=False,
        owner=dict(profile=str(tmp_path), session_id="s", session_key="r", chat_id="42", thread_id=""))
    await cards.observe(source, "r", "s", 1, "subagent.start", None, data)
    await asyncio.gather(*list(cards.pending.values()))
    assert json.loads(cards.path.read_text())["d" * 32]["send_attempts"] == 1
    restored = DelegationCards(runner, home=tmp_path, interval=0)
    await restored.reconcile()
    await asyncio.gather(*list(restored.pending.values()))
    assert adapter.send_delegation_card.await_count == 1
    card = restored.cards["d" * 32]
    card.update(retired=True, message_id="visible")
    await restored.reconcile()
    assert card["message_id"] == "visible"
    adapter.delete_message.return_value = True
    await restored.reconcile()
    assert card["message_id"] is None


@pytest.mark.asyncio
async def test_restart_retires_exact_parent_receipt_before_card_replay(tmp_path):
    """A receipt persisted before a crash fences startup replay.

    This is intentionally not an age/terminal-state inference: a terminal card with
    no exact handled ref remains recoverable for its parent.
    """
    import json
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="card")),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    owner = dict(profile="default", session_id="session", session_key="route", chat_id="42", thread_id="8")
    key = "f" * 32
    cards_path = tmp_path / "cache" / "delegation" / "cards.json"
    cards_path.parent.mkdir(parents=True)
    cards_path.write_text(json.dumps({key: {
        "owner": owner, "source": {"platform": "telegram", "chat_id": "42", "thread_id": "8"},
        "started_at": 1, "generation": 0, "rows": {"A": {"thread_ref": "A", "state": "interrupted"}},
        "message_id": None, "rendered": "", "recoveries": 0, "send_attempts": 0, "retired": False,
        "handled": ["A"],
    }}))

    restored = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    await restored.reconcile()

    assert restored.cards[key]["retired"] is True
    adapter.send_delegation_card.assert_not_awaited()
    # A late completion cannot resurrect a terminal record that has been explicitly retired.
    await restored.observe(source, "route", "session", 1, "subagent.complete", None, {
        "parent_task_id": key, "thread_ref": "A", "status": "completed", "owner": owner,
    })
    assert not restored.pending


@pytest.mark.asyncio
async def test_telegram_card_send_never_falls_back_to_other_topic():
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from telegram.error import BadRequest
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    transport = SimpleNamespace(
        _thread_kwargs_for_send=lambda *a, **k: {"message_thread_id": 8},
        format_message=lambda content: content,
        _notification_kwargs=lambda _: {"disable_notification": True},
        _link_preview_kwargs=lambda: {},
        _send_chunk_markdown_or_plain=AsyncMock(side_effect=BadRequest("Message thread not found")))
    result = await TelegramAdapter.send_delegation_card(transport, source, "Test")
    assert not result.success
    assert result.raw_response["definite_rejection"]
    assert transport._send_chunk_markdown_or_plain.await_count == 1
    assert transport._send_chunk_markdown_or_plain.call_args.args[2]["message_thread_id"] == 8


def test_concurrent_initial_callbacks_share_one_gateway_projection(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    import gateway.delegation_cards as module
    runner = SimpleNamespace()
    start = threading.Barrier(4)
    created = []

    def constructor(_):
        instance = object()
        created.append(instance)
        time.sleep(0.02)  # emulate filesystem I/O releasing the GIL
        return instance

    def callback(_):
        start.wait(timeout=5)
        return module.cards_for(runner)

    monkeypatch.setattr(module, "DelegationCards", constructor)
    with ThreadPoolExecutor(4) as pool:
        managers = list(pool.map(callback, range(4)))
    assert len(created) == 1
    assert all(manager is managers[0] for manager in managers)


@pytest.mark.asyncio
async def test_sync_batch_receipt_does_not_handle_related_background_rows(tmp_path):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    cards = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    owner = dict(profile="default", session_id="session", session_key="route", chat_id="42", thread_id="8")
    for ref, background in [("A", True), ("B", False)]:
        data = dict(parent_task_id="c" * 32, thread_ref=ref, owner=owner, background=background)
        await cards.observe(source, "route", "session", 1, "subagent.start", None, data)
        await cards.observe(source, "route", "session", 1, "subagent.complete", None, data)
    await asyncio.gather(*list(cards.pending.values()))
    event = MessageEvent(text="Synchronous B handled; A completion is still queued", source=source)
    receipt = cards.receipt(event, "route", 1)
    assert receipt["c" * 32]["refs"] == ["B"]
    await cards.delivered(receipt)
    adapter.delete_message.assert_not_awaited()
    completion = MessageEvent(text="Handle A and synchronous B", source=source, internal=True, metadata={
        "delegation_parent_task_id": "c" * 32, "delegation_owner": owner, "delegation_thread_refs": ["A"]})
    combined = cards.receipt(completion, "route", 1)
    assert combined["c" * 32]["refs"] == ["A", "B"]
    await cards.delivered(combined)
    adapter.delete_message.assert_awaited_once()

"""Real gateway projection/transport boundaries, without live Telegram traffic."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.display import get_tool_emoji
from gateway.config import Platform
from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.delegation_cards import DelegationCards, render_card
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionSource
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext


def test_render_card_is_plain_rich_text_with_task_first_rows():
    card = {
        "started_at": 0,
        "rows": {"A": {"thread_ref": "A", "task_label": "Repair restart receipt", "role": "orchestrator", "subagent_type": "lead",
                         "state": "running", "last_tool": "read_file"}},
    }

    rendered = render_card(card, now=0)

    lines = rendered.splitlines()
    assert lines[0] == "🧵 **Delegating tasks**"
    assert render_card(card, now=3600).splitlines()[0] == "🧵 **Delegating tasks**"
    assert lines[1] == "A. Repair restart receipt · Lead"
    assert lines[2] == f"↳ {get_tool_emoji('read_file')} read_file"
    assert "computer_use" in render_card({**card, "rows": {"A": {**card["rows"]["A"], "last_tool": "computer_use_multi_step"}}}, now=0)
    assert not any(line.startswith(">") for line in lines)


@pytest.mark.asyncio
async def test_telegram_card_send_and_edit_keep_plain_bold_entities():
    """Cards use the normal MarkdownV2 formatter without quote entities on both operations."""
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={"rich_messages": False}))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    adapter._bot.edit_message_text = AsyncMock(return_value=SimpleNamespace(message_id=7))
    adapter._bot.send_chat_action = AsyncMock()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    content = ("🧵 **Delegating tasks**\nA. Repair receipt · Worker\n↳ Started · awaiting activity\n"
               "\u00a0\u00a0\u00a0\u00a0A.1. Verify card edit · Explorer\n\u00a0\u00a0\u00a0\u00a0↳ ⚡ terminal\n"
               "\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0A.1.1. Verify edit payload · Worker\n"
               "\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0↳ ⚡ read_file")

    sent = await adapter.send_delegation_card(source, content)
    edited = await adapter.edit_message("42", "7", content, finalize=True)

    assert sent.success and edited.success
    send_kwargs = adapter._bot.send_message.call_args.kwargs
    edit_kwargs = adapter._bot.edit_message_text.call_args.kwargs
    assert send_kwargs["parse_mode"] == edit_kwargs["parse_mode"]
    sent_lines = send_kwargs["text"].splitlines()
    assert sent_lines[0] == "🧵 *Delegating tasks*"
    assert sent_lines[1] == "A\\. Repair receipt · Worker"
    assert sent_lines[3] == "\u00a0\u00a0\u00a0\u00a0A\\.1\\. Verify card edit · Explorer"
    assert sent_lines[4] == "\u00a0\u00a0\u00a0\u00a0↳ ⚡ terminal"
    assert sent_lines[5] == "\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0A\\.1\\.1\\. Verify edit payload · Worker"
    assert sent_lines[6] == "\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0↳ ⚡ read\\_file"
    assert "*" not in sent_lines[1]
    assert "*" not in sent_lines[2]
    assert not any(line.startswith(">") for line in sent_lines)
    assert edit_kwargs["text"] == send_kwargs["text"]


@pytest.mark.asyncio
async def test_observed_canonical_tool_name_reaches_telegram_send_and_edit(tmp_path):
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={"rich_messages": False}))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    adapter._bot.edit_message_text = AsyncMock(return_value=SimpleNamespace(message_id=7))
    adapter._bot.send_chat_action = AsyncMock()
    cards = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    data = dict(parent_task_id="a" * 32, thread_ref="A", task_label="Check display", subagent_type="lead", role="orchestrator", owner=dict(
        profile="default", session_id="s", session_key="r", chat_id="42", thread_id=""))
    await cards.observe(source, "r", "s", 1, "subagent.start", None, data)
    await cards.observe(source, "r", "s", 1, "subagent.tool", "computer_use", data)
    await asyncio.gather(*list(cards.pending.values()))
    assert "computer\\_use" in adapter._bot.send_message.call_args.kwargs["text"]
    await cards.observe(source, "r", "s", 1, "subagent.tool", "computer_use_multi_step", data)
    await asyncio.gather(*list(cards.pending.values()))
    assert "computer\\_use\\_multi\\_step" in adapter._bot.edit_message_text.call_args.kwargs["text"]
    assert " · Lead" in adapter._bot.send_message.call_args.kwargs["text"]
    assert " · Lead" in adapter._bot.edit_message_text.call_args.kwargs["text"]
    restored = DelegationCards(cards.runner, home=tmp_path, interval=0)
    await restored.reconcile()
    await drain_cards(restored)
    assert " · Lead" in adapter._bot.edit_message_text.call_args.kwargs["text"]
    assert "orchestrator" not in adapter._bot.edit_message_text.call_args.kwargs["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [
    "mcp__figma__get_context_for_code_connect_suggestions_and_details",
    "mcp__" + "測試_🧪" * 300,
], ids=["namespaced", "long-unicode"])
async def test_tool_excerpt_send_edit_is_readable_bounded_and_private(tmp_path, tool):
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={"rich_messages": False}))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    adapter._bot.edit_message_text = AsyncMock(return_value=SimpleNamespace(message_id=7))
    adapter._bot.send_chat_action = AsyncMock()
    cards = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    data = dict(parent_task_id="a" * 32, thread_ref="A", task_label="Check display", owner=dict(
        profile="default", session_id="s", session_key="r", chat_id="42", thread_id=""),
        preview="PRIVATE_PREVIEW", args={"token": "PRIVATE_ARGUMENT"}, goal="PRIVATE_GOAL")
    await cards.observe(source, "r", "s", 1, "subagent.start", None, data)
    await cards.observe(source, "r", "s", 1, "subagent.tool", tool, data)
    await drain_cards(cards)
    expected = tool[:40]
    plain = render_card(cards.cards["a" * 32], now=0)
    assert plain.splitlines()[-1] == f"↳ {get_tool_emoji(expected)} {expected}"
    sent = adapter._bot.send_message.call_args.kwargs["text"]
    assert expected.replace("_", "\\_") in sent
    # A distinct update must exercise edit formatting, not unchanged suppression.
    await cards.observe(source, "r", "s", 1, "subagent.tool", "next_" + tool, data)
    await drain_cards(cards)
    edited = adapter._bot.edit_message_text.call_args.kwargs["text"]
    assert ("next_" + tool)[:40].replace("_", "\\_") in edited
    for text in (plain, sent, edited, cards.path.read_text(encoding="utf-8")):
        assert "PRIVATE_" not in text
    for text in (sent, edited):
        assert "Last tool" not in text
        assert len(text.encode("utf-16-le")) // 2 < 4096
    row = cards.cards["a" * 32]["rows"]["A"]
    many = dict(started_at=0, rows={str(i): {**row, "thread_ref": str(i)} for i in range(100)})
    # Card row guidance is not a renderer hard cap: authored rows are preserved.
    assert "99. Check display" in render_card(many, now=0)


@pytest.mark.asyncio
async def test_nested_cards_preserve_actual_parentage_and_third_layer_role_layout(tmp_path):
    """Observed nested lifecycle events share one card without inferring parentage from labels."""
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    adapter = SimpleNamespace(
        send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True),
    )
    cards = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    owner = dict(profile="default", session_id="s", session_key="***", chat_id="42", thread_id="")
    root, child, grandchild, deeper = ("a" * 32, "b" * 32, "c" * 32, "d" * 32)

    async def event(key, ref, label, *, parent=None, tool="computer_use"):
        data = dict(parent_task_id=key, thread_ref=ref, task_label=label, subagent_type="worker", owner=owner,
                    preview="PRIVATE_PREVIEW", args={"token": "PRIVATE_ARGUMENT"})
        if parent:
            data.update(card_parent_task_id=parent[0], card_parent_thread_ref=parent[1])
        await cards.observe(source, "***", "s", 1, "subagent.start", None, data)
        await cards.observe(source, "***", "s", 1, "subagent.tool", tool, data)
        return data

    root_data = await event(root, "A", "Check Telegram edits")
    child_data = await event(child, "A", "Verify card edit", parent=(root, "A"))
    grandchild_data = await event(grandchild, "A", "Check Telegram edits", parent=(child, "A"))
    deeper_data = await event(deeper, "A", "Bound deep descendant", parent=(grandchild, "A"))
    await drain_cards(cards)

    rendered = adapter.send_delegation_card.call_args.args[1]
    lines = rendered.splitlines()
    assert lines[0] == "🧵 **Delegating tasks**"
    assert "A. Check Telegram edits · Worker" in lines
    assert "\u00a0\u00a0\u00a0\u00a0A.1. Verify card edit · Worker" in lines
    assert "\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0A.1.1. Check Telegram edits · Worker" in lines
    assert "\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0↳ ⚡ computer_use" in lines
    assert "\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0A.1.1.1. Bound deep descendant · Worker · ↑A.1.1" in lines
    assert any(len(line) > 32 for line in lines[1::2])  # guidance never truncates authored labels
    assert all("Last tool:" not in line and "PRIVATE_" not in line for line in lines)
    assert not any(line.startswith(">") for line in lines)

    for data in (root_data, child_data, grandchild_data, deeper_data):
        await cards.observe(source, "***", "s", 1, "subagent.complete", None, {**data, "status": "completed"})
    receipt = {key: {"generation": cards.cards[key]["generation"], "epoch": cards.cards[key].get("receipt_epoch", 0), "refs": ["A"]}
               for key in (root, child, grandchild, deeper)}
    await cards.delivered(receipt)
    assert all(card["retired"] for card in cards.cards.values())
    await cards.observe(source, "***", "s", 1, "subagent.tool", "terminal", root_data)
    assert all(card["retired"] for card in cards.cards.values())


@pytest.mark.asyncio
async def test_nested_relays_reach_root_card_with_root_display_owner(tmp_path):
    """Gateway accepts a trusted root card owner while actual child ownership stays distinct."""
    from tools.delegate_tool import _build_child_progress_callback

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    cards = runner._delegation_cards = DelegationCards(runner, home=tmp_path, interval=0)
    ctx = TurnContext(source=source, session_id="root", session_key="route", run_generation=1,
        tool_progress_enabled=True, progress_mode="all", _run_still_current=lambda: False)
    gateway_relay = TurnRunner(runner, ctx)
    scheduled = []
    gateway_relay._schedule = lambda coro, *_: scheduled.append(asyncio.create_task(coro))
    owner = dict(profile="default", session_id="root", session_key="route", chat_id="42", thread_id="")

    root = SimpleNamespace(_delegate_spinner=None, tool_progress_callback=gateway_relay.progress_callback)
    root_ref = dict(parent_task_id="a" * 32, thread_ref="A", task_label="root", owner=owner, card_owner=owner)
    first = _build_child_progress_callback(0, "first", root, session_ref=root_ref)
    middle = SimpleNamespace(_delegate_spinner=None, tool_progress_callback=first)
    middle_ref = dict(parent_task_id="b" * 32, thread_ref="A", task_label="child", owner={**owner, "session_id": "child-1"},
                      card_owner=owner, card_parent_task_id="a" * 32, card_parent_thread_ref="A")
    second = _build_child_progress_callback(0, "second", middle, session_ref=middle_ref)
    leaf_parent = SimpleNamespace(_delegate_spinner=None, tool_progress_callback=second)
    leaf_ref = dict(parent_task_id="c" * 32, thread_ref="A", task_label="grandchild", owner={**owner, "session_id": "child-2"},
                    card_owner=owner, card_parent_task_id="b" * 32, card_parent_thread_ref="A")
    leaf = _build_child_progress_callback(0, "leaf", leaf_parent, session_ref=leaf_ref)

    first("subagent.start")
    second("subagent.start")
    leaf("subagent.start")
    leaf("tool.started", "read_file")
    await asyncio.gather(*scheduled)
    await drain_cards(cards)

    assert cards.cards["c" * 32]["delegation_owner"]["session_id"] == "child-2"
    rendered = adapter.send_delegation_card.call_args.args[1]
    assert "A. root" in rendered
    assert "\u00a0\u00a0\u00a0\u00a0A.1. child" in rendered
    assert "\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0A.1.1. grandchild" in rendered
    assert "\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0↳ ⚡ read_file" in rendered


@pytest.mark.asyncio
async def test_root_receipt_consumes_only_terminal_nested_descendants(tmp_path):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    cards = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    owner = dict(profile="default", session_id="root", session_key="route", chat_id="42", thread_id="")

    async def lifecycle(key, *, parent=None, state="completed"):
        data = dict(parent_task_id=key, thread_ref="A", owner={**owner, "session_id": key}, card_owner=owner)
        if parent:
            data.update(card_parent_task_id=parent, card_parent_thread_ref="A")
        await cards.observe(source, "route", "root", 1, "subagent.start", None, data)
        if state != "running":
            await cards.observe(source, "route", "root", 1, "subagent.complete", None, {**data, "status": state})

    await lifecycle("a" * 32)
    await lifecycle("b" * 32, parent="a" * 32)
    await lifecycle("c" * 32, parent="b" * 32)
    await lifecycle("d" * 32, parent="c" * 32, state="running")
    await lifecycle("e" * 32)  # Same root display owner but unrelated lineage.
    event = MessageEvent(source=source, text="root result", internal=True, metadata={
        "delegation_parent_task_id": "a" * 32, "delegation_owner": owner, "delegation_thread_refs": ["A"]})

    receipt = cards.receipt(event, "route", 2)
    assert set(receipt) == {"a" * 32, "b" * 32, "c" * 32}
    await cards.delivered(receipt)

    assert cards.cards["d" * 32].get("handled", []) == []
    assert cards.cards["e" * 32].get("handled", []) == []


def test_render_card_preserves_full_explicit_label_without_card_truncation():
    label = "keep-every-character-" * 250
    rendered = render_card({"started_at": 0, "rows": {"A": {
        "thread_ref": "A", "task_label": label, "state": "running", "last_tool": None,
    }}}, now=0)

    assert label in rendered
    assert len(rendered) > 3500


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
    assert adapter.send_delegation_card.call_args.args[1].startswith("🧵 **Delegating tasks**")
    interim = MessageEvent(text="Working", source=source)
    assert cards.receipt(interim, "route", 1) == {}
    relay.progress_callback("subagent.tool", "terminal", preview="SECRET", args={"secret": "raw"}, **data)
    await asyncio.gather(*tasks)
    await asyncio.gather(*list(cards.pending.values()))
    assert f"↳ {get_tool_emoji('terminal')} terminal" in adapter.edit_message.call_args.args[2]
    assert not any(line.startswith(">") for line in adapter.edit_message.call_args.args[2].splitlines())
    assert adapter.edit_message.call_args.kwargs == {"finalize": True, "metadata": {"hermes_status": True}}
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
async def test_audited_dismissal_does_not_bind_new_row_to_deleted_historical_sibling_anchor(tmp_path):
    """A retired ambiguous sibling fences its own send; it cannot own a new presentation."""
    adapter = SimpleNamespace(
        send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="fresh")),
        edit_message=AsyncMock(return_value=SendResult(success=True)),
        delete_message=AsyncMock(return_value=True),
    )
    cards = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    owner = dict(profile="default", session_id="s", session_key="r", chat_id="42", thread_id="8")
    dismissed, historical, active, other_topic = ("x" * 32, "a" * 32, "t" * 32, "o" * 32)

    def card(*, rows, retired, thread_id="8", **extra):
        return dict(owner={**owner, "thread_id": thread_id}, source={"platform": "telegram", "chat_id": "42", "thread_id": thread_id},
                    started_at=1, generation=0, rows=rows, message_id=None, rendered="", recoveries=0,
                    retired=retired) | {"send_attempts": 0} | extra

    cards.cards = {
        dismissed: card(retired=True, message_deleted=True, send_attempts=1,
                        dismissal_request_sha256="audited", rows={"X": {"thread_ref": "X", "state": "completed"}}),
        historical: card(retired=True, presentation_key=dismissed, send_attempts=1,
                         rows={"AA": {"thread_ref": "AA", "state": "completed"}}),
        active: card(retired=False, presentation_key=dismissed,
                     rows={"AT": {"thread_ref": "AT", "state": "running"}}),
        other_topic: card(retired=False, thread_id="9", message_id="other", presentation_key=other_topic,
                          rows={"B": {"thread_ref": "B", "state": "running"}}),
    }

    cards._save()
    restarted = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    restarted._bind(active)
    await restarted._flush(restarted._anchor(active))

    assert restarted._anchor(active) == active
    assert restarted.cards[active]["message_id"] == "fresh"
    assert restarted.cards[historical]["send_attempts"] == 1  # Preserve its ambiguous-send fence.
    assert restarted.cards[dismissed]["send_attempts"] == 1
    assert restarted.cards[dismissed]["message_deleted"] is True
    assert restarted._anchor(other_topic) == other_topic
    assert restarted.cards[other_topic]["message_id"] == "other"
    adapter.send_delegation_card.assert_awaited_once()


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
    from types import MethodType
    transport._send_delegation_card = MethodType(TelegramAdapter._send_delegation_card, transport)
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


async def drain_cards(cards):
    while cards.pending:
        await asyncio.gather(*list(cards.pending.values()))


@pytest.mark.asyncio
async def test_conversation_aggregates_tasks_and_retires_only_delivered_rows(tmp_path):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="one")),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    cards = DelegationCards(runner, home=tmp_path, interval=0)
    owner = dict(profile="default", session_id="s", session_key="r", chat_id="42", thread_id="8")
    a = dict(parent_task_id="a" * 32, thread_ref="A", task_label="Check receipt", subagent_type="lead", owner=owner)
    b = dict(parent_task_id="b" * 32, thread_ref="B", task_label="Check display", subagent_type="explorer", owner=owner)
    await cards.observe(source, "r", "s", 1, "subagent.start", None, a)
    await drain_cards(cards)
    await cards.observe(source, "r", "s", 2, "subagent.start", None, b)
    await drain_cards(cards)
    adapter.send_delegation_card.assert_awaited_once()
    text = adapter.edit_message.call_args.args[2]
    assert "A. Check receipt" in text and "B. Check display" in text
    assert "Lead" in text and "Explorer" in text
    assert adapter.edit_message.call_args.args[1] == "one"
    await cards.observe(source, "r", "s", 1, "subagent.complete", None, a)
    event = MessageEvent(source=source, text="handled A", internal=True, metadata={
        "delegation_parent_task_id": a["parent_task_id"], "delegation_owner": owner, "delegation_thread_refs": ["A"]})
    proof = cards.receipt(event, "r", 3)
    # A new row must not invalidate the exact earlier receipt, nor be handled by it.
    c = {**a, "thread_ref": "C", "task_label": "Check race", "subagent_type": None}
    await cards.observe(source, "r", "s", 3, "subagent.start", None, c)
    await cards.delivered(proof)
    await drain_cards(cards)
    text = adapter.edit_message.call_args.args[2]
    assert "A. Check receipt" not in text and "B. Check display" in text and "C. Check race" in text
    assert "C. Check race\n" in text  # no fabricated default role
    adapter.delete_message.assert_not_awaited()
    restored = DelegationCards(runner, home=tmp_path, interval=0)
    await restored.reconcile()
    await drain_cards(restored)
    assert "Explorer" in adapter.edit_message.call_args.args[2]
    assert "A. Check receipt" not in adapter.edit_message.call_args.args[2]
    adapter.send_delegation_card.assert_awaited_once()
    for data in (b, c):
        event.metadata = {"delegation_parent_task_id": data["parent_task_id"], "delegation_owner": owner,
                          "delegation_thread_refs": [data["thread_ref"]]}
        await restored.delivered(restored.receipt(event, "r", 4))
    await drain_cards(restored)
    adapter.delete_message.assert_awaited_once_with("42", "one")
    restarted = DelegationCards(runner, home=tmp_path, interval=0)
    await restarted.reconcile()
    await drain_cards(restarted)
    adapter.send_delegation_card.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_sends_share_message_and_keep_topic_isolation(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    async def send(*args):
        started.set()
        await release.wait()
        return SendResult(success=True, message_id="one")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(side_effect=send),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    cards = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="8")
    owner = dict(profile="default", session_id="s", session_key="r", chat_id="42", thread_id="8")
    a = dict(parent_task_id="a" * 32, thread_ref="A", owner=owner)
    await cards.observe(source, "r", "s", 1, "subagent.start", None, a)
    await started.wait()
    b = {**a, "parent_task_id": "b" * 32, "thread_ref": "B"}
    other = asyncio.create_task(cards.observe(source, "r", "s", 2, "subagent.start", None, b))
    release.set()
    await other
    await drain_cards(cards)
    adapter.send_delegation_card.assert_awaited_once()
    assert "A." in adapter.edit_message.call_args.args[2] and "B." in adapter.edit_message.call_args.args[2]
    assert adapter.edit_message.call_args.args[1] == "one"
    source.thread_id = "9"
    await cards.observe(source, "r", "s", 3, "subagent.start", None,
                        {**a, "parent_task_id": "c" * 32, "owner": {**owner, "thread_id": "9"}})
    await drain_cards(cards)
    assert adapter.send_delegation_card.await_count == 2


@pytest.mark.asyncio
async def test_legacy_messages_merge_with_explicit_link_and_no_handled_inference(tmp_path):
    import json
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(side_effect=[
        SendResult(success=True, message_id="old-a"), SendResult(success=True, message_id="old-b")]),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=False))
    runner = SimpleNamespace(_adapter_for_source=lambda _: adapter)
    legacy = {}
    for index, key in enumerate(("a" * 32, "b" * 32)):
        manager = DelegationCards(runner, home=tmp_path / str(index), interval=0)
        owner = dict(profile="default", session_id=f"s{index}", session_key="r", chat_id="42", thread_id="")
        await manager.observe(source, "r", f"s{index}", 1, "subagent.start", None,
                              dict(parent_task_id=key, thread_ref="A", owner=owner, role="leaf"))
        await drain_cards(manager)
        legacy[key] = manager.cards[key]
        legacy[key].pop("presentation_key")
        legacy[key]["rows"]["A"].pop("display_ref")
    path = tmp_path / "cache" / "delegation" / "cards.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    restored = DelegationCards(runner, home=tmp_path, interval=0)
    await restored.reconcile()
    await drain_cards(restored)
    text = adapter.edit_message.call_args.args[2]
    assert "A. Task A" in text and "A·2. Task A·2" in text
    assert "Worker" not in text and "Leaf" not in text
    assert adapter.edit_message.call_args.args[1] == "old-a"
    adapter.delete_message.assert_awaited_once_with("42", "old-b")
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["b" * 32]["presentation_key"] == "a" * 32
    assert persisted["b" * 32]["consolidated_message_id"] == "old-b"
    assert persisted["a" * 32]["presentation_cleanup"][0]["message_id"] == "old-b"
    assert persisted["a" * 32]["presentation_cleanup"][0]["state"] == "ready"
    assert all(not c.get("handled") and not c.get("retired") for c in persisted.values())
    # Failed deletion retries even when the aggregate render is unchanged.
    adapter.delete_message.return_value = True
    again = DelegationCards(runner, home=tmp_path, interval=0)
    await again.reconcile()
    await drain_cards(again)
    assert adapter.send_delegation_card.await_count == 2
    assert again.cards["a" * 32]["presentation_cleanup"][0]["state"] == "deleted"


@pytest.mark.asyncio
async def test_flood_retry_sends_only_latest_card_state_and_preserves_one_message(tmp_path):
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(side_effect=[
        SendResult(success=False, error="flood_control:0.05", retryable=True, retry_after=0.05),
        SendResult(success=True, message_id="one")]),
        edit_message=AsyncMock(return_value=SendResult(success=True)), delete_message=AsyncMock(return_value=True))
    cards = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    data = dict(parent_task_id="a" * 32, thread_ref="A", task_label="Check display", owner=dict(
        profile="default", session_id="s", session_key="r", chat_id="42", thread_id=""))
    await cards.observe(source, "r", "s", 1, "subagent.start", None, data)
    await list(cards.pending.values())[0]
    await cards.observe(source, "r", "s", 1, "subagent.tool", "terminal", data)
    await cards.observe(source, "r", "s", 1, "subagent.tool", "computer_use", data)
    await drain_cards(cards)
    assert adapter.send_delegation_card.await_count == 2  # first was explicitly rejected, not ambiguous
    text = adapter.send_delegation_card.call_args.args[1]
    assert "computer_use" in text and "terminal" not in text
    assert cards.cards["a" * 32]["message_id"] == "one"
    assert "retry_at" not in cards.cards["a" * 32]
    adapter.edit_message.assert_not_awaited()

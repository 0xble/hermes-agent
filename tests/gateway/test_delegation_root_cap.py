"""Root-group window at the real profile config/lifecycle/Telegram boundary."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from gateway.config import Platform, PlatformConfig
from gateway.delegation_cards import DelegationCards, render_card
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter


async def drain(manager):
    while manager.pending:
        await asyncio.gather(*list(manager.pending.values()))


@pytest.mark.asyncio
@pytest.mark.parametrize("grouping", ["single", "multiple"])
async def test_root_window_preserves_hierarchy_activity_and_full_ledger(tmp_path, grouping):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={"rich_messages": False}))
    bot = MagicMock()
    adapter._bot = bot
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    bot.edit_message_text = AsyncMock(return_value=SimpleNamespace(message_id=7))
    bot.send_chat_action = AsyncMock()
    manager = DelegationCards(SimpleNamespace(_adapter_for_source=lambda _: adapter), home=tmp_path, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    owner = dict(profile="default", session_id="s", session_key="r", chat_id="42", thread_id="")
    events = []
    for i in range(7):
        key = "a" * 32 if grouping == "single" else f"{i + 1:032x}"
        ref = chr(65 + i) if grouping == "single" else "A"
        data = dict(parent_task_id=key, thread_ref=ref, task_label=f"Root {i}", owner=owner, depth=2)
        events.append(data)
        await manager.observe(source, "r", "s", 1, "subagent.start", None, data)
        child = dict(parent_task_id=f"{100 + i:032x}", thread_ref="A", task_label=f"Child {i}",
                     owner=owner, card_parent_task_id=key, card_parent_thread_ref=ref, depth=0)
        await manager.observe(source, "r", "s", 1, "subagent.start", None, child)
    await drain(manager)
    anchor = manager._anchor(events[0]["parent_task_id"])

    def check(text, cap):
        expected = list(range(max(0, 7 - cap), 7))
        assert [i for i in range(7) if f"Root {i}" in text] == expected
        assert [i for i in range(7) if f"Child {i}" in text] == expected
        assert [text.index(f"Root {i}") for i in expected] == sorted(text.index(f"Root {i}") for i in expected)
        for i in expected:
            assert text.index(f"Root {i}") < text.index(f"Child {i}")
            assert "\u00a0\u00a0\u00a0\u00a0○ Child" in text

    check(bot.send_message.call_args.kwargs["text"], 5)
    await manager.observe(source, "r", "s", 1, "subagent.tool", "terminal", events[0])
    await manager.observe(source, "r", "s", 1, "subagent.tool", "read_file", events[-1])
    await drain(manager)
    check(bot.edit_message_text.call_args.kwargs["text"], 5)
    assert manager.cards[events[0]["parent_task_id"]]["rows"][events[0]["thread_ref"]]["last_tool"] == "terminal"
    assert len(manager._projection(anchor)["rows"]) == 14
    before = copy.deepcopy(manager.cards)
    for display, cap in [({"delegation_max_visible_roots": 2}, 2),
                         ({"delegation_max_visible_roots": 2, "platforms": {"telegram": {"delegation_max_visible_roots": 6}}}, 6),
                         ({"delegation_max_visible_roots": 20}, 20)]:
        (tmp_path / "config.yaml").write_text(yaml.safe_dump({"display": display}))
        await manager._flush(anchor)
        check(bot.edit_message_text.call_args.kwargs["text"], cap)
    for key, card in manager.cards.items():
        assert card["rows"] == before[key]["rows"]
        assert not card.get("handled") and not card.get("retired")
    assert sum(len(c["rows"]) for c in json.loads(manager.path.read_text()).values()) == 14

    # Older stores have neither row chronology nor reliable distinct timestamps.
    # Stored card/row insertion order is the deterministic fallback; rendering is pure.
    legacy = manager._projection(anchor)
    for row in legacy["rows"].values():
        row.pop("presentation_order", None)
    original = copy.deepcopy(legacy)
    assert render_card(legacy) == render_card(legacy, now=999999)
    assert legacy == original
    check(render_card(legacy), 5)

    # A new identity in the oldest execution must sort AFTER newer executions,
    # even when their card timestamps tie; restart must retain that chronology.
    (tmp_path / "config.yaml").write_text("display: {delegation_max_visible_roots: 2}\n")
    late = {**events[0], "thread_ref": "Z", "task_label": "Newest identity"}
    await manager.observe(source, "r", "s", 1, "subagent.start", None, late)
    await drain(manager)
    for card in manager.cards.values():
        card["started_at"] = 0
    manager._save()
    restored = DelegationCards(manager.runner, home=tmp_path, interval=0)
    await restored._flush(anchor)
    text = bot.edit_message_text.call_args.kwargs["text"]
    assert "Root 6" in text and "Newest identity" in text and "Root 5" not in text
    assert text.index("Root 6") < text.index("Newest identity")
    # Parse failure retains the prior valid profile window rather than widening it.
    previous_text = text
    previous_edits = bot.edit_message_text.await_count
    (tmp_path / "config.yaml").write_text("display: [broken")
    await restored._flush(anchor)
    assert bot.edit_message_text.call_args.kwargs["text"] == previous_text
    assert bot.edit_message_text.await_count == previous_edits
    # A repaired authored value is adopted without losing the stored hierarchy.
    (tmp_path / "config.yaml").write_text("display: {delegation_max_visible_roots: 5}\n")
    await restored._flush(anchor)
    text = bot.edit_message_text.call_args.kwargs["text"]
    assert "Root 2" not in text and "Root 3" in text and "Newest identity" in text
    # Replacement sends use the same window; delete-first ordering is unchanged.
    from gateway.delegation_card_anchor import observe_conversation
    (tmp_path / "config.yaml").write_text("display: {delegation_max_visible_roots: 2}\n")
    bot.delete_message = AsyncMock(return_value=True)
    bot.send_message.return_value = SimpleNamespace(message_id=8)
    for mid in range(20, 26):
        observe_conversation(manager, adapter, "42", "", mid)
    await drain(manager)
    text = bot.send_message.call_args.kwargs["text"]
    assert "Root 6" in text and "Newest identity" in text and "Root 5" not in text
    bot.delete_message.assert_awaited_once()
    assert manager.cards[anchor]["last_reanchor"]["order"] == "delete_first"


@pytest.mark.parametrize("value, expected", [(None, 5), (1, 1), (3, 3), (0, 5), (-1, 5),
    (True, 5), (False, 5), (2.5, 5), ("3", 5), ("unlimited", 5), ([], 5), ({}, 5)])
def test_config_root_cap_is_positive_integer_only(value, expected):
    from gateway.display_config import resolve_display_setting
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert resolve_display_setting({}, "telegram", "delegation_max_visible_roots") == 5
    assert DEFAULT_CONFIG["display"]["delegation_max_visible_roots"] == 5
    for display in ({"delegation_max_visible_roots": value},
                    {"delegation_max_visible_roots": 5, "platforms": {"telegram": {"delegation_max_visible_roots": value}}}):
        assert resolve_display_setting({"display": display}, "telegram", "delegation_max_visible_roots") == expected


@pytest.mark.asyncio
async def test_profile_resolution_and_deep_groups_are_presentation_only(tmp_path, monkeypatch):
    from pathlib import Path
    from gateway.run import GatewayRunner
    from gateway.platforms.base import SendResult
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default = tmp_path / ".hermes"
    profile = default / "profiles" / "sample"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default))
    (default / "config.yaml").write_text("display: {delegation_max_visible_roots: 1}\n")
    (profile / "config.yaml").write_text("display: {delegation_max_visible_roots: 3}\n")
    adapter = SimpleNamespace(send_delegation_card=AsyncMock(return_value=SendResult(success=True, message_id="1")),
                              edit_message=AsyncMock(return_value=SendResult(success=True)))
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._adapter_for_source = lambda source: adapter
    manager = DelegationCards(runner, home=default, interval=0)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", profile="sample")
    owner = dict(profile="sample", session_id="s", session_key="r", chat_id="42", thread_id="")
    for i in range(6):
        parent = None
        for depth in range(4):
            key = f"{i * 4 + depth + 1:032x}"
            label = f"Root {i}" if depth == 0 else f"Descendant {i} level {depth} full unshortened label"
            data = dict(parent_task_id=key, thread_ref="A", task_label=label, owner=owner)
            if parent:
                data.update(card_parent_task_id=parent, card_parent_thread_ref="A")
            await manager.observe(source, "r", "s", 1, "subagent.start", None, data)
            parent = key
    await drain(manager)
    text = adapter.send_delegation_card.call_args.args[1]
    assert [i for i in range(6) if f"Root {i}" in text] == [3, 4, 5]
    for i in range(6):
        for depth in range(1, 4):
            assert (f"Descendant {i} level {depth} full unshortened label" in text) == (i >= 3)
    assert sum(len(c["rows"]) for c in manager.cards.values()) == 24
    for malformed in ("display: [invalid]", "display: {platforms: invalid}", "[invalid]"):
        (profile / "config.yaml").write_text(malformed)
        key = manager._anchor(next(iter(manager.cards)))
        manager.cards[key]["rendered"] = ""
        await manager._flush(key)
        text = adapter.edit_message.call_args.args[2]
        assert [i for i in range(6) if f"Root {i}" in text] == [1, 2, 3, 4, 5]

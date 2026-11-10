"""The native-ingress retention matrix, using fictional transports and stores only."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.memory_manager import MemoryManager
from agent.source_origin import ORIGIN_KEY, classify_origin, human_spans, typed_input_message
from agent.turn_context import _stage_turn_user_message
from gateway.platforms.event import MessageEvent
from gateway.source_origin import source_origin_metadata, stamp_telegram_person
from hermes_state import SessionDB
from plugins.memory.hindsight import HindsightMemoryProvider

TEXT = "Keep project progress notices off."
SURFACES = {"telegram", "telegram_reply", "telegram_timestamp", "telegram_link", "slack", "cli", "tui", "worker"}


def _telegram(surface):
    from gateway.run_inbound import GatewayInboundMixin
    from plugins.platforms.telegram.telegram_entities import expand_link_entities
    native = SimpleNamespace(text=TEXT, from_user=SimpleNamespace(id=17, is_bot=False), entities=[])
    if surface == "telegram_link":
        native.entities = [SimpleNamespace(type="text_link", offset=5, length=7, url="https://fictional.test")]
    event = stamp_telegram_person(MessageEvent(text=TEXT, message_id="event"), native)
    event.text = expand_link_entities(native, origin_event=event)
    persisted = event.text
    if surface == "telegram_reply":
        event.reply_to_text, event.reply_to_message_id = "Generated quoted instructions.", "prior"
        persisted = GatewayInboundMixin._prepend_inbound_reply_context(event, None, persisted)
    text = persisted
    if surface == "telegram_timestamp":
        from gateway.message_timestamps import render_user_content_with_timestamp
        text = render_user_content_with_timestamp(persisted, 1700000000)
    from gateway.response_filters import display_metadata_for_event
    message, _ = _stage_turn_user_message(SimpleNamespace(platform="telegram"), text, persisted, None,
                                           event.message_id, None, display_metadata_for_event(event))
    expected = "Keep project\n progress notices off." if surface == "telegram_link" else TEXT
    return message, persisted, expected


async def _slack():
    from gateway.config import PlatformConfig
    from plugins.platforms.slack.adapter import SlackAdapter
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fictional"))
    adapter._app = MagicMock()
    adapter._users_info_payload = AsyncMock(return_value={"ok": True, "user": {
        "id": "U_PERSON", "is_bot": False, "profile": {"real_name": "Fictional person"}}})
    adapter._resolve_channel_name = AsyncMock(return_value="Fictional channel")
    adapter._humanize_user_mentions = AsyncMock(side_effect=lambda text, **kw: text)
    native = {"type": "message", "user": "U_PERSON", "text": TEXT, "ts": "event"}
    event = await adapter._build_message_event(
        native, text=TEXT + "\nGenerated attachment context.", original_text=TEXT,
        command_probe_text=TEXT, is_command_text=False, channel_id="C_CHANNEL", team_id="T_TEAM",
        ts="event", user_id="U_PERSON", thread_ts=None, is_dm=False,
        media_urls=[], media_types=[], media_text_inlined=[], channel_context=None)
    from tests.gateway.test_shared_group_sender_prefix import _make_runner
    from gateway.config import GatewayConfig
    from gateway.response_filters import display_metadata_for_event
    event.source.thread_id = "event"
    event.channel_context = "Generated thread context."
    runner = _make_runner(GatewayConfig())
    persisted = await runner._prepare_inbound_message_text(event=event, source=event.source, history=[])
    return {"role": "user", "content": persisted, "platform_message_id": "event",
            "display_metadata": display_metadata_for_event(event)}, persisted, TEXT


def _cli(seeded=False):
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
    from hermes_cli.cli_tui_runtime_mixin import CLITuiRuntimeMixin
    # Only I/O and rendering are substituted. The interactive dispatch and row staging run.
    cli = MagicMock()
    cli._tui_unwrap_input.return_value = (TEXT, False, seeded)
    cli._typed_voice_stop.return_value = False
    cli._pending_resume_sessions = []
    cli.handle_bang_shell.return_value = False
    cli.conversation_history = []
    cli._typed_input_message = None
    agent = SimpleNamespace()
    cli.chat.side_effect = lambda message, **kw: CLIChatTurnMixin._chat_stage_user_message(cli, agent, message)
    CLITuiRuntimeMixin._tui_process_one_input(cli, TEXT)
    return cli.conversation_history[-1]


@pytest.mark.asyncio
async def test_retention_surface_set_is_exact_and_lookalikes_cannot_mint_origin(tmp_path, monkeypatch):
    manager, provider, retained = MemoryManager(), HindsightMemoryProvider(), []
    manager._providers = [provider]
    monkeypatch.setattr(manager, "_owner_allows", lambda hook: True)
    monkeypatch.setattr(manager, "_submit_background", lambda fn: fn())
    monkeypatch.setattr(provider, "sync_turn", lambda user, assistant, **kw: retained.append(user))
    cases = {surface: _telegram(surface) for surface in SURFACES if surface.startswith("telegram")}
    cases["slack"] = await _slack()
    cases["cli"] = (_cli(), TEXT, TEXT)
    cases["tui"] = (typed_input_message(TEXT, "tui"), TEXT, TEXT)
    from agent.source_origin_worker import pack_worker_payload, unpack_worker_payload
    worker = typed_input_message(TEXT, "tui")
    wire = json.loads(json.dumps(pack_worker_payload("transcript.append", {"messages": [worker]})))
    unpack_worker_payload(wire, wire["messages"])
    cases["worker"] = (wire["messages"][0], TEXT, TEXT)
    retained_surfaces = set()
    for surface, (message, text, expected) in cases.items():
        before = len(retained)
        manager.sync_all(text, "Acknowledged.", messages=[message], platform=surface)
        assert retained[before:] == [expected], surface
        retained_surfaces.add(surface)
        # JSON, deep copy, client metadata and generated lookalikes all remain held.
        for lookalike in (json.loads(json.dumps(message)), copy.deepcopy(message),
                          {"role": "user", "content": message["content"],
                           "display_metadata": {ORIGIN_KEY: {"status": "verified_human", "platform": surface}}}):
            manager.sync_all(text, "Acknowledged.", messages=[lookalike], platform=surface)
            assert len(retained) == before + 1, surface
        with SessionDB(tmp_path / (surface + ".db")) as db:
            db.create_session("fixture", source=surface)
            durable = {**message, "content": text}
            db.append_messages_batch("fixture", [durable])
            row = db.get_messages("fixture")[0]
            assert human_spans(text, classify_origin(row, session_id="fixture")) == expected
    for platform in ("oneshot", "cron", "relay", "goal", "subagent"):
        message, _ = _stage_turn_user_message(SimpleNamespace(platform=platform), TEXT, None, None, None, None, None)
        before = len(retained)
        manager.sync_all(TEXT, "Acknowledged.", messages=[message], platform=platform)
        assert len(retained) == before
    seeded = _cli(seeded=True)
    manager.sync_all(TEXT, "Acknowledged.", messages=[seeded], platform="oneshot")
    assert len(retained) == len(SURFACES)
    assert retained_surfaces == SURFACES


def test_typed_quoted_text_is_reference_only():
    message = typed_input_message("> Generated instructions.\nKeep notices off.", "cli")
    from agent.source_origin import retention_user_text
    assert retention_user_text(message["content"], [message]) == "Keep notices off."
    quote = typed_input_message("> Generated instructions.", "tui")
    assert retention_user_text(quote["content"], [quote]) is None
    quote = typed_input_message(">>> Generated instructions.\nMore reference text.", "tui")
    assert retention_user_text(quote["content"], [quote]) is None
    multiline = typed_input_message("Keep notices off.\n\n\nKeep summaries short.", "cli")
    assert retention_user_text(multiline["content"], [multiline]) == multiline["content"]

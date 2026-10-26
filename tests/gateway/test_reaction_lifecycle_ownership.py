"""Intentional responses retain ownership of replacing native reaction slots."""
import asyncio
from enum import Enum
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import ProcessingOutcome


@pytest.fixture(params=["signal", "telegram", "photon"])
def reaction_transport(request, monkeypatch):
    platform = request.param
    module_name = "gateway.platforms.signal" if platform == "signal" else f"plugins.platforms.{platform}.adapter"
    module = import_module(module_name)
    adapter = object.__new__(getattr(module, f"{platform.title()}Adapter"))
    adapter.platform = Platform(platform)
    adapter.config = PlatformConfig(extra={"reactions": True})
    monkeypatch.setenv(f"{platform.upper()}_REACTIONS", "true")
    calls = []
    control = SimpleNamespace(fail=False, raises=False, entered=None, release=None)

    async def deliver(chat, message, emoji, author=""):
        calls.append((str(chat), str(message), emoji, author))
        if emoji == "😁":
            if control.entered is not None:
                control.entered.set()
                await control.release.wait()
            if control.raises:
                raise RuntimeError("transport unavailable")
            if control.fail:
                return False
        return True

    if platform == "signal":
        adapter.account = "test-bot"
        adapter.dm_allow_from = ["*"]

        async def rpc(method, params):
            ok = await deliver(params["recipient"][0], params["targetTimestamp"], params["emoji"], params["targetAuthor"])
            return {} if ok else None

        adapter._rpc = AsyncMock(side_effect=rpc)
    elif platform == "telegram":
        constants = import_module("telegram.constants")
        monkeypatch.setattr(constants, "ReactionEmoji", Enum("ReactionEmoji", {"GRIN": "😁"}))

        async def telegram(**kwargs):
            return await deliver(kwargs["chat_id"], kwargs["message_id"], kwargs["reaction"])

        adapter._bot = SimpleNamespace(set_message_reaction=AsyncMock(side_effect=telegram))
    else:
        adapter._last_inbound_by_chat = {adapter._normalize_chat_key("123"): "456"}

        async def sidecar(path, body):
            ok = await deliver(body["spaceId"], body["messageId"], body.get("emoji"))
            if not ok:
                raise RuntimeError("sidecar rejected reaction")
            return {"ok": True}

        adapter._sidecar_call = AsyncMock(side_effect=sidecar)
    event = SimpleNamespace(
        source=SimpleNamespace(chat_id="123", user_id="author"), message_id="456",
        raw_message={"sender": "author", "timestamp_ms": 456},
    )
    return platform, adapter, event, calls, control


def _ordinary_completion(platform, outcome):
    if platform == "signal":
        if outcome == ProcessingOutcome.CANCELLED:
            return []
        return ["", "✅" if outcome == ProcessingOutcome.SUCCESS else "❌"]
    if outcome == ProcessingOutcome.CANCELLED:
        return [None]
    final = "👍" if outcome == ProcessingOutcome.SUCCESS else "👎"
    return [None, final] if platform == "photon" else [final]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", list(ProcessingOutcome))
@pytest.mark.parametrize("intent", [
    "current", "failed", "raised", "unsupported", "other_message", "other_chat", "other_author", "ordinary", "disabled",
])
async def test_completion_preserves_only_a_confirmed_intentional_target(reaction_transport, monkeypatch, outcome, intent):
    platform, adapter, event, calls, control = reaction_transport
    enabled = intent != "disabled"
    monkeypatch.setenv(f"{platform.upper()}_REACTIONS", str(enabled).lower())
    await adapter.on_processing_start(event)
    control.fail, control.raises = intent == "failed", intent == "raised"
    chat = "999" if intent == "other_chat" else "123"
    message = "789" if intent == "other_message" else "456"
    author = "other" if intent == "other_author" else "author"
    emoji = "not-supported" if intent == "unsupported" and platform == "telegram" else "😁"
    routing = {} if intent == "unsupported" and platform == "signal" else {"user_id": author}
    if intent == "unsupported" and platform == "photon":
        adapter._last_inbound_by_chat.clear()
        message = ""
    if intent != "ordinary":
        delivered = await adapter.add_reaction(chat, message, emoji, **routing)
        assert delivered is (intent not in {"failed", "raised", "unsupported"})
    before_complete = len(calls)
    await adapter.on_processing_complete(event, outcome)
    preserved = intent in {"current", "disabled"} or (intent == "other_author" and platform != "signal")
    expected = [] if preserved or not enabled else _ordinary_completion(platform, outcome)
    assert [call[2] for call in calls[before_complete:]] == expected
    if preserved:
        assert calls[before_complete - 1][2] == "😁"

    # Cleanup errors can invoke completion again on the same event.
    before_repeat = len(calls)
    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
    expected_repeat = [] if preserved or not enabled else _ordinary_completion(platform, ProcessingOutcome.FAILURE)
    assert [call[2] for call in calls[before_repeat:]] == expected_repeat

    # Reprocessing the same target is a new lifecycle, not a permanent mute.
    control.fail = control.raises = False
    await adapter.on_processing_start(event)
    before_complete = len(calls)
    await adapter.on_processing_complete(event, outcome)
    assert [call[2] for call in calls[before_complete:]] == (_ordinary_completion(platform, outcome) if enabled else [])
    assert not adapter._processing_reaction_states


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [True, False])
async def test_completion_waits_for_an_in_flight_explicit_reaction(reaction_transport, delivered):
    platform, adapter, event, calls, control = reaction_transport
    await adapter.on_processing_start(event)
    control.fail = not delivered
    control.entered, control.release = asyncio.Event(), asyncio.Event()
    explicit = asyncio.create_task(adapter.add_reaction("123", "456", "😁", user_id="author"))
    completion_entered = asyncio.Event()

    async def complete():
        completion_entered.set()
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    completion = None
    try:
        await asyncio.wait_for(control.entered.wait(), 5)
        before_complete = len(calls)
        completion = asyncio.create_task(complete())
        await asyncio.wait_for(completion_entered.wait(), 5)
        assert len(calls) == before_complete
        control.release.set()
        assert await asyncio.wait_for(explicit, 5) is delivered
        await asyncio.wait_for(completion, 5)
        expected = [] if delivered else _ordinary_completion(platform, ProcessingOutcome.SUCCESS)
        assert [call[2] for call in calls[before_complete:]] == expected
        assert not adapter._processing_reaction_states
    finally:
        control.release.set()
        tasks = [task for task in (explicit, completion) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

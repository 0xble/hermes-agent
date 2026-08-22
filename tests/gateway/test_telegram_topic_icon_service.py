from __future__ import annotations

from dataclasses import dataclass

import pytest

from plugins.platforms.telegram.topic_icon_service import TelegramTopicIconService
from plugins.platforms.telegram.topic_icons import CustomTopicIconCatalog
from plugins.platforms.telegram.user_transport import (
    TelegramTopicReadbackMismatch,
    TopicSnapshot,
    VerifiedTopicMutation,
)


class _Transport:
    def __init__(self, snapshots, *, mismatch=False):
        self.snapshots = list(snapshots)
        self.mismatch = mismatch
        self.set_calls = []

    async def get_topic(self, *, peer_id, topic_id):
        return self.snapshots.pop(0)

    async def set_topic_icon(
        self,
        *,
        peer_id,
        topic_id,
        icon_emoji_id,
        expected_icon_emoji_id=None,
    ):
        self.set_calls.append((peer_id, topic_id, icon_emoji_id))
        if self.mismatch:
            raise TelegramTopicReadbackMismatch("mismatch")
        return VerifiedTopicMutation(peer_id, topic_id, icon_emoji_id, icon_emoji_id, 1.0)


class _State:
    def __init__(self, ownership=None):
        self.ownership = ownership
        self.calls = []

    async def get_telegram_topic_icon_state(self, *, chat_id, thread_id):
        if self.ownership is None:
            return None
        return {"ownership": self.ownership, "custom_emoji_id": "20"}

    async def record_telegram_topic_icon_observation(self, **kwargs):
        self.calls.append(("manual", kwargs))
        return "manual"

    async def mark_telegram_topic_icon_auto(self, **kwargs):
        self.calls.append(("auto", kwargs))

    async def record_telegram_topic_icon_selection(self, **kwargs):
        self.calls.append(("history", kwargs))


def _snapshot(icon):
    return TopicSnapshot(202, 303, "Topic", icon, False, False)


def _catalog():
    return CustomTopicIconCatalog.from_rows(
        [
            {
                "emoji": "🧹",
                "custom_emoji_id": 20,
                "pack": "AppleObjectsHomeTools",
                "pack_title": "Apple Objects",
            }
        ],
        pack_order=("AppleObjectsHomeTools",),
    )


@pytest.mark.asyncio
async def test_verify_classifies_without_mutating_ownership():
    state = _State(ownership="auto")
    service = TelegramTopicIconService(
        catalog=_catalog(),
        transport=_Transport([_snapshot(20)]),
        peer_id=202,
        state=state,
    )

    result = await service.verify(303)

    assert result["state"] == "custom_auto"
    assert result["candidate"]["source"] == "custom_pack"
    assert state.calls == []


@pytest.mark.asyncio
async def test_verify_reads_ownership_from_bot_api_chat_not_mtproto_bot_peer():
    class State(_State):
        async def get_telegram_topic_icon_state(self, *, chat_id, thread_id):
            assert chat_id == "101"
            assert thread_id == "303"
            return {"ownership": "auto", "custom_emoji_id": "20"}

    service = TelegramTopicIconService(
        catalog=_catalog(),
        transport=_Transport([_snapshot(20)]),
        peer_id=202,
        state_chat_id="101",
        state=State(),
    )

    assert (await service.verify(303))["state"] == "custom_auto"


@pytest.mark.asyncio
async def test_operator_set_records_manual_only_after_verified_receipt():
    state = _State()
    transport = _Transport([_snapshot(None)])
    service = TelegramTopicIconService(
        catalog=_catalog(), transport=transport, peer_id=202, state=state
    )

    result = await service.set(303, "🧹")

    assert result["result"] == "verified"
    assert transport.set_calls == [(202, 303, 20)]
    assert [name for name, _ in state.calls] == ["manual"]


@pytest.mark.asyncio
async def test_automatic_apply_commits_auto_then_history_only_after_readback():
    state = _State(ownership="auto")
    transport = _Transport([_snapshot(20), _snapshot(20)])
    service = TelegramTopicIconService(
        catalog=_catalog(), transport=transport, peer_id=202, state=state
    )

    result = await service.apply_automatic(
        topic_id=303,
        candidate=_catalog().resolve("🧹"),
        chat_id="202",
        thread_id="303",
    )

    assert result["result"] == "verified"
    assert [name for name, _ in state.calls] == ["auto", "history"]


@pytest.mark.asyncio
async def test_automatic_apply_preserves_manual_and_writes_no_state_on_mismatch():
    manual_state = _State(ownership="manual")
    manual_transport = _Transport([_snapshot(99)])
    manual_service = TelegramTopicIconService(
        catalog=_catalog(),
        transport=manual_transport,
        peer_id=202,
        state=manual_state,
    )
    result = await manual_service.apply_automatic(
        topic_id=303,
        candidate=_catalog().resolve("🧹"),
        chat_id="202",
        thread_id="303",
    )
    assert result["result"] == "preserved_manual"
    assert manual_transport.set_calls == []

    state = _State(ownership="auto")
    transport = _Transport([_snapshot(20), _snapshot(20)], mismatch=True)
    service = TelegramTopicIconService(
        catalog=_catalog(), transport=transport, peer_id=202, state=state
    )
    with pytest.raises(TelegramTopicReadbackMismatch):
        await service.apply_automatic(
            topic_id=303,
            candidate=_catalog().resolve("🧹"),
            chat_id="202",
            thread_id="303",
        )
    assert state.calls == []


@pytest.mark.asyncio
async def test_automatic_apply_preserves_manual_clear_to_default_during_selection():
    state = _State(ownership="auto")
    transport = _Transport([_snapshot(None)])
    service = TelegramTopicIconService(
        catalog=_catalog(), transport=transport, peer_id=202, state=state
    )
    candidate = _catalog().resolve("🧹")
    assert candidate is not None

    result = await service.apply_automatic(
        topic_id=303,
        candidate=candidate,
        chat_id="202",
        thread_id="303",
        expected_icon_emoji_id=20,
    )

    assert result["result"] == "icon_changed"
    assert transport.set_calls == []
    assert state.calls == []

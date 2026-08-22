from __future__ import annotations

from dataclasses import dataclass

import pytest

from plugins.platforms.telegram.topic_icons import (
    CustomTopicIconCatalog,
    TopicIconCatalogError,
    choose_custom_topic_icon_candidate,
    normalize_emoji_selector,
)
from plugins.platforms.telegram.topic_icon_gateway import (
    apply_custom_topic_icon_after_title,
)
from plugins.platforms.telegram.user_transport import TopicSnapshot, VerifiedTopicMutation


@dataclass
class _Sticker:
    emoji: str | None
    custom_emoji_id: str | None


@dataclass
class _StickerSet:
    name: str
    title: str
    stickers: list[_Sticker]


class _Bot:
    def __init__(self, packs):
        self.packs = packs
        self.calls = []

    async def get_sticker_set(self, name):
        self.calls.append(name)
        value = self.packs[name]
        if isinstance(value, Exception):
            raise value
        return value

    async def get_me(self):
        return type("BotIdentity", (), {"id": 202, "username": "hermes_bot"})()


def test_unicode_normalization_removes_variation_selectors_but_keeps_zwj():
    assert normalize_emoji_selector("✈️") == "✈"
    assert normalize_emoji_selector("✈︎") == "✈"
    assert normalize_emoji_selector("👩‍💻") == "👩‍💻"


@pytest.mark.asyncio
async def test_catalog_loads_only_custom_rows_and_resolves_by_pack_priority():
    bot = _Bot(
        {
            "First": _StickerSet(
                "First",
                "First pack",
                [_Sticker("🧹️", "20"), _Sticker(None, "21"), _Sticker("💡", None)],
            ),
            "Second": _StickerSet(
                "Second",
                "Second pack",
                [_Sticker("🧹", "10"), _Sticker("🏳️‍🌈", "30")],
            ),
        }
    )
    catalog = CustomTopicIconCatalog(("First", "Second"))

    rows = await catalog.refresh(bot)
    resolved = catalog.resolve("🧹")

    assert len(rows) == 3
    assert resolved is not None
    assert resolved.pack == "First"
    assert resolved.custom_emoji_id == 20
    assert all(row.source == "custom_pack" for row in rows)
    assert bot.calls == ["First", "Second"]


@pytest.mark.asyncio
async def test_catalog_refresh_is_all_or_nothing():
    bot = _Bot(
        {
            "First": _StickerSet("First", "First pack", [_Sticker("🧹", "20")]),
            "Second": RuntimeError("unavailable"),
        }
    )
    catalog = CustomTopicIconCatalog(("First",))
    await catalog.refresh(bot)
    before = catalog.rows
    catalog = CustomTopicIconCatalog(("First", "Second"), initial_rows=before)

    with pytest.raises(TopicIconCatalogError, match="Second"):
        await catalog.refresh(bot)

    assert catalog.rows == before


@pytest.mark.asyncio
async def test_catalog_rejects_pack_identity_mismatch_and_duplicate_config():
    with pytest.raises(TopicIconCatalogError, match="duplicate"):
        CustomTopicIconCatalog(("First", "First"))

    catalog = CustomTopicIconCatalog(("Expected",))
    bot = _Bot(
        {"Expected": _StickerSet("Different", "Wrong pack", [_Sticker("🧹", "20")])}
    )
    with pytest.raises(TopicIconCatalogError, match="identity"):
        await catalog.refresh(bot)


def test_resolver_is_custom_only_bounded_and_supports_filters():
    catalog = CustomTopicIconCatalog.from_rows(
        [
            {"emoji": "🧹", "custom_emoji_id": 20, "pack": "First", "pack_title": "One"},
            {"emoji": "🧹", "custom_emoji_id": 10, "pack": "Second", "pack_title": "Two"},
        ],
        pack_order=("First", "Second"),
    )

    assert catalog.resolve("🧹", pack="Second").custom_emoji_id == 10
    assert catalog.resolve("🚀") is None
    assert len(catalog.list(limit=1)) == 1
    with pytest.raises(TopicIconCatalogError, match="limit"):
        catalog.list(limit=0)


def test_custom_selection_uses_unicode_and_excludes_recent_document_ids():
    catalog = CustomTopicIconCatalog.from_rows(
        [
            {"emoji": "🧹", "custom_emoji_id": 20, "pack": "First", "pack_title": "One"},
            {"emoji": "💡", "custom_emoji_id": 30, "pack": "First", "pack_title": "One"},
        ],
        pack_order=("First",),
    )
    seen = {}

    def selector(title, opening, allowed, **kwargs):
        seen["allowed"] = allowed
        return "💡️"

    selected = choose_custom_topic_icon_candidate(
        catalog,
        title="Workshop cleanup",
        user_message="organize the tools",
        recent_document_ids=[20],
        selector=selector,
    )

    assert selected.custom_emoji_id == 30
    assert seen["allowed"] == ["💡"]
    assert selected.source == "custom_pack"


def test_custom_selection_returns_none_instead_of_default_fallback():
    catalog = CustomTopicIconCatalog.from_rows([], pack_order=("First",))
    assert (
        choose_custom_topic_icon_candidate(
            catalog,
            title="No catalog",
            user_message="nothing loaded",
            selector=lambda *_args, **_kwargs: "🚀",
        )
        is None
    )


def test_custom_selection_bounds_the_model_allowlist_for_large_catalogs():
    catalog = CustomTopicIconCatalog.from_rows(
        [
            {
                "emoji": f"emoji-{index}",
                "custom_emoji_id": index + 1,
                "pack": "First",
                "pack_title": "One",
            }
            for index in range(200)
        ],
        pack_order=("First",),
    )
    seen = {}

    def selector(_title, _opening, allowed, **_kwargs):
        seen["allowed"] = allowed
        return allowed[0]

    assert choose_custom_topic_icon_candidate(
        catalog,
        title="Large catalog",
        user_message="choose a useful icon",
        selector=selector,
    ) is not None
    assert len(seen["allowed"]) <= 96
    assert len(seen["allowed"]) == len(set(seen["allowed"]))


@pytest.mark.asyncio
async def test_gateway_custom_icon_commits_history_only_after_verified_native_write():
    bot = _Bot(
        {
            "AppleObjectsHomeTools": _StickerSet(
                "AppleObjectsHomeTools", "Apple Objects", [_Sticker("🧹", "20")]
            )
        }
    )
    class Transport:
        set_calls = []

        async def get_topic(self, *, peer_id, topic_id):
            return TopicSnapshot(peer_id, topic_id, "Workshop", None, False, False)

        async def set_topic_icon(
            self,
            *,
            peer_id,
            topic_id,
            icon_emoji_id,
            expected_icon_emoji_id=None,
        ):
            self.set_calls.append((peer_id, topic_id, icon_emoji_id))
            return VerifiedTopicMutation(peer_id, topic_id, icon_emoji_id, icon_emoji_id, 1.0)

    class State:
        def __init__(self):
            self.calls = []

        async def get_telegram_topic_icon_state(self, **_kwargs):
            return None

        async def get_telegram_topic_binding(self, **_kwargs):
            return {"session_id": "sess-topic"}

        async def list_recent_telegram_topic_icons(self, **_kwargs):
            return []

        async def mark_telegram_topic_icon_auto(self, **kwargs):
            self.calls.append(("auto", kwargs))

        async def record_telegram_topic_icon_selection(self, **kwargs):
            self.calls.append(("history", kwargs))

    state = State()
    adapter = type(
        "Adapter",
        (),
        {
            "config": type(
                "Config",
                (),
                {
                    "extra": {
                        "auto_topic_icons": True,
                        "topic_icon_provider": "telegram_custom_packs",
                        "topic_icon_custom_packs": ["AppleObjectsHomeTools"],
                        "preserve_manual_topic_icons": True,
                        "topic_icon_overrides": {"Workshop Cleanup": "🧹"},
                        "user_transport": {"expected_user_id": 101},
                    }
                },
            )(),
            "_bot": bot,
            "_custom_topic_icon_catalog": None,
            "topic_icon_selector": staticmethod(
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("exact custom override must bypass the model selector")
                )
            ),
            "get_telegram_user_transport": lambda self, **_kwargs: Transport(),
        },
    )()

    result = await apply_custom_topic_icon_after_title(
        adapter,
        chat_id="101",
        thread_id="303",
        session_id="sess-topic",
        title="Workshop Cleanup",
        user_message="Organize the tools",
        session_db=state,
        hermes_home=None,
    )

    assert result["result"] == "verified"
    assert [name for name, _ in state.calls] == ["auto", "history"]
    assert Transport.set_calls == [(202, 303, 20)]

    mismatch = await apply_custom_topic_icon_after_title(
        adapter,
        chat_id="999",
        thread_id="303",
        session_id="sess-topic",
        title="Workshop Cleanup",
        user_message="Organize the tools",
        session_db=state,
        hermes_home=None,
    )
    assert mismatch["result"] == "user_identity_mismatch"
    assert Transport.set_calls == [(202, 303, 20)]

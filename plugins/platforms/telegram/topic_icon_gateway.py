"""Gateway orchestration for automatic custom-pack topic icons."""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from typing import Any

from plugins.platforms.telegram.topic_icon_service import TelegramTopicIconService
from plugins.platforms.telegram.topic_icons import (
    CustomTopicIconCatalog,
    choose_custom_topic_icon_candidate,
)


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def apply_custom_topic_icon_after_title(
    adapter: Any,
    *,
    chat_id: str,
    thread_id: str,
    session_id: str,
    title: str,
    user_message: str,
    session_db: Any,
    hermes_home: Path | None,
    preferred_aux_route: dict[str, str] | None = None,
) -> dict[str, Any]:
    extra = getattr(getattr(adapter, "config", None), "extra", None)
    extra = extra if isinstance(extra, dict) else {}
    if not extra.get("auto_topic_icons"):
        return {"schema_version": 1, "result": "disabled"}
    if str(extra.get("topic_icon_provider") or "telegram_default") != (
        "telegram_custom_packs"
    ):
        return {"schema_version": 1, "result": "different_provider"}
    user_transport = extra.get("user_transport")
    expected_user_id = (
        user_transport.get("expected_user_id")
        if isinstance(user_transport, dict)
        else None
    )
    if str(expected_user_id or "") != str(chat_id):
        return {"schema_version": 1, "result": "user_identity_mismatch"}
    packs = extra.get("topic_icon_custom_packs")
    packs = tuple(packs) if isinstance(packs, list) else ()
    catalog = getattr(adapter, "_custom_topic_icon_catalog", None)
    if not isinstance(catalog, CustomTopicIconCatalog) or catalog.pack_order != packs:
        catalog = CustomTopicIconCatalog(
            packs,
            ttl_seconds=float(extra.get("topic_icon_catalog_ttl_seconds", 86400)),
        )
        adapter._custom_topic_icon_catalog = catalog
    bot = getattr(adapter, "_bot", None)
    if bot is None:
        return {"schema_version": 1, "result": "bot_unavailable"}
    if not catalog.fresh:
        await catalog.refresh(bot)

    bot_identity = await _await(bot.get_me())
    peer_id = int(getattr(bot_identity, "id", 0) or 0)
    if peer_id <= 0:
        return {"schema_version": 1, "result": "bot_identity_unavailable"}
    transport = await _await(
        adapter.get_telegram_user_transport(
            active_bot_peer_id=peer_id,
            hermes_home=hermes_home,
        )
    )
    service = TelegramTopicIconService(
        catalog=catalog,
        transport=transport,
        peer_id=peer_id,
        state_chat_id=chat_id,
        state=session_db,
        bot=bot,
    )
    observed = await service.verify(int(thread_id))
    if observed["state"] in {"custom_manual", "unknown"} and bool(
        extra.get("preserve_manual_topic_icons", True)
    ):
        return {"schema_version": 1, "result": "preserved_manual"}

    recent_rows = await _await(
        session_db.list_recent_telegram_topic_icons(chat_id=chat_id, limit=24)
    )
    recent_ids = [
        int(row["custom_emoji_id"])
        for row in recent_rows or []
        if isinstance(row, dict) and str(row.get("custom_emoji_id") or "").isdigit()
    ]
    selector = getattr(adapter, "topic_icon_selector", None)

    candidate = None
    overrides = extra.get("topic_icon_overrides")
    normalized_title = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
    normalized_base_title = re.sub(r"\s+#\d+$", "", normalized_title).strip()
    if isinstance(overrides, dict):
        exact = None
        base = None
        for override_title, override_emoji in overrides.items():
            normalized_override = re.sub(
                r"\s+", " ", str(override_title or "")
            ).strip().casefold()
            if normalized_override == normalized_title:
                exact = str(override_emoji or "").strip()
                break
            if normalized_override == normalized_base_title and base is None:
                base = str(override_emoji or "").strip()
        override_selector = exact or base
        if override_selector:
            candidate = catalog.resolve(override_selector)
    if candidate is None:
        candidate = await asyncio.to_thread(
            choose_custom_topic_icon_candidate,
            catalog,
            title=title,
            user_message=user_message,
            recent_document_ids=recent_ids,
            current_document_id=observed.get("icon_emoji_id"),
            instructions=str(extra.get("topic_icon_instructions") or "")[:1000],
            selector=selector if callable(selector) else None,
            preferred_route=preferred_aux_route,
        )
    if candidate is None:
        return {"schema_version": 1, "result": "no_custom_candidate"}

    async def binding_is_current() -> bool:
        binding = await _await(
            session_db.get_telegram_topic_binding(
                chat_id=chat_id, thread_id=thread_id
            )
        )
        current_bot = await _await(bot.get_me())
        return bool(
            isinstance(binding, dict)
            and str(binding.get("chat_id") or chat_id) == str(chat_id)
            and str(binding.get("thread_id") or thread_id) == str(thread_id)
            and str(binding.get("session_id") or "") == str(session_id)
            and int(getattr(current_bot, "id", 0) or 0) == peer_id
        )

    return await service.apply_automatic(
        topic_id=int(thread_id),
        candidate=candidate,
        chat_id=chat_id,
        thread_id=thread_id,
        preserve_manual=bool(extra.get("preserve_manual_topic_icons", True)),
        revalidate=binding_is_current,
        expected_icon_emoji_id=observed.get("icon_emoji_id"),
    )

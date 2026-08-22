"""Readback-gated topic-icon operations shared by CLI and gateway."""

from __future__ import annotations

import inspect
from typing import Any, Callable

from plugins.platforms.telegram.topic_icons import (
    CustomTopicIconCatalog,
    TopicIconCandidate,
    TopicIconCatalogError,
)


_UNSET_ICON = object()


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class TelegramTopicIconService:
    def __init__(
        self,
        *,
        catalog: CustomTopicIconCatalog,
        transport: Any,
        peer_id: int,
        state_chat_id: str | None = None,
        state: Any | None = None,
        bot: Any | None = None,
    ) -> None:
        self.catalog = catalog
        self.transport = transport
        self.peer_id = peer_id
        self.state_chat_id = state_chat_id or str(peer_id)
        self.state = state
        self.bot = bot

    async def status(self, *, live: bool = False) -> dict[str, Any]:
        identity = None
        if live:
            identity = await self.transport.identity()
        counts = {
            pack: sum(1 for row in self.catalog.rows if row.pack == pack)
            for pack in self.catalog.pack_order
        }
        return {
            "schema_version": 1,
            "live": live,
            "provider": "telegram_custom_packs",
            "fallback": "none",
            "configured_packs": list(self.catalog.pack_order),
            "per_pack_counts": counts,
            "total_count": len(self.catalog.rows),
            "cache_age_seconds": self.catalog.age_seconds,
            "user_id": identity.user_id if identity is not None else None,
            "premium": identity.is_premium if identity is not None else None,
        }

    async def catalog_rows(
        self,
        *,
        pack: str | None = None,
        emoji: str | None = None,
        limit: int = 100,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if refresh:
            if self.bot is None:
                raise TopicIconCatalogError("Bot API catalog source is unavailable")
            await self.catalog.refresh(self.bot)
        return {
            "schema_version": 1,
            "items": [
                row.to_dict()
                for row in self.catalog.list(pack=pack, emoji=emoji, limit=limit)
            ],
            "total_loaded": len(self.catalog.rows),
            "fallback": "none",
        }

    def resolve_candidate(
        self, emoji: str, *, pack: str | None = None
    ) -> dict[str, Any] | None:
        candidate = self.catalog.resolve(emoji, pack=pack)
        return candidate.to_dict() if candidate is not None else None

    async def _state_row(self, *, chat_id: str, thread_id: str) -> dict[str, Any] | None:
        if self.state is None:
            return None
        getter = getattr(self.state, "get_telegram_topic_icon_state", None)
        if not callable(getter):
            return None
        return await _await(getter(chat_id=chat_id, thread_id=thread_id))

    async def verify(self, topic_id: int) -> dict[str, Any]:
        snapshot = await self.transport.get_topic(
            peer_id=self.peer_id, topic_id=topic_id
        )
        candidate = (
            self.catalog.by_document_id(snapshot.icon_emoji_id)
            if snapshot.icon_emoji_id is not None
            else None
        )
        state = await self._state_row(
            chat_id=self.state_chat_id, thread_id=str(topic_id)
        )
        if snapshot.icon_emoji_id is None:
            classification = "default"
        elif candidate is None:
            classification = "unknown"
        elif (
            isinstance(state, dict)
            and state.get("ownership") == "auto"
            and str(state.get("custom_emoji_id") or "")
            == str(snapshot.icon_emoji_id)
        ):
            classification = "custom_auto"
        else:
            classification = "custom_manual"
        return {
            "schema_version": 1,
            "peer_id": self.peer_id,
            "topic_id": topic_id,
            "title": snapshot.title,
            "icon_emoji_id": snapshot.icon_emoji_id,
            "state": classification,
            "candidate": candidate.to_dict() if candidate is not None else None,
        }

    async def set(
        self, topic_id: int, emoji: str, *, pack: str | None = None
    ) -> dict[str, Any]:
        candidate = self.catalog.resolve(emoji, pack=pack)
        if candidate is None:
            raise TopicIconCatalogError(
                "Unicode selector does not resolve in the configured custom packs"
            )
        await self.transport.get_topic(peer_id=self.peer_id, topic_id=topic_id)
        receipt = await self.transport.set_topic_icon(
            peer_id=self.peer_id,
            topic_id=topic_id,
            icon_emoji_id=candidate.custom_emoji_id,
        )
        if self.state is not None:
            recorder = getattr(
                self.state, "record_telegram_topic_icon_observation", None
            )
            if callable(recorder):
                await _await(
                    recorder(
                        chat_id=self.state_chat_id,
                        thread_id=str(topic_id),
                        custom_emoji_id=str(receipt.observed_icon_emoji_id),
                    )
                )
        return {
            "schema_version": 1,
            "operation": "topic.icon.write",
            "peer_id": receipt.peer_id,
            "topic_id": receipt.topic_id,
            "requested_icon_emoji_id": receipt.requested_icon_emoji_id,
            "observed_icon_emoji_id": receipt.observed_icon_emoji_id,
            "verified_at": receipt.verified_at,
            "result": "verified",
        }

    async def apply_automatic(
        self,
        *,
        topic_id: int,
        candidate: TopicIconCandidate,
        chat_id: str,
        thread_id: str,
        preserve_manual: bool = True,
        revalidate: Callable[[], Any] | None = None,
        expected_icon_emoji_id: int | None | object = _UNSET_ICON,
    ) -> dict[str, Any]:
        before = await self.transport.get_topic(
            peer_id=self.peer_id, topic_id=topic_id
        )
        if (
            expected_icon_emoji_id is not _UNSET_ICON
            and before.icon_emoji_id != expected_icon_emoji_id
        ):
            return {"schema_version": 1, "result": "icon_changed"}
        state = await self._state_row(chat_id=chat_id, thread_id=thread_id)
        known_auto = (
            isinstance(state, dict)
            and state.get("ownership") == "auto"
            and str(state.get("custom_emoji_id") or "")
            == str(before.icon_emoji_id or "")
        )
        if preserve_manual and before.icon_emoji_id is not None and not known_auto:
            return {"schema_version": 1, "result": "preserved_manual"}
        if revalidate is not None and not bool(await _await(revalidate())):
            return {"schema_version": 1, "result": "binding_changed"}
        latest = await self.transport.get_topic(
            peer_id=self.peer_id, topic_id=topic_id
        )
        if latest.icon_emoji_id != before.icon_emoji_id:
            return {"schema_version": 1, "result": "icon_changed"}
        receipt = await self.transport.set_topic_icon(
            peer_id=self.peer_id,
            topic_id=topic_id,
            icon_emoji_id=candidate.custom_emoji_id,
            expected_icon_emoji_id=latest.icon_emoji_id,
        )
        if self.state is not None:
            marker = getattr(self.state, "mark_telegram_topic_icon_auto", None)
            history = getattr(
                self.state, "record_telegram_topic_icon_selection", None
            )
            if callable(marker):
                await _await(
                    marker(
                        chat_id=chat_id,
                        thread_id=thread_id,
                        custom_emoji_id=str(receipt.observed_icon_emoji_id),
                    )
                )
            if callable(history):
                await _await(
                    history(
                        chat_id=chat_id,
                        custom_emoji_id=str(receipt.observed_icon_emoji_id),
                        emoji=candidate.emoji,
                        limit=24,
                    )
                )
        return {
            "schema_version": 1,
            "result": "verified",
            "requested_icon_emoji_id": receipt.requested_icon_emoji_id,
            "observed_icon_emoji_id": receipt.observed_icon_emoji_id,
        }


def build_profile_topic_icon_service():
    """Build the active profile's live service without touching it yet."""
    from plugins.platforms.telegram.topic_icon_runtime import ProfileTopicIconService

    return ProfileTopicIconService()

"""Custom Telegram sticker-pack catalog and Unicode-first resolver."""

from __future__ import annotations

import builtins
import hashlib
import inspect
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Literal


_PACK_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class TopicIconCatalogError(RuntimeError):
    pass


def normalize_emoji_selector(value: str) -> str:
    """Remove text/emoji presentation selectors while preserving sequences."""
    return str(value or "").strip().replace("\ufe0e", "").replace("\ufe0f", "")


@dataclass(frozen=True)
class TopicIconCandidate:
    emoji: str
    custom_emoji_id: int
    pack: str
    pack_title: str
    source: Literal["custom_pack"] = "custom_pack"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CustomTopicIconCatalog:
    def __init__(
        self,
        pack_order: Iterable[str],
        *,
        ttl_seconds: float = 86400,
        initial_rows: Iterable[TopicIconCandidate] = (),
        clock=time.time,
    ) -> None:
        packs = tuple(str(pack).strip() for pack in pack_order)
        if any(not _PACK_NAME.fullmatch(pack) for pack in packs):
            raise TopicIconCatalogError("custom pack names must be nonempty short names")
        if len(set(packs)) != len(packs):
            raise TopicIconCatalogError("duplicate custom pack names are not allowed")
        self.pack_order = packs
        self.ttl_seconds = max(1.0, min(float(ttl_seconds), 604800.0))
        self._rows = tuple(initial_rows)
        self._loaded_at: float | None = None
        self._clock = clock

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[dict[str, Any]],
        *,
        pack_order: Iterable[str],
    ) -> "CustomTopicIconCatalog":
        parsed = []
        for row in rows:
            parsed.append(
                TopicIconCandidate(
                    emoji=str(row["emoji"]),
                    custom_emoji_id=int(row["custom_emoji_id"]),
                    pack=str(row["pack"]),
                    pack_title=str(row["pack_title"]),
                )
            )
        return cls(pack_order, initial_rows=parsed)

    @property
    def rows(self) -> tuple[TopicIconCandidate, ...]:
        return self._rows

    @property
    def age_seconds(self) -> float | None:
        if self._loaded_at is None:
            return None
        return max(0.0, self._clock() - self._loaded_at)

    @property
    def fresh(self) -> bool:
        age = self.age_seconds
        return age is not None and age <= self.ttl_seconds

    async def refresh(self, bot: Any) -> tuple[TopicIconCandidate, ...]:
        getter = getattr(bot, "get_sticker_set", None) or getattr(
            bot, "getStickerSet", None
        )
        if not callable(getter):
            raise TopicIconCatalogError("Bot API getStickerSet is unavailable")

        pending: list[TopicIconCandidate] = []
        seen_ids: set[int] = set()
        for pack in self.pack_order:
            try:
                result = getter(pack)
                sticker_set = await result if inspect.isawaitable(result) else result
            except Exception as exc:
                raise TopicIconCatalogError(f"custom pack {pack} could not be loaded") from exc
            if str(getattr(sticker_set, "name", "") or "") != pack:
                raise TopicIconCatalogError(f"custom pack {pack} identity mismatch")
            title = str(getattr(sticker_set, "title", "") or "").strip()
            if not title:
                raise TopicIconCatalogError(f"custom pack {pack} has no title")
            stickers = getattr(sticker_set, "stickers", None)
            if not isinstance(stickers, (list, tuple)):
                raise TopicIconCatalogError(f"custom pack {pack} has malformed stickers")
            for sticker in stickers:
                emoji = str(getattr(sticker, "emoji", "") or "").strip()
                raw_id = getattr(sticker, "custom_emoji_id", None)
                if raw_id is None:
                    continue
                try:
                    document_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if not normalize_emoji_selector(emoji) or document_id <= 0:
                    continue
                if document_id in seen_ids:
                    continue
                seen_ids.add(document_id)
                pending.append(
                    TopicIconCandidate(
                        emoji=emoji,
                        custom_emoji_id=document_id,
                        pack=pack,
                        pack_title=title,
                    )
                )

        # Atomic publication only after every configured pack validated.
        self._rows = tuple(pending)
        self._loaded_at = self._clock()
        return self._rows

    def resolve(
        self, selector: str, *, pack: str | None = None
    ) -> TopicIconCandidate | None:
        key = normalize_emoji_selector(selector)
        if not key:
            return None
        if pack is not None and pack not in self.pack_order:
            return None
        rank = {name: index for index, name in enumerate(self.pack_order)}
        matches = [
            row
            for row in self._rows
            if normalize_emoji_selector(row.emoji) == key
            and (pack is None or row.pack == pack)
        ]
        if not matches:
            return None
        return min(
            matches,
            key=lambda row: (rank.get(row.pack, len(rank)), row.custom_emoji_id),
        )

    def by_document_id(self, document_id: int) -> TopicIconCandidate | None:
        return next(
            (row for row in self._rows if row.custom_emoji_id == document_id),
            None,
        )

    def list(
        self,
        *,
        pack: str | None = None,
        emoji: str | None = None,
        limit: int = 100,
    ) -> builtins.list[TopicIconCandidate]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise TopicIconCatalogError("catalog limit must be between 1 and 1000")
        key = normalize_emoji_selector(emoji) if emoji else None
        return [
            row
            for row in self._rows
            if (pack is None or row.pack == pack)
            and (key is None or normalize_emoji_selector(row.emoji) == key)
        ][:limit]


def choose_custom_topic_icon_candidate(
    catalog: CustomTopicIconCatalog,
    *,
    title: str,
    user_message: str,
    recent_document_ids: Iterable[int] = (),
    current_document_id: int | None = None,
    selector=None,
    instructions: str = "",
    preferred_route: dict[str, str] | None = None,
) -> TopicIconCandidate | None:
    """Choose through Unicode while returning an exact custom-pack document."""
    available = [
        row
        for row in catalog.rows
        if current_document_id is None or row.custom_emoji_id != current_document_id
    ]
    if not available:
        return None
    recent_ids = {int(value) for value in recent_document_ids}
    fresh = [row for row in available if row.custom_emoji_id not in recent_ids]
    pool = fresh or available
    pack_rank = {name: index for index, name in enumerate(catalog.pack_order)}

    by_selector: dict[str, list[TopicIconCandidate]] = {}
    for row in pool:
        by_selector.setdefault(normalize_emoji_selector(row.emoji), []).append(row)
    allowed = list(by_selector)
    if len(allowed) > 96:
        from agent.title_generator import choose_topic_icon_deterministic

        primary = choose_topic_icon_deterministic(title, user_message, allowed)
        context = f"{title}\n{user_message}".encode("utf-8", errors="ignore")
        ranked = sorted(
            (emoji for emoji in allowed if emoji != primary),
            key=lambda emoji: hashlib.sha256(
                context + b"\0" + emoji.encode("utf-8", errors="ignore")
            ).digest(),
        )
        allowed = ([primary] if primary else []) + ranked[: 96 - bool(primary)]
    if not allowed:
        return None
    if selector is None:
        from agent.title_generator import choose_topic_icon

        selector = choose_topic_icon
    kwargs: dict[str, Any] = {}
    if instructions:
        kwargs["instructions"] = instructions
    if preferred_route:
        kwargs["preferred_route"] = preferred_route
    selected_unicode = selector(title, user_message, allowed, **kwargs)
    selected_key = normalize_emoji_selector(selected_unicode or "")
    matches = by_selector.get(selected_key)
    if not matches:
        from agent.title_generator import choose_topic_icon_deterministic

        fallback = choose_topic_icon_deterministic(title, user_message, allowed)
        matches = by_selector.get(normalize_emoji_selector(fallback or ""))
    if not matches:
        return None
    return min(
        matches,
        key=lambda row: (
            pack_rank.get(row.pack, len(pack_rank)),
            row.custom_emoji_id,
        ),
    )

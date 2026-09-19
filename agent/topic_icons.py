"""Small, deterministic helpers for Telegram forum-topic icons."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Mapping, Optional


def _emoji_value(item: Any) -> str:
    return str(item.get("emoji", "") if isinstance(item, Mapping) else item).strip()


def normalize_emoji(value: Any) -> str:
    """Normalize Telegram's optional variation selector for comparisons."""
    return _emoji_value(value).replace("\ufe0f", "")


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[\w]+", str(text or ""), flags=re.UNICODE) if len(token) > 1}


def fresh_allowed_icons(allowed_emojis: Iterable[Any], recent_emojis: Iterable[Any] | None) -> list[str]:
    """Allowed emojis not in recent history, unless that leaves fewer than 8 (then the full list)."""
    allowed = [_emoji_value(item) for item in allowed_emojis if _emoji_value(item)]
    recent = {normalize_emoji(item) for item in (recent_emojis or [])}
    fresh = [item for item in allowed if normalize_emoji(item) not in recent]
    return fresh if len(fresh) >= 8 else allowed


def choose_topic_icon_deterministic(
    title: str, user_message: str, allowed_emojis: list[Any], recent_emojis: Optional[list[Any]] = None,
) -> Optional[str]:
    """Choose a stable semantic candidate, rotating away from recent icons when possible."""
    candidates = fresh_allowed_icons(allowed_emojis, recent_emojis)
    if not candidates:
        return None
    signal = _tokens(title) | _tokens(user_message)
    semantic_hints = {
        "🐛": {"bug", "error", "fix", "debug", "issue", "test"}, "💻": {"code", "coding", "software", "app"},
        "📊": {"data", "metric", "report", "analytics", "chart"}, "💡": {"idea", "plan", "design"},
        "🔒": {"auth", "security", "password", "login"}, "📚": {"learn", "study", "research", "docs"},
        "🚀": {"deploy", "release", "launch", "ship"}, "✈️": {"travel", "flight", "trip"},
        "🛒": {"buy", "shopping", "order", "product"}, "💰": {"money", "finance", "budget", "tax"},
    }
    scored = []
    for index, emoji in enumerate(candidates):
        hints = _tokens(emoji) | semantic_hints.get(emoji, set())
        score = len(signal & hints)
        scored.append((score, -index, emoji))
    if any(score for score, _, _ in scored):
        return max(scored)[2]
    recent = [normalize_emoji(item) for item in (recent_emojis or [])]
    for emoji in candidates:
        if normalize_emoji(emoji) not in recent:
            return emoji
    return candidates[0]


def resolve_override(title: str, overrides: Mapping[str, Any] | None, allowed: list[Any]) -> Optional[str]:
    """Resolve exact or case-insensitive title-substring overrides against the allow-list."""
    if not isinstance(overrides, Mapping):
        return None
    allowed_map = {normalize_emoji(item): _emoji_value(item) for item in allowed if _emoji_value(item)}
    lowered = str(title or "").casefold()
    matches = []
    for key, value in overrides.items():
        if str(key).casefold() in lowered:
            candidate = allowed_map.get(normalize_emoji(value))
            if candidate:
                matches.append((len(str(key)), candidate))
    return max(matches, default=(0, None))[1]


def validate_model_icon(value: Any, allowed: list[Any]) -> Optional[str]:
    """Return the canonical allowed emoji for a model proposal, or None."""
    normalized = normalize_emoji(value)
    for item in allowed:
        if normalized and normalized == normalize_emoji(item):
            return _emoji_value(item)
    return None

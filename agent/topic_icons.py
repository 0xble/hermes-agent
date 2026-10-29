"""Small, deterministic helpers for Telegram forum-topic icons."""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

# Default model guidance when the operator sets none. Relevance beats variety: excluding recently
# used icons from the choice pushed picks toward unrelated ones once the obvious icon was taken.
# Naming the generic icons keeps a small title model from defaulting to 💻 for every technical topic.
DEFAULT_ICON_GUIDANCE = (
    "Pick the icon that most specifically depicts the topic's subject, the one a reader would "
    "recognize at a glance. Choose a specific icon over a generic one such as 💻, 🤖 or 💬 "
    "whenever one fits. Reusing an icon another topic already has is fine."
)

# Keyword hints for the fallback chooser. Keys are icons from Telegram's native topic-icon catalog
# (getForumTopicIconStickers); a key outside that catalog can never be selected.
_SEMANTIC_HINTS: dict[str, frozenset[str]] = {
    emoji: frozenset(words.split())
    for emoji, words in {
        "💻": "code coding software app cli repo repos api plugin script scripts sdk deploy github",
        "🤖": "agent agents hermes bot bots model models llm ai gpt claude codex subagent",
        "🧪": "test tests testing eval evals experiment benchmark qa",
        "🔎": "investigate investigation search research find audit why diagnose",
        "📚": "docs documentation wiki wikis knowledge learn study skill skills guide",
        "📝": "note notes draft write writing form register registration checklist",
        "📆": "cron crons schedule scheduled calendar timezone meeting meetings deadline",
        "💰": "money finance budget tax taxes price pricing cost costs expense expenses savings",
        "💸": "payment payments statement card billing spend refund invoice",
        "📈": "growth metrics analytics report reports data dashboard stats",
        "💬": "message messages chat reply slack telegram sms imessage whatsapp",
        "🏠": "home house apartment rent landlord",
        "✈": "travel flight flights trip hotel",
        "🛒": "buy shopping order orders amazon product purchase",
        "🔥": "bug bugs error errors fix broken incident outage crash failure debug",
        "⚡": "performance speed fast slow latency optimize",
        "🧠": "memory memories recall remember hindsight",
        "🩺": "health doctor medical fitness",
        "🎨": "design ui ux logo brand rebrand",
        "📱": "iphone phone mobile ios android",
        "🪪": "identity login logins account accounts auth password oauth credential credentials",
        "💼": "business client clients company",
        "💡": "idea ideas plan proposal brainstorm",
    }.items()
}


def _emoji_value(item: Any) -> str:
    return str(item.get("emoji", "") if isinstance(item, Mapping) else item).strip()


def normalize_emoji(value: Any) -> str:
    """Normalize Telegram's optional variation selector for comparisons."""
    return _emoji_value(value).replace("\ufe0f", "")


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[\w]+", str(text or ""), flags=re.UNICODE) if len(token) > 1}


def allowed_icon_list(allowed_emojis: Any) -> list[str]:
    """The allowed emoji strings, in catalog order."""
    return [value for value in (_emoji_value(item) for item in allowed_emojis or []) if value]


def choose_topic_icon_deterministic(
    title: str, user_message: str, allowed_emojis: list[Any], recent_emojis: Optional[list[Any]] = None,
) -> Optional[str]:
    """Best keyword match, or None when nothing matches so the topic keeps its current icon.

    Recent icons only break ties between equally good matches; they never displace a better one.
    """
    signal = _tokens(title) | _tokens(user_message)
    recent = {normalize_emoji(item) for item in (recent_emojis or [])}
    best: Optional[tuple[int, int, int]] = None
    choice: Optional[str] = None
    for index, emoji in enumerate(allowed_icon_list(allowed_emojis)):
        score = len(signal & _SEMANTIC_HINTS.get(normalize_emoji(emoji), frozenset()))
        if not score:
            continue
        rank = (score, int(normalize_emoji(emoji) not in recent), -index)
        if best is None or rank > best:
            best, choice = rank, emoji
    return choice


# How many of the chat's most recent automatic icons a ranked model pick skips. Short on purpose:
# it only has to break streaks, and every fallback is still one of the model's own fitting picks.
ICON_COOLDOWN = 5
MAX_RANKED_ICONS = 3


def validate_ranked_icons(values: Any, allowed: list[Any]) -> list[str]:
    """The model's ranked proposals that are in the catalog, best first, deduplicated, at most three."""
    if isinstance(values, (str, Mapping)):
        values = [values]
    ranked: list[str] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        icon = validate_model_icon(value, allowed)
        if icon and normalize_emoji(icon) not in {normalize_emoji(item) for item in ranked}:
            ranked.append(icon)
        if len(ranked) == MAX_RANKED_ICONS:
            break
    return ranked


def pick_ranked_icon(ranked: list[str], recent_emojis: Optional[list[Any]] = None) -> Optional[str]:
    """The best-ranked icon outside the cooldown window; the top pick when every candidate is recent.

    A lone candidate always wins, so a model that sees only one fitting icon is never overruled.
    """
    if not ranked:
        return None
    cooling = {normalize_emoji(item) for item in (recent_emojis or [])[:ICON_COOLDOWN]}
    return next((icon for icon in ranked if normalize_emoji(icon) not in cooling), ranked[0])


def resolve_override(title: str, overrides: Mapping[str, Any] | None, allowed: list[Any]) -> Optional[str]:
    """Resolve exact or case-insensitive title-substring overrides against the allow-list."""
    if not isinstance(overrides, Mapping):
        return None
    allowed_map = {normalize_emoji(item): _emoji_value(item) for item in allowed if _emoji_value(item)}
    lowered = str(title or "").casefold()
    matches = []
    for key, value in overrides.items():
        if str(key).strip() and str(key).casefold() in lowered:
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

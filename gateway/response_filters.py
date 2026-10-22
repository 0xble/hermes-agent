"""Gateway response filtering helpers.

These decide whether a completed agent turn should be delivered to the chat,
not what should be persisted in conversation history.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

# Edge headers that mark an inbound message as sent by another agent session, not typed by the
# user. Such a message may end in a bare silence marker. Relay's own parser uses the same value
# class (``[^\s\]]+``), so both sides agree on what a header is.
_AGENT_ORIGIN_HEADERS = (
    re.compile(r"\[relay from=[^\s\]]+ receipt=[^\s\]]+(?: task=[^\s\]]+)?\]"),
)
_RELAY_HEADER_RE = re.compile(
    r"\[relay from=[^\s\]]+ receipt=[^\s\]]+(?P<task> task=[^\s\]]+)?\]"
)
_RELAY_FYI_FOOTER_RE = re.compile(r"From: .+ · FYI, no action requested")

# Relay uses gomoji + Unicode's Emoji_Presentation property over uniseg graphemes. Keep the
# equivalent small, dependency-free classifier here: the gateway cannot import relay's Go package,
# and accepting a bare text-default pictograph would turn ordinary prose into a relay title.
_TEXT_DEFAULT_EMOJI_BASES = frozenset(
    {
        *range(0x00A9, 0x00AA), *range(0x00AE, 0x00AF), 0x203C, 0x2049, 0x2122, 0x2139,
        *range(0x2194, 0x219A), 0x21A9, 0x21AA, 0x231A, 0x231B, 0x2328, 0x23CF,
        *range(0x23E9, 0x23F4), *range(0x23F8, 0x23FB), 0x24C2, 0x25AA, 0x25AB,
        0x25B6, 0x25C0, *range(0x25FB, 0x25FF), *range(0x2600, 0x27C0),
        0x2934, 0x2935, *range(0x2B05, 0x2B08), 0x2B1B, 0x2B1C, 0x2B50, 0x2B55,
        0x3030, 0x303D, 0x3297, 0x3299,
    }
)
_STATUS_DOTS = frozenset("🔴🟠🟡🟢🔵🟣⚫⚪🟤")


def _is_regional_indicator(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


# Unicode Emoji_Presentation (Emoji 18.0), copied range for range from relay's generated
# internal/relay/emoji_presentation.go so both sides classify titles identically.
_EMOJI_PRESENTATION_RANGES = (
    (0x231A, 0x231B), (0x23E9, 0x23EC), (0x23F0, 0x23F0), (0x23F3, 0x23F3), (0x25FD, 0x25FE), (0x2614, 0x2615),
    (0x2648, 0x2653), (0x267F, 0x267F), (0x2693, 0x2693), (0x26A1, 0x26A1), (0x26AA, 0x26AB), (0x26BD, 0x26BE),
    (0x26C4, 0x26C5), (0x26CE, 0x26CE), (0x26D4, 0x26D4), (0x26EA, 0x26EA), (0x26F2, 0x26F3), (0x26F5, 0x26F5),
    (0x26FA, 0x26FA), (0x26FD, 0x26FD), (0x2705, 0x2705), (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C),
    (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757), (0x2795, 0x2797), (0x27B0, 0x27B0), (0x27BF, 0x27BF),
    (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x1F004, 0x1F004), (0x1F0CF, 0x1F0CF), (0x1F18E, 0x1F18E),
    (0x1F191, 0x1F19A), (0x1F1E6, 0x1F1FF), (0x1F201, 0x1F201), (0x1F21A, 0x1F21A), (0x1F22F, 0x1F22F), (0x1F232, 0x1F236),
    (0x1F238, 0x1F23A), (0x1F250, 0x1F251), (0x1F300, 0x1F320), (0x1F32D, 0x1F335), (0x1F337, 0x1F37C), (0x1F37E, 0x1F393),
    (0x1F3A0, 0x1F3CA), (0x1F3CF, 0x1F3D3), (0x1F3E0, 0x1F3F0), (0x1F3F4, 0x1F3F4), (0x1F3F8, 0x1F43E), (0x1F440, 0x1F440),
    (0x1F442, 0x1F4FC), (0x1F4FF, 0x1F53D), (0x1F54B, 0x1F54E), (0x1F550, 0x1F567), (0x1F57A, 0x1F57A), (0x1F595, 0x1F596),
    (0x1F5A4, 0x1F5A4), (0x1F5FB, 0x1F64F), (0x1F680, 0x1F6C5), (0x1F6CC, 0x1F6CC), (0x1F6D0, 0x1F6D2), (0x1F6D5, 0x1F6D9),
    (0x1F6DC, 0x1F6DF), (0x1F6EB, 0x1F6EC), (0x1F6F4, 0x1F6FC), (0x1F7E0, 0x1F7EB), (0x1F7F0, 0x1F7F0), (0x1F90C, 0x1F93A),
    (0x1F93C, 0x1F945), (0x1F947, 0x1F9FF), (0x1FA70, 0x1FA7C), (0x1FA80, 0x1FAC6), (0x1FAC8, 0x1FAC8), (0x1FACC, 0x1FADD),
    (0x1FADF, 0x1FAEB), (0x1FAEF, 0x1FAFA),
)


def _is_emoji_presentation(char: str) -> bool:
    codepoint = ord(char)
    return any(low <= codepoint <= high for low, high in _EMOJI_PRESENTATION_RANGES)


def _is_keycap_cluster(cluster: str) -> bool:
    codepoints = [ord(char) for char in cluster]
    if len(codepoints) == 2:
        return (48 <= codepoints[0] <= 57 or codepoints[0] in {35, 42}) and codepoints[1] == 0x20E3
    return (
        len(codepoints) == 3
        and (48 <= codepoints[0] <= 57 or codepoints[0] in {35, 42})
        and codepoints[1] == 0xFE0F
        and codepoints[2] == 0x20E3
    )


def _grapheme_clusters(text: str) -> list[str]:
    """Segment the emoji/title subset of Unicode graphemes used by relay's title validator."""
    clusters: list[str] = []
    index = 0
    while index < len(text):
        start = index
        codepoint = ord(text[index])
        index += 1
        if _is_regional_indicator(text[start]) and index < len(text) and _is_regional_indicator(text[index]):
            index += 1
        elif (48 <= codepoint <= 57 or codepoint in {35, 42}) and index < len(text):
            if text[index] == "\ufe0f":
                index += 1
            if index < len(text) and text[index] == "\u20e3":
                index += 1
        while index < len(text):
            category = unicodedata.category(text[index])
            if text[index] in {"\ufe0e", "\ufe0f"} or category in {"Mn", "Mc", "Sk"}:
                index += 1
                continue
            if text[index] != "\u200d":
                break
            # ZWJ + next base + its variation/modifier marks form one relay grapheme.
            if index + 1 >= len(text):
                break
            index += 2
            while index < len(text):
                category = unicodedata.category(text[index])
                if text[index] in {"\ufe0e", "\ufe0f"} or category in {"Mn", "Mc", "Sk"}:
                    index += 1
                else:
                    break
        clusters.append(text[start:index])
    return clusters


def _gomoji_contains_emoji(cluster: str) -> bool:
    codepoints = [ord(char) for char in cluster]
    if _is_keycap_cluster(cluster):
        return True
    if len(codepoints) == 2 and all(0x1F1E6 <= codepoint <= 0x1F1FF for codepoint in codepoints):
        return True
    return any(_is_emoji_presentation(char) or ord(char) in _TEXT_DEFAULT_EMOJI_BASES for char in cluster)


def _is_single_emoji(token: str) -> bool:
    """Mirror relay's titleEmoji for one complete emoji grapheme."""
    if not token or _grapheme_clusters(token) != [token] or not _gomoji_contains_emoji(token):
        return False
    if token in _STATUS_DOTS:
        return False
    codepoints = [ord(char) for char in token]
    if len(codepoints) == 1 and _is_regional_indicator(token):
        return False
    if "\ufe0f" in token or "\u200d" in token or _is_keycap_cluster(token):
        return True
    if len(codepoints) == 2 and all(0x1F1E6 <= codepoint <= 0x1F1FF for codepoint in codepoints):
        return True
    return any(not _is_regional_indicator(char) and _is_emoji_presentation(char) for char in token)


def _is_relay_title(line: str) -> bool:
    clusters = _grapheme_clusters(line)
    if not clusters or not _is_single_emoji(clusters[0]):
        return False
    emoji = clusters[0]
    rest = line[len(emoji):]
    if not rest.startswith(" ") or not rest[1:].strip():
        return False
    return not any(_is_single_emoji(cluster) for cluster in _grapheme_clusters(rest[1:]))


def is_agent_origin_text(text: Any) -> bool:
    """True when the first or last non-blank line is a complete agent-origin header."""
    if not isinstance(text, str):
        return False
    non_blank_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not non_blank_lines:
        return False
    edge_lines = (non_blank_lines[0], non_blank_lines[-1])
    return any(
        pattern.fullmatch(line)
        for line in edge_lines
        for pattern in _AGENT_ORIGIN_HEADERS
    )


def is_fyi_relay_text(text: Any) -> bool:
    """True only for one complete, unaddressed relay FYI envelope."""
    if not isinstance(text, str) or not text:
        return False
    raw_lines = text.splitlines()
    while raw_lines and not raw_lines[-1].strip():
        raw_lines.pop()
    if not raw_lines or not raw_lines[0].strip():
        return False
    lines = [line.strip() for line in raw_lines]
    header_indices = [index for index, line in enumerate(lines) if _RELAY_HEADER_RE.fullmatch(line)]
    if len(header_indices) != 1 or header_indices[0] != len(lines) - 1:
        return False
    header = _RELAY_HEADER_RE.fullmatch(lines[-1])
    if header is None or header.group("task"):
        return False
    footer_indices = [index for index, line in enumerate(lines) if _RELAY_FYI_FOOTER_RE.fullmatch(line)]
    if len(footer_indices) != 1 or footer_indices[0] != len(lines) - 2:
        return False
    if not _is_relay_title(lines[0]):
        return False
    body = lines[1:-2]
    # Relay's plainMessage emits the parsed brief/details with blank separators, but never emits
    # its input `---` separator. Require content, while intentionally accepting any rendered shape.
    return any(line for line in body)


def is_passive_fyi_relay_event(event: Any, source: Any, *, enabled: bool) -> bool:
    """Shared passive-admission predicate for idle sessions."""
    return bool(
        enabled
        and getattr(getattr(source, "platform", None), "value", None) == "telegram"
        and getattr(getattr(event, "message_type", None), "value", None) == "text"
        and not getattr(event, "internal", False)
        and getattr(event, "reply_expected", None) is not True
        and is_fyi_relay_text(getattr(event, "text", None))
    )


def apply_agent_origin_reply_expectation(event: Any) -> Any:
    """Mark an agent-origin event as not needing a reply; an explicit True is never overridden."""
    if getattr(event, "reply_expected", None) is not True and is_agent_origin_text(getattr(event, "text", "")):
        event.reply_expected = False
    return event


# Exact whole-response markers meaning "the agent intentionally chose not to
# reply". Keep small and explicit; arbitrary empty output remains an
# error/empty-response path, not silence. A lane that does not think in English
# translates the sentinel rather than dropping it, and the whole control token
# then reaches the user as content, so the translated forms are carried here
# too. zh-Hans is the only non-English locale this project ships documentation
# for, which is where the list stops.
LIVE_GATEWAY_SILENT_MARKERS = frozenset({
    "[SILENT]", "SILENT", "NO_REPLY", "NO REPLY",
    "[静默]", "静默", "[沉默]", "沉默",
})

# Bracketed markers drive the autonomous lane's prefix rule ("[SILENT] nothing
# new this tick"). Derived from the set above so a new marker cannot be added to
# one rule and forgotten in the other.
_BRACKETED_SILENCE_MARKERS = tuple(
    sorted(m for m in LIVE_GATEWAY_SILENT_MARKERS if m.startswith("["))
)

# The persisted user-row kind of a self-injected MessageEvent(internal=True) turn; only these
# machinery kinds may vanish on a bare silence marker.
INTERNAL_NOTIFICATION_DISPLAY_KIND = "internal_notification"
# Async-result cards the in-process TUI persisted: a producer's internal event naming one (with a
# display_text) persists as that row kind instead of internal_notification.
PRODUCER_NOTICE_DISPLAY_KINDS = frozenset({"process_complete", "async_delegation_complete"})
MACHINERY_DISPLAY_KINDS = frozenset({INTERNAL_NOTIFICATION_DISPLAY_KIND, *PRODUCER_NOTICE_DISPLAY_KINDS})
# What a gateway producer's card may name (a liveness tick names internal_notification for its CLI
# receipt). Closed, and kept apart from the silence set above so a kind added there never becomes
# nameable; never a client-chosen presentation such as ``hidden``.
PRODUCER_CARD_KINDS = frozenset({INTERNAL_NOTIFICATION_DISPLAY_KIND, *PRODUCER_NOTICE_DISPLAY_KINDS})

# Longer than any marker could plausibly be, even with stray punctuation.
_MARKER_LENGTH_CAP = 64
_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})(?:.*)?$")


def _canonical_silence_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def _is_edge_punctuation(ch: str) -> bool:
    # Square brackets stay structural so malformed ``[SILENT`` cannot become ``SILENT``.
    return ch not in "[]" and unicodedata.category(ch).startswith("P")


def _strip_edge_silence_punctuation(text: str) -> str:
    """Strip stray edge punctuation (``.NO_REPLY``, ``*NO_REPLY*``) without erasing marker structure."""
    start, end = 0, len(text)
    while start < end and _is_edge_punctuation(text[start]):
        start += 1
    while end > start and _is_edge_punctuation(text[end - 1]):
        end -= 1
    return text[start:end].strip()


def _canonical_silence_candidates(text: Any) -> tuple[str, ...]:
    """Canonical forms of a short marker-sized response; ``()`` when not a candidate at all."""
    stripped = text.strip() if isinstance(text, str) else ""
    if not 0 < len(stripped) <= _MARKER_LENGTH_CAP:
        return ()
    depunctuated = _strip_edge_silence_punctuation(stripped)
    forms = (stripped,) if depunctuated == stripped else (stripped, depunctuated)
    return tuple(_canonical_silence_candidate(f) for f in forms)


def is_intentional_silence_response(response: Any) -> bool:
    """True only when ``response`` is exactly a silence marker.

    Prose that merely mentions ``NO_REPLY`` must be delivered normally. A blank
    response is not silence either — that is the empty-response failure path.
    """
    return any(c in LIVE_GATEWAY_SILENT_MARKERS for c in _canonical_silence_candidates(response))


def _fenced_line_states(lines: list[str]) -> list[bool]:
    """Return whether each line starts inside a Markdown fenced code block."""
    states: list[bool] = []
    fence_char: str | None = None
    fence_len = 0
    for line in lines:
        states.append(fence_char is not None)
        match = _FENCE_LINE_RE.match(line.rstrip("\r\n"))
        if not match:
            continue
        fence = match.group(1)
        if fence_char is not None:
            if fence[0] == fence_char and len(fence) >= fence_len:
                fence_char = None
                fence_len = 0
        else:
            fence_char = fence[0]
            fence_len = len(fence)
    return states


def _strip_trailing_marker_lines(
    text: Any, is_marker, *, respect_fences: bool = True, preserve_bare: bool = False,
) -> Any:
    """Strip consecutive top-level marker lines from the end of *text*.

    ``preserve_bare`` retains the last marker when removing it would otherwise make the
    whole response empty. Silence uses that behavior so its existing exact-marker path
    remains authoritative; display-only control markers do not.
    """
    if not isinstance(text, str):
        return text
    lines = text.splitlines(keepends=True)
    if not lines:
        return text
    fenced = _fenced_line_states(lines) if respect_fences else [False] * len(lines)
    end = len(lines)
    last_marker = ""
    while end:
        while end and not lines[end - 1].strip():
            end -= 1
        if not end or fenced[end - 1] or not is_marker(lines[end - 1]):
            break
        last_marker = last_marker or lines[end - 1].strip()
        end -= 1
    if not last_marker:
        return text
    kept = "".join(lines[:end]).rstrip()
    return kept if kept.strip() or not preserve_bare else last_marker


def _strip_leading_marker_lines(text: str, is_marker) -> tuple[str, str]:
    """Strip consecutive marker lines (and blank lines between them) opening *text*.

    The first line can never sit inside a fence, and the scan stops at the first
    non-marker line, so a later fence cannot hide or expose anything here.
    Returns ``(rest, last_marker)``; ``last_marker`` is empty when nothing was removed.
    """
    lines = text.splitlines(keepends=True)
    start = 0
    last_marker = ""
    while start < len(lines):
        if not lines[start].strip():
            start += 1
            continue
        if not is_marker(lines[start]):
            break
        last_marker = lines[start].strip()
        start += 1
    if not last_marker:
        return text, ""
    return "".join(lines[start:]).lstrip("\r\n"), last_marker


def opens_with_silence_marker(text: Any) -> bool:
    """True when the first non-blank line of ``text`` is a standalone silence marker.

    Streaming previews hold while this is true, so a reply that opens
    ``NO_REPLY\\n\\n<prose>`` never shows (or push-notifies) the marker before the
    final strip removes it. The first line can never be inside a fence.
    """
    if not isinstance(text, str):
        return False
    return bool(_strip_leading_marker_lines(text, is_intentional_silence_response)[1])


def strip_edge_silence_markers(text: Any, *, respect_fences: bool = True) -> Any:
    """Remove top-level standalone silence-marker lines opening or ending substantive text.

    A model sometimes emits the marker *and* prose (``NO_REPLY\\n\\nUpdate: ...`` or
    ``Done.\\n\\nNO_REPLY``). The prose is the reply; the marker line is control text the
    user must never see. Consecutive marker runs at either edge are removed. The exact
    interactive silence rule remains authoritative for a bare marker, so a bare marker
    is returned unchanged, and a response made only of marker lines collapses to its
    last marker so it still reaches the intentional-silence path rather than the
    empty-response one. Marker-looking lines inside fenced code are content.
    ``respect_fences=False`` is only for raw text whose fence-aware visible form
    (think blocks removed) already proved the trailing lines are top-level.
    """
    if not isinstance(text, str) or is_intentional_silence_response(text):
        return text
    rest, leading_marker = _strip_leading_marker_lines(text, is_intentional_silence_response)
    if leading_marker and not rest.strip():
        return leading_marker
    return _strip_trailing_marker_lines(
        rest, is_intentional_silence_response,
        respect_fences=respect_fences, preserve_bare=True,
    )


_LOOP_COMPLETE_LINE_RE = re.compile(r"^\s*LOOP_COMPLETE\s*[.!]?\s*$", re.IGNORECASE)
_LOOP_COMPLETE_MARKER = "LOOP_COMPLETE"


def is_loop_complete_marker(text: Any) -> bool:
    """True when a line is the loop completion marker with optional punctuation."""
    return isinstance(text, str) and _LOOP_COMPLETE_LINE_RE.fullmatch(text) is not None


def strip_trailing_loop_complete_marker(text: Any, *, respect_fences: bool = True) -> Any:
    """Remove trailing top-level ``LOOP_COMPLETE`` lines for display only.

    A marker in a fenced code block or mid-prose is content. A marker-only reply
    becomes empty; loop detection must use the raw response instead.
    """
    return _strip_trailing_marker_lines(
        text, is_loop_complete_marker,
        respect_fences=respect_fences, preserve_bare=False,
    )


def split_trailing_loop_complete_marker(text: Any, *, context: str = "") -> tuple[Any, str]:
    """Split safe prefix from a trailing top-level marker candidate for streaming.

    The held tail is the trailing candidate line plus every complete top-level marker line
    (and blank line) directly before it, so a repeated marker never flashes on screen.
    ``context`` is the text already released this segment: fence state is judged over
    ``context + text`` (a fence opened in an earlier chunk may close in this one), but only
    ``text`` is ever split, since ``context`` is already on screen.
    """
    if not isinstance(text, str):
        return text, ""
    context = context if isinstance(context, str) else ""
    full = context + text
    if not ends_with_partial_loop_complete_marker(full):
        return text, ""
    lines = full.splitlines(keepends=True)
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    if not end:
        return text, ""
    fenced = _fenced_line_states(lines)
    start = end - 1
    probe = start
    while probe:
        while probe and not lines[probe - 1].strip():
            probe -= 1
        if not probe or fenced[probe - 1] or not is_loop_complete_marker(lines[probe - 1]):
            break
        probe -= 1
        start = probe
    cut = max(len("".join(lines[:start])) - len(context), 0)
    return text[:cut], text[cut:]


def ends_with_partial_loop_complete_marker(text: Any) -> bool:
    """True while the trailing top-level line could still become ``LOOP_COMPLETE``."""
    if not isinstance(text, str):
        return False
    lines = text.splitlines(keepends=True)
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    if not end or _fenced_line_states(lines)[end - 1]:
        return False
    candidate = lines[end - 1].strip().upper()
    if not candidate:
        return False
    if candidate == _LOOP_COMPLETE_MARKER or is_loop_complete_marker(lines[end - 1]):
        return True
    return _LOOP_COMPLETE_MARKER.startswith(candidate)


def ends_with_partial_silence_marker(text: Any) -> bool:
    """True while the last top-level line of streamed ``text`` could still be a marker.

    Mid-stream previews hold an edit while this is true, so prose followed by a
    trailing ``NO_REPLY`` never shows the marker before the final strip removes it.
    """
    if not isinstance(text, str):
        return False
    lines = text.splitlines(keepends=True)
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    if not end or _fenced_line_states(lines)[end - 1]:
        return False
    return is_partial_silence_marker(lines[end - 1])


def is_autonomous_silence_response(response: Any) -> bool:
    """Loose silence matcher for autonomous lanes (cron, webhook).

    Models reliably bracket ``[SILENT]`` with a short note, so unlike the
    interactive EXACT rule this also suppresses when a marker sits on its own
    first/last line or the bracketed sentinel opens the response (``[SILENT] No
    changes detected``).  A token buried mid-sentence is still delivered.
    Shares :data:`LIVE_GATEWAY_SILENT_MARKERS` so the two sets cannot drift.
    """
    stripped = response.strip() if isinstance(response, str) else ""
    if not stripped:
        return False
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    # Bracketed form only for the prefix rule, so a bare "Silent retry succeeded" is NOT swallowed.
    # Same de-punctuating forms as the interactive rule, so ``【静默】`` / ``静默。`` cannot
    # be suppressed in chat yet delivered by cron.
    return stripped.upper().startswith(_BRACKETED_SILENCE_MARKERS) or any(
        is_intentional_silence_response(c) for c in (stripped, lines[0], lines[-1])
    )


def is_intentional_silence_agent_result(agent_result: dict | None, response: Any) -> bool:
    """Silence markers suppress delivery only for successful agent turns."""
    return isinstance(agent_result, dict) and not agent_result.get("failed") and is_intentional_silence_response(response)


def display_kind_for_event(event: Any) -> str | None:
    """The persisted user-row kind for a gateway turn: only self-injected events are machinery.

    A scheduled heartbeat prompt is self-injected too (``_heartbeat_session_id`` is stamped only
    by the gateway poller, never inferred from inbound text), but it deliberately stays
    non-internal so authorization and the emergency stop still apply to it.
    """
    from gateway.session_display import admission_display
    if kind := admission_display(event).get("kind"):
        # A canonical admission committed hidden (Desktop widget intent): never a user bubble.
        return kind
    if getattr(event, "internal", False) and _producer_card(event):
        return event.metadata["display_kind"]
    if getattr(event, "internal", False) or getattr(event, "_heartbeat_session_id", None):
        return INTERNAL_NOTIFICATION_DISPLAY_KIND
    return None


def _producer_card(event: Any) -> dict:
    metadata = getattr(event, "metadata", None) or {}
    text = metadata.get("display_text")
    if (getattr(event, "internal", False) and metadata.get("display_kind") in PRODUCER_NOTICE_DISPLAY_KINDS
            and isinstance(text, str) and text.strip()):
        return {"display_text": text}
    return {}


def display_metadata_for_event(event: Any) -> dict:
    """``{"display_text": ...}`` for an internal event whose producer named an async-result card, plus the
    ``title_preview`` (titler-only) a canonical admission committed for this exact event."""
    from gateway.session_display import admission_display
    preview = admission_display(event).get("title_preview")
    return {**_producer_card(event), **({"title_preview": preview} if preview else {})}


def is_machinery_display_kind(display_kind: Any) -> bool:
    """Only a machinery turn may vanish on a bare silence marker; a human turn gets a visible fallback.

    The caller passes the current turn's persisted display kind instead of inferring it from the
    transcript: the inbound user row is not persisted yet, and a previous internal row must never
    authorize silence on a human turn.
    """
    return display_kind in MACHINERY_DISPLAY_KINDS


def silence_allowed(display_kind: Any, reply_expected: Optional[bool] = None) -> bool:
    """Whether a successful bare silence marker may remain silent for this turn."""
    return is_machinery_display_kind(display_kind) or reply_expected is False


def reply_expected_metadata(reply_expected: Optional[bool]) -> dict:
    """The persisted user row's ``reply_expected`` key, only when the adapter knew; crash recovery
    reads it back to judge a silence marker as the live turn did."""
    return {} if reply_expected is None else {"reply_expected": reply_expected}


def is_partial_silence_marker(text: Any) -> bool:
    """True while streamed ``text`` could still resolve to a silence marker.

    A buffer whose canonical form is a non-empty *prefix* of a marker (``"NO"`` on
    the way to ``"NO_REPLY"``, or an exact marker not yet terminated by stream-end)
    is held back so a raw marker is never shown and then retracted.  Divergence
    from every marker, or exceeding the cap, resumes normal streaming.
    """
    return any(
        c and any(marker.startswith(c) for marker in LIVE_GATEWAY_SILENT_MARKERS)
        for c in _canonical_silence_candidates(text)
    )


def hold_silence_delta(hold: dict, delta: str) -> str:
    """Streamed text safe to release now while a bare silence marker stays withheld.

    ``hold`` is per-turn state ``{"buf": "", "held": ""}``. The agent opens the first text after
    a tool round with ``"\\n\\n"``, so such a delta re-arms the hold for the new segment: the reply
    after tool commentary may still be the bare marker. A prefix still held from the previous
    segment is content once more text follows, so it is released.
    """
    released = ""
    if delta.startswith("\n\n"):
        released, hold["held"], hold["buf"] = hold["held"], "", ""
    hold["buf"] += delta
    if is_partial_silence_marker(hold["buf"]):
        hold["held"] += delta
        return released
    released, hold["held"] = released + hold["held"] + delta, ""
    return released

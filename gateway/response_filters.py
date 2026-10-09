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

# The persisted user-row kind of a self-injected MessageEvent(internal=True) turn — the only
# machinery kind the gateway produces; only these may vanish on a bare silence marker.
INTERNAL_NOTIFICATION_DISPLAY_KIND = "internal_notification"
MACHINERY_DISPLAY_KINDS = frozenset({INTERNAL_NOTIFICATION_DISPLAY_KIND})

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


def strip_trailing_silence_marker(text: Any, *, respect_fences: bool = True) -> Any:
    """Remove the run of top-level standalone silence-marker lines ending substantive text.

    One or more consecutive trailing marker lines are removed. The exact interactive
    silence rule remains authoritative for a bare marker, so a bare marker is returned
    unchanged, and a response made only of marker lines collapses to its last marker so
    it still reaches the intentional-silence path rather than the empty-response one.
    Marker-looking lines inside fenced code are content, not control text.
    ``respect_fences=False`` is only for raw text whose fence-aware visible form
    (think blocks removed) already proved the trailing lines are top-level.
    """
    if not isinstance(text, str) or is_intentional_silence_response(text):
        return text
    return _strip_trailing_marker_lines(
        text, is_intentional_silence_response,
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
    if getattr(event, "internal", False) or getattr(event, "_heartbeat_session_id", None):
        return INTERNAL_NOTIFICATION_DISPLAY_KIND
    return None


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

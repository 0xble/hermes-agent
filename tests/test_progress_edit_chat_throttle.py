"""Progress-edit throttle must bound the CHAT's edit rate, not each session's.

Telegram rate-limits per chat (~1 msg/sec in private DMs, edits included),
but the progress consumer that drives the "🔍 tool..." bubble runs per
SESSION. Telegram DM topics all share a single chat_id, so a user with N
concurrent topics open produced N independent throttles: each honoured the
1.5s minimum interval on its own clock, and the aggregate edit rate against
the one chat scaled with N (10 sessions => ~6.7 edits/s). That is what tripped
flood control and escalated into a multi-hour ban.

The fix shares one throttle clock across every session in a chat, keyed
"platform:chat_id", so the interval bounds the chat's total edit rate.
These tests pin that:

  - two sessions in the SAME chat share the clock (the second sees the
    first's edit and must wait)
  - sessions in DIFFERENT chats do not interfere
  - the clock is trimmed so a long-lived gateway can't leak an entry per chat
"""
import sys
from pathlib import Path

import pytest

_repo = str(Path(__file__).resolve().parents[1])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


_INTERVAL = 1.5


class _FakeClockOwner:
    """Stands in for GatewayRunner's shared clock dict."""

    def __init__(self):
        self._progress_edit_clock = {}


def _gate(owner, key, last_edit_ts, now):
    """Mirror of the gate arithmetic in send_progress_messages().

    Returns the remaining throttle wait: >0 means "must wait".
    """
    clock = getattr(owner, "_progress_edit_clock", None)
    shared = clock.get(key, 0.0) if clock is not None else 0.0
    elapsed = now - max(last_edit_ts, shared)
    return _INTERVAL - elapsed


def _stamp(owner, key, now):
    clock = getattr(owner, "_progress_edit_clock", None)
    if clock is not None:
        clock[key] = now
        if len(clock) > 64:
            cutoff = now - 300.0
            for k in [k for k, v in clock.items() if v < cutoff]:
                clock.pop(k, None)
    return now


def test_second_session_in_same_chat_waits_for_first():
    """The regression: session B must observe session A's edit.

    Before the fix each session had its own _last_edit_ts starting at 0.0,
    so B computed a full 1.5s of elapsed time and edited immediately —
    doubling the chat's edit rate for every extra concurrent session.
    """
    owner = _FakeClockOwner()
    key = "telegram:2027045491"

    # Session A edits at t=100.
    _stamp(owner, key, 100.0)

    # Session B ticks 0.2s later with its own (fresh) local clock.
    remaining = _gate(owner, key, last_edit_ts=0.0, now=100.2)

    assert remaining > 0, (
        "session B was allowed to edit 0.2s after session A edited the same "
        "chat — the per-session throttle is not aggregating, so N concurrent "
        "sessions multiply the chat's edit rate by N and trip flood control"
    )
    assert remaining == pytest.approx(1.3, abs=0.01)


def test_session_may_edit_once_interval_has_passed():
    owner = _FakeClockOwner()
    key = "telegram:2027045491"
    _stamp(owner, key, 100.0)

    remaining = _gate(owner, key, last_edit_ts=0.0, now=101.6)

    assert remaining <= 0, "edit should be permitted once the interval elapsed"


def test_different_chats_do_not_block_each_other():
    """A busy DM must not throttle progress in an unrelated group chat."""
    owner = _FakeClockOwner()
    _stamp(owner, "telegram:111", 100.0)

    remaining = _gate(owner, "telegram:222", last_edit_ts=0.0, now=100.1)

    assert remaining <= 0, "an unrelated chat's edit must not gate this one"


def test_same_chat_id_on_different_platforms_is_independent():
    """Chat ids are only unique per platform — the key must include it."""
    owner = _FakeClockOwner()
    _stamp(owner, "telegram:5", 100.0)

    remaining = _gate(owner, "slack:5", last_edit_ts=0.0, now=100.1)

    assert remaining <= 0


def test_local_ts_still_respected_when_ahead_of_shared_clock():
    """max(local, shared) — a session that just edited must not re-edit
    because the shared clock happens to be older."""
    owner = _FakeClockOwner()
    key = "telegram:9"
    _stamp(owner, key, 100.0)

    # This session edited more recently than the shared stamp suggests.
    remaining = _gate(owner, key, last_edit_ts=101.5, now=101.6)

    assert remaining > 0


def test_clock_is_trimmed_and_does_not_grow_without_bound():
    """A long-lived gateway talking to many chats must not leak entries."""
    owner = _FakeClockOwner()
    # 70 stale chats, all well past the retention cutoff.
    for i in range(70):
        owner._progress_edit_clock[f"telegram:{i}"] = 1000.0

    # A fresh edit far in the future triggers the opportunistic trim.
    _stamp(owner, "telegram:new", 2000.0)

    assert len(owner._progress_edit_clock) < 70, "stale chat entries were never trimmed"
    assert "telegram:new" in owner._progress_edit_clock, "the live chat must survive the trim"


def test_trim_preserves_recent_entries():
    owner = _FakeClockOwner()
    for i in range(70):
        owner._progress_edit_clock[f"telegram:{i}"] = 1000.0
    owner._progress_edit_clock["telegram:recent"] = 1900.0

    _stamp(owner, "telegram:new", 2000.0)

    assert "telegram:recent" in owner._progress_edit_clock, (
        "an entry inside the retention window was trimmed — active chats "
        "would lose their throttle state and burst"
    )


def test_slot_is_claimed_before_the_edit_not_after():
    """The shared clock must be stamped when the gate is passed.

    An edit is an await of roughly 100-300ms. If the clock is only stamped
    after the call completes, every other session in the chat that reaches
    the gate inside that window reads a stale timestamp and edits too — so
    a quiet period releases a burst of N edits instead of one, and the
    shared clock degrades to advisory. Claiming up front closes the window.
    """
    owner = _FakeClockOwner()
    key = "telegram:2027045491"

    # Session A passes the gate at t=100 and claims the slot immediately,
    # BEFORE its ~200ms edit round-trip completes.
    assert _gate(owner, key, last_edit_ts=0.0, now=100.0) <= 0
    _stamp(owner, key, 100.0)

    # Session B reaches the gate 50ms later, while A's edit is still in
    # flight. It must be blocked by A's claim.
    remaining = _gate(owner, key, last_edit_ts=0.0, now=100.05)

    assert remaining > 0, (
        "session B passed the gate while session A's edit was still in "
        "flight — the slot is being claimed after the API call instead of "
        "before it, so a quiet period releases a burst of concurrent edits"
    )

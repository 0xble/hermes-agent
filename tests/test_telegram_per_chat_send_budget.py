"""The per-chat outbound budget, sized from Telegram's per-chat ceilings.

Telegram publishes no exact numbers and adapts them under load, but the
community-established envelope is ~1 request/s to one private chat (~60/min)
and ~20/min to one group, supergroup or channel, with EVERY Bot API call
drawing on it: sendMessage, editMessageText, sendChatAction, deleteMessage.

On 2026-09-10 a DM took a 2h09m flood ban while its visible send+edit rate was
only 8-12/min. The cause was arithmetic, not a burst: the send gate allowed
54/min on its own and the typing loop ran a SECOND, independent budget worth
another 60/min, so the two together permitted ~114/min into one chat. Sizing
each limiter near the whole ceiling is the bug these tests exist to prevent,
so they assert the SUM of every per-chat limiter against the ceiling rather
than checking any one of them in isolation.
"""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_repo = str(Path(__file__).resolve().parents[1])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from tests.test_telegram_typing_chat_budget import _ensure_telegram_mock  # noqa: E402

_ensure_telegram_mock()

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram.adapter import (  # noqa: E402
    TelegramAdapter,
    _TelegramSendCooldownExceeded,
)

# Telegram's per-chat envelopes, as calls per minute. Every limiter below is
# measured against these, not against a single-request limit.
PRIVATE_CHAT_CEILING_PER_MIN = 60.0
GROUP_CHAT_CEILING_PER_MIN = 20.0

# Real ids from the incident: private chats are positive, groups negative.
DM = "2027045491"
GROUP = "-1002027045491"


def _adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    return adapter


class _FloodError(Exception):
    """Stands in for telegram.error.RetryAfter, which the real PTB may or may
    not be installed to provide. `_telegram_retry_after` reads the attribute."""

    def __init__(self, retry_after: float):
        super().__init__(f"Flood control exceeded. Retry in {retry_after} seconds")
        self.retry_after = retry_after


# --- chat classification -------------------------------------------------------------------------

def test_chat_class_comes_from_the_id_sign():
    """Telegram gives groups, supergroups and channels negative ids, so the id
    alone classifies the chat with no extra getChat round-trip."""
    assert TelegramAdapter._chat_is_group(GROUP) is True
    assert TelegramAdapter._chat_is_group(DM) is False


def test_unparseable_chat_ids_fall_back_to_the_looser_class():
    """Test doubles and usernames must not crash the gate. They take the
    private-chat gap, which is the safe direction: a real group id is always
    numeric, so nothing that reaches production is misclassified downward."""
    assert TelegramAdapter._chat_is_group("chat-1") is False
    assert TelegramAdapter._chat_is_group("") is False
    assert TelegramAdapter._chat_is_group(None) is False


# --- the budget itself ---------------------------------------------------------------------------

def test_private_chat_sends_and_typing_together_stay_under_the_ceiling():
    """The invariant the 2026-09-10 ban proved was missing.

    Both limiters spend the SAME per-chat budget, so their rates add. Checking
    either alone is what let 54/min and 60/min ship next to each other.
    """
    adapter = _adapter()

    sends = 60.0 / adapter._chat_send_gap(DM)
    typing = 60.0 / adapter._typing_chat_min_gap(DM)

    assert sends + typing <= PRIVATE_CHAT_CEILING_PER_MIN, (sends, typing)


def test_group_chat_sends_and_typing_together_stay_under_the_ceiling():
    """A group's ceiling is ~3x stricter, so it needs its own, wider gaps."""
    adapter = _adapter()

    sends = 60.0 / adapter._chat_send_gap(GROUP)
    typing = 60.0 / adapter._typing_chat_min_gap(GROUP)

    assert sends + typing <= GROUP_CHAT_CEILING_PER_MIN, (sends, typing)


def test_the_budget_keeps_real_headroom_under_the_ceiling():
    """Telegram calls the limits 'unspecified', adapts them under load, and
    escalates the penalty on repeat offence. Sitting just under the published
    figure is not a margin, so pin a real one."""
    adapter = _adapter()

    for chat, ceiling in ((DM, PRIVATE_CHAT_CEILING_PER_MIN), (GROUP, GROUP_CHAT_CEILING_PER_MIN)):
        total = 60.0 / adapter._chat_send_gap(chat) + 60.0 / adapter._typing_chat_min_gap(chat)
        assert total <= ceiling * 0.8, (chat, total, ceiling)


def test_group_gap_is_wider_than_the_private_chat_gap():
    adapter = _adapter()

    assert adapter._chat_send_gap(GROUP) > adapter._chat_send_gap(DM)


def test_edits_take_a_floor_of_their_own():
    """Guidance is no more than one edit per second to the same message,
    'ideally further apart'. An edit carries only the LATEST state, so spacing
    it costs a reader nothing."""
    adapter = _adapter()

    edit_gap = adapter._chat_send_gap(DM, edit=True)

    assert edit_gap >= adapter._chat_send_gap(DM)
    assert 60.0 / edit_gap <= 30.0, edit_gap


def test_an_edit_never_narrows_a_group_gap():
    """The edit floor is a floor, not an override: it must not pull a group's
    wider gap back down toward the private-chat rate."""
    adapter = _adapter()

    assert adapter._chat_send_gap(GROUP, edit=True) >= adapter._chat_send_gap(GROUP)


# --- adaptive backoff ----------------------------------------------------------------------------

def test_a_published_retry_after_widens_that_chats_gap():
    """A retry_after is the server saying this chat is at its limit. Walking
    straight back to the normal gap is what escalated 0.6s and 0.7s cooldowns
    into a 7728s ban on 2026-09-10."""
    adapter = _adapter()
    before = adapter._chat_send_gap(DM)

    adapter._record_send_penalty(DM)

    assert adapter._chat_send_gap(DM) > before


def test_the_penalty_is_scoped_to_the_offending_chat():
    adapter = _adapter()
    before = adapter._chat_send_gap(GROUP)

    adapter._record_send_penalty(DM)

    assert adapter._chat_send_gap(GROUP) == before


def test_the_penalty_expires():
    """A brake, not a latch — the chat returns to its normal rate."""
    adapter = _adapter()
    normal = adapter._chat_send_gap(DM)

    adapter._record_send_penalty(DM)
    assert adapter._chat_send_gap(DM) > normal
    adapter._send_penalty_until[DM] = time.monotonic() - 1.0

    assert adapter._chat_send_gap(DM) == normal


@pytest.mark.asyncio
async def test_a_flood_error_through_the_gate_records_the_penalty():
    """Wiring check: the widening must happen on the real error path, not only
    when something calls the helper directly."""
    adapter = _adapter()

    async def _flooded(*_args, **_kwargs):
        raise _FloodError(1.0)

    with pytest.raises(_FloodError):
        await adapter._run_send_call(DM, _flooded)

    assert adapter._send_penalty_until.get(DM, 0.0) > time.monotonic()


# --- the gate still delivers ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_routine_group_gap_is_waited_out_not_rejected():
    """The inline-wait cap exists so a 7000s server penalty cannot stall the
    chat path for two hours. It must never reject a chat's OWN spacing: with a
    group gap wider than the cap, every second ordinary send would surface as
    a retryable flood error instead of being sent."""
    adapter = _adapter()
    adapter._send_cooldown_max_wait = 5.0
    assert adapter._chat_send_gap(GROUP) > adapter._send_cooldown_max_wait, (
        "precondition: this test is only meaningful while the group gap "
        "exceeds the inline-wait cap")
    sent = []

    async def _send(*_args, **_kwargs):
        sent.append(time.monotonic())
        return "ok"

    await adapter._run_send_call(GROUP, _send)
    await adapter._run_send_call(GROUP, _send)

    assert len(sent) == 2
    assert sent[1] - sent[0] >= adapter._chat_send_gap(GROUP) - 0.05


@pytest.mark.asyncio
async def test_a_long_server_penalty_is_still_handed_back_to_the_caller():
    """The other half of the same cap: a multi-thousand-second wait must not
    be slept off inline."""
    adapter = _adapter()
    adapter._send_cooldown_until[DM] = time.monotonic() + 7728.0

    async def _send(*_args, **_kwargs):
        pytest.fail("must not send while the chat is flood-banned")

    with pytest.raises(_TelegramSendCooldownExceeded):
        await adapter._run_send_call(DM, _send)


@pytest.mark.asyncio
async def test_concurrent_senders_in_one_chat_share_the_budget():
    """Sessions are per-thread, flood limits are per-chat. Four senders must
    not produce four times the rate."""
    adapter = _adapter()
    adapter._send_cooldown_seconds = 0.1
    adapter._send_cooldown_max_wait = 5.0
    stamps = []

    async def _send(*_args, **_kwargs):
        stamps.append(time.monotonic())
        return "ok"

    await asyncio.gather(*[adapter._run_send_call(DM, _send) for _ in range(4)])

    assert len(stamps) == 4
    spread = max(stamps) - min(stamps)
    assert spread >= 0.3 - 0.05, f"four sends compressed into {spread:.3f}s"

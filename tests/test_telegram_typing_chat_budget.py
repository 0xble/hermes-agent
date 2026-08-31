"""Per-chat governance of the typing indicator's Bot API traffic.

``_keep_typing`` runs per SESSION while platforms rate-limit per CHAT, and
Telegram DM topics all share one ``chat_id``. Before this contract, N
concurrent sessions in one chat multiplied the ``sendChatAction`` rate by N
against a ~1 msg/s per-chat envelope, and ``send_typing`` bypassed the
per-chat send cooldown entirely — so it kept calling a bot the server had
already flood-banned.

These tests pin three things: the chat-wide floor, the shed-don't-queue
behavior, and the cooldown gate.
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


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    # Prefer real PTB; the sys.modules mock is process-wide and permanent, so
    # it would poison the real-PTB tests that run later in the same session.
    try:
        import telegram  # noqa: F401
        import telegram.constants  # noqa: F401
        import telegram.error  # noqa: F401
        import telegram.ext  # noqa: F401
        return
    except Exception:
        pass
    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    mod.error.RetryAfter = type(
        "RetryAfter",
        (Exception,),
        {"__init__": lambda self, retry_after=1: setattr(self, "retry_after", retry_after)},
    )
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.base import BasePlatformAdapter  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter(gap: float = 0.05) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_chat_action = AsyncMock(return_value=None)
    # Instance override keeps the assertions fast without real 1s waits.
    adapter._TYPING_CHAT_MIN_GAP_S = gap
    return adapter


@pytest.mark.asyncio
async def test_second_session_in_same_chat_sheds_its_tick():
    """Two sessions sharing a chat_id get one typing slot, not two.

    This is the whole bug: DM topics share a chat_id, so the per-session loop
    used to issue one sendChatAction each.
    """
    adapter = _make_adapter()

    assert adapter._claim_typing_chat_budget("chat-1") is True
    assert adapter._claim_typing_chat_budget("chat-1") is False
    assert adapter._claim_typing_chat_budget("chat-1") is False


@pytest.mark.asyncio
async def test_budget_is_per_chat_not_global():
    """A busy chat must not silence typing in an unrelated chat."""
    adapter = _make_adapter()

    assert adapter._claim_typing_chat_budget("chat-1") is True
    assert adapter._claim_typing_chat_budget("chat-2") is True
    assert adapter._claim_typing_chat_budget("chat-3") is True


@pytest.mark.asyncio
async def test_budget_replenishes_after_the_gap():
    """The floor is a rate limit, not a one-shot latch."""
    adapter = _make_adapter(gap=0.05)

    assert adapter._claim_typing_chat_budget("chat-1") is True
    assert adapter._claim_typing_chat_budget("chat-1") is False
    await asyncio.sleep(0.06)
    assert adapter._claim_typing_chat_budget("chat-1") is True


@pytest.mark.asyncio
async def test_aggregate_rate_is_bounded_across_many_sessions():
    """Ten concurrent sessions in one chat stay under the chat's floor.

    Without the shared budget the aggregate rate scaled with session count
    until the platform issued a multi-hour flood penalty.
    """
    adapter = _make_adapter(gap=0.05)

    granted = 0
    deadline = time.monotonic() + 0.22
    while time.monotonic() < deadline:
        for _ in range(10):  # ten sessions all trying at once
            if adapter._claim_typing_chat_budget("chat-1"):
                granted += 1
        await asyncio.sleep(0.01)

    # ~0.22s at one per 0.05s ⇒ at most ~5, regardless of the 10 callers.
    assert granted <= 6, f"aggregate typing rate not bounded: {granted}"
    assert granted >= 2, f"typing starved entirely: {granted}"


def test_keep_typing_interval_stays_under_platform_expiry():
    """Refresh cadence must sit under the ~5s platform typing expiry.

    Pinned as a regression: the old 2.0s default refreshed ~2.5x more often
    than the lease required, which is pure flood budget spent on a cosmetic
    indicator.
    """
    import inspect

    sig = inspect.signature(BasePlatformAdapter._keep_typing)
    interval = sig.parameters["interval"].default
    assert 3.0 <= interval < 5.0, interval


@pytest.mark.asyncio
async def test_send_typing_is_gated_by_the_per_chat_send_cooldown():
    """A published flood window must quiesce typing, not just send()/edits.

    ``send_chat_action`` used to bypass ``_send_cooldown_until`` entirely,
    so a chat under a multi-thousand-second penalty kept taking typing calls
    every couple of seconds per active session.
    """
    adapter = _make_adapter()
    adapter._send_cooldown_until["123"] = time.monotonic() + 3600.0

    await adapter.send_typing("123")

    adapter._bot.send_chat_action.assert_not_called()


@pytest.mark.asyncio
async def test_send_typing_resumes_once_the_cooldown_expires():
    """The gate is time-bounded — it must not latch the indicator off."""
    adapter = _make_adapter()
    adapter._send_cooldown_until["123"] = time.monotonic() - 1.0

    await adapter.send_typing("123")

    adapter._bot.send_chat_action.assert_called_once()


@pytest.mark.asyncio
async def test_send_typing_unaffected_for_other_chats():
    """A cooldown on one chat must not gate typing in another."""
    adapter = _make_adapter()
    adapter._send_cooldown_until["123"] = time.monotonic() + 3600.0

    await adapter.send_typing("456")

    adapter._bot.send_chat_action.assert_called_once()


@pytest.mark.asyncio
async def test_a_lone_session_is_never_shed_by_the_chat_floor():
    """The floor must not override an explicitly configured faster cadence.

    A fixed constant coarser than the caller's interval would shed a LONE
    session's ticks and kill the indicator for a single user who deliberately
    asked for a fast refresh. The floor is min(constant, interval).
    """
    adapter = _make_adapter(gap=1.0)

    # One session refreshing every 0.05s must get every tick.
    for _ in range(3):
        assert adapter._claim_typing_chat_budget("chat-1", 0.05) is True
        await asyncio.sleep(0.06)


@pytest.mark.asyncio
async def test_concurrent_sessions_never_exceed_the_single_session_rate():
    """The invariant the floor actually enforces."""
    adapter = _make_adapter(gap=1.0)

    interval = 0.05
    granted = 0
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        for _ in range(8):  # eight sessions, same chat, same cadence
            if adapter._claim_typing_chat_budget("chat-1", interval):
                granted += 1
        await asyncio.sleep(0.01)

    # A single session over 0.25s at 0.05s would produce ~5. Eight sessions
    # must not beat that.
    assert granted <= 6, f"concurrent sessions exceeded single-session rate: {granted}"


@pytest.mark.asyncio
async def test_production_interval_keeps_the_constant_floor():
    """min() must not weaken the bound at the real 4.0s cadence."""
    adapter = _make_adapter(gap=1.0)

    assert adapter._claim_typing_chat_budget("chat-1", 4.0) is True
    # Second session 0.1s later is still inside the 1.0s floor.
    await asyncio.sleep(0.1)
    assert adapter._claim_typing_chat_budget("chat-1", 4.0) is False


@pytest.mark.asyncio
async def test_budget_helper_tolerates_bare_adapters():
    """Legacy/bare adapters built without __init__ keep typing rather than
    losing the indicator to an AttributeError."""
    bare = TelegramAdapter.__new__(TelegramAdapter)

    assert bare._claim_typing_chat_budget("chat-1") is True

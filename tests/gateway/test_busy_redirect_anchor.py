"""A successful busy redirect re-anchors the running turn's reply to the redirecting message.

The turn's reply anchor and ledger identity are bound to the message that OPENED it and the
final send is bracketed against that event, so before this a redirected turn answered B while
its reply still quoted A (#115001, Repro A). Both redirect entry points — the interrupt-mode
busy path and the priority path — must move the anchor; a refused redirect must not.
"""

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import _reply_anchor_for_event
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


class Receiver:
    _supports_active_turn_redirect = True

    def __init__(self, accept=True):
        self.accept = accept

    def redirect(self, text):
        return self.accept

    def steer(self, text):
        return self.accept


def _running_turn(runner, key, receiver):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1", chat_type="dm")
    opening = MessageEvent(text="What is the weather in Shanghai?", source=source, message_id="461")
    ctx = TurnContext(session_key=key, event_message_id="461", inbound_message_id="461")
    turn = runner._session_state(key).turn
    turn.agent, turn.event, turn.ctx = receiver, opening, ctx
    redirecting = MessageEvent(text="What day is tomorrow?", source=source, message_id="462")
    return opening, ctx, redirecting, source


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["busy_interrupt", "priority"])
async def test_successful_redirect_moves_the_turn_reply_anchor_to_the_redirecting_message(route):
    runner = GatewayRunner(config=GatewayConfig())
    receiver = Receiver()
    opening, ctx, redirecting, source = _running_turn(runner, "key", receiver)

    if route == "priority":
        await runner._hm_busy_interrupt(redirecting, source, receiver, "key")
    else:
        outcome = await runner._resolve_busy_steer_or_redirect(redirecting, "key", "interrupt", receiver)
        assert outcome.redirected is True

    # The final send is bracketed against the OPENING event: it now quotes B and is ledgered as B.
    assert _reply_anchor_for_event(opening) == "462"
    assert opening.ledger_message_id == "462"
    # The queued-first-response lane reads the TurnContext anchor: it follows too.
    assert (ctx.event_message_id, ctx.inbound_message_id) == ("462", "462")
    # A's own identity is untouched (the ledger keys on ledger_message_id, not on this).
    assert opening.message_id == "461"


@pytest.mark.asyncio
async def test_refused_or_foreign_redirect_leaves_the_anchor_on_the_opening_message():
    runner = GatewayRunner(config=GatewayConfig())
    refusing = Receiver(accept=False)
    opening, ctx, redirecting, _ = _running_turn(runner, "key", refusing)
    outcome = await runner._resolve_busy_steer_or_redirect(redirecting, "key", "interrupt", refusing)
    assert outcome.redirected is False
    assert _reply_anchor_for_event(opening) == "461" and opening.ledger_message_id is None
    assert ctx.event_message_id == "461"

    # A redirect that lands on an agent which no longer owns the slot (a newer turn claimed it)
    # must not re-anchor the newer turn.
    displaced = Receiver()
    opening2, ctx2, redirecting2, _ = _running_turn(runner, "key2", Receiver())
    assert await runner._resolve_busy_steer_or_redirect(redirecting2, "key2", "interrupt", displaced)
    assert _reply_anchor_for_event(opening2) == "461" and ctx2.event_message_id == "461"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route", ["busy_interrupt", "priority_interrupt", "busy_steer", "priority_steer", "slash_steer"])
async def test_a_turn_that_takes_in_an_addressed_message_keeps_the_silence_fallback(route):
    """Redirect and steer fold a new message into the running turn, which then answers it too: if
    that message was addressed to the bot, a bare silence marker must not end the turn silently."""
    runner = GatewayRunner(config=GatewayConfig())
    receiver = Receiver()
    opening, ctx, incoming, source = _running_turn(runner, "key", receiver)
    opening.reply_expected, incoming.reply_expected = False, True

    if route == "priority_interrupt":
        await runner._hm_busy_interrupt(incoming, source, receiver, "key")
    elif route == "priority_steer":
        runner._hm_busy_steer(incoming, receiver, "key")
    elif route == "slash_steer":
        incoming.text = "/steer " + incoming.text
        assert (await runner._busy_steer_command(incoming, "key", source)).startswith("⏩")
    else:
        outcome = await runner._resolve_busy_steer_or_redirect(incoming, "key", route[5:], receiver)
        assert outcome.redirected or outcome.steered

    assert (opening.reply_expected, ctx.reply_expected) == (True, True)


@pytest.mark.asyncio
async def test_accepted_relay_steer_keeps_a_relay_turn_unaddressed_and_sends_no_ack():
    """Relay sends "/steer <header>"; admission sees the slash and cannot mark it. Folding an
    unknown expectation into a relay-opened turn used to reset False to None, so a bare NO_REPLY
    posted the visible fallback again."""
    runner = GatewayRunner(config=GatewayConfig())
    receiver = Receiver()
    opening, ctx, incoming, source = _running_turn(runner, "key", receiver)
    opening.reply_expected = ctx.reply_expected = False
    incoming.text = "/steer [relay from=agent@example.com receipt=receipt-2]\nplease also check this"

    reply = await runner._busy_steer_command(incoming, "key", source)

    assert reply is None
    assert incoming.reply_expected is False
    assert (opening.reply_expected, ctx.reply_expected) == (False, False)


@pytest.mark.asyncio
async def test_addressed_relay_steer_keeps_its_expectation_and_ack():
    runner = GatewayRunner(config=GatewayConfig())
    receiver = Receiver()
    opening, ctx, incoming, source = _running_turn(runner, "key", receiver)
    incoming.text = "/steer [relay from=agent@example.com receipt=receipt-3]\nplease answer me"
    incoming.reply_expected = True

    reply = await runner._busy_steer_command(incoming, "key", source)

    assert reply
    assert incoming.reply_expected is True
    assert ctx.reply_expected is True


@pytest.mark.asyncio
async def test_addressed_relay_steer_queue_fallback_keeps_its_ack():
    runner = GatewayRunner(config=GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1", chat_type="dm")
    queued = []
    runner._delivery_adapter_for = lambda _source: object()
    runner._enqueue_fifo = lambda _key, queued_event, _adapter: queued.append(queued_event)
    runner._peek_session_state = lambda _key: None
    event = MessageEvent(
        text="/steer [relay from=agent@example.com receipt=receipt-4]\nplease answer me",
        source=source, message_id="steer-4", reply_expected=True,
    )

    reply = await runner._busy_steer_command(event, "key", source)

    assert reply == "No active agent — /steer queued for the next turn."
    assert queued[0].reply_expected is True


@pytest.mark.asyncio
async def test_steer_queue_fallback_marks_relay_origin_unaddressed():
    runner = GatewayRunner(config=GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1", chat_type="dm")
    queued = []
    runner._delivery_adapter_for = lambda _source: object()
    runner._enqueue_fifo = lambda _key, queued_event, _adapter: queued.append(queued_event)
    runner._peek_session_state = lambda _key: None
    event = MessageEvent(
        text="/steer [relay from=agent@example.com receipt=receipt-1]\nplease handle this",
        source=source, message_id="steer-1",
    )

    reply = await runner._busy_steer_command(event, "key", source)

    assert reply is None
    assert len(queued) == 1
    assert queued[0].reply_expected is False


@pytest.mark.asyncio
async def test_typed_steer_queue_fallback_keeps_its_ack():
    runner = GatewayRunner(config=GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1", chat_type="dm")
    queued = []
    runner._delivery_adapter_for = lambda source: object()
    runner._enqueue_fifo = lambda session_key, queued_event, adapter: queued.append(queued_event)
    runner._peek_session_state = lambda session_key: None
    event = MessageEvent(text="/steer please handle this", source=source, message_id="steer-2")

    reply = await runner._busy_steer_command(event, "key", source)

    assert reply == "No active agent — /steer queued for the next turn."
    assert len(queued) == 1
    assert queued[0].reply_expected is None

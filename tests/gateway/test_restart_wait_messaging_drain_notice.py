"""A chat message that arrives while a restart is draining is kept for replay, silently.

RECORDED DIVERGENCE from upstream. Upstream refuses such a message at the session authority's
admission gate (``runtime_draining``) and answers it with the "Gateway is restarting" notice, so the
sender has to send it again (#152410). The fork takes it before that gate: a drain arrival is
preserved (adapter FIFO, else the shutdown spool) and replayed by the next gateway, with no notice.
Either way it must not escape ``_handle_message`` as an exception the adapter turns into the generic
"Something went wrong… /retry" turn error, and nothing is half-admitted. Real GatewayRunner,
DiscordAdapter and SessionAuthority over SQLite.
"""

import pytest

from tests.gateway.test_wake_during_restart_drain_authority import admissions, authority_route


@pytest.mark.asyncio
async def test_message_during_restart_wait_gets_the_drain_notice_not_a_turn_error(monkeypatch):
    from gateway.platforms.event import MessageEvent

    executed = []
    runner, _adapter, authority, source = await authority_route(monkeypatch, executed)
    runner._restart_requested = True
    runner._draining = True  # the restart's wait ended: stop() is draining and refusing new turns
    event = MessageEvent(text="are you there?", source=source, message_id="B")

    reply = await runner._handle_message(event)

    assert reply is None and getattr(event, "_drain_deferred", False) is True  # kept for replay, not refused
    assert executed == [] and admissions(runner, authority, source) == []  # nothing half-admitted

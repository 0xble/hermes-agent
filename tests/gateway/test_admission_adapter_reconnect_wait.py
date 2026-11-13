"""An admitted native turn claimed while its platform adapter reconnects waits for the adapter.

A fatal adapter leaves ``runner.adapters`` until the reconnect watcher installs its replacement.
When that happened after the claim's route preflight, restoring the turn's provenance raised
``not_found`` and the user's message was lost as 'The admitted turn failed.' The turn now waits
(bounded) for the slot to refill; a slot still empty at the bound fails as before.
"""
import asyncio

import pytest


async def _admitted(tmp_path, monkeypatch, text):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import SessionAuthority
    from gateway.session_ingress_context import native_callback
    from hermes_constants import get_hermes_home
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.fakes.runtime_owner import initialize_session_authority

    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    event = MessageEvent(text=text, source=source, message_id=text)
    with native_callback(runner, event, get_hermes_home()):
        receipt = await authority.admit_native(event)
    executed = []

    async def handle(event):
        executed.append(event.text)
        return 'ok'

    monkeypatch.setattr(runner, '_handle_message', handle)
    return runner, adapter, authority, receipt, executed


def _adapter_lost_after_claim(monkeypatch, runner):
    """The adapter goes fatal between the claim's preflight and execution (the live window)."""
    from gateway.config import Platform
    from gateway.session_authority import SessionAuthority
    claim = SessionAuthority._claim_next

    async def claim_then_lose_adapter(self, ref, live):
        claimed = await claim(self, ref, live)
        runner.adapters.pop(Platform.DISCORD, None)  # the fatal handler's teardown
        return claimed

    monkeypatch.setattr(SessionAuthority, '_claim_next', claim_then_lose_adapter)


def _statuses(authority, receipt):
    from hermes_state_runtime import list_session_admissions
    rows = list_session_admissions(authority.db, session_id=receipt.ref.session_id, pending_only=False)
    assert all('provenance' in row['payload']['native_text_v1'] for row in rows)
    return [row['status'] for row in rows]


@pytest.mark.asyncio
async def test_turn_waits_out_a_brief_adapter_reconnect(tmp_path, monkeypatch):
    from gateway.config import Platform
    from gateway.session_authority import SessionAuthority

    runner, adapter, authority, receipt, executed = await _admitted(tmp_path, monkeypatch, 'during-reconnect')
    try:
        _adapter_lost_after_claim(monkeypatch, runner)

        async def reinstall():
            while Platform.DISCORD in runner.adapters:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.6)
            runner.adapters[Platform.DISCORD] = adapter  # the reconnect watcher's install

        reconnect = asyncio.create_task(reinstall())
        await SessionAuthority._drain(authority, receipt.ref)
        await reconnect
        assert executed == ['during-reconnect']
        assert _statuses(authority, receipt) == ['terminal']
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_turn_still_fails_when_the_adapter_never_returns(tmp_path, monkeypatch, caplog):
    from gateway import session_ingress_context
    from gateway.config import Platform
    from gateway.session_authority import SessionAuthority

    monkeypatch.setattr(session_ingress_context, 'ADAPTER_RECONNECT_WAIT_S', 0.5)
    runner, _adapter, authority, receipt, executed = await _admitted(tmp_path, monkeypatch, 'adapter-gone')
    try:
        _adapter_lost_after_claim(monkeypatch, runner)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(SessionAuthority._drain(authority, receipt.ref), timeout=5)
        assert 0.5 <= loop.time() - started < 5
        assert executed == []
        assert _statuses(authority, receipt) == ['terminal']  # settled as failed, not left queued
        assert any(record.getMessage().startswith('Admitted turn ') and record.exc_info
                   and 'not_found' in str(record.exc_info[1]) for record in caplog.records)
    finally:
        authority.db.close()

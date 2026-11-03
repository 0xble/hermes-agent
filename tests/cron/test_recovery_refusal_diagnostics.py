"""Recovery refusal diagnostics do not reclassify a lost submit as an owner refusal."""
from contextlib import asynccontextmanager

import pytest

from cron.scheduler_transport import recovering_cron_client
from hermes_cli import gateway_client


@pytest.mark.asyncio
async def test_recovery_refusals_log_the_reason_and_preserve_original_failure(monkeypatch, caplog):
    original = TimeoutError('cron.submit phase=answer timeout=30s request_id=fire job_id=job')
    calls = []

    class Peer:
        async def rpc(self, method, **params):
            calls.append(method)
            if method == 'cron.submit':
                raise original
            raise gateway_client.GatewayRPCError('storage_unavailable')

    @asynccontextmanager
    async def connected():
        yield Peer()

    async def no_delay(delay):
        pass

    monkeypatch.setattr(gateway_client, 'connect_gateway', connected)
    monkeypatch.setattr('cron.scheduler_transport.asyncio.sleep', no_delay)
    params = {'job_id': 'job', 'request_id': 'fire', 'extra_prompt': None}
    async with recovering_cron_client(params) as call:
        with pytest.raises(TimeoutError) as caught:
            await call('submit', params)
    assert caught.value is original
    assert calls == ['cron.submit'] + ['cron.recover'] * 4
    assert caplog.text.count('Cron recovery read refused: storage_unavailable') == 4

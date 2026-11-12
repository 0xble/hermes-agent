"""An established gateway context must not rename its caller's error a dial failure."""
import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from hermes_cli import gateway_client


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['submit_answer', 'worker_register', 'caller_io'])
async def test_connected_gateway_preserves_failure_phase(monkeypatch, phase):
    async def handler(ws):
        async for raw in ws:
            assert json.loads(raw)['method'] == 'cron.submit'
            # The owner has received the request; its missing answer isn't an open failure.

    async with serve(handler, '127.0.0.1', 0) as server:
        monkeypatch.setenv('HERMES_TUI_GATEWAY_URL',
            f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}')
        original = TimeoutError('worker_receipt_timeout') if phase == 'worker_register' else OSError('caller disk full')
        with pytest.raises(Exception) as caught:
            async with gateway_client.connect_gateway() as client:
                if phase == 'submit_answer':
                    await client.rpc('cron.submit', _timeout=.01, request_id='fire', job_id='job')
                else:
                    raise original
        assert 'Gateway connection failed' not in str(caught.value)
        if phase == 'submit_answer':
            assert isinstance(caught.value, TimeoutError)
            assert 'cron.submit' in str(caught.value) and 'answer' in str(caught.value)
            assert '0.01' in str(caught.value) and 'fire' in str(caught.value)
            assert isinstance(caught.value.__cause__, TimeoutError)
        else:
            assert caught.value is original

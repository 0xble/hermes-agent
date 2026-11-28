"""Direct fault probes for automation_peer: pass a scratch directory and replay or clean-close."""
import asyncio
import functools
import json
from pathlib import Path
import sys
from unittest.mock import patch

import websockets
from websockets.asyncio.server import ServerConnection

from tests.gateway.fixtures.automation_peer import probe


async def prove(root, base, fault):
    initial_reply = None
    injected = []
    buffered_markers = ('BUFFERED_AT_CLOSE_1', 'BUFFERED_AT_CLOSE_2')

    class FaultConnection(ServerConnection):
        async def __aiter__(self):
            nonlocal initial_reply
            async for line in super().__aiter__():
                frame = json.loads(line)
                action = frame.get('action', {})
                if (initial_reply is None and frame['type'] == 'outbound'
                        and action.get('op') == 'send' and action.get('content') == 'AUTOMATION_ACK'):
                    initial_reply = frame
                if frame['type'] != 'going_idle':
                    yield line
                    continue

                assert initial_reply is not None
                if fault == 'replay':
                    injected.append('AUTOMATION_ACK')
                    # Copy the actual warm-up reply at the successor's relay receive boundary.
                    yield json.dumps({**initial_reply, 'requestId': 'probe-replay'})
                    yield line
                else:
                    yield line  # Let the connector acknowledge going_idle before closing.
                    await self.wait_closed()
                    assert self.close_code in (1000, 1001), self.close_code
                    # Model two complete frames already in the receive buffer at clean EOF.
                    # ACKing either one now raises the real socket's ConnectionClosedOK.
                    for marker in buffered_markers:
                        injected.append(marker)
                        yield json.dumps({**initial_reply, 'requestId': marker,
                                          'action': {**initial_reply['action'], 'content': marker}})

    serve = functools.partial(websockets.serve, create_connection=FaultConnection)
    with patch.object(websockets, 'serve', serve):
        if fault == 'replay':
            try:
                await probe(root, base, 'restart-started')
            except AssertionError as exc:
                if not str(exc).startswith('unexpected post-restart AUTOMATION_ACK sends:'):
                    raise
                assert injected == ['AUTOMATION_ACK'], injected
                print('Replay at the relay boundary was rejected by the drained recount.')
            else:
                raise AssertionError(f'Fixture accepted a replayed warm-up reply: {injected!r}')
        else:
            await probe(root, base, 'restart-started')
            assert injected == list(buffered_markers), injected
            print('Both buffered frames were consumed after clean close; the relay drained.')


if __name__ == '__main__':
    base, fault = Path(sys.argv[1]), sys.argv[2]
    assert fault in ('replay', 'clean-close'), fault
    asyncio.run(prove(Path(__file__).resolve().parents[3], base, fault))

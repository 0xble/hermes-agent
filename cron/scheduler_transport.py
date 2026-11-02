"""Fresh-link observation of an ambiguous canonical cron RPC; never re-executes work."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager


@asynccontextmanager
async def recovering_cron_client(params):
    from hermes_cli.gateway_client import connect_gateway, GatewayClientError, GatewayRPCError
    from websockets.exceptions import ConnectionClosed

    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(connect_gateway())

        async def call(op, data):
            nonlocal client
            try:
                return await client.rpc('cron.' + op, **data)
            except GatewayRPCError:
                raise  # A refusal to the original request is not a lost answer.
            except (GatewayClientError, ConnectionClosed, OSError) as original:
                if op not in ('submit', 'status', 'recover'):
                    raise
                # Close the blocked submit link before reading on a fresh one. Replaying only
                # reads also keeps a later recovery refusal from becoming a submission refusal.
                for delay in (1, 2, 4, 8):
                    await stack.aclose()
                    await asyncio.sleep(delay)
                    try:
                        client = await stack.enter_async_context(connect_gateway())
                        state = await client.rpc('cron.recover' if op == 'submit' else 'cron.' + op,
                                                 **(params if op == 'submit' else data))
                    except GatewayRPCError as exc:
                        import logging
                        logging.getLogger(__name__).warning('Cron recovery read refused: %s', exc)
                        continue
                    except (GatewayClientError, ConnectionClosed, OSError):
                        continue
                    if op != 'submit':
                        return state
                    if state['status'] in ('queued', 'started', 'terminal'):
                        return {key: state[key] for key in ('session_id', 'admission_id')}
                    if state['status'] != 'missing':
                        break
                raise original

        yield call

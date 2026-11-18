"""Fresh-link observation of an ambiguous canonical cron RPC; never re-executes work."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from time import monotonic

# Recovery reads per lost answer (1+2+4+8s of backoff); also the failed or refused reads tolerated.
RECOVERY_READS = 4
# A lost submit answer is re-read at least this often while the owner may still commit it.
RECOVERY_MAX_DELAY = 10


@asynccontextmanager
async def recovering_cron_client(params):
    from cron import scheduler_authority
    from hermes_cli.gateway_client import connect_gateway, GatewayClientError, GatewayRPCError
    from websockets.exceptions import ConnectionClosed

    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(connect_gateway())

        async def call(op, data):
            nonlocal client
            started = monotonic()
            try:
                return await client.rpc('cron.' + op, **data)
            except GatewayRPCError:
                raise  # A refusal to the original request is not a lost answer.
            except (GatewayClientError, ConnectionClosed, OSError) as original:
                if op not in ('submit', 'status', 'recover'):
                    raise
                # Close the blocked submit link before reading on a fresh one. Replaying only
                # reads also keeps a later recovery refusal from becoming a submission refusal.
                delay, reads, failures, missing = 1, 0, 0, False
                while True:
                    await stack.aclose()
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, RECOVERY_MAX_DELAY)
                    reads += 1
                    try:
                        client = await stack.enter_async_context(connect_gateway())
                        state = await client.rpc('cron.recover' if op == 'submit' else 'cron.' + op,
                                                 **(params if op == 'submit' else data))
                    except GatewayRPCError as exc:
                        import logging
                        logging.getLogger(__name__).warning('Cron recovery read refused: %s', exc)
                        failures += 1
                    except (GatewayClientError, ConnectionClosed, OSError):
                        failures += 1
                    else:
                        if op != 'submit':
                            return state
                        if state['status'] in ('queued', 'started', 'terminal'):
                            return {key: state[key] for key in ('session_id', 'admission_id')}
                        if state['status'] != 'missing':
                            break
                        missing = True
                    if failures >= RECOVERY_READS:
                        break
                    # A contended owner can admit a submit long after its 30s answer budget. Until
                    # the grace the owner-side reconcile also allows, "missing" is not an answer.
                    within_grace = monotonic() - started < scheduler_authority.MISSING_SUBMIT_GRACE_SECONDS
                    if reads >= RECOVERY_READS and not (missing and within_grace):
                        break
                raise original

        yield call

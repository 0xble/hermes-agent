"""A tool-free native loopback turn, with terminal output and no network adapter."""
import asyncio
from dataclasses import replace
from pathlib import Path

from gateway.config import Platform, PlatformConfig
from gateway.outbox import Outbox
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionSource
from hermes_constants import get_hermes_home

STARTUP_SECONDS = 45


class _LoopbackAdapter(BasePlatformAdapter):
    def __init__(self, home, identity):
        super().__init__(PlatformConfig(), Platform.LOCAL)
        self.store = Outbox(home)
        self.identity = identity
        self.reply = None

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "name": "Startup loopback", "type": "dm"}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        # Never create a recoverable pending row, even for a refused canary.
        await asyncio.to_thread(self.store.enqueue_synthetic, f'startup:{self.identity.id}',
            {'destination': 'loopback', 'chat_id': chat_id, 'text': content})
        self.reply = content
        return SendResult(success=True, message_id='synthetic')

    async def send_final_ledgered(self, event, session_key, text_content, metadata, **kwargs):
        # The delivery ledger must not manufacture a user-visible recovery send.
        return await self.send(event.source.chat_id, text_content, metadata=metadata), self


async def run_startup_gate(config, identity):
    from gateway import run
    home = Path(get_hermes_home())
    deadline = asyncio.get_running_loop().time() + STARTUP_SECONDS
    previous_ref = run._gateway_runner_ref
    class LoopbackRunner(run.GatewayRunner):
        def _init_startup_checks(self):
            # Tool installation is outside a standby's loopback boundary.
            pass

        def _init_session_db(self):
            super()._init_session_db(maintenance=False)

        def _init_registries_and_clocks(self):
            super()._init_registries_and_clocks()
            from gateway.hooks import HookRegistry
            self.hooks = HookRegistry()  # Never discover arbitrary profile hooks.

        def _get_proxy_url(self):
            # Remote gateway execution cannot enforce this turn's empty toolset.
            return None

        def _persist_active_agents(self):
            # This turn must never enter user-session crash recovery.
            pass

        async def _run_post_turn_hooks(self, **kwargs):
            # A canary cannot create autonomous goals or loops.
            pass

    runner = LoopbackRunner(replace(config, platforms={}, multiplex_profiles=False))
    source = SessionSource(platform=Platform.LOCAL, chat_id=f'__hermes_startup_gate__:{identity.id}',
                           user_id='__hermes_startup_gate__')
    runner._startup_gate_source = source
    adapter = _LoopbackAdapter(home, identity)
    adapter.gateway_runner = runner
    adapter.set_message_handler(runner._handle_message)
    runner.adapters[Platform.LOCAL] = adapter
    event = MessageEvent(text='Reply with exactly HERMES_READY.', source=source, internal=True,
                         allow_gateway_control=False)
    try:
        async with asyncio.timeout_at(deadline):
            await adapter.handle_message(event)
            key = adapter._event_session_key(event)
            task = adapter._session_tasks.get(key)
            if task is None:
                raise RuntimeError('startup gate did not reach native dispatch')
            await task
            if (getattr(event, '_agent_turn_succeeded', False) is not True or not isinstance(adapter.reply, str) or
                    adapter.reply.strip(' \t\r\n\"\'`*_.,!?:;').upper() != 'HERMES_READY'):
                raise RuntimeError('startup gate did not produce the loopback reply')
    except TimeoutError as exc:
        raise RuntimeError('startup gate exceeded its 45-second deadline') from exc
    finally:
        run._gateway_runner_ref = previous_ref
        await adapter.disconnect()
        runner.session_store.close_all_db_handles()
        runner.close_all_session_db_handles()

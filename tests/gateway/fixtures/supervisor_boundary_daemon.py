"""Test-only barriers at native ordered admission and the real execution entry."""
import os
from pathlib import Path
import runpy

from gateway import session_automation
from gateway.run_inbound import GatewayInboundMixin
from gateway.session_authority import SessionAuthority
from tests.waiting import async_wait_until, wait_until

home = Path(os.environ['HERMES_HOME'])
mode = os.environ['SUPERVISOR_BOUNDARY']
owner = session_automation._owner
handle = GatewayInboundMixin._handle_message
admit = SessionAuthority.admit_native


def snapshot_owner(runner, event, authority):
    if mode == 'checkin-race-admission' and event.metadata.get('checkin') and not (home / 'boundary-ready').exists():
        (home / 'boundary-ready').touch()
        wait_until((home / 'boundary-release').exists, timeout=40, what='ordered admission released')
    return owner(runner, event, authority)


async def native_admission(self, event, *args, **kwargs):
    if event.text == 'HUMAN_FIFO_FOLLOWER':
        (home / 'human-seen').touch()
    return await admit(self, event, *args, **kwargs)


async def inbound(self, event):
    if mode == 'checkin-race-execution' and event.metadata.get('checkin') and not (home / 'boundary-ready').exists():
        (home / 'boundary-ready').touch()
        await async_wait_until((home / 'boundary-release').exists, timeout=40, what='execution entry released')
    return await handle(self, event)


SessionAuthority.admit_native = native_admission
session_automation._owner = snapshot_owner
GatewayInboundMixin._handle_message = inbound
runpy.run_module('gateway.run', run_name='__main__')

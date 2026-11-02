"""A finite client's private auto-start must not outlive its owner, even after SIGKILL."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import psutil
import pytest

from tests.waiting import wait_until


# Only the heavyweight gateway/provider boundary is fake. The real transport bootstrap,
# ensure policy, Popen options and (when present) early lifetime watcher run in child processes.
CLIENT = r'''
import asyncio, json, os, sys, threading
from pathlib import Path
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import patch
from hermes_cli import gateway_client, gateway_runtime, gateway_runtime_service, gateway_runtime_start
home = Path(os.environ['HERMES_HOME'])
real_popen = gateway_runtime_start.subprocess.Popen
GATEWAY = r"""
import json, os, sys, threading
from pathlib import Path
home = Path(os.environ['HERMES_HOME'])
if '--client-owned' in sys.argv:
    from gateway.client_lifetime import arm_client_lifetime
    arm_client_lifetime()
(home / 'gateway_state.json').write_text(json.dumps({'pid': os.getpid(), 'gateway_state': 'running'}))
(home / 'ready').touch()
threading.Event().wait(60)
"""
def spawn(argv, **kw):
    if 'hermes_cli.main' not in argv:
        return real_popen(argv, **kw)
    return real_popen([sys.executable, '-c', GATEWAY, *argv[1:]], **kw)
gateway_runtime_start.subprocess.Popen = spawn
def discover(*a, **kw):
    if (home / 'ready').exists():
        endpoint = SimpleNamespace(api_origin='http://127.0.0.1:1', profile_id=str(home),
                                   instance_id='fake', runtime_protocol=1)
        return gateway_runtime.GatewayDiscovery('ready', endpoint)
    return gateway_runtime.GatewayDiscovery('absent')
gateway_runtime._observe_replacing_stale = lambda *a: discover()
gateway_runtime.discover_gateway_endpoint = discover
gateway_runtime._update_fenced = lambda *a: False
gateway_runtime._starting_multiplexer_root = lambda *a: None
import hermes_cli.gateway_runtime_multiplex as multiplex
multiplex.multiplexer_serves_home = lambda *a: None
multiplex.implied_host_root = lambda *a: None
gateway_runtime_service.discover_existing_gateway_service = lambda *a, **kw: None
gateway_client._session_ticket = lambda *a, **kw: 'fake'
class WS:
    subprotocol = gateway_client.GATEWAY_WS_PROTOCOL
    async def __aiter__(self):
        await asyncio.Future()
        yield
@asynccontextmanager
async def connect(*a, **kw):
    yield WS()
async def run():
    kwargs = {'one_shot': True} if 'one_shot' in __import__('inspect').signature(gateway_client.connect_gateway).parameters else {}
    with patch('websockets.asyncio.client.connect', connect):
        async with gateway_client.connect_gateway(**kwargs):
            (home / 'attached').touch()
            if sys.argv[1] != 'exit':
                await asyncio.Future()
asyncio.run(run())
'''


def _alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # real signals to exact test-created, now-orphaned grandchildren
def test_parallel_finite_clients_leave_no_live_gateways(tmp_path):
    children = []
    gateway_pids = []
    try:
        for mode in ("exit", "term", "kill"):
            home = tmp_path / mode
            home.mkdir(mode=0o700)
            env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(Path.cwd())}
            child = subprocess.Popen([sys.executable, "-c", CLIENT, mode], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            children.append((child, home, mode))
        for child, home, mode in children:
            assert wait_until(lambda: (home / "attached").exists() or child.poll() is not None, timeout=15)
            if not (home / "attached").exists():
                out, err = child.communicate(timeout=5)
                pytest.fail(f"client bootstrap failed: {out}\n{err}")
            gateway_pids.append(json.loads((home / "gateway_state.json").read_text())["pid"])
            if mode != "exit":
                child.send_signal(signal.SIGTERM if mode == "term" else signal.SIGKILL)
            child.wait(timeout=10)
        assert wait_until(lambda: all(not _alive(pid) for pid in gateway_pids), timeout=7), gateway_pids
        # Canonical runtime liveness, not an untrusted diagnostic 'running' string.
        from gateway.status import live_gateway_pid_for_home
        assert all(live_gateway_pid_for_home(home) is None for _, home, _ in children)
    finally:
        for child, home, _ in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
            record = home / "gateway_state.json"
            if record.exists():
                pid = json.loads(record.read_text())["pid"]
                if _alive(pid):
                    os.kill(pid, signal.SIGKILL)  # only the exact children this test spawned

"""A second finite client must not borrow an owner whose creator can end its life."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.waiting import wait_until


# Run the real bootstrap/descriptor/control listener and ensure/lease in separate
# processes. Only provider/MCP setup and the client's WebSocket peer are external doubles.
FIRST_CLIENT = r'''
import asyncio, os, sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from hermes_cli import gateway_client, gateway_runtime_start
home = Path(os.environ['HERMES_HOME'])
real_popen = subprocess_popen = gateway_runtime_start.subprocess.Popen
GATEWAY = r"""
import asyncio, os
from pathlib import Path
from unittest.mock import AsyncMock, patch
from gateway.control_socket import GatewayControlServer
from gateway.client_lifetime import arm_client_lifetime
arm_client_lifetime()
from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.run_bootstrap import start_gateway
home = Path(os.environ['HERMES_HOME'])
real_handle = GatewayControlServer._handle_connection
async def handle(self, reader, writer):
    if not (home / 'interrupt-identify').exists():
        return await real_handle(self, reader, writer)
    (home / 'interrupt-identify').unlink()
    try:
        await reader.readline()
        writer.write(b'{"protocol":1')
        await writer.drain()
        (home / 'identify-mid-read').touch()
        # Owner release ends the process with identify still awaiting its newline.
        await asyncio.Future()
    finally:
        writer.close()
GatewayControlServer._handle_connection = handle
with patch.object(GatewayRunner, '_start_mcp_discovery'), patch.object(GatewayRunner, '_await_mcp_discovery', AsyncMock()):
    asyncio.run(start_gateway(GatewayConfig(), client_owned=True))
"""
TRAMPOLINE = "import subprocess, sys; child = subprocess.Popen([sys.executable, '-c', sys.argv[1]], stdin=sys.stdin); sys.exit(child.wait())"
def spawn(argv, **kwargs):
    if 'hermes_cli.main' in argv:
        if os.environ.get('CLIENT_TRAMPOLINE'):
            child = real_popen([sys.executable, '-c', TRAMPOLINE, GATEWAY], **kwargs)
            (home / 'launcher-pid').write_text(str(child.pid))
            return child
        return real_popen([sys.executable, '-c', GATEWAY], **kwargs)
    return real_popen(argv, **kwargs)
class WS:
    subprotocol = gateway_client.GATEWAY_WS_PROTOCOL
    async def __aiter__(self):
        await asyncio.Future()
        yield
@asynccontextmanager
async def connect(*args, **kwargs):
    yield WS()
def arm_interruption():
    from hermes_cli import gateway_runtime_discovery as discovery
    real_query = discovery.query_identify
    armed = False
    def identify(*args, **kwargs):
        nonlocal armed
        try:
            result = real_query(*args, **kwargs)
        except discovery.DiscoveryError as exc:
            if exc.reason == 'incomplete_control_response':
                (home / 'identify-eof').touch()
            raise
        if result.get('client_owned') and not armed:
            armed = True
            (home / 'second-observed-owner').touch()
        return result
    discovery.query_identify = identify
async def main():
    if os.environ.get('INTERRUPT_IDENTIFY'):
        arm_interruption()
    (home / (os.environ.get('CLIENT_MARKER', 'first') + '-started')).touch()
    with patch.object(gateway_runtime_start.subprocess, 'Popen', spawn), patch('websockets.asyncio.client.connect', connect):
        async with gateway_client.connect_gateway(one_shot=True):
            (home / (os.environ.get('CLIENT_MARKER', 'first') + '-attached')).touch()
            await asyncio.to_thread(sys.stdin.buffer.read)
asyncio.run(main())
'''


@pytest.mark.parametrize("client_lease_token", [None, "other-lease", "creator-lease"])
def test_multiplexer_discovery_requires_the_creators_lease(tmp_path, monkeypatch, client_lease_token):
    from hermes_cli import gateway_runtime as runtime

    home = tmp_path / "profiles" / "child"
    home.mkdir(parents=True, mode=0o700)
    payload = {"runtime_protocol": 1, "pid": 123, "client_owned": True, "client_lease_token": "creator-lease", "supervisor": "none",
               "state": "ready", "instance_id": "finite", "authority_epoch": 1,
               "api_origin": "http://127.0.0.1:41817",
               "served_profiles": [{"profile_id": str(home), "home": str(home)}],
               "capabilities": ["session-authority-v1"]}
    from hermes_cli import gateway_runtime_multiplex, gateway_runtime_discovery
    monkeypatch.setattr(gateway_runtime_multiplex, "multiplexer_root_for", lambda _home: tmp_path)
    monkeypatch.setattr(gateway_runtime_discovery, "query_identify", lambda *_a, **_kw: payload)
    observed = runtime.discover_gateway_endpoint(home, client_lease_token=client_lease_token)
    if client_lease_token == "creator-lease":
        assert observed.state == "ready"
        assert observed.endpoint is not None
        assert observed.endpoint.control_home == str(tmp_path)
    else:
        assert observed.state == "conflict"
        assert observed.reason_code == "client_owned"
        assert observed.endpoint is None


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("interrupt_identify", [False, True], ids=["normal", "eof-on-release"])
def test_second_finite_client_waits_then_owns_its_gateway(tmp_path, monkeypatch, interrupt_identify):
    from hermes_cli.gateway_runtime import ensure_gateway_runtime

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    tmp_path.chmod(0o700)
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": str(Path.cwd())}
    first = subprocess.Popen([sys.executable, "-c", FIRST_CLIENT], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        assert wait_until(lambda: (tmp_path / "first-attached").exists() or first.poll() is not None,
                          timeout=60)
        if first.poll() is not None:
            out, err = first.communicate(timeout=5)
            pytest.fail(f"first finite client failed: {out}\n{err}")

        second = subprocess.Popen([sys.executable, "-c", FIRST_CLIENT], env={**env, "CLIENT_MARKER": "second",
                                        **({"INTERRUPT_IDENTIFY": "1"} if interrupt_identify else {})},
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8")
        assert wait_until(lambda: (tmp_path / "second-started").exists(), timeout=20)
        # A bounded third observer supplies a positive synchronization barrier while
        # the second is pending; neither may attach to or stop the finite owner.
        verdict = ensure_gateway_runtime(tmp_path, timeout=2)
        assert verdict.reason_code == "client_owned"
        assert verdict.endpoint is None
        assert "separate HERMES_HOME" in verdict.detail
        assert "wait" in verdict.detail.lower()
        assert second.poll() is None
        assert not (tmp_path / "second-attached").exists()
        from hermes_cli.gateway_runtime import discover_gateway_endpoint
        from gateway.control_socket import query_gateway_control
        identity = query_gateway_control(tmp_path, "identify", timeout=2)
        assert identity is not None and identity["client_owned"] is True
        status = query_gateway_control(tmp_path, "status", timeout=2)
        assert status is not None and status["client_owned"] is True
        observed = discover_gateway_endpoint(tmp_path, timeout=2)
        assert observed.state == "conflict"
        assert observed.reason_code == "client_owned"
        assert observed.endpoint is None
        assert first.poll() is None
        if interrupt_identify:
            assert wait_until(lambda: (tmp_path / "second-observed-owner").exists(), timeout=20)
            (tmp_path / "interrupt-identify").touch()
            assert wait_until(lambda: (tmp_path / "identify-mid-read").exists(), timeout=20)
        first.stdin.close()
        first.stdin = None
        assert first.wait(timeout=15) == 0
        if interrupt_identify:
            assert wait_until(lambda: (tmp_path / "identify-eof").exists(), timeout=5)
        assert wait_until(lambda: (tmp_path / "second-attached").exists() or second.poll() is not None,
                          timeout=60)
        assert (tmp_path / "second-attached").exists(), second.communicate(timeout=5)
        next_identity = query_gateway_control(tmp_path, "identify", timeout=2)
        assert next_identity["instance_id"] != identity["instance_id"]
        assert next_identity["client_owned"] is True
    finally:
        if "second" in locals():
            second.stdin.close()
            second.stdin = None
            second_out, second_err = second.communicate(timeout=15)
            assert second.returncode == 0, (second_out, second_err)
        if first.stdin is not None:
            first.stdin.close()
            first.stdin = None
        out, err = first.communicate(timeout=15)
        assert first.returncode == 0, (out, err)

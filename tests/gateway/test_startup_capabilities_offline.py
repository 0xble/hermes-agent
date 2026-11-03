"""Session creation must not wait for optional image-capability catalog discovery."""
import asyncio
from pathlib import Path
import subprocess
import sys
import time

from websockets.asyncio.client import connect
from websockets.typing import Subprotocol

from hermes_cli.gateway_client import GatewayClient
from tests.e2e.core.providers._native_helpers import make_home
from tests.gateway.test_normal_runtime_boot import control
from tests.waiting import wait_until


_BOOTSTRAP = '''import runpy, socket, threading
entered = threading.Event()
original = socket.getaddrinfo
def resolve(host, *args, **kwargs):
    if host == "models.dev":
        print("CATALOG_DNS_BLOCKED", threading.current_thread().name, flush=True)
        entered.set()
        threading.Event().wait()
        raise OSError("delayed catalog DNS unavailable")
    return original(host, *args, **kwargs)
socket.getaddrinfo = resolve
from agent.models_dev import _start_background_refresh_models_dev
_start_background_refresh_models_dev()
assert entered.wait(10), "catalog warmer did not enter DNS"
runpy.run_module("gateway.run", run_name="__main__")
'''


async def _create(home, descriptor, cwd):
    grant = control(home, "session-ticket", {
        "profile_id": str(home), "instance_id": descriptor["instance_id"], "purpose": "interactive",
    })
    url = descriptor["api_origin"].replace("http:", "ws:") + "/api/ws"
    async with connect(url, subprotocols=[Subprotocol("hermes-gateway-v1"), Subprotocol("hermes-gateway-ticket." + grant["ticket"])]) as ws:
        async with GatewayClient(ws) as client:
            return await asyncio.wait_for(client.rpc(
                "session.create", request_id="cold-catalog", source="cli", cwd=str(cwd),
            ), 15)


def test_session_create_completes_while_catalog_dns_is_blocked(tmp_path):
    home = make_home(tmp_path, {
        "provider": "openai", "default": "gpt-5.5", "base_url": "http://127.0.0.1:1/v1",
    }, env_file={"OPENAI_API_KEY": "loopback-only"}, extra_config={
        "gateway": {"multiplex_profiles": False},
        "browser": {"backend": "browser-use"},
        "agent": {"environment_probe": False},
    })
    root = Path(__file__).resolve().parents[2]
    log_path = tmp_path / "gateway.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([sys.executable, "-c", _BOOTSTRAP], cwd=root, env=home.env(),
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            def ready():
                assert process.poll() is None, log_path.read_text()
                try:
                    descriptor = control(home.hermes_home, "identify")
                    return descriptor if descriptor.get("state") == "ready" else None
                except (OSError, ValueError):
                    return None

            descriptor = wait_until(ready, timeout=60, what="gateway readiness")
            started = time.monotonic()
            created = asyncio.run(_create(home.hermes_home, descriptor, home.project))
            elapsed = time.monotonic() - started
            assert created["session_id"] == created["stored_session_id"]
            assert elapsed < 15, f"session.create waited {elapsed:.1f}s for catalog discovery"
            output = log_path.read_text()
            assert "CATALOG_DNS_BLOCKED models-dev-refresh" in output, output
            print(f"session.create completed in {elapsed:.2f}s with models.dev DNS still blocked")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

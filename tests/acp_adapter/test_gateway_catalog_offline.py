"""Real ACP attachment must not wait for remote model-catalog discovery."""
import asyncio

import pytest

from tests.acp_adapter.test_gateway_sessions import editor, viewer


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("method", ["session/new", "session/load"])
@pytest.mark.asyncio
async def test_cold_attach_reads_catalogs_without_foreground_dns(daemon, tmp_path, monkeypatch, method):
    # The parent process's offline-inventory fixture cannot patch the ACP subprocess.
    # Fail remote resolution immediately there, and distinguish a harmless daemon
    # cache refresh from network work the session/load reply actually waits for.
    bootstrap = '''import runpy, socket, sys, threading
original = socket.getaddrinfo
def resolve(host, *args, **kwargs):
    if host not in ("127.0.0.1", "::1", "localhost", None):
        if not threading.current_thread().daemon:
            print("FOREGROUND_EXTERNAL_DNS", host, file=sys.stderr, flush=True)
            import traceback
            traceback.print_stack(file=sys.stderr)
        raise OSError("remote catalog unavailable")
    return original(host, *args, **kwargs)
socket.getaddrinfo = resolve
runpy.run_module("acp_adapter.entry", run_name="__main__")
'''
    spawn = asyncio.create_subprocess_exec

    async def guarded_spawn(executable, *args, **kwargs):
        assert args == ("-m", "acp_adapter.entry")
        return await spawn(executable, "-c", bootstrap, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded_spawn)
    async with viewer(daemon) as ws:
        created = await ws.rpc("session.create", request_id="offline-acp", source="cli", cwd=str(tmp_path))
        async with editor(daemon, tmp_path) as acp:
            assert "result" in await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
            params = {"cwd": str(tmp_path), "mcpServers": []}
            if method == "session/load":
                params["sessionId"] = created["session_id"]
            loaded = await acp.rpc(method, **params)
            assert "result" in loaded, loaded
            models = loaded["result"]["models"]
            assert models["currentModelId"] in {row["modelId"] for row in models["availableModels"]}
    assert "FOREGROUND_EXTERNAL_DNS" not in (tmp_path / "acp.log").read_text()

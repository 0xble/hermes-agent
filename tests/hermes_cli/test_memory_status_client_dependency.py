"""Status uses Hindsight's operation prerequisite boundary without making a client."""
import builtins
import importlib.metadata
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest

from hermes_cli import memory_setup
from plugins.memory import hindsight


@pytest.mark.parametrize("client", ["missing", "old", "too-new", "broken-import", "supported"])
@pytest.mark.parametrize("mode", ["local_external", "cloud", "local_embedded"])
def test_hindsight_status_reports_client_readiness(tmp_path, monkeypatch, capsys, client, mode):
    home = tmp_path / "home"
    (home / "hindsight").mkdir(parents=True)
    (home / "config.yaml").write_text("memory: {provider: hindsight}\n")
    (home / "hindsight/config.json").write_text(json.dumps(
        {"mode": mode, "api_url": "http://fixture.invalid:9177"}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(hindsight, "_check_local_runtime", lambda: (True, ""))
    # Import is allowed, construction, initialization and network access are not.
    new_cloud_client = hindsight.HindsightMemoryProvider._new_cloud_client
    monkeypatch.setattr(hindsight.HindsightMemoryProvider, "initialize",
                        lambda *a, **kw: pytest.fail("status initialized provider"))
    monkeypatch.setattr(hindsight.HindsightMemoryProvider, "_new_cloud_client",
                        lambda *a, **kw: pytest.fail("status created cloud client"))
    monkeypatch.setattr(hindsight.HindsightMemoryProvider, "_new_embedded_client",
                        lambda *a, **kw: pytest.fail("status started embedded client"))
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("status used network"))
    monkeypatch.setattr("pm.sync_venv", lambda *a, **kw: pytest.fail("status installed dependencies"))
    monkeypatch.setattr(memory_setup, "prepare_memory_provider_dependencies",
                        lambda *a, **kw: pytest.fail("status prepared dependencies"))
    sdk = ModuleType("hindsight_client")
    sdk.Hindsight = lambda *a, **kw: pytest.fail("status constructed SDK client")
    monkeypatch.setitem(sys.modules, "hindsight_client", sdk)
    real_version = importlib.metadata.version

    def version(name):
        if name != "hindsight-client":
            return real_version(name)
        if client == "missing":
            raise importlib.metadata.PackageNotFoundError(name)
        return {"old": "0.5.0", "too-new": "1.0.0"}.get(client, "0.10.3")

    monkeypatch.setattr(importlib.metadata, "version", version)
    real_import = builtins.__import__

    def import_client(name, *args, **kwargs):
        if name == "hindsight_client" and client == "broken-import":
            raise ModuleNotFoundError("fixture client is not importable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_client)
    from hermes_cli.config import load_config
    config = load_config()
    # Warm the CLI's existing registry/config bootstrap before checking the provider
    # probe. Auth reads take an advisory lock even when there are no credentials.
    from hermes_cli.tools_config import _get_platform_tools
    _get_platform_tools(config, "cli", include_default_mcp_servers=False)
    memory_setup._get_available_providers()
    before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    memory_setup.memory_command(SimpleNamespace(memory_command="status"))
    out = capsys.readouterr().out
    if client == "supported":
        assert "Status:    available ✓" in out
    else:
        assert "Status:    not available ✗" in out
        assert "Status:    available ✓" not in out
        assert "Hindsight client" in out and "hermes pm" in out
        assert "Reason:" in out
        assert "Missing:" not in out  # a missing SDK is not a missing API key
        with pytest.raises(ImportError, match="Hindsight client"):
            new_cloud_client(hindsight.HindsightMemoryProvider())
    assert {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before

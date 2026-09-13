"""A named browser-use invocation must prove its authenticated route before execution."""
import builtins
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import browser_tool, browser_use_cli


@pytest.fixture
def admission(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "managed"))
    (tmp_path / "config.yaml").write_text(json.dumps({"browser": {
        "use_real_profile": True,
        "real_profile_identities": {"work": {"browser": "chrome", "source_profile": "Default"}},
    }}))
    monkeypatch.setattr(browser_use_cli, "_find_cli", lambda: ["fixture-browser-use"])
    monkeypatch.setattr(browser_use_cli, "_base_subprocess_env", lambda: {
        browser_use_cli._REAL_PROFILE_SENTINEL: "1"})  # Inherited proof must be discarded.
    monkeypatch.setattr(browser_use_cli, "_attach_vault_supervisor", lambda *args: None)
    monkeypatch.setattr(browser_tool, "_get_cdp_override_raw", lambda: None)
    monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: None)
    backend = Mock(return_value=None)
    execute = Mock(return_value=SimpleNamespace(returncode=0, stdout="fixture", stderr=""))
    bind = Mock(return_value=(None, None))
    monkeypatch.setattr(browser_use_cli, "_resolve_backend_cdp", backend)
    monkeypatch.setattr(browser_use_cli, "_bind_browser_exec_identity", bind)
    monkeypatch.setattr(browser_use_cli, "_run_cli_killing_process_group", execute)
    return backend, bind, execute


@pytest.mark.parametrize("failure", ["import", "empty_cdp", "noop_route"])
def test_named_exec_never_reaches_fallback_or_binding_without_route_proof(admission, monkeypatch, failure):
    backend, bind, execute = admission
    if failure == "import":
        original = builtins.__import__

        def broken(name, *args, **kwargs):
            if name == "tools.browser_tool" and len(args) > 2 and "_real_profile_cdp" in args[2]:
                raise ImportError("fixture facade initialization failed")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", broken)
    elif failure == "empty_cdp":
        monkeypatch.setattr(browser_tool, "_real_profile_cdp", lambda *args, **kwargs: (None, None))
    else:
        monkeypatch.setattr(browser_use_cli, "_resolve_real_profile_cdp", lambda *args, **kwargs: None)
    result = json.loads(browser_use_cli._browser_exec("print(1)", identity="work", session="refused"))
    assert not result.get("success", False)
    assert "real-profile" in result["error"]
    backend.assert_not_called()
    bind.assert_not_called()
    execute.assert_not_called()


def test_named_exec_with_actual_route_proof_reaches_execution(admission, monkeypatch):
    backend, bind, execute = admission
    monkeypatch.setattr(browser_tool, "_real_profile_cdp", lambda *args, **kwargs: ("ws://127.0.0.1:9999", None))
    result = json.loads(browser_use_cli._browser_exec("print(1)", identity="work", session="accepted"))
    assert result["success"] and result["identity"] == "work"
    backend.assert_called_once()
    bind.assert_called_once()
    execute.assert_called_once()
    assert execute.call_args.args[2]["BU_CDP_WS"] == "ws://127.0.0.1:9999"

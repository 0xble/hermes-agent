"""Real config reads must fail closed before navigation can select a cookie jar."""
import builtins
import json
from unittest.mock import Mock

import pytest

from hermes_cli import config
from hermes_cli.browser_identity import BrowserIdentityError, resolve_browser_identity
from tests.tools.test_browser_identity import _browser_cfg
from tools import browser_tool as bt


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    monkeypatch.setattr(config, "_RAW_CONFIG_CACHE", {})
    monkeypatch.setattr(bt, "_active_sessions", {})
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_read_browser_identity_binding", lambda task: None)
    # Exercise admission only: even a fail-open regression cannot start a browser.
    monkeypatch.setattr(bt, "_get_session_info", Mock(side_effect=AssertionError("browser started")))
    return path


def resolve(entrypoint):
    if entrypoint == "policy":
        return resolve_browser_identity(None)
    return bt._resolve_navigation_identity("policy-test", None)


@pytest.mark.parametrize("entrypoint", ["policy", "navigation"])
@pytest.mark.parametrize("text", ["browser: [", "- not-a-mapping\n", "false\n", "[]\n"])
@pytest.mark.parametrize("warm_tolerant_cache", [False, True])
def test_invalid_real_config_never_becomes_legacy(policy_file, entrypoint, text, warm_tolerant_cache):
    policy_file.write_text(text)
    if warm_tolerant_cache:
        assert config.read_raw_config() == {}
        if text != "browser: [":
            assert str(policy_file) in config._RAW_CONFIG_CACHE
    with pytest.raises(BrowserIdentityError, match="could not read browser identity configuration"):
        resolve(entrypoint)


@pytest.mark.parametrize("entrypoint", ["policy", "navigation"])
@pytest.mark.parametrize("operation", ["stat", "open"])
def test_real_reader_io_failure_bypasses_tolerant_cache(policy_file, monkeypatch, entrypoint, operation):
    policy_file.write_text("{}")
    assert config.read_raw_config() == {}
    assert str(policy_file) in config._RAW_CONFIG_CACHE
    if operation == "stat":
        original = type(policy_file).stat

        def denied(path, *args, **kwargs):
            if path == policy_file:
                raise PermissionError("fixture unreadable policy")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(type(policy_file), "stat", denied)
    else:
        def denied(path, *args, **kwargs):
            if path == policy_file:
                raise PermissionError("fixture unreadable policy")
            return builtins.open(path, *args, **kwargs)

        monkeypatch.setattr(config, "open", denied, raising=False)
    with pytest.raises(BrowserIdentityError, match="fixture unreadable policy"):
        resolve(entrypoint)


@pytest.mark.parametrize("entrypoint", ["policy", "navigation"])
@pytest.mark.parametrize("text", [None, "{}", ""])
def test_absent_or_empty_policy_preserves_legacy(policy_file, entrypoint, text):
    if text is not None:
        policy_file.write_text(text)
    assert resolve(entrypoint) is None


@pytest.mark.parametrize("entrypoint", ["policy", "navigation"])
def test_intact_required_identity_rejects_omission(policy_file, entrypoint):
    policy_file.write_text(json.dumps({"browser": _browser_cfg(required=True)}))
    with pytest.raises(BrowserIdentityError, match="identity is required"):
        resolve(entrypoint)


def test_named_navigation_binding_still_precedes_default(policy_file, monkeypatch):
    policy_file.write_text(json.dumps({"browser": _browser_cfg(required=True)}))
    monkeypatch.setattr(bt, "_read_browser_identity_binding", lambda task: ("lpg", "fixture-key"))
    resolved = bt._resolve_navigation_identity("bound", None)
    assert resolved is not None and resolved.alias == "lpg"
    policy_file.write_text("browser: [")
    with pytest.raises(BrowserIdentityError, match="could not read browser identity configuration"):
        bt._resolve_navigation_identity("bound", None)

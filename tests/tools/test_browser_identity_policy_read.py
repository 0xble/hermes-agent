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
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "managed"))
    monkeypatch.setattr(bt, "_active_sessions", {})
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_read_browser_identity_binding", lambda task: None)
    # Exercise admission only: even a fail-open regression cannot start a browser.
    monkeypatch.setattr(bt, "_get_session_info", Mock(side_effect=AssertionError("browser started")))
    return path


def resolve(entrypoint, requested=None):
    if entrypoint == "policy":
        return resolve_browser_identity(requested)
    return bt._resolve_navigation_identity("policy-test", requested)


@pytest.mark.parametrize("entrypoint", ["policy", "navigation"])
@pytest.mark.parametrize("user_policy", ["{}", "browser:\n  require_identity: false\n  real_profile_identities:\n    work:\n      browser: chrome\n      source_profile: User\n"])
def test_managed_identity_policy_wins_and_reloads(policy_file, entrypoint, user_policy):
    managed = policy_file.parent / "managed" / "config.yaml"
    managed.parent.mkdir()
    policy_file.write_text(user_policy, encoding="utf-8")
    managed.write_text(
        "browser:\n  require_identity: true\n  real_profile_identities:\n"
        "    work:\n      browser: chrome\n      source_profile: Managed\n",
        encoding="utf-8",
    )
    with pytest.raises(BrowserIdentityError, match="identity is required"):
        resolve(entrypoint)
    assert resolve(entrypoint, "work").source_profile == "Managed"
    managed.write_text(
        "browser:\n  require_identity: true\n  real_profile_identities:\n"
        "    work:\n      browser: chrome\n      source_profile: Updated\n",
        encoding="utf-8",
    )
    assert resolve(entrypoint, "work").source_profile == "Updated"


@pytest.mark.parametrize("entrypoint", ["policy", "navigation"])
@pytest.mark.parametrize("layer", ["user", "managed"])
@pytest.mark.parametrize("damage", ["browser: [", "[]", "false", "browser: []", "browser:\n  require_identity: nope", "browser:\n  real_profile_identities: []", "stat", "open"])
def test_effective_policy_damage_never_uses_warm_cache(policy_file, monkeypatch, entrypoint, layer, damage):
    managed = policy_file.parent / "managed" / "config.yaml"
    managed.parent.mkdir()
    policy_file.write_text("{}", encoding="utf-8")
    managed.write_text("{}", encoding="utf-8")
    assert resolve(entrypoint) is None
    config.load_config_readonly()  # Even a warm permissive effective cache cannot authorize.
    target = policy_file if layer == "user" else managed
    if damage == "stat":
        original = type(target).stat

        def denied(path, *args, **kwargs):
            if path == target:
                raise PermissionError("fixture unreadable layer")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(type(target), "stat", denied)
    elif damage == "open":
        def denied(path, *args, **kwargs):
            if path == target:
                raise PermissionError("fixture unreadable layer")
            return builtins.open(path, *args, **kwargs)

        monkeypatch.setattr(config, "open", denied, raising=False)
    else:
        target.write_text(damage, encoding="utf-8")
    with pytest.raises(BrowserIdentityError):
        resolve(entrypoint)


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

"""Gateway reaction tool exposure follows adapter capabilities without construction."""

from types import SimpleNamespace

import pytest

from gateway.platform_registry import PlatformEntry, PlatformRegistry
from gateway.platforms.base import BasePlatformAdapter


class _UnsupportedAdapter(BasePlatformAdapter):
    def __init__(self, config):
        raise AssertionError("capability inspection must not construct adapters")


class _SupportedAdapter(BasePlatformAdapter):
    async def add_reaction(self, chat_id, message_id, emoji, **routing):
        return True


def _register(registry, name, adapter_class):
    registry.register(PlatformEntry(
        name=name, label=name, adapter_factory=adapter_class, adapter_class=adapter_class,
        check_fn=lambda: True,
    ))


def test_reaction_capability_is_class_based_and_does_not_instantiate():
    registry = PlatformRegistry()
    _register(registry, "unsupported", _UnsupportedAdapter)
    _register(registry, "supported", _SupportedAdapter)

    assert registry.reactions_supported("unsupported") is False
    assert registry.reactions_supported("supported") is True


def test_gateway_gate_drops_known_unsupported_and_keeps_explicit_disable(monkeypatch):
    import gateway.platform_registry as platforms
    from toolsets import session_disabled_toolsets

    registry = PlatformRegistry()
    _register(registry, "unsupported", _UnsupportedAdapter)
    monkeypatch.setattr(platforms, "platform_registry", registry)

    assert "reactions" in (session_disabled_toolsets([], "unsupported") or [])
    assert (session_disabled_toolsets(["reactions"], "unsupported") or []).count("reactions") == 1


def test_pin_carry_keeps_react_only_for_the_capability_gate(monkeypatch):
    """A pinned react survives restore when only adapter capability removed it, never when config,
    or the desktop opt-in, disabled it."""
    import gateway.platform_registry as platforms
    from tools.mcp_tool_agent import _config_permitted_names

    registry = PlatformRegistry()
    _register(registry, "unsupported", _UnsupportedAdapter)
    _register(registry, "supported", _SupportedAdapter)
    monkeypatch.setattr(platforms, "platform_registry", registry)

    def agent(platform, configured):
        # The session's resolved disables always include "reactions" here: from capability, config or opt-in.
        disabled = list(dict.fromkeys([*configured, "reactions"]))
        return SimpleNamespace(platform=platform, enabled_toolsets=None, disabled_toolsets=disabled,
                               _configured_disabled_toolsets=list(configured))

    assert "react" in _config_permitted_names(agent("unsupported", []))
    assert "react" not in _config_permitted_names(agent("unsupported", ["reactions"]))
    assert "react" not in _config_permitted_names(agent("supported", ["reactions"]))
    assert "react" not in _config_permitted_names(agent("local", []))
    assert "react" not in _config_permitted_names(agent(None, []))


def test_inherited_supports_reactions_flag_counts():
    class _Declared(_UnsupportedAdapter):
        SUPPORTS_REACTIONS = True

    class _Inherits(_Declared):
        pass

    registry = PlatformRegistry()
    _register(registry, "inherits", _Inherits)
    assert registry.reactions_supported("inherits") is True


def test_unknown_adapter_fails_open(monkeypatch):
    import gateway.platform_registry as platforms
    from toolsets import session_disabled_toolsets

    registry = PlatformRegistry()
    registry.register(PlatformEntry(
        name="deferred", label="deferred", adapter_factory=lambda config: None, check_fn=lambda: True,
    ))
    monkeypatch.setattr(platforms, "platform_registry", registry)

    assert "reactions" not in (session_disabled_toolsets([], "deferred") or [])


@pytest.mark.parametrize(
    "module_name, expected",
    [
        ("plugins.platforms.email.adapter", False),
        ("plugins.platforms.sms.adapter", False),
        ("plugins.platforms.telegram.adapter", True),
        ("plugins.platforms.slack.adapter", True),
    ],
)
def test_real_plugin_registration_exposes_adapter_capability(module_name, expected):
    module = __import__(module_name, fromlist=["register"])
    captured = []
    module.register(SimpleNamespace(register_platform=lambda **kwargs: captured.append(kwargs)))
    assert captured, module_name
    entry = PlatformEntry(**captured[0])
    registry = PlatformRegistry()
    registry.register(entry)
    assert registry.reactions_supported(entry.name) is expected

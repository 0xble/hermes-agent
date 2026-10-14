"""Python pre_tool_call hook applicability must bypass timeout suppression."""

from __future__ import annotations

import threading
import time

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest, _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE


def _register_hook(manager, callback, *, tools=None):
    context = PluginContext(PluginManifest(name="test-plugin"), manager)
    context.register_hook("pre_tool_call", callback, tools=tools)


def test_hung_matched_callback_fails_closed_for_its_declared_tool(monkeypatch):
    import hermes_cli.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 0.05)
    never = threading.Event()

    def hung(**_kwargs):
        never.wait(5)

    manager = PluginManager()
    _register_hook(manager, hung, tools={"terminal"})

    started = time.monotonic()
    result = manager.invoke_hook("pre_tool_call", tool_name="terminal", args={}, tool_call_id="matched")

    assert time.monotonic() - started < 1
    assert result == [{"action": "block", "message": _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE}]


def test_hung_matched_callback_does_not_block_unmatched_tool_during_suppression(monkeypatch):
    import hermes_cli.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 0.05)
    never = threading.Event()

    def hung(**_kwargs):
        never.wait(5)

    manager = PluginManager()
    _register_hook(manager, hung, tools={"terminal"})

    assert manager.invoke_hook("pre_tool_call", tool_name="terminal", args={}, tool_call_id="first")
    started = time.monotonic()
    result = manager.invoke_hook("pre_tool_call", tool_name="read_file", args={}, tool_call_id="unmatched")

    assert time.monotonic() - started < 0.1
    assert result == []
    assert manager._hook_timeout_suppressed_until


def test_undeclared_callback_keeps_gating_every_tool(monkeypatch):
    import hermes_cli.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 0.05)
    never = threading.Event()

    def hung(**_kwargs):
        never.wait(5)

    manager = PluginManager()
    _register_hook(manager, hung)

    assert manager.invoke_hook("pre_tool_call", tool_name="terminal", args={}, tool_call_id="first")
    assert manager.invoke_hook("pre_tool_call", tool_name="read_file", args={}, tool_call_id="second") == [
        {"action": "block", "message": _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE},
    ]


def test_tool_matcher_accepts_predicate(monkeypatch):
    import hermes_cli.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 0)
    seen = []

    def callback(tool_name, **_kwargs):
        seen.append(tool_name)

    manager = PluginManager()
    _register_hook(manager, callback, tools=lambda name: name == "terminal")

    assert manager.invoke_hook("pre_tool_call", tool_name="read_file", args={}) == []
    assert manager.invoke_hook("pre_tool_call", tool_name="terminal", args={}) == []
    assert seen == ["terminal"]

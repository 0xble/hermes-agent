"""Python pre_tool_call hook applicability must bypass timeout suppression."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

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


@pytest.mark.parametrize("hook_name", ["pre_llm_call", "transform_terminal_output", "unknown_hook"])
def test_tool_matcher_rejects_hooks_without_tool_name(hook_name):
    manager = PluginManager()
    context = PluginContext(PluginManifest(name="test-plugin"), manager)
    with pytest.raises(ValueError, match="tools=.*tool_name"):
        context.register_hook(hook_name, lambda **kwargs: None, tools={"terminal"})
    assert not manager.has_hook(hook_name)


@pytest.mark.parametrize("tools", ["terminal", {"terminal", 1}, [None]])
def test_tool_matcher_rejects_invalid_names(tools):
    manager = PluginManager()
    with pytest.raises(TypeError):
        _register_hook(manager, lambda **kwargs: None, tools=tools)
    assert not manager.has_hook("pre_tool_call")


@pytest.mark.parametrize("hook_name", ["pre_tool_call", "post_tool_call", "transform_tool_result"])
@pytest.mark.parametrize("async_delivery", [False, True])
@pytest.mark.parametrize("predicate", [False, True])
def test_tool_matcher_filters_sync_and_async_delivery(monkeypatch, hook_name, async_delivery, predicate):
    import hermes_cli.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 0)
    manager = PluginManager()
    context = PluginContext(PluginManifest(name="test-plugin"), manager)
    tools = (lambda name: name == "terminal") if predicate else {"terminal"}
    seen = []

    async def callback(tool_name):
        seen.append(tool_name)
        return "matched"

    context.register_hook(hook_name, callback, tools=tools)
    for name, expected in [("read_file", []), ("terminal", ["matched"])]:
        result = (asyncio.run(manager.ainvoke_hook(hook_name, tool_name=name)) if async_delivery
                  else manager.invoke_hook(hook_name, tool_name=name))
        assert result == expected
    assert seen == ["terminal"]


@pytest.mark.parametrize("async_delivery", [False, True])
def test_raising_tool_matcher_still_runs_policy_callback(monkeypatch, caplog, async_delivery):
    import hermes_cli.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "_resolve_hook_callback_timeout", lambda: 0)

    def broken_matcher(_name):
        raise RuntimeError("broken matcher")

    manager = PluginManager()
    directive = {"action": "block", "message": "policy enforced"}
    _register_hook(manager, lambda tool_name: directive, tools=broken_matcher)
    result = (asyncio.run(manager.ainvoke_hook("pre_tool_call", tool_name="terminal")) if async_delivery
              else manager.invoke_hook("pre_tool_call", tool_name="terminal"))
    assert result == [directive]
    assert "treating the tool as matched" in caplog.text

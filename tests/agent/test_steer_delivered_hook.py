"""Delivered-steer observer contract at both native insertion boundaries."""
from __future__ import annotations

from uuid import UUID
import threading

import pytest

from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
from agent.turn_context import _bind_turn_identity
from agent.turn_iteration_prep import _inject_steer_after_newest_tool_result
from hermes_cli import lifecycle
from run_agent import AIAgent


def _agent():
    agent = object.__new__(AIAgent)
    agent.session_id = "session-a"
    agent.platform = "telegram"
    agent._pending_steer = None
    agent._pending_steer_lock = threading.Lock()
    _bind_turn_identity(agent, "task", None, None, None, None)
    return agent


def _deliver(route, agent, messages):
    if route == "post-tool":
        apply_pending_steer_to_tool_results(agent, messages, 1)
    else:
        text = agent._drain_pending_steer()
        if text:
            _inject_steer_after_newest_tool_result(agent, messages, text)


@pytest.mark.parametrize("route", ["post-tool", "pre-api"])
def test_reports_insertions_once_with_turn_identity_and_verbatim_batch(monkeypatch, route):
    agent = _agent()
    events = []
    messages = [{"role": "tool", "content": "result"}]

    def observe(name, **payload):
        assert name == "post_steer_delivered"
        assert messages[-1]["display_kind"] == "steer"  # only AFTER insertion
        events.append(payload)
        return [{"action": "rewrite", "text": "ignored"}]

    monkeypatch.setattr(lifecycle, "invoke_hook", observe)
    for n in range(2):
        if n:
            messages.append({"role": "tool", "content": "next result"})
        agent.steer("  first\nline  ")
        agent.steer("second")
        _deliver(route, agent, messages)
        _deliver(route, agent, messages)  # draining again is not a delivery
    assert [e["text"] for e in events] == ["first\nline\nsecond"] * 2
    assert [e["ordinal"] for e in events] == [1, 2]
    assert all(e["session_id"] == agent.session_id and e["turn_id"] == agent._current_turn_id
               and e["platform"] == agent.platform for e in events)
    assert len({e["delivery_id"] for e in events}) == 2
    assert all(UUID(e["delivery_id"]).version == 4 for e in events)
    assert "ignored" not in messages[-1]["content"]
    previous_turn = agent._current_turn_id
    _bind_turn_identity(agent, "task", None, None, None, None)
    messages.append({"role": "tool", "content": "new turn result"})
    agent.steer("third")
    _deliver(route, agent, messages)
    assert events[-1]["ordinal"] == 1 and events[-1]["turn_id"] != previous_turn


@pytest.mark.parametrize("route", ["post-tool", "pre-api"])
def test_requeue_is_silent_and_hook_failure_does_not_undo_delivery(monkeypatch, caplog, route):
    agent = _agent()
    calls = []

    def broken(name, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("observer unavailable")

    monkeypatch.setattr(lifecycle, "invoke_hook", broken)
    messages = [{"role": "assistant", "content": "no tool"}]
    agent.steer("fallback")
    _deliver(route, agent, messages)
    assert not calls and agent._pending_steer == "fallback"
    # A next-turn fallback drains text as ordinary input; no delivery observer.
    assert agent._drain_pending_steer() == "fallback"
    assert not calls
    agent.steer("delivered despite broken observer")
    messages.append({"role": "tool", "content": "result"})
    _deliver(route, agent, messages)
    assert len(calls) == 1 and calls[0]["ordinal"] == 1
    assert messages[-1]["display_kind"] == "steer"
    assert "delivered despite broken observer" in messages[-1]["content"]
    assert agent._pending_steer is None
    assert "post_steer_delivered" in caplog.text


@pytest.mark.parametrize("route", ["post-tool", "pre-api"])
def test_slow_plugin_is_bounded_without_undoing_delivery(monkeypatch, caplog, route):
    import contextvars
    from hermes_cli import plugins

    agent = _agent()
    messages = [{"role": "tool", "content": "result"}]
    started = threading.Event()
    release = threading.Event()
    callback_finished = threading.Event()
    observed = []

    def slow(text, **kwargs):
        assert messages[-1]["display_kind"] == "steer"
        started.set()
        try:
            release.wait(timeout=10.0)
            return {"action": "rewrite", "text": "ignored"}
        finally:
            callback_finished.set()

    manager = plugins.PluginManager()
    ctx = plugins.PluginContext(plugins.PluginManifest(name="steer-observer"), manager)
    ctx.register_hook("post_steer_delivered", slow)
    ctx.register_hook("post_steer_delivered", lambda **payload: observed.append(payload))
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0.1)
    agent.steer("continue despite slow observer")
    context = contextvars.copy_context()
    caller = threading.Thread(target=context.run, args=(_deliver, route, agent, messages))
    caller.start()
    try:
        assert started.wait(timeout=2.0)
        caller.join(timeout=2.0)
        assert not caller.is_alive(), "slow plugin stalled the steer delivery caller"
        assert not callback_finished.is_set()  # delivery proceeded while the plugin was blocked
        assert len(observed) == 1 and observed[0]["text"] == "continue despite slow observer"
        assert messages[-1]["display_kind"] == "steer"
        assert "continue despite slow observer" in messages[-1]["content"]
        assert "ignored" not in messages[-1]["content"]
        assert agent._pending_steer is None
        assert "post_steer_delivered" in caplog.text and "timed out" in caplog.text
    finally:
        release.set()
        caller.join(timeout=2.0)
        assert callback_finished.wait(timeout=2.0)


@pytest.mark.parametrize("route", ["post-tool", "pre-api"])
def test_missing_turn_and_platform_use_empty_string_fallbacks(monkeypatch, route):
    agent = _agent()
    del agent._current_turn_id
    del agent.platform
    events = []
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **payload: events.append(payload))
    messages = [{"role": "tool", "content": "result"}]
    agent.steer("fallback identities")
    _deliver(route, agent, messages)
    assert len(events) == 1
    assert events[0]["turn_id"] == events[0]["platform"] == ""
    assert events[0]["session_id"] == agent.session_id

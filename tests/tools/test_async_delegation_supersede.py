"""A background unit its owner re-dispatched elsewhere is recorded but never re-enters the parent.

Regression: a rate-limited reviewer child was retried on the next route from its ``subagent_stop`` hook, yet the
failed attempt's completion still woke the parent, which then narrated the rate limit and the retry before the
replacement's own result arrived.
"""

import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from hermes_cli import plugins
from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def _clean_state():
    original = plugins._plugin_manager
    plugins._plugin_manager = plugins.PluginManager()
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    plugins._plugin_manager = original


def _drain(timeout=3.0):
    events, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        while not process_registry.completion_queue.empty():
            events.append(process_registry.completion_queue.get_nowait())
        if events and not ad.active_count():
            break
        time.sleep(0.02)
    return events


def _background_parent(monkeypatch, outcomes):
    """delegate_task wired to fake children whose status comes from ``outcomes[goal]``."""
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None

    def child(task_index, goal, child=None, parent_agent=None, **kw):
        status = outcomes[goal]
        return {"task_index": task_index, "status": status, "summary": f"{status}: {goal}", "api_calls": 1,
                "duration_seconds": 0.1, "model": "m", "exit_reason": status}

    def build(**kw):
        c = MagicMock()
        c._delegate_role = "leaf"
        c.session_id = f"child-{kw['task_index']}-{time.monotonic_ns()}"
        return c

    creds = {"model": "m", "provider": None, "base_url": None, "api_key": None, "api_mode": None, "command": None,
             "args": None}
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    monkeypatch.setattr(dt, "_build_child_agent", build)
    monkeypatch.setattr(dt, "_run_single_child", child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    return dt, parent


def test_unit_replaced_from_its_stop_hook_reports_only_through_the_replacement(monkeypatch):
    dt, parent = _background_parent(monkeypatch, {"review on route A": "failed", "review on route B": "completed"})
    first: dict = {}
    replacements = []

    def retry_elsewhere(child_status=None, **_):
        # The owner's recovery, as review-candidate does it: re-dispatch, then mark the failed unit replaced.
        if child_status != "failed":
            return
        handle = json.loads(dt.delegate_task(goal="review on route B", background=True, parent_agent=parent))
        replacements.append(handle["delegation_id"])
        first["superseded"] = ad.supersede_delegation(first["id"], handle["delegation_id"], "route A rate-limited")

    plugins.get_plugin_manager()._hooks.setdefault("subagent_stop", []).append(retry_elsewhere)
    gate = threading.Event()
    real_run = dt._run_single_child
    monkeypatch.setattr(dt, "_run_single_child", lambda *a, **k: (gate.wait(5), real_run(*a, **k))[1])
    first["id"] = json.loads(dt.delegate_task(goal="review on route A", background=True, parent_agent=parent))[
        "delegation_id"]
    gate.set()

    events = _drain()
    assert first["superseded"] is True
    assert [e["delegation_id"] for e in events] == replacements  # the parent is woken once, by the replacement
    assert events[0]["results"][0]["status"] == "completed"
    row = ad.get_durable_delegation(first["id"])
    assert row["state"] != "running" and row["delivery_state"] == "superseded"  # recorded, never replayed
    import queue
    replay = queue.Queue()
    ad.restore_undelivered_completions(replay)  # after a restart, only the replacement is still owed to the parent
    assert [replay.get_nowait()["delegation_id"] for _ in range(replay.qsize())] == replacements


def test_supersede_is_refused_when_the_parent_could_lose_the_outcome(monkeypatch):
    gate = threading.Event()

    def dispatch(goals, session_key="k"):
        return ad.dispatch_async_delegation_batch(
            goals=goals, context=None, toolsets=None, role="leaf", model="m", session_key=session_key,
            max_async_children=8,
            runner=lambda: (gate.wait(5), {"results": [{"task_index": i, "status": "failed"} for i in
                                                        range(len(goals))]})[1])["delegation_id"]

    single, pair, other = dispatch(["one"]), dispatch(["a", "b"]), dispatch(["replacement"])
    elsewhere = dispatch(["replacement"], session_key="another parent")
    assert ad.supersede_delegation(single, "deleg_unknown") is False  # no admitted replacement
    assert ad.supersede_delegation(single, single) is False
    assert ad.supersede_delegation(single, elsewhere) is False  # would report to a different parent
    assert ad.supersede_delegation(pair, other) is False  # would hide a sibling's result
    gate.set()
    assert {e["delegation_id"] for e in _drain()} == {single, pair, other, elsewhere}
    assert ad.supersede_delegation(single, other) is False  # already reported

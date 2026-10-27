"""Turn-scoped deferral dispatch, caching, concurrency and native compression contracts."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.steer_deferral import (
    defer_message, offer_auto_steer, preserve_steer_receipts, register_steer,
    requeue_undelivered_steers,
)


def agent():
    return SimpleNamespace(_current_turn_id="turn", redirect=Mock(return_value=True),
                           steer=Mock(return_value=True), _supports_active_turn_redirect=True)


def test_tool_schema_is_stable_before_during_and_after_steer():
    from model_tools import get_tool_definitions
    from agent.inline_tool_executors import _defer_message
    a = agent()
    before = copy.deepcopy(get_tool_definitions(["steering"], quiet_mode=True))
    assert [t["function"]["name"] for t in before] == ["defer_message"]
    assert json.loads(_defer_message(a, {"message_id": "unknown"}, None))["accepted"] is False
    key, _ = register_steer(a, "question", lambda: True)
    assert get_tool_definitions(["steering"], quiet_mode=True) == before
    assert json.loads(_defer_message(a, {"message_id": key}, None))["accepted"]
    assert get_tool_definitions(["steering"], quiet_mode=True) == before


def test_concurrent_duplicate_deferrals_commit_once():
    a = agent()
    queue = Mock(return_value=True)
    key, _ = register_steer(a, "question", queue)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: defer_message(a, {"message_id": key}), range(24)))
    assert all(r["accepted"] and r["status"] == "deferred" for r in results)
    queue.assert_called_once_with()


def test_handoff_exception_keeps_pending_receipt():
    a = agent()
    queue = Mock(side_effect=RuntimeError("full"))
    key, _ = register_steer(a, "question", queue)
    assert not defer_message(a, {"message_id": key})["accepted"]
    compressed = [{"role": "user", "content": "summary"}]
    preserve_steer_receipts(a, compressed)
    assert key in compressed[0]["content"] and '"status": "actionable"' in compressed[0]["content"]
    queue.side_effect = None
    queue.return_value = True
    assert defer_message(a, {"message_id": key})["accepted"]


def test_late_auto_steers_return_to_original_host_queue_once():
    a = agent()
    first, second = Mock(return_value=True), Mock(return_value=True)
    _, first_wrapper = register_steer(a, "first", first)
    _, second_wrapper = register_steer(a, "second", second)
    pending = f"explicit steer\n{first_wrapper}\n{second_wrapper}"
    assert requeue_undelivered_steers(a, pending) == "explicit steer"
    assert requeue_undelivered_steers(a, pending) == "explicit steer"
    first.assert_called_once_with()
    second.assert_called_once_with()


def test_consumed_auto_steer_is_not_replayed_by_finalizer():
    a = agent()
    queue = Mock(return_value=True)
    register_steer(a, "already delivered", queue)
    assert requeue_undelivered_steers(a, "explicit steer") == "explicit steer"
    queue.assert_not_called()


@pytest.mark.parametrize("stopped", [False, True])
def test_receipts_do_not_cross_turn_or_stop(stopped):
    a = agent()
    queue = Mock(return_value=True)
    key, _ = register_steer(a, "question", queue)
    if stopped:
        a._interrupt_requested = True
    else:
        a._current_turn_id = "next-turn"
    assert not defer_message(a, {"message_id": key})["accepted"]
    queue.assert_not_called()


def test_multi_steer_compression_retains_pending_and_deferred_ids_without_prefix_edit():
    a = agent()
    first, wrapper = register_steer(a, "unrelated A", lambda: True)
    second, _ = register_steer(a, "unrelated B", lambda: True)
    transcript = [{"role": "tool", "content": "result\n" + wrapper}]
    old = copy.deepcopy(transcript)
    assert defer_message(a, {"message_id": second})["accepted"]
    assert transcript == old  # Deferred state is a new tool result, not a prefix rewrite.
    compressed = [{"role": "user", "content": "summary"}, {"role": "assistant", "content": "tail"}]
    preserve_steer_receipts(a, compressed)
    assert first in compressed[0]["content"] and second in compressed[0]["content"]
    assert '"status": "deferred"' in compressed[0]["content"]
    assert defer_message(a, {"message_id": first})["accepted"]


@pytest.mark.parametrize("reject", [False, True])
def test_redirect_rejection_or_exception_falls_back_once(reject):
    a = agent()
    a.redirect.side_effect = RuntimeError("unsupported") if reject else None
    a.redirect.return_value = False
    assert offer_auto_steer(a, "question", lambda: True)
    a.redirect.assert_called_once()
    a.steer.assert_called_once()
    assert a.redirect.call_args == a.steer.call_args


def test_final_auto_admission_close_does_not_carry_into_next_turn():
    from agent.steer_deferral import close_auto_steer

    a = agent()
    close_auto_steer(a)
    assert not offer_auto_steer(a, "late follow-up", lambda: True)
    a.redirect.assert_not_called()
    a.steer.assert_not_called()
    a._current_turn_id = "next-turn"
    assert offer_auto_steer(a, "new correction", lambda: True)
    a.redirect.assert_called_once()


def test_both_reject_discards_receipt_and_does_not_interrupt():
    a = agent()
    a.redirect.return_value = a.steer.return_value = False
    assert not offer_auto_steer(a, "question", lambda: True)
    assert a._auto_steer_receipts == {}


@pytest.mark.parametrize("text", ["related correction", "  related correction\n\n  "])
def test_absorbed_native_steer_is_drained_once_never_replayed(monkeypatch, text):
    import threading
    from agent.interrupt_control import InterruptControlMixin
    from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
    from hermes_cli import lifecycle

    events = []
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **payload: events.append(payload))
    a = InterruptControlMixin()
    a._current_turn_id = "turn"
    a._pending_steer = None
    a._pending_steer_lock = threading.Lock()
    a.session_id = "sid"
    a.platform = "telegram"
    queue = Mock(return_value=True)
    key, wrapper = register_steer(a, text, queue)
    assert a.steer(wrapper)
    messages = [{"role": "tool", "content": "result"}]
    apply_pending_steer_to_tool_results(a, messages, 1)
    assert key in messages[-1]["content"]
    assert len(events) == 1
    assert events[0]["auto_messages"] == [{"message_id": key, "text": text}]
    assert events[0]["manual_text"] == ""
    assert a._drain_pending_steer() is None
    assert a._drain_pending_steer() is None
    queue.assert_not_called()

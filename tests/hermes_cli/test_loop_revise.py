"""Versioned agent-authorized /loop revisions and cross-surface cache safety."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import patch


def _manager(tmp_path, monkeypatch, sid="rev"):
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import goals
    monkeypatch.setattr("hermes_state_dbfile._iter_darwin_fd_targets", lambda: iter(()))
    goals._DB_CACHE.clear()
    from hermes_cli.loops import LoopManager
    return LoopManager(sid)


def test_state_version_round_trip_and_old_json(tmp_path, monkeypatch):
    from hermes_cli.loops import LoopState

    old = LoopState.from_json('{"prompt":"watch"}')
    assert old.revisions == []
    assert old.version == 1
    state = LoopState(prompt="watch", revisions=[{"reason": "slower"}])
    loaded = LoopState.from_json(state.to_json())
    assert loaded.revisions == [{"reason": "slower"}]
    assert loaded.version == 2


def test_less_active_revision_preserves_lifecycle_and_reanchors(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, monkeypatch)
    state = mgr.set("watch", interval_seconds=300, times=5, route={"platform": "t"})
    created = state.created_at
    state.ticks_fired = 2
    state.last_fired_at = time.time() - 10
    state.awaiting_response = False
    result = mgr.revise(reason="reduce polling", interval_seconds=600, times=3)
    assert result["ok"] is True
    assert result["version"] == 2
    assert mgr.state.prompt == "watch"
    assert mgr.state.ticks_fired == 2
    assert mgr.state.created_at == created
    assert mgr.state.route == {"platform": "t"}
    assert mgr.state.interval_seconds == 600
    assert mgr.state.next_due_at >= state.last_fired_at + 600
    assert set(result["revision"]["before"]) == {"interval_seconds", "current_delay", "times"}


def test_authority_branches(tmp_path, monkeypatch):
    from hermes_cli.loops import LoopManager

    mgr = _manager(tmp_path, monkeypatch, "authority")
    mgr.set("watch", interval_seconds=300, times=5, until="green")
    assert mgr.revise(reason="", interval_seconds=600)["error_code"] == "reason_required"
    assert mgr.revise(reason="change wording", prompt="new")["error_code"] == "user_authority_required"
    assert mgr.revise(reason="change wording", prompt="new", user_quote="short")["error_code"] == "user_quote_too_short"
    assert mgr.revise(reason="change wording", prompt="new", user_quote="User asked to change wording", user_messages=[])["error_code"] == "user_quote_not_found"
    long = "x" * 4001
    assert mgr.revise(reason="change wording", prompt="new", user_quote="change wording now", user_messages=[long + " change wording now"])["error_code"] == "user_message_too_long"
    quote = "Please change the loop wording to the new deployment check"
    assert mgr.revise(reason="change wording", prompt="new", user_quote=quote, user_messages=[quote])["ok"] is True

    mgr = _manager(tmp_path, monkeypatch, "authority2")
    mgr.set("watch", interval_seconds=300, times=5)
    assert mgr.revise(reason="faster", interval_seconds=60)["error_code"] == "user_authority_required"
    assert mgr.revise(reason="cap", times=2)["ok"] is True
    assert mgr.revise(reason="remove cap", times=0)["error_code"] == "user_authority_required"
    # Self-paced starts at the 60s floor: faster than 300s, so it needs the user's words.
    assert mgr.revise(reason="switch mode", self_paced=True)["error_code"] == "user_authority_required"

    mgr = _manager(tmp_path, monkeypatch, "authority3")
    mgr.set("watch", interval_seconds=30)
    # From a 30s interval the 60s floor is slower, so no quote is needed.
    assert mgr.revise(reason="back off", self_paced=True)["ok"] is True


def test_loop_wakeup_text_is_never_user_authority(tmp_path, monkeypatch):
    from hermes_cli import goals

    mgr = _manager(tmp_path, monkeypatch, "wakeup-quote")
    state = mgr.set("watch", interval_seconds=3600)
    state.next_due_at = time.time() - 1
    wakeup = mgr.fire_tick()
    # CLI/TUI submit wakeups as plain user turns with no display_kind.
    assert goals._is_user_typed({"content": wakeup, "display_kind": None}) is False
    guidance = "revise the loop with the loop_set tool (action=revise)"
    assert guidance in wakeup
    rows = [{"content": wakeup, "display_kind": None}]
    pool = [r["content"] for r in rows if goals._is_user_typed(r)]
    result = mgr.revise(reason="faster", interval_seconds=30, user_quote=guidance, user_messages=pool)
    assert result["error_code"] == "user_quote_not_found"
    assert mgr.state.interval_seconds == 3600


def test_refresh_keeps_cached_state_when_read_fails(tmp_path, monkeypatch):
    from hermes_cli import loops

    mgr = _manager(tmp_path, monkeypatch, "refresh-fail")
    state = mgr.set("watch", interval_seconds=300)
    state.next_due_at = time.time() - 1
    mgr.fire_tick()
    monkeypatch.setattr(loops, "load_loop", lambda _sid: None)
    mgr.refresh()
    assert mgr.state is not None and mgr.state.awaiting_response is True
    assert mgr.complete_tick("still working")["status"] == "active"


def test_replace_keeps_a_user_paused_loop_paused(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, monkeypatch, "replace-paused")
    mgr.set("old", interval_seconds=300)
    mgr.pause(reason="user-paused")
    quote = "Please reword the loop to check the deploy instead"
    result = mgr.replace(prompt="check the deploy", interval_seconds=300, reason="reworded",
                         user_quote=quote, user_messages=[quote])
    assert result["ok"] is True
    assert mgr.state.status == "paused"
    assert mgr.state.paused_reason == "user-paused"
    assert mgr.is_due() is False


def test_invalid_no_change_and_done_errors(tmp_path, monkeypatch):
    from hermes_cli.loops import LoopManager

    mgr = _manager(tmp_path, monkeypatch, "errors")
    assert mgr.revise(reason="nothing")["error_code"] == "no_loop"
    mgr.set("watch", interval_seconds=300)
    assert mgr.revise(reason="bad cadence", interval_seconds=60, self_paced=True)["error_code"] == "invalid_cadence"
    assert mgr.revise(reason="nothing")["error_code"] == "no_change"
    mgr.clear()
    assert mgr.revise(reason="after clear", interval_seconds=600)["error_code"] == "no_loop"


def test_replace_requires_quote_and_preserves_route_in_history(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, monkeypatch, "replace")
    state = mgr.set("old", interval_seconds=300, times=9, until="old condition", route={"chat_id": "1"})
    state.ticks_fired = 4
    old_created_at = state.created_at
    quote = "Please replace the loop with the deploy verification task"
    result = mgr.replace(
        prompt="deploy verification", interval_seconds=600, times=3, until="green",
        reason="the task changed", user_quote=quote, user_messages=[quote],
    )
    assert result["ok"] is True
    assert result["revision"]["kind"] == "replace"
    assert result["revision"]["before"]["ticks_fired"] == 4
    assert mgr.state.route == {"chat_id": "1"}
    assert mgr.state.ticks_fired == 0
    assert mgr.state.created_at > old_created_at
    assert mgr.state.version == 2


def test_status_and_wakeup_text_include_revision_guidance(tmp_path, monkeypatch):
    from hermes_cli.loops import LoopManager

    mgr = _manager(tmp_path, monkeypatch, "text")
    state = mgr.set("watch", interval_seconds=300)
    state.next_due_at = time.time() - 1
    quote = "Please slow the loop down while the deploy settles"
    assert mgr.revise(reason="slow down", interval_seconds=600, user_quote="", user_messages=[quote])["ok"] is True
    assert "v2" in mgr.status_line()
    state.next_due_at = time.time() - 1
    wakeup = mgr.fire_tick()
    assert wakeup and "loop_set tool (action=revise)" in wakeup


def test_cli_completion_refresh_preserves_mid_wakeup_revision(tmp_path, monkeypatch):
    from hermes_cli.cli_loops_mixin import CLILoopsMixin
    from hermes_cli.loops import LoopManager

    mgr = _manager(tmp_path, monkeypatch, "cli-cache")
    mgr.set("watch", interval_seconds=300)
    mgr.state.next_due_at = time.time() - 1
    mgr.fire_tick()
    quote = "Please change the loop to watch deploy readiness"
    external = LoopManager("cli-cache")
    assert external.revise(reason="user changed task", prompt="watch deploy readiness", user_quote=quote, user_messages=[quote])["ok"]

    fake = SimpleNamespace(
        session_id="cli-cache", _loop_manager=mgr, _last_turn_interrupted=False,
        conversation_history=[{"role": "assistant", "content": "still working"}],
    )
    fake._get_loop_manager = lambda: mgr
    fake._maybe_complete_loop_tick_after_turn = CLILoopsMixin._maybe_complete_loop_tick_after_turn.__get__(fake)
    fake._last_assistant_response_text = lambda: "still working"
    fake._cprint = lambda *args, **kwargs: None
    with patch("cli._cprint", lambda *args, **kwargs: None), patch("hermes_cli.cli_loops_mixin._print_decision_message", lambda decision: None):
        fake._maybe_complete_loop_tick_after_turn()
    assert mgr.state.prompt == "watch deploy readiness"
    assert mgr.state.awaiting_response is False


def test_tui_completion_uses_fresh_manager(tmp_path, monkeypatch):
    from hermes_cli.loops import LoopManager
    from tui_gateway.prompt_turn import _after_complete_turn

    mgr = _manager(tmp_path, monkeypatch, "tui-cache")
    mgr.set("watch", interval_seconds=300)
    mgr.state.next_due_at = time.time() - 1
    mgr.fire_tick()
    quote = "Please change the loop to watch deploy readiness"
    external = LoopManager("tui-cache")
    assert external.revise(reason="user changed task", prompt="watch deploy readiness", user_quote=quote, user_messages=[quote])["ok"]
    session = {"session_key": "tui-cache"}
    st = SimpleNamespace(tts_queue=object())
    with patch("tui_gateway.prompt_turn._emit", create=True), patch("tui_gateway.prompt_turn._hook_failure"):
        _after_complete_turn("rpc", session, st, "still working")
    assert LoopManager("tui-cache").state.prompt == "watch deploy readiness"

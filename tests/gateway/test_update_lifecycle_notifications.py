"""Native launch → progress → restart → receipt → delivery contracts."""
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gateway.config import Platform
from gateway.update_launcher import launch_native_update
from gateway.update_notifications import final_outcome, read_pending, same_update
from tests.gateway.test_update_command import _make_runner
from tests.gateway.update_fixtures import finalize_update


@pytest.mark.live_system_guard_bypass  # Fixed python -c only: no updater, git, service or runtime access.
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX detached wrapper")
def test_native_wrapper_records_real_process_exit_only_after_termination(tmp_path):
    from gateway.slash_commands import _spawn_detached_update
    output = tmp_path / ".update_output.txt"
    pre_restart_exit = tmp_path / ".update_exit_code"
    final_exit = tmp_path / ".update_process_exit_code"
    script = "print('real detached process output', flush=True); raise SystemExit(7)"
    with patch("gateway.slash_commands._systemd_scope_wrap_if_supervised", side_effect=lambda argv: (argv, None)):
        _spawn_detached_update([sys.executable, "-c", script], output, pre_restart_exit)
    deadline = time.monotonic() + 5
    while not final_exit.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert final_exit.read_text() == "7"
    assert "real detached process output" in output.read_text()
    assert not pre_restart_exit.exists()


def pending(home, *, reason=True):
    data = {"platform": "telegram", "chat_id": "42", "thread_id": "77", "chat_type": "dm",
            "session_key": "agent:main:telegram:dm:42:thread:77",
            "timestamp": datetime.now(timezone.utc).isoformat()}
    if reason:
        data["reason"] = "Activating the delegation-label fix."
    launch_native_update(home=home, hermes_cmd=["hermes"], pending=data, spawn=Mock())
    return read_pending(home)[1]


def test_same_update_accepts_bom_prefixed_marker(tmp_path):
    pending = {"request_id": "request-1", "reason": "Apply the update"}
    marker = tmp_path / ".update_pending.json"
    marker.write_bytes(b"\xef\xbb\xbf" + json.dumps(pending).encode("utf-8"))

    assert same_update(marker, pending)


@pytest.mark.asyncio
async def test_update_restart_reason_reaches_every_interrupted_conversation(tmp_path):
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
    data = pending(tmp_path)
    runner, adapter = make_restart_runner()
    runner._restart_requested = True
    runner._snapshot_running_agents = Mock(return_value={"other": object()})
    other = make_restart_source(chat_id="unrelated", thread_id="other-topic")
    runner._shutdown_notification_target = AsyncMock(return_value=(other, "telegram", "unrelated", "other-topic"))
    with patch("gateway.run._hermes_home", tmp_path):
        await runner._notify_active_sessions_of_shutdown()
    origin = [text for chat, text, _ in adapter.sent_calls if chat == "42"]
    unrelated = [text for chat, text, _ in adapter.sent_calls if chat == "unrelated"]
    assert len(origin) == 2
    assert all(data["reason"] in text for text in origin)
    assert len(unrelated) == 1
    assert unrelated[0].startswith("🔄 Restarting") and data["reason"] in unrelated[0]
    assert "try to resume" in unrelated[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["Activating the restart-reason fix.", None])
async def test_direct_restart_reason_reaches_interrupted_conversations(tmp_path, reason):
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
    runner, adapter = make_restart_runner()
    runner._restart_requested = True
    runner._restart_reason = reason
    runner._snapshot_running_agents = Mock(return_value={"other": object()})
    other = make_restart_source(chat_id="unrelated", thread_id="other-topic")
    runner._shutdown_notification_target = AsyncMock(return_value=(other, "telegram", "unrelated", "other-topic"))
    with patch("gateway.run._hermes_home", tmp_path):
        await runner._notify_active_sessions_of_shutdown()
    [text] = [text for chat, text, _ in adapter.sent_calls if chat == "unrelated"]
    if reason:
        assert text.startswith("🔄 Restarting") and reason in text
    else:
        assert text.startswith("⚠️ Hermes is restarting")
    assert "try to resume" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["result", "exception"])
async def test_reason_progress_and_final_survive_restart_and_delivery_failure(tmp_path, failure):
    data = pending(tmp_path)
    reason = data["reason"]
    output = tmp_path / ".update_output.txt"
    output.write_text("native progress before restart\n")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    first = _make_runner()
    first.adapters = {Platform.TELEGRAM: adapter}
    with patch("gateway.run._hermes_home", tmp_path):
        watch = asyncio.create_task(first._watch_update_progress(poll_interval=.01, stream_interval=.01))
        for _ in range(200):
            if read_pending(tmp_path)[1].get("output_offset"):
                break
            await asyncio.sleep(.01)
        assert read_pending(tmp_path)[1]["output_offset"] == output.stat().st_size
        assert await first._send_update_phase("restarting")
        watch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watch
        # Pre-restart zero alone must never consume the route or announce success.
        (tmp_path / ".update_exit_code").write_text("0")
        second = _make_runner()
        second.adapters = {Platform.TELEGRAM: adapter}
        assert await second._send_update_notification() is False
        assert not any("✅" in c.args[1] for c in adapter.send.call_args_list)
        finalize_update(tmp_path)
        output.write_text(output.read_text() + "native progress after restart\n")
        if failure == "exception":
            adapter.send.side_effect = OSError("transport unavailable")
        else:
            adapter.send.return_value = SimpleNamespace(success=False)
        assert await second._send_update_notification() is False
        assert read_pending(tmp_path)[1]["reason"] == reason
        adapter.send.side_effect = None
        adapter.send.return_value = SimpleNamespace(success=True)
        marker, record = read_pending(tmp_path)
        record["notice_retry_at"] = 0  # advance beyond the persisted backoff
        marker.write_text(json.dumps(record), encoding="utf-8")
        results = await asyncio.gather(second._send_update_notification(), second._send_update_notification())
        assert results.count(True) == 1
        assert read_pending(tmp_path) is None
        assert await second._send_update_notification() is False
    messages = [c.args[1] for c in adapter.send.call_args_list]
    phases = [m for m in messages if m.startswith(("⬆️", "🔄", "✅", "❌"))]
    assert [m.split("\n", 1)[0] for m in phases] == ["⬆️ Updating", "🔄 Restarting", "✅ Update Complete"]
    assert all(reason in m and "Reason:" not in m for m in phases)
    assert all("Hermes" not in m for m in phases[:2])
    assert "recovery will be attempted" in phases[1]
    assert sum("native progress before restart" in m for m in messages) == 1
    assert all(str(c.kwargs["metadata"]["thread_id"]) == "77" for c in adapter.send.call_args_list)


@pytest.mark.asyncio
async def test_stream_retry_only_sends_unsent_chunk(tmp_path):
    pending(tmp_path)
    output = tmp_path / ".update_output.txt"
    output.write_text("A" * 3500 + "B" * 50, encoding="utf-8")
    calls = []
    failed = False

    async def send(_chat, text, **_kwargs):
        nonlocal failed
        if text.startswith("```"):
            calls.append(text)
            if "B" in text and not failed:
                failed = True
                return SimpleNamespace(success=False)
        return SimpleNamespace(success=True)

    adapter = SimpleNamespace(send=send)
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    with patch("gateway.run._hermes_home", tmp_path):
        watcher = asyncio.create_task(runner._watch_update_progress(
            poll_interval=.01, stream_interval=.01, timeout=10))
        try:
            retry_advanced = False
            for _ in range(300):
                if failed and not retry_advanced:
                    marker, record = read_pending(tmp_path)
                    record["notice_retry_at"] = 0
                    marker.write_text(json.dumps(record), encoding="utf-8")
                    retry_advanced = True
                if failed and read_pending(tmp_path)[1].get("output_offset") == output.stat().st_size:
                    break
                await asyncio.sleep(.01)
            assert failed
            assert read_pending(tmp_path)[1]["output_offset"] == output.stat().st_size
        finally:
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
    assert sum("A" in text for text in calls) == 1
    assert sum("B" in text for text in calls) == 2


@pytest.mark.asyncio
async def test_final_retry_only_sends_unsent_chunk_and_then_final(tmp_path):
    pending(tmp_path)
    output = tmp_path / ".update_output.txt"
    output.write_bytes(("A" * 3500 + "B" * 49 + "é").encode() + b"\xff")
    finalize_update(tmp_path)
    calls = []
    failed = False

    async def send(_chat, text, **_kwargs):
        nonlocal failed
        calls.append(text)
        if text.startswith("```") and "B" in text and not failed:
            failed = True
            return SimpleNamespace(success=False)
        return SimpleNamespace(success=True)

    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: SimpleNamespace(send=send)}
    with patch("gateway.run._hermes_home", tmp_path):
        assert await runner._send_update_notification() is False
        assert read_pending(tmp_path)[1]["output_offset"] == 3500
        assert not any(text.startswith("✅") for text in calls)
        marker, record = read_pending(tmp_path)
        record["notice_retry_at"] = 0
        marker.write_text(json.dumps(record), encoding="utf-8")
        assert await runner._send_update_notification() is True
    chunks = [text for text in calls if text.startswith("```")]
    assert sum("A" in text for text in chunks) == 1
    assert sum("B" in text for text in chunks) == 2
    assert sum(text.startswith("✅") for text in calls) == 1
    assert read_pending(tmp_path) is None


@pytest.mark.parametrize("case", ["pre_restart", "missing", "stale", "partial", "unknown", "pending_restart", "malformed", "failed", "success", "legacy"])
def test_final_success_requires_completed_matching_receipt_and_runtime(tmp_path, case):
    data = pending(tmp_path, reason=case != "legacy")
    (tmp_path / ".update_exit_code").write_text("0")
    if case == "pre_restart":
        assert final_outcome(tmp_path, data) is None
        return
    finalize_update(tmp_path, outcome="partial" if case == "partial" else "success",
                    fleet_state="unknown" if case == "unknown" else "current")
    path = tmp_path / "logs" / "update_receipts" / "latest.json"
    receipt = json.loads(path.read_text())
    if case == "missing":
        path.unlink()
    elif case == "stale":
        receipt["started_at"] = receipt["finished_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        path.write_text(json.dumps(receipt))
    elif case == "pending_restart":
        (tmp_path / "fleet_restart_pending").write_text("native updater obligation")
    elif case == "malformed":
        path.write_text('{"finished_at":')
    elif case == "failed":
        (tmp_path / ".update_process_exit_code").write_text("7")
    elif case == "legacy":
        data.pop("notification_version")
        data.pop("timestamp")
        # Old markers and manual launches lack a reason and process-exit sentinel.
    result = final_outcome(tmp_path, data)
    if case in {"missing", "stale", "pending_restart", "malformed"}:
        assert result is None
    elif case in {"partial", "unknown", "failed"}:
        assert result[0] is False
    else:
        assert result[0] is True


@pytest.mark.parametrize("case", ["noop", "noop_with_stale_fleet", "noop_failed_outcome", "noop_pending_restart"])
def test_already_up_to_date_is_success_without_runtime_to_verify(tmp_path, case):
    """A no-op update restarts nothing, so an empty fleet is expected, not missing evidence."""
    data = pending(tmp_path)
    finalize_update(tmp_path, noop=True, outcome="partial" if case == "noop_failed_outcome" else "success")
    path = tmp_path / "logs" / "update_receipts" / "latest.json"
    if case == "noop_with_stale_fleet":
        receipt = json.loads(path.read_text())
        receipt["fleet"] = [{"profile": "default", "pid": 1, "state": "stale", "code_sha": "c" * 40}]
        path.write_text(json.dumps(receipt))
    elif case == "noop_pending_restart":
        (tmp_path / "fleet_restart_pending").write_text("native updater obligation")
    result = final_outcome(tmp_path, data)
    if case == "noop":
        assert result == (True, f"Hermes is already at revision {'a' * 12}.")
    elif case == "noop_pending_restart":
        assert result is None
    else:
        assert result[0] is False


@pytest.mark.parametrize("live", ["replacement_current", "replacement_old_code", "not_back_yet", "down", "other_row_stale"])
def test_self_restart_pending_is_judged_by_the_replacement_gateway(tmp_path, live):
    """An in-gateway update (request_update) finalizes before its own gateway restarts, so the receipt
    row is ``restart_pending`` with the OLD pid and sha. The notice must wait for the replacement and
    judge it: the new code running is success, old code is failure, not-yet-back is still pending.
    Rendering every such row as a failure sent a false "Update Failed" after each promotion."""
    data = pending(tmp_path)
    finalize_update(tmp_path, fleet_state="restart_pending")
    path = tmp_path / "logs" / "update_receipts" / "latest.json"
    receipt = json.loads(path.read_text())
    receipt["fleet"][0]["code_sha"] = "b" * 40  # the pre-update code the enclosing gateway still ran
    if live == "other_row_stale":
        receipt["fleet"].append({"profile": "ops", "pid": 4321, "state": "stale", "code_sha": "b" * 40})
    path.write_text(json.dumps(receipt))
    live_pid = {"replacement_current": 5678, "replacement_old_code": 5678, "not_back_yet": 1234,
                "down": None, "other_row_stale": 5678}[live]
    runtime_sha = "b" * 40 if live == "replacement_old_code" else "a" * 40
    (tmp_path / "gateway_state.json").write_text(json.dumps({"pid": live_pid, "code_sha": runtime_sha}))
    with patch("gateway.status.live_gateway_pid_for_home", return_value=live_pid):
        result = final_outcome(tmp_path, data)
    if live == "replacement_current":
        assert result is not None and result[0] is True and "a" * 12 in result[1]
    elif live in {"not_back_yet", "down"}:
        assert result is None
    else:
        assert result is not None and result[0] is False


@pytest.mark.asyncio
async def test_already_up_to_date_request_reports_already_latest(tmp_path):
    data = pending(tmp_path)
    finalize_update(tmp_path, noop=True)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    with patch("gateway.run._hermes_home", tmp_path):
        assert await runner._send_update_notification() is True
    final = adapter.send.call_args_list[-1].args[1]
    assert final.startswith("ℹ️ Already Latest")
    assert data["reason"] in final and "not restarted" in final
    assert not any("❌" in c.args[1] for c in adapter.send.call_args_list)
    assert read_pending(tmp_path) is None


@pytest.mark.asyncio
async def test_failed_legacy_update_output_never_claims_success(tmp_path):
    runner = _make_runner()
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner.adapters = {Platform.TELEGRAM: adapter}
    data = {"platform": "telegram", "chat_id": "42", "session_key": "legacy"}
    (tmp_path / ".update_pending.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / ".update_output.txt").write_text("dependency installation failed", encoding="utf-8")
    (tmp_path / ".update_exit_code").write_text("7", encoding="utf-8")
    with patch("gateway.run._hermes_home", tmp_path):
        assert await runner._send_update_notification()
    sent = " ".join(call.args[1] for call in adapter.send.call_args_list)
    assert "dependency installation failed" in sent
    assert "Hermes update failed" in sent and "code 7" in sent
    assert "successfully" not in sent
    assert read_pending(tmp_path) is None


@pytest.mark.asyncio
async def test_housekeeping_retries_undelivered_notice_after_watcher_deadline(tmp_path):
    from gateway.run import _start_gateway_housekeeping
    pending(tmp_path)
    finalize_update(tmp_path)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    # A completed watcher has stopped trying; the regular tick must recover it.
    runner._update_notification_task = asyncio.get_running_loop().create_future()
    runner._update_notification_task.set_result(None)
    stop = __import__("threading").Event()
    loop = asyncio.get_running_loop()

    def tick(*_args):
        stop.set()

    with patch("gateway.run._hermes_home", tmp_path), \
         patch("gateway.run._write_runtime_status_quiet"), \
         patch("gateway.run._housekeeping_chore", side_effect=lambda label, fn: fn() if label == "Update notice retry" else None), \
         patch("gateway.run_delivery_queue_watch.wait_for_next_tick", side_effect=tick):
        await asyncio.to_thread(_start_gateway_housekeeping, stop, loop=loop, runner=runner)
        for _ in range(100):
            if read_pending(tmp_path) is None:
                break
            await asyncio.sleep(.01)
    assert read_pending(tmp_path) is None
    assert any("Update Complete" in call.args[1] for call in adapter.send.call_args_list)


@pytest.mark.asyncio
async def test_superseded_watcher_cannot_clear_new_marker(tmp_path):
    data = pending(tmp_path)
    finalize_update(tmp_path)
    runner = _make_runner()
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=False, error="flood_control:120")))
    runner.adapters = {Platform.TELEGRAM: adapter}
    from gateway.update_launcher import launch_native_update
    with patch("gateway.run._hermes_home", tmp_path):
        old = asyncio.create_task(runner._watch_update_progress(poll_interval=.01, stream_interval=.01, timeout=1))
        for _ in range(100):
            if read_pending(tmp_path)[1].get("notice_retry_at"):
                break
            await asyncio.sleep(.01)
        assert read_pending(tmp_path)[1].get("notice_retry_at")
        assert launch_native_update(home=tmp_path, hermes_cmd=["hermes"],
                                    pending={**data, "reason": "second request"}, spawn=Mock())["started"]
        await asyncio.wait_for(old, 2)
    current = read_pending(tmp_path)
    assert current is not None and current[1]["reason"] == "second request"


@pytest.mark.asyncio
async def test_notice_retry_honors_flood_delay_and_prior_result(tmp_path):
    from gateway.update_notifications import save_pending
    data = pending(tmp_path)
    marker, data = read_pending(tmp_path)
    data["previous_outcome"] = {"reason": "Earlier change", "success": True,
                                "detail": "Verified earlier revision"}
    save_pending(marker, data)
    finalize_update(tmp_path)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(
        success=False, error="flood_control:7200")))
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    with patch("gateway.run._hermes_home", tmp_path):
        assert not await runner._send_update_notification()
        marker, deferred = read_pending(tmp_path)
        assert deferred["notice_retry_at"] - datetime.now(timezone.utc).timestamp() > 7100
        before = adapter.send.call_count
        runner._retry_update_notice_if_due()
        await asyncio.sleep(0)
        assert adapter.send.call_count == before
        deferred["notice_retry_at"] = 0
        save_pending(marker, deferred)
        adapter.send.return_value = SimpleNamespace(success=True)
        runner._retry_update_notice_if_due()
        await runner._update_notice_retry_task
    assert read_pending(tmp_path) is None
    text = adapter.send.call_args_list[-1].args[1]
    assert "Earlier change" in text and "Verified earlier revision" in text


@pytest.mark.asyncio
async def test_legacy_nested_previous_outcomes_are_bounded_in_rendered_notice(tmp_path):
    from gateway.update_notifications import save_pending

    pending(tmp_path)
    marker, record = read_pending(tmp_path)
    previous = None
    for index in range(50):
        previous = {
            "reason": f"prior reason {index} " + "R" * 2000,
            "success": index % 2 == 0,
            "detail": f"prior detail {index} " + "D" * 5000,
            "previous_outcome": previous,
        }
    record["previous_outcome"] = previous
    save_pending(marker, record)
    finalize_update(tmp_path)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}

    with patch("gateway.run._hermes_home", tmp_path):
        assert await runner._send_update_notification() is True

    text = adapter.send.call_args_list[-1].args[1]
    assert len(text) <= 4096
    assert text.count("Previous update ") <= 8
    assert read_pending(tmp_path) is None


def test_bounded_previous_outcome_history_keeps_new_marker_small(tmp_path):
    data = pending(tmp_path)
    for index in range(50):
        finalize_update(tmp_path)
        result = launch_native_update(
            home=tmp_path, hermes_cmd=["hermes"],
            pending={**data, "reason": f"update {index}"}, spawn=Mock(),
        )
        assert result["started"] is True
        data = read_pending(tmp_path)[1]

    previous = data["previous_outcome"]
    assert isinstance(previous, dict)
    assert "previous_outcome" not in previous
    assert data["previous_outcome_older_count"] == 49
    assert len(previous["detail"]) <= 240


@pytest.mark.asyncio
@pytest.mark.parametrize("send_path", ["final_notice", "final_output", "phase_ack"])
@pytest.mark.parametrize("error", [TimeoutError("transport timed out"), Exception("transport unavailable")])
async def test_post_deadline_delivery_exception_persists_retry_backoff(tmp_path, send_path, error):
    pending(tmp_path)
    marker, record = read_pending(tmp_path)
    if send_path != "phase_ack":
        record["updating_notified"] = True
    marker.write_text(json.dumps(record), encoding="utf-8")
    if send_path == "final_output":
        (tmp_path / ".update_output.txt").write_text("native output\n", encoding="utf-8")
    finalize_update(tmp_path)
    adapter = SimpleNamespace(send=AsyncMock(side_effect=error))
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    # A completed watcher cannot retry; only the housekeeping tick remains.
    runner._update_notification_task = asyncio.get_running_loop().create_future()
    runner._update_notification_task.set_result(None)

    class Clock(datetime):
        instant = datetime.now(timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)

    with patch("gateway.run._hermes_home", tmp_path), patch("gateway.run_notifications.datetime", Clock):
        runner._retry_update_notice_if_due()
        await runner._update_notice_retry_task
        first = read_pending(tmp_path)[1]
        assert adapter.send.call_count == 1
        assert first["notice_attempts"] == 1
        assert first["notice_retry_at"] > Clock.now(timezone.utc).timestamp()
        assert first.get("output_offset", 0) == 0
        for _ in range(3):
            runner._retry_update_notice_if_due()
            await asyncio.sleep(0)
        assert adapter.send.call_count == 1
        Clock.instant = datetime.fromtimestamp(first["notice_retry_at"] + 1, timezone.utc)
        runner._retry_update_notice_if_due()
        await runner._update_notice_retry_task
        second = read_pending(tmp_path)[1]
        assert adapter.send.call_count == 2
        assert second["notice_attempts"] == 2
        assert second["notice_retry_at"] - Clock.now(timezone.utc).timestamp() >= 120
        assert second.get("output_offset", 0) == 0


@pytest.mark.asyncio
async def test_missing_adapter_retries_then_expires_without_success_claim(tmp_path, caplog):
    runner = _make_runner()
    runner.adapters = {}
    data = pending(tmp_path)
    with patch("gateway.run._hermes_home", tmp_path):
        assert await runner._send_update_notification() is False
        assert read_pending(tmp_path) is not None
        marker, data = read_pending(tmp_path)
        data["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        marker.write_text(json.dumps(data), encoding="utf-8")
        assert await runner._send_update_notification() is True
    assert read_pending(tmp_path) is None
    assert "adapter never connected" in caplog.text


@pytest.mark.asyncio
@pytest.mark.live_system_guard_bypass  # Fixed python -c only: no updater, git, service or runtime access.
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX detached wrapper")
async def test_slash_update_final_outcome_follows_the_detached_wrapper(tmp_path):
    """A /update request and the wrapper it spawns must agree on the completion marker.

    The wrapper reports only ``.update_process_exit_code`` and deletes ``.update_exit_code``,
    so a request still waiting on the legacy marker never sees the updater finish.
    """
    from tests.gateway.test_update_command import _make_event
    runner = _make_runner()
    event = _make_event(platform=Platform.TELEGRAM, chat_id="42")
    fake_root = tmp_path / "project"
    (fake_root / ".git").mkdir(parents=True)
    (fake_root / "gateway").mkdir()
    (fake_root / "gateway" / "run.py").touch()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".update_process_exit_code").write_text("0", encoding="utf-8")  # stale, from an earlier update
    failing = [sys.executable, "-c", "print('updater ran', flush=True); raise SystemExit(7)"]
    with patch("gateway.run._hermes_home", home), \
         patch("gateway.run.__file__", str(fake_root / "gateway" / "run.py")), \
         patch("gateway.run._resolve_hermes_bin", return_value=failing), \
         patch("gateway.slash_commands._systemd_scope_wrap_if_supervised", side_effect=lambda argv: (argv, None)), \
         patch.object(runner, "_schedule_update_notification_watch"):
        await runner._handle_update_command(event)
    record = read_pending(home)[1]
    deadline = time.monotonic() + 10
    outcome = None
    while outcome is None and time.monotonic() < deadline:
        outcome = final_outcome(home, record)
        time.sleep(.05)
    assert outcome is not None and outcome[0] is False and "code 7" in outcome[1]


@pytest.mark.asyncio
async def test_whitespace_only_trailing_output_does_not_hold_the_final_notice(tmp_path):
    """A trailing newline after the last flush must not delay completion to the deadline."""
    pending(tmp_path)
    output = tmp_path / ".update_output.txt"
    output.write_text("\n")
    finalize_update(tmp_path)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}
    with patch("gateway.run._hermes_home", tmp_path):
        # The watcher deadline is 30s; completion must arrive well before it.
        await asyncio.wait_for(
            runner._watch_update_progress(poll_interval=.01, stream_interval=.01, timeout=30.0), 5
        )
    messages = [c.args[1] for c in adapter.send.call_args_list]
    assert messages[-1].startswith("✅ Update Complete")
    assert not any("timed out" in m.lower() or "still running" in m.lower() for m in messages)
    assert read_pending(tmp_path) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("send_path", ["final_notice", "final_output", "phase", "watcher_output"])
async def test_inflight_old_notice_cannot_mutate_superseding_request(tmp_path, send_path):
    old = pending(tmp_path)
    if send_path != "phase":
        marker, record = read_pending(tmp_path)
        record["updating_notified"] = True
        marker.write_text(json.dumps(record), encoding="utf-8")
    if send_path in {"final_output", "watcher_output"}:
        (tmp_path / ".update_output.txt").write_text("A" * 3500 + "B")
    finalize_update(tmp_path)
    messages = []

    async def send(_chat, text, **_kwargs):
        messages.append(text)
        if len(messages) == 1:
            result = launch_native_update(
                home=tmp_path, hermes_cmd=["hermes"],
                pending={**old, "reason": "second request",
                         "timestamp": datetime.now(timezone.utc).isoformat()}, spawn=Mock(),
            )
            assert result["started"] is True
        return SimpleNamespace(success=True)

    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: SimpleNamespace(send=send)}
    with patch("gateway.run._hermes_home", tmp_path):
        if send_path in {"final_notice", "final_output"}:
            assert await runner._send_update_notification() is False
        elif send_path == "phase":
            assert await runner._send_update_phase("updating") is False
        else:
            await asyncio.wait_for(runner._watch_update_progress(
                poll_interval=.01, stream_interval=0, timeout=.1), 2)
    current = read_pending(tmp_path)
    assert current is not None
    assert current[1]["reason"] == "second request"
    assert current[1]["request_id"] != old["request_id"]
    assert current[1]["previous_outcome"]["reason"] == old["reason"]
    assert not current[1].get("updating_notified")
    assert not current[1].get("output_offset")
    assert not current[1].get("notice_retry_at")
    assert len(messages) == 1


def _overwrite_latest_with_pm_sync(home):
    """``pm`` sync finishes after the update and replaces the shared ``latest.json`` pointer."""
    path = home / "logs" / "update_receipts" / "latest.json"
    path.write_text(json.dumps({
        "schema": 1, "kind": "sync", "outcome": "ok", "update_id": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }))


def test_final_outcome_reads_the_update_receipt_after_a_pm_sync_overwrites_latest(tmp_path):
    data = pending(tmp_path)
    finalize_update(tmp_path)
    _overwrite_latest_with_pm_sync(tmp_path)
    with patch("gateway.status.live_gateway_pid_for_home", return_value=1234):
        result = final_outcome(tmp_path, data)
    assert result is not None and result[0] is True, result


def _unroutable_pending(home):
    """A request_update from cron or the CLI has no chat to report back to."""
    launch_native_update(home=home, hermes_cmd=["hermes"], spawn=Mock(), pending={
        "reason": "Promote the merged fix.", "timestamp": datetime.now(timezone.utc).isoformat()})
    return read_pending(home)[1]


@pytest.mark.asyncio
async def test_unroutable_marker_waits_for_the_outcome_then_clears(tmp_path):
    _unroutable_pending(tmp_path)
    runner = _make_runner()
    with patch("gateway.run._hermes_home", tmp_path):
        assert await runner._send_update_notification() is False
        assert read_pending(tmp_path) is not None, "an unfinished update must keep its admission"
        finalize_update(tmp_path)
        with patch("gateway.status.live_gateway_pid_for_home", return_value=1234):
            assert await runner._send_update_notification() is True
    assert read_pending(tmp_path) is None

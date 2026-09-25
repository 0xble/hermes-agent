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
from gateway.update_notifications import final_outcome, read_pending
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


@pytest.mark.asyncio
async def test_shutdown_reason_does_not_cross_conversation_boundaries(tmp_path):
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
    assert data["reason"] not in unrelated[0]
    assert "try to resume" in unrelated[0]


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
            for _ in range(300):
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

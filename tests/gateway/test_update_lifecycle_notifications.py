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
    runner._shutdown_notification_target = AsyncMock(return_value=(other, "telegram", "unrelated", "other-topic", None))
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
    assert all(reason in m and "Reason:" not in m and "Hermes" not in m for m in phases)
    assert "recovery will be attempted" in phases[1]
    assert sum("native progress before restart" in m for m in messages) == 1
    assert all(str(c.kwargs["metadata"]["thread_id"]) == "77" for c in adapter.send.call_args_list)


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

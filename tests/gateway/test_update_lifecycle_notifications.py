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
    output.write_text("already observed progress\n", encoding="utf-8")
    script = "print('real detached process output', flush=True); raise SystemExit(7)"
    with patch("gateway.slash_commands._systemd_scope_wrap_if_supervised", side_effect=lambda argv: (argv, None)):
        _spawn_detached_update([sys.executable, "-c", script], output, pre_restart_exit)
    deadline = time.monotonic() + 5
    while not final_exit.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert final_exit.read_text() == "7"
    assert output.read_text() == "already observed progress\nreal detached process output\n"
    assert not pre_restart_exit.exists()


def test_windows_helper_appends_without_truncating_observed_output(tmp_path):
    from gateway.slash_commands import _WINDOWS_UPDATE_HELPER

    output, exit_code = tmp_path / "output", tmp_path / "exit"
    output.write_bytes(b"already observed\n")

    def child(cmd, *, stdout, **kwargs):
        stdout.write(b"child progress\n")
        return SimpleNamespace(wait=lambda **kwargs: 7)

    # Execute the portable helper body, never a Windows process on another OS.
    with patch.object(sys, "argv", ["helper", str(output), str(exit_code), "fixture"]), \
         patch("subprocess.Popen", side_effect=child):
        exec(compile(_WINDOWS_UPDATE_HELPER, "<windows-update-helper>", "exec"), {})
    assert output.read_bytes() == b"already observed\nchild progress\n"
    assert exit_code.read_text() == "7"


@pytest.mark.parametrize("existing_marker", [None, ".update_pending.json", ".update_pending.claimed.json"])
def test_new_request_initializes_output_before_any_watcher_can_resolve_it(tmp_path, monkeypatch, existing_marker):
    from gateway import update_launcher
    from gateway.update_notifications import request_identity

    if existing_marker:
        files = {existing_marker: b'{"reason": "existing"}', ".update_output.txt": b"acknowledged bytes\n",
                 ".update_exit_code": b"0", ".update_process_exit_code": b"7"}
        for name, content in files.items():
            (tmp_path / name).write_bytes(content)
        spawn = Mock()
        assert launch_native_update(home=tmp_path, hermes_cmd=["hermes"], pending={"reason": "new"}, spawn=spawn) == {
            "started": False, "pending": True,
        }
        spawn.assert_not_called()
        assert {name: (tmp_path / name).read_bytes() for name in files} == files
        return

    output = tmp_path / ".update_output.txt"
    output.write_text("stale conversation output\n", encoding="utf-8")
    runner = _make_runner()
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner.adapters = {Platform.TELEGRAM: adapter}
    real_fsync = update_launcher.os.fsync
    observations = []

    def observe_publication(fd):
        real_fsync(fd)
        # A startup watcher is independent of the explicitly scheduled watcher.
        target = runner._resolve_update_target(runner._update_paths())
        if target is not None:
            observations.append(output.read_bytes())
            assert output.read_bytes() == b""

    monkeypatch.setattr(update_launcher.os, "fsync", observe_publication)

    def delayed_child(cmd, output, exit_code):
        async def scenario():
            paths = runner._update_paths()
            target = runner._resolve_update_target(paths)
            marker = read_pending(tmp_path)
            assert target is not None and marker is not None
            request = request_identity(marker[1])
            assert await runner._drain_update_output(target, paths, request)
            adapter.send.assert_not_called()
            with output.open("ab") as stream:
                stream.write(b"new progress\n")
            assert await runner._drain_update_output(target, paths, request)
            finalize_update(tmp_path)
            assert await runner._send_update_notification()
            assert read_pending(tmp_path) is None
        asyncio.run(scenario())

    with patch("gateway.run._hermes_home", tmp_path):
        result = launch_native_update(home=tmp_path, hermes_cmd=["hermes"], pending={
            "platform": "telegram", "chat_id": "42", "timestamp": datetime.now(timezone.utc).isoformat(),
        }, spawn=delayed_child)
    assert result["started"]
    assert observations
    messages = [call.args[1] for call in adapter.send.call_args_list]
    assert any("new progress" in message for message in messages)
    assert all("stale conversation" not in message for message in messages)


def pending(home, *, reason=True):
    data = {"platform": "telegram", "chat_id": "42", "thread_id": "77", "chat_type": "dm",
            "session_key": "agent:main:telegram:dm:42:thread:77",
            "timestamp": datetime.now(timezone.utc).isoformat()}
    if reason:
        data["reason"] = "Activating the delegation-label fix."
    launch_native_update(home=home, hermes_cmd=["hermes"], pending=data, spawn=Mock())
    return read_pending(home)[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [" \n\t\n", "visible progress\n\n"])
async def test_finalized_output_acknowledges_whitespace_without_timeout(tmp_path, content):
    pending(tmp_path)
    output = tmp_path / ".update_output.txt"
    output.write_text(content)
    finalize_update(tmp_path)
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: SimpleNamespace(send=AsyncMock())}
    runner._send_update_output = AsyncMock(return_value=True)
    offsets = []

    async def notify(*, timed_out=False):
        assert not timed_out
        offsets.append(read_pending(tmp_path)[1]["output_offset"])
        return True

    runner._send_update_notification = notify
    with patch("gateway.run._hermes_home", tmp_path):
        await asyncio.wait_for(runner._watch_update_progress(poll_interval=.01, stream_interval=.01), 2)
    assert offsets == [len(content.encode())]
    assert runner._send_update_output.await_count == int(bool(content.strip()))


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [False, True])
async def test_output_offsets_require_visible_delivery_but_consume_later_blank_tail(tmp_path, delivered):
    pending(tmp_path)
    output = tmp_path / ".update_output.txt"
    output.write_text("first visible progress\n")
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: SimpleNamespace(send=AsyncMock())}
    runner._send_update_output = AsyncMock(return_value=delivered)
    notified = []

    async def notify(*, timed_out=False):
        assert not timed_out
        notified.append(read_pending(tmp_path)[1].get("output_offset", 0))
        return True

    runner._send_update_notification = notify
    with patch("gateway.run._hermes_home", tmp_path):
        task = asyncio.create_task(runner._watch_update_progress(poll_interval=.01, stream_interval=.01))
        for _ in range(100):
            if runner._send_update_output.await_count:
                break
            await asyncio.sleep(.01)
        assert runner._send_update_output.await_count
        if delivered:
            assert read_pending(tmp_path)[1]["output_offset"] == output.stat().st_size
            with output.open("a") as stream:
                stream.write(" \n\t")
            finalize_update(tmp_path)
            await asyncio.wait_for(task, 2)
            assert notified == [output.stat().st_size]
            assert runner._send_update_output.await_count == 1
        else:
            assert read_pending(tmp_path)[1].get("output_offset", 0) == 0
            assert not notified
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


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


@pytest.mark.skipif(sys.platform == "win32", reason="systemd scope wrapper is POSIX-only")
def test_supervised_update_scope_unit_is_unique_per_launch(monkeypatch):
    """Two gateway profiles under one OS user must not race for a single fixed scope unit:
    the second ``systemd-run`` fails asynchronously, the wrapper never records an exit code,
    and that profile is stuck behind a pending marker."""
    from gateway import slash_commands
    monkeypatch.setenv("INVOCATION_ID", "fixture-supervised")
    monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr("tools.process_registry.systemd_user_bus_env",
                        lambda base_env=None: {"XDG_RUNTIME_DIR": "/run/user/1000"})
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None)
    argv = ["setsid", "bash", "-c", "true"]

    def unit(wrapped):
        return wrapped[wrapped.index("--unit") + 1]

    first, env = slash_commands._systemd_scope_wrap_if_supervised(argv)
    second, _ = slash_commands._systemd_scope_wrap_if_supervised(argv)
    assert env == {"XDG_RUNTIME_DIR": "/run/user/1000"}
    assert first[0] == "/usr/bin/systemd-run" and first[-len(argv):] == argv
    assert unit(first) != unit(second)
    for name in (unit(first), unit(second)):
        assert name.startswith("hermes-gateway-update-") and name.endswith(".scope")

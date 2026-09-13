"""Notification expiry is not updater termination or permission to reuse its files."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gateway.config import Platform
from gateway.update_launcher import launch_native_update
from gateway.update_notifications import read_pending, request_identity, save_pending
from tests.gateway.test_update_command import _make_runner
from tests.gateway.test_update_lifecycle_notifications import pending
from tests.gateway.update_fixtures import finalize_update


@pytest.mark.asyncio
@pytest.mark.parametrize("marker_name", [".update_pending.json", ".update_pending.claimed.json"])
@pytest.mark.parametrize("completion", ["success", "failed", "unverified"])
@pytest.mark.parametrize("wrapper_exit", [None, "unfinished"])
async def test_timeout_retains_admission_until_completion_across_restart(tmp_path, marker_name, completion, wrapper_exit):
    data = pending(tmp_path)
    marker = tmp_path / marker_name
    (tmp_path / ".update_pending.json").rename(marker)
    output = tmp_path / ".update_output.txt"
    output.write_bytes(b"old writer output\n")
    (tmp_path / ".update_exit_code").write_text("0", encoding="utf-8")  # pre-restart, not termination
    if wrapper_exit is not None:
        (tmp_path / ".update_process_exit_code").write_text(wrapper_exit, encoding="utf-8")
    (tmp_path / ".update_prompt.json").write_text('{}', encoding="utf-8")
    (tmp_path / ".update_response").write_text('y', encoding="utf-8")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    first = _make_runner()
    first.adapters = {Platform.TELEGRAM: adapter}
    with patch("gateway.run._hermes_home", tmp_path):
        # Expire the real bounded watcher without sleeping or launching an updater.
        await asyncio.wait_for(first._watch_update_progress(timeout=0), 2)
        record = read_pending(tmp_path)
        assert record is not None
        assert request_identity(record[1]) == request_identity(data)
        assert record[1]["timeout_notified"] is True
        preserved = {path: path.read_bytes() for path in tmp_path.glob('.update*') if path.is_file()}
        spawn = Mock()
        assert launch_native_update(home=tmp_path, hermes_cmd=["hermes"], pending={"reason": "new"}, spawn=spawn) == {
            "started": False, "pending": True}
        spawn.assert_not_called()
        assert {path: path.read_bytes() for path in preserved} == preserved
        second = _make_runner()
        second.adapters = {Platform.TELEGRAM: adapter}
        calls = adapter.send.await_count
        assert await second._send_update_notification() is False  # startup must still schedule recovery
        await asyncio.wait_for(second._watch_update_progress(timeout=0), 2)
        assert adapter.send.await_count == calls  # no repeated timeout/phase/output after restart
        with output.open("ab") as stream:
            stream.write(b"old writer finally finished\n")
        if completion == "success":
            finalize_update(tmp_path)
        else:
            (tmp_path / ".update_process_exit_code").write_text(
                "7" if completion == "failed" else "0", encoding="utf-8")
        # A known exit without a receipt still waits for proof until its deadline.
        if completion == "unverified":
            assert await second._send_update_notification() is False
        assert await second._send_update_notification(timed_out=True) is True
        assert read_pending(tmp_path) is None
        assert not output.exists()
        assert not (tmp_path / ".update_process_exit_code").exists()
        assert launch_native_update(home=tmp_path, hermes_cmd=["hermes"], pending={"reason": "new"}, spawn=spawn)["started"]
        spawn.assert_called_once()
    messages = [call.args[1] for call in adapter.send.call_args_list]
    assert sum("notification deadline" in text for text in messages) == (2 if completion == "unverified" else 1)
    assert sum("old writer output" in text for text in messages) == 1
    assert sum("old writer finally finished" in text for text in messages) == 1
    assert messages[-1].startswith("✅" if completion == "success" else "❌")


@pytest.mark.asyncio
@pytest.mark.parametrize("during_send", ["failure", "exception", "claim", "replacement", "completion"])
async def test_timeout_acknowledges_only_accepted_current_request(tmp_path, during_send):
    data = pending(tmp_path)
    marker, data = read_pending(tmp_path)
    data["updating_notified"] = True
    save_pending(marker, data)
    runner = _make_runner()
    replacement = None

    async def send(*args, **kwargs):
        nonlocal replacement
        if during_send == "failure":
            return SimpleNamespace(success=False)
        if during_send == "exception":
            raise OSError("transport unavailable")
        if during_send == "claim":
            marker.rename(tmp_path / ".update_pending.claimed.json")
        if during_send == "replacement":
            replacement = {**data, "reason": "a different request"}
            save_pending(marker, replacement)
            (tmp_path / ".update_output.txt").write_bytes(b"replacement output")
        if during_send == "completion":
            finalize_update(tmp_path)
        return SimpleNamespace(success=True)

    adapter = SimpleNamespace(send=AsyncMock(side_effect=send))
    runner.adapters = {Platform.TELEGRAM: adapter}
    with patch("gateway.run._hermes_home", tmp_path):
        assert await runner._send_update_notification(timed_out=True) is False
        record = read_pending(tmp_path)
        assert record is not None
        if during_send == "replacement":
            assert record[1] == replacement
            assert (tmp_path / ".update_output.txt").read_bytes() == b"replacement output"
            return
        assert request_identity(record[1]) == request_identity(data)
        assert bool(record[1].get("timeout_notified")) == (during_send in {"claim", "completion"})
        adapter.send.side_effect = None
        adapter.send.return_value = SimpleNamespace(success=True)
        if during_send != "completion":
            assert await runner._send_update_notification(timed_out=True) is False
            assert read_pending(tmp_path)[1]["timeout_notified"] is True
            finalize_update(tmp_path)
        assert await runner._send_update_notification() is True
        assert read_pending(tmp_path) is None

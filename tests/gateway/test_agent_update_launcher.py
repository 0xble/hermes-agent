"""Native update handoff uses persisted SQLite routes and the real control socket."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.control_socket import query_gateway_control
from gateway.session import SessionSource, SessionStore
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import AsyncSessionDB
from hermes_cli.gateway import _cmd_update


def test_agent_update_resolver_error_is_user_facing_without_marker(tmp_path):
    from gateway.update_launcher import make_agent_update_handler

    def unreadable():
        raise RuntimeError("repair release-layout.json and retry")

    handler = make_agent_update_handler(
        runner=SimpleNamespace(), home=tmp_path, main_loop=Mock(),
        resolve_hermes_bin=unreadable, spawn=Mock(), is_managed=lambda: False)
    result = handler({"reason": "Apply fix", "session_id": "s"})
    assert result == {"accepted": False, "error": "repair release-layout.json and retry"}
    assert not (tmp_path / ".update_pending.json").exists()


def test_cli_invalid_reason_returns_nonzero_without_ipc(monkeypatch, capsys):
    ipc = Mock()
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", ipc)

    assert _cmd_update(SimpleNamespace(reason=None)) == 2
    assert _cmd_update(SimpleNamespace(reason="bad\nreason")) == 2

    assert ipc.call_count == 0
    assert "--reason" in capsys.readouterr().out


def _real_store_with_routes(tmp_path):
    """Create a persisted messaging parent and two real delegated descendants."""
    token = set_hermes_home_override(tmp_path)
    try:
        store = SessionStore(tmp_path / "sessions", GatewayConfig())
        direct = store.get_or_create_session(
            SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="private", user_id="u", thread_id="t")
        )
        db = store._db
        assert db is not None
        db.create_session("delegate-1", "agent", parent_session_id=direct.session_id)
        db.create_session("delegate-2", "agent", parent_session_id="delegate-1")
        # Delegates intentionally have no chat columns. The handler must follow lineage.
        assert db.get_session("delegate-2")["chat_id"] is None
        return store, direct.session_id, "delegate-2"
    finally:
        reset_hermes_home_override(token)


@pytest.mark.skipif(sys.platform == "win32", reason="real unix socket transport")
@pytest.mark.parametrize("route_kind", ("direct", "nested"))
def test_agent_update_socket_uses_real_sqlite_session_lineage_and_marshals_watcher(tmp_path, route_kind):
    from gateway.run import _start_gateway_start_control_socket

    store, direct_id, nested_id = _real_store_with_routes(tmp_path)
    watched = asyncio.Event()
    watch_threads = []
    main_thread = threading.get_ident()

    def watch():
        watch_threads.append(threading.get_ident())
        watched.set()

    runner = SimpleNamespace(
        _session_db=AsyncSessionDB(store._db), _schedule_update_notification_watch=watch,
        request_restart=Mock(),
    )
    spawns = []

    def fake_spawn(cmd, output, exit_code):
        spawns.append((cmd, output, exit_code))

    session_id = direct_id if route_kind == "direct" else nested_id

    async def scenario():
        with patch("gateway.run._hermes_home", tmp_path), \
             patch("gateway.run._resolve_hermes_bin", return_value=["hermes"]), \
             patch("gateway.slash_commands._spawn_detached_update", fake_spawn), \
             patch("hermes_cli.config.is_managed", return_value=False):
            server = await _start_gateway_start_control_socket(runner)
            assert server is not None
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None, lambda: query_gateway_control(
                        tmp_path, "agent-update", payload={"session_id": session_id, "reason": "Apply security fix"},
                    )
                )
                await asyncio.wait_for(watched.wait(), timeout=1)
                duplicate = await loop.run_in_executor(
                    None, lambda: query_gateway_control(
                        tmp_path, "agent-update", payload={"session_id": session_id, "reason": "Apply security fix"},
                    )
                )
                unknown = await loop.run_in_executor(None, lambda: query_gateway_control(tmp_path, "not-a-route"))
            finally:
                await server.stop()
        return result, duplicate, unknown

    result, duplicate, unknown = asyncio.run(scenario())
    marker = json.loads((tmp_path / ".update_pending.json").read_text())
    assert result["accepted"] is True and result["started"] is True
    assert duplicate == {"accepted": True, "started": False, "pending": True,
                         "handoff": "Update accepted. End this turn now; the native updater owns completion."}
    assert unknown is None
    assert marker["reason"] == "Apply security fix"
    assert marker["parent_session_id"] == direct_id
    assert marker["parent_route"] == {
        "source": "telegram", "chat_id": "42", "chat_type": "private", "user_id": "u",
        "session_key": marker["parent_route"]["session_key"], "thread_id": "t",
    }
    assert len(spawns) == 1
    assert watch_threads == [main_thread]
    runner.request_restart.assert_not_called()
    store._db.close()


@pytest.mark.skipif(sys.platform == "win32", reason="real unix socket transport")
def test_agent_update_invalid_reason_or_route_has_no_side_effects(tmp_path):
    from gateway.run import _start_gateway_start_control_socket

    store, _direct_id, _nested_id = _real_store_with_routes(tmp_path)
    spawn = Mock()
    runner = SimpleNamespace(
        _session_db=AsyncSessionDB(store._db), _schedule_update_notification_watch=Mock(), request_restart=Mock(),
    )

    async def scenario():
        with patch("gateway.run._hermes_home", tmp_path), \
             patch("gateway.run._resolve_hermes_bin", return_value=["hermes"]), \
             patch("gateway.slash_commands._spawn_detached_update", spawn), \
             patch("hermes_cli.config.is_managed", return_value=False):
            server = await _start_gateway_start_control_socket(runner)
            assert server is not None
            try:
                loop = asyncio.get_running_loop()
                bad_reason = await loop.run_in_executor(
                    None, lambda: query_gateway_control(
                        tmp_path, "agent-update", payload={"session_id": "delegate-2", "reason": "bad\nreason"},
                    )
                )
                bad_route = await loop.run_in_executor(
                    None, lambda: query_gateway_control(
                        tmp_path, "agent-update", payload={"session_id": "missing", "reason": "Need update"},
                    )
                )
            finally:
                await server.stop()
        return bad_reason, bad_route

    bad_reason, bad_route = asyncio.run(scenario())
    assert bad_reason == {"accepted": False, "error": "--reason must be a single paragraph"}
    assert bad_route == {"accepted": False, "error": "no deliverable messaging session route"}
    spawn.assert_not_called()
    assert not (tmp_path / ".update_pending.json").exists()
    assert not (tmp_path / ".update_pending.claimed.json").exists()
    store._db.close()


def test_finished_undelivered_v2_notice_does_not_block_next_update(tmp_path):
    from gateway.update_launcher import launch_native_update
    from tests.gateway.update_fixtures import finalize_update
    from datetime import datetime, timezone

    old = {"platform": "telegram", "chat_id": "42", "reason": "Earlier update",
           "timestamp": datetime.now(timezone.utc).isoformat(), "notification_version": 2}
    (tmp_path / ".update_pending.json").write_text(json.dumps(old), encoding="utf-8")
    finalize_update(tmp_path)
    spawn = Mock()
    result = launch_native_update(
        home=tmp_path, hermes_cmd=["hermes"], pending={"reason": "New update"}, spawn=spawn,
    )
    assert result == {"started": True, "pending": False}
    marker = json.loads((tmp_path / ".update_pending.json").read_text(encoding="utf-8"))
    assert marker["reason"] == "New update"
    assert marker["previous_outcome"]["reason"] == "Earlier update"
    assert marker["previous_outcome"]["success"] is True
    assert not (tmp_path / ".update_process_exit_code").exists()
    spawn.assert_called_once()


def test_replacement_clears_prior_update_lifecycle_artifacts_and_offset(tmp_path):
    from gateway.update_launcher import launch_native_update
    from tests.gateway.update_fixtures import finalize_update
    from datetime import datetime, timezone

    marker = tmp_path / ".update_pending.json"
    marker.write_text(json.dumps({"notification_version": 2, "reason": "prior",
                                  "timestamp": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    finalize_update(tmp_path)
    (tmp_path / ".update_output.txt").write_text("stale output", encoding="utf-8")
    (tmp_path / ".update_prompt.json").write_text("stale prompt", encoding="utf-8")
    (tmp_path / ".update_response").write_text("stale response", encoding="utf-8")

    result = launch_native_update(
        home=tmp_path, hermes_cmd=["hermes"],
        pending={"reason": "new", "output_offset": 999}, spawn=Mock(),
    )

    assert result == {"started": True, "pending": False}
    assert not (tmp_path / ".update_output.txt").exists()
    assert not (tmp_path / ".update_prompt.json").exists()
    assert not (tmp_path / ".update_response").exists()
    replacement = json.loads(marker.read_text(encoding="utf-8"))
    assert "output_offset" not in replacement


def test_failed_superseding_spawn_restores_old_notice(tmp_path):
    from gateway.update_launcher import launch_native_update
    from tests.gateway.update_fixtures import finalize_update
    from datetime import datetime, timezone
    old = {"notification_version": 2, "reason": "prior",
           "timestamp": datetime.now(timezone.utc).isoformat()}
    marker = tmp_path / ".update_pending.json"
    marker.write_text(json.dumps(old), encoding="utf-8")
    finalize_update(tmp_path)
    old_artifacts = {
        ".update_output.txt": b"prior output",
        ".update_prompt.json": b"prior prompt",
        ".update_response": b"prior response",
    }
    for name, content in old_artifacts.items():
        (tmp_path / name).write_bytes(content)
    with pytest.raises(OSError):
        launch_native_update(home=tmp_path, hermes_cmd=["hermes"], pending={"reason": "new"},
                             spawn=Mock(side_effect=OSError("cannot launch")))
    assert json.loads(marker.read_text(encoding="utf-8")) == old
    assert (tmp_path / ".update_process_exit_code").read_text(encoding="utf-8") == "0"
    for name, content in old_artifacts.items():
        assert (tmp_path / name).read_bytes() == content


def test_finished_marker_admission_is_exclusive_across_threads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from gateway.update_launcher import launch_native_update
    from tests.gateway.update_fixtures import finalize_update
    from datetime import datetime, timezone

    marker = tmp_path / ".update_pending.json"
    marker.write_text(json.dumps({"notification_version": 2, "reason": "prior",
                                  "timestamp": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    finalize_update(tmp_path)
    spawn = Mock()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda n: launch_native_update(
            home=tmp_path, hermes_cmd=["hermes"], pending={"reason": f"new-{n}"}, spawn=spawn), range(2)))
    assert sum(result["started"] for result in results) == 1
    spawn.assert_called_once()
    assert json.loads(marker.read_text(encoding="utf-8"))["previous_outcome"]["reason"] == "prior"


def test_unfinished_marker_remains_exclusive_even_with_a_receipt(tmp_path):
    from gateway.update_launcher import launch_native_update
    from datetime import datetime, timezone
    marker = tmp_path / ".update_pending.json"
    marker.write_text(json.dumps({"notification_version": 2, "reason": "running",
                                  "timestamp": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    spawn = Mock()
    assert launch_native_update(home=tmp_path, hermes_cmd=["hermes"], pending={"reason": "new"}, spawn=spawn) == {
        "started": False, "pending": True}
    spawn.assert_not_called()
    assert json.loads(marker.read_text(encoding="utf-8"))["reason"] == "running"


def test_claimed_marker_prevents_duplicate_spawn_and_preserves_reason(tmp_path):
    from gateway.update_launcher import launch_native_update

    claimed = tmp_path / ".update_pending.claimed.json"
    claimed.write_text(json.dumps({"reason": "existing precise reason"}), encoding="utf-8")
    spawn = Mock()
    result = launch_native_update(
        home=tmp_path, hermes_cmd=["hermes"], pending={"reason": "new reason"}, spawn=spawn,
    )

    assert result == {"started": False, "pending": True}
    spawn.assert_not_called()
    assert json.loads(claimed.read_text(encoding="utf-8"))["reason"] == "existing precise reason"
    assert not (tmp_path / ".update_pending.json").exists()

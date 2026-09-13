"""Initializer death is recoverable; published/uncertain updater ownership is not."""
import json
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest

from gateway.update_launcher import launch_native_update
from gateway.update_notifications import read_pending


_INITIALIZER = '''# isolated-update-initializer-fixture
import os
import sys
from pathlib import Path

def deny_network(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.fork"}:
        raise RuntimeError("initializer fixture forbids external execution/network")
sys.addaudithook(deny_network)
from gateway.update_launcher import launch_native_update
home = Path(sys.argv[1])
checkpoint = int(sys.argv[2])
real_fsync = os.fsync
calls = 0
def interrupt(fd):
    global calls
    real_fsync(fd)
    calls += 1
    if calls == checkpoint or (checkpoint == 3 and calls == 2):
        if checkpoint == 3:
            os.ftruncate(fd, 3)  # A torn staging write must not become admission.
        print("initializing", flush=True)
        sys.stdin.readline()
        os._exit(23)
os.fsync = interrupt
def never_spawn(*args):
    raise AssertionError("fixture must die before spawn")
launch_native_update(home=home, hermes_cmd=["not-an-updater"], pending={"reason": "interrupted"}, spawn=never_spawn)
'''


@pytest.mark.parametrize("checkpoint", [1, 2, 3])
def test_initializer_lock_survives_content_age_but_not_process_death(tmp_path, checkpoint):
    """Real OS lock excludes a second launcher, then dies with its initializer."""
    home = tmp_path / "home"
    home.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", _INITIALIZER, str(home), str(checkpoint)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "initializing"
        # A watcher must never see an empty or partial admission as a request.
        assert read_pending(home) is None
        for path in home.glob(".update*"):
            os.utime(path, (1, 1))  # Age cannot overrule an initializer's live lock.
        before = {p.name: p.read_bytes() for p in home.glob(".update*")}
        spawn = Mock()
        assert launch_native_update(home=home, hermes_cmd=["fixture"], pending={"reason": "duplicate"}, spawn=spawn) == {
            "started": False, "pending": True,
        }
        spawn.assert_not_called()
        assert {p.name: p.read_bytes() for p in home.glob(".update*")} == before
    finally:
        # No signals or updater: the isolated fixture exits itself while holding the lock.
        _, errors = child.communicate("exit\n", timeout=10)
    assert child.returncode == 23, errors
    assert read_pending(home) is None
    spawn = Mock()
    assert launch_native_update(home=home, hermes_cmd=["fixture"], pending={"reason": "recovered"}, spawn=spawn)["started"]
    spawn.assert_called_once()
    record = read_pending(home)
    assert record is not None and record[1]["reason"] == "recovered"


@pytest.mark.parametrize("boundary", ["published", "spawn_error", "spawn_interrupt", "claimed", "claim_race", "legacy_empty", "legacy_partial"])
def test_unresolved_publication_is_never_reclaimed_on_restart(tmp_path, monkeypatch, boundary):
    """Publication is the conservative no-retry fence, even if spawn never returned."""
    from gateway import update_launcher
    marker = tmp_path / ".update_pending.json"
    if boundary == "claim_race":
        from gateway import status
        acquire = status._try_acquire_file_lock
        def claim_during_admission(handle):
            acquired = acquire(handle)
            (tmp_path / ".update_pending.claimed.json").write_bytes(b'{"reason":"prior updater"}')
            return acquired
        monkeypatch.setattr(status, "_try_acquire_file_lock", claim_during_admission)
        (tmp_path / ".update_output.txt").write_bytes(b"prior writer output")
        (tmp_path / ".update_admission.lock").write_text("\n")
    elif boundary.startswith("legacy"):
        marker.write_bytes(b"" if boundary == "legacy_empty" else b'{"reason":')
    else:
        def uncertain_spawn(cmd, output, exit_code):
            output.write_bytes(b"possibly running writer\n")
            if boundary == "claimed":
                marker.rename(tmp_path / ".update_pending.claimed.json")
            raise (OSError("unknown spawn outcome") if boundary == "spawn_error" else KeyboardInterrupt())
        if boundary == "published":
            # A failure after the atomic syscall cannot prove publication did not happen.
            replace = update_launcher.os.replace
            def interrupted_replace(source, target):
                replace(source, target)
                raise KeyboardInterrupt()
            monkeypatch.setattr(update_launcher.os, "replace", interrupted_replace)
        with pytest.raises((OSError, KeyboardInterrupt)):
            launch_native_update(home=tmp_path, hermes_cmd=["fixture"], pending={"reason": "unresolved"}, spawn=uncertain_spawn)
        record = read_pending(tmp_path)
        assert record is not None and record[1]["reason"] == "unresolved"
        assert json.loads(record[0].read_text())["notification_version"] == 2
    before = {p.name: p.read_bytes() for p in tmp_path.glob(".update*")}
    if boundary == "claim_race":
        before[".update_pending.claimed.json"] = b'{"reason":"prior updater"}'
    spawn = Mock()
    assert launch_native_update(home=tmp_path, hermes_cmd=["fixture"], pending={"reason": "replacement"}, spawn=spawn) == {
        "started": False, "pending": True,
    }
    spawn.assert_not_called()
    assert {p.name: p.read_bytes() for p in tmp_path.glob(".update*")} == before

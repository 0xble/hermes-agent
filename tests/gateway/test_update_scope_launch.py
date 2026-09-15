"""Native owned-process launch handshake tests. No systemd or live updater runs."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from gateway.update_launcher import launch_native_update, observe_scope_launch, scope_launch_handshake
from gateway.update_notifications import final_outcome, save_pending


# Only temporary helper scripts execute, never hermes_cli or a service command.
@pytest.mark.live_system_guard_bypass
@pytest.mark.macos_only
@pytest.mark.parametrize("mode", ["fail", "zero", "entered", "entry_refused"])
def test_scope_launch_requires_entry_before_updater(tmp_path, monkeypatch, mode):
    import gateway.slash_commands as sc

    # This owned executable models systemd's documented in-place exec contract.
    # Native Linux/systemd integration still belongs on a supervised Linux host.
    def wrap(argv):
        if mode == "entry_refused":
            marker, _ = scope_launch_handshake(tmp_path)
            marker.mkdir()  # O_EXCL must fail closed before the updater runs.
        return [sys.executable, "-c", "import os,sys; os.execvp(sys.argv[1],sys.argv[1:])", *argv], dict(os.environ)

    # Keep the actual bash command at the end, just as systemd-run does.
    def wrapper(argv):
        if mode in {"fail", "zero"}:
            return [sys.executable, "-c", f"raise SystemExit({1 if mode == 'fail' else 0})", *argv], dict(os.environ)
        return wrap(argv)

    monkeypatch.setattr(sc, "_systemd_scope_wrap_if_supervised", wrapper)
    children = []
    real_spawn = sc._popen_detached_update
    def capture_spawn(*args, **kwargs):
        child = real_spawn(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(sc, "_popen_detached_update", capture_spawn)
    ran = tmp_path / "ran"
    helper = tmp_path / "updater.py"
    helper.write_text("from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('ran')\n")
    launch_native_update(home=tmp_path, hermes_cmd=[sys.executable, str(helper), str(ran)],
                         pending={"reason": "test"}, spawn=sc._spawn_detached_update)
    child_code = children[0].wait(timeout=5)
    pending = json.loads((tmp_path / ".update_pending.json").read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if (tmp_path / ".update_process_exit_code").exists() or (mode == "entry_refused" and list(tmp_path.glob('.update_scope_entered-*'))):
            break
        time.sleep(.02)
    assert (tmp_path / ".update_pending.json").exists()
    if mode in {"fail", "zero"}:
        assert not ran.exists()
        assert final_outcome(tmp_path, pending)[0] is False
    elif mode == "entered":
        assert ran.read_text() == "ran"
        assert (tmp_path / ".update_process_exit_code").read_text() == "0"
    else:
        assert child_code == 125
        assert not ran.exists()
        assert final_outcome(tmp_path, pending) is None


@pytest.mark.parametrize("mode", ["progress", "successor", "changed", "claimed", "entered", "unreadable", "live", "delayed"])
def test_scope_failure_receipt_is_bound_to_admission(tmp_path, monkeypatch, mode):
    pending_path = tmp_path / ".update_pending.json"
    payload = {"reason": "identical", "launch_id": "caller-controlled"}
    launch_native_update(home=tmp_path, hermes_cmd=[], pending=payload, spawn=lambda *a: None)
    original = json.loads(pending_path.read_text())
    entered, identity = scope_launch_handshake(tmp_path)
    assert original["launch_id"] != payload["launch_id"]
    if mode == "progress":
        inode = pending_path.stat().st_ino
        save_pending(pending_path, {**original, "updating_notified": True, "output_offset": 42})
        assert pending_path.stat().st_ino != inode
    elif mode == "successor":
        pending_path.unlink()  # Model a completed prior request and new identical payload.
        launch_native_update(home=tmp_path, hermes_cmd=[], pending=payload, spawn=lambda *a: None)
        assert json.loads(pending_path.read_text())["launch_id"] != original["launch_id"]
    elif mode == "changed":
        save_pending(pending_path, {**original, "reason": "changed"})
    elif mode == "claimed":
        (tmp_path / ".update_pending.claimed.json").write_text(json.dumps(original))
    elif mode == "entered":
        entered.write_text("entered")
    elif mode == "unreadable":
        real_lstat = type(entered).lstat
        def lstat(path, *args, **kwargs):
            if path == entered:
                raise PermissionError("unreadable")
            return real_lstat(path, *args, **kwargs)
        monkeypatch.setattr(type(entered), "lstat", lstat)
    command = ("import time; time.sleep(5)" if mode == "live" else
               "import time; time.sleep(.2); raise SystemExit(1)" if mode == "delayed" else
               "raise SystemExit(1)")
    process = subprocess.Popen([sys.executable, "-c", command])
    try:
        if mode not in {"live", "delayed"}:
            process.wait(timeout=5)
        if mode == "live":
            observer = threading.Thread(target=observe_scope_launch,
                                        args=(process, tmp_path, entered, identity),
                                        kwargs={"poll_interval": .01}, daemon=True)
            observer.start()
            time.sleep(.15)
            assert observer.is_alive()
            entered.write_text("entered")
            observer.join(timeout=5)
            assert not observer.is_alive()
        else:
            observe_scope_launch(process, tmp_path, entered, identity, poll_interval=.01)
        assert pending_path.exists()
        assert (tmp_path / ".update_process_exit_code").exists() == (mode in {"progress", "delayed"})
        if mode == "live":
            assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)

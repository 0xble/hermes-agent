"""Real login rc options must not defeat durable command output or exit collection."""
from pathlib import Path
import shutil
import shlex

import pytest

from tools.process_registry import ProcessRegistry


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shell_name", ["bash", "zsh"])
@pytest.mark.parametrize("errexit", [False, True], ids=["noclobber", "noclobber-errexit"])
@pytest.mark.parametrize("spawn_kind", ["ordinary", "persistent", "survivor", "worker"])
@pytest.mark.parametrize("failure", ["exit 3", "false; printf 'must-not-run\\n'"])
def test_login_rc_options_preserve_output_and_atomic_exit(
    tmp_path, monkeypatch, shell_name, errexit, spawn_kind, failure,
):
    from tools.environments.local import _find_shell

    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / "hermes"))
    monkeypatch.setenv("ZDOTDIR", str(home))
    monkeypatch.setenv("SHELL", shell)
    # bash login shells read .bash_profile, not .bashrc directly.
    (home / ".bash_profile").write_text('. "$HOME/.bashrc"\n', encoding="utf-8")
    rc = "setopt noclobber\n" if shell_name == "zsh" else "set -o noclobber\n"
    rc += "set -e\n" if errexit else "set +e\n"
    rc += "__hermes_test_rc_loaded=yes\n"
    (home / f".{shell_name}rc").write_text(rc, encoding="utf-8")
    assert _find_shell() == shell
    registry = ProcessRegistry()
    # Observe the wrapper's own exit publication, not the poller's Popen fallback.
    monkeypatch.setattr(registry, "_track_started", lambda *args, **kwargs: None)
    command = (
        "printf 'rc:%s\\n' \"$__hermes_test_rc_loaded\"; "
        # A leftover private temp file must not prevent replacing the exit marker.
        "for run in \"$HERMES_HOME\"/process-runs/proc_*; do "
        "printf 'stale\\n' > \"$run/exit.tmp\"; done; "
        + failure
    )
    if spawn_kind == "ordinary":
        command = "printf 'rc:%s\\n' \"$__hermes_test_rc_loaded\"; " + failure
    if spawn_kind == "worker":
        log = home / "worker.log"
        monkeypatch.setattr("tools.process_registry.worker_output_log", lambda session_id: log)
        command = ("printf 'rc:%s\\n' \"$__hermes_test_rc_loaded\"; "
                   f"printf stale > {shlex.quote(str(log.with_suffix('.exit.tmp')))}; " + failure)
    session = registry.spawn_local(command, cwd=str(home), survive_restart=spawn_kind == "survivor",
                                   persist_on_release=spawn_kind == "persistent")
    assert session.process is not None
    log_path = Path(session.output_log or session.log_path)
    exit_path = log_path.with_suffix(".exit") if session.output_log else Path(session.exit_path)
    try:
        session.process.wait(timeout=10)
        expected = 3 if failure == "exit 3" else (1 if errexit else 0)
        if spawn_kind == "ordinary":
            assert not session.exit_path and not session.log_path
            assert session.process.returncode == expected
        else:
            assert exit_path.read_text(encoding="utf-8-sig").strip() == str(expected)
            assert not exit_path.with_name(exit_path.name + ".tmp").exists()
        if session.output_log:
            registry._log_reader_loop(session)
        elif session.log_path:
            registry._survivor_poller_loop(session)
        else:
            registry._reader_loop(session)
        output = session.output_buffer
        assert "rc:yes" in output, output
        if errexit:
            assert "must-not-run" not in output
        assert session.exit_code == expected
        assert "rc:yes" in session.output_buffer
    finally:
        if session.process.poll() is None:
            session.process.kill()
        session.process.wait(timeout=10)

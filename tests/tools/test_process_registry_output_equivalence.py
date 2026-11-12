"""File-backed ordinary jobs preserve pipe monitoring, streaming and result text."""
import re
import shlex
import subprocess
import sys

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession


def _python_command(source):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _observed(session, streamed):
    # The wrapper adds shell source lines, but not a different diagnostic.
    def normalize(text):
        return re.sub(r"line \d+:", "line N:", text)

    return session._watch_hits, normalize("".join(streamed)), normalize(session.output_buffer)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("persist", [False, True], ids=["ordinary-pipe", "persistent-log"])
@pytest.mark.parametrize("shell_flag", ["-c", "-lic"], ids=["plain-shell", "login-shell"])
@pytest.mark.parametrize("command", [
    _python_command("import sys; sys.stdout.write('PANIC\\n' + 'x' * 600000)"),
    _python_command("import os; os.write(1, b'x' * 4095 + '€終'.encode())"),
    "printf '",
    "printf 'normal-out\\n'; printf 'normal-err\\n' >&2",
    "printf 'bash: no job control in this shell\\nnormal-out\\n'",
], ids=["burst-watch", "split-utf8", "parser-error", "normal", "shell-noise"])
def test_ordinary_output_matches_pipe_reader(tmp_path, monkeypatch, command, shell_flag, persist):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Exercise real bash without user startup files or host-specific dispatch.
    def argv(session, safe_command, unit_suffix, label):
        return ["/bin/bash", shell_flag, f"set +m; {safe_command}"]

    pipe_registry = ProcessRegistry()
    pipe_text = []
    pipe_registry.on_output = lambda session, chunk: pipe_text.append(chunk)
    pipe = ProcessSession(id="proc_pipe_parity", command=command, watch_patterns=["PANIC"])
    pipe.process = subprocess.Popen(argv(pipe, command, pipe.id, "Local"),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    pipe_registry._running[pipe.id] = pipe
    pipe_registry._reader_loop(pipe)

    registry = ProcessRegistry()
    streamed = []
    registry.on_output = lambda session, chunk: streamed.append(chunk)
    monkeypatch.setattr(registry, "_scope_argv", argv)
    # Delay file ingestion until the burst has landed, rather than racing its writer.
    monkeypatch.setattr(registry, "_track_started", lambda *args, **kwargs: None)
    survivor = registry.spawn_local(command, cwd=str(tmp_path), persist_on_release=persist)
    survivor.watch_patterns = ["PANIC"]
    try:
        if persist:
            survivor.process.wait(timeout=10)
            registry._survivor_poller_loop(survivor)
        else:
            registry._reader_loop(survivor)
        assert survivor.exit_code == pipe.exit_code
        assert _observed(survivor, streamed) == _observed(pipe, pipe_text)
        assert survivor.output_buffer, "shell parser failures must retain their diagnostic"
    finally:
        if survivor.process.poll() is None:
            registry.kill_process(survivor.id)


@pytest.mark.parametrize("tail,expected", [(b"\x82\xac", "€"), (b"", "�")],
                         ids=["complete-across-polls", "truncated-at-exit"])
def test_survivor_decoder_spans_polls_and_flushes_at_exit(tmp_path, tail, expected):
    registry = ProcessRegistry()
    streamed = []
    registry.on_output = lambda session, chunk: streamed.append(chunk)
    log = tmp_path / "out.log"
    session = ProcessSession(id="proc_utf8_polls", command="utf8", log_path=str(log))
    log.write_bytes(b"prefix \xe2")
    registry._read_survivor_log(session)
    assert session.output_buffer == "prefix "
    with log.open("ab") as output:
        output.write(tail)
    registry._finish_survivor(session, 0, "exited", "")
    assert session.output_buffer == "prefix " + expected
    assert "".join(streamed) == session.output_buffer
    registry._read_survivor_log(session)
    assert "".join(streamed) == session.output_buffer

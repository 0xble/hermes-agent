"""Classic CLI driver failures must stop at the first lost synchronization."""
import subprocess
import sys
import time
from itertools import cycle
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from tests.e2e.core.cli_tmux import _classic
from tests.e2e.core.cli_tmux._classic import ClassicHome, Pane, kill_home_processes
from tests.e2e.core.cli_tmux.test_classic_cli_live_session import Session


def test_step_fails_at_the_missing_witness_before_driving_more_input():
    pane = Mock()
    pane.wait.return_value = None
    pane.screen.return_value = "still waiting in the approval modal"
    pane.scrollback.return_value = ""
    pane.ch.diagnostics.return_value = "gateway detail"
    with pytest.raises(AssertionError, match="approve_once.*LIVE02 done") as exc:
        Session(pane).step("approve_once", lambda: pane.keys("Enter"), ["LIVE02 done"], timeout=2)
    assert "still waiting in the approval modal" in str(exc.value)
    assert "gateway detail" in str(exc.value)
    pane.settle.assert_not_called()


def _clock(monkeypatch):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(_classic, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    return now


def test_settle_ignores_spinner_frames_but_not_changed_content(monkeypatch):
    now = _clock(monkeypatch)
    pane = Pane(Mock())
    pane.screen = Mock(side_effect=cycle(["⠋ Waiting for approval (59s)", "⠙ Waiting for approval (58s)"]))
    pane.settle(quiet=0.5, timeout=2)
    assert now[0] < 1, "a stable modal must not consume the whole settle deadline"


def test_settle_fails_with_the_last_screen_at_its_deadline(monkeypatch):
    now = _clock(monkeypatch)
    home = Mock()
    home.diagnostics.return_value = "gateway detail"
    pane = Pane(home)
    pane.screen = Mock(side_effect=cycle(["first content", "different content"]))
    with pytest.raises(AssertionError, match="screen did not settle within 2s") as exc:
        pane.settle(quiet=0.5, timeout=2)
    assert "content" in str(exc.value)
    assert "gateway detail" in str(exc.value)
    assert now[0] < 2.5


def test_submit_does_not_send_enter_without_a_composer_acknowledgement():
    pane = Pane(Mock())
    pane.scrollback = Mock(return_value="old output")
    pane.screen = Mock(return_value="modal still active")
    pane.tmux = Mock()
    pane.wait = Mock(return_value=None)
    with pytest.raises(AssertionError, match="composer never showed 'new prompt'"):
        pane.submit("new prompt")
    assert pane.tmux.call_args_list == [call("send-keys", "-t", "p", "-l", "new prompt")]


@pytest.mark.parametrize("prefix", ["", "──", "As"])
def test_submit_accepts_a_slash_command_when_the_menu_displaces_the_prompt(monkeypatch, prefix):
    """Replay the unframed buffer and both post-interrupt hosted redraws."""
    now = _clock(monkeypatch)
    pane = Pane(Mock())
    pane.tmux = Mock()
    pane.cursor_row = Mock(return_value=f"{prefix}/compress")
    before = "/compress           - Compress conversation context\n❯ STEP8 tell me a very long story\n"
    after = before + f"{prefix}/compress   \n\n /compress  Compress conversation context\n"
    calls = {"n": 0}

    def scrollback():
        calls["n"] += 1
        return before if calls["n"] == 1 else after

    pane.scrollback = scrollback
    pane.submit("/compress")
    assert pane.tmux.call_args_list == [
        call("send-keys", "-t", "p", "-l", "/compress"),
        call("send-keys", "-t", "p", "Enter"),
    ]
    assert now[0] < 10


@pytest.mark.parametrize("visible", [
    "/comp\n /compress  Compress conversation context\n",
    "❯ /compress\n /compress  Compress conversation context\n",
    "/compress\n",
])
def test_submit_does_not_treat_a_completion_suggestion_as_the_buffer(monkeypatch, visible):
    now = _clock(monkeypatch)
    pane = Pane(Mock())
    pane.tmux = Mock()
    pane.screen = Mock(return_value="menu still open")
    # Even if auto-suggest paints the full command, the insertion cursor is
    # still after the prefix. A menu or ghost suffix is not an acknowledgement.
    pane.cursor_row = Mock(return_value="/comp")
    pane.dead = Mock(return_value=False)
    pane.ch.diagnostics = Mock(return_value="detail")
    before = "idle\n"
    after = before + visible
    calls = {"n": 0}

    def scrollback():
        calls["n"] += 1
        return before if calls["n"] == 1 else after

    pane.scrollback = scrollback
    with pytest.raises(AssertionError, match="composer never showed '/compress'"):
        pane.submit("/compress")
    assert pane.tmux.call_args_list == [call("send-keys", "-t", "p", "-l", "/compress")]
    assert now[0] >= 10


def test_composer_rejects_an_unchanged_cursor_and_transcript(monkeypatch):
    _clock(monkeypatch)
    pane = Pane(Mock())
    pane.tmux = Mock()
    pane.scrollback = Mock(return_value="As/compress\n")
    pane.cursor_row = Mock(return_value="As/compress")
    pane.dead = Mock(return_value=False)
    pane.screen = Mock(return_value="As/compress")
    with pytest.raises(AssertionError, match="composer never showed"):
        pane.submit("/compress")
    assert pane.tmux.call_args_list == [call("send-keys", "-t", "p", "-l", "/compress")]


@pytest.mark.parametrize("row,x,expected", [
    ("As/compress", 11, "As/compress"),
    ("──/compress", 11, "──/compress"),
    ("❯ /compress", 7, "❯ /comp"),
    ("界/compress", 11, "界/compress"),
])
def test_cursor_row_uses_terminal_cells_and_excludes_ghost_text(row, x, expected):
    pane = Pane(Mock())
    pane.tmux = Mock(return_value=f"transcript\n{row}\n /compress menu\n{x} 1\n")
    assert pane.cursor_row() == expected


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("command_hangs", [False, True])
def test_non_tty_run_does_not_wait_for_a_descendant_to_close_output(tmp_path, monkeypatch, command_hangs):
    home = ClassicHome(tmp_path / "run").write("http://127.0.0.1:9")
    ready, release, done = (tmp_path / name for name in ("child-ready", "child-release", "child-done"))
    child = (
        "from pathlib import Path\nimport time\n"
        f"ready, release, done = [Path(p) for p in {list(map(str, (ready, release, done)))!r}]\n"
        "ready.touch()\ndeadline = time.monotonic() + 15\n"
        "while not release.exists() and time.monotonic() < deadline: time.sleep(0.05)\n"
        "done.touch()\n"
    )
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "print('parent output', flush=True); "
        f"time.sleep({30 if command_hangs else 0})"
    )
    monkeypatch.setattr(home, "argv", lambda *args: [sys.executable, "-c", script])
    try:
        if command_hangs:
            with pytest.raises(subprocess.TimeoutExpired) as exc:
                home.run("probe", timeout=2)
            assert "parent output" in exc.value.output
        else:
            result = home.run("probe", timeout=5)
            assert result.returncode == 0
            assert "parent output" in result.stdout
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "the inheriting child never started"
        assert not done.exists(), "run waited for the inheriting child to close output"
    finally:
        # The successful command can already be gone, leaving an orphan. Release
        # the inert child and await its exit without bypassing the signal guard.
        release.touch()
        deadline = time.monotonic() + 5
        while home.pids() and time.monotonic() < deadline:
            time.sleep(0.05)
        kill_home_processes(home.hermes_home)
    assert not home.pids()

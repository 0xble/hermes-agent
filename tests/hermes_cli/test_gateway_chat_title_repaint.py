"""Force the bare /title's second paint to follow a captured Session ID line."""
import os

import pytest

from tests.hermes_cli import test_gateway_chat_commands_live as commands


@pytest.mark.platforms("linux")
def test_classic_title_waits_for_delayed_output(tmp_path, monkeypatch):
    fifo = tmp_path / "release-title"
    blocked = tmp_path / "title-blocked"
    os.mkfifo(fifo)
    wrapper = tmp_path / "delayed_client.py"
    wrapper.write_text(f'''import runpy
from pathlib import Path
import cli
paint = cli._cprint
def delayed(text):
    if "Title: View Title" in text:
        Path({str(blocked)!r}).touch()
        with open({str(fifo)!r}, "rb", buffering=0) as release:
            assert release.read(1) == b"1"
    paint(text)
cli._cprint = delayed
runpy.run_module("hermes_cli.main", run_name="__main__")
''')
    join = commands.shlex.join
    run = commands.subprocess.run
    id_seen = False
    released = False

    def launch(args):
        if args[1:3] == ["-m", "hermes_cli.main"]:
            args = [args[0], str(wrapper), *args[3:]]
        return join(args)

    def capture(args, **kwargs):
        nonlocal id_seen, released
        result = run(args, **kwargs)
        if "capture-pane" in args and "Session ID: " in result.stdout:
            if id_seen and not released and blocked.exists():
                # Capture first, then permit the next paint. The immediate assertion
                # must see this incomplete frame; a screen wait can observe the next.
                assert "Title: View Title" not in result.stdout
                with fifo.open("wb", buffering=0) as release:
                    release.write(b"1")
                released = True
            id_seen = True
        return result

    monkeypatch.setattr(commands.shlex, "join", launch)
    monkeypatch.setattr(commands.subprocess, "run", capture)
    commands.test_classic_view_title_and_usage_match_main(tmp_path)
    assert released, "the real /title output hook was not exercised"

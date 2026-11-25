"""Real terminal probe for the post-interrupt composer acknowledgement race."""
import sys

import pytest

from tests.e2e.core.cli_tmux._classic import ClassicHome, Pane, require_tmux

require_tmux()

pytestmark = pytest.mark.platforms("linux", "macos")


@pytest.mark.parametrize("prefix", ["As", "──", "界"])
def test_composer_acknowledges_input_at_the_cursor_not_its_chrome(tmp_path, monkeypatch, prefix):
    home = ClassicHome(tmp_path / "cursor").write("http://127.0.0.1:9")
    script = (
        "from prompt_toolkit import PromptSession; "
        "from hermes_cli.commands_completion import SlashCommandCompleter; "
        "session = PromptSession(completer=SlashCommandCompleter()); "
        f"text = session.prompt({prefix!r}); "
        "print('SUBMITTED:' + text, flush=True); "
        "session.prompt('done> ')"
    )
    monkeypatch.setattr(home, "argv", lambda *args: [sys.executable, "-c", script])
    pane = Pane(home)
    try:
        pane.launch()
        assert pane.wait([prefix], 10), pane.screen()
        pane.submit("/compress")
        assert pane.wait(["SUBMITTED:/compress"], 10), pane.screen()
    finally:
        pane.kill()

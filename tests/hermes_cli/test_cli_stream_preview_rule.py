"""Streaming previews remain distinguishable from committed transcript and status chrome.

Resize recovery conservatively leaves a paint whose reflow is uncertain rather than erase
transcript rows. A preview must keep its leading ellipsis, without a width-sized frame that
can strand its tail as an apparent second copy of the answer.
"""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("width", [60, 120])
@pytest.mark.parametrize("activity", ["… pending answer words", "… " + "pending answer words " * 8, "Running tests"])
def test_composer_rule_preserves_preview_identity_without_losing_activity(width, activity):
    from cli import HermesCLI
    from hermes_cli.cli_modern_layout import top_rule_fragments

    cli = SimpleNamespace(
        _get_tui_terminal_width=lambda: width,
        _agent_running=True,
        _spinner_text=activity,
        _prompt_start_time=None,
        _spinner_token_flow=lambda: "↓ 42 tok",
        _get_status_bar_session_title=lambda: "Session title",
        _status_bar_display_width=HermesCLI._status_bar_display_width,
        _trim_status_bar_text=HermesCLI._trim_status_bar_text,
    )
    line = "".join(text for _, text in top_rule_fragments(cli))
    assert HermesCLI._status_bar_display_width(line) <= width
    if activity.startswith("… "):
        assert line.startswith("… pending answer words")
        assert "─" not in line and "Session title" not in line
    else:
        assert line.startswith("── ") and activity in line and "Session title" in line
    if len(activity) < 30:
        assert "↓ 42 tok" in line

"""Unknown process exit status is neither success nor a collected failure."""

import pytest

from hermes_cli import cli_process_dock
from tools.process_registry_notifications import format_process_notification, process_completion_display_text


@pytest.mark.parametrize("exit_code,reason,source", [
    (None, "exited", ""),
    (None, "exited", "checkpoint.exit_uncollected"),
    (None, "lost", "survivor_lost"),
    (0, "lost", "survivor_lost"),
    (-15, "lost", "backend_lost"),
])
def test_shared_cli_tui_completion_and_classic_dock_report_unknown(exit_code, reason, source):
    event = dict(type="completion", session_id="proc_unknown", command="build", exit_code=exit_code,
                 completion_reason=reason, termination_source=source)
    title = process_completion_display_text([event])
    text = format_process_notification(event)
    assert "exit status unknown" in title.lower(), title
    assert text is not None
    assert "exit status unknown" in text.lower(), text
    assert "completed normally" not in text and "exit code None" not in text
    status = cli_process_dock.process_status({**event, "status": "exited"})
    row = dict(status=status, exit_code=exit_code, since_exit=1)
    assert status != "done"
    assert cli_process_dock.process_glyph(row) not in {"✔", "✅"}
    assert "exit status unknown" in cli_process_dock.process_activity(row).lower()


def test_shared_completion_retains_real_exit_verdicts():
    base = dict(type="completion", session_id="proc_known", command="build", completion_reason="exited")
    assert "Finished" in process_completion_display_text([{**base, "exit_code": 0}])
    assert "Failed (exit 7)" in process_completion_display_text([{**base, "exit_code": 7}])

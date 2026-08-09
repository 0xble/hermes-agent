"""Interactive /cron timezone surface."""

from __future__ import annotations

from cron.jobs import get_job, list_jobs
from hermes_cli.cli_commands_mixin import CLICommandsMixin


class _CLI(CLICommandsMixin):
    pass


def test_interactive_cron_create_edit_and_list_timezone(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    cli = _CLI()

    cli._handle_cron_command(
        '/cron add "0 9 * * *" "Daily brief" --timezone America/New_York'
    )
    job = list_jobs()[0]
    assert job["timezone"] == "America/New_York"

    cli._handle_cron_command(
        f'/cron edit {job["id"]} --timezone America/Los_Angeles'
    )
    assert get_job(job["id"])["timezone"] == "America/Los_Angeles"

    cli._handle_cron_command("/cron list")
    assert "America/Los_Angeles (explicit)" in capsys.readouterr().out

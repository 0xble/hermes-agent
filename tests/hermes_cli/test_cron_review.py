"""Reviewed delivery can be set, retained, and cleared through the real CLI."""
import argparse
import json

from cron.jobs import create_job, get_job
from hermes_cli.cron import cron_command
from hermes_cli.subcommands.cron import build_cron_parser


def test_edit_sets_preserves_and_clears_review(tmp_path, monkeypatch):
    for name, path in (("CRON_DIR", tmp_path / "cron"), ("JOBS_FILE", tmp_path / "cron/jobs.json"),
                       ("OUTPUT_DIR", tmp_path / "cron/output")):
        monkeypatch.setattr(f"cron.jobs.{name}", path)
    job = create_job(prompt="Daily report", schedule="every 1h", deliver="local")
    parser = argparse.ArgumentParser()
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=cron_command)
    declaration = {"contract": "reports/stripe-brief.yaml", "subject": "139328"}
    cron_command(parser.parse_args(["cron", "edit", job["id"], "--review-json", json.dumps(declaration)]))
    saved = get_job(job["id"])
    assert saved["review"] == {**declaration, "deadline_minutes": 10,
                               "rerun_deadline_minutes": 20, "max_reruns": 1}
    assert saved["output_local_only"] is True
    cron_command(parser.parse_args(["cron", "edit", job["id"], "--name", "Renamed report"]))
    assert get_job(job["id"])["review"] == saved["review"]
    cron_command(parser.parse_args(["cron", "edit", job["id"], "--clear-review"]))
    assert get_job(job["id"])["review"] is None
    assert get_job(job["id"])["output_local_only"] is False

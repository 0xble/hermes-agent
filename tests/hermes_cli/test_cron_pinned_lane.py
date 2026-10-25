"""Cron CLI lane protection survives parser, command, and store boundaries."""

import argparse

import pytest

from cron.jobs import get_job, list_jobs, use_cron_store
from hermes_cli import cron as cron_cli
from hermes_cli.subcommands.cron import build_cron_parser


@pytest.mark.parametrize("create_command", ["create", "add"])
@pytest.mark.parametrize("initial_value", [True, False])
def test_cli_persists_pinned_lane(tmp_path, monkeypatch, create_command, initial_value):
    monkeypatch.setattr(cron_cli, "_warn_if_gateway_not_running", lambda: None)
    parser = argparse.ArgumentParser(prog="hermes")
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=cron_cli.cron_command)
    flag = {True: "--keep-pinned-lane", False: "--no-keep-pinned-lane"}

    with use_cron_store(tmp_path):
        args = parser.parse_args([
            "cron", create_command, "every 1h", "Daily report", flag[initial_value], "--paused",
        ])
        assert args.func(args) == 0
        job = list_jobs(include_disabled=True)[0]
        assert job["keep_pinned_lane"] is initial_value

        for value in (not initial_value, initial_value):
            args = parser.parse_args(["cron", "edit", job["id"], flag[value]])
            assert args.func(args) == 0
            updated = get_job(job["id"])
            assert updated is not None
            assert updated["keep_pinned_lane"] is value

        args = parser.parse_args(["cron", "edit", job["id"], "--name", "Renamed report"])
        assert args.func(args) == 0
        unchanged = get_job(job["id"])
        assert unchanged is not None
        assert unchanged["keep_pinned_lane"] is initial_value

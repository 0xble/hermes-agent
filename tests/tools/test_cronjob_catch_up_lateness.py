"""Catch-up lateness values survive the model-tool and CLI interface boundaries."""

import argparse
import json
from unittest.mock import Mock

import pytest

from hermes_cli import cron as cron_cli
from hermes_cli.subcommands.cron import build_cron_parser
from tools import cronjob_tools
from tools.registry import registry


FIELD = "max_catch_up_lateness_seconds"
OMITTED = object()


@pytest.fixture
def job_boundary(monkeypatch):
    job = {
        "id": "lateness-job", "name": "Report", "prompt": "Report",
        "schedule": {"kind": "interval", "minutes": 60},
        "schedule_display": "every 1h", "next_run_at": "2099-01-01T00:00:00+00:00",
        "deliver": "local", "enabled": True, "state": "scheduled",
    }
    create = Mock(return_value=job)
    update = Mock(return_value=job)
    monkeypatch.setattr("cron.scheduler.create_job_with_scheduler_registration", create)
    monkeypatch.setattr(cronjob_tools, "update_job", update)
    monkeypatch.setattr(cronjob_tools, "resolve_job_ref", lambda ref: job)
    monkeypatch.setattr("cron.jobs.resolve_job_ref", lambda ref: job)
    monkeypatch.setattr(cronjob_tools, "_notify_provider_jobs_changed_safe", lambda: None)
    monkeypatch.setattr(cron_cli, "_builtin_gateway_liveness", lambda: True)
    return create, update


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("value", [90.5, 0, "", None, OMITTED],
                         ids=["finite", "strict-zero", "clear", "null", "omitted"])
def test_model_tool_forwards_catch_up_lateness(job_boundary, action, value):
    create, update = job_boundary
    args = {"action": action, "deliver": "local"}
    if action == "create":
        args.update(schedule="every 1h", prompt="Report")
    else:
        args.update(job_id="lateness-job", name="Renamed report")
    if value is not OMITTED:
        args[FIELD] = value

    raw_result = registry.dispatch("cronjob_manage", args)
    assert isinstance(raw_result, str)
    result = json.loads(raw_result)
    assert result["success"], result
    forwarded = create.call_args.kwargs if action == "create" else update.call_args.args[1]
    if action == "update" and (value is OMITTED or value is None):
        assert FIELD not in forwarded
    else:
        assert FIELD in forwarded
        assert forwarded[FIELD] == (None if value is OMITTED else value)

    # The advertised model interface must include numeric seconds and the clear sentinel.
    schema = registry.get_schema("cronjob_manage")
    assert schema is not None
    parameters = schema["parameters"]
    assert FIELD not in parameters.get("required", [])
    alternatives = parameters["properties"][FIELD]["anyOf"]
    assert {"type": "number", "minimum": 0} in alternatives
    assert {"type": "string", "enum": [""]} in alternatives


@pytest.mark.parametrize("action", ["create", "update", "list"])
@pytest.mark.parametrize("value", [90.5, 0, "", None, OMITTED],
                         ids=["finite", "strict-zero", "cleared", "null", "unset"])
def test_model_tool_reports_catch_up_lateness(job_boundary, monkeypatch, action, value):
    create, _ = job_boundary
    job = create.return_value
    expected = None if value is OMITTED or value == "" else value
    if value is not OMITTED:
        job[FIELD] = expected
    monkeypatch.setattr(cronjob_tools, "list_jobs", Mock(return_value=[job]))
    args = {"action": action}
    if action == "create":
        args.update(schedule="every 1h", prompt="Report", deliver="local")
    elif action == "update":
        args.update(job_id="lateness-job", name="Renamed report")
    if action != "list" and value is not OMITTED:
        args[FIELD] = value

    raw_result = registry.dispatch("cronjob_manage", args)
    assert isinstance(raw_result, str)
    result = json.loads(raw_result)

    assert result["success"], result
    view = result["jobs"][0] if action == "list" else result["job"]
    # Explicit null means unlimited, including legacy records with no stored field.
    # Zero is a strict bound and must not disappear through truthy-only projection.
    assert FIELD in view
    assert view[FIELD] == expected


@pytest.mark.parametrize("command, flags, expected", [
    (command, flags, expected)
    for command in ("create", "add", "edit")
    for flags, expected in (
        (["--max-catch-up-lateness-seconds", "90.5"], 90.5),
        (["--max-catch-up-lateness-seconds", "0"], 0.0),
        ([], None),
    )
] + [("edit", ["--clear-max-catch-up-lateness"], "")])
def test_cli_forwards_catch_up_lateness(job_boundary, command, flags, expected):
    create, update = job_boundary
    parser = argparse.ArgumentParser(prog="hermes")
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=cron_cli.cron_command)
    positional = ["lateness-job", "--name", "Renamed report"] if command == "edit" else [
        "every 1h", "Report", "--deliver", "local"]
    args = parser.parse_args(["cron", command, *positional, *flags])

    assert args.func(args) == 0
    forwarded = update.call_args.args[1] if command == "edit" else create.call_args.kwargs
    if command == "edit" and expected is None:
        assert FIELD not in forwarded
    else:
        assert FIELD in forwarded
        assert forwarded[FIELD] == expected

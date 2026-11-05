"""Keep the trusted and opaque wrapper grammars aligned with heavy-slot's parser."""

import argparse
import runpy
import shlex
from pathlib import Path

import pytest

from cron import lifecycle_guard as guard
from hermes_platform.resolver import locate_command


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("value", ["incident", "urgent", "normal", "gate", "user", "bogus", "", "URGENT"])
@pytest.mark.parametrize("attached", [False, True])
def test_urgency_preserves_wrapper_boundaries(tmp_path, monkeypatch, value, attached):
    helper = tmp_path / ".local" / "bin" / "heavy-slot"
    helper.parent.mkdir(parents=True)
    helper.write_text('#!/usr/bin/env python3\n"""/dev/null"""\n', encoding="utf-8")
    helper.chmod(0o755)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PATH", str(helper.parent))
    options = [f"--urgency={value}"] if attached else ["--urgency", value]
    valid = value in {"incident", "urgent", "normal"}
    for wrapper in (str(helper), "heavy-slot"):
        segment = [wrapper, *options, "--", "true"]
        target, refusal = guard._heavy_slot_fallback_command_index(segment, 0)
        if valid:
            assert (target, refusal) == (len(segment) - 1, None)
            if wrapper == str(helper):
                assert guard._heavy_slot_command_index(segment, 0) == target
            for payload, blocked in ((["true"], False), (["hermes", "gateway", "restart"], True)):
                command = shlex.join([wrapper, *options, "--", *payload])
                assert guard.scan_gateway_lifecycle(command) == (blocked, None)
        else:
            assert target is None
            assert refusal is not None and "invalid option" in refusal
            assert guard._heavy_slot_command_index(segment, 0) is None
            unsafe, reason = guard.scan_gateway_lifecycle(shlex.join(segment))
            assert unsafe and reason is not None and "invalid option" in reason
    for options in (["--urgency"], ["--urgency", "--", "true"], ["--urgen", "urgent", "--", "true"]):
        unsafe, refusal = guard.scan_gateway_lifecycle(shlex.join([str(helper), *options]))
        assert unsafe and refusal is not None


@pytest.mark.platforms("posix")
@pytest.mark.requires_heavy_slot
def test_installed_helper_option_table_matches_guard(monkeypatch):
    resolution = locate_command("heavy-slot")
    if not resolution.command:
        pytest.skip("requires_heavy_slot: installed host helper is unavailable; no parser to compare")

    class ParserCaptured(Exception):
        pass

    parsers = []

    def capture(parser, *args, **kwargs):
        parsers.append(parser)
        raise ParserCaptured

    # Stop at argument parsing: no command, ticket, lock or admission is executed.
    # Unlike --help, the live table also exposes the hidden --gate-fd handshake.
    with monkeypatch.context() as patch:
        patch.setattr(argparse.ArgumentParser, "parse_args", capture)
        with pytest.raises(ParserCaptured):
            runpy.run_path(resolution.command[0], run_name="__main__")
    actions = {
        option: action
        for action in parsers[0]._actions
        for option in action.option_strings
    }
    valued = {option for option, action in actions.items() if action.nargs != 0}
    flags = set(actions) - valued
    assert valued == set(guard._HEAVY_SLOT_VALUE_OPTIONS)
    assert all(actions[option].nargs is None for option in valued)
    assert flags == guard._HEAVY_SLOT_FLAG_OPTIONS
    urgency = actions["--urgency"]
    classes = set(urgency.choices or urgency.metavar.split("|"))
    validate = guard._HEAVY_SLOT_VALUE_OPTIONS["--urgency"]
    assert {value for value in classes | {"incident", "urgent", "gate", "user", "normal", "bogus", "", "URGENT"} if validate(value)} == classes

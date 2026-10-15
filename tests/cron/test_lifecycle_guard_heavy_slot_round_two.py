"""Opaque helper grammar must never hide executable tails or change unrelated budgets."""

from pathlib import Path

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle

pytestmark = pytest.mark.platforms("posix")


@pytest.mark.parametrize("wrapper", ["heavy-slot", "{installed}"])
@pytest.mark.parametrize("option", ["--lab x", "--l=x", "--unknown"])
def test_unknown_options_cannot_skip_to_a_command_delimiter(tmp_path, monkeypatch, wrapper, option):
    home = tmp_path / "home"
    installed = home / ".local" / "bin" / "heavy-slot"
    installed.parent.mkdir(parents=True)
    installed.write_text('#!/usr/bin/env python3\n"""/dev/null"""\n')
    installed.chmod(0o755)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("PATH", str(installed.parent))
    restart = tmp_path / "restart.sh"
    restart.write_text("hermes gateway restart\n")
    command = f"{wrapper.format(installed=installed)} {option} bash {restart} -- true"
    unsafe, refusal = scan_gateway_lifecycle(command)
    assert unsafe, command
    assert refusal is not None and "heavy-slot wrapper" in refusal
    assert option.split()[0] in refusal


@pytest.mark.parametrize(("command", "blocked"), [
    ("bash /dev/stdin < restart.sh", True),
    ("bash /dev/stdout", True),
    ("bash /dev/stderr", True),
    ("bash /dev/fd/0", True),
    ("nohup " * 9 + "true", False),
    ("nohup " * 9 + "heavy-slot --label t -- true", True),
    ("heavy-slot -h", False),
    ("heavy-slot --help", False),
    ("bash /dev/null", False),
])
def test_only_heavy_slot_gets_new_budget_refusals_and_only_empty_devices_are_exempt(
    tmp_path, monkeypatch, command, blocked,
):
    helper = tmp_path / "heavy-slot"
    helper.write_text('#!/usr/bin/env python3\n"""/dev/null"""\n')
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / "restart.sh").write_text("hermes gateway restart\n")
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe is blocked, (command, refusal)


@pytest.mark.parametrize("spelling", ["//dev/stdin", "/dev//stdin", "/dev/./stdin", "/dev/fd/./0"])
def test_alternate_stream_device_spellings_stay_fail_closed(tmp_path, spelling):
    from cron.lifecycle_guard import _is_fail_closed_device_path

    assert _is_fail_closed_device_path(spelling)
    assert not _is_fail_closed_device_path("/dev/fd/../x/r.sh")
    (tmp_path / "restart.sh").write_text("hermes gateway restart\n")
    unsafe, _ = scan_gateway_lifecycle(f"bash {spelling} < restart.sh", cwd=str(tmp_path))
    assert unsafe is True


@pytest.mark.parametrize("command", [
    "echo hi 2>/dev/null",
    "echo hi </dev/null",
    "echo hi >/dev/tty",
    "echo hi > /dev/zero 2>&1",
    "cat < /dev/stdin",
    "git grep foo 2>/dev/null",
    "_hermes_heavy_slot 'x' scripts/run_tests.sh t.py 2>/dev/null",
    "heavy-slot --label t -- bash scripts/run_tests.sh t.py 2>/dev/null",
])
def test_device_redirect_targets_are_never_scripts(tmp_path, monkeypatch, command):
    # Redirect targets and read sources are never executed, so no /dev path there may refuse.
    helper = tmp_path / "heavy-slot"
    helper.write_text('#!/usr/bin/env python3\n"""/dev/null"""\n')
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_tests.sh").write_text("echo tests\n")
    (tmp_path / "t.py").write_text("print(1)\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


def test_device_redirect_does_not_hide_an_executed_restart_script(tmp_path):
    (tmp_path / "restart.sh").write_text("hermes gateway restart\n")
    assert scan_gateway_lifecycle("bash restart.sh 2>/dev/null", cwd=str(tmp_path))[0] is True

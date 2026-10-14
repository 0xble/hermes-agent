"""Only the installed helper's exact, explicit-path wrapper grammar is transparent."""

import os
import shlex
from pathlib import Path

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle

pytestmark = pytest.mark.platforms("posix")


def _script(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def trusted_slot(tmp_path, monkeypatch):
    home = tmp_path / "home"
    installed = home / ".local" / "bin" / "heavy-slot"
    installed.parent.mkdir(parents=True)
    installed.write_text('#!/usr/bin/env python3\n"""/dev/null"""\n')
    installed.chmod(0o755)
    monkeypatch.setattr(Path, "home", lambda: home)
    return installed


def test_lpg_wrapper_scan_is_allowed(tmp_path, trusted_slot):
    recovery = _script(tmp_path, "run-recovery.sh", "#!/bin/sh\nprintf 'proof\\n'\n")
    command = (
        f"env -u HEAVY_SLOT_HELD {trusted_slot} --label lpg-v2-fresh-copy-old-vs-batched-recovery "
        f"-- bash {recovery}"
    )
    assert scan_gateway_lifecycle(command) == (False, None)


@pytest.mark.parametrize("options", [
    "", "--label proof", "--label=proof", "--label=", "--gate-fd 3", "--gate-fd=3",
    "--label proof --gate-fd 3 --label=again",
])
@pytest.mark.parametrize("prefix", ["", "env -u HEAVY_SLOT_HELD ", "nohup ", "timeout 30 "])
def test_exact_wrappers_scan_the_wrapped_command(tmp_path, trusted_slot, options, prefix):
    restart = _script(tmp_path, "restart.sh", "#!/bin/sh\nhermes gateway restart\n")
    assert scan_gateway_lifecycle(f"{prefix}{trusted_slot} {options} -- bash {restart}") == (True, None)


@pytest.mark.parametrize("options", [
    "--l x", "--lab x", "--g 3", "--unknown x", "--label", "--gate-fd nope",
    "--gate-fd=", "--label --unknown", "run", "--label proof bash",
])
def test_unknown_grammar_reads_the_helper_strictly(tmp_path, trusted_slot, options):
    trusted_slot.write_text("#!/usr/bin/env python3\nhermes gateway restart\n", encoding="utf-8")
    benign = _script(tmp_path, "benign.sh", "#!/bin/sh\ntrue\n")
    unsafe, refusal = scan_gateway_lifecycle(f"{trusted_slot} {options} -- bash {benign}")
    assert (unsafe, refusal) == (True, None)


def test_missing_double_dash_stays_strict(tmp_path, trusted_slot):
    trusted_slot.write_text("#!/usr/bin/env python3\nhermes gateway restart\n", encoding="utf-8")
    restart = _script(tmp_path, "restart.sh", "#!/bin/sh\ntrue\n")
    unsafe, refusal = scan_gateway_lifecycle(f"{trusted_slot} --label proof bash {restart}")
    assert (unsafe, refusal) == (True, None)


def test_identity_not_basename_controls_trust(tmp_path, trusted_slot):
    alias = tmp_path / "different-name"
    alias.symlink_to(trusted_slot)
    benign = _script(tmp_path, "benign.sh", "#!/bin/sh\ntrue\n")
    assert scan_gateway_lifecycle(f"{alias} --label proof -- bash {benign}") == (False, None)
    shadow = _script(tmp_path, "heavy-slot", '#!/usr/bin/env python3\nhermes gateway restart\n')
    unsafe, refusal = scan_gateway_lifecycle(f"{shadow} --label proof -- bash {benign}")
    assert (unsafe, refusal) == (True, None)


@pytest.mark.parametrize("prefix", ["", "PATH=/tmp/evil ", "export PATH=/tmp/evil; ", "env PATH=/tmp/evil "])
def test_bare_names_stay_strict_without_refusing_device_prose(tmp_path, trusted_slot, monkeypatch, prefix):
    monkeypatch.setenv("PATH", str(trusted_slot.parent))
    benign = _script(tmp_path, "benign.sh", "#!/bin/sh\ntrue\n")
    assert scan_gateway_lifecycle(f"{prefix}heavy-slot --label proof -- bash {benign}") == (False, None)


def test_path_shadowed_bare_heavy_slot_is_strict(tmp_path, monkeypatch):
    shadow = _script(tmp_path, "heavy-slot", '#!/usr/bin/env python3\nhermes gateway restart\n')
    monkeypatch.setenv("PATH", str(shadow.parent))
    assert scan_gateway_lifecycle("heavy-slot --label proof -- true") == (True, None)


@pytest.mark.parametrize(
    "command",
    [
        "heavy-slot --help",
        "heavy-slot --label t -- bash scripts/run_tests.sh tests/a.py",
    ],
)
def test_bare_heavy_slot_ignores_python_docstring_device_path(
    tmp_path, trusted_slot, monkeypatch, command,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    _script(scripts, "run_tests.sh", "#!/bin/sh\ntrue\n")
    monkeypatch.setenv("PATH", str(trusted_slot.parent))
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize(
    "command_template",
    [
        "heavy-slot --label t -- bash scripts/r.sh",
        "nohup heavy-slot --label t -- bash scripts/r.sh",
        "heavy-slot --label t -- ./scripts/r.sh",
        "heavy-slot --label t -- . scripts/r.sh",
        "{trusted} --bogus -- bash scripts/r.sh",
        "{trusted} --label t bash scripts/r.sh",
        "{trusted} --label t -- bash scripts/r.sh",
    ],
)
def test_bare_and_unknown_heavy_slot_wrappers_scan_the_wrapped_command(
    tmp_path, trusted_slot, monkeypatch, command_template,
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    _script(scripts, "r.sh", "#!/bin/sh\nhermes gateway restart\n")
    monkeypatch.setenv("PATH", str(trusted_slot.parent))
    command = command_template.format(trusted=trusted_slot)
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe is True
    if "--bogus" in command:
        assert refusal is not None and "unknown option `--bogus`" in refusal
    else:
        assert refusal is None


def test_heavy_slot_device_path_predicate_is_exact():
    from cron.lifecycle_guard import _is_non_executable_device_path

    assert _is_non_executable_device_path("/dev/null")
    assert _is_non_executable_device_path("/dev/zero")
    assert not _is_non_executable_device_path("/dev/fd/0")
    assert not _is_non_executable_device_path("/dev/fd/123")
    assert not _is_non_executable_device_path("/dev/fd/../root/restart.sh")
    assert not _is_non_executable_device_path("/dev/fd/../cwd/restart.sh")


@pytest.mark.parametrize("wrapper", ["heavy-slot", "{trusted}"])
@pytest.mark.parametrize("prefix", ["--bogus --", "--label t"])
@pytest.mark.parametrize("tail", [
    "env -S {payload}", "su -c {payload}", "runuser -c {payload}",
    "env -S {payload} -- true",
])
def test_opaque_heavy_slot_keeps_option_carried_commands(
    tmp_path, trusted_slot, monkeypatch, wrapper, prefix, tail,
):
    child = _script(tmp_path, "child.sh", "#!/bin/sh\nhermes gateway restart\n")
    monkeypatch.setenv("PATH", str(trusted_slot.parent))
    payload = shlex.quote(f"bash {child}")
    command = f"{wrapper.format(trusted=trusted_slot)} {prefix} {tail.format(payload=payload)}"
    unsafe, refusal = scan_gateway_lifecycle(command)
    assert unsafe is True
    if prefix.startswith("--bogus"):
        assert refusal is not None and "unknown option `--bogus`" in refusal
    else:
        assert refusal is None


@pytest.mark.parametrize("options", ["--bogus", "--label", "--gate-fd", "--"])
def test_ambiguous_heavy_slot_command_is_refused_by_name(trusted_slot, options):
    unsafe, refusal = scan_gateway_lifecycle(f"{trusted_slot} {options}")
    assert unsafe is True
    assert refusal is not None and "heavy-slot wrapper" in refusal
    assert "/dev/null" not in refusal


@pytest.mark.parametrize("command_template", [
    "{trusted} --label t -- " + "nohup " * 8 + "true",
    "nohup " * 9 + "{trusted} --label t -- true",
])
def test_exhausted_heavy_slot_tail_is_refused_instead_of_dropped(trusted_slot, command_template):
    command = command_template.format(trusted=trusted_slot)
    unsafe, refusal = scan_gateway_lifecycle(command)
    assert unsafe is True
    assert refusal is not None and "prefix scan budget" in refusal


def test_remote_heavy_slot_fallback_scans_the_wrapped_command(tmp_path):
    slot = tmp_path / "heavy-slot"
    child = tmp_path / "child.sh"
    source = {
        str(slot): '#!/usr/bin/env python3\n"""/dev/null"""\n',
        str(child): "#!/bin/sh\nhermes gateway restart\n",
    }
    assert scan_gateway_lifecycle(
        f"{slot} --label t -- bash {child}", read_remote_script=source.get,
    ) == (True, None)


def test_remote_callback_cannot_attest_local_helper_identity(tmp_path, trusted_slot):
    # The helper stays installed, so only the remote-callback rule keeps it from being peeled.
    blocked = tmp_path / "blocked-fifo"
    os.mkfifo(blocked)
    trusted_slot.write_text(f'#!/usr/bin/env python3\n"""/dev/null"""\n{blocked}\n', encoding="utf-8")
    unsafe, refusal = scan_gateway_lifecycle(
        f"{trusted_slot} --label proof -- true", read_remote_script={}.get,
    )
    assert unsafe is True
    assert refusal is not None and str(blocked) in refusal


def test_peel_does_not_hide_an_untrusted_outer_wrapper(tmp_path, trusted_slot):
    outer = _script(tmp_path, "env", "#!/bin/sh\nhermes gateway restart\n")
    unsafe, refusal = scan_gateway_lifecycle(f"{outer} {trusted_slot} --label proof -- true")
    assert (unsafe, refusal) == (True, None)


def test_shell_command_payload_is_still_scanned(trusted_slot):
    assert scan_gateway_lifecycle(f"{trusted_slot} --label proof -- bash -c 'hermes gateway restart'") == (True, None)


def test_string_wrapper_payload_after_heavy_slot_is_scanned(tmp_path, trusted_slot):
    child = _script(tmp_path, "child.sh", "#!/bin/sh\nhermes gateway restart\n")
    payload = shlex.quote(f"bash {child}")
    assert scan_gateway_lifecycle(f"{trusted_slot} --label proof -- env -S {payload}") == (True, None)


@pytest.mark.parametrize(
    ("outer_nohups", "tail"),
    [
        (8, "bash {child}"),
        (7, "nohup bash {child}"),
        (6, "nohup nohup bash {child}"),
    ],
    ids=["at-helper", "after-helper", "after-helper-twice"],
)
def test_exhaustion_at_or_after_heavy_slot_keeps_helper_strict(
    tmp_path, trusted_slot, monkeypatch, outer_nohups, tail,
):
    """A budget stop never earns the helper peel, whether it lands at or after the helper."""
    monkeypatch.setattr("cron.lifecycle_guard._MAX_PREFIX_PEELS", 8)
    trusted_slot.write_text("#!/bin/sh\nhermes gateway restart\n", encoding="utf-8")
    child = _script(tmp_path, "child.sh", "#!/bin/sh\ntrue\n")
    command = " ".join(
        ["nohup"] * outer_nohups
        + [str(trusted_slot), "--label", "proof", "--"]
        + tail.format(child=shlex.quote(str(child))).split()
    )
    assert scan_gateway_lifecycle(command) == (True, None)


def test_natural_heavy_slot_peel_reaches_wrapped_script(tmp_path, trusted_slot):
    restart = _script(tmp_path, "restart.sh", "#!/bin/sh\nhermes gateway restart\n")
    trusted_slot.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
    command = " ".join(
        ["nohup"] * 5
        + [str(trusted_slot), "--label", "proof", "--", "nohup", "bash", str(restart)]
    )
    assert scan_gateway_lifecycle(command) == (True, None)

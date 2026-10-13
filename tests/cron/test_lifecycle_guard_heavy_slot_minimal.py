"""Only the installed helper's exact, explicit-path wrapper grammar is transparent."""

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
    benign = _script(tmp_path, "benign.sh", "#!/bin/sh\ntrue\n")
    unsafe, refusal = scan_gateway_lifecycle(f"{trusted_slot} {options} -- bash {benign}")
    assert unsafe is True
    assert refusal is not None and "/dev/null" in refusal


def test_missing_double_dash_stays_strict(tmp_path, trusted_slot):
    restart = _script(tmp_path, "restart.sh", "#!/bin/sh\nhermes gateway restart\n")
    unsafe, refusal = scan_gateway_lifecycle(f"{trusted_slot} --label proof bash {restart}")
    assert unsafe is True
    assert refusal is not None and "/dev/null" in refusal


def test_identity_not_basename_controls_trust(tmp_path, trusted_slot):
    alias = tmp_path / "different-name"
    alias.symlink_to(trusted_slot)
    benign = _script(tmp_path, "benign.sh", "#!/bin/sh\ntrue\n")
    assert scan_gateway_lifecycle(f"{alias} --label proof -- bash {benign}") == (False, None)
    shadow = _script(tmp_path, "heavy-slot", '#!/usr/bin/env python3\n"""/dev/null"""\n')
    unsafe, refusal = scan_gateway_lifecycle(f"{shadow} --label proof -- bash {benign}")
    assert unsafe is True
    assert refusal is not None and "/dev/null" in refusal


@pytest.mark.parametrize("prefix", ["", "PATH=/tmp/evil ", "export PATH=/tmp/evil; ", "env PATH=/tmp/evil "])
def test_bare_names_never_earn_the_peel(tmp_path, trusted_slot, monkeypatch, prefix):
    monkeypatch.setenv("PATH", str(trusted_slot.parent))
    benign = _script(tmp_path, "benign.sh", "#!/bin/sh\ntrue\n")
    unsafe, refusal = scan_gateway_lifecycle(f"{prefix}heavy-slot --label proof -- bash {benign}")
    assert unsafe is True
    assert refusal is not None and "/dev/null" in refusal


def test_path_shadowed_bare_heavy_slot_is_strict(tmp_path, monkeypatch):
    shadow = _script(tmp_path, "heavy-slot", '#!/usr/bin/env python3\n"""/dev/null"""\n')
    monkeypatch.setenv("PATH", str(shadow.parent))
    unsafe, refusal = scan_gateway_lifecycle("heavy-slot --label proof -- true")
    assert unsafe is True
    assert refusal is not None and "/dev/null" in refusal


def test_remote_callback_cannot_attest_local_helper_identity(trusted_slot):
    source = {str(trusted_slot): '#!/usr/bin/env python3\n/dev/null\n', "/dev/null": "x" * 1_048_577}
    unsafe, refusal = scan_gateway_lifecycle(
        f"{trusted_slot} --label proof -- true", read_remote_script=source.get,
    )
    assert unsafe is True
    assert refusal is not None and "/dev/null" in refusal


def test_peel_does_not_hide_an_untrusted_outer_wrapper(tmp_path, trusted_slot):
    outer = _script(tmp_path, "env", "#!/bin/sh\n/dev/null\n")
    unsafe, refusal = scan_gateway_lifecycle(f"{outer} {trusted_slot} --label proof -- true")
    assert unsafe is True
    assert refusal is not None and "/dev/null" in refusal


def test_shell_command_payload_is_still_scanned(trusted_slot):
    assert scan_gateway_lifecycle(f"{trusted_slot} --label proof -- bash -c 'hermes gateway restart'") == (True, None)

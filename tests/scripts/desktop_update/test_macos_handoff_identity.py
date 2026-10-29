"""The update hand-off shell and Python updater agree on precise macOS process identities.

The no-site recovery reader was adapted from NousResearch/hermes-agent#135407
(bullsheath-design); this fork keeps the strict own-incarnation rule instead of
its whole-second exception.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from hermes_cli import update_lock

pytestmark = pytest.mark.platforms("macos")
ROOT = Path(__file__).resolve().parents[3]
POSIX = ROOT / "scripts" / "desktop-update" / "posix.sh"


def test_no_site_reader_matches_the_shell_and_never_invents_whole_seconds(monkeypatch):
    expected = update_lock.process_create_time()
    assert expected is not None
    command = f'. {shlex.quote(str(POSIX.with_name("marker.sh")))}; proc_ct {os.getpid()}'
    shell = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True,
                           check=True, timeout=20)
    # The shell truncates to milliseconds; an epoch timestamp's default relative
    # tolerance would otherwise admit minutes of clock disagreement.
    assert float(shell.stdout) == pytest.approx(expected, rel=0, abs=0.001)
    assert update_lock._stdlib_create_time(os.getpid()) == pytest.approx(expected, rel=0, abs=0.000001)

    def unavailable(*args, **kwargs):
        raise OSError("libproc unavailable")

    monkeypatch.setattr(ctypes, "CDLL", unavailable)
    assert update_lock._stdlib_create_time(os.getpid()) is None


@pytest.mark.parametrize("no_site", [False, True], ids=["psutil", "frozen-stdlib"])
def test_real_handoff_delegate_acquires_and_releases_without_whole_second_exception(tmp_path, no_site):
    consumer = ROOT
    if no_site:
        from hermes_cli._early_recovery import RECOVERY_CLOSURE
        consumer = tmp_path / "frozen"
        for rel in RECOVERY_CLOSURE:
            target = consumer / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, target)
        (consumer / "hermes_cli" / "__init__.py").write_text("")

    home, install = tmp_path / "home", tmp_path / "install"
    launcher = install / ".hermes" / "bin" / "hermes"
    launcher.parent.mkdir(parents=True)
    (install / "pm").mkdir()
    witness = tmp_path / "witness.json"
    payload = tmp_path / "inert_updater.py"
    payload.write_text(f'''import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(consumer)!r})
from hermes_cli.update_lock import UpdateLock, process_create_time
if '--help' in sys.argv:
    print('--keep-stash'); sys.exit(0)
p = Path(os.environ['HERMES_HOME']) / '.hermes-update-in-progress'
before = p.read_text()
lock = UpdateLock(path=p, install_root=Path.cwd())
ok = lock.acquire()
lock.release()
Path({str(witness)!r}).write_text(json.dumps(dict(ok=ok, before=before, after=p.read_text(), pid=os.getpid(), ct=process_create_time())))
sys.exit(0 if ok else 2)
''')
    launcher.write_text(f'#!/bin/bash\nexec {shlex.quote(sys.executable)} {"-I -S" if no_site else ""} {shlex.quote(str(payload))} "$@"\n')
    launcher.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "HERMES_HOME": str(home),
           "TMPDIR": str(tmp_path), "HERMES_UPDATE_SHIM_GRACE_SECONDS": "0"}
    result = subprocess.run(["/bin/bash", str(POSIX), "--daemonized", "--no-ui",
                             "--install-root", str(install)], env=env, cwd=tmp_path,
                            text=True, capture_output=True, timeout=60)
    assert witness.exists(), result.stdout + result.stderr
    observed = json.loads(witness.read_text())
    assert observed["ok"], observed
    assert result.returncode == 0, result.stdout + result.stderr
    delegate = next(line for line in observed["before"].splitlines() if line.startswith("delegate:"))
    assert delegate.startswith(f'delegate:{observed["pid"]} ct:')
    assert float(delegate.split("ct:")[1]) == pytest.approx(observed["ct"], rel=0, abs=0.001)
    assert "delegate:" not in observed["after"], "the update retires only its own identity"
    assert observed["before"].splitlines()[0] == observed["after"].splitlines()[0]
    assert not (home / ".hermes-update-in-progress").exists(), "the shell releases custody last"

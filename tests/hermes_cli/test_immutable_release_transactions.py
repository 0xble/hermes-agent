"""Kill a real child after each transaction mutation; retry through public entry points.

All homes and labels are disposable. Nothing in this module names the installed
Hermes service, and the launchd test bootstraps only its own UUID label.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import stat
import subprocess
import sys
import time
import uuid
import venv
from pathlib import Path

import pytest

from hermes_cli import immutable_releases as releases


# The tags are durable mutation destinations, not line numbers. Occurrence 2 of
# release-txn.json is the post-reload acknowledgement (or migration intent).
# Crash strictly *after* the actual syscall; no finally/exception repair runs.
_STEPS = {
    "promote": ("backup", "txn", "previous", "current", "plist", "loaded", "ack", "last", "delete-txn", "delete-backup"),
    "rollback": ("backup", "txn", "previous", "current", "plist", "loaded", "ack", "last", "delete-txn", "delete-backup"),
    "migration": ("backup", "txn", "journal", "previous", "current", "plist", "loaded", "ack", "last", "delete-txn", "delete-backup"),
    "migration_rollback": ("backup", "txn", "current", "previous", "journal", "plist", "loaded", "ack", "last", "delete-txn", "delete-backup"),
}

_CHILD = r'''
import os, pathlib, sys
from hermes_cli import immutable_releases as r
home, source, plist, scenario, checkpoint, original, intended, real = sys.argv[1:9]
home, source, plist = map(pathlib.Path, (home, source, plist))
original, intended = bytes.fromhex(original), bytes.fromhex(intended)
replace, unlink = os.replace, pathlib.Path.unlink
seen = {}
def hit(kind, target):
    target = pathlib.Path(target)
    if target.parent == home and target.name.startswith('release-plist-') and target.suffix == '.backup':
        tag = 'backup' if kind == 'replace' else 'delete-backup'
    elif target == home / 'release-txn.json':
        if kind == 'unlink': tag = 'delete-txn'
        else:
            seen['txn'] = seen.get('txn', 0) + 1
            tag = 'txn' if seen['txn'] == 1 else 'ack'
    elif target == home / 'release-last-txn.json' and kind == 'replace': tag = 'last'
    elif kind == 'replace' and target == home / 'release-layout.json': tag = 'journal'
    elif target == home / 'current': tag = 'current'
    elif target == home / 'previous': tag = 'previous'
    elif kind == 'replace' and target == plist: tag = 'plist'
    else: return
    if tag == checkpoint: os._exit(73)
def watched_replace(src, dst, *a, **kw):
    result = replace(src, dst, *a, **kw)
    hit('replace', dst)
    return result
def watched_unlink(self, *a, **kw):
    result = unlink(self, *a, **kw)
    hit('unlink', self)
    return result
os.replace = watched_replace
pathlib.Path.unlink = watched_unlink
r._source_python_valid = lambda *args: True  # fixture has no installed Hermes package
if real != 'real':
    original_ack = r.acknowledge_running_release
    def observed_ack(path):
        record = r._read_txn(r.ReleasePaths.for_home(path))
        if not record or (path / 'loaded').read_bytes() != pathlib.Path(plist).read_bytes():
            return False
        r._verify_transaction(r.ReleasePaths.for_home(path), record)
        record['reload_ack'] = {'plist_sha256': record['plist']['intended_sha256'],
                                'launchd_pid': os.getpid(), 'gateway_pid': os.getpid(),
                                'release_root': record.get('candidate') or record.get('source'),
                                'code_sha': record.get('source_sha') or pathlib.Path(record['candidate']).name}
        record['reload_done'] = True
        r._write_txn(r.ReleasePaths.for_home(path), record)
        r._finish_txn(r.ReleasePaths.for_home(path), record)
        return True
    r.acknowledge_running_release = observed_ack

def reload():
    if real == 'real':
        from hermes_cli import gateway, gateway_launchd
        gateway.get_launchd_label = lambda: plist.stem
        gateway._launchd_domain = lambda: f'gui/{os.getuid()}'
        result = gateway_launchd._reload_installed_launchd_plist(plist)
    else:
        result = True
    # Marker models the service's loaded definition; real mode also confirms
    # launchctl registration in the parent after the child is gone.
    (home / 'loaded').write_bytes(plist.read_bytes())
    if checkpoint == 'loaded': os._exit(73)
    return result

if scenario == 'promote':
    r.activate_release(home, home / 'releases/B', plist_path=plist,
                       plist_body=intended, reload_callback=reload)
elif scenario == 'rollback':
    r.rollback(home, plist_path=plist, plist_body=original, reload_callback=reload)
elif scenario == 'migration':
    r.activate_release(home, home / 'releases/B', source=source, plist_path=plist,
                       plist_body=intended, reload_callback=reload)
elif scenario == 'migration_rollback':
    r.rollback(home, plist_path=plist, reload_callback=reload)
else: raise AssertionError(scenario)
sys.exit(0)
'''


def _release(path: Path, *, real: bool = False) -> None:
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text("[project]\nname='probe'\n")
    (path / "uv.lock").write_text("")
    python = path / ".venv/bin/python"
    if real:
        venv.EnvBuilder(with_pip=False).create(path / ".venv")
    else:
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(python.stat().st_mode | stat.S_IEXEC)
    (path / ".release-ready").write_text(path.name + "\n")
    (path / ".hermes_build_sha").write_text(path.name + "\n")


def _source(source: Path) -> str:
    source.mkdir()
    (source / "version.txt").write_text("source\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "version.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Probe",
                    "-c", "user.email=probe@example.test", "-c", "commit.gpgsign=false",
                    "commit", "-qm", "fixture"], check=True)
    return releases.release_sha(source)


def _definition(plist: Path, home: Path, root: Path, *, real: bool) -> bytes:
    python = root / ".venv/bin/python" if real else Path(sys.executable)
    data = {"Label": plist.stem, "WorkingDirectory": str(root),
            "ProgramArguments": [str(python), "-c",
                                 "import os,pathlib,time;pathlib.Path(os.environ['S2_OUTPUT']).write_text(os.getcwd());time.sleep(90)",
                                 "hermes_cli.main", "gateway", "run"],
            "EnvironmentVariables": {"HERMES_HOME": str(home), "S2_OUTPUT": str(home / "observed")},
            "RunAtLoad": True, "KeepAlive": False}
    return plistlib.dumps(data)


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _state(home: Path, plist: Path) -> tuple[Path | None, Path | None, str | None, str, str | None]:
    journal = home / "release-layout.json"
    loaded = home / "loaded"
    return (releases.read_pointer(home / "current"), releases.read_pointer(home / "previous"),
            json.loads(journal.read_text())["state"] if journal.exists() else None,
            _sha(plist.read_bytes()), _sha(loaded.read_bytes()) if loaded.exists() else None)


def _crash(home: Path, source: Path, plist: Path, scenario: str, checkpoint: str,
           original: bytes, intended: bytes, *, real: bool = False) -> None:
    env = dict(os.environ, HERMES_HOME=str(home), S2_CRASH_CHILD=_CHILD)
    result = subprocess.run([sys.executable, "-c", "import os;exec(os.environ['S2_CRASH_CHILD'])",
                             str(home), str(source), str(plist), scenario, checkpoint,
                             original.hex(), intended.hex(), "real" if real else "simulated"],
                            env=env, cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=100 if real else 20)
    assert result.returncode == 73, (scenario, checkpoint, result.stdout, result.stderr)


def _fixture(tmp_path: Path, scenario: str, *, real: bool = False):
    home, source = tmp_path / "profile", tmp_path / "source"
    a, b = (home / "releases" / name for name in ("A", "B"))
    _release(a, real=real)
    _release(b, real=real)
    source_sha = _source(source)
    if real:
        venv.EnvBuilder(with_pip=False).create(source / ".venv")
    label = f"ai.hermes.s2crash.{uuid.uuid4().hex}" if real else "ai.hermes.disposable"
    plist = tmp_path / f"{label}.plist"
    first = scenario.startswith("migration")
    original = _definition(plist, home, source if first else a, real=real)
    intended = _definition(plist, home, b, real=real)
    plist.write_bytes(original)
    (home / "loaded").write_bytes(original)
    if not first:
        releases.promote(home, a)
        if scenario == "rollback":
            releases.promote(home, b)
            plist.write_bytes(intended)
            (home / "loaded").write_bytes(intended)
    if scenario == "migration_rollback":
        from unittest.mock import patch
        with patch.object(releases, "_source_python_valid", return_value=True):
            releases.activate_release(home, b, source=source, plist_path=plist,
                                      plist_body=intended)
        (home / "loaded").write_bytes(intended)
    return home, source, plist, a, b, source_sha, original, intended


def _retry(home, source, plist, scenario, a, b, source_sha, original, intended, monkeypatch,
           *, real=False, checkpoint=None):
    from hermes_cli import gateway, gateway_launchd, update_cmd
    real_reload = gateway_launchd._reload_installed_launchd_plist
    def reload():
        if real:
            assert real_reload(plist)
        (home / "loaded").write_bytes(plist.read_bytes())
        return True
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(releases, "_source_python_valid", lambda *args: True)
    if not real:
        def observed_ack(path):
            paths = releases.ReleasePaths.for_home(path)
            record = releases._read_txn(paths)
            if not record or (home / "loaded").read_bytes() != plist.read_bytes():
                return False
            releases._verify_transaction(paths, record)
            record["reload_ack"] = {"plist_sha256": record["plist"]["intended_sha256"],
                                    "launchd_pid": os.getpid(), "gateway_pid": os.getpid(),
                                    "release_root": record.get("candidate") or record.get("source"),
                                    "code_sha": record.get("source_sha") or Path(record["candidate"]).name}
            record["reload_done"] = True
            releases._write_txn(paths, record)
            releases._finish_txn(paths, record)
            return True
        monkeypatch.setattr(releases, "acknowledge_running_release", observed_ack)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: plist.stem)
    monkeypatch.setattr(gateway, "_launchd_domain", lambda: f"gui/{os.getuid()}")
    monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist", lambda path: reload())
    if scenario in ("promote", "migration"):
        monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
        monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"immutable_releases": True})
        monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", source)
        monkeypatch.setattr(releases, "stage_release", lambda *args, **kw: (b, "existing"))
        monkeypatch.setattr(gateway, "generate_launchd_plist", lambda **kw: intended.decode())
        assert update_cmd._activate_immutable_release(sha="B", source=source)
    else:
        releases.rollback(home, plist_path=plist, plist_body=original, reload_callback=reload)
    expected = ((b, source, "done") if scenario == "migration" else
                (None, None, "rolled-back") if scenario == "migration_rollback" else
                (b, a, None) if scenario == "promote" else (a, b, None))
    assert _state(home, plist) == (*expected, _sha(original if scenario.endswith("rollback") else intended),
                                  _sha(original if scenario.endswith("rollback") else intended))
    assert not (home / "release-txn.json").exists()
    # A crash immediately after backup publication but before the intent is
    # durable cannot identify that backup for cleanup; it is unreferenced only.
    backups = list(home.glob("release-plist-*.backup"))
    assert not backups or all(_sha(path.read_bytes()) in {_sha(original), _sha(intended)}
                              for path in backups)
    assert not list(home.glob(".*.tmp-*"))
    if scenario.startswith("migration"):
        journal = json.loads((home / "release-layout.json").read_text())
        assert journal["source_sha"] == source_sha
        assert releases.migration_plist(home) == (plist, original)
        assert releases.release_sha(source) == source_sha


@pytest.mark.macos_only
@pytest.mark.parametrize("scenario,checkpoint", [(name, step) for name, steps in _STEPS.items() for step in steps])
def test_crash_after_each_durable_mutation(tmp_path, monkeypatch, scenario, checkpoint):
    home, source, plist, a, b, source_sha, original, intended = _fixture(tmp_path, scenario)
    before = _state(home, plist)
    _crash(home, source, plist, scenario, checkpoint, original, intended)
    txn = home / "release-txn.json"
    if checkpoint in ("backup", "delete-txn", "delete-backup"):
        assert not txn.exists()
    else:
        record = json.loads(txn.read_text())
        operation = {"migration": "first-migration", "migration_rollback": "first-migration-rollback"}.get(scenario, scenario)
        assert record["operation"] == operation
        assert record["plist"]["sha256"] == _sha(intended if scenario == "rollback" else original)
        assert _sha(Path(record["plist"]["backup"]).read_bytes()) == record["plist"]["sha256"]
        assert record["plist"]["intended_sha256"] == _sha(original if scenario.endswith("rollback") else intended)
        assert record.get("reload_done", False) == (checkpoint in ("ack", "last"))
    current, previous, state, plist_hash, loaded_hash = _state(home, plist)
    steps = _STEPS[scenario]
    reached = steps.index(checkpoint)
    after = lambda tag: reached >= steps.index(tag)
    initial_current, initial_previous, initial_state, initial_plist, initial_loaded = before
    desired_current = None if scenario == "migration_rollback" else a if scenario == "rollback" else b
    desired_previous = (None if scenario == "migration_rollback" else b if scenario == "rollback"
                        else source if scenario == "migration" else a)
    assert current == (desired_current if after("current") else initial_current)
    assert previous == (desired_previous if after("previous") else initial_previous)
    desired_state = "done" if scenario == "migration" else "rolled-back"
    assert state == (desired_state if "journal" in steps and after("journal") else initial_state)
    desired_hash = _sha(original if scenario.endswith("rollback") else intended)
    assert plist_hash == (desired_hash if after("plist") else initial_plist)
    assert loaded_hash == (desired_hash if after("loaded") else initial_loaded)
    _retry(home, source, plist, scenario, a, b, source_sha, original, intended, monkeypatch,
           checkpoint=checkpoint)


@pytest.mark.macos_only
@pytest.mark.parametrize("scenario,checkpoint", [("migration", "plist"), ("migration", "loaded"),
                                                 ("migration_rollback", "plist"),
                                                 ("migration_rollback", "loaded")])
def test_disposable_launchd_first_migration_and_rollback(tmp_path, monkeypatch, scenario, checkpoint):
    """Crash across installed-vs-loaded boundary; inspect only a UUID launchctl target."""
    home, source, plist, a, b, source_sha, original, intended = _fixture(tmp_path, scenario, real=True)
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{plist.stem}"
    observed = home / "observed"
    def wait_for(root):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if observed.exists() and observed.read_text() == str(root):
                return
            time.sleep(.1)
        raise AssertionError(f"launchd did not load {root} for {target}")
    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, timeout=15)
        wait_for(source if scenario == "migration" else b)
        _crash(home, source, plist, scenario, checkpoint, original, intended, real=True)
        # At the plist boundary launchd still owns the old definition; after
        # the reload boundary it must run the new target despite the child dying.
        wait_for((source if scenario == "migration" else b) if checkpoint == "plist"
                 else (b if scenario == "migration" else source))
        assert subprocess.run(["launchctl", "print", target], capture_output=True, timeout=15).returncode == 0
        _retry(home, source, plist, scenario, a, b, source_sha, original, intended, monkeypatch, real=True,
               checkpoint=checkpoint)
        wait_for(b if scenario == "migration" else source)
        assert subprocess.run(["launchctl", "print", target], capture_output=True, timeout=15).returncode == 0
    finally:
        subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=15)
        assert subprocess.run(["launchctl", "print", target], capture_output=True, timeout=15).returncode != 0


@pytest.mark.macos_only
@pytest.mark.parametrize("failure", ["submit", "bootstrap"])
def test_failed_launchd_reload_remains_unacknowledged_until_intended_process_starts(
    tmp_path, monkeypatch, failure
):
    """A failed helper/bootout-bootstrap cannot commit a release transaction."""
    from hermes_cli import gateway, gateway_launchd

    home, source, plist, a, b, _, original, intended = _fixture(tmp_path, "promote", real=True)
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{plist.stem}"
    observed = home / "observed"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: plist.stem)
    monkeypatch.setattr(gateway, "_launchd_domain", lambda: domain)

    def wait_for(root):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if observed.exists() and observed.read_text() == str(root):
                return
            time.sleep(.05)
        raise AssertionError(f"no intended process at {root} for {target}")

    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, timeout=15)
        wait_for(a)
        if failure == "submit":
            # Failed submission leaves the original registered service untouched.
            def failed_reload():
                assert subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0
                return False
        else:
            def failed_reload():
                subprocess.run(["launchctl", "bootout", target], check=True, timeout=15)
                missing = plist.with_name("nonexistent.plist")
                assert subprocess.run(["launchctl", "bootstrap", domain, str(missing)],
                                      capture_output=True, timeout=15).returncode != 0
                return False

        with pytest.raises(RuntimeError, match="reload|refresh"):
            releases.activate_release(home, b, plist_path=plist, plist_body=intended,
                                      reload_callback=failed_reload)
        txn = home / "release-txn.json"
        assert txn.is_file()
        assert not json.loads(txn.read_text()).get("reload_done", False)
        assert releases.read_pointer(home / "current") == b
        assert releases.read_pointer(home / "previous") == a
        assert plist.read_bytes() == intended
        assert not releases.acknowledge_running_release(home)
        assert txn.is_file()
        if failure == "submit":
            subprocess.run(["launchctl", "bootout", target], check=True, timeout=15)
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, timeout=15)
        wait_for(b)
        # The startup acknowledgement is authoritative; the callback return is not.
        assert releases.acknowledge_running_release(home)
        assert not txn.exists()
        assert subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0
    finally:
        subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=15)
        assert subprocess.run(["launchctl", "print", target], capture_output=True).returncode != 0


@pytest.mark.macos_only
@pytest.mark.parametrize("phase", ["before-bootout", "after-bootout", "after-bootstrap"])
def test_deferred_helper_crash_phases_never_acknowledge_transaction(tmp_path, monkeypatch, phase):
    """Kill the real generated bash helper at each handoff phase; WAL remains."""
    from hermes_cli import gateway, gateway_launchd

    home, source, plist, a, b, _, original, intended = _fixture(tmp_path, "promote")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    captured = {}
    real_run = subprocess.run

    def intercept(args, **kwargs):
        if args[:2] == ["launchctl", "submit"]:
            captured["script"] = args[-1]
            return subprocess.CompletedProcess(args, 0, b"", b"")
        return real_run(args, **kwargs)

    monkeypatch.setattr(gateway_launchd.subprocess, "run", intercept)
    result = releases.activate_release(
        home, b, plist_path=plist, plist_body=intended,
        reload_callback=lambda: "deferred" if gateway_launchd._spawn_deferred_launchd_reload(
            domain=f"gui/{os.getuid()}", label=plist.stem,
            target=f"gui/{os.getuid()}/{plist.stem}", plist_path=plist,
            gateway_pid=99999999) else False,
    )
    assert result["reload_pending"]
    txn = home / "release-txn.json"
    before = txn.read_bytes()
    shim = tmp_path / "shim"
    shim.mkdir()
    launchctl = shim / "launchctl"
    launchctl.write_text(
        "#!/bin/bash\n"
        "case $1 in\n"
        "bootout) test \"$CRASH_PHASE\" = after-bootout && kill -KILL $PPID; exit 0;;\n"
        "bootstrap) test \"$CRASH_PHASE\" = after-bootstrap && kill -KILL $PPID; exit 0;;\n"
        "list) printf '\"PID\" = 12345;\\n'; exit 0;;\n"
        "remove) exit 0;;\n"
        "esac\nexit 1\n", encoding="utf-8")
    launchctl.chmod(0o755)
    sleep = shim / "sleep"
    sleep.write_text("#!/bin/bash\ntest \"$CRASH_PHASE\" = before-bootout && kill -KILL $PPID\nexit 0\n")
    sleep.chmod(0o755)
    # A fake launchctl in the child PATH prevents any interaction with the host's jobs.
    child = real_run(["/bin/bash", "-c", captured["script"]], timeout=15,
                     env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "CRASH_PHASE": phase})
    assert child.returncode != 0
    assert txn.read_bytes() == before
    assert not json.loads(txn.read_text()).get("reload_done", False)
    assert releases.read_pointer(home / "current") == b
    assert plist.read_bytes() == intended

"""Kill a real child after each transaction mutation; retry through public entry points.

All homes and labels are disposable. Nothing in this module names the installed
Hermes service, and the launchd test bootstraps only its own UUID label.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import stat
import subprocess
import sys
import time
import uuid
import venv
from pathlib import Path

import pytest
from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli import immutable_releases as releases
from tests.hermes_cli.immutable_launchd_cleanup import register_disposable_label, sweep_prior_sessions, install_probe_process_dependency


@pytest.fixture(scope="module", autouse=True)
def _sweep_disposable_jobs(request):
    # The cleanup command is launchd-only; unmarked transaction invariants still run on Linux.
    if sys.platform == "darwin":
        sweep_prior_sessions(request)


# The tags are durable mutation destinations, not line numbers. Occurrence 2 of
# release-txn.json is the post-reload acknowledgement (or migration intent).
# Crash strictly *after* the actual syscall; no finally/exception repair runs.
_STEPS = {
    "promote": ("backup", "txn", "previous", "current", "plist", "issued", "loaded", "ack", "last", "delete-txn", "delete-backup"),
    "rollback": ("backup", "txn", "previous", "current", "plist", "issued", "loaded", "ack", "last", "delete-txn", "delete-backup"),
    "migration": ("backup", "txn", "journal", "previous", "current", "plist", "issued", "loaded", "ack", "last", "delete-txn", "delete-backup"),
    "migration_rollback": ("backup", "txn", "current", "previous", "journal", "plist", "issued", "loaded", "ack", "last", "delete-txn", "delete-backup"),
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
            tag = 'txn' if seen['txn'] == 1 else 'issued' if seen['txn'] == 2 else 'ack'
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
        install_probe_process_dependency(path / ".venv")
        package = path / "hermes_cli"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "main.py").write_text(
            "import os,pathlib,time\n"
            "pathlib.Path(os.environ['S2_OUTPUT']).write_text(os.getcwd())\n"
            "time.sleep(90)\n")
    else:
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(python.stat().st_mode | stat.S_IEXEC)
    (path / ".release-ready").write_text(path.name + "\n")
    (path / ".hermes_build_sha").write_text(path.name + "\n")


def _source(source: Path, *, real: bool = False) -> str:
    source.mkdir()
    (source / "version.txt").write_text("source\n")
    if real:
        package = source / "hermes_cli"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "main.py").write_text(
            "import os,pathlib,time\n"
            "pathlib.Path(os.environ['S2_OUTPUT']).write_text(os.getcwd())\n"
            "time.sleep(90)\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "version.txt", *(["hermes_cli"] if real else [])], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Probe",
                    "-c", "user.email=probe@example.test", "-c", "commit.gpgsign=false",
                    "commit", "-qm", "fixture"], check=True)
    return releases.release_sha(source)


def _definition(plist: Path, home: Path, root: Path, *, real: bool) -> bytes:
    python = root / ".venv/bin/python" if real else Path(sys.executable)
    data = {"Label": plist.stem, "WorkingDirectory": str(root),
            "ProgramArguments": ([str(python), "-m", "hermes_cli.main", "gateway", "run"] if real else
                                 [str(python), "-c",
                                  "import os,pathlib,time;pathlib.Path(os.environ['S2_OUTPUT']).write_text(os.getcwd());time.sleep(90)",
                                  "hermes_cli.main", "gateway", "run"]),
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
    source_sha = _source(source, real=real)
    if real:
        venv.EnvBuilder(with_pip=False).create(source / ".venv")
        install_probe_process_dependency(source / ".venv")
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
    callbacks = []
    def reload():
        callbacks.append(1)
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
    if checkpoint == "issued":
        record = releases._read_txn(releases.ReleasePaths.for_home(home))
        assert record is not None and record["reload_issued"]["attempt"] == 1
        pending = releases.recover_pending_transaction(home, reload_callback=reload)
        assert pending is not None and pending["reload_pending"] and callbacks == []
        # Explicit operator repair, not recovery, makes the target observable.
        (home / "loaded").write_bytes(plist.read_bytes())
    if scenario in ("promote", "migration"):
        monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
        monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"immutable_releases": True})
        monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", source)
        monkeypatch.setattr(releases, "stage_release", lambda *args, **kw: (b, "existing"))
        monkeypatch.setattr(gateway, "generate_launchd_plist", lambda **kw: intended.decode())
        assert update_cmd._activate_immutable_release(sha="B", source=source)
    else:
        releases.rollback(home, plist_path=plist, plist_body=original, reload_callback=reload)
    assert callbacks == ([] if checkpoint in {"issued", "loaded", "ack", "last", "delete-txn", "delete-backup"}
                         else [1])
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


@pytest.mark.platforms("macos")
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


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("scenario,checkpoint", [("migration", "plist"), ("migration", "loaded"),
                                                 ("migration_rollback", "plist"),
                                                 ("migration_rollback", "loaded")])
def test_disposable_launchd_first_migration_and_rollback(tmp_path, monkeypatch, request, scenario, checkpoint):
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
    register_disposable_label(request, plist.stem, plist)
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


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("failure", ["submit", "bootstrap"])
def test_failed_launchd_reload_remains_unacknowledged_until_intended_process_starts(
    tmp_path, monkeypatch, request, failure
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

    register_disposable_label(request, plist.stem, plist)
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


@pytest.mark.platforms("macos")
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


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("reason", ["failed-submit", "unobserved"])
def test_reload_submission_is_not_release_completion(tmp_path, monkeypatch, reason):
    from hermes_cli import gateway_launchd

    home, _, plist, a, b, _, _, intended = _fixture(tmp_path, "promote")
    calls = []
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_log_path", lambda: tmp_path / "reload.log")
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_budget", lambda: 1)
    monkeypatch.setattr(gateway_launchd, "_gw", lambda: SimpleNamespace(
        _append_launchd_reload_log=lambda *_: None,
        logger=SimpleNamespace(warning=lambda *_: None)))
    def submit(args, **kwargs):
        calls.append(args)
        if reason == "failed-submit":
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(gateway_launchd.subprocess, "run", submit)
    monkeypatch.setattr(gateway_launchd, "_launchctl_supervised_pid", lambda _: None)
    def reload():
        submitted = gateway_launchd._spawn_deferred_launchd_reload(
            domain=f"gui/{os.getuid()}", label=plist.stem,
            target=f"gui/{os.getuid()}/{plist.stem}", plist_path=plist,
            gateway_pid=os.getpid())
        return "deferred" if submitted else False
    if reason == "failed-submit":
        with pytest.raises(RuntimeError, match="refresh"):
            releases.activate_release(home, b, plist_path=plist, plist_body=intended,
                                      reload_callback=reload)
    else:
        result = releases.activate_release(home, b, plist_path=plist, plist_body=intended,
                                           reload_callback=reload)
        assert result["reload_pending"]
    txn = home / "release-txn.json"
    assert len(calls) == 1 and calls[0][:2] == ["launchctl", "submit"]
    assert txn.exists() and not json.loads(txn.read_text()).get("reload_done")
    assert releases.read_pointer(home / "current") == b
    assert releases.read_pointer(home / "previous") == a
    monkeypatch.setattr(releases, "acknowledge_running_release", lambda *_: False)
    if reason == "unobserved":
        repeated = []
        again = releases.recover_pending_transaction(home, reload_callback=lambda: repeated.append(1) or "deferred")
        assert again is not None and again["reload_pending"] and txn.exists() and repeated == []
    def observed_ack(path):
        paths = releases.ReleasePaths.for_home(path)
        record = releases._read_txn(paths)
        releases._verify_transaction(paths, record)
        record["reload_ack"] = {"gateway_pid": os.getpid(), "release_root": str(b)}
        record["reload_done"] = True
        releases._write_txn(paths, record)
        releases._finish_txn(paths, record)
        return True
    monkeypatch.setattr(releases, "acknowledge_running_release", observed_ack)
    assert releases.recover_pending_transaction(home, reload_callback=lambda: "deferred")["current"] == str(b)
    assert not txn.exists()


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("scenario", ["promote", "rollback", "migration_rollback"])
def test_issued_reload_is_observation_only_across_entry_points(tmp_path, monkeypatch, scenario):
    from hermes_cli import gateway, gateway_launchd, update_cmd
    home, source, plist, a, b, _, original, intended = _fixture(tmp_path, scenario)
    calls = []
    callback = lambda: calls.append(1) or "deferred"
    monkeypatch.setattr(releases, "acknowledge_running_release", lambda *_: False)
    if scenario == "promote":
        assert releases.activate_release(home, b, plist_path=plist, plist_body=intended,
                                         reload_callback=callback)["reload_pending"]
    else:
        assert releases.rollback(home, plist_path=plist,
                                 plist_body=original if scenario == "rollback" else None,
                                 reload_callback=callback)["reload_pending"]
    assert calls == [1]
    record = releases._read_txn(releases.ReleasePaths.for_home(home))
    assert record is not None and record["reload_issued"]["plist_sha256"] == record["plist"]["intended_sha256"]
    assert record["reload_issued"]["attempt"] == 1
    pending = releases.recover_pending_transaction(home, reload_callback=callback)
    assert pending is not None and pending["reload_pending"]
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_release_acknowledgement_timeout", lambda: 0)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist", lambda _: callback())
    with pytest.raises(RuntimeError, match="observation-only"):
        update_cmd._finish_pending_release_transaction(home)
    # Rollback CLI retry replays its existing WAL rather than creating an
    # inverse operation or issuing a second callback.
    if scenario != "promote":
        assert releases.rollback(home, plist_path=plist, reload_callback=callback)["reload_pending"]
    assert calls == [1]


@pytest.mark.platforms("macos")
def test_catch_up_pending_reload_never_invokes_callback(tmp_path, monkeypatch):
    from hermes_cli import gateway, gateway_launchd, update_cmd
    home, source, plist, a, b, _, original, intended = _fixture(tmp_path, "promote")
    calls = []
    monkeypatch.setattr(releases, "acknowledge_running_release", lambda *_: False)
    assert releases.activate_release(home, b, plist_path=plist, plist_body=intended,
                                     reload_callback=lambda: calls.append(1) or "deferred")["reload_pending"]
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {
        "immutable_releases": True, "release_acknowledgement_timeout_seconds": 0})
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", source)
    monkeypatch.setattr(releases, "release_sha", lambda _: "B")
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway, "launchd_plist_is_current", lambda **kw: True)
    monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist", lambda _: calls.append(1) or "deferred")
    with pytest.raises(RuntimeError, match="observation-only"):
        update_cmd._catch_up_immutable_release(defer=False, sha="B", source=source)
    assert calls == [1]


@pytest.mark.platforms("macos")
@pytest.mark.spawns_gateway_lookalike
def test_updater_waits_for_deferred_gateway_ack_without_second_reload(tmp_path, monkeypatch):
    import threading
    from hermes_cli import gateway_launchd, update_cmd

    from hermes_cli import gateway as gateway_cli, update_receipt
    home, source, plist, _, candidate, _, _, intended = _fixture(tmp_path, "promote", real=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"immutable_releases": True})
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", source)
    monkeypatch.setattr(releases, "release_sha", lambda _: "B")
    monkeypatch.setattr(releases, "stage_release", lambda *args, **kwargs: (candidate, "existing"))
    monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway_cli, "generate_launchd_plist", lambda **kwargs: intended.decode())
    calls = []
    monkeypatch.setattr(gateway_launchd, "_reload_installed_launchd_plist",
                        lambda path: calls.append(path) or "deferred")
    processes = []
    def launch_gateway():
        env = dict(os.environ, S2_OUTPUT=str(home / "observed"))
        env.pop("PYTHONPATH", None)
        processes.append(subprocess.Popen(
            [str(candidate / ".venv/bin/python"), "-m", "hermes_cli.main", "gateway", "run"],
            cwd=candidate, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    monkeypatch.setattr(gateway_launchd, "_launchctl_supervised_pid",
                        lambda _: processes[0].pid if processes else None)
    wait = update_cmd._await_release_acknowledgement
    monkeypatch.setattr(update_cmd, "_await_release_acknowledgement",
                        lambda path: wait(path, timeout_seconds=4))
    timer = threading.Timer(0.1, launch_gateway)
    timer.start()
    try:
        update_receipt.begin_update_receipt()
        assert update_cmd._activate_immutable_release(sha="B", source=source)
        receipt = update_receipt.finalize_update_receipt("success")
        assert json.loads(receipt.read_text())["outcome"] == "success"
        assert not (home / "release-txn.json").exists()
        assert calls == [plist]
    finally:
        timer.join()
        for process in processes:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.platforms("macos")
def test_updater_ack_timeout_preserves_pending_without_new_reload(tmp_path, monkeypatch):
    from hermes_cli import update_cmd
    home, _, plist, _, candidate, _, _, intended = _fixture(tmp_path, "promote")
    calls = []
    pending = releases.activate_release(home, candidate, plist_path=plist, plist_body=intended,
                                        reload_callback=lambda: calls.append(1) or "deferred")
    assert pending["reload_pending"]
    assert not update_cmd._await_release_acknowledgement(home, timeout_seconds=0)
    assert (home / "release-txn.json").exists()
    assert calls == [1]


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("wrong", ["release", "pid", "wrapper"])
def test_wrong_gateway_identity_cannot_acknowledge(tmp_path, monkeypatch, wrong):
    import psutil
    from hermes_cli import gateway_launchd

    home, _, plist, _, b, _, _, intended = _fixture(tmp_path, "promote")
    assert releases.activate_release(home, b, plist_path=plist, plist_body=intended,
                                     reload_callback=lambda: "deferred")["reload_pending"]
    monkeypatch.setattr(gateway_launchd, "_launchctl_supervised_pid", lambda _: 31415)
    gateway = SimpleNamespace(
        pid=31416 if wrong == "pid" else 31415,
        cmdline=lambda: (["python", "-m", "hermes_cli.stderr_timestamp", "--", "python",
                          "-m", "hermes_cli.main", "gateway", "run"] if wrong == "wrapper" else
                         ["python", "-m", "hermes_cli.main", "gateway", "run"]),
        exe=lambda: str((home / "releases/A" if wrong == "release" else b) / ".venv/bin/python"),
        cwd=lambda: str(home / "releases/A" if wrong == "release" else b),
        environ=lambda: {}, children=lambda **_: [])
    monkeypatch.setattr(psutil, "Process", lambda _: gateway)
    assert not releases.acknowledge_running_release(home)
    record = json.loads((home / "release-txn.json").read_text())
    assert not record.get("reload_done") and "reload_ack" not in record


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("launcher", ["generated", "other-release", "foreign-code"])
def test_generated_launchd_gateway_acknowledges_only_its_release(tmp_path, monkeypatch, launcher):
    """The gateway launchd starts from the generated plist acknowledges its own release only."""
    import psutil
    from hermes_cli import gateway as gateway_cli, gateway_launchd
    from hermes_cli._launchers import runtime_command

    home, _, plist, a, b, _, _, intended = _fixture(tmp_path, "promote")
    assert releases.activate_release(home, b, plist_path=plist, plist_body=intended,
                                     reload_callback=lambda: "deferred")["reload_pending"]
    monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: home)
    target = releases.read_pointer(home / "current")
    definition = plistlib.loads(gateway_cli.generate_launchd_plist(release_target=target).encode())
    shell, _ = json.JSONDecoder().raw_decode(definition["ProgramArguments"][-1].split("$.system(", 1)[1])
    wrapper = shlex.split(shell.removeprefix("exec ").split(" >> ", 1)[0])
    argv = wrapper[wrapper.index("--") + 1:]
    assert argv[-3:] == ["gateway", "run", "--external-supervisor"]
    if launcher == "other-release":
        argv = [argv[0], *runtime_command(a, (), module="hermes_cli.main")[1:4], *argv[4:]]
    elif launcher == "foreign-code":
        argv = [argv[0], "-I", "-c", "import time; time.sleep(60)", *argv[4:]]

    interpreter = tmp_path / "interpreter"
    monkeypatch.setattr(releases, "_interpreter_process_executable", lambda _: interpreter)
    process = lambda pid, cmd: SimpleNamespace(  # noqa: E731
        pid=pid, cmdline=lambda: cmd, exe=lambda: str(interpreter), cwd=lambda: str(target),
        environ=lambda: {}, children=lambda **_: [])
    supervisor = process(31415, definition["ProgramArguments"])
    supervisor.children = lambda **_: [process(31416, wrapper), process(31417, argv)]
    monkeypatch.setattr(gateway_launchd, "_launchctl_supervised_pid", lambda _, **kwargs: supervisor.pid)
    monkeypatch.setattr(psutil, "Process", lambda _: supervisor)

    assert releases.acknowledge_running_release(home) is (launcher == "generated")
    assert (home / "release-txn.json").exists() is (launcher != "generated")


@pytest.mark.platforms("macos")
def test_no_gateway_restart_pending_txn_has_no_launchctl_or_pointer_writes(tmp_path, monkeypatch):
    from hermes_cli import update_cmd

    home, _, plist, _, b, _, _, intended = _fixture(tmp_path, "promote")
    assert releases.activate_release(home, b, plist_path=plist, plist_body=intended,
                                     reload_callback=lambda: "deferred")["reload_pending"]
    txn = home / "release-txn.json"
    before = (txn.read_bytes(), plist.read_bytes(),
              releases.read_pointer(home / "current"), releases.read_pointer(home / "previous"))
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_resolve_update_options", lambda *_: SimpleNamespace(no_gateway_restart=True))
    real_run = subprocess.run
    launchctl_calls = []
    def refuse_launchctl(args, *positional, **keywords):
        if isinstance(args, (list, tuple)) and args and args[0] == "launchctl":
            launchctl_calls.append(args)
            raise AssertionError("launchctl called by restart-prohibited update")
        return real_run(args, *positional, **keywords)
    with (patch.object(update_cmd, "_finish_pending_release_transaction", side_effect=AssertionError("replayed")),
          patch.object(update_cmd, "_require_immutable_launchd", side_effect=AssertionError("service touched")),
          patch.object(update_cmd, "_finalize_receipt"),
          patch.object(subprocess, "run", side_effect=refuse_launchctl),
          patch.object(releases, "acknowledge_running_release", side_effect=AssertionError("launchctl queried")),
          pytest.raises(SystemExit) as exit_info):
        update_cmd._cmd_update_impl(SimpleNamespace(rollback=False, no_gateway_restart=True), False)
    assert exit_info.value.code == 1
    assert not launchctl_calls
    assert before == (txn.read_bytes(), plist.read_bytes(),
                      releases.read_pointer(home / "current"),
                      releases.read_pointer(home / "previous"))


@pytest.mark.platforms("macos")
def test_release_manager_wait_observes_delayed_ack_without_reloading(tmp_path, monkeypatch):
    pending = tmp_path / "release-txn.json"
    pending.write_text("{}", encoding="utf-8")
    observations = iter([False, True])
    seen = []

    def acknowledge(home):
        seen.append(home)
        acknowledged = next(observations)
        if acknowledged:
            pending.unlink()
        return acknowledged

    monkeypatch.setattr(releases, "acknowledge_running_release", acknowledge)
    monkeypatch.setattr(releases.time, "sleep", lambda _: None)
    assert releases.wait_for_release_acknowledgement(tmp_path, timeout_seconds=1)
    assert seen == [tmp_path, tmp_path]


def test_immutable_update_ack_call_site_registry_is_complete():
    from hermes_cli import update_cmd

    assert update_cmd._IMMUTABLE_RELEASE_ACK_CALL_SITES == {
        "_activate_immutable_release",
        "_finish_pending_release_transaction",
        "_catch_up_immutable_release",
        "_cmd_update_impl.rollback",
        "_cmd_update_impl.repair-service",
    }

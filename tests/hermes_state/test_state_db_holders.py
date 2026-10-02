"""Behavioral tests for the state-holder and repair-admission authority."""

import os
import select
import subprocess
import sys
from types import SimpleNamespace

import pytest

import hermes_state_holders


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("generation", ["current", "hardlink", "retired"])
def test_darwin_holder_scan_uses_native_descriptor_identity(tmp_path, monkeypatch, generation):
    """A foreign descriptor remains authoritative without stat-ing unrelated files."""
    db = tmp_path / "state.db"
    db.write_bytes(b"held generation")
    opened = db
    if generation == "hardlink":
        opened = tmp_path / "alias.db"
        os.link(db, opened)
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; held=open(sys.argv[1], 'rb'); print('ready', flush=True); sys.stdin.read()",
         str(opened)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert select.select([child.stdout], [], [], 10)[0], "holder did not open its descriptor"
        assert child.stdout.readline().strip() == "ready"
        if generation == "retired":
            db.unlink()
            db.write_bytes(b"replacement generation")
        monkeypatch.setattr(hermes_state_holders.psutil, "process_iter",
                            lambda *args, **kwargs: pytest.fail("ambient psutil path scan"))
        holders = hermes_state_holders.foreign_state_db_holders(db)
        assert any(pid == child.pid for pid, _ in holders), holders
        assert not any(pid < 0 for pid, _ in holders), holders
    finally:
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("partial", [False, True])
def test_darwin_holder_scan_retains_uncertainty_on_enumeration_failure(tmp_path, monkeypatch, partial):
    import hermes_state_dbfile

    db = tmp_path / "state.db"
    db.touch()
    info = db.stat()
    def interrupted():
        if partial:
            yield 4242, 7, str(db), (info.st_dev, info.st_ino)
        raise RuntimeError("descriptor enumeration interrupted")
    monkeypatch.setattr(hermes_state_dbfile, "_iter_darwin_fd_targets", interrupted)
    holders = hermes_state_holders.foreign_state_db_holders(db)
    assert any(pid < 0 and "descriptor enumeration interrupted" in detail for pid, detail in holders)
    assert ((4242, str(db)) in holders) == partial


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("identity", ["alias", "different-inode", "different-device"])
def test_foreign_holder_uses_device_and_inode_not_path(
    tmp_path, monkeypatch, identity
):
    """Descriptor identity is authoritative even when /proc spells another path."""
    db_path = tmp_path / "state.db"
    db_path.touch()
    alias_path = tmp_path / "namespace-alias" / "state.db" if identity == "alias" else db_path

    proc_root = tmp_path / "proc"
    for pid in (111, 222):
        (proc_root / str(pid) / "fd").mkdir(parents=True)
    os.symlink(db_path, proc_root / "222" / "fd" / "3")

    # Keep the /proc projection local: patching os globally also intercepts
    # pytest's home guard and pathlib while they inspect these same symlinks.
    projected_os = SimpleNamespace(**vars(os))
    projected_os.getpid = lambda: 111
    monkeypatch.setattr(hermes_state_holders, "os", projected_os)
    real_listdir = os.listdir

    def _listdir(path):
        if isinstance(path, str):
            path = path.replace("/proc", str(proc_root))
        return real_listdir(path)

    monkeypatch.setattr(hermes_state_holders.os, "listdir", _listdir)

    def _readlink(path):
        if path == "/proc/222/fd/3":
            return str(alias_path)
        return os.readlink(path.replace("/proc", str(proc_root)))

    monkeypatch.setattr(hermes_state_holders.os, "readlink", _readlink)
    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        mapped = str(path).replace("/proc", str(proc_root))
        result = real_stat(mapped, *args, **kwargs)
        if str(path) == "/proc/222/fd/3" and identity != "alias":
            values = list(result)
            values[1 if identity == "different-inode" else 2] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(hermes_state_holders.os, "stat", _stat)

    assert hermes_state_holders.foreign_state_db_holders(db_path) == (
        [(222, str(alias_path))] if identity == "alias" else []
    )


def test_windows_restart_manager_scan_sizes_then_excludes_self(monkeypatch, tmp_path):
    """The rstrtmgr lane sizes on ERROR_MORE_DATA, drops our own pid, and fails closed."""
    import ctypes

    db_path = tmp_path / "state.db"
    db_path.touch()
    (tmp_path / "state.db-wal").touch()
    calls = []

    class _Fn:
        def __init__(self, impl):
            self.impl = impl

        def __call__(self, *args):
            return self.impl(*args)

    def start(session, _flags, _key):
        session._obj.value = 7
        return 0

    def register(_session, count, names, *_rest):
        calls.append(("register", count, sorted(names)))
        return 0

    def get_list(_session, needed, count, apps, _reasons):
        needed._obj.value = 2
        if apps is None:
            return 234  # ERROR_MORE_DATA: sizing call
        apps[0].process.pid = os.getpid()
        apps[1].process.pid = 4242
        count._obj.value = 2
        return 0

    class _Api:
        RmStartSession = _Fn(start)
        RmRegisterResources = _Fn(register)
        RmGetList = _Fn(get_list)
        RmEndSession = _Fn(lambda _session: calls.append("end") or 0)

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _Api(), raising=False)

    holders = hermes_state_holders._windows_restart_manager_holders(db_path)

    assert [pid for pid, _ in holders] == [4242]
    assert calls[0] == ("register", 2, sorted(str(p) for p in (db_path, tmp_path / "state.db-wal")))
    assert calls[-1] == "end"

    _Api.RmStartSession = _Fn(lambda *_args: 5)
    with pytest.raises(OSError):
        hermes_state_holders._windows_restart_manager_holders(db_path)

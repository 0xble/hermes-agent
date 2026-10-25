"""Keep shared-runner signal injection inside the fixture HOME without bubblewrap."""
from __future__ import annotations

from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Iterator

import pytest

if TYPE_CHECKING:
    from tests.e2e.core.upgrade.handoff._nshost import NamespaceHost

@pytest.fixture
def proc_table(monkeypatch):
    """Controlled /proc boundary: no native process discovery or signalling."""
    from unittest.mock import Mock

    from tests.e2e.core.upgrade.handoff import _ns_agent as agent

    environments = {
        101: b"HOME=/fixture/home\0HERMES_E2E_UNIT=owned\0",
        102: b"HOME=/fixture/home-other\0HERMES_E2E_UNIT=changed-home\0",
        103: b"HERMES_E2E_UNIT=no-home\0",
    }
    listdir = os.listdir
    # /proc directory entries are strings; the kernel stat fields stay controlled.
    monkeypatch.setattr(os, "listdir", lambda path: [str(pid) for pid in environments]
                        if path == "/proc" else listdir(path))
    monkeypatch.setattr(agent, "_stat", lambda pid: ["S", "1"] + ["0"] * 17 + [str(pid)])
    monkeypatch.setattr(agent, "_read", lambda path: environments[int(path.split("/")[2])]
                        if path.endswith("/environ") else b"python\0worker\0")
    monkeypatch.setattr(os, "getpid", lambda: 101)
    kill = Mock()
    monkeypatch.setattr(os, "kill", kill)
    return agent, kill


@pytest.mark.parametrize("mode,home,expected", [
    ("pid", "/fixture/home", {101, 102, 103}),
    ("pid", None, {101, 102, 103}),
    ("home", "/fixture/home", {101}),
    ("home", None, set()),
    (None, "/fixture/home", set()),
    ("invalid", "/fixture/home", set()),
])
def test_agent_enumeration_and_signals_follow_explicit_mode(proc_table, monkeypatch, mode, home, expected):
    agent, kill = proc_table
    for key, value in (("HERMES_E2E_NAMESPACE_MODE", mode), ("HOME", home)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    rows = agent._procs()
    assert {row["pid"] for row in rows} == expected
    if 102 in expected:
        assert next(row for row in rows if row["pid"] == 102)["env"]["HERMES_E2E_UNIT"] == "changed-home"
    assert agent._kill({"pid": 101}) == {"ok": False}  # Never signal the agent itself.
    for pid in (102, 103, 999, -1):
        assert agent._kill({"pid": pid}) == {"ok": pid in expected}
    assert [call.args for call in kill.call_args_list] == [
        (pid, signal.SIGTERM) for pid in (102, 103) if pid in expected
    ]


@pytest.fixture
def controlled_nshost(monkeypatch):
    """Exercise the real host class without importing its POSIX-only dependencies."""
    from tests.e2e.core import dashboard, upgrade

    helpers = ModuleType("tests.e2e.core.upgrade._helpers")
    reaper = ModuleType("tests.e2e.core.dashboard._reaper")
    for package, name, module in ((upgrade, "_helpers", helpers), (dashboard, "_reaper", reaper)):
        monkeypatch.setitem(sys.modules, module.__name__, module)
        # A native test may already have imported the real dependency in this process.
        monkeypatch.setattr(package, name, module, raising=False)
    source = Path(__file__).resolve().parents[1] / "e2e/core/upgrade/handoff/_nshost.py"
    spec = spec_from_file_location("controlled_namespace_host", source)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    # Missing Unix imports/APIs must not be hidden by the test runner's import cache.
    with monkeypatch.context() as import_guard:
        import_guard.setitem(sys.modules, "pwd", None)
        import_guard.delattr(os, "getuid", raising=False)
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("isolated,mode", [(True, "pid"), (False, "home")])
def test_host_passes_actual_isolation_mode_only_to_agent(tmp_path, monkeypatch, isolated, mode, controlled_nshost):
    from unittest.mock import Mock

    _nshost = controlled_nshost
    monkeypatch.setattr(_nshost.H, "BWRAP_OK", isolated, raising=False)
    monkeypatch.setattr(_nshost.H, "sandbox_argv", lambda argv, **kwargs: argv, raising=False)
    monkeypatch.setattr(_nshost._reaper, "adopt_orphans", Mock(), raising=False)
    monkeypatch.setattr(_nshost.threading, "Thread", Mock())
    monkeypatch.setattr(_nshost.NamespaceHost, "call", Mock(return_value={"pid": 101}))
    popen = Mock()
    monkeypatch.setattr(_nshost.subprocess, "Popen", popen)
    env = {"HOME": str(tmp_path), "HERMES_E2E_NAMESPACE_MODE": "inherited-wrong-mode"}
    _nshost.NamespaceHost(tmp_path, env)
    assert popen.call_args.kwargs["env"]["HERMES_E2E_NAMESPACE_MODE"] == mode
    assert env["HERMES_E2E_NAMESPACE_MODE"] == "inherited-wrong-mode"


@contextmanager
def _host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[NamespaceHost]:
    from tests.e2e.core.upgrade import _helpers as H
    from tests.e2e.core.upgrade.handoff._nshost import NamespaceHost

    monkeypatch.setattr(H, "BWRAP_OK", False)
    monkeypatch.setenv("CI", "1")
    root = tmp_path / "machine"
    root.mkdir()
    home = root / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "HERMES_HOME": str(home / ".hermes")}
    host = NamespaceHost(root, env)
    try:
        yield host
    finally:
        assert host.close() == []


@pytest.mark.platforms("linux")
def test_fallback_neither_exposes_nor_signals_another_fixture(tmp_path, monkeypatch):
    sibling = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        env={**os.environ, "HOME": str(tmp_path / "machine" / "home-other")},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with _host(tmp_path, monkeypatch) as host:
            owned = host.spawn([sys.executable, "-c", "import time; time.sleep(300)"],
                               log=host.root / "owned.log")
            rows = host.procs()
            assert owned in {row["pid"] for row in rows}
            assert sibling.pid not in {row["pid"] for row in rows}
            assert not host.kill(sibling.pid, signal.SIGKILL)  # windows-footgun: ok - linux-only fallback cell
            assert sibling.poll() is None
            assert host.kill(owned, signal.SIGKILL)  # windows-footgun: ok - linux-only fallback cell
        assert sibling.poll() is None
    finally:
        sibling.terminate()
        sibling.wait(timeout=10)


@pytest.mark.platforms("linux")
def test_fallback_born_after_kill_cannot_signal_foreign_same_uid_process(tmp_path, monkeypatch):
    from tests.e2e.core.upgrade.git._hostile_post import _kill_born_after

    sibling = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        env={**os.environ, "HOME": str(tmp_path / "machine" / "home-other")},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with _host(tmp_path, monkeypatch) as host:
            before = {row["pid"] for row in host.procs()}
            owned = host.spawn([sys.executable, "-c", "import time; time.sleep(300)"],
                               log=host.root / "born-after.log")
            killed = _kill_born_after(host, before)
            assert owned in killed
            assert sibling.poll() is None
        assert sibling.poll() is None
    finally:
        sibling.terminate()
        sibling.wait(timeout=10)


@pytest.mark.platforms("linux")
def test_fallback_close_reaps_a_detached_completion_child(tmp_path, monkeypatch):
    from tests.e2e.core.dashboard import _reaper

    with _host(tmp_path, monkeypatch) as host:
        result = host.run([
            sys.executable, "-c",
            "import subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'], "
            "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL); print(child.pid)",
        ])
        assert result.returncode == 0, result.stderr
        detached = int(result.stdout.strip())
        assert host.alive(detached)
    assert _reaper.start_time(detached) is None

"""Service-wrapper gateway descendant discovery regressions (#66900, #105938).

The recursive descendant design and strict command matching are adapted from PR #66913 by
@Tranquil-Flow. This current-main variant covers the all-profiles service discovery contract used
by update/reaper sweeps while preserving default profile scoping.
"""

from types import SimpleNamespace

import psutil
import pytest

import hermes_cli.gateway as gateway


class _FakeProc:
    def __init__(self, pid, cmdline=(), children=()):
        self.pid = pid
        self._cmdline = list(cmdline)
        self._children = list(children)

    def children(self, recursive=False):
        if not recursive:
            return list(self._children)
        found, pending = [], list(self._children)
        while pending:
            child = pending.pop()
            found.append(child)
            pending.extend(child._children)
        return found

    def cmdline(self):
        return list(self._cmdline)


def _gateway_proc(pid=503):
    return _FakeProc(
        pid,
        ["python", "-m", "hermes_cli.main", "--profile", "default", "gateway", "run", "--external-supervisor"],
    )


def test_gateway_descendants_walks_nested_wrappers_and_uses_strict_matcher(monkeypatch):
    gateway_child = _gateway_proc()
    misleading = _FakeProc(504, ["sh", "-c", "echo gateway run"])
    shell = _FakeProc(502, ["sh", "-c", "exec hermes"], [gateway_child, misleading])
    wrapper = _FakeProc(501, ["python", "-m", "hermes_cli.stderr_timestamp"], [shell])
    monkeypatch.setattr(psutil, "Process", lambda pid: wrapper)

    assert gateway._gateway_descendants_of(501) == {503}


def test_gateway_descendants_tolerates_vanished_wrapper(monkeypatch):
    def missing(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", missing)
    assert gateway._gateway_descendants_of(501) == set()


@pytest.mark.linux_only
def test_systemd_service_pid_includes_recursive_gateway_descendant(monkeypatch):
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gateway, "get_service_name", lambda: "hermes-gateway.service")
    monkeypatch.setattr(gateway, "_gateway_descendants_of", lambda pid: {503})

    def run(args, **kwargs):
        if "list-units" in args:
            return SimpleNamespace(returncode=0, stdout="hermes-gateway.service loaded active running\n", stderr="")
        if "show" in args:
            return SimpleNamespace(returncode=0, stdout="501\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gateway.subprocess, "run", run)
    assert gateway._get_service_pids() == {501, 503}


@pytest.mark.macos_only
def test_launchd_default_scope_includes_recursive_gateway_descendant(monkeypatch):
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.hermes.gateway.default")
    monkeypatch.setattr(gateway, "_locate_launchd_gateway_service", lambda label: ("gui/501", 501))
    monkeypatch.setattr(gateway, "_gateway_descendants_of", lambda pid: {503})

    assert gateway._get_service_pids() == {501, 503}


@pytest.mark.macos_only
def test_launchd_fleet_prefix_scan_expands_each_unmapped_wrapper(monkeypatch):
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.hermes.gateway.default")
    monkeypatch.setattr(gateway, "launchd_gateway_labels_for_install", lambda: [])
    monkeypatch.setattr(gateway, "_locate_launchd_gateway_service", lambda label: (None, None))
    monkeypatch.setattr(gateway, "_gateway_descendants_of", lambda pid: {pid + 1})
    monkeypatch.setattr(
        gateway.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="501\t-\tai.hermes.gateway.default\n601\t-\tai.hermes.gateway.other\n",
            stderr="",
        ),
    )

    assert gateway._get_service_pids(all_profiles=True) == {501, 502, 601, 602}


def test_wrapped_service_gateway_is_excluded_from_manual_sweep(monkeypatch):
    service_pids = {501, 503}
    monkeypatch.setattr(gateway, "_get_service_pids", lambda **kwargs: service_pids)
    monkeypatch.setattr(gateway, "_scan_gateway_pids", lambda *args, **kwargs: [503, 700])

    assert gateway.find_gateway_pids(
        exclude_pids=service_pids,
        all_profiles=True,
    ) == [700]


@pytest.mark.macos_only
def test_launchd_exclusion_protects_real_wrapped_process(tmp_path, monkeypatch):
    """Service lookup is simulated, but ancestry and argv come from real child processes."""
    import json
    import os
    import subprocess
    import sys
    import time

    package = tmp_path / "hermes_cli"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "main.py").write_text("import time; time.sleep(60)\n")
    pid_file = tmp_path / "child.json"
    wrapper_code = (
        "import subprocess,sys,json,pathlib; "
        "p=subprocess.Popen([sys.executable,'-m','hermes_cli.main','gateway','run']); "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(p.pid)); p.wait()"
    )
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    wrapper = subprocess.Popen([sys.executable, "-c", wrapper_code, str(pid_file)], cwd=tmp_path, env=env)
    child = None
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists(), "test wrapper did not start its child"
        child = psutil.Process(json.loads(pid_file.read_text()))
        monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.hermes.gateway.test")
        monkeypatch.setattr(gateway, "_locate_launchd_gateway_service", lambda label: ("gui/501", wrapper.pid))
        service_pids = gateway._get_service_pids()
        # A genuine manual gateway stays eligible while the wrapped runtime is protected.
        monkeypatch.setattr(gateway, "_scan_gateway_pids", lambda *args, **kwargs: [child.pid, 700])
        monkeypatch.setattr(gateway, "_get_service_pids", lambda **kwargs: service_pids)
        monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **kwargs: [])
        from hermes_cli.update_cmd_fleet import _restart_manual_gateways
        signals = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append(pid))
        outcome = SimpleNamespace(
            killed_pids=set(), stopped_unmapped_pids=set(), restarted_services=[],
            relaunched_profiles=[], externally_supervised_profiles=[],
        )
        _restart_manual_gateways(outcome, 45)
        assert signals == [700]
        assert outcome.killed_pids == {700}
        assert child.is_running()
    finally:
        monkeypatch.undo()
        if child is not None and child.is_running():
            child.terminate()
        wrapper.wait(timeout=10)

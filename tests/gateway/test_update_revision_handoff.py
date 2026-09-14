"""Pinned admission crosses real CLI, control socket, detached child and Git boundaries.

Only the fixture child is launched: it exercises source preparation against a local
Git remote, never installs dependencies, starts services or touches the live checkout.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


_CHILD = '''
import argparse, json, sys
from pathlib import Path
from hermes_cli.subcommands.update import build_update_parser
from hermes_cli.update_cmd import _git_run
from hermes_cli.update_revision import prepare_revision_target, checkout_revision
checkout, report = map(Path, sys.argv[1:3])
p = argparse.ArgumentParser()
build_update_parser(p.add_subparsers(), cmd_update=lambda args: None)
a = p.parse_args(sys.argv[3:])
result = {"argv": sys.argv[3:], "revision": a.revision, "gateway": a.gateway}
try:
    assert a.revision, "fixture refuses unpinned updater"
    target = prepare_revision_target(_git_run, ["git"], checkout, a.revision)
    checkout_revision(_git_run, ["git"], checkout, target)
except Exception as exc:
    result["error"] = str(exc)
report.write_text(json.dumps(result))
sys.exit(1 if "error" in result else 0)
'''


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, timeout=30).stdout.strip()


def _repository(root):
    seed, remote, checkout = (root / name for name in ("seed", "remote.git", "checkout"))
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.name", "Fixture")
    _git(seed, "config", "user.email", "fixture@example.invalid")
    for name in ("hermes_cli/main.py", "gateway/run.py", "run_agent.py", "pyproject.toml"):
        file = seed / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("# fixture\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "incompatible stale main")
    stale = _git(seed, "rev-parse", "HEAD")
    (seed / "runtime-compatibility.json").write_text(json.dumps({
        "schema": 1, "capabilities": ["delegation-admitted-v1", "managed-downgrade-floor-v1"],
    }))
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "compatible installed release")
    installed = _git(seed, "rev-parse", "HEAD")
    _git(root, "clone", "--bare", str(seed), str(remote))
    # Advertise the incompatible target: servers need not permit fetching an
    # unadvertised ancestor by SHA, even when its object exists in the remote.
    _git(seed, "push", str(remote), f"{stale}:refs/heads/fixture-stale")
    _git(root, "clone", str(remote), str(checkout))
    _git(checkout, "checkout", "--detach", installed)
    _git(checkout, "branch", "-f", "main", stale)
    (seed / "target.txt").write_text("compatible intended target\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "compatible target")
    target = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", str(remote), "HEAD:main")
    return checkout, stale, installed, target


# This integration launches only the isolated gateway CLI and the explicit fixture
# executable below; the fixture's source mutations are confined to its temp Git repo.
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("_host", [pytest.param("linux", marks=pytest.mark.linux_only),
                                  pytest.param("darwin", marks=pytest.mark.macos_only)])
@pytest.mark.parametrize("case", ["compatible", "incompatible", "malformed", "legacy", "duplicate"])
@pytest.mark.asyncio
async def test_revision_handoff_never_falls_back_or_claims_completion(tmp_path, monkeypatch, case, _host):
    from gateway import run
    from hermes_state import SessionDB

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    checkout, stale, installed, target = _repository(tmp_path)
    # No live-state imports/configuration or executable is used by the child.
    helper = tmp_path / "fixture child.py"
    helper.write_text(_CHILD)
    report = tmp_path / "child.json"
    monkeypatch.setattr(run, "_hermes_home", home)
    monkeypatch.setattr(run, "_resolve_hermes_bin", lambda: [sys.executable, str(helper), str(checkout), str(report)])
    db = SessionDB(home / "state.db")
    db.create_session("parent", source="telegram", chat_id="fixture", chat_type="private")
    db.create_session("child", source="delegate", parent_session_id="parent")
    runner = SimpleNamespace(_session_db=SimpleNamespace(_db=db),
                             config=SimpleNamespace(multiplex_profiles=False),
                             _schedule_update_notification_watch=Mock())
    server = await run._start_gateway_start_control_socket(runner)
    assert server is not None
    if case == "legacy":
        server._handlers.pop("agent-update-revision", None)
    marker = home / ".update_pending.json"
    before = b""
    if case == "duplicate":
        marker.write_text(json.dumps({"reason": "prior request", "revision": installed}))
        before = marker.read_bytes()
    revision = {"incompatible": stale, "malformed": "A" * 40}.get(case, target)
    env = {**os.environ, "HOME": str(home), "HERMES_HOME": str(home),
           "HERMES_SESSION_ID": "child", "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    try:
        if case == "compatible":
            from gateway.control_socket import query_gateway_control
            for payload in ({"reason": "fixture", "session_id": "child"},
                            {"reason": "fixture", "session_id": "child", "revision": None}):
                refused = await asyncio.to_thread(query_gateway_control, home, "agent-update-revision", payload=payload)
                assert refused and not refused["accepted"]
                assert not marker.exists()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "hermes_cli.main", "gateway", "update", "--reason", "fixture handoff",
            "--revision", revision, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = out.decode() + err.decode()
        if case in {"malformed", "legacy", "duplicate"}:
            assert proc.returncode != 0, output
            assert not report.exists()
            runner._schedule_update_notification_watch.assert_not_called()
            if case == "duplicate":
                assert marker.read_bytes() == before
                assert "already pending" in output
                assert "End this turn now and inspect the existing update" in output
                assert "Do not retry automatically or fall back to an unpinned update" in output
            else:
                assert not (home / ".update_pending.json").exists()
            assert "request accepted" not in output.lower()
        else:
            assert proc.returncode == 0, output
            assert "request accepted" in output.lower() and "not complete" in output.lower()
            async with asyncio.timeout(30):
                while not (home / ".update_process_exit_code").exists():
                    await asyncio.sleep(0.05)
            result = json.loads(report.read_text())
            assert result["argv"] == ["update", "--gateway", "--revision", revision]
            assert result["gateway"] and result["revision"] == revision
            pending = json.loads((home / ".update_pending.json").read_text())
            assert pending["revision"] == revision and pending["parent_session_id"] == "parent"
            runner._schedule_update_notification_watch.assert_called_once()
            if case == "compatible":
                assert "error" not in result
                assert (home / ".update_process_exit_code").read_text().strip() == "0"
            else:
                assert "compatibility guard refused" in result["error"]
                assert (home / ".update_process_exit_code").read_text().strip() == "1"
        assert _git(checkout, "rev-parse", "HEAD") == (target if case == "compatible" else installed)
        assert _git(checkout, "rev-parse", "main") == stale
        assert _git(checkout, "status", "--porcelain") == ""
        assert _git(checkout, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    finally:
        await server.stop()
        db.close()


@pytest.mark.parametrize("revision", ["main", "A" * 40, "a" * 39, " a" + "a" * 39, "", 42])
def test_untrusted_revision_rejected_before_admission_or_spawn(tmp_path, revision):
    from gateway.update_launcher import launch_native_update, make_agent_update_handler
    home = tmp_path / "admission"
    home.mkdir()
    spawn = Mock()
    handler = make_agent_update_handler(runner=Mock(), home=home, main_loop=Mock(),
                                        resolve_hermes_bin=Mock(), spawn=spawn, is_managed=lambda: False)
    result = handler({"reason": "fixture", "session_id": "child", "revision": revision})
    assert not result["accepted"] and "40-character lowercase" in result["error"]
    with pytest.raises(ValueError, match="40-character lowercase"):
        launch_native_update(home=home, hermes_cmd=["never"], pending={}, spawn=spawn, revision=revision)
    spawn.assert_not_called()
    assert list(home.iterdir()) == []

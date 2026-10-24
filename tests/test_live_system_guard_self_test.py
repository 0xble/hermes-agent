"""Self-test for the live-system guard fixture in tests/conftest.py.

This file is the canary. If anyone removes a guard or weakens it, these
tests fail. If anyone adds a NEW kill primitive to the codebase without
adding it to the guard, the corresponding test added here will fail too.

The guard exists to protect the developer's live ``hermes-gateway`` process
from being SIGTERMed by tests. See PR #23397 for the original incident
(5+ live gateway kills in 3 days). Per Teknium 2026-05-10:

  > "You better do such a deep scan and scrub of the tests that this
  >  never is possible ever again for all eternity."

Every primitive that can deliver a signal to a foreign process or mutate
the live systemd unit MUST be exercised below. Adding a new primitive to
the guard? Add a test here too.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import types
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

# A guaranteed-foreign PID: PID 1 (init).  Owned by root, not us, and
# always exists. A sane guard refuses to signal it.
FOREIGN_PID = 1


# ──────────────────── fail-closed self-protection ──────────────
#
# This file executes REAL kill primitives — os.kill(-1, SIGTERM), os.killpg,
# pkill -f python — and depends entirely on the autouse ``_live_system_guard``
# fixture in tests/conftest.py to intercept them. That makes the canary
# fail-OPEN: in any collection context where this file is present but its home
# conftest is not, the primitives fire for real and ``os.kill(-1, SIGTERM)``
# SIGTERMs every process the invoking user owns (a full desktop-session kill was
# reported in the field — see issue #68311). Such contexts are not exotic:
# published sdists that ship ``tests/`` but not ``tests/conftest.py``, trees
# assembled by copying ``test*.py`` files (that glob does NOT match
# ``conftest.py``), ``pytest --noconftest``, or running from a foreign rootdir.
#
# The fixture below makes the canary fail-CLOSED instead: it refuses to run any
# test in this file unless the guard is provably active, so no collection
# context can ever detonate the primitives. The one thing the canary can detect
# about its own safety is that the guard monkeypatches ``os.kill`` with a plain
# Python function, whereas the unguarded primitive is a C builtin.


def _live_system_guard_is_active() -> bool:
    """True iff tests/conftest.py's ``_live_system_guard`` has patched os.kill.

    The guard replaces ``os.kill`` with a plain Python function; the raw,
    unguarded primitive is a C builtin (``types.BuiltinFunctionType``). If
    ``os.kill`` is still the builtin, the guard never loaded and every kill
    primitive in this file would fire for real.
    """
    return not isinstance(os.kill, types.BuiltinFunctionType)


@pytest.fixture(autouse=True)
def _refuse_to_fire_live_weapons(request):
    """Fail closed: refuse to run a canary test unless the guard is active.

    Tests genuinely marked ``@pytest.mark.live_system_guard_bypass`` opt out
    (they run the raw primitive deliberately and harmlessly, e.g. a signal-0
    liveness probe of our own PID), matching the guard's own bypass contract.
    """
    if request.node.get_closest_marker("live_system_guard_bypass"):
        yield
        return
    if not _live_system_guard_is_active():
        pytest.fail(
            "REFUSING TO RUN: the live-system guard from tests/conftest.py is "
            "not active in this interpreter (os.kill is still the raw C "
            "builtin). This canary file executes real kill primitives — "
            "os.kill(-1, SIGTERM), os.killpg, pkill -f python — and relies on "
            "the guard to intercept them; unguarded, they SIGTERM every process "
            "the current user owns. This usually means the file was collected "
            "without its home tests/conftest.py (note: a test*.py copy glob "
            "does NOT match conftest.py). See issue #68311.",
            pytrace=False,
        )
    yield


def test_fail_closed_probe_reports_guard_active():
    """In the real suite the guard is loaded, so the probe reports active and
    ``_refuse_to_fire_live_weapons`` stays out of the way (no false positives
    that would wedge CI)."""
    assert _live_system_guard_is_active() is True




# ──────────────────── kill primitives ─────────────────────────


@pytest.mark.platforms("linux")
def test_os_kill_blocks_foreign_pid():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.kill(FOREIGN_PID, signal.SIGTERM)


def test_os_kill_blocks_negative_one():
    """``os.kill(-1, sig)`` signals every process we can reach. Must be blocked."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.kill(-1, signal.SIGTERM)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="killpg POSIX-only")
def test_os_killpg_blocks_foreign_pgid():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.killpg(FOREIGN_PID, signal.SIGTERM)


@contextmanager
def _record_signal_delivery(request, *, deliver=False):
    """Install the real fixture above recording sinks, never bypass its checks."""
    from tests._fixtures.live_system_guard import _live_system_guard

    with pytest.MonkeyPatch.context() as patch:
        kill = Mock(wraps=os.kill if deliver else None)
        killpg = Mock(wraps=os.killpg if deliver else None) if hasattr(os, "killpg") else None
        patch.setattr(os, "kill", kill)
        if killpg is not None:
            patch.setattr(os, "killpg", killpg)
        guard = _live_system_guard.__wrapped__(request, patch)
        next(guard)
        try:
            yield kill, killpg
        finally:
            with pytest.raises(StopIteration):
                next(guard)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("primitive", ["kill", "killpg"])
def test_signal_guard_preserves_zero_as_own_group(request, primitive):
    # Recording sinks prove pass-through without signalling pytest's own group.
    with _record_signal_delivery(request) as (kill, killpg):
        getattr(os, primitive)(0, signal.SIGTERM)
        sink = kill if primitive == "kill" else killpg
        assert sink is not None
        sink.assert_called_once_with(0, signal.SIGTERM)


def _missing_signal_target(state, primitive, pid, target, lookup, monkeypatch):
    import psutil

    if state == "missing_during_parents":
        target.parents.side_effect = psutil.NoSuchProcess(pid)
    else:
        lookup.side_effect = psutil.NoSuchProcess(pid)
    if not state.startswith("missing_leader_"):
        return ProcessLookupError
    # A missing leader is not proof of either group ownership or absence.
    member = Mock(pid=pid + 1)
    member.create_time.return_value = 11.0
    member.parents.return_value = (
        [types.SimpleNamespace(pid=os.getpid())]
        if state == "missing_leader_owned_member" else []
    )

    def leader_gone(value):
        if value == member.pid:
            return member
        raise psutil.NoSuchProcess(value)

    lookup.side_effect = leader_gone
    pids = Mock(return_value=[member.pid])
    getpgid = Mock(return_value=pid)
    monkeypatch.setattr(psutil, "pids", pids)
    monkeypatch.setattr(os, "getpgid", getpgid, raising=False)
    if state == "missing_leader_reaped_member":
        getpgid.side_effect = ProcessLookupError()
    elif state == "missing_leader_unverifiable_member":
        getpgid.side_effect = PermissionError()
    elif state == "missing_leader_unverifiable_scan":
        pids.side_effect = psutil.AccessDenied(member.pid)
    if primitive == "killpg" and state == "missing_leader_owned_member":
        return None
    return (
        RuntimeError if primitive == "killpg" and state != "missing_leader_reaped_member"
        else ProcessLookupError
    )


@pytest.mark.parametrize("primitive", ["kill", pytest.param("killpg", marks=pytest.mark.platforms("posix"))])
@pytest.mark.parametrize(
    "state",
    [
        "owned", "foreign", "foreign_ancestor_gone", "missing_before", "missing_during_parents",
        "access_denied_before", "access_denied_parents", "access_denied_identity",
        "unexpected_lookup_error", "initial_owned", "initial_recycled",
        "initial_unverifiable", "recycled_during_parents",
        "missing_leader_owned_member", "missing_leader_foreign_member",
        "missing_leader_unverifiable_member", "missing_leader_unverifiable_scan",
        "missing_leader_reaped_member",
    ],
)
def test_signal_guard_only_delivers_to_verified_owned_targets(request, monkeypatch, primitive, state):
    """Vanished PIDs are not an allowlist, and a missing leader is not a missing group."""
    import psutil

    pid = 987654321
    target = Mock(pid=pid)
    target.create_time.return_value = 10.0
    target.children.return_value = []
    target.parents.return_value = [types.SimpleNamespace(pid=os.getpid())]
    owner = Mock()
    owner.children.return_value = [target] if state.startswith("initial_") else []
    lookup = Mock(side_effect=lambda value: owner if value == os.getpid() else target)
    monkeypatch.setattr(psutil, "Process", lookup)
    monkeypatch.setattr(psutil, "pids", Mock(return_value=[pid]))
    monkeypatch.setattr(os, "getpgid", Mock(return_value=pid), raising=False)

    with _record_signal_delivery(request) as (kill, killpg):
        sink = kill if primitive == "kill" else killpg
        assert sink is not None
        if state == "initial_owned":
            target.parents.return_value = []  # Identity survives reparenting.
        expected = RuntimeError
        if state in {"owned", "initial_owned"}:
            expected = None
        elif state == "foreign":
            target.parents.return_value = []
        elif state == "foreign_ancestor_gone":
            # A vanished ancestor does not prove that this specific target vanished.
            target.parents.side_effect = psutil.NoSuchProcess(pid + 1)
        elif state.startswith("missing_"):
            expected = _missing_signal_target(state, primitive, pid, target, lookup, monkeypatch)
        elif state == "access_denied_before":
            lookup.side_effect = psutil.AccessDenied(pid)
        elif state == "access_denied_parents":
            target.parents.side_effect = psutil.AccessDenied(pid)
        elif state in {"access_denied_identity", "initial_unverifiable"}:
            target.create_time.side_effect = psutil.AccessDenied(pid)
        elif state == "unexpected_lookup_error":
            lookup.side_effect = ValueError("unverifiable process identity")
            expected = ValueError
        elif state == "initial_recycled":
            target.create_time.return_value = 20.0
        elif state == "recycled_during_parents":
            replacement = Mock(pid=pid)
            replacement.create_time.return_value = 20.0
            lookup.side_effect = [target, replacement]

        if expected is None:
            getattr(os, primitive)(pid, signal.SIGTERM)
            sink.assert_called_once_with(pid, signal.SIGTERM)
        else:
            with pytest.raises(expected):
                getattr(os, primitive)(pid, signal.SIGTERM)
            sink.assert_not_called()
        other = killpg if primitive == "kill" else kill
        if other is not None:
            other.assert_not_called()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("primitive", ["kill", "killpg"])
@pytest.mark.parametrize("interleave", ["live", "reap_before_handle", "reap_during_parents"])
def test_signal_guard_handles_owned_child_exit_interleavings(request, monkeypatch, primitive, interleave):
    """Synchronize real child exit at the two inspection boundaries, without delays."""
    import psutil

    with _record_signal_delivery(request, deliver=True) as sinks, subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as child:
        original_process = psutil.Process
        original_init = psutil.Process.__init__
        original_parents = psutil.Process.parents
        identity = original_process(child.pid).create_time()
        assert original_process(child.pid).ppid() == os.getpid()
        assert os.getpgid(child.pid) == child.pid
        assert child.stdin is not None

        def reap():
            child.stdin.close()
            assert child.wait(timeout=5) == 0

        def inspect_process(process, pid=None):
            if pid == child.pid and interleave == "reap_before_handle":
                reap()
            original_init(process, pid)

        def inspect_parents(process):
            if process.pid == child.pid and interleave == "reap_during_parents":
                reap()
            return original_parents(process)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(original_process, "__init__", inspect_process)
                patch.setattr(original_process, "parents", inspect_parents)
                sink = sinks[0] if primitive == "kill" else sinks[1]
                assert sink is not None
                if interleave == "live":
                    getattr(os, primitive)(child.pid, signal.SIGTERM)
                    assert child.wait(timeout=5) == -signal.SIGTERM
                    sink.assert_called_once_with(child.pid, signal.SIGTERM)
                else:
                    with pytest.raises(ProcessLookupError):
                        getattr(os, primitive)(child.pid, signal.SIGTERM)
                    for sink in sinks:
                        assert sink is not None
                        sink.assert_not_called()
        finally:
            if child.poll() is None:
                assert original_process(child.pid).create_time() == identity
                reap()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("primitive", ["kill", "killpg"])
def test_signal_guard_keeps_owned_orphan_identity(request, primitive):
    """A verified helper remains owned after TERM reaps its group leader."""
    import psutil

    script = (
        "import os,signal,sys\n"
        "if os.fork() == 0:\n"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        " print(os.getpid(), flush=True)\n"
        "sys.stdin.buffer.read()\n"
    )
    with _record_signal_delivery(request, deliver=True), subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        start_new_session=True, text=True,
    ) as leader:
        assert leader.stdin is not None and leader.stdout is not None
        helper = psutil.Process(int(leader.stdout.readline()))
        identity = helper.create_time()
        try:
            os.kill(leader.pid, signal.SIGTERM)
            assert leader.wait(timeout=5) == -signal.SIGTERM
            assert all(parent.pid != os.getpid() for parent in helper.parents())
            assert helper.create_time() == identity
            getattr(os, primitive)(helper.pid if primitive == "kill" else leader.pid, signal.SIGKILL)
            assert leader.stdout.read() == ""  # EOF proves the surviving helper released its pipe.
        finally:
            leader.stdin.close()  # Also releases the helper if an assertion failed.
            leader.stdout.close()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("state", ["owned", "recycled", "unverifiable", "foreign", "mixed"])
@pytest.mark.parametrize("leader_exited", [False, True])
def test_signal_guard_rechecks_group_members(request, monkeypatch, state, leader_exited):
    """Saved ownership never authorizes a replacement or a foreign group member."""
    import psutil

    leader_pid = 987654321
    member = Mock(pid=leader_pid + 1)
    member.create_time.return_value = 10.0
    member.parents.return_value = [types.SimpleNamespace(pid=os.getpid())]
    foreign = Mock(pid=leader_pid + 2)
    foreign.create_time.return_value = 20.0
    foreign.parents.return_value = []
    leader = Mock(pid=leader_pid)
    leader.create_time.return_value = 9.0
    leader.parents.return_value = [types.SimpleNamespace(pid=os.getpid())]
    leader.children.return_value = [member]
    owner = Mock()
    owner.children.return_value = []
    processes = {os.getpid(): owner, leader_pid: leader, member.pid: member}

    def lookup(pid):
        if pid not in processes:
            raise psutil.NoSuchProcess(pid)
        return processes[pid]

    monkeypatch.setattr(psutil, "Process", lookup)
    monkeypatch.setattr(psutil, "pids", lambda: [pid for pid in processes if pid != os.getpid()])
    monkeypatch.setattr(os, "getpgid", lambda pid: leader_pid)
    with _record_signal_delivery(request) as (kill, killpg):
        os.kill(leader_pid, signal.SIGTERM)
        kill.assert_called_once_with(leader_pid, signal.SIGTERM)
        if leader_exited:
            del processes[leader_pid]
        member.parents.return_value = []
        if state == "recycled":
            member.create_time.return_value = 30.0
        elif state == "unverifiable":
            member.create_time.side_effect = psutil.AccessDenied(member.pid)
        elif state in {"foreign", "mixed"}:
            processes[foreign.pid] = foreign
            if state == "foreign":
                del processes[member.pid]
        assert killpg is not None
        if state == "owned":
            os.killpg(leader_pid, signal.SIGKILL)
            killpg.assert_called_once_with(leader_pid, signal.SIGKILL)
        else:
            with pytest.raises(RuntimeError, match="live-system guard"):
                os.killpg(leader_pid, signal.SIGKILL)
            killpg.assert_not_called()


# ──────────────────── subprocess regex bypasses ────────────────


def test_subprocess_run_systemctl_restart_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["systemctl", "--user", "restart", "hermes-gateway"], check=False)


@pytest.mark.parametrize(
    "executable",
    [
        r"C:\repo\.venv\Scripts\hermes.exe",
        r"C:\repo\.venv\Scripts\HERMES.EXE",
        r"C:\repo\hermes.exe",
        r"C:\repo\HERMES.EXE",
        r"C:\repo\.venv\Scripts\hermes",
        r"C:\repo\.venv\Scripts\HERMES",
        r"C:\repo\hermes",
        r"C:\repo\HERMES",
    ],
)
def test_subprocess_run_windows_native_hermes_update_blocked(executable):
    """Native Windows argv preserves backslashes and must still block updates."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        _check_update_command_without_spawning([executable, "update"])


def _check_update_command_without_spawning(command, kwargs=None):
    # Exercise the installed guard, never a real updater if the canary fails.
    import inspect

    check = inspect.getclosurevars(subprocess.run).nonlocals["_check_subprocess_cmd"]
    check("Popen", command, kwargs)


@pytest.mark.parametrize(
    "command, blocked",
    [
        pytest.param(["bash", "-c", "hermes update --gateway"], True, id="bash-c"),
        pytest.param(["setsid", "bash", "-c", "python -m hermes_cli.main update --gateway"], True, id="setsid-bash-c"),
        pytest.param(["sh", "-lc", "hermes update"], True, id="sh-lc"),
        pytest.param(["bash", "-lc", "--", "hermes update"], True, id="shell-command-option-boundary"),
        pytest.param(["zsh", "-ic", "hermes update"], True, id="zsh-ic"),
        pytest.param(["dash", "-ilc", "hermes update"], True, id="dash-combined-c"),
        pytest.param(["env", "MODE=test", "nohup", "timeout", "10", "sudo", "-u", "nobody", "/bin/bash", "-lc", "hermes update"], True, id="prefixed-bash-lc"),
        pytest.param(["cmd", "/c", r"C:\repo\hermes.exe update"], True, id="cmd-c"),
        pytest.param(["cmd.exe", "/c", r'"C:\Program Files\hermes.exe" update'], True, id="cmd-exe-c-quoted"),
        pytest.param(["powershell", "-Command", r"C:\repo\hermes.exe update"], True, id="powershell-command"),
        pytest.param(["pwsh", "-c", r"& 'C:\Program Files\hermes.exe' update"], True, id="pwsh-c-quoted"),
        pytest.param(["bash", "-c", "sh -lc 'hermes update'"], True, id="nested-shells"),
        pytest.param(["bash", "-c", "hermes update '"], True, id="malformed-shell-fail-closed"),
        pytest.param(["bash", "-c", "hermes update; true"], True, id="shell-operator"),
        pytest.param(["bash", "-c", "echo ok; sh -c 'hermes update'"], True, id="nested-after-command"),
        pytest.param(["bash", "-c", "sh -c true; sh -c 'hermes update'"], True, id="second-shell-payload"),
        pytest.param(["bash", "-c", "python -m hermes_cli.main update '"], True, id="malformed-module-fail-closed"),
        pytest.param(["bash", "-c", "hermes status"], False, id="shell-read-only"),
        pytest.param(["cmd", "/c", r"C:\hermes-ci-home\npm-fixture\npm.cmd update"], True, id="cmd-npm-update-conservative-base"),
        pytest.param(["python", "-c", "print(\"hermes update\")"], False, id="non-shell-payload"),
        pytest.param(["bash", "-c", r'printf %s "C:\Program Files\npm.cmd update"'], False, id="shell-data-argument"),
    ],
)
def test_shell_wrapper_hermes_update_blocked_without_spawning(command, blocked):
    if blocked:
        with pytest.raises(RuntimeError, match="would run.*hermes update"):
            _check_update_command_without_spawning(command)
    else:
        _check_update_command_without_spawning(command)


@pytest.mark.parametrize(
    "command, blocked",
    [
        pytest.param(r"C:\repo\.venv\Scripts\hermes.exe update", True, id="native-windows-string"),
        pytest.param(r'"C:\Program Files\hermes.exe" update', True, id="quoted-windows-spaces"),
        pytest.param(r"C:\Program Files\hermes.exe update", True, id="unquoted-windows-spaces"),
        pytest.param(r'"C:\Program Files\hermes.exe update', True, id="malformed-windows-fail-closed"),
        pytest.param(r"C:\hermes-ci-home\npm-fixture\npm.cmd update", True, id="native-npm-update-conservative-base"),
        pytest.param(r'"C:\Program Files\hermes.exe" status', False, id="windows-read-only"),
    ],
)
def test_windows_string_hermes_update_blocked_without_spawning(command, blocked):
    if blocked:
        with pytest.raises(RuntimeError, match="would run.*hermes update"):
            _check_update_command_without_spawning(command)
    else:
        _check_update_command_without_spawning(command)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["python", "hermes_cli/main.py", "update"], id="relative-script"),
        pytest.param(["python", "/repo/hermes_cli/main.py", "update"], id="absolute-posix-script"),
        pytest.param(["python", r"C:\repo\hermes_cli\main.py", "update"], id="windows-script"),
        pytest.param(["bash", "-c", "python hermes_cli/main.py update"], id="bash-script"),
        pytest.param(["bash", "-c", 'exec hermes "$@"', "--", "update"], id="shell-positional-update"),
    ],
)
def test_round_three_updater_bypasses_blocked_without_spawning(command):
    with pytest.raises(RuntimeError, match="would run.*hermes update"):
        _check_update_command_without_spawning(command)


def test_updater_guard_is_at_least_as_blocking_as_pre_pr_base():
    """Cross-product regression: every updater the frozen baseline blocks stays blocked."""
    import inspect
    import shlex

    check = inspect.getclosurevars(subprocess.run).nonlocals["_check_subprocess_cmd"]
    base = inspect.getclosurevars(check).nonlocals["_base_is_hermes_update"]
    entrypoints = [
        ["hermes"], ["HERMES"], ["hermes.exe"], ["hermes.cmd"],
        ["/repo/.venv/bin/hermes"], ["/repo with spaces/.venv/bin/hermes"],
        [r"C:\repo\.venv\Scripts\hermes.exe"], [r"C:\Program Files\HERMES.EXE"],
        ["python", "-m", "hermes_cli.main"], ["python3", "-m", "hermes_cli.main.py"],
        ["python", "hermes_cli/main.py"], ["python", "/repo/hermes_cli/main.py"],
        ["python", r"C:\repo\hermes_cli\main.py"],
    ]
    suffixes = [["update"], ["update", "--gateway"], ["--profile", "test", "update"],
                ["UPDATE"], ["update", "--yes"], ["update;", "true"]]
    wrappers = [
        lambda argv: argv,
        lambda argv: ["env", "MODE=test", *argv],
        lambda argv: ["setsid", *argv],
        lambda argv: ["sudo", "-u", "nobody", *argv],
        lambda argv: ["bash", "-c", " ".join(argv)],
        lambda argv: ["setsid", "bash", "-lc", " ".join(argv)],
        lambda argv: ["bash", "-c", 'exec "$@"', "--", *argv],
        lambda argv: ["cmd.exe", "/c", " ".join(argv)],
        lambda argv: ["powershell", "-Command", " ".join(argv)],
        lambda argv: ["bash", "-c", "sh -lc " + shlex.quote(" ".join(argv))],
    ]
    matched = 0
    for entrypoint in entrypoints:
        for suffix in suffixes:
            for wrap in wrappers:
                argv = wrap([*entrypoint, *suffix])
                text = " ".join(argv)
                for command in (argv, tuple(argv), text, text.encode()):
                    # Frozen behavior from adaf68a8, independent of the live helper.
                    low = text.lower()
                    base_blocked = "update" in low and (
                        ("hermes" in low and "update" in low.split())
                        or ("hermes_cli" in low and "update" in low.split())
                        or (".venv/bin/hermes" in low and "update" in low)
                    )
                    assert base(command) == base_blocked, command
                    if base_blocked:
                        matched += 1
                        with pytest.raises(RuntimeError, match="would run.*hermes update"):
                            _check_update_command_without_spawning(command)
    assert matched > 0  # The implication must not pass vacuously.
    print(f"pre-PR base implication: {matched} updater shapes blocked")


def test_npm_refresh_allowance_is_only_the_direct_temporary_fixture(tmp_path, monkeypatch):
    import tempfile

    temp = tmp_path / "hermes-ci-home"
    root = temp / "pm-deps-fixture"
    repo = root / "repo"
    repo.mkdir(parents=True)
    binary = root / "store" / "npm-fixture"
    binary = binary / "npm.cmd" if os.name == "nt" else binary / "bin" / "npm"
    binary.parent.mkdir(parents=True)
    binary.write_text("fixture, never executed")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    command = [str(binary), "update"]
    kwargs = {"cwd": str(repo)}
    _check_update_command_without_spawning(command, kwargs)
    _check_update_command_without_spawning(tuple(command), kwargs)

    # The exception is not an npm basename allowlist or a shell-string exemption.
    denied = [
        (command, None), (command, {"cwd": str(temp)}),
        (command, {**kwargs, "shell": True}),
        (command, {**kwargs, "executable": "hermes"}),
        ([*command, "--gateway"], kwargs),
        (" ".join(command), kwargs),
        (["bash", "-c", " ".join(command)], kwargs),
        (["cmd.exe", "/c", " ".join(command)], kwargs),
        ([str(root / "store" / "npm-fixture" / "hermes.exe"), "update"], kwargs),
        ([str(root / ".venv" / "bin" / "hermes"), "update"], kwargs),
        (["python", str(root / "hermes_cli" / "main.py"), "update"], kwargs),
        (["bash", "-c", 'exec hermes "$@"', "--", "update"], kwargs),
    ]
    for candidate, options in denied:
        with pytest.raises(RuntimeError, match="would run.*hermes update"):
            _check_update_command_without_spawning(candidate, options)


def test_subprocess_run_full_path_systemctl_blocked():
    """``/usr/bin/systemctl`` (full path) must be blocked too."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["/usr/bin/systemctl", "--user", "stop", "hermes-gateway"], check=False)


def test_subprocess_run_sudo_systemctl_blocked():
    """``sudo systemctl ...`` defeated the old head==systemctl check."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["sudo", "systemctl", "restart", "hermes-gateway"], check=False)


def test_subprocess_run_env_systemctl_blocked():
    """``env systemctl ...`` similarly defeated the old head check."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["env", "systemctl", "--user", "restart", "hermes-gateway"], check=False)


def test_subprocess_run_bash_c_systemctl_blocked():
    """``bash -c "systemctl ..."`` must also be caught."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["bash", "-c", "systemctl --user restart hermes-gateway"], check=False)




def test_subprocess_run_setsid_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["setsid", "systemctl", "kill", "hermes-gateway"], check=False)


def test_subprocess_run_string_shell_true_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(
            "systemctl --user restart hermes-gateway",
            shell=True,
            check=False,
        )


def test_subprocess_popen_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(["systemctl", "--user", "stop", "hermes-gateway"])


def test_subprocess_call_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.call(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_check_call_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.check_call(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_check_output_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.check_output(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_getoutput_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.getoutput("systemctl --user restart hermes-gateway")


def test_subprocess_getstatusoutput_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.getstatusoutput("systemctl --user restart hermes-gateway")


# ──────────────────── os.system / os.popen ────────────────────


def test_os_system_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.system("systemctl --user restart hermes-gateway")


def test_os_popen_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.popen("systemctl --user restart hermes-gateway")


# ──────────────────── pty.spawn ────────────────────────────────


@pytest.mark.platforms("linux")
def test_pty_spawn_systemctl_blocked():
    import pty
    with pytest.raises(RuntimeError, match="live-system guard"):
        pty.spawn(["systemctl", "--user", "restart", "hermes-gateway"])


# ──────────────────── asyncio.create_subprocess_* ──────────────


def test_asyncio_create_subprocess_exec_systemctl_blocked():
    import asyncio

    async def _attempt():
        await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", "hermes-gateway"
        )

    with pytest.raises(RuntimeError, match="live-system guard"):
        asyncio.run(_attempt())


def test_asyncio_create_subprocess_shell_systemctl_blocked():
    import asyncio

    async def _attempt():
        await asyncio.create_subprocess_shell(
            "systemctl --user restart hermes-gateway"
        )

    with pytest.raises(RuntimeError, match="live-system guard"):
        asyncio.run(_attempt())


# ──────────────────── pkill / killall / taskkill ───────────────


def test_subprocess_pkill_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "hermes"], check=False)




def test_subprocess_pkill_python_dash_f_blocked():
    """``pkill -f python`` matches the gateway's "python -m hermes_cli.main"."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "python"], check=False)


def test_subprocess_killall_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["killall", "hermes"], check=False)


# ──────────────────── pass-through cases (must NOT raise) ──────
















# ──────────────────── real gateway runtime spawn ─────────────────


def test_subprocess_popen_real_gateway_restart_blocked():
    """``python -m hermes_cli.main gateway restart`` is a detached child that
    inherits the pytest-tmp HERMES_HOME, resolves the developer's real
    ``hermes-gateway`` unit, and outlives the test (39 six-day orphans squatted
    the webhook port, 2026-09-03). Blocked at the spawn primitive."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(
            [sys.executable, "-m", "hermes_cli.main", "gateway", "restart"],
            start_new_session=True,
        )


def test_subprocess_popen_inline_source_restart_watcher_blocked():
    """``gateway._spawn_gateway_restart_watcher`` hides the real gateway argv behind
    ``python -c <src> <old_pid> …``. The identity matcher must ignore that trailing argv (#107002),
    but the guard reads it as SPAWN INTENT — otherwise the watcher sails through, waits out its
    120s deadline and leaves a real detached gateway squatting the webhook port."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(1)", "4242",
             sys.executable, "-m", "hermes_cli.main", "gateway", "run"],
            start_new_session=True,
        )


def test_subprocess_run_gateway_status_passes_through():
    """Only lifecycle verbs are blocked: ``gateway status`` (and every other
    read-only subcommand) must still spawn — via the canonical matcher, not an
    argv substring."""
    result = subprocess.run(
        [sys.executable, "-c", "import sys; print(sys.argv[1:])", "-m", "hermes_cli.main", "gateway", "status"],
        capture_output=True, text=True,
        check=False,
    )
    assert result.returncode == 0


# ──────────────────── bypass marker ─────────────────────────────


@pytest.mark.live_system_guard_bypass
def test_bypass_marker_disables_guard():
    """The bypass marker exists for tests that genuinely need real signal delivery
    (e.g. PTY tests SIGINTing their own child). Verify it works.

    We use it harmlessly here by signaling our own PID 0 (own group) so we
    don't actually kill anything — but the call goes through real os.kill.
    """
    # With bypass, the guard yields without installing the monkeypatch,
    # so we get the real os.kill. Calling os.kill(os.getpid(), 0) just
    # checks that the PID exists — harmless.
    os.kill(os.getpid(), 0)  # No exception — guard is OFF.
    # Signal 0 passes the guard too, so prove the patch itself is absent.
    assert not _live_system_guard_is_active()

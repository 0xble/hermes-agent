"""The passive update check's remote git dies with the CLI that started it.

``hermes chat`` starts ``check_for_updates`` in a daemon thread at startup; on an official
checkout it resolves the stable release and verifies the tag with ``git ls-remote --tags origin``,
which forks ``git remote-https``. A ``/exit`` while that lookup waits on GitHub abandoned the
thread, and the git process and its helper reparented to init and kept running (the 0.23.2
release E2E caught them in ``test_terminal_transcript_pty.py[cli-turns]``).

Real processes, no mocks of process semantics: a child interpreter runs the real
``resolve_source_release`` from a daemon thread against a fake ``git`` that forks a helper and
hangs, then exits the way the CLI does. Both the git and its helper must be gone afterwards.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli._subprocess_compat import pid_exists_stdlib
from tests.waiting import wait_until

pytestmark = [
    pytest.mark.platforms("posix"),  # the owner-death supervisor is POSIX
    # Cleanup SIGKILLs the fake git's pids if they outlived the child: reparented to init, they are
    # outside this test's process subtree but still ours.
    pytest.mark.live_system_guard_bypass,
]

REPO = Path(__file__).resolve().parents[2]
SHA = "a" * 40

_CHILD = textwrap.dedent('''
    import sys, threading
    from hermes_cli import source_releases

    fake_git, cwd, marker = sys.argv[1:4]
    import pathlib, time
    target = pathlib.Path(marker)
    if len(sys.argv) > 4:
        from hermes_cli import process_identity

        def wait_before_registration(pgids):
            pathlib.Path(sys.argv[4]).write_text(str(pgids[0]))
            # Hold the parent in the spawn-to-register window until its main thread exits.
            threading.Event().wait()

        process_identity.own_process_groups = wait_before_registration
        target = pathlib.Path(sys.argv[4])
    answers = {
        "https://api.github.com/repos/o/r/releases/latest":
            {"draft": False, "prerelease": False, "tag_name": "v1.2.3"},
        "https://api.github.com/repos/o/r/commits/v1.2.3": {"sha": "@SHA@"},
    }
    source_releases._json = answers.__getitem__  # GitHub's answers; only git is left to wait on

    threading.Thread(target=source_releases.resolve_source_release, daemon=True,
                     args=("stable", [fake_git], cwd), kwargs={"repository": "o/r", "pointer": False}).start()
    # Exit as /exit does: normally once git and its helper are both running; in the
    # regression case, while registration is blocked and only the bootstrap may exist.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if (len(target.read_text().split()) if target.exists() else 0) >= (1 if len(sys.argv) > 4 else 2):
            break
        time.sleep(0.05)
''').replace("@SHA@", SHA)



@pytest.mark.parametrize('exit_before_registration', [False, True], ids=['running', 'before-registration'])
def test_exit_during_the_stable_tag_lookup_leaves_no_git_behind(tmp_path, exit_before_registration):
    marker = tmp_path / "pids"
    marker.write_text("")
    fake_git = tmp_path / "git"
    # ls-remote's shape: the git process plus a forked transport helper, both waiting on the network.
    fake_git.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        sleep 300 &
        echo $$ $! >> {marker}
        wait
    """))
    fake_git.chmod(0o755)
    home = tmp_path / "hermes-home"
    home.mkdir()
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(REPO)}
    registration = tmp_path / "registering"
    args = [sys.executable, "-c", _CHILD, str(fake_git), str(tmp_path), str(marker)]
    if exit_before_registration:
        registration.touch()
        args.append(str(registration))
    pids: list[int] = []
    try:
        child = subprocess.run(args, cwd=REPO, env=env, capture_output=True, text=True, timeout=60, check=False)
        pids = [int(p) for p in marker.read_text().split()]
        assert child.returncode == 0, child.stderr
        if exit_before_registration:
            registering_pids = [int(p) for p in registration.read_text().split()]
            assert len(registering_pids) == 1, f"spawn did not reach registration: {child.stderr}"
            pids = list(set(pids + registering_pids))
        else:
            assert len(pids) == 2, f"the fake git never ran: {child.stderr}"
        wait_until(lambda: not any(map(pid_exists_stdlib, pids)), timeout=15,
                   what="the update check's git and its helper to die with the CLI")
        if exit_before_registration:
            assert not marker.read_text(), "git executed before its process group was registered"
    finally:
        for pid in pids:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

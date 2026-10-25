"""The source swap of ``hermes update`` is one crash-safe commit point (lane LP-COMMIT).

Each cell drives a real install (HEAD's own ``scripts/install.sh`` in a bwrap sandbox) through a
real ``hermes update`` against a local origin, breaks it at one named point with a real SIGKILL or a
real git failure, and then judges the next real launch from the files the updater itself owns:

* ``kill_after_tree_moved``: git finished the fast-forward of a release with NO dependency change
  and the updater is SIGKILLed before the completion child starts. The launcher tail (launchers,
  builds, config migration, ``install-stamp.json``) must still be owed and finished by the next
  launch — a tail armed only by the child is lost for good.
* ``torn_tree_on_git_failure``: git exits non-zero halfway through writing the release (a
  read-only directory: ``unable to unlink old ...``). The checkout must end whole at its pre-update
  commit, never torn with the interrupted-pull marker already dropped.
* ``kill_during_branch_switch``: the checkout is parked on a fully merged branch, so the update
  first switches it to ``main`` (CP0); the updater dies while git is rewriting files. The next
  launch must put the parked tree back.
* ``kill_mid_zip_swap``: the ZIP swap is SIGKILLed between renames. The next launch must finish or
  roll back the swap, leaving no ``*.hermes-update-staging``/``-old`` sibling to wedge a retry.
* ``kill_mid_zip_swap_with_hermes_cli_moved_aside``: the swap renamed ``hermes_cli/`` (the recovery
  code itself) aside and died before its replacement landed. The next launch must still restore.
* ``syntax_error_target``: a release whose startup module does not compile is refused BEFORE
  HEAD moves (no fast-forward in the reflog), not merely rolled back afterwards.
* ``failed_upstream_sync_after_origin_pull``: a fork checkout whose origin pull already moved the
  tree, then the upstream fast-forward fails (read-only directory) and the updater is killed. The
  tail owed for the moved tree must survive the failed second move.
* ``kill_mid_zip_swap_over_leftover_backup``: the only copy of an entry is a leftover
  ``*.hermes-update-old`` the swap heals before swapping; a kill mid-swap must never delete both.
* ``kill_during_branch_switch_without_local_branch``: the parked checkout has no local ``main``
  (only ``origin/main``); git's checkout guess would create it and rewrite the tree unmarked.
* ``kill_during_syntax_rollback``: a broken release past the preflight is rolled back and the
  updater dies mid-rollback; the next launch must land on the pre-update commit, not the broken one.
* ``git_killed_inside_rollback_reset``: the rollback's own ``git reset -q <pre>`` is SIGKILLed the
  moment its ``index.lock`` exists (HEAD still on the broken release, the lock left behind). The
  updater must keep the interrupted-pull marker (never report the rollback complete), and the next
  launch must reclaim the dead lock, redo the rollback and land on the pre-update commit whole.
"""

from __future__ import annotations

from tests.e2e.core.upgrade.git._hostile_diagnostics import phase

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.e2e.core.upgrade import _helpers as H
from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade.pm import _pm as P
from tests.e2e.core.upgrade.test_upgrade_path import _RETRY_PREFIX
from tests.fakes.fake_llm_provider import FakeLLMServer

pytestmark = [
    pytest.mark.platforms("linux"),
    pytest.mark.live_system_guard_bypass,
    pytest.mark.skipif(H.sandbox_required_reason() is not None, reason=str(H.sandbox_required_reason())),
    pytest.mark.skipif(shutil.which("git") is None, reason="git required"),
    pytest.mark.skipif(I.real_uv() is None, reason="uv required"),
]

REAL_GIT = shutil.which("git") or "git"
ARTIFACT_SUFFIXES = (".hermes-update-staging", ".hermes-update-old")


@pytest.fixture(scope="module")
def provider():
    with FakeLLMServer(default_text="fake reply for the hostile commit suite") as srv:
        yield srv


@pytest.fixture()
def world(tmp_path_factory, provider, hostile_diagnostics):
    root = tmp_path_factory.mktemp("hostile-commit")
    phase("fresh install start", root / "ns-agent.stderr")
    sb, origin = P.install_head(root)
    phase("fresh install complete", sb.hermes_home / "logs" / "update.log")
    P.configure(sb, provider.base_url)
    shim = sb.root / "wrap" / "git"
    world = {"sb": sb, "origin": origin, "root": root, "shim": shim, "shim_text": shim.read_text(encoding="utf-8-sig"), "n": 0}
    yield world
    shim.write_text(world["shim_text"], encoding="utf-8")


def _hostile_git(world, body: str) -> None:
    """Prefix the sandbox's git shim with ``body`` (bash; ``$REAL`` is the real git, ``$PPID`` the
    updater process that spawned git)."""
    text = world["shim_text"]
    head, _, rest = text.partition("\n")
    world["shim"].write_text(f'{head}\nREAL="{REAL_GIT}"\n{body}\n{rest}', encoding="utf-8")


def _release(world, files: dict[str, str]) -> str:
    world["n"] += 1
    return I.publish_commit(world["origin"], world["root"], f"release: hostile commit {world['n']}", files)


def _head(sb) -> str:
    return I.git("rev-parse", "HEAD", cwd=sb.checkout)


def _tracked_dirty(sb) -> str:
    return I.git("status", "--porcelain", "--untracked-files=no", cwd=sb.checkout)


def _stamp_commit(sb) -> str:
    try:
        return json.loads((sb.checkout / "install-stamp.json").read_text(encoding="utf-8-sig")).get("commit") or ""
    except (OSError, ValueError):
        return ""


def _launch(sb, marker: str) -> subprocess.CompletedProcess:
    """A later real launch by the user (fresh pid, lazy installs on: the real launch path)."""
    phase(f"launch start: {marker}")
    cp = P.run_env(sb, [*_RETRY_PREFIX, sb.hermes, "-z", marker], P.lazy_env(sb), timeout=P.UPDATE_TIMEOUT)
    phase(f"launch complete: rc={cp.returncode}")
    return cp


def _update(sb) -> subprocess.CompletedProcess:
    phase("update start")
    cp = P.run_env(sb, [*_RETRY_PREFIX, sb.hermes, "update", "--yes", "--branch", "main", "--no-gateway-restart"],
                   sb.env, timeout=P.UPDATE_TIMEOUT)
    phase(f"update complete: rc={cp.returncode}")
    return cp


def _artifacts(sb) -> list[str]:
    return sorted(p.name for p in sb.checkout.iterdir() if p.name.endswith(ARTIFACT_SUFFIXES))


_ZIP_DRIVER = r"""
import os, signal, sys
sys.path.insert(0, sys.argv[3])  # the install's checkout (the venv's own path points at its workspace)
from hermes_cli import update_cmd_zip as z
import hermes_cli.main as m
assert str(m.PROJECT_ROOT) == sys.argv[3], m.PROJECT_ROOT
swaps, kill_at = [0], sys.argv[2]
def killing(real):
    def move(src, dst):
        real(src, dst)
        if kill_at.startswith("after:"):  # right after the named entry's replacement landed
            if os.path.basename(str(src)) == kill_at[6:] + ".hermes-update-staging":
                os.kill(os.getpid(), signal.SIGKILL)
        elif kill_at == "hermes_cli-moved-aside":  # the live package renamed away, its replacement not yet in
            if os.path.basename(str(src)) == "hermes_cli" and str(dst).endswith(".hermes-update-old"):
                os.kill(os.getpid(), signal.SIGKILL)
        elif str(src).endswith(".hermes-update-staging"):  # one entry swapped in
            swaps[0] += 1
            if swaps[0] >= int(kill_at):
                os.kill(os.getpid(), signal.SIGKILL)
    return move
os.rename, os.replace = killing(os.rename), killing(os.replace)
z._download_and_swap_zip("main", sys.argv[1])
"""


def _zip_killed_at(world, kill_at: str, files: dict[str, str] | None = None):
    """A ZIP update of a fresh release, SIGKILLed at ``kill_at`` (N-th entry swapped in, or a named point)."""
    sb = world["sb"]
    target = _release(world, {"e2e_zip_release.py": f"Z = {world['n']}\n", "hermes_cli/e2e_zip_marker.py": "M = 1\n",
                              **(files or {})})
    archive = world["root"] / f"release-{world['n']}.zip"
    I.git("archive", "--format=zip", "--prefix=hermes-agent-main/", "-o", str(archive), target, cwd=world["origin"])
    driver = world["root"] / "zip_driver.py"
    driver.write_text(_ZIP_DRIVER, encoding="utf-8")
    return P.run_env(sb, [sb.python, str(driver), archive.as_uri(), kill_at, str(sb.checkout)], sb.env,
                     timeout=P.UPDATE_TIMEOUT)


# Run by the git shim inside the sandbox: start the real git under an inotify watch on its git dir and
# SIGKILL it the moment ``index.lock`` is created (git then holds the lock and has not moved HEAD).
_GIT_KILLER = r"""
import ctypes, os, signal, struct, subprocess, sys
real, args = sys.argv[1], sys.argv[2:]
git_dir = subprocess.run([real, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True).stdout.strip()
libc = ctypes.CDLL(None, use_errno=True)
fd = libc.inotify_init1(0)
libc.inotify_add_watch(fd, git_dir.encode(), 0x100)  # IN_CREATE
git = subprocess.Popen([real, *args])
while git.poll() is None:
    buf, i = os.read(fd, 4096), 0
    while i < len(buf):
        length = struct.unpack_from("iIII", buf, i)[3]
        name, i = buf[i + 16:i + 16 + length].rstrip(b"\0"), i + 16 + length
        if name == b"index.lock":
            os.kill(git.pid, signal.SIGKILL)
            git.wait()
            sys.exit(137)
sys.exit(git.returncode)
"""

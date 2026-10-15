"""Shallow and stale installs through the real ``hermes update``.

N-1's ``scripts/install.sh`` clones ``--depth 1 --single-branch``: that is the checkout every
N-1 user has. Three independent runner-managed files reuse these worlds over smart HTTP:

* ``depth1``: N-1's own depth-1 install, updated to HEAD and then to one more release. The first
  update must not download the whole project history to move one release (#123254: on a real
  network that transfer hits the 300 s fetch cap, so a stale shallow install can never update), and
  the second must succeed after the first one unshallowed the clone (#124272: the unshallow applied
  ``--filter=tree:0`` to a clone whose objects sit in a non-promisor pack, and the next fetch dies
  in ``pack-objects ... should_include_obj``).
* ``checked``: the same install after three passive ``hermes update --check`` runs while upstream
  moved (each is a depth-1 fetch that appends a graft), then updated to HEAD. The history is
  shared, so the update must not claim orphan divergence (#105951's symptom).
* ``prefetched``: a HEAD install (non-shallow ``tree:0``) with a local commit, where the user ran
  ``git fetch --depth 1 origin main`` (the documented workaround for slow fetches) before
  ``hermes update``. The local commit shares history with upstream: it must stay recoverable, and
  the update must not declare orphan divergence (#123346).
"""

from __future__ import annotations

import faulthandler
import os
import re
import time

import pytest

from tests.e2e.core._pending_fixes import known_failure
from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade.git import _git_world as G

ORPHAN = "orphan divergence"
HISTORICAL_FETCH_CAP = "historical pre-swap plain fetch exceeded the byte bound and hit its 300s cap"


def _has_prefetch_heal(source: str) -> bool:
    return "_heal_stale_shallow_checkout(_m().PROJECT_ROOT, branch)" in source


def _phase(name: str) -> None:
    print(f"shallow-install {time.monotonic():.1f}: {name}", flush=True)


def _first_n1_update(run: dict) -> None:
    """Record the pre-swap implementation and actual fetch, not just its exit."""
    w = run["w"]
    run["base"] = w.head()
    source = (w.checkout / "hermes_cli" / "update_cmd.py").read_text(encoding="utf-8")
    run["has_prefetch_heal"] = _has_prefetch_heal(source)
    run["mark1"] = w.srv.mark()
    _phase("N-1 update start")
    run["cp1"], run["head1"] = w.update(), w.head()
    _phase("N-1 update complete")
    run["shape1"] = w.shape()
    run["fetches1"] = [r for r in w.srv.since(run["mark1"]) if r.command == "fetch"]


def _assert_first_n1_update(run: dict) -> None:
    w, cp = run["w"], run["cp1"]
    out = G.output(cp)
    fetches = run["fetches1"]
    # #123254 already gates the byte overfetch below. On slower runners the
    # SAME old pre-swap transfer hits its 300s cap before reaching that assertion.
    # Gate only a proved historical, unfiltered overfetch with no checkout move;
    # current-code, transport, authentication and unmeasured timeouts stay red.
    # Strict acceptance (HERMES_E2E_STRICT_ACCEPTANCE) refuses the excuse.
    historical = (not run["has_prefetch_heal"] and cp.returncode == 1
                  and run["head1"] == run["base"]
                  and run["shape1"] == {"shallow": "true", "promisor": "-", "filter": "-"}
                  and "Failed to fetch updates from origin" in out
                  and "git fetch timed out after 300s" in out
                  and G.TRACEBACK not in out and G.SUCCESS not in out
                  and fetches and all(r.status == 200 and not r.fault for r in fetches)
                  and sum(r.body_bytes for r in fetches) > 2 * w.install_bytes)
    with known_failure(re.escape(HISTORICAL_FETCH_CAP),
                       "#123254: N-1's pre-swap plain fetch downloads ~the whole history and, on slower "
                       "runners, hits its 300s cap; this flips once a release carrying pre-fetch healing is N-1"):
        assert not historical, f"N-1 -> HEAD update failed: {HISTORICAL_FETCH_CAP}:\n{w.diag(cp)}"
    assert cp.returncode == 0 and run["head1"] == I.head_sha(), f"N-1 -> HEAD update failed:\n{w.diag(cp)}"


def _depth1(root) -> dict:
    _phase("depth1 install start")
    cm, w = G.start_world(root / "depth1", base=G.n1_base(), installer_ref=G.n1_tag())
    _phase("install complete")
    run = {"cm": cm, "w": w, "shape0": w.install_shape}
    w.set_main(I.head_sha())
    _first_n1_update(run)
    run["target2"] = w.publish("release: e2e shallow next", {"e2e-shallow-next.txt": "release\n"})
    run["mark2"] = w.srv.mark()
    _phase("depth1 second update start")
    run["cp2"], run["head2"] = w.update(), w.head()
    _phase("depth1 second update complete")
    run["version"] = w.version()
    return run


def _checked(root) -> dict:
    _phase("checked install start")
    cm, w = G.start_world(root / "checked", base=G.n1_base(), installer_ref=G.n1_tag())
    _phase("install complete")
    run = {"cm": cm, "w": w, "shape0": w.install_shape, "checks": []}
    between = I.git("rev-list", "--first-parent", f"{G.n1_base()}..{I.head_sha()}", cwd=I.H.WORKTREE).split()
    for k in (len(between) * 3 // 4, len(between) // 2, len(between) // 4):
        w.set_main(between[k])
        _phase("checked passive check start")
        run["checks"].append(w.sb.cli("update", "--check", timeout=600))
    shallow = w.checkout / ".git" / "shallow"
    run["grafts"] = len(shallow.read_text(encoding="utf-8").split()) if shallow.is_file() else 0
    w.set_main(I.head_sha())
    _first_n1_update(run)
    return run


def _prefetched(root) -> dict:
    _phase("prefetched install start")
    cm, w = G.start_world(root / "prefetched", base=I.head_sha())
    _phase("install complete")
    run = {"cm": cm, "w": w}
    (w.checkout / "e2e-local-before-prefetch.txt").write_text("my local commit\n", encoding="utf-8")
    w.git("add", "-A")
    w.git("commit", "-q", "-m", "my local patch")
    run["local"] = w.head()
    run["target"] = w.publish("release: e2e shallow prefetch", {"e2e-shallow-prefetch.txt": "release\n"})
    w.git("fetch", "-q", "--depth", "1", "origin", "main")
    run["prefetch_shape"] = w.shape()
    run["cp1"], run["head1"] = w.update(), w.head()
    return run


@pytest.fixture(scope="module")
def run(request, tmp_path_factory):
    """One established world per runner-managed file, shared by that file's tests."""
    root = tmp_path_factory.mktemp("git-shallow")
    builders = {"depth1": _depth1, "checked": _checked, "prefetched": _prefetched}
    # Keep diagnostics on the runner's pipe, not pytest's capture file lost on SIGKILL.
    capture = request.config.pluginmanager.getplugin("capturemanager")
    result = None
    with capture.global_and_fixture_disabled():
        diagnostics = os.fdopen(os.dup(2), "w")
    try:
        faulthandler.dump_traceback_later(840, file=diagnostics)
        with capture.global_and_fixture_disabled():
            result = builders[request.module.WORLD](root)
        yield result
    finally:
        faulthandler.cancel_dump_traceback_later()
        diagnostics.close()
        if result is not None:
            result["cm"].__exit__(None, None, None)

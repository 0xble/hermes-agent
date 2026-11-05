"""Partial and full clones updated N-1 -> HEAD -> next release through the real ``hermes update``.

Users' checkouts come in several shapes: HEAD's installer makes a ``--filter=blob:none`` clone,
installers from late Sep 2026 made ``--filter=tree:0`` ones, and plenty of users cloned the repository
themselves (a full clone). Each shape is seeded as a real smart-HTTP clone of the fork's promoted
N-1 after its genuine ``scripts/install.sh`` provisions the runtime: modern bootstrap otherwise
converts old Git shapes before the updater can see them. Only .git is replaced at the same SHA.
Then update twice: to HEAD (N-1's updater hands off to HEAD's),
and to one more upstream release (HEAD's updater end to end). The three installs run concurrently.

Property: both updates land and the install runs, and the clone keeps its shape: a partial clone
keeps its filter, and a full clone stays full. The one conversion is treeless -> blobless (#129712):
a treeless checkout re-downloads whole directory snapshots on every checkout. Converting a full clone to ``tree:0`` re-arms the
``pack-objects ... should_include_obj`` fetch crash for every later update (#124272, #124323).
"""

from __future__ import annotations

import os

import pytest

from tests.e2e.core.upgrade import _install_helpers as I
from tests.e2e.core.upgrade._install_cache import InstallCache
from tests.e2e.core.upgrade.git import _git_world as G

pytestmark = G.PYTESTMARK

SHAPES = {
    "tree0": ["--filter=tree:0"],
    "blobless": ["--filter=blob:none"],
    "full": [],
}


def _scenario(root, shape: str, cache: InstallCache) -> dict:
    cm, w = G.start_world(root / shape, base=G.n1_base(), installer_ref=G.n1_tag(), cache=cache)
    w.seed_clone_shape(SHAPES[shape])
    run = {"cm": cm, "w": w, "shape0": w.install_shape}
    w.set_main(I.head_sha())
    run["cp1"], run["head1"], run["shape1"] = w.update(), w.head(), w.shape()
    run["target2"] = w.publish(f"release: e2e partial {shape}", {f"e2e-partial-{shape}.txt": "release\n"})
    run["cp2"], run["head2"], run["shape2"] = w.update(), w.head(), w.shape()
    run["version"] = w.version()
    return run


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("git-partial")
    cache = InstallCache(root / "downloads", os.environ)
    results = G.in_parallel({shape: (lambda s=shape: _scenario(root, s, cache)) for shape in SHAPES})
    yield results
    for res in results.values():
        if isinstance(res, dict):
            res["cm"].__exit__(None, None, None)


@pytest.mark.parametrize("shape", list(SHAPES))
def test_updates_land_and_the_clone_keeps_its_shape(runs, shape):
    run = runs[shape]
    if isinstance(run, BaseException):
        raise run
    w: G.World = run["w"]
    cp1, cp2 = run["cp1"], run["cp2"]
    seeded_filter = {"tree0": "tree:0", "blobless": "blob:none", "full": "-"}[shape]
    assert run["shape0"] == {
        "promisor": "-" if shape == "full" else "true", "filter": seeded_filter, "shallow": "false",
    }, f"bootstrap silently changed the retained {shape} fixture: {run['shape0']}"
    expected = {**run["shape0"], "filter": "blob:none"} if shape == "tree0" else run["shape0"]
    assert run["shape0"]["shallow"] == "false" and (shape == "full") == (run["shape0"]["filter"] == "-"), \
        f"seeded the wrong clone shape for {shape}: {run['shape0']}"
    assert cp1.returncode == 0 and run["head1"] == I.head_sha(), f"N-1 -> HEAD update failed:\n{w.diag(cp1)}"
    assert G.TRACEBACK not in G.output(cp1), f"N-1 -> HEAD update printed a traceback:\n{w.diag(cp1)}"
    assert run["shape1"] == expected, (
        f"{'the full clone was converted to a partial clone' if shape == 'full' else 'the clone changed shape'} "
        f"by the N-1 -> HEAD update: {run['shape0']} -> {run['shape1']} (expected {expected})\n{w.diag(cp1)}")
    assert cp2.returncode == 0 and run["head2"] == run["target2"], f"HEAD -> next update failed:\n{w.diag(cp2)}"
    assert run["shape2"] == expected, f"the clone changed shape: {expected} -> {run['shape2']}\n{w.diag(cp2)}"
    version = run["version"]
    assert version.returncode == 0 and G.TRACEBACK not in G.output(version), w.diag(version)

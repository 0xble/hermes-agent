"""Real shallow-install depth1 world; its own unchanged 900 s file budget."""
from tests.e2e.core._pending_fixes import known_failure
from tests.e2e.core.upgrade.git import _git_world as G
from tests.e2e.core.upgrade.git._shallow_install import _assert_first_n1_update, run

pytestmark = G.PYTESTMARK
WORLD = "depth1"


def test_stale_depth1_install_updates_without_fetching_the_whole_history(run):
    w: G.World = run["w"]
    cp1 = run["cp1"]
    assert run["shape0"]["shallow"] == "true", f"N-1's installer did not make a shallow clone: {run['shape0']}"
    _assert_first_n1_update(run)
    main_fetch = run["fetches1"][0].body_bytes if run["fetches1"] else 0
    bound = 2 * w.install_bytes
    with known_failure(r"downloaded .* the depth-1 install itself",
                       "#123254: on a depth-1 install N-1's plain `git fetch origin main` downloads ~the whole "
                       "history. HEAD unshallows (commits only) before that fetch; the fetch runs pre-swap, so "
                       "this flips once a release carrying the fix is N-1"):
        assert main_fetch <= bound, (
            f"moving one release, the update's main fetch downloaded {main_fetch} bytes, more than twice the "
            f"{w.install_bytes} bytes of the depth-1 install itself:\n{w.diag(cp1, run['mark1'])}")


def test_second_update_after_the_unshallowing_update_succeeds(run):
    w: G.World = run["w"]
    cp1, cp2 = run["cp1"], run["cp2"]
    _assert_first_n1_update(run)
    assert cp2.returncode == 0 and run["head2"] == run["target2"], (
        f"the update after the unshallowing update failed (rc={cp2.returncode}):\n"
        f"{G.output(cp2)[-1500:]}\n{w.diag(cp2, run['mark2'])}")
    version = run["version"]
    assert version.returncode == 0 and G.TRACEBACK not in G.output(version), w.diag(version)

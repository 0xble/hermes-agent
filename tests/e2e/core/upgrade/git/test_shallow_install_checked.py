"""Real shallow-install checked world; its own unchanged 900 s file budget."""
from tests.e2e.core._pending_fixes import known_failure
from tests.e2e.core.upgrade.git import _git_world as G
from tests.e2e.core.upgrade.git._shallow_install import ORPHAN, _assert_first_n1_update, run

pytestmark = G.PYTESTMARK
WORLD = "checked"


def test_passive_checks_do_not_push_the_next_update_into_orphan_divergence(run):
    w: G.World = run["w"]
    cp1 = run["cp1"]
    for cp in run["checks"]:
        assert cp.returncode == 0 and G.TRACEBACK not in G.output(cp), f"`hermes update --check` failed:\n{w.diag(cp)}"
    _assert_first_n1_update(run)
    with known_failure(r"claims orphan divergence",
                       "#124645: each depth-1 `--check` appends a graft the fetch reflog pins, and N-1's "
                       "update resets as orphan divergence. HEAD's prune expires those reflogs and HEAD "
                       "unshallows before the pull; it is N-1's own check and pull code, so this flips once "
                       "a release carrying the fix is N-1"):
        assert ORPHAN not in G.output(cp1) and not w.refs("refs/hermes-update-backups/orphan-*"), (
            f"after {len(run['checks'])} passive checks ({run['grafts']} grafts in .git/shallow) the update "
            f"claims orphan divergence on a history it shares with upstream:\n{w.diag(cp1)}")

"""Real shallow-install prefetched world; its own unchanged 900 s file budget."""
from tests.e2e.core._pending_fixes import known_failure
from tests.e2e.core.upgrade.git import _git_world as G
from tests.e2e.core.upgrade.git._shallow_install import ORPHAN, run

pytestmark = G.PYTESTMARK
WORLD = "prefetched"


def test_depth1_prefetch_never_turns_shared_history_into_orphan_divergence(run):
    w: G.World = run["w"]
    cp1 = run["cp1"]
    assert run["prefetch_shape"]["shallow"] == "true", f"the --depth 1 pre-fetch did not graft: {run['prefetch_shape']}"
    assert cp1.returncode == 0 and run["head1"] == run["target"], f"update failed:\n{w.diag(cp1)}"
    keepers = w.refs_containing(run["local"])
    assert keepers, f"local commit {run['local'][:12]} is reachable only from the reflog:\n{w.diag(cp1)}"
    assert ORPHAN not in G.output(cp1) and not w.refs("refs/hermes-update-backups/orphan-*"), (
        f"after a --depth 1 pre-fetch the update claims orphan divergence and force-resets main "
        f"(local commit {run['local'][:12]} shares history with upstream):\n{w.diag(cp1)}")
"""Hostile post: sticky profile receipts and logs.

See ``_hostile_post`` for the shared harness and full cell contract.
The module-scoped real install is shared only within this bounded file.
"""
from __future__ import annotations

from tests.e2e.core.upgrade.git import _hostile_post as C
from tests.e2e.core.upgrade.git._hostile_post import (
    G,
    H,
    PROFILE,
    _clear_toggles,
    _facts,
    _update_log,
    json,
    w as w,
)
from tests.e2e.core.upgrade.git._hostile_diagnostics import (
    hostile_cell as hostile_cell,
    hostile_diagnostics as hostile_diagnostics,
)

pytestmark = C.pytestmark


def test_sticky_profile_update_logs_and_receipts_land_in_the_root_home(w):
    w.reset_clean()
    _clear_toggles(w)
    root = w.sb.hermes_home
    prof_receipts = root / "profiles" / PROFILE / "logs" / "update_receipts"
    if not (root / "profiles" / PROFILE).is_dir():
        cp = w.sb.cli("profile", "create", PROFILE, "--no-alias", timeout=300)
        assert cp.returncode == 0, f"premise: profile create failed:\n{H.describe(cp)}"
    cp = w.sb.cli("profile", "use", PROFILE, timeout=300)
    assert cp.returncode == 0, f"premise: profile use failed:\n{H.describe(cp)}"
    try:
        assert (root / "active_profile").read_text(encoding="utf-8-sig").strip() == PROFILE, \
            "premise: the sticky profile was not recorded"
        prof_before = {p.name for p in prof_receipts.iterdir()} if prof_receipts.is_dir() else set()
        log0 = _update_log(w).stat().st_size if _update_log(w).is_file() else 0
        prev = w.receipt()
        target = w.publish("release: e2e hostile post profile d", {"docs/e2e-hostile-post-d.txt": "d\n"})

        cp = w.update("--no-gateway-restart")

        prof_after = {p.name for p in prof_receipts.iterdir()} if prof_receipts.is_dir() else set()
        log1 = _update_log(w).stat().st_size if _update_log(w).is_file() else 0
        rec = w.receipt()
        diag = _facts(w, cp, profile_receipts_new=sorted(prof_after - prof_before), root_update_log_bytes=f"{log0}->{log1}",
                      previous_update_id=prev.get("update_id")) + w.diag(cp)
        assert w.head() == target, f"premise: the release never committed:\n{diag}"
        assert cp.returncode == 0, f"the sticky-profile update failed:\n{diag}"
        # The update's receipts never follow the sticky profile: the root dir is what the Desktop
        # and the hand-off scripts read. pm's own sync receipts stay where pm writes them (the
        # active home; pm is not the updater's). pm's reader shows the newest receipt of any kind,
        # as when both shared one folder: the updater finalizes after pm's sync, so that is this
        # run's update receipt, mirrored into the profile store pm reads.
        assert not [n for n in prof_after - prof_before if n.startswith("update_")], \
            f"the update wrote update receipts into the profile home {prof_receipts}:\n{diag}"
        assert [n for n in prof_after - prof_before if n.startswith("pm_") and "-sync-" in n], \
            f"pm's sync receipt for this update is missing from the profile home {prof_receipts}:\n{diag}"
        pm_cp = w.sb.cli("pm", "status", timeout=300)
        assert pm_cp.returncode == 0 and json.loads(pm_cp.stdout).get("update_id") == rec.get("update_id"), \
            f"`hermes pm status` under the profile does not show this run's newest receipt:\n{H.describe(pm_cp)}\n{diag}"
        assert log1 > log0, f"the root update.log did not grow:\n{diag}"
        assert rec.get("update_id") != prev.get("update_id") and rec.get("outcome") == "success", \
            f"root latest.json is not this run's success:\n{diag}"
        # The readers follow the writers: `hermes logs update` under the profile shows the root log.
        logs_cp = w.sb.cli("logs", "update", "-n", "400", timeout=300)
        assert logs_cp.returncode == 0 and "hermes update started" in G.output(logs_cp), \
            f"`hermes logs update` under the sticky profile does not show the root update.log:\n{H.describe(logs_cp)}\n{diag}"
    finally:
        w.sb.cli("profile", "use", "default", timeout=300)

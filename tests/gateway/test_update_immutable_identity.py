"""Immutable homes judge an update by the active release, never the frozen source HEAD.

The updater runs from the journal-bound source checkout, whose HEAD is frozen by design,
so the receipt's ``post_update`` names that checkout. A verified gateway serving the
active release must read as success; a real receipt/pointer/runtime mismatch must fail;
legacy homes without a release pointer keep the checkout-HEAD contract.
"""
import json

import pytest

from gateway.update_notifications import expected_revision, final_outcome
from tests.gateway.update_fixtures import finalize_update

SOURCE = "a" * 40  # frozen source-checkout HEAD recorded as pre/post_update
RELEASE = "c" * 40  # active immutable release


def _release(home, sha=RELEASE):
    path = home / "releases" / sha
    path.mkdir(parents=True)
    for name in (".release-ready", ".hermes_build_sha"):
        (path / name).write_text(sha)
    (home / "current").symlink_to(path)
    return path


def _pending():
    from datetime import datetime, timezone
    return {"platform": "telegram", "chat_id": "42", "notification_version": 2,
            "timestamp": datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()}


def _receipt(home, *, fleet_sha, transition=None, restart=True):
    finalize_update(home)
    path = home / "logs" / "update_receipts" / "latest.json"
    receipt = json.loads(path.read_text())
    receipt["pre_update"]["sha"] = SOURCE  # a no-op run: the source checkout never moves
    receipt["post_update"]["sha"] = SOURCE
    receipt["fleet"][0]["code_sha"] = fleet_sha
    if not restart:
        receipt["gateway_restart"] = {}
        receipt["fleet"] = []
    if transition:
        receipt["release_transition"] = {"to_sha": transition}
    path.write_text(json.dumps(receipt))
    return receipt


def test_verified_release_gateway_is_success_despite_frozen_source_head(tmp_path):
    _release(tmp_path)
    _receipt(tmp_path, fleet_sha=RELEASE)
    ok, detail = final_outcome(tmp_path, _pending())
    assert ok is True and RELEASE[:12] in detail


def test_already_latest_immutable_noop_names_the_release(tmp_path):
    _release(tmp_path)
    _receipt(tmp_path, fleet_sha=RELEASE, restart=False)
    assert final_outcome(tmp_path, _pending()) == (True, f"Hermes is already at revision {RELEASE[:12]}.")


@pytest.mark.parametrize("case", ["gateway_on_other_code", "transition_contradicts_pointer"])
def test_real_immutable_mismatch_still_fails(tmp_path, case):
    _release(tmp_path)
    if case == "gateway_on_other_code":
        _receipt(tmp_path, fleet_sha=SOURCE)
    else:
        _receipt(tmp_path, fleet_sha=RELEASE, transition="d" * 40)
    ok, detail = final_outcome(tmp_path, _pending())
    assert ok is False
    if case == "transition_contradicts_pointer":
        assert "active release" in detail


def test_legacy_home_keeps_checkout_head_contract(tmp_path):
    receipt = _receipt(tmp_path, fleet_sha=SOURCE)
    assert expected_revision(tmp_path, receipt) == (SOURCE, None)
    assert final_outcome(tmp_path, _pending())[0] is True
    _receipt(tmp_path, fleet_sha=RELEASE)
    assert final_outcome(tmp_path, _pending())[0] is False


def test_unready_release_pointer_falls_back_to_legacy(tmp_path):
    path = _release(tmp_path)
    (path / ".release-ready").unlink()
    receipt = _receipt(tmp_path, fleet_sha=SOURCE)
    assert expected_revision(tmp_path, receipt) == (SOURCE, None)


def test_result_heading_names_the_active_release(tmp_path):
    from gateway.run_notifications import GatewayNotificationsMixin as Mixin
    _release(tmp_path)
    _receipt(tmp_path, fleet_sha=RELEASE)
    heading, detail = Mixin._update_result_heading(tmp_path, _pending(), 0)
    assert heading == "✅ Update Complete"
    assert RELEASE[:12] in detail and SOURCE[:12] not in detail


@pytest.mark.parametrize("restart", [True, False])
def test_result_heading_fails_on_transition_pointer_mismatch(tmp_path, restart):
    """The pointer can move after final_outcome; the heading must not claim success or no-op."""
    from gateway.run_notifications import GatewayNotificationsMixin as Mixin
    _release(tmp_path)
    _receipt(tmp_path, fleet_sha=RELEASE, transition="d" * 40, restart=restart)
    heading, detail = Mixin._update_result_heading(tmp_path, _pending(), 0)
    assert heading == "❌ Update Failed"
    assert "active release" in detail

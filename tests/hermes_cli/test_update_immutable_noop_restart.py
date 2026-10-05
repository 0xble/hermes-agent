"""An already-up-to-date immutable update must not restart a gateway already on the release.

The updater runs from the frozen source checkout. Judging the fleet against that HEAD marked a
gateway serving the active release as stale, so every no-op ``hermes update`` restarted it.
"""
import json

from hermes_cli import update_cmd_fleet
from hermes_constants import get_hermes_home

SOURCE = "a" * 40
RELEASE = "c" * 40


def _release(home, sha=RELEASE):
    path = home / "releases" / sha
    path.mkdir(parents=True)
    for name in (".release-ready", ".hermes_build_sha"):
        (path / name).write_text(sha)
    (home / "current").symlink_to(path)


def _latest_receipt(home, fleet_sha):
    directory = home / "logs" / "update_receipts"
    directory.mkdir(parents=True)
    (directory / "latest.json").write_text(json.dumps({
        "outcome": "success", "exit_code": 0,
        "post_update": {"sha": SOURCE},
        "fleet": [{"profile": "default", "pid": 9, "code_sha": fleet_sha, "state": "current"}],
    }))


def _frozen_source_head(monkeypatch):
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity",
                        lambda refresh=False: {"sha": SOURCE, "source": "git"})


def test_expected_sha_is_the_active_release(monkeypatch):
    _frozen_source_head(monkeypatch)
    _release(get_hermes_home())
    assert update_cmd_fleet._current_checkout_sha() == RELEASE


def test_legacy_home_keeps_checkout_head(monkeypatch):
    _frozen_source_head(monkeypatch)
    assert update_cmd_fleet._current_checkout_sha() == SOURCE


def test_immutable_noop_with_current_release_gateway_owes_no_restart(monkeypatch):
    _frozen_source_head(monkeypatch)
    home = get_hermes_home()
    _release(home)
    _latest_receipt(home, RELEASE)
    assert update_cmd_fleet._receipt_reports_stale_runtime(
        json.loads((home / "logs" / "update_receipts" / "latest.json").read_text())) is False
    assert update_cmd_fleet._pending_fleet_restart_needed() is False


def test_immutable_gateway_off_the_release_still_owes_restart(monkeypatch):
    _frozen_source_head(monkeypatch)
    home = get_hermes_home()
    _release(home)
    _latest_receipt(home, "b" * 40)
    receipt = json.loads((home / "logs" / "update_receipts" / "latest.json").read_text())
    assert update_cmd_fleet._receipt_reports_stale_runtime(receipt) is True

"""The candidate-owned post-swap updater must settle maintenance before release activation.

Exercise the real handoff reader and receipt writer with a disposable home. The
candidate identity is simulated by rebinding PROJECT_ROOT; maintenance and
activation are bounded so no live service or release is changed.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import main, update_cmd, update_cmd_maint, update_receipt
from hermes_cli.immutable_releases import ReleasePaths
from hermes_cli.immutable_update_handoff import detach_update_receipt


@pytest.fixture
def post_swap_candidate(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    paths = ReleasePaths.for_home(home)
    previous = paths.releases / ("a" * 40)
    candidate = paths.releases / ("b" * 40)
    previous.mkdir(parents=True)
    candidate.mkdir()
    paths.current.symlink_to(previous)
    monkeypatch.setattr(main, "PROJECT_ROOT", candidate)
    monkeypatch.setattr(update_cmd, "_resolve_update_options", lambda args, gateway_mode: update_cmd._UpdateOptions(
        pre_update_version="new",
        gw_input_fn=None, assume_yes=True, keep_stash=False, switch_branch=False,
        discard_local_changes=False, no_gateway_restart=True,
    ))
    update_receipt.begin_update_receipt()
    update_receipt.record_step("pre_update_backup", True, "disposable snapshot")
    payload = {
        "swap": "immutable", "release": str(candidate), "candidate_sha": candidate.name,
        "source": str(tmp_path / "source"), "source_python": str(tmp_path / "source" / ".venv" / "bin" / "python"),
        "pre_update_version": "old", "pre_update_snapshot_id": "snapshot-1",
        "receipt": detach_update_receipt(),
    }
    handoff = tmp_path / "post_swap.json"
    handoff.write_text(json.dumps(payload), encoding="utf-8")
    yield home, paths, previous, candidate, handoff
    update_receipt._current.set(None)


def _latest(home):
    receipts = list((home / "logs" / "update_receipts").glob("update_*.json"))
    assert len(receipts) == 1
    persisted = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert persisted == update_receipt.read_latest_receipt()
    assert persisted["steps"][0]["name"] == "pre_update_backup"
    assert not update_receipt.has_active_update_receipt()
    return persisted


def test_immutable_post_swap_maintains_before_activation_and_receipts_success(post_swap_candidate, monkeypatch):
    home, paths, previous, candidate, handoff = post_swap_candidate
    events = []

    def strict(release):
        assert release == candidate
        assert paths.current.resolve() == previous
        events.append("strict")

    def post_update(**kwargs):
        assert kwargs["pre_update_snapshot_id"] == "snapshot-1"
        assert kwargs["pre_update_version"] == "old"
        events.append("post_update")
        return True

    def activate(**kwargs):
        assert kwargs["sha"] == candidate.name and kwargs["defer"] is True
        assert paths.current.resolve() == previous
        events.append("activate")
        return True

    monkeypatch.setattr(update_cmd_maint, "strict_immutable_maintenance", strict)
    monkeypatch.setattr(update_cmd, "_run_post_update_maintenance", post_update)
    monkeypatch.setattr(update_cmd, "_activate_immutable_release", activate)
    monkeypatch.setattr(update_cmd, "_restart_gateway_fleet_after_update", lambda *a, **kw: pytest.fail("restart forbidden"))

    update_cmd._run_post_swap_phase(SimpleNamespace(post_swap=str(handoff)), gateway_mode=False)

    assert events == ["strict", "post_update", "activate"]
    assert not handoff.exists()
    receipt = _latest(home)
    assert receipt["outcome"] == "success"
    steps = {step["name"]: step for step in receipt["steps"]}
    assert steps["immutable_maintenance"]["ok"] is True
    assert "immutable_activation" not in steps
    assert any(item["name"] == "immutable_activation" and "deferred" in item["reason"]
               for item in receipt["skips"])


@pytest.mark.parametrize("failure", ["raised", "incomplete"])
def test_immutable_post_swap_failure_is_partial_and_never_activates(post_swap_candidate, monkeypatch, failure):
    home, paths, previous, candidate, handoff = post_swap_candidate
    events = []

    def strict(release):
        assert release == candidate
        events.append("strict")
        if failure == "raised":
            raise RuntimeError("forced candidate maintenance failure")

    def post_update(**kwargs):
        events.append("post_update")
        return False

    monkeypatch.setattr(update_cmd_maint, "strict_immutable_maintenance", strict)
    monkeypatch.setattr(update_cmd, "_run_post_update_maintenance", post_update)
    monkeypatch.setattr(update_cmd, "_activate_immutable_release", lambda **kw: pytest.fail("failed maintenance promoted release"))
    monkeypatch.setattr(update_cmd, "_restart_gateway_fleet_after_update", lambda *a, **kw: pytest.fail("failed maintenance restarted fleet"))

    with pytest.raises(SystemExit) as exited:
        update_cmd._run_post_swap_phase(SimpleNamespace(post_swap=str(handoff)), gateway_mode=False)

    assert exited.value.code == 1
    assert events == (["strict"] if failure == "raised" else ["strict", "post_update"])
    assert paths.current.resolve() == previous
    assert not handoff.exists()
    receipt = _latest(home)
    assert receipt["outcome"] == "partial"
    step = next(step for step in receipt["steps"] if step["name"] == "immutable_maintenance")
    assert step["ok"] is False
    assert ("forced candidate maintenance failure" if failure == "raised" else "incomplete") in step["detail"]
    message = next(step["detail"] for step in receipt["steps"] if step["name"] == "immutable_maintenance")
    assert candidate.name in message
    assert str(paths.home / "release-txn.json") in message
    assert "hermes update" in message

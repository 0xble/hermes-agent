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
        active_lazy_features=None, active_tool_dependencies=None, pre_update_version="new",
        gw_input_fn=None, assume_yes=True, keep_stash=False, switch_branch=False,
        discard_local_changes=False, no_gateway_restart=True,
    ))
    update_receipt.begin_update_receipt()
    update_receipt.record_step("pre_update_backup", True, "disposable snapshot")
    payload = {
        "swap": "immutable", "release": str(candidate), "candidate_sha": candidate.name,
        "source": str(tmp_path / "source"), "source_python": str(tmp_path / "source" / ".venv" / "bin" / "python"),
        "pre_update_version": "old", "pre_update_snapshot_id": "snapshot-1",
        "receipt": update_receipt.detach_update_receipt(),
    }
    handoff = tmp_path / "post_swap.json"
    handoff.write_text(json.dumps(payload), encoding="utf-8")
    yield home, paths, previous, candidate, handoff
    update_receipt._current = None


def _latest(home):
    receipts = list((home / "logs" / "update_receipts").glob("update_*.json"))
    assert len(receipts) == 1
    persisted = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert persisted == update_receipt.read_latest_receipt()
    assert persisted["steps"][0]["name"] == "pre_update_backup"
    assert not update_receipt._current
    return persisted


@pytest.mark.parametrize("proof,expected", [
    ({"outcome": "rolled_back"}, "rolled_back"),
    ({"outcome": "blocked"}, "blocked"),
    ({"outcome": "success"}, "partial"),
    (None, "partial"),
])
def test_overlap_failure_receipt_outcome_is_typed_without_claiming_success(monkeypatch, proof, expected):
    from hermes_cli import update_cmd, update_receipt
    current = type("Receipt", (), {"data": {"overlap_generation": proof}})()
    monkeypatch.setattr(update_receipt, "_current", current)
    assert update_cmd._overlap_failure_outcome() == expected


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
        assert kwargs["node_failures"] == [] and kwargs["desktop_build_ok"] is True
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


@pytest.mark.parametrize("proof_valid", [False, True])
def test_overlap_post_swap_exit_requires_live_admission_readback(post_swap_candidate, monkeypatch, proof_valid):
    """Exercise the same post-swap entry point as hermes update, not a receipt stub."""
    from dataclasses import replace
    from hermes_cli import gateway_overlap
    home, _paths, _previous, candidate, handoff = post_swap_candidate
    resolve = update_cmd._resolve_update_options
    monkeypatch.setattr(update_cmd, "_resolve_update_options", lambda *args:
                        replace(resolve(*args), no_gateway_restart=False))
    monkeypatch.setattr(update_cmd_maint, "strict_immutable_maintenance", lambda *_: None)
    monkeypatch.setattr(update_cmd, "_run_post_update_maintenance", lambda **_: True)
    def promote(**_kwargs):
        update_receipt.record_overlap_generation({
            "old_id": "a", "new_id": "b", "old_sha": "a" * 40,
            "new_sha": candidate.name, "epoch": 2,
            "previous": str(home / "releases" / ("a" * 40)), "current": str(candidate),
            "admission": {"source_event_id": "42", "message_id": "42"},
            "poller": {"polling": True, "tokens": ["token"]},
        })
        return True
    monkeypatch.setattr(update_cmd, "_activate_immutable_release", promote)
    monkeypatch.setattr(gateway_overlap, "verified_overlap", lambda *_: proof_valid)
    monkeypatch.setattr(update_cmd, "_restart_gateway_fleet_after_update",
                        lambda *_a, **_k: pytest.fail("legacy fleet bootout would kill A"))
    exits = []
    monkeypatch.setattr(update_cmd, "_write_gateway_update_exit_code", exits.append)
    if proof_valid:
        update_cmd._run_post_swap_phase(SimpleNamespace(post_swap=str(handoff)), gateway_mode=False)
    else:
        with pytest.raises(SystemExit) as failed:
            update_cmd._run_post_swap_phase(SimpleNamespace(post_swap=str(handoff)), gateway_mode=False)
        assert failed.value.code == 1
    receipt = _latest(home)
    assert receipt["outcome"] == ("success" if proof_valid else "blocked")
    assert exits == [proof_valid]
    assert receipt["overlap_generation"].get("outcome") == ("blocked" if not proof_valid else None)


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

"""Failed updates explain the cause without claiming unproved runtime changes."""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.update_notifications import _failure_cause, final_outcome
from tests.gateway.update_fixtures import RELEASE, SOURCE, _make_runner, _release, finalize_update, pending


@pytest.fixture(autouse=True)
def notification_checkout(tmp_path, monkeypatch):
    """Keep recovery-state probes in a synthetic linked checkout, not the running tree."""
    root = tmp_path / "source"
    root.mkdir()
    git_dir = tmp_path / "git" / "worktrees" / "source"
    git_dir.mkdir(parents=True)
    (root / ".git").write_text(f"gitdir: {git_dir}\n")
    monkeypatch.setattr("gateway.update_notifications.__file__", str(root / "gateway" / "update_notifications.py"))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "host-state"))
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize("immutable", [False, True])
@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("stop_reason", [
    "\n  candidate plugin smoke failed on plugin rea.  \nTraceback (most recent call last):\n  File \"probe.py\", line 1\nRuntimeError: smoke failed",
    "x" * 1000,
    "Traceback (most recent call last):\n  File \"probe.py\", line 1\nRuntimeError: smoke failed",
    None,
    "sys.exit(1)",
])
async def test_failed_update_delivers_unchanged_version_and_one_line_cause(
    tmp_path, immutable, exit_code, stop_reason,
):
    data = pending(tmp_path, reason=False)
    data["updating_notified"] = True
    (tmp_path / ".update_pending.json").write_text(json.dumps(data))
    finalize_update(tmp_path, outcome="failed", noop=True)
    path = tmp_path / "logs" / "update_receipts" / "latest.json"
    receipt = json.loads(path.read_text())
    receipt.update(stop_reason=stop_reason, stop_class="syntax_rollback",
                   pre_update={"sha": SOURCE}, post_update={"sha": SOURCE})
    path.write_text(json.dumps(receipt))
    (tmp_path / ".update_process_exit_code").write_text(str(exit_code))
    if immutable:
        _release(tmp_path)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}

    with patch("gateway.run._hermes_home", tmp_path):
        assert await runner._send_update_notification()

    text = adapter.send.call_args.args[1]
    assert text.startswith("❌ Update Failed\n\nNothing changed. Still on ")
    assert (RELEASE if immutable else SOURCE)[:12] in text
    assert "Cause:" in text and len(text.splitlines()[-1]) < 400
    assert "Traceback" not in text and "RuntimeError" not in text
    assert "unverified" not in text
    if stop_reason and "candidate plugin" in stop_reason:
        assert "Cause: candidate plugin smoke failed on plugin rea." in text
    if stop_reason == "sys.exit(1)":
        assert "Cause: The downloaded update contains a syntax error." in text
        assert "sys.exit" not in text
    assert not (tmp_path / ".update_pending.json").exists()


@pytest.mark.parametrize("case", [
    "missing", "malformed", "stale", "bad_timestamp", "unfinished", "changed", "missing_sha", "invalid_sha",
    "restart", "fleet", "transition", "release_pending", "fleet_pending", "success_receipt", "broken_release",
    "missing_working_tree_fact",
])
def test_failed_exit_never_claims_unchanged_without_current_receipt_proof(tmp_path, case):
    data = pending(tmp_path, reason=False)
    finalize_update(tmp_path, outcome="failed", noop=True)
    (tmp_path / ".update_process_exit_code").write_text("7")
    path = tmp_path / "logs" / "update_receipts" / "latest.json"
    receipt = json.loads(path.read_text())
    receipt["stop_reason"] = "candidate plugin smoke failed on plugin rea."
    if case == "missing":
        path.unlink()
    elif case == "malformed":
        path.write_text("{")
    else:
        if case == "missing_working_tree_fact":
            receipt["steps"] = []
        elif case == "stale":
            receipt["started_at"] = receipt["finished_at"] = (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).isoformat()
        elif case == "bad_timestamp":
            receipt["finished_at"] = "not a timestamp"
        elif case == "unfinished":
            receipt.pop("finished_at")
        elif case == "changed":
            receipt["post_update"]["sha"] = "b" * 40
        elif case == "missing_sha":
            receipt["post_update"] = {}
        elif case == "invalid_sha":
            receipt["pre_update"] = receipt["post_update"] = {"sha": "not-a-sha"}
        elif case == "broken_release":
            (tmp_path / "current").symlink_to(tmp_path / "missing-release")
        elif case == "restart":
            receipt["gateway_restart"] = {"incomplete": True}
        elif case == "fleet":
            receipt["fleet"] = [{"state": "restart_pending"}]
        elif case == "transition":
            _release(tmp_path)
            receipt["release_transition"] = {"from_sha": SOURCE, "to_sha": RELEASE}
        elif case == "release_pending":
            (tmp_path / "release-txn.json").write_text("{}")
        elif case == "fleet_pending":
            (tmp_path / "fleet_restart_pending").write_text("pending")
        elif case == "success_receipt":
            receipt["outcome"] = "success"
        path.write_text(json.dumps(receipt))

    ok, detail = final_outcome(tmp_path, data)
    assert ok is False
    assert "Runtime state is unverified" in detail
    assert "Nothing changed" not in detail and "Still on" not in detail
    if case in {"changed", "missing_sha", "restart", "fleet", "transition", "release_pending", "fleet_pending"}:
        assert "Cause: candidate plugin smoke failed on plugin rea." in detail


@pytest.mark.parametrize("stop_class, expected", [
    ("syntax_rollback", "The downloaded update contains a syntax error."),
    ("target_syntax_error", "The selected update contains a syntax error."),
    ("future_stop_class", "future_stop_class"),
    (None, "The updater exited with code 1."),
])
@pytest.mark.parametrize("reason", ["sys.exit(1)", "\n  sys.exit(130)  \n", None])
def test_boundary_exit_reason_falls_back_to_stop_class(stop_class, expected, reason):
    assert _failure_cause({"stop_reason": reason, "stop_class": stop_class}, 1) == expected


def test_boundary_exit_reason_preserves_restart_explanation():
    receipt = {"stop_reason": "sys.exit(1)", "stop_class": "syntax_rollback",
               "gateway_restart": {"phase_error": "The gateway could not restart."}}
    assert _failure_cause(receipt, 1) == "The gateway could not restart."


@pytest.mark.parametrize("marker", ["interrupted_pull", "completion", "host", "legacy_host"])
def test_matching_head_does_not_prove_an_unchanged_checkout_with_recovery_owed(
    tmp_path, notification_checkout, marker,
):
    from hermes_cli._early_recovery import interrupted_pull_marker
    from hermes_cli.update_host_obligation import HOST_OBLIGATION_NAME, host_obligation_path
    from hermes_cli.venv_sync import completion_pending_path

    data = pending(tmp_path, reason=False)
    finalize_update(tmp_path, outcome="failed", noop=True)
    path = tmp_path / "logs" / "update_receipts" / "latest.json"
    receipt = json.loads(path.read_text())
    receipt.update(stop_reason="sys.exit(1)", stop_class="syntax_rollback")
    path.write_text(json.dumps(receipt))
    (tmp_path / ".update_process_exit_code").write_text("1")
    markers = {
        "interrupted_pull": interrupted_pull_marker(notification_checkout),
        "completion": completion_pending_path(notification_checkout),
        "host": host_obligation_path(notification_checkout),
        "legacy_host": host_obligation_path(notification_checkout).with_name(HOST_OBLIGATION_NAME),
    }
    owed = markers[marker]
    owed.parent.mkdir(parents=True, exist_ok=True)
    owed.write_text("unreadable recovery record")

    ok, detail = final_outcome(tmp_path, data)
    assert ok is False
    assert detail == ("Cause: The downloaded update contains a syntax error. "
                      "Runtime state is unverified; see the update output.")
    assert "Nothing changed" not in detail and "Still on" not in detail

    owed.unlink()
    assert final_outcome(tmp_path, data) == (
        False, "Nothing changed. Still on aaaaaaaaaaaa. Cause: The downloaded update contains a syntax error.",
    )


@pytest.mark.parametrize("marker", ["interrupted_pull", "completion", "host", "legacy_host"])
def test_immutable_notice_checks_recovery_in_journal_bound_source(
    tmp_path, notification_checkout, monkeypatch, marker,
):
    from hermes_cli import update_host_obligation as obligation
    from hermes_cli._early_recovery import interrupted_pull_marker
    from hermes_cli.venv_sync import completion_pending_path

    # Start fresh rather than reinitializing the autouse fixture's synthetic gitdir.
    # A real source identity keeps the immutable resolver's git validation in the test.
    source = tmp_path / "immutable-source"
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", str(source)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                    "commit", "--allow-empty", "-m", "source"],
                   check=True, capture_output=True)
    release = _release(tmp_path)
    (tmp_path / "release-layout.json").write_text(json.dumps({"source": str(source)}))
    monkeypatch.setattr("gateway.update_notifications.__file__", str(release / "gateway" / "update_notifications.py"))
    # The updater runs from the source, while the gateway imports its release copy.
    monkeypatch.setattr(obligation, "__file__", str(source / "hermes_cli" / "update_host_obligation.py"))
    data = pending(tmp_path, reason=False)
    finalize_update(tmp_path, outcome="failed", noop=True)
    (tmp_path / ".update_process_exit_code").write_text("1")
    markers = {
        "interrupted_pull": interrupted_pull_marker(source),
        "completion": completion_pending_path(source),
        "host": obligation.host_obligation_path(),
        "legacy_host": obligation.host_obligation_path().with_name(obligation.HOST_OBLIGATION_NAME),
    }
    owed = markers[marker]
    if marker == "host":
        assert obligation.write_host_obligation(expected_sha=RELEASE)
        assert owed.is_file()
        assert not (tmp_path / "fleet_restart_pending").exists()
        assert not markers["legacy_host"].exists()
    else:
        owed.parent.mkdir(parents=True, exist_ok=True)
        owed.write_text("unreadable recovery record")
    # Model module locations in the gateway process, after the source updater exits.
    monkeypatch.setattr(obligation, "__file__", str(release / "hermes_cli" / "update_host_obligation.py"))
    assert obligation.host_obligation_path() != markers["host"]

    ok, detail = final_outcome(tmp_path, data)
    assert ok is False
    assert "Runtime state is unverified" in detail
    assert "Nothing changed" not in detail and "Still on" not in detail

    owed.unlink()
    assert final_outcome(tmp_path, data) == (
        False, f"Nothing changed. Still on {RELEASE[:12]}. Cause: The updater exited with code 1.",
    )

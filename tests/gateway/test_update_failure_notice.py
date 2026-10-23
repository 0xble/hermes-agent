"""Failed updates explain the cause without claiming unproved runtime changes."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.update_notifications import final_outcome
from tests.gateway.test_update_command import _make_runner
from tests.gateway.test_update_immutable_identity import RELEASE, SOURCE, _release
from tests.gateway.test_update_lifecycle_notifications import pending
from tests.gateway.update_fixtures import finalize_update


@pytest.mark.asyncio
@pytest.mark.parametrize("immutable", [False, True])
@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("stop_reason", [
    "\n  candidate plugin smoke failed on plugin rea.  \nTraceback (most recent call last):\n  File \"probe.py\", line 1\nRuntimeError: smoke failed",
    "x" * 1000,
    "Traceback (most recent call last):\n  File \"probe.py\", line 1\nRuntimeError: smoke failed",
    None,
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
    receipt.update(stop_reason=stop_reason, pre_update={"sha": SOURCE}, post_update={"sha": SOURCE})
    path.write_text(json.dumps(receipt))
    (tmp_path / ".update_process_exit_code").write_text(str(exit_code))
    if immutable:
        _release(tmp_path)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner = _make_runner()
    runner.adapters = {Platform.TELEGRAM: adapter}

    with patch("gateway.run._hermes_home", tmp_path):
        assert await runner._send_update_notification()

    text = adapter.send.call_args.kwargs["content"]
    assert text.startswith("❌ Update Failed\n\nNothing changed. Still on ")
    assert (RELEASE if immutable else SOURCE)[:12] in text
    assert "Cause:" in text and len(text.splitlines()[-1]) < 400
    assert "Traceback" not in text and "RuntimeError" not in text
    assert "unverified" not in text
    if stop_reason and "candidate plugin" in stop_reason:
        assert "Cause: candidate plugin smoke failed on plugin rea." in text
    assert not (tmp_path / ".update_pending.json").exists()


@pytest.mark.parametrize("case", [
    "missing", "malformed", "stale", "bad_timestamp", "unfinished", "changed", "missing_sha", "invalid_sha",
    "restart", "fleet", "transition", "release_pending", "fleet_pending", "success_receipt", "broken_release",
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
        if case == "stale":
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

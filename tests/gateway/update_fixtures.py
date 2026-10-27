"""Native updater receipt fixtures built through its real persistence boundary."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.update_launcher import launch_native_update
from gateway.update_notifications import read_pending

SOURCE = "a" * 40  # frozen source-checkout HEAD recorded as pre/post_update
RELEASE = "c" * 40  # active immutable release


def _make_event(text="/update", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890", thread_id=None):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
        thread_id=thread_id,
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner without calling __init__."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._update_prompt_pending = {}
    return runner


def pending(home, *, reason=True):
    data = {"platform": "telegram", "chat_id": "42", "thread_id": "77", "chat_type": "dm",
            "session_key": "agent:main:telegram:dm:42:thread:77",
            "timestamp": datetime.now(UTC).isoformat()}
    if reason:
        data["reason"] = "Activating the delegation-label fix."
    launch_native_update(home=home, hermes_cmd=["hermes"], pending=data, spawn=Mock())
    return read_pending(home)[1]


def _release(home, sha=RELEASE):
    path = home / "releases" / sha
    path.mkdir(parents=True)
    for name in (".release-ready", ".hermes_build_sha"):
        (path / name).write_text(sha)
    (home / "current").symlink_to(path)
    return path


def immutable_source(home):
    """Create a real source commit for immutable release identity checks."""
    from tests.hermes_cli.test_update_target_identity import git

    source = home / "immutable-source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "source")
    return source


def fail_immutable_update(home, source=None):
    """Fail at the build boundary, retaining the real immutable apply's receipt facts."""
    from hermes_cli import main, update_cmd, update_receipt
    from tests.hermes_cli.test_update_target_identity import git

    if source is None:
        source = immutable_source(home)
    # Fetch a real descendant without moving HEAD or touching the source index.
    target = git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "downloaded update")
    git(source, "fetch", "--no-tags", ".", target)
    (home / "release-layout.json").write_text(json.dumps({"source": str(source)}))
    with patch.dict("os.environ", {"HERMES_HOME": str(home)}), \
         patch.object(main, "PROJECT_ROOT", source), \
         patch.object(update_receipt, "_receipt_dir", return_value=home / "logs" / "update_receipts"), \
         patch.object(update_receipt, "_code_identity", side_effect=lambda **_: {"sha": git(source, "rev-parse", "HEAD")}), \
         patch("hermes_cli.immutable_releases.stage_release", side_effect=RuntimeError("candidate build failed")):
        update_receipt.begin_update_receipt()
        try:
            with pytest.raises(RuntimeError, match="candidate build failed"):
                update_cmd._apply_fetched_immutable_update(
                    ["git"], "main", SimpleNamespace(no_gateway_restart=True), None,
                    gateway_mode=True, had_desktop_app_before_update=False,
                    pre_update_snapshot_id=None, _pre_update_plan=None, _windows_gateway_resume=None,
                )
        finally:
            path = update_receipt.finalize_update_receipt("failed", stop_reason="candidate build failed")
    (home / ".update_process_exit_code").write_text("1")
    assert path is not None
    return path


def finalize_update(home, *, outcome="success", fleet_state="current", noop=False):
    """``noop`` mirrors "Already up to date": same revision, no restart, empty fleet."""
    from hermes_cli import update_receipt
    sha = "a" * 40
    with patch.object(update_receipt, "_receipt_dir", return_value=home / "logs" / "update_receipts"), \
         patch.object(update_receipt, "_code_identity", return_value={"sha": sha}):
        update_receipt.begin_update_receipt()
        if not (home / "current").is_symlink():
            update_receipt.record_step("local_changes_stash", True, "not_needed: clean working tree, no autostash taken")
        if not noop:
            update_receipt.record_gateway_restart(restarted_services=["gateway"])
        path = update_receipt.finalize_update_receipt(outcome, fleet=[] if noop else [{
            "profile": "default", "pid": 1234, "state": fleet_state, "code_sha": sha,
        }])
    (home / ".update_process_exit_code").write_text("0")
    assert path is not None
    for receipt_path in (path, home / "logs" / "update_receipts" / "latest.json"):
        if receipt_path.exists():
            receipt_data = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_data.setdefault("pre_update", {})["sha"] = sha if noop else "b" * 40
            receipt_path.write_text(json.dumps(receipt_data), encoding="utf-8")
    return path

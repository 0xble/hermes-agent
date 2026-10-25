"""The update owner reads a requested session only in a currently served profile."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from gateway.run import GatewayRunner, _SESSION_DB_UNPINNED
from gateway.update_launcher import make_agent_update_handler
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override


@pytest.mark.parametrize("profile", ("other", "missing", "../other"))
def test_agent_update_uses_only_the_served_profiles_session_store(tmp_path, monkeypatch, profile):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    satellite = root / "profiles" / "other"
    satellite.mkdir(parents=True)
    (satellite / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr("hermes_constants._default_hermes_root_memo", None)
    # The hermetic conftest pins ``hermes_state.DEFAULT_DB_PATH`` at one sandbox store whenever
    # hermes_state is already imported, and that pin WINS over ``get_hermes_home()`` inside
    # ``_default_db_path()`` — exactly the per-profile resolution this test exists to prove.
    # Restore the import-time sentinel so the two seed rows land in their own stores.
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    # Disabling the hermetic pin is only safe while the sentinel still resolves INSIDE the sandbox:
    # a resolution that escaped to the real home would have this test writing the live store.
    resolved = Path(hermes_state._default_db_path())
    assert resolved.is_relative_to(tmp_path), f"unpinned store escaped the sandbox: {resolved}"
    runner = object.__new__(GatewayRunner)
    runner._session_db_pinned = _SESSION_DB_UNPINNED
    runner._session_db_handles = {}
    runner._session_db_handles_lock = threading.Lock()
    runner._served_profile_homes = {"default": root, "other": satellite}
    runner._schedule_update_notification_watch = Mock()
    spawn = Mock()
    loop = Mock()
    try:
        # The same ID in two real stores makes an unscoped lookup observably wrong.
        for home, chat in ((root, "root-chat"), (satellite, "other-chat")):
            token = set_hermes_home_override(home)
            try:
                db = runner._session_db._db
                db.create_session("requesting-session", "telegram", chat_id=chat, chat_type="private")
            finally:
                reset_hermes_home_override(token)
        handler = make_agent_update_handler(
            runner=runner, home=root, main_loop=loop, resolve_hermes_bin=lambda: ["hermes"],
            spawn=spawn, is_managed=lambda: False,
        )
        result = handler({"session_id": "requesting-session", "reason": "Apply routing fix", "profile": profile})
        assert get_hermes_home() == root  # The socket worker must not retain the requesting scope.
        if profile == "other":
            assert result["accepted"] is True
            pending = json.loads((root / ".update_pending.json").read_text(encoding="utf-8"))
            assert pending["chat_id"] == "other-chat"
            assert pending["profile"] == "other"
            assert pending["parent_session_id"] == "requesting-session"
            assert not (satellite / ".update_pending.json").exists()
            spawn.assert_called_once()
            loop.call_soon_threadsafe.assert_called_once_with(runner._schedule_update_notification_watch)
        else:
            assert result == {"accepted": False, "error": "profile is not served by this gateway"}
            spawn.assert_not_called()
            loop.call_soon_threadsafe.assert_not_called()
            assert not (root / ".update_pending.json").exists()
    finally:
        runner.close_all_session_db_handles()

"""Restart recovery must visit every served profile's pending-message spool."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from hermes_constants import get_hermes_home
from gateway import run as gateway_run
from gateway.run_pending_recovery import recover_pending_shutdown_flush
from gateway.shutdown_flush import flush_pending_to_file


def test_startup_recovers_secondary_spool_after_shared_bot_shutdown(tmp_path, monkeypatch):
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(primary))
    dbs = {primary: MagicMock(), secondary: MagicMock()}
    keys = {primary: "agent:main:telegram:dm:1", secondary: "agent:other:telegram:dm:2"}
    # A → B → A: simulate the shared primary adapter's two routed slots being
    # preserved under their owning profiles, then startup back under the launch home.
    with gateway_run._profile_runtime_scope(primary, prepared_secret_scope={}):
        assert flush_pending_to_file({keys[primary]: "first"}) == 1
    with gateway_run._profile_runtime_scope(secondary, prepared_secret_scope={}):
        assert flush_pending_to_file({keys[secondary]: "second"}) == 1
    with gateway_run._profile_runtime_scope(primary, prepared_secret_scope={}):
        assert Path(get_hermes_home()) == primary

        def resolve(key, *, not_after=None):
            home = Path(get_hermes_home())
            if key != keys[home]:
                return None
            return f"session-{home.name}", dbs[home]

        runner = SimpleNamespace(
            session_store=SimpleNamespace(resolve_session_id_for_key=resolve),
            _served_profile_homes={"default": primary, "other": secondary},
        )
        assert recover_pending_shutdown_flush(runner) == 2

    dbs[primary].append_message.assert_called_once()
    dbs[secondary].append_message.assert_called_once()
    assert dbs[primary].append_message.call_args.kwargs["content"] == "first"
    assert dbs[secondary].append_message.call_args.kwargs["content"] == "second"
    assert not list((primary / "pending_messages").glob("*.json"))
    assert not list((secondary / "pending_messages").glob("*.json"))

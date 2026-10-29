"""Restart recovery must visit every served profile's pending-message spool."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore

from hermes_constants import get_hermes_home
from gateway import run as gateway_run
from gateway.run_pending_recovery import recover_pending_shutdown_flush
from gateway.shutdown_flush import flush_pending_to_file


def test_shutdown_spools_real_runner_session_views(tmp_path, monkeypatch):
    """Runner legacy attributes are SessionFieldView mappings, not plain dicts."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._sessions = {}
    runner._primary_profile_name = "default"
    runner._served_profile_homes = {"default": tmp_path}
    key = "agent:main:telegram:dm:real-runner"
    runner._session_state(key)
    runner._pending_messages[key] = "pending"
    runner._queued_events[key] = ["queued"]
    flushed = []
    monkeypatch.setattr(
        runner, "_flush_owned_pending",
        lambda session_key, value, **kwargs: flushed.append((session_key, value, kwargs)) or 1,
    )

    assert runner._persist_shutdown_pending_messages() == 2
    assert [value for _key, value, _kwargs in flushed] == ["pending", ["queued"]]
    assert dict(runner._pending_messages) == {}
    assert dict(runner._queued_events) == {}


def test_startup_recovers_secondary_spool_after_shared_bot_shutdown(tmp_path, monkeypatch):
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(primary))
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._primary_profile_name = "default"
    runner._served_profile_homes = {"default": primary, "other": secondary}
    dbs = {primary: MagicMock(), secondary: MagicMock()}
    keys = {primary: "agent:main:telegram:dm:1", secondary: "agent:other:telegram:dm:2"}
    # A → B → A: the shared primary bot has slots for two runtime profiles.
    # The shutdown writer must scope each slot by key, not by transport owner.
    assert runner._flush_owned_pending(keys[primary], "first", reason="adapter_shutdown") == 1
    assert runner._flush_owned_pending(keys[secondary], "second", reason="adapter_shutdown") == 1
    with gateway_run._profile_runtime_scope(primary, prepared_secret_scope={}):
        assert Path(get_hermes_home()) == primary

        def resolve(key, *, not_after=None):
            home = Path(get_hermes_home())
            if key != keys[home]:
                return None
            return f"session-{home.name}", dbs[home]

        runner.session_store = SimpleNamespace(resolve_session_id_for_key=resolve)
        assert recover_pending_shutdown_flush(runner) == 2

    dbs[primary].append_message.assert_called_once()
    dbs[secondary].append_message.assert_called_once()
    assert dbs[primary].append_message.call_args.kwargs["content"] == "first"
    assert dbs[secondary].append_message.call_args.kwargs["content"] == "second"
    assert not list((primary / "pending_messages").glob("*.json"))
    assert not list((secondary / "pending_messages").glob("*.json"))


@pytest.mark.parametrize("primary_name", ["other", "default"])
def test_single_profile_pending_queues_round_trip_at_launch_home(tmp_path, monkeypatch, primary_name):
    """The actual single-profile key generator uses agent:main even for a named launch."""
    launch = tmp_path / primary_name
    launch.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    store = SessionStore(sessions_dir=launch / "sessions", config=GatewayConfig(multiplex_profiles=False))
    key = store._generate_session_key(SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm"))
    assert key == "agent:main:telegram:dm:1"
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = store.config
    runner.session_store = store
    runner._primary_profile_name = primary_name
    runner._served_profile_homes = {primary_name: launch}
    # The primary adapter, runner's pending slot, and the conversation's overflow tail
    # all use the same owner-scoped shutdown writer.
    assert runner._flush_owned_pending(key, "adapter", reason="adapter_shutdown") == 1
    assert runner._flush_owned_pending(key, "runner", reason="shutdown") == 1
    assert runner._flush_owned_pending(key, ["overflow"], reason="shutdown", overflow=True) == 1
    payloads = [json.loads(path.read_text(encoding="utf-8"))
                for path in (launch / "pending_messages").glob("*.json")]
    assert sorted(payload["data"]["text"] for payload in payloads) == ["adapter", "overflow", "runner"]
    db = MagicMock()
    runner.session_store = SimpleNamespace(resolve_session_id_for_key=lambda key, **kw: ("session", db))
    assert recover_pending_shutdown_flush(runner) == 3
    assert sorted(call.kwargs["content"] for call in db.append_message.call_args_list) == [
        "adapter", "overflow", "runner"]
    assert not list((launch / "pending_messages").glob("*.json"))


@pytest.mark.parametrize("primary_name", ["default", "other"])
def test_old_launch_spool_replays_served_secondary_but_keeps_unserved(tmp_path, monkeypatch, primary_name):
    """Pre-upgrade shutdown wrote shared-bot secondary slots to the launch spool."""
    launch = tmp_path / primary_name
    secondary_name = "other" if primary_name == "default" else "satellite"
    secondary = tmp_path / secondary_name
    launch.mkdir()
    secondary.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._primary_profile_name = primary_name
    runner._served_profile_homes = {primary_name: launch, secondary_name: secondary}
    served_key = f"agent:{secondary_name}:telegram:dm:2"
    unserved_key = "agent:unserved:telegram:dm:3"
    # The previous shutdown used the launch scope for both the runner queue and
    # the shared primary adapter, regardless of the queued key's runtime owner.
    with gateway_run._profile_runtime_scope(launch, prepared_secret_scope={}):
        assert flush_pending_to_file({served_key: "old secondary", unserved_key: "unknown"}) == 2
    db = MagicMock()
    resolved = []

    def resolve(key, *, not_after=None):
        resolved.append((key, Path(get_hermes_home())))
        return ("secondary-session", db) if key == served_key else None

    runner.session_store = SimpleNamespace(resolve_session_id_for_key=resolve)
    assert recover_pending_shutdown_flush(runner) == 1
    assert recover_pending_shutdown_flush(runner) == 0
    db.append_message.assert_called_once()
    assert db.append_message.call_args.kwargs["content"] == "old secondary"
    assert (served_key, secondary) in resolved
    remaining = [json.loads(path.read_text(encoding="utf-8"))
                 for path in (launch / "pending_messages").glob("*.json")]
    assert [payload["session_key"] for payload in remaining] == [unserved_key]
    assert not list((secondary / "pending_messages").glob("*.json"))


@pytest.mark.parametrize("primary_name", ["default", "other"])
def test_multiplex_pending_owner_matrix(tmp_path, monkeypatch, primary_name):
    launch = tmp_path / primary_name
    launch.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    peer_name = "other" if primary_name == "default" else "default"
    peer = tmp_path / peer_name
    peer.mkdir()
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._primary_profile_name = primary_name
    runner._served_profile_homes = {primary_name: launch, peer_name: peer}
    dbs = {launch: MagicMock(), peer: MagicMock()}
    keys = {}
    store = SessionStore(sessions_dir=launch / "sessions", config=runner.config)
    for name, home in ((primary_name, launch), (peer_name, peer)):
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm", profile=name)
        key = store._generate_session_key(source)
        keys[home] = key
        assert runner._flush_owned_pending(key, name, reason="adapter_shutdown") == 1

    def resolve(key, *, not_after=None):
        home = Path(get_hermes_home())
        return ("session-" + home.name, dbs[home]) if key == keys[home] else None
    runner.session_store = SimpleNamespace(resolve_session_id_for_key=resolve)
    assert recover_pending_shutdown_flush(runner) == 2
    for home, db in dbs.items():
        assert db.append_message.call_args.kwargs["content"] == home.name

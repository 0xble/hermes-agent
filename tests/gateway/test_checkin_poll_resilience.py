"""Profile-bounded polling and preservation of armed clocks across unreadable config."""

import asyncio
import errno
from collections import Counter
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.secret_scope import is_multiplex_active, set_multiplex_active
from gateway import run_checkins
from gateway.checkin_delivery import delivery_metadata, permits_metadata
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run import _profile_runtime_scope
from gateway.run_turn import GatewayTurnMixin
from gateway.session import SessionSource
from hermes_cli.checkins import CheckinConfig
from hermes_cli.config import FailedConfigRead, atomic_config_replace
from hermes_constants import get_hermes_home
from tests.gateway import test_supervisor_checkins as supervisor_fixtures

fixture = supervisor_fixtures.fixture
_LOAD_CONFIG = CheckinConfig.load.__func__


@pytest.mark.asyncio
async def test_seen_idle_poll_loads_config_and_enters_scope_once_per_profile(tmp_path, monkeypatch):
    homes = {name: tmp_path / name for name in ("alpha", "beta")}
    configs = {name: CheckinConfig(only_roots=(f"root-{name}",)) for name in homes}
    entries = []
    for name, home in homes.items():
        home.mkdir()
        atomic_config_replace(home / "config.yaml", {"checkins": {"only_roots": [f"root-{name}"]}})
        for index in range(12):
            source = SessionSource(platform=Platform.TELEGRAM, chat_id=str(index), profile=name)
            entries.append(SimpleNamespace(session_key=f"route-{name}-{index}",
                                           session_id=f"session-{name}-{index}",
                                           suspended=False, origin=source))
    # Interleave the homes so grouping cannot rely on contiguous source order.
    entries = [entry for pair in zip(entries[:12], entries[12:]) for entry in pair]
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True),
                             session_store=SimpleNamespace(list_sessions=lambda: entries),
                             _run_in_executor_with_context=asyncio.to_thread,
                             _restored_source=lambda entry: entry.origin,
                             _resolve_profile_home_for_source=lambda source: homes[source.profile],
                             _checkin_seen={(entry.session_key, entry.session_id): configs[entry.origin.profile]
                                            for entry in entries})
    runner._profile_scope_key_for_source = GatewayTurnMixin._profile_scope_key_for_source.__get__(runner)
    scope_entries, loads = [], []

    @asynccontextmanager
    async def scope(source):
        home = runner._profile_scope_key_for_source(source)
        scope_entries.append(home)
        with _profile_runtime_scope(home, hydrate_secrets=False):
            yield

    def load(cls):
        loads.append(get_hermes_home())
        return _LOAD_CONFIG(cls)

    runner._async_profile_scope_for_source = scope
    reconcile = AsyncMock()
    monkeypatch.setattr(run_checkins, "reconcile_turn", reconcile)
    monkeypatch.setattr(CheckinConfig, "load", classmethod(load))
    previous = is_multiplex_active()
    set_multiplex_active(True)
    try:
        for _ in range(2):
            loads.clear()
            scope_entries.clear()
            await run_checkins.poll_checkins(runner)
            assert Counter(loads) == Counter(homes.values())
            assert Counter(scope_entries) == Counter(homes.values())
            reconcile.assert_not_awaited()
    finally:
        set_multiplex_active(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("read_errno", [errno.EIO, errno.EMFILE])
async def test_transient_config_failure_pauses_without_resetting_armed_attempt(fixture, monkeypatch, read_errno):
    f = fixture
    f.runner._profile_scope_key_for_source = GatewayTurnMixin._profile_scope_key_for_source.__get__(f.runner)
    raw = {"checkins": {"enabled": True, "only_roots": ["owner"]}}
    failing = False

    def load_config():
        return FailedConfigRead(raw, error=OSError(read_errno, "temporary config read failure")) if failing else raw

    monkeypatch.setattr(CheckinConfig, "load", classmethod(_LOAD_CONFIG))
    monkeypatch.setattr("hermes_cli.config.load_config", load_config)
    await run_checkins.poll_checkins(f.runner)
    due = f.manager.state.due_at
    f.clock.now = due
    assert f.manager.start(due, f.work)
    armed = f.db.get_meta("checkin:owner")
    attempt_at = f.manager.state.next_attempt_at
    seen, watches = dict(f.runner._checkin_seen), dict(f.runner._checkin_watch)
    f.clock.now = due + 100
    failing = True
    event = MessageEvent(text="pending", source=f.source, internal=True)
    event._checkin_pending_root = "owner"
    assert not await permits_metadata(f.runner, delivery_metadata(event, {}))
    await run_checkins.poll_checkins(f.runner)
    assert f.db.get_meta("checkin:owner") == armed
    assert f.runner._checkin_seen == seen
    assert f.runner._checkin_watch == watches
    f.authority.admit_automation.assert_not_awaited()

    failing = False
    f.clock.now = attempt_at - 1
    await run_checkins.poll_checkins(f.runner)
    assert f.db.get_meta("checkin:owner") == armed
    f.authority.admit_automation.assert_not_awaited()
    f.clock.now = attempt_at
    await run_checkins.poll_checkins(f.runner)
    admitted = f.authority.admit_automation.await_args.args[1]
    assert admitted.metadata["checkin"] == ["owner", due]
    assert f.manager.state.next_attempt_at == attempt_at

    # An authored disable is still destructive disarm, unlike a failed read.
    raw = {"checkins": {"enabled": False, "only_roots": ["owner"]}}
    await run_checkins.poll_checkins(f.runner)
    assert f.manager.state is None
    assert f.entry.session_key not in f.runner._checkin_watch

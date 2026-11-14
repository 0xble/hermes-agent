"""The check-in poll walks every routed session on the event loop; it must stay cheap per entry.

The live gateway held 36,898 routed sessions. Each poll (every 5 s) restored every origin on the
loop thread, and each restore realpath'd the session DB path again: a pass of ~150k lstat calls
that released and re-took the GIL per call and stalled the loop for seconds under thread load.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import authz_mixin, run_checkins
from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.config import Platform
from gateway.session import SessionSource


def _standalone_runner(db_path):
    return SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False),
                           _session_db=SimpleNamespace(db_path=str(db_path)))


def test_restoring_many_origins_resolves_the_session_db_path_once(tmp_path, monkeypatch):
    real = tmp_path / "real-home"
    real.mkdir()
    (tmp_path / "home").symlink_to(real)
    runner = _standalone_runner(tmp_path / "home" / "state.db")
    authz_mixin._resolved_db_parent.cache_clear()
    expected = real.resolve()
    calls = []
    resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: calls.append(self) or resolve(self, *a, **k))

    homes = set()
    for index in range(200):
        entry = SimpleNamespace(origin=SimpleNamespace(), transport_profile=None)
        source = GatewayAuthorizationMixin._restored_source(runner, entry)
        homes.add(source._restored_runtime_home)

    assert homes == {expected}  # still the symlink-resolved parent
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_large_roster_scan_lets_other_loop_work_run(monkeypatch):
    entries = [SimpleNamespace(session_key=f"route-{i}", session_id=f"session-{i}", suspended=False,
                               origin=SessionSource(platform=Platform.TELEGRAM, chat_id=str(i)))
               for i in range(2048)]
    ticks = []
    seen_at = []

    async def ticker():
        while True:
            ticks.append(1)
            await asyncio.sleep(0)

    def restored(entry):  # returns None: nothing to group, this test is only about the scan
        seen_at.append(len(ticks))

    runner = SimpleNamespace(session_store=SimpleNamespace(list_sessions=lambda: entries),
                             _run_in_executor_with_context=asyncio.to_thread,
                             _restored_source=restored, _checkin_watch={})
    monkeypatch.setattr(run_checkins, "reconcile_turn", AsyncMock())
    task = asyncio.create_task(ticker())
    try:
        await run_checkins.poll_checkins(runner)
    finally:
        task.cancel()
    assert len(seen_at) == len(entries)
    # Without a yield the ticker cannot run between the first and last entry of the scan.
    assert seen_at[-1] > seen_at[0]

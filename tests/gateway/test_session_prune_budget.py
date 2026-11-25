"""The owner's default leaves room inside the prune client's timeout."""
import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_owner_prune_default_tracks_the_client_timeout(monkeypatch):
    import gateway.session_prune as owner
    import gateway.session_runtime_workers as workers
    import hermes_cli.sessions_owner as client

    authority = SimpleNamespace(profile_id="synthetic", _require_admission_open=lambda: None)
    connection = SimpleNamespace(native_owner=True, authority=authority,
                                 actor=SimpleNamespace(profile_id="synthetic", capabilities={"session:operator"}))
    monkeypatch.setattr(client, "PRUNE_RPC_TIMEOUT", 400)
    monkeypatch.setattr(workers, "track_mutation", lambda authority, coroutine: asyncio.create_task(coroutine))

    async def apply(authority, filters, never_active, within_window, max_seconds):
        return max_seconds
    monkeypatch.setattr(owner, "_apply", apply)
    assert await owner.prune(connection, None, {"filters": {}, "now": True}) == 360
    assert await owner.prune(connection, None, {"filters": {}, "now": True, "max_seconds": 7}) == 7

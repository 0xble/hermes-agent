"""Generation-owned routing preserves obligations until the owner is truly idle."""
from types import SimpleNamespace

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity
from gateway.owned_routing import OwnedRouting


@pytest.mark.asyncio
async def test_session_claim_releases_only_after_dependent_work_drains(tmp_path, monkeypatch):
    store = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="slot-a")
    new = GenerationIdentity.create(release_sha="b", label="slot-b")
    store.register(old, state="draining")
    store.register(new, state="serving")
    old_epoch = store.acquire_lease("active_generation", old.id)
    key = "agent:default:telegram:chat-1"
    store.claim_session(str(tmp_path), "telegram", key, old.id, old_epoch, outstanding_work=1)
    assert store.release_lease("active_generation", old.id, old_epoch)
    new_epoch = store.acquire_lease("active_generation", new.id)
    busy = [1]
    runner = SimpleNamespace(adapters={}, _overlap_draining=True, _pending_approvals={},
                             _active_work_count=lambda: busy[0])
    generation = SimpleNamespace(coordinator=store, identity=old, epoch=old_epoch,
                                 runner=runner, home=tmp_path)
    routing = OwnedRouting(generation)
    from tools.process_registry import process_registry
    monkeypatch.setattr(process_registry, "has_any_active", lambda: False)
    monkeypatch.setattr(process_registry, "pending_watchers", [])

    await routing._drain_once()
    with store.connect() as db:
        claim = db.execute("SELECT generation_id,outstanding_work FROM sessions WHERE session_key=?", (key,)).fetchone()
        assert tuple(claim) == (old.id, 1)

    busy[0] = 0
    await routing._drain_once()
    with store.connect() as db:
        claim = db.execute("SELECT generation_id,epoch,outstanding_work FROM sessions WHERE session_key=?", (key,)).fetchone()
        assert tuple(claim) == (new.id, new_epoch, 0)

"""A restart never resumes an invisible cron review turn; its queued report replays instead."""
import concurrent.futures
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionStore
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _store(tmp_path):
    from hermes_state import SessionDB
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    if store._db is not None:
        store._db.close()
    store._db = SessionDB(db_path=tmp_path / "state.db")
    return store


@pytest.mark.asyncio
async def test_interrupted_review_turn_is_never_resumed_while_a_normal_turn_is(tmp_path):
    store = _store(tmp_path)
    normal, review = make_restart_source(chat_id="normal"), make_restart_source(chat_id="review")
    events = {
        store.get_or_create_session(normal).session_key: MessageEvent(text="hi", source=normal),
        store.get_or_create_session(review).session_key: MessageEvent(
            text="review", source=review, internal=True,
            metadata={"_cron_review_capture": concurrent.futures.Future()}),
    }
    runner, _adapter = make_restart_runner()
    runner.session_store, runner._restart_requested = store, True
    runner._running_agents = {key: object() for key in events}
    runner._peek_session_state = lambda key: type("S", (), {"turn": type("T", (), {"event": events[key]})})
    for key, event in events.items():
        await runner._mark_durable_active_turn(event, key)
    await runner._mark_running_sessions_resume_pending("pre-drain")
    store._db.close()

    # Next boot reads only durable state, including the unclean-exit active-turn recovery path.
    booted = _store(tmp_path)
    boot, adapter = make_restart_runner()
    boot.session_store = booted
    boot._is_user_authorized_for_source = lambda _source: True
    adapter.handle_message = AsyncMock()
    await boot._recover_unclean_sessions()
    candidates = boot._resume_pending_candidates(record_boot=False)

    assert boot._schedule_resume_pending_sessions(candidates=candidates) == 1
    normal_key, review_key = events
    assert normal_key in boot._running_agents
    assert review_key not in boot._running_agents
    assert not booted._entries[review_key].resume_pending
    booted._db.close()

"""Regression: /goal continuations enqueued via the adapter FIFO are drained
automatically — no extra user message required (#47699).

Issue #47699 reported that a Slack ``/goal`` continuation could be enqueued by
``_post_turn_goal_continuation`` → ``_enqueue_fifo`` (adapter pending slot) but
never consumed until the next real inbound message woke the session. The
post-delivery judge can outlive the adapter's bounded bookkeeping join, so the
adapter may already be idle when the continuation is enqueued. These tests pin
that contract from the runner hook down: an idle enqueue must start the normal
adapter drain without duplicating a real in-flight turn.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


class _DrainProbeAdapter(BasePlatformAdapter):
    """Minimal concrete adapter that records deliveries."""

    def __init__(self) -> None:
        super().__init__(PlatformConfig(enabled=True, token="x"), Platform.SLACK)
        self.sent: list[str] = []

    async def start(self):  # pragma: no cover - unused
        pass

    async def stop(self):  # pragma: no cover - unused
        pass

    async def connect(self):  # pragma: no cover - unused
        pass

    async def disconnect(self):  # pragma: no cover - unused
        pass

    async def get_chat_info(self, chat_id):  # pragma: no cover - unused
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)

        class _R:
            success = True
            message_id = "m1"

        return _R()

    async def send_typing(self, chat_id, metadata=None):
        pass


def _slack_thread_source() -> SessionSource:
    return SessionSource(
        platform=Platform.SLACK,
        user_id="U1",
        chat_id="C1",
        user_name="tester",
        chat_type="channel",
        thread_id="1718600000.000100",
    )


CONTINUATION_TEXT = "[Continuing toward your standing goal]\nGoal: ship it"


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    # Pre-warm the SessionDB cache from this sync (non-loop) context so the
    # async tests' GoalManager.set() never races the bounded loop-thread
    # bootstrap window on loaded CI runners (goal silently not persisted →
    # continuation never enqueued; flaked on main run 33455779041).
    goals._get_session_db()
    yield home
    goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_fifo_enqueued_continuation_is_drained_without_new_user_message():
    """A continuation placed in the adapter pending slot during the handler
    frame (exactly what _post_turn_goal_continuation does via _enqueue_fifo)
    must start a second turn automatically."""
    adapter = _DrainProbeAdapter()
    src = _slack_thread_source()
    key = build_session_key(src)
    handled: list[str] = []

    async def handler(event):
        handled.append(event.text)
        if len(handled) == 1:
            # Mirror the runner's goal hook: enqueue the synthetic
            # continuation into the adapter FIFO while this frame is live.
            cont = MessageEvent(
                text=CONTINUATION_TEXT,
                message_type=MessageType.TEXT,
                source=src,
            )
            adapter._pending_messages[key] = cont
        return f"reply-{len(handled)}"

    adapter.set_message_handler(handler)
    event = MessageEvent(text="do the thing", message_type=MessageType.TEXT, source=src)

    await adapter._process_message_background(event, key)
    # The in-band drain hands off to a fresh task (#17758); let it run.
    for _ in range(40):
        # Wait for the SENDS, not just the handler calls: the delivery
        # ledger hops to worker threads around each send, so the handler
        # can return while reply-2's send is still in flight.
        if len(adapter.sent) >= 2:
            break
        await asyncio.sleep(0.05)

    assert handled == ["do the thing", CONTINUATION_TEXT], (
        "goal continuation was enqueued but never drained — "
        "a user nudge would be required (#47699)"
    )
    assert adapter.sent == ["reply-1", "reply-2"]
    # The drain task must have released the session guard when the chain ended.
    for _ in range(40):
        if key not in adapter._active_sessions:
            break
        await asyncio.sleep(0.05)
    assert key not in adapter._active_sessions


@pytest.mark.asyncio
async def test_runner_goal_hook_enqueues_into_the_key_the_adapter_drains(hermes_home):
    """_post_turn_goal_continuation resolves the FIFO key via
    _session_key_for_source; the adapter drain uses build_session_key on the
    event source. These must agree or the continuation is orphaned under a
    key nobody drains (the silent-stall shape from #47699)."""
    from unittest.mock import MagicMock, patch
    from datetime import datetime
    import uuid

    from gateway.run import GatewayRunner
    from gateway.session import SessionEntry
    from hermes_cli.goals import GoalManager

    src = _slack_thread_source()
    adapter_key = build_session_key(src)

    runner = object.__new__(GatewayRunner)
    from gateway.config import GatewayConfig

    runner.config = GatewayConfig(
        platforms={Platform.SLACK: PlatformConfig(enabled=True, token="x")},
    )
    session_entry = SessionEntry(
        session_key=adapter_key,
        session_id=f"goal-sess-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.SLACK,
        chat_type="channel",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store._generate_session_key.return_value = adapter_key

    adapter = _DrainProbeAdapter()
    runner.adapters = {Platform.SLACK: adapter}

    GoalManager(session_entry.session_id).set("ship it")
    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "still needs work", False, None, False),
    ), patch("hermes_cli.goals._evidence_only_read_only_status", return_value=False), \
            patch("hermes_cli.goals._goal_progress_fingerprint", side_effect=lambda _e: uuid.uuid4().hex):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="partial progress",
        )
        await asyncio.sleep(0.05)

    assert adapter_key in adapter._pending_messages, (
        "continuation enqueued under a different key than the adapter "
        f"drains: pending keys={list(adapter._pending_messages)} "
        f"expected={adapter_key}"
    )


@pytest.mark.asyncio
async def test_idle_post_turn_goal_continuation_starts_a_new_fifo_drain(hermes_home):
    """A deferred judge can finish after the original adapter task releases its guard.

    The continuation must kick an idle adapter itself; otherwise it sits in the pending
    slot until an unrelated human message arrives (the incident's strand shape).
    """
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock, patch

    from gateway.config import GatewayConfig
    from gateway.platforms.base import post_delivery_owner
    from gateway.run import GatewayRunner
    from gateway.session import AsyncSessionStore, SessionEntry
    from gateway.session_results import execution_result
    from hermes_cli.goals import GoalManager

    src = _slack_thread_source()
    key = build_session_key(src)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True, token="x")})
    runner._queued_events = {}
    entry = SessionEntry(
        session_key=key, session_id=f"idle-drain-{uuid.uuid4().hex[:8]}", created_at=datetime.now(),
        updated_at=datetime.now(), platform=Platform.SLACK, chat_type="channel",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._generate_session_key.return_value = key
    runner._async_session_store = AsyncSessionStore(runner.session_store)
    setattr(runner._async_session_store, "lookup_by_session_key", AsyncMock(return_value=entry))
    adapter = _DrainProbeAdapter()
    runner.adapters = {Platform.SLACK: adapter}
    handled: list[str] = []

    async def handler(event):
        handled.append(event.text)
        return "reply"

    adapter.set_message_handler(handler)
    guard = asyncio.Event()
    adapter._active_sessions[key] = guard
    event = MessageEvent(text="delivered question", message_type=MessageType.TEXT, source=src)
    GoalManager(entry.session_id).set("ship it")
    captured = {"post_delivery_owned": True}
    token = execution_result.set(captured)
    owner_token = post_delivery_owner.set((adapter, event))
    try:
        with patch(
            "hermes_cli.goals.judge_goal",
            return_value=("continue", "still needs work", False, None, False),
        ), patch("hermes_cli.goals._evidence_only_read_only_status", return_value=False), \
                patch("hermes_cli.goals._goal_progress_fingerprint", side_effect=lambda _e: uuid.uuid4().hex):
            await runner._defer_post_turn_hooks(
                agent_result={"final_response": "partial progress"}, source=src, is_internal=False,
                event=event, session_key=key, generation=None,
            )
    finally:
        post_delivery_owner.reset(owner_token)
        execution_result.reset(token)

    # Ensure the deferred bookkeeping task registered its delivery callback before firing it.
    for _ in range(40):
        if key in adapter._post_delivery_callbacks:
            break
        await asyncio.sleep(0.01)
    assert key in adapter._post_delivery_callbacks

    # Model the real race: the adapter turn releases its guard before delivery fires.
    adapter._release_session_guard(key, guard=guard)
    await adapter._fire_post_delivery_callback(key, None)
    await getattr(event, "_post_turn_bookkeeping_task")
    for _ in range(80):
        if handled:
            break
        await asyncio.sleep(0.05)

    assert len(handled) == 1 and handled[0].startswith(CONTINUATION_TEXT), handled
    assert key not in adapter._pending_messages
    for _ in range(80):
        if key not in adapter._active_sessions:
            break
        await asyncio.sleep(0.05)
    assert key not in adapter._active_sessions


@pytest.mark.asyncio
async def test_deferred_goal_continuation_does_not_duplicate_a_running_successor(hermes_home):
    """Deferred bookkeeping leaves a pending head for the successor that owns the guard."""
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock, patch

    from gateway.config import GatewayConfig
    from gateway.platforms.base import post_delivery_owner
    from gateway.run import GatewayRunner
    from gateway.session import AsyncSessionStore, SessionEntry
    from gateway.session_results import execution_result
    from hermes_cli.goals import GoalManager

    src = _slack_thread_source()
    key = build_session_key(src)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True, token="x")})
    runner._queued_events = {}
    entry = SessionEntry(
        session_key=key, session_id=f"successor-{uuid.uuid4().hex[:8]}", created_at=datetime.now(),
        updated_at=datetime.now(), platform=Platform.SLACK, chat_type="channel",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._generate_session_key.return_value = key
    runner._async_session_store = AsyncSessionStore(runner.session_store)
    setattr(runner._async_session_store, "lookup_by_session_key", AsyncMock(return_value=entry))
    adapter = _DrainProbeAdapter()
    runner.adapters = {Platform.SLACK: adapter}
    event = MessageEvent(text="delivered question", message_type=MessageType.TEXT, source=src)
    guard = asyncio.Event()
    adapter._active_sessions[key] = guard
    successor_hold = asyncio.Event()
    successor = asyncio.create_task(successor_hold.wait())
    adapter._session_tasks[key] = successor
    GoalManager(entry.session_id).set("ship it")
    captured = {"post_delivery_owned": True}
    token = execution_result.set(captured)
    owner_token = post_delivery_owner.set((adapter, event))
    try:
        with patch(
            "hermes_cli.goals.judge_goal",
            return_value=("continue", "still needs work", False, None, False),
        ), patch("hermes_cli.goals._evidence_only_read_only_status", return_value=False), \
                patch("hermes_cli.goals._goal_progress_fingerprint", side_effect=lambda _e: uuid.uuid4().hex), \
                patch.object(adapter, "_start_session_processing", wraps=adapter._start_session_processing) as start:
            await runner._defer_post_turn_hooks(
                agent_result={"final_response": "partial progress"}, source=src, is_internal=False,
                event=event, session_key=key, generation=None,
            )
            for _ in range(40):
                if key in adapter._post_delivery_callbacks:
                    break
                await asyncio.sleep(0.01)
            await adapter._fire_post_delivery_callback(key, None)
            await getattr(event, "_post_turn_bookkeeping_task")
            start.assert_not_called()
    finally:
        post_delivery_owner.reset(owner_token)
        execution_result.reset(token)
        successor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await successor

    assert key in adapter._pending_messages


@pytest.mark.asyncio
async def test_gateway_goal_continuation_uses_synthetic_provenance(hermes_home):
    """The real gateway post-turn caller must count chained continuations as automatic turns."""
    from datetime import datetime
    from unittest.mock import MagicMock, patch
    import uuid

    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session import SessionEntry
    from hermes_cli.goals import GoalManager

    src = _slack_thread_source()
    key = build_session_key(src)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True, token="x")})
    runner._queued_events = {}
    entry = SessionEntry(
        session_key=key, session_id=f"synthetic-{uuid.uuid4().hex[:8]}", created_at=datetime.now(),
        updated_at=datetime.now(), platform=Platform.SLACK, chat_type="channel",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._generate_session_key.return_value = key
    adapter = _DrainProbeAdapter()
    runner.adapters = {Platform.SLACK: adapter}
    GoalManager(entry.session_id).set("wait for external review", max_turns=20)

    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "external review still pending", False, None, False),
    ):
        await runner._post_turn_goal_continuation(
            session_entry=entry, source=src, final_response="nothing new",
        )

    # Counted as automatic: a no-evidence CONTINUE parks on the first rung of the
    # quiet no-progress ladder instead of chaining a continuation.
    state = GoalManager(entry.session_id).state
    assert state is not None and state.status == "active"
    assert state.consecutive_no_progress == 1
    assert state.backoff_level == 1
    assert state.waiting_until - state.waiting_since == pytest.approx(900, abs=1)
    assert key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_gateway_chained_no_progress_continuations_park_silently_on_the_ladder(hermes_home):
    """The first no-progress automatic turn parks silently; later wakes are held unjudged."""
    from datetime import datetime
    from unittest.mock import MagicMock, patch
    import uuid

    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session import SessionEntry
    from hermes_cli.goals import GoalManager

    src = _slack_thread_source()
    key = build_session_key(src)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True, token="x")})
    runner._queued_events = {}
    entry = SessionEntry(
        session_key=key, session_id=f"gap-{uuid.uuid4().hex[:8]}", created_at=datetime.now(),
        updated_at=datetime.now(), platform=Platform.SLACK, chat_type="channel",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._generate_session_key.return_value = key
    adapter = _DrainProbeAdapter()
    runner.adapters = {Platform.SLACK: adapter}
    GoalManager(entry.session_id).set("wait for external review", max_turns=20)

    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "external review still pending", False, None, False),
    ) as judge:
        for _ in range(3):
            await runner._post_turn_goal_continuation(
                session_entry=entry, source=src, final_response="nothing new",
            )

    state = GoalManager(entry.session_id).state
    assert state is not None and state.status == "active"
    assert state.waiting_until > state.waiting_since
    assert state.backoff_level == 1
    assert judge.call_count == 1 and state.turns_used == 1  # later wakes held without a judge call
    assert key not in adapter._pending_messages
    assert adapter.sent == []  # the ladder is quiet

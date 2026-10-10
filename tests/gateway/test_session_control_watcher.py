from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals
    goals._DB_CACHE.clear()
    from hermes_state_registry import acquire
    db = acquire(home / "state.db")
    for sid in ("requester", "target"):
        db.create_session(sid, "telegram", profile_name="default", chat_id=sid,
                          chat_type="private", session_key=f"agent:main:telegram:dm:{sid}")
    goals._DB_CACHE[str(home)] = db
    yield db
    goals._DB_CACHE.clear()
    from hermes_state_registry import release_or_close
    release_or_close(db)


def _runner(state, *, target_route=True, request_success=True, send_success=True, injection_result=True,
            target_origin=True):
    from gateway.run_session_controls import GatewaySessionControlsMixin
    from gateway.run_goals import GatewayGoalsMixin
    from gateway.session import Platform, SessionEntry, SessionSource
    from hermes_cli import session_controls

    class Adapter:
        def __init__(self):
            self.request_success = request_success
            self.send_success = send_success
            self.requests = []
            self.sends = []
            self._pending_messages = {}
            self._active_sessions = {}
            self._message_handler = object()

        async def send_control_request(self, chat_id, text, request_id, metadata=None):
            self.requests.append((chat_id, text, request_id, metadata))
            return SimpleNamespace(success=self.request_success)

        async def send(self, chat_id, text, metadata=None):
            self.sends.append((chat_id, text, metadata))
            return SimpleNamespace(success=self.send_success)

    adapter = Adapter()
    now = datetime.now(timezone.utc)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="target", chat_type="dm", thread_id="42")
    requester_source = SessionSource(platform=Platform.TELEGRAM, chat_id="requester", chat_type="dm", thread_id="43")
    target = SessionEntry("target-key", "target", now, now, origin=source if target_origin else None)
    requester = SessionEntry("requester-key", "requester", now, now, origin=requester_source)

    class Runner(GatewaySessionControlsMixin, GatewayGoalsMixin):
        session_store = SimpleNamespace(
            lookup_by_session_id=lambda sid: {"target": target, "requester": requester}.get(sid)
        )
        _running = True

        async def _run_in_executor_with_context(self, func, *args):
            return func(*args)

        async def _warm_goals_session_db(self, _label):
            return None

        def _restored_source(self, entry):
            return entry.origin

        def _delivery_adapter_for(self, source):
            return adapter if target_route and source.chat_id == "target" else None

        def _thread_metadata_for_source(self, source):
            return {"direct_messages_topic_id": source.thread_id, "thread_id": source.thread_id}

        def _clear_goal_pending_continuations(self, key, _adapter, before=None):
            self.cleared = key
            return 1

        def _is_session_running(self, _key):
            return False

        def _queue_depth(self, _key, adapter=None):
            return 0

        async def _dispatch_plugin_message_injection(self, **kwargs):
            self.injections.append(kwargs)
            return injection_result

    runner = Runner()
    runner.injections = []
    runner.adapter = adapter
    runner.target = target
    runner.requester = requester
    runner.session_controls = session_controls
    return runner


@pytest.mark.asyncio
async def test_watcher_posts_pending_once_with_topic_metadata_and_skips_unrouted(state):
    from hermes_cli.session_controls import request_control, pending_outbox

    runner = _runner(state)
    record = request_control("goal", "clear", "target", requester_sid="requester", reason="done")
    await runner._drain_session_controls()
    await runner._drain_session_controls()
    assert len(runner.adapter.requests) == 1
    assert runner.adapter.requests[0][3]["direct_messages_topic_id"] == "42"
    assert record["id"] not in {r[2] for r in runner.adapter.requests[1:]}
    assert pending_outbox()[0]["request_posted"] is True

    # No persisted gateway origin (CLI/TUI target): nothing can ever post the card.
    unrouted = _runner(state, target_origin=False)
    record2 = request_control("goal", "clear", "target", requester_sid="requester")
    await unrouted._drain_session_controls()
    import json
    from hermes_cli import session_controls
    failed = json.loads(state.get_meta(session_controls._record_key(record2["id"])))
    assert failed["status"] == "failed"
    assert failed["error"] == "target_unroutable"
    assert len(unrouted.injections) == 1


@pytest.mark.asyncio
async def test_watcher_applied_clear_delivers_notice_injection_and_marks_done(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import apply_control, pending_outbox

    runner = _runner(state)
    GoalManager("target").set("watch the build")
    state.append_message("requester", "user", "Please clear the target goal immediately")
    result = apply_control("goal", "clear", "target", requester_sid="requester",
                           user_quote="clear the target goal immediately")
    assert result["status"] == "applied"
    await runner._drain_session_controls()
    assert load_goal("target").status == "cleared"
    assert runner.cleared == "target-key"
    assert len(runner.adapter.sends) == 1
    assert len(runner.injections) == 1
    assert pending_outbox() == []


@pytest.mark.asyncio
async def test_watcher_denied_control_leaves_goal_and_notifies_requester(state):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli.session_controls import request_control, pending_outbox, resolve_request

    runner = _runner(state)
    GoalManager("target").set("watch the build")
    request = request_control("goal", "clear", "target", requester_sid="requester")
    assert resolve_request(request["id"], "deny", "99")["status"] == "denied"
    await runner._drain_session_controls()
    assert load_goal("target").status == "active"
    assert len(runner.injections) == 1
    assert "denied" in runner.injections[0]["content"]
    assert pending_outbox() == []
    # A declined pause/clear must not drop the target's queued continuation.
    assert not hasattr(runner, "cleared")


@pytest.mark.asyncio
async def test_watcher_failed_or_expired_pause_keeps_queued_continuation(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import expire_request, request_control
    import json
    from hermes_cli import session_controls

    GoalManager("target").set("watch the build")
    runner = _runner(state)
    expired = request_control("goal", "pause", "target", requester_sid="requester")
    raw = json.loads(state.get_meta(session_controls._record_key(expired["id"])))
    raw["expires_at"] = 0
    state.set_meta(session_controls._record_key(expired["id"]), json.dumps(raw))
    assert expire_request(expired["id"])["status"] == "expired"
    failed = request_control("goal", "clear", "target", requester_sid="requester")
    assert session_controls.fail_request(failed["id"], "target_unroutable")["status"] == "failed"
    await runner._drain_session_controls()
    assert not hasattr(runner, "cleared")
    assert GoalManager("target").state.status == "active"


@pytest.mark.asyncio
async def test_watcher_approved_goal_resume_admits_one_continuation(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import pending_outbox, request_control, resolve_request

    runner = _runner(state)
    GoalManager("target").set("watch the build")
    GoalManager("target").pause("waiting")
    approved = request_control("goal", "resume", "target", requester_sid="requester")
    record = resolve_request(approved["id"], "approve", "99")
    assert record["status"] == "applied"
    async def admit_event(_adapter, event):
        event._gateway_accepted = True

    with patch("gateway.wake.admit_internal_event", new=AsyncMock(side_effect=admit_event)) as admit:
        await runner._drain_session_controls()
        first = pending_outbox()
        assert first == [] or first[0].get("continuation_enqueued") is True
        await runner._drain_session_controls()
    admit.assert_awaited_once()
    assert pending_outbox() == []


@pytest.mark.asyncio
async def test_watcher_expiry_does_not_deadlock(state):
    from hermes_cli import session_controls

    runner = _runner(state)
    record = session_controls.request_control("goal", "clear", "target", requester_sid="requester")
    key = session_controls._record_key(record["id"])
    value = __import__("json").loads(state.get_meta(key))
    value["expires_at"] = 0
    state.set_meta(key, __import__("json").dumps(value))
    await asyncio.wait_for(runner._drain_session_controls(), timeout=1)
    assert __import__("json").loads(state.get_meta(key))["status"] == "expired"


@pytest.mark.asyncio
async def test_watcher_retries_failed_request_delivery(state):
    from hermes_cli.session_controls import pending_outbox, request_control

    runner = _runner(state, request_success=False)
    request = request_control("goal", "clear", "target", requester_sid="requester")
    await runner._drain_session_controls()
    await runner._drain_session_controls()
    record = next(item for item in pending_outbox() if item["id"] == request["id"])
    assert len(runner.adapter.requests) == 2
    assert record["request_posted"] is False


@pytest.mark.asyncio
async def test_watcher_retries_failed_target_notice_and_requester_injection(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control, pending_outbox

    runner = _runner(state, send_success=False, injection_result=False)
    GoalManager("target").set("watch the build")
    state.append_message("requester", "user", "Please clear the target goal immediately")
    result = apply_control(
        "goal", "clear", "target", requester_sid="requester",
        user_quote="clear the target goal immediately",
    )
    await runner._drain_session_controls()
    record = next(item for item in pending_outbox() if item["id"] == result["record"]["id"])
    assert record["target_notice_sent"] is False
    runner.adapter.send_success = True
    await runner._drain_session_controls()
    record = next(item for item in pending_outbox() if item["id"] == result["record"]["id"])
    assert record["target_notice_sent"] is True
    assert record["requester_notified"] is False
    runner._dispatch_plugin_message_injection = AsyncMock(return_value=True)
    await runner._drain_session_controls()
    assert pending_outbox() == []


@pytest.mark.asyncio
async def test_watcher_retries_wake_not_accepted_and_processes_next_record(state):
    from gateway.wake import WakeNotAccepted
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import pending_outbox, request_control, resolve_request

    runner = _runner(state)
    GoalManager("target").set("watch the build")
    GoalManager("target").pause("waiting")
    resume = request_control("goal", "resume", "target", requester_sid="requester")
    denied = request_control("goal", "clear", "target", requester_sid="requester")
    assert resolve_request(resume["id"], "approve", "admin")["status"] == "applied"
    assert resolve_request(denied["id"], "deny", "admin")["status"] == "denied"
    with patch("gateway.wake.admit_internal_event", new=AsyncMock(side_effect=WakeNotAccepted("busy"))):
        await runner._drain_session_controls()
    first = next(item for item in pending_outbox() if item["id"] == resume["id"])
    assert first["continuation_enqueued"] is False
    assert len(runner.injections) == 1


@pytest.mark.asyncio
async def test_watcher_enqueues_replaced_goal_continuation(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control, pending_outbox

    runner = _runner(state)
    GoalManager("target").set("old")
    GoalManager("target").pause("waiting")
    state.append_message("requester", "user", "Please replace the target goal with ship now")
    result = apply_control(
        "goal", "replace", "target", requester_sid="requester",
        user_quote="replace the target goal with ship now",
        payload={"goal": "ship now"},
    )
    assert result["record"]["continuation_prompt"]
    async def admit_event(_adapter, event):
        event._gateway_accepted = True

    with patch("gateway.wake.admit_internal_event", new=AsyncMock(side_effect=admit_event)) as admit:
        await runner._drain_session_controls()
    admit.assert_awaited_once()
    assert pending_outbox() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["quote", "button"])
@pytest.mark.parametrize("action", ["resume", "replace"])
@pytest.mark.parametrize("subsequent", ["pause", "clear", "replace"])
async def test_watcher_discards_superseded_control_continuation(state, authority, action, subsequent):
    from hermes_cli.goals import GoalManager, load_goal
    from hermes_cli import session_controls

    GoalManager("target").set("old objective")
    GoalManager("target").pause("waiting")
    payload = {"goal": "approved objective"} if action == "replace" else None
    if authority == "quote":
        quote = f"Please {action} the target goal right now"
        state.append_message("requester", "user", quote)
        record = session_controls.apply_control("goal", action, "target", requester_sid="requester",
                                                user_quote=quote, payload=payload)["record"]
    else:
        request = session_controls.request_control("goal", action, "target", requester_sid="requester",
                                                   payload=payload)
        record = session_controls.resolve_request(request["id"], "approve", "admin")
    assert record["status"] == "applied"
    assert record["continuation_prompt"]
    if subsequent == "replace":
        GoalManager("target").set("superseding objective")
    else:
        getattr(GoalManager("target"), subsequent)()
    # Recreate the runner from durable state, as after a gateway restart.
    runner = _runner(state)
    async def admit_event(_adapter, event):
        event._gateway_accepted = True
    with patch("gateway.wake.admit_internal_event", new=AsyncMock(side_effect=admit_event)) as admit:
        await runner._drain_session_controls()
        await runner._drain_session_controls()
    admit.assert_not_awaited()
    persisted = session_controls._load_record(record["id"])
    assert persisted["continuation_enqueued"] is True
    assert persisted["continuation_discarded"] == "target_changed"
    assert session_controls.pending_outbox() == []
    assert len(runner.adapter.sends) == 1
    assert len(runner.injections) == 1
    assert load_goal("target").status == ("active" if subsequent == "replace" else "paused" if subsequent == "pause" else "cleared")


@pytest.mark.asyncio
async def test_watcher_discards_superseded_continuation_after_idle_probe(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli import session_controls

    GoalManager("target").set("old objective")
    GoalManager("target").pause()
    request = session_controls.request_control("goal", "resume", "target", requester_sid="requester")
    record = session_controls.resolve_request(request["id"], "approve", "admin")
    runner = _runner(state)

    def pause_while_probing(_key):
        GoalManager("target").pause("new instruction at idle boundary")
        return False

    runner._is_session_running = pause_while_probing
    with patch("gateway.wake.admit_internal_event", new=AsyncMock()) as admit:
        await runner._drain_session_controls()
    admit.assert_not_awaited()
    assert session_controls._load_record(record["id"])["continuation_discarded"] == "target_changed"
    assert session_controls.pending_outbox() == []


@pytest.mark.asyncio
async def test_watcher_discards_superseded_continuation_at_admission_boundary(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli import session_controls

    GoalManager("target").set("old objective")
    GoalManager("target").pause()
    request = session_controls.request_control("goal", "resume", "target", requester_sid="requester")
    record = session_controls.resolve_request(request["id"], "approve", "admin")
    runner = _runner(state)
    build_event = runner._synthetic_prompt_event

    def pause_after_build(*args, **kwargs):
        event = build_event(*args, **kwargs)
        GoalManager("target").pause("new instruction before adapter admission")
        return event

    runner._synthetic_prompt_event = pause_after_build
    runner.adapter.handle_message = AsyncMock()
    await runner._drain_session_controls()
    runner.adapter.handle_message.assert_not_awaited()
    assert session_controls._load_record(record["id"])["continuation_discarded"] == "target_changed"
    assert session_controls.pending_outbox() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("subsequent", ["pause", "clear", "replace"])
async def test_control_continuation_revalidated_at_idle_runner_ingress(state, subsequent):
    from gateway.run_inbound import GatewayInboundMixin
    from hermes_cli.goals import GoalManager
    from hermes_cli import session_controls

    GoalManager("target").set("old objective")
    GoalManager("target").pause()
    request = session_controls.request_control("goal", "resume", "target", requester_sid="requester")
    record = session_controls.resolve_request(request["id"], "approve", "admin")
    runner = _runner(state)
    source = runner.session_store.lookup_by_session_id("target").origin
    event = runner._synthetic_prompt_event(
        source, record["continuation_prompt"], reply_expected=False, goal_continuation=True,
        goal_session_id="target", goal_fingerprint=record["continuation_fingerprint"],
        goal_instance=record["continuation_instance"],
    )
    # Adapter accepted the wake, then a new control arrived before its idle handler task ran.
    event._gateway_accepted = True
    if subsequent == "replace":
        GoalManager("target").set("superseding objective")
    else:
        getattr(GoalManager("target"), subsequent)()
    with patch("gateway.run_inbound._admit_outbox_event", new=AsyncMock()) as admit:
        assert await GatewayInboundMixin._hm_admit_event(runner, event) is None
    admit.assert_not_awaited()
    await runner._drain_session_controls()  # Durable receipt belongs to the restart-persisted outbox.
    assert session_controls._load_record(record["id"])["continuation_discarded"] == "target_changed"


@pytest.mark.asyncio
async def test_control_continuation_revalidated_for_active_replacement_in_followup(state):
    from gateway.run_turn import GatewayTurnMixin
    from gateway.turn_context import TurnContext
    from hermes_cli.goals import GoalManager
    from hermes_cli import session_controls

    GoalManager("target").set("old objective")
    GoalManager("target").pause()
    request = session_controls.request_control("goal", "resume", "target", requester_sid="requester")
    record = session_controls.resolve_request(request["id"], "approve", "admin")
    runner = _runner(state)
    source = runner.session_store.lookup_by_session_id("target").origin
    prompt = record["continuation_prompt"]
    event = runner._synthetic_prompt_event(
        source, prompt, reply_expected=False, goal_continuation=True,
        goal_session_id="target", goal_fingerprint=record["continuation_fingerprint"],
        goal_instance=record["continuation_instance"],
    )
    GoalManager("target").set("superseding objective")
    runner._MAX_INTERRUPT_DEPTH = 5
    runner._is_goal_continuation_event = lambda value: value.metadata.get("goal_continuation", False)
    runner._run_agent = AsyncMock()
    ctx = TurnContext(source=source, session_key="target-key", session_id="target", run_generation=1, history=[])
    result = {"interrupted": True, "messages": []}
    returned = await GatewayTurnMixin._run_agent_queued_followup(
        runner, ctx, runner.adapter, prompt, event, "", result, None
    )
    assert returned is result
    runner._run_agent.assert_not_awaited()
    await runner._drain_session_controls()
    assert session_controls._load_record(record["id"])["continuation_discarded"] == "target_changed"


def test_replace_notices_show_old_and_new_goal(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control, request_control, resolve_request

    runner = _runner(state)
    GoalManager("target").set("old objective")
    state.append_message("requester", "user", "Please replace the target goal with ship now")
    quoted = apply_control("goal", "replace", "target", requester_sid="requester",
                           user_quote="replace the target goal with ship now", payload={"goal": "ship now"})
    record = quoted["record"]
    notice = runner._control_notice(record)
    assert "goal: old objective -> ship now" in notice
    assert 'Full message: "Please replace the target goal with ship now"' in notice
    assert "(goal: old objective -> ship now)" in runner._requester_notice(record)

    request = request_control("goal", "replace", "target", requester_sid="requester", reason="pivot",
                              payload={"goal": "write docs"})
    approved = resolve_request(request["id"], "approve", "42")
    assert "(approved in Telegram)\ngoal: ship now -> write docs" in runner._control_notice(approved)
    assert "after Telegram approval. (goal: ship now -> write docs)" in runner._requester_notice(approved)


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff", ["event_loop", "reentrant"])
async def test_pause_cleanup_cannot_delete_human_arriving_in_drained_adapter_slot(state, handoff):
    import threading
    from types import MethodType
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.run_busy import GatewayBusySessionMixin
    from hermes_cli.goals import GoalManager
    from tests.gateway.test_goal_continuation_identity import _apply, _post_turn_event

    runner = _runner(state)
    GoalManager("target").set("old objective")
    old = await _post_turn_event(runner)
    _apply(state, "pause")
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    replaced = threading.Event()
    cleanup_threads = []
    human = MessageEvent(text="my next instruction", source=runner.target.origin)
    key = runner.target.session_key
    runner._prefetch_queued_voice_transcript = lambda event, adapter: None
    runner._enqueue_fifo = MethodType(GatewayBusySessionMixin._enqueue_fifo, runner)
    runner._overflow_queue = lambda key: []
    runner._is_goal_continuation_event = GatewayBusySessionMixin._is_goal_continuation_event
    runner._clear_goal_pending_continuations = MethodType(
        GatewayBusySessionMixin._clear_goal_pending_continuations, runner,
    )
    runner._run_in_executor_with_context = MethodType(GatewayRunner._run_in_executor_with_context, runner)
    runner._get_executor = lambda: None  # Real gateway offload, using the loop-owned default pool.

    def adapter_drain_and_human_arrival():
        runner.adapter._pending_messages.pop(key, None)
        runner._enqueue_fifo(key, human, runner.adapter)
        replaced.set()

    class HandoffSlot(dict):
        handed_off = False

        def get(self, lookup_key, default=None):
            item = super().get(lookup_key, default)
            if item is old and not self.handed_off:
                self.handed_off = True
                cleanup_threads.append(threading.get_ident())
                if handoff == "reentrant":
                    adapter_drain_and_human_arrival()
                else:
                    loop.call_soon_threadsafe(adapter_drain_and_human_arrival)
                    if threading.get_ident() != loop_thread:
                        assert replaced.wait(5), "event-loop handoff did not run"
            return item

    runner.adapter._pending_messages = HandoffSlot()
    runner._enqueue_fifo(key, old, runner.adapter)
    await runner._drain_session_controls()
    assert replaced.is_set()
    assert runner.adapter._pending_messages.get(key) is human
    assert cleanup_threads == [loop_thread]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resume", "replace", "pause", "clear"])
async def test_control_outbox_retries_persisted_origin_when_adapter_is_unavailable(state, action):
    from hermes_cli.goals import GoalManager
    from hermes_cli import session_controls
    from tests.gateway.test_goal_continuation_identity import _apply

    GoalManager("target").set("old objective")
    if action == "resume":
        GoalManager("target").pause()
    record = _apply(state, action, **({"goal": "new objective"} if action == "replace" else {}))
    disconnected = _runner(state, target_route=False)
    await disconnected._drain_session_controls()
    persisted = session_controls._load_record(record["id"])
    if action in {"resume", "replace"}:
        assert not persisted.get("continuation_enqueued")
    else:
        assert not persisted.get("continuations_cleared")
    assert not persisted.get("target_notice_skipped")
    assert not persisted.get("target_notice_sent")
    assert not persisted.get("continuations_cleared")
    assert not persisted.get("outbox_done")

    # A replacement watcher reads the same durable receipt after reconnect/restart.
    reconnected = _runner(state)
    accepted = []

    async def accept(_adapter, event):
        accepted.append(event)
        event._gateway_accepted = True

    with patch("gateway.wake.admit_internal_event", new=AsyncMock(side_effect=accept)):
        await reconnected._drain_session_controls()
        await reconnected._drain_session_controls()
    assert len(accepted) == (1 if action in {"resume", "replace"} else 0)
    assert len(reconnected.adapter.sends) == 1
    assert session_controls._load_record(record["id"])["outbox_done"] is True
    assert session_controls.pending_outbox() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement", ["superseded", "expired"])
async def test_control_outbox_retries_persisted_origin_when_adapter_is_unavailable_until_settled(
    state, monkeypatch, settlement,
):
    from hermes_cli.goals import GoalManager
    from hermes_cli import session_controls
    from tests.gateway.test_goal_continuation_identity import _apply

    GoalManager("target").set("old objective")
    GoalManager("target").pause()
    record = _apply(state, "resume")
    runner = _runner(state, target_route=False)
    await runner._drain_session_controls()
    assert not session_controls._load_record(record["id"]).get("outbox_done")
    if settlement == "superseded":
        GoalManager("target").clear()
        await runner._drain_session_controls()
        persisted = session_controls._load_record(record["id"])
        assert persisted["continuation_discarded"] == "target_changed"
        assert persisted["continuation_enqueued"] is True
        assert not persisted.get("target_notice_skipped")
    else:
        monkeypatch.setattr(session_controls, "_now", lambda: record["resolved_at"] + 86401)
        await runner._drain_session_controls()
        assert session_controls._load_record(record["id"])["outbox_done"] is True
        assert session_controls.pending_outbox() == []


@pytest.mark.asyncio
async def test_pending_request_waits_for_offline_adapter_then_posts(state):
    """A persisted origin with a disconnected adapter keeps the card pending, not failed."""
    from hermes_cli import session_controls
    from hermes_cli.session_controls import request_control

    record = request_control("goal", "clear", "target", requester_sid="requester")
    offline = _runner(state, target_route=False)
    await offline._drain_session_controls()
    await offline._drain_session_controls()
    persisted = session_controls._load_record(record["id"])
    assert persisted["status"] == "pending"
    assert not persisted.get("request_posted")
    assert offline.injections == []

    online = _runner(state)
    await online._drain_session_controls()
    assert [r[2] for r in online.adapter.requests] == [record["id"]]
    assert session_controls._load_record(record["id"])["request_posted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["denied", "failed", "expired"])
async def test_unapplied_outcome_notice_waits_for_offline_adapter(state, monkeypatch, outcome):
    """Denied, failed and expired outcomes keep their target notice retryable while offline."""
    from hermes_cli.goals import GoalManager
    from hermes_cli import session_controls
    from hermes_cli.session_controls import request_control, resolve_request

    GoalManager("target").set("watch the build")
    record = request_control("goal", "clear", "target", requester_sid="requester")
    if outcome == "denied":
        resolve_request(record["id"], "deny", "99")
    elif outcome == "failed":
        session_controls.fail_request(record["id"], "apply_failed")
    else:
        with monkeypatch.context() as patched:
            patched.setattr(session_controls, "_now", lambda: record["created_at"] + 86401)
            session_controls.expire_request(record["id"])
    offline = _runner(state, target_route=False)
    await offline._drain_session_controls()
    persisted = session_controls._load_record(record["id"])
    assert persisted["status"] == outcome
    assert not persisted.get("target_notice_skipped")
    assert not persisted.get("target_notice_sent")
    assert not persisted.get("outbox_done")
    assert persisted.get("requester_notified") is True

    online = _runner(state)
    await online._drain_session_controls()
    assert len(online.adapter.sends) == 1
    assert session_controls._load_record(record["id"])["outbox_done"] is True

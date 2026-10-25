from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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


def _runner(state, *, target_route=True, request_success=True, send_success=True, send_error=None,
            send_retryable=True, injection_result=True, target_origin=True):
    from gateway.run_session_controls import GatewaySessionControlsMixin
    from gateway.run_background_admission import GatewayBackgroundAdmissionMixin
    from gateway.run_goals import GatewayGoalsMixin
    from gateway.run_plugin_injection import GatewayPluginInjectionMixin
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
            return SimpleNamespace(success=self.send_success, error=send_error, retryable=send_retryable)

    adapter = Adapter()
    now = datetime.now(UTC)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="target", chat_type="dm", thread_id="42")
    requester_source = SessionSource(platform=Platform.TELEGRAM, chat_id="requester", chat_type="dm", thread_id="43")
    target = SessionEntry("target-key", "target", now, now, origin=source if target_origin else None)
    requester = SessionEntry("requester-key", "requester", now, now, origin=requester_source)

    class Runner(GatewaySessionControlsMixin, GatewayGoalsMixin, GatewayPluginInjectionMixin,
                 GatewayBackgroundAdmissionMixin):
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

        def _is_goal_continuation_event(self, event):
            from gateway.platforms.event import is_goal_continuation_event
            return is_goal_continuation_event(event)

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


def test_control_card_leads_with_decision_and_nonblocking_note():
    from gateway.run_session_controls import GatewaySessionControlsMixin

    text = GatewaySessionControlsMixin._control_text({
        "kind": "goal", "action": "replace", "target_session_id": "target",
        "requester_session_id": "requester", "target_title": "Target session",
        "requester_title": "Requester session", "reason": "new outcome",
        "affected_text": "Current goal: old objective -> new objective",
        "payload": {"goal": "new objective", "contract": {"verification": "green"}, "max_turns": 8},
    })
    assert text.startswith("◆ Replace this goal?\n\n")
    assert "Target: Target session" in text
    assert "Requested by: Requester session" in text
    assert "Proposed goal: new objective" in text
    assert "Proposed contract (replaces the current one):" in text
    assert "Approve only if this still matches the target's current state." in text
    assert "Session control request" not in text


@pytest.mark.asyncio
async def test_request_control_replace_without_an_active_goal_returns_no_goal(state):
    from hermes_cli.session_controls import pending_outbox, request_control

    runner = _runner(state)
    result = request_control(
        "goal", "replace", "target", requester_sid="requester", reason="pivot",
        payload={"goal": "new objective"},
    )

    assert result["error_code"] == "no_goal"
    assert result["status"] == "failed"
    assert result["revision"] is None
    await runner._drain_session_controls()
    assert runner.adapter.requests == []
    assert pending_outbox() == []


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
    assert runner.injections[0]["expected_session_id"] == "requester"
    assert pending_outbox() == []


@pytest.mark.asyncio
async def test_watcher_settles_nonretryable_target_notice_failure(state):
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import _load_record, apply_control, pending_outbox

    runner = _runner(state, send_success=False, send_error="daily_budget_shed", send_retryable=False)
    GoalManager("target").set("watch the build")
    state.append_message("requester", "user", "Please clear the target goal immediately")
    result = apply_control("goal", "clear", "target", requester_sid="requester",
                           user_quote="clear the target goal immediately")
    assert result["status"] == "applied"

    await runner._drain_session_controls()

    record = _load_record(result["record"]["id"])
    assert record is not None
    assert record["target_notice_skipped"] is True
    assert record["requester_notified"] is True
    assert record["outbox_done"] is True
    assert pending_outbox() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rebind", ["new", "resume", None])
@pytest.mark.parametrize("timing", ["lookup", "scope"])
async def test_requester_outcome_stays_in_its_recorded_session(state, rebind, timing):
    """Drive real outbox + injector + strict resolution across a session-key rebind."""
    from contextlib import asynccontextmanager
    from types import MethodType
    from gateway.run_plugin_injection import GatewayPluginInjectionMixin
    from gateway.run_turn import GatewayTurnMixin
    from hermes_cli.goals import GoalManager
    from hermes_cli.session_controls import apply_control, _load_record

    runner = _runner(state)
    old_requester = runner.requester
    current = old_requester
    admitted = []

    def move_route():
        nonlocal current
        if rebind:
            current = SimpleNamespace(
                session_key=old_requester.session_key, session_id=f"{rebind}-session",
                origin=old_requester.origin,
            )

    async def lookup(_key):
        if timing == "lookup":
            move_route()
        return current

    @asynccontextmanager
    async def scope(_source):
        if timing == "scope":
            move_route()
        yield

    async def handle(event):
        resolved = await GatewayTurnMixin._hmwa_resolve_session(runner, event, event.source)
        if resolved is not None:
            admitted.append(resolved[1].session_id)
            event._gateway_accepted = True

    runner.async_session_store = SimpleNamespace(lookup_by_session_key=lookup)
    runner._plugin_injection_accepting = lambda: True
    runner._plugin_injection_authorized = lambda *args, **kwargs: True
    runner._plugin_injection_scope = scope
    runner._session_key_for_source = lambda source: old_requester.session_key
    runner._cache_session_source = lambda *args: None
    runner._is_telegram_topic_lane = lambda source: False
    runner._delivery_adapter_for = lambda source: runner.adapter
    runner.adapter.handle_message = handle
    runner._dispatch_plugin_message_injection = MethodType(
        GatewayPluginInjectionMixin._dispatch_plugin_message_injection, runner,
    )
    GoalManager("target").set("watch the build")
    state.append_message("requester", "user", "Please clear the target goal immediately")
    result = apply_control("goal", "clear", "target", requester_sid="requester",
                           user_quote="clear the target goal immediately")

    await runner._drain_session_controls()

    record = _load_record(result["record"]["id"])
    assert record is not None
    assert record["requester_notified"] is (rebind is None)
    assert not record["requester_notification_skipped"]
    assert record["outbox_done"] is (rebind is None)
    assert admitted == ([] if rebind else ["requester"])



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
async def test_watcher_admits_no_continuation_for_replaced_goal(state):
    """Slice 6: replacing a paused goal keeps it paused and starts no turn. The drain settles the
    continuation step (``continuation_enqueued`` means settled) without admitting a wake."""
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
    assert "continuation_prompt" not in result["record"]
    async def admit_event(_adapter, event):
        event._gateway_accepted = True

    with patch("gateway.wake.admit_internal_event", new=AsyncMock(side_effect=admit_event)) as admit:
        await runner._drain_session_controls()
    admit.assert_not_awaited()
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
    if action == "replace":
        assert "continuation_prompt" not in record
    else:
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
    if action == "replace":
        # No prompt to deliver: the step settles without a discard receipt.
        assert persisted["continuation_enqueued"] is True
        assert persisted.get("continuation_discarded") is None
    else:
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


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "resume", "replace"])
@pytest.mark.parametrize("notices", [True, False])
@pytest.mark.parametrize("user_quote", ["", "check progress"])
async def test_autonomous_goal_outbox_attributes_both_notices_to_autonomy(
    state, action, notices, user_quote,
):
    from hermes_constants import get_hermes_home
    from hermes_cli import session_controls
    from hermes_cli.goals import GoalManager

    get_hermes_home().joinpath("config.yaml").write_text(
        f"goals:\n  autonomy: true\n  auto_notices: {str(notices).lower()}\n", encoding="utf-8",
    )
    manager = GoalManager("target")
    manager.set("old objective")
    if action == "resume":
        manager.pause()
    result = session_controls.apply_control(
        "goal", action, "target", requester_sid="requester", reason="wait for tests",
        payload={"goal": "new objective"} if action == "replace" else None,
        user_quote=user_quote,
    )
    assert result["status"] == "applied"
    record = session_controls._load_record(result["record"]["id"])
    assert record is not None
    assert record["authority"] == {"via": "autonomy", "session_id": "requester", "quote": user_quote}
    runner = _runner(state)

    async def accept(_adapter, event):
        event._gateway_accepted = True

    with patch("gateway.wake.admit_internal_event", new=AsyncMock(side_effect=accept)):
        await runner._drain_session_controls()
        await runner._drain_session_controls()

    # Control receipts keep their existing delivery policy, independent of routine goal notices.
    assert len(runner.adapter.sends) == len(runner.injections) == 1
    target = runner.adapter.sends[0][1]
    requester = runner.injections[0]["content"]
    assert "(applied autonomously without approval)" in target
    assert "was applied autonomously without approval." in requester
    assert "Telegram" not in target + requester
    assert "your words" not in target + requester
    assert runner.adapter.requests == []
    assert runner.injections[0]["expected_session_id"] == "requester"
    assert session_controls._load_record(record["id"])["authority"] == record["authority"]
    assert session_controls.pending_outbox() == []
    if action == "replace":
        assert "Affected: goal: old objective\nProposed goal: new objective" in target
        assert "Affected: goal: old objective\nProposed goal: new objective" in requester


@pytest.mark.parametrize("authority", [{}, {"via": "future-authority"}])
def test_control_notices_do_not_invent_approval_for_unrecorded_authority(authority):
    from gateway.run_session_controls import GatewaySessionControlsMixin

    record = {"status": "applied", "kind": "goal", "action": "pause", "authority": authority}
    for notice in (GatewaySessionControlsMixin._control_notice(record),
                   GatewaySessionControlsMixin._requester_notice(record)):
        assert "authority source not recorded" in notice
        assert "Telegram" not in notice


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
    assert "Affected: goal: old objective\nProposed goal: ship now" in notice
    assert 'Full message: "Please replace the target goal with ship now"' in notice
    assert "Affected: goal: old objective\nProposed goal: ship now" in runner._requester_notice(record)

    request = request_control("goal", "replace", "target", requester_sid="requester", reason="pivot",
                              payload={"goal": "write docs"})
    approved = resolve_request(request["id"], "approve", "42")
    assert "(approved in Telegram)\nAffected: goal: ship now\nProposed goal: write docs" in runner._control_notice(approved)
    assert "after Telegram approval.\nAffected: goal: ship now\nProposed goal: write docs" in runner._requester_notice(approved)

    session_applied = apply_control(
        "goal", "replace", "target", requester_sid="requester", reason="session pivot",
        payload={"goal": "update the plan"},
    )
    assert (
        "(applied by session requester without approval)"
        in runner._control_notice(session_applied["record"])
    )
    assert (
        "by session authority from requester."
        in runner._requester_notice(session_applied["record"])
    )


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


@pytest.mark.parametrize("status", ["pending", "applied"])
def test_long_goals_keep_cards_and_notices_within_one_telegram_message(state, status):
    """A long goal or message must not push a card or notice past Telegram's 4096-char cap."""
    long_goal = "migrate every service " * 400
    record = {
        "kind": "goal", "action": "replace", "status": status,
        "target_session_id": "target", "requester_session_id": "requester",
        "requester_title": "R" * 500, "target_title": "T" * 500, "reason": "why " * 500,
        "affected_text": f"goal: {long_goal} -> {long_goal}",
        "authority": {"via": "quote", "quote": "replace the target goal " * 40,
                      "message": "Please replace the target goal " * 200},
    }
    runner = _runner(state)
    for text in (runner._control_text(record), runner._control_notice(record),
                 runner._requester_notice(record)):
        assert len(text) <= 4096
    assert "…" in runner._control_text(record)


def test_replace_card_shows_the_whole_proposal_when_the_current_goal_is_long():
    from gateway.run_session_controls import GatewaySessionControlsMixin

    old = "o" * 5000
    record = {"kind": "goal", "action": "replace", "target_session_id": "t", "requester_session_id": "r",
              "reason": "pivot", "payload": {"goal": "NEW OBJECTIVE: ship docs"},
              "affected_text": f"goal: {old} -> NEW OBJECTIVE: ship docs"}
    text = GatewaySessionControlsMixin._control_text(record)
    assert "Proposed goal: NEW OBJECTIVE: ship docs" in text
    assert len(text) < 4096


def test_replace_card_shows_contract_and_max_turns_that_approval_installs():
    from gateway.run_session_controls import GatewaySessionControlsMixin

    record = {"kind": "goal", "action": "replace", "target_session_id": "t", "requester_session_id": "r",
              "reason": "pivot", "affected_text": "goal: old -> finish writing docs",
              "payload": {"goal": "finish writing docs", "max_turns": 999,
                          "contract": {"constraints": "deploy production now"}}}
    text = GatewaySessionControlsMixin._control_text(record)
    assert "Proposed goal: finish writing docs" in text
    assert "- Constraints: deploy production now" in text
    assert "Max turns: 999" in text
    record["payload"] = {"goal": "finish writing docs"}
    text = GatewaySessionControlsMixin._control_text(record)
    assert "Proposed contract: none (the current contract is dropped)" in text
    assert "Max turns: unchanged" in text
    record["payload"] = {"goal": "finish writing docs", "max_turns": 0}
    assert "Max turns: unlimited" in GatewaySessionControlsMixin._control_text(record)
    record["current_max_turns"] = 20
    assert "Max turns: unlimited" in GatewaySessionControlsMixin._control_text(record)
    record["current_max_turns"] = 0
    assert "Max turns" not in GatewaySessionControlsMixin._control_text(record)
    record["payload"] = {"goal": "finish writing docs"}
    assert "Max turns" not in GatewaySessionControlsMixin._control_text(record)
    record["payload"] = {"goal": "finish writing docs", "max_turns": 30}
    assert "Max turns: 30" in GatewaySessionControlsMixin._control_text(record)


def test_control_card_names_loop_actions():
    from gateway.run_session_controls import GatewaySessionControlsMixin

    for action in ("pause", "resume", "stop"):
        text = GatewaySessionControlsMixin._control_text({"kind": "loop", "action": action})
        assert "this loop" in text
        assert "this goal" not in text

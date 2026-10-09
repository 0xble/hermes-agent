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


def _runner(state, *, target_route=True):
    from gateway.run_session_controls import GatewaySessionControlsMixin
    from gateway.session import Platform, SessionEntry, SessionSource
    from hermes_cli import session_controls
    from hermes_state_registry import acquire

    class Adapter:
        def __init__(self):
            self.requests = []
            self.sends = []
            self._pending_messages = {}
            self._active_sessions = {}
            self._message_handler = object()

        async def send_control_request(self, chat_id, text, request_id, metadata=None):
            self.requests.append((chat_id, text, request_id, metadata))
            return SimpleNamespace(success=True)

        async def send(self, chat_id, text, metadata=None):
            self.sends.append((chat_id, text, metadata))
            return SimpleNamespace(success=True)

    adapter = Adapter()
    now = datetime.now(timezone.utc)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="target", chat_type="dm", thread_id="42")
    requester_source = SessionSource(platform=Platform.TELEGRAM, chat_id="requester", chat_type="dm", thread_id="43")
    target = SessionEntry("target-key", "target", now, now, origin=source)
    requester = SessionEntry("requester-key", "requester", now, now, origin=requester_source)

    class Runner(GatewaySessionControlsMixin):
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

        def _clear_goal_pending_continuations(self, key, _adapter):
            self.cleared = key
            return 1

        def _is_session_running(self, _key):
            return False

        def _queue_depth(self, _key, adapter=None):
            return 0

        def _synthetic_prompt_event(self, source, prompt, **kwargs):
            event = SimpleNamespace(source=source, text=prompt, metadata={})
            return event

        async def _dispatch_plugin_message_injection(self, **kwargs):
            self.injections.append(kwargs)

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

    unrouted = _runner(state, target_route=False)
    record2 = request_control("goal", "clear", "target", requester_sid="requester")
    await unrouted._drain_session_controls()
    assert all(r["id"] != record2["id"] for r in pending_outbox())


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

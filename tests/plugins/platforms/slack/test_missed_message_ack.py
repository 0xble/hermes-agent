"""Slack messages missed while the gateway was offline are found and replayed through the live handler.

Every Slack Web API call is a local fake: nothing here reaches Slack.
"""

from types import SimpleNamespace

import pytest

from gateway import missed_inbound_recovery as recovery
from gateway.config import Platform
from hermes_state import SessionDB
from tests.gateway.test_slack import adapter as slack_fixture

new_slack_adapter = slack_fixture.__wrapped__  # the plain function behind the pytest fixture

SINCE, UNTIL = 1000.0, 2000.0


class FakeSlack:
    """The three Web API methods a scan reads, backed by dicts and recording every call."""

    def __init__(self, channels, history=None, replies=None, *, page_size=None, fail=None):
        self.channels, self.history, self.replies = list(channels), history or {}, replies or {}
        self.page_size, self.fail = page_size, fail or {}
        self.calls = []

    def _call(self, name, kwargs):
        self.calls.append((name, kwargs))
        error = self.fail.get((name, kwargs.get("channel"))) or self.fail.get(name)
        if error:
            raise error

    async def users_conversations(self, **kwargs):
        self._call("users_conversations", kwargs)
        size = self.page_size or len(self.channels) or 1
        start = int(kwargs.get("cursor") or 0)
        more = start + size < len(self.channels)
        return {"channels": self.channels[start:start + size],
                "response_metadata": {"next_cursor": str(start + size) if more else ""}}

    async def conversations_history(self, **kwargs):
        self._call("conversations_history", kwargs)
        return {"messages": list(self.history.get(kwargs["channel"], []))}

    async def conversations_replies(self, **kwargs):
        self._call("conversations_replies", kwargs)
        return {"messages": list(self.replies.get((kwargs["channel"], kwargs["ts"]), []))}

    def names(self):
        return [name for name, _ in self.calls]


def human(ts, text="hello", user="U_OK", **extra):
    return {"type": "message", "user": user, "ts": ts, "text": text, "client_msg_id": f"m-{ts}", **extra}


class RateLimited(RuntimeError):
    response = {"ok": False, "error": "ratelimited"}


def rate_limit():
    return RateLimited("ratelimited")


@pytest.fixture
def adapter():
    slack = new_slack_adapter()
    slack._missed_inbound_call_spacing_s = 0
    slack.set_authorization_check(lambda user_id, chat_type=None, chat_id=None, **_: user_id == "U_OK")
    return slack


def install(adapter, fake):
    client = adapter._app.client
    client.users_conversations = fake.users_conversations
    client.conversations_history = fake.conversations_history
    client.conversations_replies = fake.conversations_replies
    return fake


async def missed(adapter):
    return [(r["conversation"], r["ts"]) for r in await adapter.fetch_missed_inbound(SINCE, UNTIL)]


DM = {"id": "D1", "is_im": True}
CHANNEL = {"id": "C1", "is_channel": True, "is_member": True}


@pytest.mark.asyncio
async def test_a_missed_dm_is_found_and_built_like_a_live_event(adapter):
    install(adapter, FakeSlack([DM], {"D1": [human("1500.000100", "are you there?", reply_users_count=0)]}))

    [found] = await adapter.fetch_missed_inbound(SINCE, UNTIL)

    assert (found["conversation"], found["ts"]) == ("D1", "1500.000100")
    assert found["event"] == {
        "type": "message", "user": "U_OK", "ts": "1500.000100", "text": "are you there?",
        "client_msg_id": "m-1500.000100", "channel": "D1", "channel_type": "im", "event_ts": "1500.000100"}


@pytest.mark.asyncio
async def test_bot_and_own_messages_are_skipped(adapter):
    install(adapter, FakeSlack([DM], {"D1": [
        human("1500.000100"),
        {"type": "message", "user": "U_BOT", "ts": "1400.000100", "text": "my own earlier post"},
        {"type": "message", "subtype": "bot_message", "bot_id": "B9", "ts": "1600.000100", "text": "workflow"},
        {"type": "message", "user": "U_OK", "bot_id": "B7", "app_id": "A7", "ts": "1700.000100", "text": "app post"},
        {"type": "message", "subtype": "channel_join", "user": "U_OK", "ts": "1800.000100", "text": "joined"},
    ]}))

    assert await missed(adapter) == [("D1", "1500.000100")]


@pytest.mark.asyncio
async def test_an_unauthorized_user_is_skipped(adapter):
    install(adapter, FakeSlack([DM, CHANNEL], {
        "D1": [human("1500.000100", user="U_STRANGER"), human("1600.000100")],
        "C1": [human("1700.000100", "<@U_BOT> let me in", user="U_STRANGER")]}))

    assert await missed(adapter) == [("D1", "1600.000100")]


@pytest.mark.asyncio
async def test_a_channel_mention_is_found_and_a_non_mention_is_ignored(adapter):
    install(adapter, FakeSlack([CHANNEL], {"C1": [
        human("1500.000100", "<@U_BOT> can you check the deploy?"),
        human("1600.000100", "talking to someone else"),
        human("1700.000100", "<@U_OTHER> not for the bot"),
    ]}))

    [found] = await adapter.fetch_missed_inbound(SINCE, UNTIL)

    assert (found["conversation"], found["ts"]) == ("C1", "1500.000100")
    assert found["event"]["channel_type"] == "channel"


@pytest.mark.asyncio
async def test_a_message_the_bot_already_answered_is_skipped(adapter):
    fake = install(adapter, FakeSlack(
        [DM, CHANNEL],
        {"D1": [human("1500.000100", reply_count=1), human("1600.000100", reply_count=1)],
         "C1": [human("1700.000100", "<@U_BOT> status?", reply_count=1)]},
        {("D1", "1500.000100"): [human("1500.000100"), {"user": "U_BOT", "ts": "1501.000100", "text": "done"}],
         ("D1", "1600.000100"): [human("1600.000100"), human("1601.000100", "anyone?", thread_ts="1600.000100")],
         ("C1", "1700.000100"): [human("1700.000100", "<@U_BOT> status?"),
                                 {"user": "U_BOT", "ts": "1701.000100", "text": "all green"}]}))

    # Only the thread nobody answered is left, parent and follow-up both.
    assert await missed(adapter) == [("D1", "1600.000100"), ("D1", "1601.000100")]
    assert fake.names().count("conversations_replies") == 3


@pytest.mark.asyncio
async def test_a_flat_conversation_the_bot_already_posted_in_is_skipped(adapter):
    adapter.config.extra["reply_in_thread"] = False
    install(adapter, FakeSlack([DM], {"D1": [
        human("1500.000100"), {"user": "U_BOT", "ts": "1550.000100", "text": "answered"}, human("1600.000100")]}))

    assert await missed(adapter) == [("D1", "1600.000100")]


@pytest.mark.asyncio
async def test_a_dm_thread_reply_in_a_known_thread_is_found(adapter):
    origin = SimpleNamespace(platform=Platform.SLACK, chat_id="D1", thread_id="900.000100", scope_id=None)
    elsewhere = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="D1", thread_id="800.000100", scope_id=None)
    seen = {}

    def list_sessions(active_minutes=None):
        seen["active_minutes"] = active_minutes
        return [SimpleNamespace(origin=origin), SimpleNamespace(origin=elsewhere), SimpleNamespace(origin=None)]

    adapter._session_store = SimpleNamespace(list_sessions=list_sessions)
    # The thread's parent predates the outage, so history never shows the reply.
    fake = install(adapter, FakeSlack([DM], {"D1": []}, {("D1", "900.000100"): [
        human("900.000100", "the original question"),
        human("1500.000100", "any update on this?", thread_ts="900.000100")]}))

    [found] = await adapter.fetch_missed_inbound(SINCE, UNTIL)

    assert (found["conversation"], found["ts"]) == ("D1", "1500.000100")
    assert found["event"]["thread_ts"] == "900.000100"
    [(_, reply_call)] = [call for call in fake.calls if call[0] == "conversations_replies"]
    assert reply_call["ts"] == "900.000100"
    assert float(reply_call["oldest"]) == SINCE
    assert seen["active_minutes"] == 7 * 24 * 60

    await adapter.dispatch_missed_inbound(found, "[Delivered late: test]")

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "[Delivered late: test]\n\nany update on this?"
    assert (event.source.chat_id, event.source.thread_id) == ("D1", "900.000100")


@pytest.mark.asyncio
async def test_messages_outside_the_window_are_ignored(adapter):
    fake = install(adapter, FakeSlack([DM], {"D1": [
        human("999.000100"), human("1000.000000"), human("1500.000100"), human("2000.000001")]}))

    assert await missed(adapter) == [("D1", "1500.000100")]
    [(_, history_call)] = [call for call in fake.calls if call[0] == "conversations_history"]
    assert (float(history_call["oldest"]), float(history_call["latest"])) == (SINCE, UNTIL)


@pytest.mark.asyncio
async def test_conversations_are_paginated_and_dms_are_read_first(adapter):
    channels = [CHANNEL, {"id": "G1", "is_mpim": True}, {"id": "C2", "is_private": True}, DM]
    fake = install(adapter, FakeSlack(channels, page_size=2))

    assert await missed(adapter) == []

    listing = [kwargs for name, kwargs in fake.calls if name == "users_conversations"]
    assert [kwargs.get("cursor") for kwargs in listing] == [None, "2"]
    assert listing[0]["types"] == "im,mpim,public_channel,private_channel"
    assert [kwargs["channel"] for name, kwargs in fake.calls if name == "conversations_history"] == [
        "D1", "G1", "C1", "C2"]


@pytest.mark.asyncio
async def test_a_rate_limit_ends_the_scan_and_keeps_what_was_found(adapter):
    fake = install(adapter, FakeSlack(
        [DM, CHANNEL, {"id": "C2", "is_channel": True}],
        {"D1": [human("1500.000100")], "C2": [human("1600.000100", "<@U_BOT> hi")]},
        fail={("conversations_history", "C1"): rate_limit()}))

    assert await missed(adapter) == [("D1", "1500.000100")]
    assert [kwargs["channel"] for name, kwargs in fake.calls if name == "conversations_history"] == ["D1", "C1"]


@pytest.mark.asyncio
async def test_a_scan_is_bounded_and_one_unreadable_conversation_does_not_end_it(adapter):
    adapter._MISSED_INBOUND_MAX_CALLS = 4
    channels = [{"id": f"D{i}", "is_im": True} for i in range(1, 9)]
    fake = install(adapter, FakeSlack(
        channels, {"D2": [human("1500.000100")]},
        fail={("conversations_history", "D1"): RuntimeError("channel_not_found")}))

    assert await missed(adapter) == [("D2", "1500.000100")]
    assert len(fake.calls) == 4


@pytest.mark.asyncio
async def test_ignored_and_disallowed_conversations_are_never_read(adapter):
    adapter.config.extra["ignored_channels"] = "C_MUTED"
    adapter.config.extra["allowed_channels"] = "C1"
    fake = install(adapter, FakeSlack(
        [DM, CHANNEL, {"id": "C_MUTED", "is_channel": True}, {"id": "C_OTHER", "is_channel": True}]))

    await adapter.fetch_missed_inbound(SINCE, UNTIL)

    assert [kwargs["channel"] for name, kwargs in fake.calls if name == "conversations_history"] == ["D1", "C1"]


@pytest.mark.asyncio
async def test_a_late_dm_goes_through_the_live_handler_once_with_the_note(adapter):
    install(adapter, FakeSlack([DM], {"D1": [human("1500.000100", "are you there?")]}))
    [found] = await adapter.fetch_missed_inbound(SINCE, UNTIL)
    note = recovery.late_note(1500.0001, SINCE, UNTIL)

    await adapter.dispatch_missed_inbound(found, note)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == f"{note}\n\nare you there?"
    assert event.message_id == "1500.000100"
    assert (event.source.chat_id, event.source.chat_type, event.source.user_id) == ("D1", "dm", "U_OK")

    # Slack replays the same message over Socket Mode after the reconnect.
    await adapter._handle_slack_message(
        {"type": "message", "user": "U_OK", "channel": "D1", "channel_type": "im", "ts": "1500.000100",
         "text": "are you there?"}, {"team_id": "T1"})
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_replay_is_dropped_even_after_a_restart_forgot_the_dedup_cache(adapter):
    adapter.mark_missed_inbound_recovered("D1", "1500.000100")  # what recovery reports from its claims
    replay = {"type": "message", "user": "U_OK", "channel": "D1", "channel_type": "im", "text": "are you there?"}

    await adapter._handle_slack_message({**replay, "ts": "1500.000100"}, {"team_id": "T1"})
    adapter.handle_message.assert_not_awaited()

    await adapter._handle_slack_message({**replay, "ts": "1700.000100"}, {"team_id": "T1"})
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_late_message_from_an_unauthorized_user_is_never_answered(adapter):
    record = {"conversation": "D1", "ts": "1500.000100", "event": {
        "type": "message", "user": "U_STRANGER", "channel": "D1", "channel_type": "im", "ts": "1500.000100",
        "text": "hello"}}

    await adapter.dispatch_missed_inbound(record, "[Delivered late: test]")

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_late_channel_message_obeys_the_mention_gate(adapter):
    base = {"type": "message", "user": "U_OK", "channel": "C1", "channel_type": "channel"}

    await adapter.dispatch_missed_inbound(
        {"conversation": "C1", "ts": "1500.000100", "event": {**base, "ts": "1500.000100", "text": "no mention"}},
        "[Delivered late: test]")
    adapter.handle_message.assert_not_awaited()

    await adapter.dispatch_missed_inbound(
        {"conversation": "C1", "ts": "1600.000100",
         "event": {**base, "ts": "1600.000100", "text": "<@U_BOT> are we shipping?"}},
        "[Delivered late: test]")
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text.startswith("[Delivered late: test]\n\n")
    assert event.text.endswith("are we shipping?")
    assert event.source.chat_type == "group"


@pytest.mark.asyncio
async def test_a_late_command_reaches_the_agent_as_text_and_is_not_executed(adapter):
    from gateway.platforms.event import MessageType

    record = {"conversation": "D1", "ts": "1500.000100", "event": {
        "type": "message", "user": "U_OK", "channel": "D1", "channel_type": "im", "ts": "1500.000100",
        "text": "!new"}}

    await adapter.dispatch_missed_inbound(record, "[Delivered late: test]")

    event = adapter.handle_message.await_args.args[0]
    assert event.message_type == MessageType.TEXT
    assert event.is_command() is False
    assert event.text == "[Delivered late: test]\n\n/new"


@pytest.mark.asyncio
async def test_a_live_message_is_reported_to_recovery_and_a_late_one_is_not(adapter):
    seen = []
    adapter.missed_inbound_observer = lambda conversation, ts: seen.append((conversation, ts))
    live = {"type": "message", "user": "U_OK", "channel": "D1", "channel_type": "im", "text": "hi"}

    await adapter._handle_slack_message({**live, "ts": "1700.000100"}, {"team_id": "T1"})
    await adapter._handle_slack_message({**live, "ts": "1701.000100", "user": "U_STRANGER"}, {"team_id": "T1"})
    await adapter.dispatch_missed_inbound(
        {"conversation": "D1", "ts": "1500.000100", "event": {**live, "ts": "1500.000100"}}, "[Delivered late: test]")

    assert seen == [("D1", "1700.000100")]


@pytest.mark.asyncio
async def test_socket_mode_liveness_reports_down_and_the_last_proven_moment(adapter):
    assert await adapter.missed_inbound_live_until() is False  # no Socket Mode task

    adapter._socket_mode_task = SimpleNamespace(done=lambda: False)
    adapter._handler = SimpleNamespace(client=SimpleNamespace(
        is_connected=lambda: True, ping_interval=30, last_ping_pong_time=None))
    adapter._socket_handler_started_monotonic = None
    assert isinstance(await adapter.missed_inbound_live_until(), float)

    import time
    proven = time.time() - 20
    adapter._handler.client.last_ping_pong_time = proven
    assert await adapter.missed_inbound_live_until() == proven

    adapter._handler.client.last_ping_pong_time = time.time() - 3600  # a wedged socket
    assert await adapter.missed_inbound_live_until() is False
    adapter._handler.client.is_connected = lambda: False
    assert await adapter.missed_inbound_live_until() is False


@pytest.mark.asyncio
async def test_rebuilding_socket_mode_asks_recovery_for_a_scan(adapter, monkeypatch):
    import asyncio

    calls = []
    adapter.missed_inbound_reconnected = lambda: calls.append("scan")
    adapter._app_token = "xapp-test"
    adapter._socket_reconnect_lock = asyncio.Lock()

    async def stopped():
        return None

    monkeypatch.setattr(adapter, "_stop_socket_mode_handler", stopped)
    monkeypatch.setattr(adapter, "_start_socket_mode_handler", lambda: None)

    await adapter._restart_socket_mode("ping/pong stale")

    assert calls == ["scan"]


@pytest.mark.asyncio
async def test_recovery_answers_a_missed_slack_dm_once_across_restarts(adapter, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.advance_missed_inbound_watermark("slack", SINCE)
        install(adapter, FakeSlack([DM, CHANNEL], {
            "D1": [human("1500.000100", "are you there?")],
            "C1": [human("1600.000100", "<@U_BOT> ping"), human("1700.000100", "unrelated chatter")]}))
        runner = SimpleNamespace(
            config=SimpleNamespace(missed_inbound_recovery_enabled=True), _session_db=db,
            adapters={Platform.SLACK: adapter})

        assert await recovery.recover_platform(runner, Platform.SLACK, until=UNTIL) == 2

        texts = [call.args[0].text for call in adapter.handle_message.await_args_list]
        assert [text.rsplit("\n\n", 1)[1] for text in texts] == ["are you there?", "ping"]
        assert all(text.startswith("[Delivered late: sent ") for text in texts)

        # The gateway crashes before the watermark moves, and a fresh process starts.
        db._execute_write(lambda conn: conn.execute(
            "UPDATE state_meta SET value='1000.0' WHERE key='missed_inbound:watermark:slack'"))
        restarted = new_slack_adapter()
        restarted._missed_inbound_call_spacing_s = 0
        restarted._app.client = adapter._app.client
        runner.adapters = {Platform.SLACK: restarted}

        assert await recovery.recover_platform(runner, Platform.SLACK, until=UNTIL + 60) == 0
        restarted.handle_message.assert_not_awaited()

        # ...and Slack's own replay of the message into the new process is dropped too.
        await restarted._handle_slack_message(
            {"type": "message", "user": "U_OK", "channel": "D1", "channel_type": "im", "ts": "1500.000100",
             "text": "are you there?"}, {"team_id": "T1"})
        restarted.handle_message.assert_not_awaited()
    finally:
        db.close()

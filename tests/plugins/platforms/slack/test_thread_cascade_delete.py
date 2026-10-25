"""Only tombstoned thread roots in the configured owner's IM are cascade-deleted."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from gateway.config import HomeChannel, Platform, PlatformConfig
from plugins.platforms.slack import adapter as slack


TEAM = "T_OWNER"
OWNER = "U_OWNER"
BOT = "U_BOT"
DM = "D_OWNER"
ROOT = "123.000001"


def _tombstone(parent_user=OWNER):
    return {
        "type": "message", "subtype": "message_changed", "channel": DM,
        "channel_type": "im", "team": TEAM, "ts": "124.000001",
        "message": {
            "type": "message", "subtype": "tombstone", "user": "USLACKBOT",
            "text": "This message was deleted.", "ts": ROOT,
            "thread_ts": ROOT, "reply_count": 2,
        },
        "previous_message": {"ts": ROOT, "user": parent_user},
    }


def _adapter(monkeypatch):
    config = PlatformConfig(
        enabled=True, token="test-bot-token",
        home_channel=HomeChannel(
            platform=Platform.SLACK, chat_id=DM, name="Owner DM", user_id=OWNER,
            scope_id=TEAM,
        ),
    )
    adapter = slack.SlackAdapter(config)
    bot = AsyncMock()
    user = AsyncMock()
    adapter._app = SimpleNamespace(client=bot)
    adapter._team_clients = {TEAM: bot}
    adapter._team_bot_user_ids = {TEAM: BOT}
    adapter._bot_user_id = BOT
    adapter.handle_message = AsyncMock()
    adapter._processed_message_ts[ROOT] = True
    bot.conversations_info.return_value = {
        "ok": True, "channel": {"id": DM, "is_im": True, "user": OWNER},
    }
    bot.conversations_replies.return_value = {
        "ok": True,
        "messages": [
            _tombstone()["message"],
            {"ts": "123.000002", "thread_ts": ROOT, "user": BOT},
            {"ts": "123.000003", "thread_ts": ROOT, "user": OWNER},
        ],
    }
    bot.chat_delete.return_value = user.chat_delete.return_value = {"ok": True}
    user.auth_test.return_value = {"ok": True, "team_id": TEAM, "user_id": OWNER}
    monkeypatch.setattr(slack, "_get_scoped_secret", lambda key: "test-user-token")
    monkeypatch.setattr(adapter, "_new_web_client", Mock(return_value=user))
    return adapter, bot, user


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_user", [OWNER, BOT])
async def test_tombstone_deletes_all_replies_with_their_author_client(monkeypatch, parent_user):
    adapter, bot, user = _adapter(monkeypatch)
    event = _tombstone(parent_user)
    payload = {"team_id": event.pop("team")} if parent_user == BOT else None
    # Read all pages before deletion, including a page without the parent.
    first = bot.conversations_replies.return_value
    first["response_metadata"] = {"next_cursor": "page-2"}
    bot.conversations_replies.side_effect = [first, {
        "ok": True, "messages": [{"ts": "123.000004", "thread_ts": ROOT, "user": BOT}],
        "response_metadata": {"next_cursor": ""},
    }]

    await adapter._handle_slack_message(event, payload)
    # Reply deletion emits additional root updates. They must not restart cleanup.
    await adapter._handle_slack_message({**event, "ts": "125.000001"}, payload)

    assert bot.chat_delete.await_args_list == [
        call(channel=DM, ts="123.000002"), call(channel=DM, ts="123.000004"),
    ]
    user.chat_delete.assert_awaited_once_with(channel=DM, ts="123.000003")
    user.auth_test.assert_awaited_once()
    assert bot.conversations_replies.await_args_list == [
        call(channel=DM, ts=ROOT, limit=200, cursor=""),
        call(channel=DM, ts=ROOT, limit=200, cursor="page-2"),
    ]
    assert bot.mock_calls.index(call.conversations_replies(
        channel=DM, ts=ROOT, limit=200, cursor="page-2",
    )) < bot.mock_calls.index(call.chat_delete(channel=DM, ts="123.000002"))
    bot.chat_postMessage.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "channel", "non_owner_dm", "non_owner_peer", "wrong_team", "no_home_owner",
    "reply_not_root", "live_parent", "ordinary_edit", "delete_error",
    "delete_exception", "foreign_reply", "missing_token", "wrong_token_team",
    "wrong_token_user", "fetch_error",
])
async def test_cascade_scope_and_single_failure_notice(monkeypatch, case):
    adapter, bot, user = _adapter(monkeypatch)
    event = _tombstone()
    failure = case in {
        "delete_error", "delete_exception", "foreign_reply", "missing_token",
        "wrong_token_team", "wrong_token_user", "fetch_error",
    }
    if case == "channel":
        event.update(channel="C_PUBLIC", channel_type="channel")
    elif case == "non_owner_dm":
        event["channel"] = "D_OTHER"
    elif case == "non_owner_peer":
        bot.conversations_info.return_value["channel"]["user"] = "U_OTHER"
    elif case == "wrong_team":
        event["team"] = "T_OTHER"
    elif case == "no_home_owner":
        adapter.config.home_channel.user_id = None
    elif case == "reply_not_root":
        event["message"]["thread_ts"] = "122.000001"
    elif case == "live_parent":
        bot.conversations_replies.return_value["messages"][0] = {"ts": ROOT, "user": OWNER}
    elif case == "ordinary_edit":
        adapter._processed_message_ts.clear()
        event["message"] = {
            "ts": ROOT, "user": OWNER, "text": "Edited text", "client_msg_id": "edited-human-message",
        }
        accepted = await adapter._prefilter_inbound(event, None)
        assert accepted is not None
        assert accepted[0]["text"] == "Edited text"
        bot.conversations_replies.assert_not_awaited()
        bot.chat_delete.assert_not_awaited()
        user.chat_delete.assert_not_awaited()
        return
    elif case == "delete_error":
        user.chat_delete.return_value = {"ok": False, "error": "cant_delete_message"}
        bot.chat_delete.return_value = {"ok": False, "error": "restricted_action"}
    elif case == "delete_exception":
        error = RuntimeError("DO NOT DISCLOSE RAW EXCEPTION")
        error.response = {"ok": False, "error": "cant_delete_message"}
        user.chat_delete.side_effect = error
    elif case == "foreign_reply":
        bot.conversations_replies.return_value["messages"][2]["user"] = "U_OTHER"
    elif case == "missing_token":
        monkeypatch.setattr(slack, "_get_scoped_secret", lambda key: None)
    elif case == "wrong_token_team":
        user.auth_test.return_value["team_id"] = "T_OTHER"
    elif case == "wrong_token_user":
        user.auth_test.return_value["user_id"] = "U_OTHER"
    elif case == "fetch_error":
        bot.conversations_replies.return_value = {"ok": False, "error": "missing_scope"}

    await adapter._handle_slack_message(event)
    await adapter._handle_slack_message({**event, "ts": "125.000001"})

    adapter.handle_message.assert_not_awaited()
    if failure:
        bot.chat_postMessage.assert_awaited_once()
        notice = bot.chat_postMessage.await_args.kwargs
        assert notice["channel"] == DM
        assert "thread_ts" not in notice
        assert "Couldn't clear deleted thread" in notice["text"]
        assert "DO NOT DISCLOSE" not in notice["text"]
        if case.startswith("delete_"):
            assert "cant_delete_message" in notice["text"]
            assert user.chat_delete.await_count == 1
        if case in {"wrong_token_team", "wrong_token_user"}:
            user.chat_delete.assert_not_awaited()
            bot.chat_delete.assert_not_awaited()
    else:
        bot.chat_delete.assert_not_awaited()
        user.chat_delete.assert_not_awaited()
        bot.chat_postMessage.assert_not_awaited()

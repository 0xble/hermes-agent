"""The public reaction contract drives native transports without live sends."""
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.platforms.base import BasePlatformAdapter


@pytest.mark.asyncio
async def test_unsupported_adapter_reactions_are_false():
    assert await BasePlatformAdapter.add_reaction(
        None, "chat", "message", "👍", team_id="workspace", thread_id="thread",
    ) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("platform, class_name", [
    ("telegram", "TelegramAdapter"), ("slack", "SlackAdapter"),
    ("discord", "DiscordAdapter"), ("signal", "SignalAdapter"),
    ("photon", "PhotonAdapter"), ("matrix", "MatrixAdapter"),
    ("buzz", "BuzzAdapter"), ("feishu", "FeishuAdapter"),
    ("dingtalk", "DingTalkAdapter"),
])
async def test_public_reaction_delivers_exact_target_and_reports_failure(monkeypatch, platform, class_name):
    monkeypatch.setenv(f"{platform.upper()}_REACTIONS", "false")
    module_name = "gateway.platforms.signal" if platform == "signal" else f"plugins.platforms.{platform}.adapter"
    module = import_module(module_name)
    adapter = object.__new__(getattr(module, class_name))
    adapter.platform = SimpleNamespace(value=platform)
    routing = {"user_id": "+15550001111", "team_id": "T2", "scope_id": "T2", "thread_id": "789"}
    chat, message, emoji = "123", "456", "👍"

    if platform == "telegram":
        # The shared gateway fixture stubs optional Telegram SDK constants.
        from enum import StrEnum
        constants = import_module("telegram.constants")
        monkeypatch.setattr(constants, "ReactionEmoji", StrEnum("ReactionEmoji", {"THUMBS_UP": emoji}))
        transport = AsyncMock(return_value=True)
        adapter._bot = SimpleNamespace(set_message_reaction=transport)
        expected_args, expected_kwargs = (), {"chat_id": 123, "message_id": 456, "reaction": emoji}
    elif platform == "slack":
        transport = AsyncMock(return_value={"ok": True})
        client = SimpleNamespace(reactions_add=transport)
        adapter._app = object()
        adapter._get_client = Mock(return_value=client)
        emoji = "thumbsup"
        expected_args, expected_kwargs = (), {"channel": chat, "timestamp": message, "name": emoji}
    elif platform == "discord":
        transport = AsyncMock()
        native_message = SimpleNamespace(add_reaction=transport)
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=native_message))
        adapter._client = SimpleNamespace(get_channel=Mock(return_value=None), fetch_channel=AsyncMock(return_value=channel))
        expected_args, expected_kwargs = (emoji,), {}
    elif platform == "signal":
        transport = AsyncMock(return_value={})
        adapter._rpc = transport
        adapter.account = "+15550002222"
        chat = "group:group-id"
        expected_args, expected_kwargs = ("sendReaction", {
            "account": adapter.account, "emoji": emoji, "targetAuthor": routing["user_id"],
            "targetTimestamp": 456, "groupId": "group-id",
        }), {}
    elif platform == "photon":
        transport = AsyncMock(return_value={"ok": True})
        adapter._sidecar_call = transport
        expected_args, expected_kwargs = ("/react", {"spaceId": chat, "messageId": message, "emoji": emoji}), {}
    elif platform == "matrix":
        transport = AsyncMock(return_value="$reaction")
        adapter._client = SimpleNamespace(send_message_event=transport)
        expected_args, expected_kwargs = (chat, module.EventType.REACTION, {
            "m.relates_to": {"rel_type": "m.annotation", "event_id": message, "key": emoji},
        }), {}
    elif platform == "buzz":
        transport = AsyncMock(return_value=(0, "", ""))
        adapter.cli_path = "buzz-cli"
        adapter._run_cli = transport
        expected_args, expected_kwargs = (["reactions", "add", "--event", message, "--emoji", emoji],), {}
    elif platform == "feishu":
        # Keep the optional SDK at its transport boundary, including lazy request construction.
        from unittest.mock import MagicMock
        import sys
        sdk = MagicMock()
        monkeypatch.setitem(sys.modules, "lark_oapi.api.im.v1", sdk)
        transport = AsyncMock(return_value=SimpleNamespace(success=lambda: True, data=SimpleNamespace(reaction_id="reaction")))
        adapter._run_blocking = transport
        api = object()
        adapter._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_reaction=SimpleNamespace(create=api))))
        emoji = "THUMBSUP"
        request = sdk.CreateMessageReactionRequest.builder.return_value.message_id.return_value.request_body.return_value.build.return_value
        expected_args, expected_kwargs = (api, request), {}
    else:
        transport = AsyncMock(return_value=SimpleNamespace(body=SimpleNamespace(success=True)))
        adapter._sdk_call = transport
        adapter._get_access_token = AsyncMock(return_value="test-token")
        adapter._robot_code = "robot"
        api = object()
        adapter._robot_sdk = SimpleNamespace(robot_reply_emotion_with_options_async=api)
        models = SimpleNamespace(**{name: SimpleNamespace for name in (
            "RobotReplyEmotionRequestTextEmotion", "RobotReplyEmotionRequest", "RobotReplyEmotionHeaders",
        )})
        monkeypatch.setattr(module, "dingtalk_robot_models", models, raising=False)
        expected_args, expected_kwargs = None, {}

    assert await adapter.add_reaction(chat, message, emoji, **routing) is True
    if platform == "dingtalk":
        args = transport.await_args.args
        assert args[0] is api
        assert (args[1].open_msg_id, args[1].open_conversation_id, args[1].emotion_name) == (message, chat, emoji)
    else:
        transport.assert_awaited_once_with(*expected_args, **expected_kwargs)
    if platform == "slack":
        adapter._get_client.assert_called_once_with(chat, team_id="T2")
    if platform == "discord":
        adapter._client.fetch_channel.assert_awaited_once_with(789)
        channel.fetch_message.assert_awaited_once_with(456)
    if platform == "feishu":
        sdk.CreateMessageReactionRequest.builder.return_value.message_id.assert_called_once_with(message)
        sdk.CreateMessageReactionRequestBody.builder.return_value.reaction_type.assert_called_once_with({"emoji_type": emoji})

    failures = {
        "telegram": False, "signal": None, "matrix": None,
        "buzz": (1, "", "rejected"), "feishu": SimpleNamespace(success=lambda: False),
        "dingtalk": SimpleNamespace(body=SimpleNamespace(success=False)),
    }
    if platform == "telegram":
        transport.reset_mock()
        assert await adapter.add_reaction(chat, message, "not-a-reaction") is False
        transport.assert_not_awaited()
    if platform in failures:
        transport.return_value = failures[platform]
        assert await adapter.add_reaction(chat, message, emoji, **routing) is False
    transport.side_effect = RuntimeError("transport unavailable")
    assert await adapter.add_reaction(chat, message, emoji, **routing) is False
    if platform == "signal":
        transport.reset_mock()
        assert await adapter.add_reaction(chat, message, emoji) is False
        assert await adapter.add_reaction(chat, "not-a-timestamp", emoji, **routing) is False
        transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_signal_inbound_id_and_author_are_usable_reaction_targets():
    from gateway.config import PlatformConfig
    from gateway.platforms.signal import SignalAdapter

    config = PlatformConfig(extra={"account": "+15550002222"})
    adapter = SignalAdapter(config)
    adapter.handle_message = AsyncMock()
    adapter._rpc = AsyncMock(return_value={"results": [{"type": "SUCCESS"}]})
    sender, timestamp = "+15550001111", 1781234567890
    await adapter._handle_envelope({"envelope": {
        "sourceNumber": sender, "timestamp": timestamp,
        "dataMessage": {"message": "Thanks"},
    }})
    event = adapter.handle_message.await_args.args[0]
    assert (event.message_id, event.source.message_id, event.source.user_id) == (str(timestamp), str(timestamp), sender)
    assert await adapter.add_reaction(event.source.chat_id, event.message_id, "👍", user_id=event.source.user_id) is True
    adapter._rpc.assert_awaited_once_with("sendReaction", {
        "account": adapter.account, "emoji": "👍", "targetAuthor": sender,
        "targetTimestamp": timestamp, "recipient": [sender],
    })
    adapter._rpc.return_value = {"results": [{"type": "UNREGISTERED_FAILURE"}]}
    assert await adapter.add_reaction(event.source.chat_id, event.message_id, "👍", user_id=sender) is False


@pytest.mark.asyncio
async def test_photon_reaction_retains_last_inbound_fallback():
    from plugins.platforms.photon.adapter import PhotonAdapter

    adapter = object.__new__(PhotonAdapter)
    adapter._last_inbound_by_chat = {}
    adapter._sidecar_call = AsyncMock(return_value={"ok": True})
    assert await adapter.add_reaction("+15550001111", "", "👍") is False
    adapter._record_last_inbound("any;-;+15550001111", "latest")
    assert await adapter.add_reaction("+15550001111", "", "👍") is True
    adapter._sidecar_call.assert_awaited_once_with("/react", {
        "spaceId": "+15550001111", "messageId": "latest", "emoji": "👍",
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("emoji, name", [
    ("+1", "+1"), (":pray:", "pray"), ("joy", "joy"),
    ("👍", "thumbsup"), ("🙏", "pray"), ("😂", "joy"), ("❤️", "heart"),
])
async def test_slack_reactions_normalize_names_and_unicode(emoji, name):
    from plugins.platforms.slack.adapter import SlackAdapter

    adapter = object.__new__(SlackAdapter)
    adapter._app = object()
    transport = AsyncMock(return_value={"ok": True})
    adapter._get_client = Mock(return_value=SimpleNamespace(reactions_add=transport))
    assert await adapter.add_reaction("C2", "123.45", emoji, scope_id="T2") is True
    adapter._get_client.assert_called_once_with("C2", team_id="T2")
    transport.assert_awaited_once_with(channel="C2", timestamp="123.45", name=name)
    transport.return_value = {"ok": False}
    assert await adapter.add_reaction("C2", "123.45", emoji, scope_id="T2") is False

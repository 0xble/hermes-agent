from __future__ import annotations

from dataclasses import dataclass

import pytest

import plugins.platforms.telegram.mtproto_telethon as mtproto
from plugins.platforms.telegram.mtproto_telethon import TelethonTelegramUserTransport
from plugins.platforms.telegram.user_transport import (
    TelegramTopicReadbackMismatch,
    TelegramUserTransportConfig,
    TelegramUserTransportError,
)


@dataclass
class _EditForumTopicRequest:
    peer: object
    topic_id: int
    icon_emoji_id: int


@dataclass
class _GetForumTopicsByIDRequest:
    peer: object
    topics: list[int]


class _Messages:
    EditForumTopicRequest = _EditForumTopicRequest
    GetForumTopicsByIDRequest = _GetForumTopicsByIDRequest


class _Functions:
    messages = _Messages


@dataclass
class _Peer:
    user_id: int


@dataclass
class _Topic:
    id: int
    title: str = "Native icons"
    icon_emoji_id: int | None = None
    closed: bool = False
    hidden: bool = False


@dataclass
class _Response:
    topics: list[_Topic]


class _Client:
    def __init__(
        self, *, observed_icon: int | None = None, edit_error=None, land_edit=True
    ):
        self.observed_icon = observed_icon
        self.edit_error = edit_error
        self.land_edit = land_edit
        self.requests = []
        self.connected = False
        self.disconnected = False
        self.call_handler = None

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return type(
            "Me", (), {"id": 101, "username": "tester", "premium": True}
        )()

    async def get_input_entity(self, peer_id):
        return _Peer(peer_id)

    async def __call__(self, request):
        if self.call_handler is not None:
            return await self.call_handler(request)
        return await self._default_call(request)

    async def _default_call(self, request):
        self.requests.append(request)
        if isinstance(request, _EditForumTopicRequest):
            if self.edit_error is not None:
                raise self.edit_error
            if self.land_edit:
                self.observed_icon = request.icon_emoji_id
            return object()
        return _Response([_Topic(request.topics[0], icon_emoji_id=self.observed_icon)])


class _FloodWait(Exception):
    def __init__(self, seconds: float):
        super().__init__("sensitive Telegram detail")
        self.seconds = seconds


def _config(**overrides):
    values = {
        "enabled": True,
        "expected_user_id": 101,
        "require_premium": True,
        "capabilities": ["topic.read", "topic.icon.write"],
        "allowed_bot_peer_ids": [202],
    }
    values.update(overrides)
    return TelegramUserTransportConfig.from_mapping(values)


def _transport(tmp_path, client, **kwargs):
    return TelethonTelegramUserTransport(
        config=_config(),
        active_bot_peer_id=202,
        api_id=123,
        api_hash="secret",
        hermes_home=tmp_path,
        client_factory=lambda *_args, **_kw: client,
        functions_module=_Functions,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_transport_reads_one_exact_topic_with_the_required_raw_request(tmp_path):
    client = _Client(observed_icon=77)
    transport = _transport(tmp_path, client)

    snapshot = await transport.get_topic(peer_id=202, topic_id=303)
    await transport.close()

    assert snapshot.peer_id == 202
    assert snapshot.topic_id == 303
    assert snapshot.icon_emoji_id == 77
    assert isinstance(client.requests[0], _GetForumTopicsByIDRequest)
    assert client.requests[0].topics == [303]
    assert client.disconnected is True


@pytest.mark.asyncio
async def test_failed_post_connect_storage_check_does_not_cache_disconnected_client(
    tmp_path, monkeypatch
):
    class DisconnectAwareClient(_Client):
        async def get_input_entity(self, peer_id):
            if self.disconnected:
                raise RuntimeError("disconnected client reused")
            return await super().get_input_entity(peer_id)

    first = DisconnectAwareClient(observed_icon=77)
    second = DisconnectAwareClient(observed_icon=77)
    clients = iter((first, second))
    real_check = mtproto.ensure_safe_session_storage
    checks = 0

    def fail_third_check(home):
        nonlocal checks
        checks += 1
        if checks == 3:
            raise TelegramUserTransportError("synthetic post-connect safety failure")
        return real_check(home)

    monkeypatch.setattr(mtproto, "ensure_safe_session_storage", fail_third_check)
    transport = TelethonTelegramUserTransport(
        config=_config(),
        active_bot_peer_id=202,
        api_id=123,
        api_hash="secret",
        hermes_home=tmp_path,
        client_factory=lambda *_args, **_kwargs: next(clients),
        functions_module=_Functions,
    )

    with pytest.raises(TelegramUserTransportError, match="post-connect"):
        await transport.get_topic(peer_id=202, topic_id=303)
    snapshot = await transport.get_topic(peer_id=202, topic_id=303)
    await transport.close()

    assert snapshot.icon_emoji_id == 77
    assert first.disconnected is True
    assert second.connected is True


@pytest.mark.asyncio
async def test_fresh_session_resolves_bot_by_username_then_verifies_numeric_id(tmp_path):
    class FreshClient(_Client):
        async def get_input_entity(self, peer_id):
            if isinstance(peer_id, int):
                raise ValueError("numeric peer is not cached")
            assert peer_id == "hermes_bot"
            return _Peer(202)

    transport = _transport(
        tmp_path,
        FreshClient(observed_icon=77),
        active_bot_username="hermes_bot",
    )
    snapshot = await transport.get_topic(peer_id=202, topic_id=303)
    assert snapshot.icon_emoji_id == 77


@pytest.mark.asyncio
async def test_username_fallback_still_rejects_wrong_resolved_peer(tmp_path):
    class FreshClient(_Client):
        async def get_input_entity(self, peer_id):
            del peer_id
            return _Peer(999)

    transport = _transport(
        tmp_path,
        FreshClient(observed_icon=77),
        active_bot_username="hermes_bot",
    )
    with pytest.raises(TelegramUserTransportError, match="did not match"):
        await transport.get_topic(peer_id=202, topic_id=303)


@pytest.mark.asyncio
async def test_transport_edits_then_reads_back_exact_document_id(tmp_path):
    client = _Client()
    transport = _transport(tmp_path, client)

    receipt = await transport.set_topic_icon(
        peer_id=202, topic_id=303, icon_emoji_id=404
    )

    assert [type(request) for request in client.requests] == [
        _GetForumTopicsByIDRequest,
        _EditForumTopicRequest,
        _GetForumTopicsByIDRequest,
    ]
    assert receipt.requested_icon_emoji_id == 404
    assert receipt.observed_icon_emoji_id == 404


@pytest.mark.asyncio
async def test_transport_never_reports_success_before_exact_readback(tmp_path):
    client = _Client(observed_icon=999, land_edit=False)
    transport = _transport(tmp_path, client)

    with pytest.raises(TelegramTopicReadbackMismatch, match="exact readback"):
        await transport.set_topic_icon(peer_id=202, topic_id=303, icon_emoji_id=404)


@pytest.mark.asyncio
async def test_transport_rejects_icon_change_in_its_immediate_prewrite_read(tmp_path):
    client = _Client(observed_icon=None)
    transport = _transport(tmp_path, client)

    with pytest.raises(TelegramTopicReadbackMismatch, match="immediately before"):
        await transport.set_topic_icon(
            peer_id=202,
            topic_id=303,
            icon_emoji_id=404,
            expected_icon_emoji_id=20,
        )
    assert not any(isinstance(request, _EditForumTopicRequest) for request in client.requests)


@pytest.mark.asyncio
async def test_ambiguous_edit_timeout_reads_back_instead_of_blind_retry(tmp_path):
    client = _Client(observed_icon=404, edit_error=TimeoutError("ambiguous secret"))
    transport = _transport(tmp_path, client)

    receipt = await transport.set_topic_icon(
        peer_id=202, topic_id=303, icon_emoji_id=404
    )

    assert receipt.observed_icon_emoji_id == 404
    assert sum(isinstance(r, _EditForumTopicRequest) for r in client.requests) == 1


@pytest.mark.asyncio
async def test_ambiguous_connection_loss_reads_back_instead_of_blind_retry(tmp_path):
    client = _Client(
        observed_icon=404,
        edit_error=ConnectionError("connection lost after write"),
    )
    transport = _transport(tmp_path, client)

    receipt = await transport.set_topic_icon(
        peer_id=202, topic_id=303, icon_emoji_id=404
    )

    assert receipt.observed_icon_emoji_id == 404
    assert sum(isinstance(r, _EditForumTopicRequest) for r in client.requests) == 1


@pytest.mark.asyncio
async def test_bounded_read_flood_wait_sleeps_then_retries_once(tmp_path):
    client = _Client(observed_icon=404)
    calls = 0
    original_call = client._default_call

    async def flood_once(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            client.requests.append(request)
            raise _FloodWait(0.25)
        return await original_call(request)

    client.call_handler = flood_once
    sleeps = []
    transport = _transport(tmp_path, client, sleeper=lambda value: _async_append(sleeps, value))

    snapshot = await transport.get_topic(peer_id=202, topic_id=303)

    assert snapshot.icon_emoji_id == 404
    assert sleeps == [0.25]
    assert sum(isinstance(r, _GetForumTopicsByIDRequest) for r in client.requests) == 2


@pytest.mark.asyncio
async def test_edit_flood_wait_reads_back_before_one_bounded_retry(tmp_path):
    client = _Client(observed_icon=999)
    edit_calls = 0

    async def call(request):
        nonlocal edit_calls
        client.requests.append(request)
        if isinstance(request, _EditForumTopicRequest):
            edit_calls += 1
            if edit_calls == 1:
                raise _FloodWait(0.25)
            client.observed_icon = request.icon_emoji_id
            return object()
        return _Response([_Topic(request.topics[0], icon_emoji_id=client.observed_icon)])

    client.call_handler = call
    sleeps = []
    transport = _transport(tmp_path, client, sleeper=lambda value: _async_append(sleeps, value))

    receipt = await transport.set_topic_icon(
        peer_id=202, topic_id=303, icon_emoji_id=404
    )

    assert receipt.observed_icon_emoji_id == 404
    assert [type(request) for request in client.requests] == [
        _GetForumTopicsByIDRequest,
        _EditForumTopicRequest,
        _GetForumTopicsByIDRequest,
        _EditForumTopicRequest,
        _GetForumTopicsByIDRequest,
    ]
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_excessive_flood_wait_opens_circuit_and_blocks_followup_rpc(tmp_path):
    client = _Client()

    async def always_flood(request):
        client.requests.append(request)
        raise _FloodWait(30)

    client.call_handler = always_flood
    transport = _transport(tmp_path, client)

    with pytest.raises(TelegramUserTransportError, match="FloodWait exceeds"):
        await transport.get_topic(peer_id=202, topic_id=303)
    with pytest.raises(TelegramUserTransportError, match="circuit is open"):
        await transport.get_topic(peer_id=202, topic_id=303)

    assert len(client.requests) == 1
    assert transport.circuit_status()["open"] is True


@pytest.mark.asyncio
async def test_identity_mismatch_fails_before_topic_rpc_and_is_sanitized(tmp_path):
    client = _Client()

    async def wrong_me():
        return type(
            "Me", (), {"id": 999, "username": "private", "premium": True}
        )()

    client.get_me = wrong_me
    transport = _transport(tmp_path, client)

    with pytest.raises(TelegramUserTransportError, match="identity mismatch") as exc:
        await transport.get_topic(peer_id=202, topic_id=303)

    assert "private" not in str(exc.value)
    assert client.requests == []


@pytest.mark.asyncio
async def test_transport_constructs_client_with_updates_disabled(tmp_path):
    captured = {}
    client = _Client()

    def factory(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return client

    transport = TelethonTelegramUserTransport(
        config=_config(),
        active_bot_peer_id=202,
        api_id=123,
        api_hash="secret",
        hermes_home=tmp_path,
        client_factory=factory,
        functions_module=_Functions,
    )
    await transport.identity()

    assert captured["kwargs"]["receive_updates"] is False
    assert "secret" not in repr(transport)


@pytest.mark.asyncio
async def test_adapter_rejects_user_transport_when_bot_identity_does_not_match(tmp_path):
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(
        enabled=True,
        token="redacted",
        extra={
            "user_transport": {
                "enabled": True,
                "expected_user_id": 101,
                "capabilities": ["topic.read"],
                "allowed_bot_peer_ids": [202],
            }
        },
    )
    adapter._telegram_user_transport = None
    adapter._bot = type(
        "Bot", (), {"get_me": lambda self: _async_value(type("Me", (), {"id": 999})())}
    )()

    with pytest.raises(TelegramUserTransportError, match="Bot API identity"):
        await adapter.get_telegram_user_transport(
            active_bot_peer_id=202, hermes_home=tmp_path
        )


async def _async_value(value):
    return value


async def _async_append(items, value):
    items.append(value)

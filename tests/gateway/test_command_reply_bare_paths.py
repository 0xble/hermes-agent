"""A slash-command reply that mentions a local file keeps it as text (no upload).

``/goal resume`` echoes the goal, and goals routinely name their plan or handoff file. The
bare-path detector exists for agent output; running it on gateway-authored command text uploaded
that file as a surprise attachment on every resume, and the attachment failed whenever the chat
was flood-limited ("Couldn't deliver the file attachment").
"""

from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult, as_command_reply
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


class _Adapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False):
        pass

    async def disconnect(self):
        pass

    async def send(self, chat_id, content="", **kwargs):
        return SendResult(success=True, message_id="m-1")

    async def get_chat_info(self, chat_id):
        return {}


def _event():
    return MessageEvent(
        text="/goal resume", message_id="msg-1", message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="u-1"))


async def _deliver(reply: str):
    adapter = _Adapter(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)
    adapter._send_with_retry = AsyncMock(return_value=SendResult(success=True, message_id="s-1"))
    adapter.send_document = AsyncMock(return_value=SendResult(success=True, message_id="d-1"))

    async def _handler(_event):
        return reply

    adapter.set_message_handler(_handler)
    with patch.object(adapter, "_keep_typing", new=AsyncMock()):
        await adapter._process_message_background(_event(), "agent:main:telegram:dm:42")
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("command_reply", [True, False])
async def test_command_reply_mentions_path_without_uploading_it(tmp_path, monkeypatch, command_reply):
    monkeypatch.setenv("HOME", str(tmp_path))
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# handoff\n")
    text = "▶ Goal resumed: finish the migration. Handoff: ~/handoff.md."
    adapter = await _deliver(as_command_reply(text) if command_reply else text)

    sent = adapter._send_with_retry.call_args.kwargs["content"]
    if command_reply:
        assert sent == text  # the path stays readable in the notice
        adapter.send_document.assert_not_called()
    else:  # agent output keeps bare-path auto-delivery
        assert "~/handoff.md" not in sent
        assert adapter.send_document.call_args.kwargs["file_path"] == str(handoff)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["idle", "busy"])
async def test_gateway_dispatch_marks_slash_replies_as_command_replies(path):
    """Both dispatch paths hand the adapter a ``CommandReply``, which is what stops the upload."""
    from types import SimpleNamespace

    from gateway.platforms.base import CommandReply
    from gateway.run import GatewayRunner
    from hermes_cli.commands import resolve_command

    runner = object.__new__(GatewayRunner)
    reply = "▶ Goal resumed: see ~/handoff.md"
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42")
    event = SimpleNamespace(text="/goal resume", get_command=lambda: "goal")

    async def _resolve(event, source, qk):
        return False, None, "goal", "goal"

    async def _canonical(event, source, qk, canonical):
        return True, reply

    async def _busy(event, cmd_def, qk, source):
        return reply

    runner._hm_resolve_command = _resolve
    runner._hm_dispatch_canonical_command = _canonical
    runner._check_slash_access = lambda source, name: None
    runner._dispatch_busy_slash_command = _busy
    if path == "idle":
        handled, result = await runner._hm_dispatch_idle_commands(event, source, "qk")
    else:
        assert resolve_command("goal") is not None
        handled, result = await runner._hm_busy_slash_or_photo(event, source, "qk")
    assert handled and result == reply and isinstance(result, CommandReply)

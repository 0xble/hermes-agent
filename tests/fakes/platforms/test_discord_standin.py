"""The Discord fake must not stall the real SDK after a cancelled progress send.

This lives beside the fake, outside tests/e2e and tests/gateway, whose conftests replace
`discord` with a mock module; it must drive the real discord.py HTTP client (a pinned
dependency), and fails loudly rather than skipping if a mock ever shadows it.
"""

import asyncio
from contextlib import suppress

import discord
import discord.http as discord_http
import pytest

from tests.fakes.platforms.discord_standin import DiscordStandin, TOKEN


@pytest.mark.asyncio
@pytest.mark.parametrize("preceding_forbidden", [False, True])
async def test_cancelled_progress_does_not_stall_next_send(monkeypatch, preceding_forbidden):
    standin = DiscordStandin()
    standin.start()
    monkeypatch.setattr(discord_http.Route, "BASE", standin.api_base)
    client = discord_http.HTTPClient(asyncio.get_running_loop())
    try:
        await client.static_login(TOKEN)
        channel = await client.start_private_message(111)
        route = discord_http.Route("POST", "/channels/{channel_id}/messages", channel_id=channel["id"])
        if preceding_forbidden:
            standin.fail("create_message", {"message": "Missing Access", "code": 50001}, status=403)
            with pytest.raises(discord.Forbidden):
                await client.request(route, json={"content": "rejected"})

        parse = discord_http.json_or_text
        cancelled = asyncio.Event()

        async def cancel_after_progress_response(response):
            result = await parse(response)
            if isinstance(result, dict) and result.get("content") == "progress":
                # Turn cleanup can cancel the progress sender after Discord accepted it but
                # before the SDK's rate-limit context exits. Schedule that exact interleaving,
                # without replacing the SDK's real bucket, HTTP request, or cancellation.
                asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
                cancelled.set()
            return result

        monkeypatch.setattr(discord.http, "json_or_text", cancel_after_progress_response)
        progress = asyncio.create_task(client.request(route, json={"content": "progress"}))
        with suppress(asyncio.CancelledError):
            await progress
        assert cancelled.is_set()
        final = await asyncio.wait_for(client.request(route, json={"content": "final"}), timeout=2)
        assert final["content"] == "final"
        assert [call.params["content"] for call in standin.calls_of("create_message") if not call.faulted] == [
            "progress", "final",
        ]
    finally:
        await client.close()
        standin.stop()

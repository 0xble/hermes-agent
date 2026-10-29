"""Background completions keep attribution and content in the same delivery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _runner(adapter, body):
    return SimpleNamespace(
        _delivery_adapter_for=lambda source: adapter,
        _thread_metadata_for_source=lambda *args: {"thread_id": "1.0"},
        _resolve_session_agent_runtime_async=AsyncMock(return_value=("test", {"api_key": "test"})),
        _resolve_turn_toolsets=lambda *args: ([], None),
        _provider_routing={},
        _resolve_session_reasoning_config=lambda **kwargs: None,
        _resolve_session_service_tier=lambda **kwargs: None,
        _resolve_turn_agent_config=lambda *args: {"model": "test", "runtime": {}},
        _run_in_executor_with_context=AsyncMock(return_value={"final_response": body, "messages": []}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_succeeds", [True, False], ids=["concurrent", "failed-header"])
async def test_concurrent_background_results_never_deliver_unattributed_bodies(monkeypatch, delivery_succeeds):
    from agent.i18n import t

    monkeypatch.setattr("gateway.run._load_gateway_config", dict)
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 1)
    source = SessionSource(platform=Platform.SLACK, chat_id="C1", thread_id="1.0")
    attempts = []
    both_started = asyncio.Event()

    async def send(*, chat_id, content, metadata):
        attempts.append(content)
        if len(attempts) == 2:
            both_started.set()
        await both_started.wait()
        return SimpleNamespace(success=delivery_succeeds)

    adapter = SimpleNamespace(
        send=send,
        extract_media=lambda text: ([], text),
        extract_images=lambda text: ([], text),
        supports_copy_blocks=MagicMock(return_value=False),
    )
    await asyncio.wait_for(asyncio.gather(*(
        GatewayRunner._run_background_task_inner(_runner(adapter, body), prompt, source, prompt)
        for prompt, body in [("task A", "answer A"), ("task B", "answer B")]
    )), timeout=5)

    # The barrier forces header A, header B, body A, body B on the broken producer.
    # A rejected send must not trigger a second, bare-body delivery either.
    assert attempts == [
        t("gateway.background.complete_header", preview="task A") + "answer A",
        t("gateway.background.complete_header", preview="task B") + "answer B",
    ]

"""The real PTB-backed TelegramApplication marks the task running each update as the ingress consumer."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("telegram.ext", reason="python-telegram-bot not installed")

from gateway.platforms.base import in_ingress_consumer  # noqa: E402
from plugins.platforms.telegram.update_admission import TelegramApplication  # noqa: E402


@pytest.mark.asyncio
async def test_update_processing_runs_on_a_marked_consumer_and_spawned_work_is_not():
    seen = []

    async def handle(update):
        seen.append(in_ingress_consumer())

        async def child():
            return in_ingress_consumer()

        seen.append(await asyncio.get_running_loop().create_task(child()))

    application = SimpleNamespace(_process_update_on_consumer=handle)
    await TelegramApplication.process_update(application, object())
    assert seen == [True, False]
    assert not in_ingress_consumer()


@pytest.mark.asyncio
async def test_marker_is_cleared_when_processing_raises():
    async def boom(update):
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError):
        await TelegramApplication.process_update(SimpleNamespace(_process_update_on_consumer=boom), object())
    assert not in_ingress_consumer()

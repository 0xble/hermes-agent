"""Real owner-loop scheduling, retry receipts and serialized Telegram title requests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.async_utils import safe_schedule_threadsafe
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def runner_and_source():
    runner = object.__new__(GatewayRunner)
    runner._is_telegram_topic_lane = lambda source: True
    runner._telegram_topic_is_stale = lambda source, **kwargs: False
    runner._telegram_topic_auto_rename_disabled = lambda source: False
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="7")
    return runner, source


def capture_futures(monkeypatch):
    futures = []

    def schedule(*args, **kwargs):
        future = safe_schedule_threadsafe(*args, **kwargs)
        futures.append(future)
        return future

    monkeypatch.setattr("gateway.run.safe_schedule_threadsafe", schedule)
    return futures


@pytest.mark.parametrize("missing_loop", ["missing", "closed"])
def test_no_usable_loop_does_not_poison_title_retry(monkeypatch, missing_loop):
    runner, source = runner_and_source()
    runner._rename_telegram_topic_for_session_title = AsyncMock(return_value=True)
    if missing_loop == "closed":
        loop = asyncio.new_event_loop()
        loop.close()
        runner._gateway_loop = loop
    runner._schedule_telegram_topic_title_rename(source, "session", "Title")
    assert not getattr(runner, "_telegram_topic_title_requests", {})
    futures = capture_futures(monkeypatch)

    async def retry():
        runner._gateway_loop = asyncio.get_running_loop()
        runner._schedule_telegram_topic_title_rename(source, "session", "Title")
        await asyncio.wrap_future(futures[-1])

    asyncio.run(retry())
    runner._rename_telegram_topic_for_session_title.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, None, "exception", "schedule", "cancel"])
async def test_failed_title_request_retries_but_success_deduplicates(monkeypatch, failure):
    runner, source = runner_and_source()
    runner._gateway_loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    calls = []

    async def rename(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 1:
            if failure == "exception":
                raise OSError("transient")
            if failure == "cancel":
                entered.set()
                await asyncio.Event().wait()
            if failure in (False, None):
                return failure
        return True

    runner._rename_telegram_topic_for_session_title = rename
    futures = capture_futures(monkeypatch)
    if failure == "schedule":
        def reject(coro, *args, **kwargs):
            coro.close()
            return None
        monkeypatch.setattr("gateway.run.safe_schedule_threadsafe", reject)
        runner._schedule_telegram_topic_title_rename(source, "session", "Title")
        assert not getattr(runner, "_telegram_topic_title_requests", {})
        futures = capture_futures(monkeypatch)
    else:
        runner._schedule_telegram_topic_title_rename(source, "session", "Title")
        if failure == "cancel":
            await asyncio.wait_for(entered.wait(), 2)
            futures[-1].cancel()
        try:
            await asyncio.wrap_future(futures[-1])
        except (OSError, asyncio.CancelledError):
            pass
        for _ in range(100):
            if not next(iter(runner._telegram_topic_title_requests.values())).pending:
                break
            await asyncio.sleep(.001)
    runner._schedule_telegram_topic_title_rename(source, "session", "Title")
    await asyncio.wrap_future(futures[-1])
    runner._schedule_telegram_topic_title_rename(source, "session", "Title")
    await asyncio.wrap_future(futures[-1])
    assert calls == ["Title"] * (1 if failure == "schedule" else 2)
    state = next(iter(runner._telegram_topic_title_requests.values()))
    assert state.pending is None and state.confirmed == ("session", "Title")


@pytest.mark.asyncio
@pytest.mark.parametrize("last_title,first_succeeds", [("A", True), ("A", False), ("C", True)])
async def test_latest_title_serializes_and_old_completion_cannot_drop_new_request(monkeypatch, last_title, first_succeeds):
    runner, source = runner_and_source()
    owner_loop = asyncio.get_running_loop()
    runner._gateway_loop = owner_loop
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    active = 0

    async def rename(_source, _session, title, **kwargs):
        nonlocal active
        assert asyncio.get_running_loop() is owner_loop
        active += 1
        assert active == 1
        calls.append(title)
        try:
            if len(calls) == 1:
                entered.set()
                await release.wait()
                return first_succeeds
            return True
        finally:
            active -= 1

    runner._rename_telegram_topic_for_session_title = rename
    futures = capture_futures(monkeypatch)
    # Call from a foreign running event loop, as well as ordinary title threads.
    def foreign_request(title):
        async def invoke():
            runner._schedule_telegram_topic_title_rename(source, "session", title)
        asyncio.run(invoke())
    await asyncio.to_thread(foreign_request, "A")
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.to_thread(runner._schedule_telegram_topic_title_rename, source, "session", "A")
    await asyncio.wrap_future(futures[-1])  # Duplicate in-flight request.
    for title in ("B", last_title):
        await asyncio.to_thread(runner._schedule_telegram_topic_title_rename, source, "session", title)
        for _ in range(100):
            if next(iter(runner._telegram_topic_title_requests.values())).pending[2] == title:
                break
            await asyncio.sleep(.001)
    release.set()
    await asyncio.wait_for(asyncio.gather(*(asyncio.wrap_future(future) for future in futures)), 2)
    assert calls == (["A"] if last_title == "A" and first_succeeds else ["A", last_title])
    state = next(iter(runner._telegram_topic_title_requests.values()))
    assert state.confirmed == ("session", last_title)
    assert state.pending is None


@pytest.mark.asyncio
async def test_title_dedup_is_scoped_to_the_profile_bot(monkeypatch):
    runner, source = runner_and_source()
    runner._gateway_loop = asyncio.get_running_loop()
    runner._rename_telegram_topic_for_session_title = AsyncMock(return_value=True)
    futures = capture_futures(monkeypatch)
    for profile in (None, "work"):
        source.profile = profile
        runner._schedule_telegram_topic_title_rename(source, "same-session", "Title")
        await asyncio.wrap_future(futures[-1])
    assert runner._rename_telegram_topic_for_session_title.await_count == 2


@pytest.mark.asyncio
async def test_real_telegram_rename_failure_is_retryable_and_success_is_confirmed(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter
    runner, source = runner_and_source()
    runner._gateway_loop = asyncio.get_running_loop()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._bot = SimpleNamespace(edit_forum_topic=AsyncMock(side_effect=[OSError("offline"), True]))
    runner._adapter_for_source = lambda source: adapter
    runner._select_telegram_topic_icon_id = AsyncMock(return_value=None)
    futures = capture_futures(monkeypatch)
    for _ in range(3):
        runner._schedule_telegram_topic_title_rename(source, "session", "Title")
        await asyncio.wrap_future(futures[-1])
    assert adapter._bot.edit_forum_topic.await_count == 2
    assert next(iter(runner._telegram_topic_title_requests.values())).confirmed == ("session", "Title")


@pytest.mark.asyncio
@pytest.mark.parametrize('same_title,outcome', [(False, True), (False, False), (False, None), (True, True), (True, False)])
async def test_manual_title_joins_background_order_and_awaits_wire_result(tmp_path, monkeypatch, same_title, outcome):
    from unittest.mock import MagicMock
    from hermes_state import AsyncSessionDB, SessionDB
    from gateway.platforms.event import MessageEvent

    runner, source = runner_and_source()
    runner._gateway_loop = asyncio.get_running_loop()
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('session', 'telegram')
    runner._session_db = AsyncSessionDB(db)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(session_id='session')
    entered, release = asyncio.Event(), asyncio.Event()
    wire = []

    async def select_icon(adapter, src, title, *args):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return None

    async def rename_topic(chat_id, thread_id, name, **kwargs):
        wire.append(name)
        if outcome is False:
            raise OSError('rename refused')

    # Actual gateway rename helper uses the fake adapter only at its transport boundary.
    class Adapter:
        rename_dm_topic = staticmethod(rename_topic)

    runner._adapter_for_source = lambda src: Adapter()
    runner._select_telegram_topic_icon_id = select_icon
    runner._telegram_topic_auto_rename_disabled = lambda src: outcome is None and release.is_set()
    runner._telegram_topic_extra = lambda *args: {}
    futures = capture_futures(monkeypatch)
    runner._schedule_telegram_topic_title_rename(source, 'session', 'Auto')
    await asyncio.wait_for(entered.wait(), 2)
    manual = 'Auto' if same_title else 'Manual'
    command = asyncio.create_task(runner._handle_title_command(MessageEvent(source=source, text='/title ' + manual)))
    for _ in range(2000):
        state = next(iter(runner._telegram_topic_title_requests.values()))
        if state.pending and state.pending[2] == manual and db.get_session_title('session') == manual:
            break
        await asyncio.sleep(.001)
    assert not command.done()
    release.set()
    response = await asyncio.wait_for(command, 2)
    await asyncio.wrap_future(futures[0])
    assert db.get_session_title('session') == manual
    assert ('topic name was not changed' in response) is (outcome is False)
    if outcome is True:
        assert wire[-1] == manual
        assert next(iter(runner._telegram_topic_title_requests.values())).confirmed == ('session', manual)
        assert wire == (['Auto'] if same_title else ['Auto', 'Manual'])
    db.close()

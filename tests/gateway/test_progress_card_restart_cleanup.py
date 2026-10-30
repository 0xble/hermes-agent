"""Temporary progress cards survive a restart only as a durable record that boot deletes.

End-of-turn deletion is a post-delivery callback held in process memory. A restart before it
fires used to strand every card the turn posted. Each card is now recorded on the session's
routing entry as it is sent, cleared when cleanup runs, and deleted at the next boot when the
process that recorded it is gone.
"""

import asyncio

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from tests.gateway.test_run_cleanup_progress import (
    CleanupCaptureAdapter,
    ProgressAgent,
    _fire_post_delivery_cb,
    _install_fakes,
    _make_runner,
)


class FailingProgressAgent(ProgressAgent):
    def run_conversation(self, message, conversation_history=None, task_id=None, **_kwargs):
        super().run_conversation(message, conversation_history, task_id)
        return {"final_response": "", "messages": [], "api_calls": 1, "failed": True, "error": "provider down"}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))


SOURCE = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", user_id="u1")


def _attach_store(runner, store, adapter):
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._background_tasks = set()
    runner._delivery_adapter_for = lambda _source: adapter


async def _settle(runner):
    """Let the off-loop record/forget writes the turn spawned finish."""
    for _ in range(50):
        pending = [task for task in runner._background_tasks if not task.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


async def _run_turn(monkeypatch, tmp_path, agent_cls):
    """One real turn with cleanup_progress on, against a real store. Returns (store, adapter, key, result)."""
    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, agent_cls, cleanup_on=True)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    entry = store.get_or_create_session(SOURCE)
    _attach_store(runner, store, adapter)
    result = await runner._run_agent(
        message="hello", context_prompt="", history=[], source=SOURCE,
        session_id=entry.session_id, session_key=entry.session_key,
    )
    await _settle(runner)
    return runner, store, adapter, entry.session_key, result


async def _reboot_and_sweep(monkeypatch, tmp_path):
    """A new process over the same state: the recording process is gone, then boot sweeps."""
    import gateway.status as status
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: False)
    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    _attach_store(runner, store, adapter)
    await runner._sweep_orphan_progress_cards()
    await _settle(runner)
    return store, adapter


def _cards(store, key):
    return [card["message_id"] for card in store._entries[key].progress_cards]


@pytest.mark.asyncio
async def test_card_stranded_by_a_restart_is_deleted_at_boot(monkeypatch, tmp_path):
    _runner, store, adapter, key, result = await _run_turn(monkeypatch, tmp_path, ProgressAgent)
    assert result["final_response"] == "done"
    progress_ids = [sent["message_id"] for sent in adapter.sent]
    assert progress_ids, "the turn posted no progress card"
    # Restart before the post-delivery callback fires: nothing deleted, the record survives.
    assert adapter.deleted == []
    assert _cards(store, key) == progress_ids

    rebooted, boot_adapter = await _reboot_and_sweep(monkeypatch, tmp_path)

    assert [d["message_id"] for d in boot_adapter.deleted] == progress_ids
    assert all(d["chat_id"] == SOURCE.chat_id for d in boot_adapter.deleted)
    assert _cards(rebooted, key) == []
    # The sweep is idempotent: a second boot finds nothing to delete.
    _again, second = await _reboot_and_sweep(monkeypatch, tmp_path)
    assert second.deleted == []


@pytest.mark.asyncio
async def test_normal_cleanup_clears_the_record_so_boot_does_not_delete_twice(monkeypatch, tmp_path):
    runner, store, adapter, key, _result = await _run_turn(monkeypatch, tmp_path, ProgressAgent)
    assert _cards(store, key)

    await _fire_post_delivery_cb(adapter.pop_post_delivery_callback(key))
    await _settle(runner)

    assert adapter.deleted and _cards(store, key) == []
    _rebooted, boot_adapter = await _reboot_and_sweep(monkeypatch, tmp_path)
    assert boot_adapter.deleted == []


@pytest.mark.asyncio
async def test_boot_sweep_never_touches_the_final_reply(monkeypatch, tmp_path):
    _runner, store, adapter, key, result = await _run_turn(monkeypatch, tmp_path, ProgressAgent)
    progress_ids = {sent["message_id"] for sent in adapter.sent}
    # The adapter handler delivers the final reply; the restart lands before cleanup fires.
    final = await adapter.send(SOURCE.chat_id, result["final_response"])

    _rebooted, boot_adapter = await _reboot_and_sweep(monkeypatch, tmp_path)

    deleted = {d["message_id"] for d in boot_adapter.deleted}
    assert final.message_id not in deleted
    assert deleted == progress_ids


@pytest.mark.asyncio
async def test_failed_run_keeps_its_cards_across_a_restart(monkeypatch, tmp_path):
    """Failed runs keep their progress as breadcrumbs; boot must not delete them either."""
    _runner, store, adapter, key, result = await _run_turn(monkeypatch, tmp_path, FailingProgressAgent)
    assert result.get("failed") and adapter.sent
    assert _cards(store, key) == []

    _rebooted, boot_adapter = await _reboot_and_sweep(monkeypatch, tmp_path)
    assert boot_adapter.deleted == []


@pytest.mark.asyncio
async def test_boot_leaves_cards_of_a_live_owner(monkeypatch, tmp_path):
    """A zero-cut predecessor still finishing its turn owns its cards' cleanup."""
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    entry = store.get_or_create_session(SOURCE)
    assert store.record_progress_card(entry.session_key, SOURCE.chat_id, "77")
    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    _attach_store(runner, SessionStore(tmp_path / "sessions", GatewayConfig()), adapter)

    await runner._sweep_orphan_progress_cards()

    assert adapter.deleted == []

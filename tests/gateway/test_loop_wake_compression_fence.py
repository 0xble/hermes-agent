"""Native loop wake fences across session moves during turn preparation.

The wake carries the real admission fence (``_gateway_loop_execution_fence`` +
``gateway_session_key``) and runs through real admission and the turn handler.
Pre-turn hygiene compression carries the started tick to the child, which the
fence follows. An unrelated route (a /resume elsewhere) defers and a pause blocks.
"""
import asyncio
import logging

import pytest

from hermes_cli import goals, loops
from tests.gateway import test_loop_interruption_recovery as recovery
from tests.gateway.test_loop_interruption_recovery import create_loop, native_runner, scan

control_home = recovery.control_home


def _tick(state):
    return state.created_at, state.ticks_fired


def _wire(runner, entry, during_prepare):
    """Run the wake through real admission; ``during_prepare`` mutates state inside preparation."""
    executions, prepared = [], []

    async def prepare(event, src, session_entry, session_key, quick_key, run_generation):
        await during_prepare(session_entry, session_key)
        prepared.append(session_entry.session_id)
        return runner._PreparedTurn([], "", event.text, event.text, None, None), {}

    async def model(**kwargs):
        executions.append(kwargs["session_id"])
        return {"final_response": "checked", "messages": []}

    runner._hmwa_prepare_turn = prepare
    runner._run_agent = model
    return executions, prepared


async def _fire(runner, adapter, entry):
    adapter.set_message_handler(runner._handle_admitted_message)
    await scan(runner, entry.session_id)
    await asyncio.gather(*adapter._background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_hygiene_rotation_runs_and_settles_tick_once_on_child(control_home):
    runner, adapter, source, entry = native_runner(control_home)
    parent, child = entry.session_id, entry.session_id + "-child"
    create_loop(entry, source)
    settles = []
    original_complete = loops.LoopManager.complete_tick

    def complete_tick(self, *args, **kwargs):
        settles.append(self.session_id)
        return original_complete(self, *args, **kwargs)

    async def rotate(session_entry, session_key):
        def compress():
            db = goals._get_session_db()
            db.end_session(parent, "compression")
            db.create_session(child, "telegram", parent_session_id=parent)
            assert loops.migrate_loop_to_session(parent, child, reason="compression")
        await asyncio.to_thread(compress)
        session_entry.session_id = child
        runner.session_store._entries[session_key].session_id = child

    executions, prepared = _wire(runner, entry, rotate)
    loops.LoopManager.complete_tick = complete_tick
    try:
        await _fire(runner, adapter, entry)
        assert prepared == [child]
        assert executions == [child], "the hygiene-rotated loop wake never reached the model"
        assert settles == [child]
        saved = loops.load_loop(child)
        assert saved.status == "active" and not saved.awaiting_response and saved.ticks_fired == 1
        assert loops.load_loop(parent).status == "cleared"
    finally:
        loops.LoopManager.complete_tick = original_complete
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_resume_elsewhere_during_preparation_defers_without_settling(control_home, caplog):
    caplog.set_level(logging.INFO, logger="gateway.run_loop_wake")
    runner, adapter, source, entry = native_runner(control_home)
    parent = entry.session_id
    create_loop(entry, source)

    async def resume_elsewhere(session_entry, session_key):
        await asyncio.to_thread(goals._get_session_db().create_session, "unrelated", "telegram")
        switched = await runner.async_session_store.switch_session(session_key, "unrelated")
        assert switched.session_id == "unrelated"

    executions, prepared = _wire(runner, entry, resume_elsewhere)
    try:
        await _fire(runner, adapter, entry)
        assert prepared == [parent]
        assert executions == []
        saved = loops.load_loop(parent)
        assert saved.status == "active" and saved.awaiting_response and saved.claim_phase == "started"
        assert saved.ticks_fired == 1
        assert loops.load_loop("unrelated") is None
        # The suppressed exit names the session, tick and reason for operators.
        assert (f"Discarding loop wake for session {parent} tick {_tick(saved)}: "
                "superseded during turn preparation") in caplog.text
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_pause_during_preparation_blocks_the_model_run(control_home):
    runner, adapter, source, entry = native_runner(control_home)
    parent = entry.session_id
    create_loop(entry, source)

    async def pause(session_entry, session_key):
        await asyncio.to_thread(lambda: loops.LoopManager(parent).pause())

    executions, prepared = _wire(runner, entry, pause)
    try:
        await _fire(runner, adapter, entry)
        assert prepared == [parent]
        assert executions == []
        assert loops.load_loop(parent).status == "paused"
    finally:
        runner.session_store.close_all_db_handles()

"""An authorized resume pins its session until the viewer has attached."""
import asyncio
import threading

import pytest

from gateway.session_api_turn import admit_api_turn, observe_api_turn
from gateway.session_contract import SessionRef
from gateway.session_controls import AuthorityConnection


@pytest.mark.asyncio
async def test_resume_keeps_api_session_live_during_history_read(api, owner, monkeypatch):
    from gateway import session_local_recovery, session_viewer_close
    from gateway.session_results import execution_result

    async def answer(event):
        result = execution_result.get()
        assert result is not None
        result['result'] = {'final_response': 'ok'}
        return 'ok'

    owner.runner._handle_message = answer
    first = admit_api_turn(api, session_id='resume-race', user_message='one', conversation_history=[])
    ref = SessionRef('default', 'resume-race')
    live = owner.sessions[ref.session_id]
    busy_entered, busy_release = threading.Event(), threading.Event()
    history_entered, history_release = threading.Event(), threading.Event()
    original_target = session_local_recovery.transcript_target

    original_busy = session_viewer_close._busy

    def held_busy(*args):
        busy = original_busy(*args)
        busy_entered.set()
        assert busy_release.wait(10), 'the idle snapshot was not released'
        return busy

    def held_target(authority, ref):
        history_entered.set()
        assert history_release.wait(10), 'the history read was not released'
        return original_target(authority, ref)

    monkeypatch.setattr(session_viewer_close, '_busy', held_busy)
    monkeypatch.setattr(session_local_recovery, 'transcript_target', held_target)
    connection = AuthorityConnection(owner, object(), {
        'user_id': 'resume-client', 'profile_id': 'default', 'instance_id': 'first',
        'capabilities': ['session:read']})
    resume = None
    try:
        await asyncio.wait_for(observe_api_turn(first), 10)
        assert await asyncio.to_thread(busy_entered.wait, 10), 'retirement did not reach its busy read'
        retiring = live.task
        resume = asyncio.create_task(connection.resume(ref, {}))
        assert await asyncio.to_thread(history_entered.wait, 10), 'resume did not reach its history read'
        busy_release.set()
        await asyncio.wait_for(asyncio.shield(retiring), 10)
        history_release.set()
        snapshot = await asyncio.wait_for(resume, 10)
        assert owner.sessions[ref.session_id] is live
        assert snapshot['session_id'] == ref.session_id
        assert live.attaching == 0
    finally:
        busy_release.set()
        history_release.set()
        tasks = [task for task in (live.task, resume) if task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)

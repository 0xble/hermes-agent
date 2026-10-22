"""Local automation reads must leave the loop free and retain route authority."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway.session_authority import SessionAuthority
from gateway.session_contract import SessionRef
from hermes_state_runtime import RuntimeStoreError


@pytest.mark.asyncio
@pytest.mark.parametrize(('change', 'reason'), [
    (None, None),
    ('route', 'admission_conflict'),
    ('replacement', 'admission_conflict'),
    ('draining', 'runtime_draining'),
])
async def test_local_automation_reauthorization_rechecks_after_worker(monkeypatch, change, reason):
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    reads = []

    def read(authority, ref, row, *, route):
        reads.append((threading.get_ident(), route))
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr('gateway.session_automation.check_local_automation', read)
    ref = SessionRef('profile', 'session')
    live = SimpleNamespace(route='route')
    authority = SessionAuthority.__new__(SessionAuthority)
    authority.sessions = {ref.session_id: live}
    authority._require_admission_open = lambda *, queue_only=False: None
    task = asyncio.create_task(authority._check_local_automation(ref, {}, route='route'))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
        assert not task.done()
        if change == 'route':
            live.route = 'other'
        elif change == 'replacement':
            authority.sessions[ref.session_id] = SimpleNamespace(route='route')
        elif change == 'draining':
            def refused(*, queue_only=False):
                raise RuntimeStoreError('runtime_draining')
            authority._require_admission_open = refused
    finally:
        release.set()
    if reason is None:
        await task
    else:
        with pytest.raises(RuntimeStoreError) as error:
            await task
        assert error.value.reason == reason
    assert len(reads) == 1
    assert reads[0][0] != loop_thread
    assert reads[0][1] == 'route'

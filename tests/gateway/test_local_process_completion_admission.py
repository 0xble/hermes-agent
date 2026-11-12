"""A desktop steer backgrounds a terminal and the watcher must start a turn.

The completion source is rebuilt from the session origin, then copied again
to drop a stale reply anchor. Local admission accepts only the registered
live object, so that copy used to raise permission_denied and the refusal
was logged as internal. No model turn started, and the chat never saw the
notification.
"""

import pytest

from gateway.session_contract import Principal
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_automation_notification_category import _authority


@pytest.mark.asyncio
@pytest.mark.parametrize('message_id', [None, 'stale-user-message'])
async def test_local_completion_admits_when_the_origin_is_rebuilt(tmp_path, monkeypatch, message_id):
    from gateway.session_local import LocalSessionAdapter, create_local_session
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    runner, authority = await _authority(tmp_path, monkeypatch)
    actor = Principal(
        'test-owner', 'default', frozenset({'session:create', 'session:submit', 'session:read'}), 'fixture')
    token = set_hermes_home_override(tmp_path)
    monkeypatch.setattr('gateway.run._hermes_home', tmp_path)
    try:
        ref = create_local_session(
            authority, actor, dict(request_id='desk', source='gui', model='fixture', toolsets=[]))
        live = authority.sessions[ref.session_id]
        # The persisted origin carries the message that started the process.
        # Clearing that anchor must not fork the object local admission accepts.
        from gateway.session_identity import replace_source
        runner.session_store._entries[live.route].origin = replace_source(
            live.source, message_id=message_id)
        # Rebinding is private to this internal producer. Public local
        # admission must still reject copies, changed owners and dead sources.
        adapter = runner._adapter_for_source(live.source)
        assert isinstance(adapter, LocalSessionAdapter)
        copied = replace_source(live.source, message_id=message_id)
        assert not adapter.authorize_source(copied)
        assert runner._bind_registered_local_source(adapter, copied) is live.source
        assert runner._bind_registered_local_source(
            adapter, replace_source(copied, user_id='foreign-owner')) is None
        injected = await runner._inject_watch_notification(
            '[IMPORTANT: Background process proc-1 exited (exit code 0).]',
            {
                'type': 'completion',
                'session_key': live.route,
                'parent_session_id': ref.session_id,
                'session_id': 'proc-1',
                'started_at': 1_700_000_000,
                'output': 'core-ran',
                'exit_code': 0,
            },
            raise_not_accepted=True,
        )
        assert injected is True
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        assert rows, 'the completion never entered the session FIFO'
        assert live.source.message_id is None
        authority.sessions.pop(ref.session_id)
        assert runner._bind_registered_local_source(adapter, copied) is None
    finally:
        reset_hermes_home_override(token)
        authority.db.close()

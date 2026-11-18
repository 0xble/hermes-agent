"""A trusted process notice borrows committed source, not stale origin display data."""

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session_identity import replace_source
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_automation_notification_category import _SOURCE, _authority


@pytest.mark.asyncio
async def test_process_notice_refuses_stale_origin_on_a_per_user_route(tmp_path, monkeypatch):
    from gateway.session_ingress_context import native_callback, register_transport_home
    from plugins.platforms.discord.adapter import DiscordAdapter

    runner, authority = await _authority(tmp_path, monkeypatch)
    runner.config.group_sessions_per_user = True
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False))
    runner.adapters = {Platform.DISCORD: adapter}
    runner._wire_adapter_handlers(adapter)
    register_transport_home(runner, None, tmp_path)
    monkeypatch.setenv('DISCORD_ALLOW_ALL_USERS', 'true')
    group = {**_SOURCE, 'chat_type': 'group'}
    source = adapter.build_source(**group)
    human = MessageEvent(text='human', source=adapter.build_source(**group, message_id='human-1'),
                         message_id='human-1')
    try:
        with native_callback(runner, human, tmp_path):
            await authority.admit_native(human)
        route = runner.session_store._generate_session_key(source)
        sid = runner.session_store.peek_session_id(route)
        runner.session_store._entries[route].origin = replace_source(source, user_id='other-speaker')
        before = list_session_admissions(authority.db, session_id=authority.logical_owner(sid),
                                         pending_only=False)
        evt = {'type': 'completion', 'session_id': 'proc_wrong_user', 'session_key': route,
               'platform': 'discord', 'chat_id': '42', 'chat_type': 'group', 'exit_code': 0,
               'output': 'done', 'started_at': 2.0, 'parent_session_id': sid}
        assert await runner._inject_watch_notification('[done]', evt) is False
        assert list_session_admissions(authority.db, session_id=authority.logical_owner(sid),
                                       pending_only=False) == before
    finally:
        authority.db.close()
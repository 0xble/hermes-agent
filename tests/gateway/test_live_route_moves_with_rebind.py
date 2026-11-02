"""A conversation rebound to another lane keeps answering there without a restart.

The drain's route preflight (``check_native_route``) compares each queued input's route with the
live entry's source. A live entry that kept the old lane's source after a rebind refused every
input on the new lane with ``admission_conflict`` and paused the FIFO with a "will not be
answered here" notice, until a restart rebuilt the entry."""
from types import SimpleNamespace

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_authority import LiveSession, SessionAuthority


def _authority():
    return SimpleNamespace(sessions={}, profile_id='p', _require_admission_open=lambda: None)


def test_rebind_to_another_lane_moves_the_live_entry():
    authority = _authority()
    slack = SessionSource(platform=Platform.SLACK, chat_id='C1', thread_id='1.2', chat_type='group')
    telegram = SessionSource(platform=Platform.TELEGRAM, chat_id='42', thread_id='7', chat_type='dm')
    SessionAuthority._publish_route(authority, slack, 'sid', 'agent:main:slack:group:C1:1.2')
    live = authority.sessions['sid']
    SessionAuthority._publish_route(authority, telegram, 'sid', 'agent:main:telegram:dm:42:7')
    assert authority.sessions['sid'] is live  # same FIFO, waiters and stream
    assert live.route == 'agent:main:telegram:dm:42:7' and live.source is telegram


def test_same_lane_keeps_its_live_source():
    authority = _authority()
    first = SessionSource(platform=Platform.TELEGRAM, chat_id='42', thread_id='7', chat_type='dm')
    again = SessionSource(platform=Platform.TELEGRAM, chat_id='42', thread_id='7', chat_type='dm', user_name='B')
    SessionAuthority._publish_route(authority, first, 'sid', 'agent:main:telegram:dm:42:7')
    SessionAuthority._publish_route(authority, again, 'sid', 'agent:main:telegram:dm:42:7')
    assert authority.sessions['sid'].source is first

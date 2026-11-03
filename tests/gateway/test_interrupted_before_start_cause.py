"""A turn interrupted before its first model call names its real cause (2026-10-31).

Every "Your message was interrupted before processing started (likely by a recent /stop)" reply in
Brian's chat from August to October came from a gateway restart, a watchdog or another system stop,
never a /stop. Only a Stop may say /stop."""
import pytest

from agent.i18n import t
from gateway.run import (
    _INTERRUPT_REASON_GATEWAY_RESTART, _INTERRUPT_REASON_GATEWAY_SHUTDOWN, _INTERRUPT_REASON_STOP,
    _INTERRUPT_REASON_TIMEOUT, _normalize_empty_agent_response,
)


def _reply(**fields):
    return _normalize_empty_agent_response({'final_response': '', 'interrupted': True, 'api_calls': 0, **fields}, '')


@pytest.mark.parametrize('reason', [_INTERRUPT_REASON_GATEWAY_RESTART, _INTERRUPT_REASON_GATEWAY_SHUTDOWN])
def test_restart_interrupt_says_restart_not_stop(reason):
    reply = _reply(interrupt_message=reason, interrupt_source='gateway_shutdown')
    assert reply == t('gateway.errors.interrupted_before_start_restart')
    assert '/stop' not in reply


@pytest.mark.parametrize('fields', [
    {'interrupt_message': _INTERRUPT_REASON_TIMEOUT, 'interrupt_source': 'gateway_inactivity_watchdog'},
    {'interrupt_message': 'Session ended while the turn was running'},
    {'interrupt_message': 'please look at the other thing instead'},  # a follow-up message interrupted it
], ids=['inactivity-watchdog', 'eviction', 'follow-up'])
def test_system_and_follow_up_interrupts_never_blame_stop(fields):
    reply = _reply(**fields)
    assert reply == t('gateway.errors.interrupted_before_start_other')
    assert '/stop' not in reply


@pytest.mark.parametrize('fields', [{}, {'interrupt_message': _INTERRUPT_REASON_STOP}], ids=['bare-hard-stop', 'stop-requested'])
def test_a_stop_still_says_stop(fields):
    assert _reply(**fields) == t('gateway.errors.interrupted_before_start')


def test_interrupted_after_work_stays_silent():
    assert _normalize_empty_agent_response(
        {'final_response': '', 'interrupted': True, 'api_calls': 3,
         'interrupt_message': _INTERRUPT_REASON_GATEWAY_RESTART}, '') == ''

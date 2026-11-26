"""Fallback routes get one short provider-capacity/transport retry, never a long cooldown."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.error_classifier import classify_api_error
from agent.turn_api_error import fast_failover_on_provider_outage, settle_unrecovered_error
from agent.turn_recovery import compute_error_backoff
from agent.turn_retry_state import TurnRetryState


class _Err(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status_code = status
        self.message = message
        self.response = SimpleNamespace(headers={"Retry-After": str(retry_after)} if retry_after else {})
        self.body = {"error": {"message": message}}


class _Agent:
    provider = "custom:openai-proxy"
    platform = "cli"
    log_prefix = ""
    verbose = False
    _credential_pool = None
    _current_streamed_assistant_text = ""

    def __init__(self, fallback=True):
        self.fallback = fallback
        self._retry_status_buffer = []
        self.waits = []
        self.statuses = []

    def _has_pending_fallback(self):
        return self.fallback

    def _try_activate_fallback(self, **kwargs):
        self.activated = True
        return self.fallback

    def _try_recover_primary_transport(self, *args, **kwargs):
        return False

    def _emit_diagnostic_status(self, text):
        self.statuses.append(str(text))

    def _buffer_diagnostic_status(self, text):
        self._retry_status_buffer.append(("status", str(text)))

    def _emit_diagnostic_wait(self, text):
        self.waits.append(str(text))

    def _summarize_api_error(self, error):
        return str(error)

    def _client_log_context(self):
        return ""

    def _has_content_after_think_block(self, text):
        return bool(text.strip())

    def __getattr__(self, name):
        if name in {"_max_retry_wait_s", "_turn_independent_attempt_active"}:
            return None
        return lambda *args, **kwargs: None


def _classified(error):
    return classify_api_error(error, provider="custom:openai-proxy")


def _settle(agent, error, *, retry=None, retry_count=0, max_retries=3, sleeps=None):
    classified = _classified(error)
    retry = retry or TurnRetryState(primary_recovery_attempted=True)
    with (
        patch("agent.conversation_loop._credential_refresh_profile", lambda agent: None),
        patch("agent.conversation_loop._arm_fallback_restart", lambda *args: None),
        patch("agent.turn_api_error.max_retries_exhausted_result", lambda *args, **kwargs: {"failed": True}),
        patch("agent.turn_api_error.nonretryable_client_error_result", lambda *args, **kwargs: {"failed": True}),
        patch(
            "agent.turn_api_error.interruptible_backoff_sleep",
            lambda _agent, wait, *args, **kwargs: (sleeps.append(wait) if sleeps is not None else None),
        ),
    ):
        verdict = settle_unrecovered_error(
            agent, api_error=error, classified=classified, _retry=retry, status_code=error.status_code,
            error_msg=str(error).lower(), is_context_length_error=False, is_rate_limited=False,
            _is_zai_coding_overload=False, _provider=agent.provider, _base="http://proxy", _model="primary",
            messages=[], api_messages=[], api_kwargs={}, active_system_prompt=None,
            conversation_history=[], approx_tokens=0, retry_count=retry_count, max_retries=max_retries,
            compression_attempts=0, api_call_count=1, hold_noninteractive_transient=False,
        )
    return verdict, agent


def test_overload_gets_one_retry_capped_to_five_seconds_then_fallback():
    error = _Err(503, "server_is_overloaded", retry_after=320)
    agent = _Agent()
    classified = _classified(error)
    assert fast_failover_on_provider_outage(agent, classified, error)
    sleeps = []
    retry = TurnRetryState(primary_recovery_attempted=True)
    verdict, agent = _settle(agent, error, retry=retry, retry_count=1, sleeps=sleeps)
    assert verdict.action == "fallthrough"
    assert sleeps and len(sleeps) == 1 and sleeps[0] <= 5

    verdict, agent = _settle(agent, error, retry=retry, retry_count=1)
    assert verdict.action == "break"
    assert getattr(agent, "activated", False)


def test_same_overload_without_fallback_keeps_retry_after():
    error = _Err(503, "server_is_overloaded", retry_after=320)
    agent = _Agent(fallback=False)
    assert not fast_failover_on_provider_outage(agent, _classified(error), error)
    wait = compute_error_backoff(
        agent, error, retry_count=1, max_retries=3, is_rate_limited=False,
        is_zai_coding_overload=False, base_url="http://proxy", model="primary",
    )
    assert wait == 320


def test_auth_unavailable_is_capacity_but_real_401_is_not():
    unavailable = _Err(503, "auth_unavailable: no auth available", retry_after=300)
    assert fast_failover_on_provider_outage(_Agent(), _classified(unavailable), unavailable)
    invalid = _Err(401, "invalid api key")
    assert not fast_failover_on_provider_outage(_Agent(), _classified(invalid), invalid)


@pytest.mark.parametrize(
    "message", ["unexpected EOF", "connection reset by peer", "TLS handshake EOF", "connection refused"]
)
def test_transport_failures_get_fast_failover(message):
    error = _Err(None, message)
    agent = _Agent()
    assert fast_failover_on_provider_outage(agent, _classified(error), error)
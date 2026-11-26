"""Fallback routes get one short provider-capacity/transport retry, never a long cooldown."""
import ssl
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx
import openai
import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.route_verdicts import reset_route_verdicts, route_visit
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

    def __init__(self, fallback=True, recover_primary=False):
        # ``fallback`` is the number of fallback routes still pending (True counts as one).
        self.fallback = int(fallback)
        self.recover_primary = recover_primary
        self.recovery_calls = 0
        self._retry_status_buffer = []
        self.waits = []
        self.statuses = []
        self.activations = 0
        self.route = "primary"

    def _has_pending_fallback(self):
        return self.fallback > 0

    def _try_activate_fallback(self, **kwargs):
        self.activated = True
        if self.fallback <= 0:
            return False
        self.fallback -= 1
        self.activations += 1
        self.route = f"fallback-{self.activations}"
        reset_route_verdicts(self)  # a fallback bind starts a new route visit (route_binding)
        return True

    def _try_recover_primary_transport(self, *args, **kwargs):
        self.recovery_calls += 1
        return self.recover_primary

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
            agent, api_error=error, classified=classified, _retry=retry,
            status_code=getattr(error, "status_code", None),
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


def test_fast_failover_exhaustion_skips_successful_primary_recovery():
    """A spent fast-failover retry must activate fallback without rebuilding primary."""
    error = _Err(503, "server_is_overloaded", retry_after=320)
    agent = _Agent(recover_primary=True)
    retry = TurnRetryState()
    sleeps = []

    verdict, _ = _settle(agent, error, retry=retry, retry_count=1, sleeps=sleeps)
    assert verdict.action == "fallthrough"
    assert sleeps and sleeps[0] <= 5.0

    verdict, _ = _settle(agent, error, retry=retry, retry_count=1, sleeps=sleeps)

    assert verdict.action == "break"
    assert agent.activations == 1
    assert agent.recovery_calls == 0
    assert len(sleeps) == 1


def test_fast_failover_retry_wait_cap_survives_adaptive_backoff():
    error = _Err(429, "rate limited")
    agent = _Agent()
    with patch(
        "agent.retry_utils.adaptive_rate_limit_backoff",
        return_value=(30.0, "zai_coding_overload_long"),
    ):
        wait = compute_error_backoff(
            agent, error, retry_count=4, max_retries=5, is_rate_limited=True,
            is_zai_coding_overload=False, base_url="http://proxy", model="primary",
            retry_wait_cap=5.0,
        )
    assert wait == 5.0


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


def _request():
    return httpx.Request("POST", "http://proxy/v1/chat/completions")


def _wrapped(sdk_error, cause):
    sdk_error.__cause__ = cause
    return sdk_error


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: httpx.ConnectError("all connection attempts failed"), id="httpx-connect-error"),
        pytest.param(lambda: httpx.ConnectTimeout("timed out"), id="httpx-connect-timeout"),
        pytest.param(lambda: httpx.RemoteProtocolError("Server disconnected without sending a response."),
                     id="httpx-remote-protocol"),
        pytest.param(lambda: ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:2406)"),
                     id="tls-handshake-eof"),
        pytest.param(lambda: ConnectionResetError(54, "Connection reset by peer"), id="connection-reset"),
        pytest.param(lambda: ConnectionRefusedError(61, "Connection refused"), id="connection-refused"),
        pytest.param(lambda: openai.APIConnectionError(request=_request()), id="openai-connection"),
        pytest.param(lambda: openai.APITimeoutError(request=_request()), id="openai-timeout"),
        pytest.param(lambda: anthropic.APIConnectionError(request=_request()), id="anthropic-connection"),
        pytest.param(
            lambda: _wrapped(openai.APIConnectionError(request=_request()),
                             ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:2406)")),
            id="openai-connection-over-tls-eof",
        ),
    ],
)
def test_transport_exception_types_get_fast_failover(build):
    error = build()
    classified = _classified(error)
    assert classified.reason is FailoverReason.timeout, classified
    assert fast_failover_on_provider_outage(_Agent(), classified, error)
    assert not fast_failover_on_provider_outage(_Agent(fallback=False), classified, error)


@pytest.mark.parametrize(
    "message",
    ["unexpected EOF", "connection reset by peer", "TLS handshake EOF", "connection refused", "upstream connect error"],
)
def test_transport_wording_without_a_transport_exception_is_not_an_outage(message):
    # Text alone proves nothing: only the exception type (or a status) can.
    error = RuntimeError(message)
    assert not fast_failover_on_provider_outage(_Agent(), _classified(error), error)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
@pytest.mark.parametrize(
    "body",
    [
        "connection reset by peer",
        "connection refused",
        "auth_unavailable: no auth available",
        "unexpected eof while reading; tls handshake failed; upstream connect error; no auth available",
    ],
)
def test_4xx_bodies_echoing_outage_text_never_fail_over(status, body):
    error = _Err(status, f"the tool output was: {body}", retry_after=600)
    classified = _classified(error)
    assert not fast_failover_on_provider_outage(_Agent(), classified, error)
    retry = TurnRetryState(primary_recovery_attempted=True)
    _settle(_Agent(), error, retry=retry, retry_count=1, sleeps=[])
    assert retry.fast_failover_retry_visit is None, "the short-retry path was entered for a 4xx"


@pytest.mark.parametrize("status", [503, 529])
def test_gateway_statuses_get_fast_failover(status):
    error = _Err(status, "upstream unavailable", retry_after=300)
    classified = _classified(error)
    assert fast_failover_on_provider_outage(_Agent(), classified, error)
    assert not fast_failover_on_provider_outage(_Agent(fallback=False), classified, error)


def test_429_needs_a_long_retry_after():
    assert fast_failover_on_provider_outage(
        _Agent(), _classified(_Err(429, "slow down", retry_after=300)), _Err(429, "slow down", retry_after=300))
    short = _Err(429, "slow down", retry_after=2)
    assert not fast_failover_on_provider_outage(_Agent(), _classified(short), short)
    bare = _Err(429, "slow down")
    assert not fast_failover_on_provider_outage(_Agent(), _classified(bare), bare)


@pytest.mark.parametrize(
    "upstream", ["rate_limit_error: This request would exceed your account's rate limit", "overloaded_error"],
)
def test_structured_proxy_auth_unavailable_is_an_outage_via_the_classifier(upstream):
    error = _Err(
        503,
        f"auth_unavailable: no auth available (providers=claude; last upstream error: {upstream})",
        retry_after=300,
    )
    classified = _classified(error)
    assert classified.reason in {FailoverReason.rate_limit, FailoverReason.overloaded}
    assert fast_failover_on_provider_outage(_Agent(), classified, error)


def test_auth_unavailable_text_on_a_client_error_is_not_structured_evidence():
    error = _Err(401, "auth_unavailable: no auth available; last upstream error: rate_limit_error", retry_after=300)
    assert not fast_failover_on_provider_outage(_Agent(), _classified(error), error)


def test_each_fallback_route_gets_its_own_short_retry():
    """The one short retry is per route: a fallback that also fails gets its own, not the primary's spent one."""
    error = _Err(503, "server_is_overloaded", retry_after=320)
    agent = _Agent(fallback=2)
    retry = TurnRetryState(primary_recovery_attempted=True)

    sleeps = []
    verdict, _ = _settle(agent, error, retry=retry, retry_count=1, sleeps=sleeps)  # primary: short retry
    assert verdict.action == "fallthrough" and len(sleeps) == 1 and sleeps[0] <= 5
    verdict, _ = _settle(agent, error, retry=retry, retry_count=1, sleeps=sleeps)  # primary spent: fail over
    assert verdict.action == "break" and agent.route == "fallback-1"
    assert len(sleeps) == 1

    # The first fallback is also overloaded once, with a second fallback still pending.
    visit = route_visit(agent)
    verdict, _ = _settle(agent, error, retry=retry, retry_count=1, sleeps=sleeps)
    assert verdict.action == "fallthrough", "the fallback's own short retry was skipped"
    assert len(sleeps) == 2 and sleeps[1] <= 5
    assert agent.route == "fallback-1" and route_visit(agent) == visit  # retried in place

    # ...and then it succeeds on that retry: nothing further was activated.
    assert agent.activations == 1

    # A fallback that keeps failing is still bounded: one short retry per route, then the next route.
    verdict, _ = _settle(agent, error, retry=retry, retry_count=1, sleeps=sleeps)
    assert verdict.action == "break" and agent.route == "fallback-2"
    assert len(sleeps) == 2


# --- unattended same-provider window (platform="subagent"): the window owns the retry cadence ---

def _unattended_run(retry_after=None):
    from tests.agent.test_noninteractive_fallback_retry import (
        FALLBACK, PRIMARY, _agent, _drive, _ok, _unavailable,
    )

    agent = _agent("subagent")

    def behaviour(route, _now):
        if route == PRIMARY:
            raise _unavailable(retry_after)
        return _ok("served by fallback")

    result, calls, sleeps = _drive(agent, behaviour)
    primary = [t for route, t in calls if route == PRIMARY]
    fallback = [t for route, t in calls if route == FALLBACK]
    return agent, result, primary, fallback, sleeps


def test_unattended_window_503_keeps_the_ordinary_cadence_not_a_five_second_retry_storm():
    from tests.agent.test_noninteractive_fallback_retry import WINDOW

    agent, result, primary, fallback, sleeps = _unattended_run(retry_after=None)

    assert result["completed"] is True and result["final_response"] == "served by fallback"
    assert len(fallback) == 1 and fallback[0] - primary[0] >= WINDOW  # fails over only after the window
    # The window's own backoff paces the retries (2, 4, 8, 16, 32, then 60s steps); capping every
    # wait at 5s would multiply the attempts on the overloaded primary (32 vs 8 over 181s).
    assert max(sleeps) > 5.0, sleeps
    assert len(primary) == 8, (len(primary), sleeps)


def test_unattended_window_with_a_short_retry_after_is_not_capped_to_five_seconds():
    _agent_, result, primary, fallback, sleeps = _unattended_run(retry_after="30")

    assert result["completed"] is True and len(fallback) == 1
    # Main's cadence inside the window: every wait honours the 30s Retry-After (never the 5s cap),
    # and the window hands off once the next 30s wait no longer fits, after 6 primary attempts.
    assert sleeps and all(s == 30.0 for s in sleeps), sleeps
    assert len(primary) == 6, (len(primary), sleeps)
    assert fallback[0] - primary[0] >= 5 * 30.0


def test_unattended_retry_after_longer_than_the_window_still_fails_over_fast():
    agent, result, primary, fallback, sleeps = _unattended_run(retry_after="320")

    assert result["completed"] is True and result["final_response"] == "served by fallback"
    # No window opens (the provider's cooldown outlasts it): one short capped retry, then fallback.
    assert len(primary) == 2, (len(primary), sleeps)
    assert len(sleeps) == 1 and sleeps[0] <= 5.0, sleeps
    assert len(fallback) == 1 and fallback[0] - primary[0] < 30.0
    assert agent._fallback_activated is True


def test_held_unattended_error_never_enters_the_short_retry_path():
    error = _Err(503, "server_is_overloaded")
    agent = _Agent()
    agent.platform = "subagent"
    retry = TurnRetryState(primary_recovery_attempted=True)
    sleeps = []
    classified = _classified(error)
    with (
        patch("agent.conversation_loop._credential_refresh_profile", lambda agent: None),
        patch("agent.conversation_loop._arm_fallback_restart", lambda *args: None),
        patch("agent.turn_api_error.max_retries_exhausted_result", lambda *args, **kwargs: {"failed": True}),
        patch("agent.turn_api_error.interruptible_backoff_sleep", lambda _a, wait, *a, **k: sleeps.append(wait)),
    ):
        verdict = settle_unrecovered_error(
            agent, api_error=error, classified=classified, _retry=retry, status_code=503,
            error_msg=str(error).lower(), is_context_length_error=False, is_rate_limited=False,
            _is_zai_coding_overload=False, _provider=agent.provider, _base="http://proxy", _model="primary",
            messages=[], api_messages=[], api_kwargs={}, active_system_prompt=None,
            conversation_history=[], approx_tokens=0, retry_count=1, max_retries=3,
            compression_attempts=0, api_call_count=1, hold_noninteractive_transient=True,
        )
    assert verdict.action == "fallthrough"
    assert retry.fast_failover_retry_visit is None, "the window hold must keep the ordinary retry path"

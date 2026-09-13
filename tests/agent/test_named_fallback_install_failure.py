"""Named fallback installation failures leave the retry loop immediately."""

from types import SimpleNamespace
import time

from agent import chat_completion_helpers as helpers
from agent import conversation_loop
from agent import turn_api_error
from agent.error_classifier import FailoverReason
from agent.errors import NamedFallbackInstallationError
from agent.turn_api_call import ApiCallVerdict, NousRateGuardVerdict
from agent.turn_api_request import ApiRequestBuild
from agent.turn_retry_state import TurnRetryState
from tools.custom_subagents import ResolvedRoute, RuntimePin


class _ContentFilterTransport:
    def validate_response(self, response):
        return True

    def normalize_response(self, response):
        return SimpleNamespace(
            finish_reason="content_filter",
            content="fixture refusal",
            reasoning=None,
        )


def test_named_fallback_partial_install_escapes_retry_loop(monkeypatch):
    route = ResolvedRoute(
        "anthropic",
        "fixture-fallback",
        "https://api.anthropic.com",
        "anthropic_messages",
        None,
        "fixture-key",
        "fixture-fingerprint",
        "{}",
    )
    pin = RuntimePin(
        "fixture",
        "primary",
        "fixture-primary",
        "https://primary.invalid",
        "chat_completions",
        None,
        "fixture-fingerprint",
        True,
        fallback_routes=(route,),
    )
    transport = _ContentFilterTransport()
    agent = SimpleNamespace(
        _anthropic_prompt_cache_policy=lambda **kwargs: (False, False),
        _buffer_status=lambda *args: None,
        _delegation_runtime_pin=pin,
        _extract_api_error_context=lambda error: {},
        _fallback_activated=False,
        _fallback_chain=[route.native_entry()],
        _fallback_index=0,
        _get_transport=lambda: transport,
        _has_pending_fallback=lambda: True,
        _interrupt_requested=False,
        _invoke_api_request_error_hook=lambda **kwargs: None,
        _is_anthropic_oauth=False,
        _should_treat_stop_as_truncated=lambda *args: False,
        _touch_activity=lambda *args: None,
        _unavailable_fallback_keys=set(),
        _vprint=lambda *args, **kwargs: None,
        api_key="fixture-key",
        api_mode=pin.api_mode,
        base_url=pin.base_url,
        log_prefix="",
        model=pin.model,
        provider=pin.provider,
        quiet_mode=True,
        requested_provider=pin.provider,
        thinking_callback=None,
        verbose_logging=False,
    )

    activation_calls = 0

    def activate(reason=None):
        nonlocal activation_calls
        activation_calls += 1
        if activation_calls > 1:
            return False
        return helpers.try_activate_fallback(agent, reason)

    agent._try_activate_fallback = activate

    monkeypatch.setattr("agent.fallback_cooldown._arm_rate_limit_cooldown", lambda *args: None)
    monkeypatch.setattr(helpers, "_should_skip_fallback_candidate", lambda *args: False)
    monkeypatch.setattr(helpers, "_rebind_fallback_credential_pool", lambda *args, **kwargs: None)

    def partial_install_then_fail(target, *args):
        target.api_key = "partially-installed-key"
        raise ValueError("fixture install failure")

    monkeypatch.setattr("agent.client_lifecycle._swap_fallback_clients", partial_install_then_fail)

    # Keep the broken-code path deterministic: before the fix, the install failure is
    # misclassified as an API error and reaches the generic fallback branch a second time.
    monkeypatch.setattr(turn_api_error, "recover_before_classification", lambda *args, **kwargs: (False, kwargs["active_system_prompt"]))
    monkeypatch.setattr(
        turn_api_error,
        "classify_api_error",
        lambda *args, **kwargs: SimpleNamespace(
            reason=FailoverReason.unknown,
            status_code=None,
            retryable=False,
            should_compress=False,
            should_rotate_credential=False,
            should_fallback=False,
        ),
    )
    monkeypatch.setattr(turn_api_error, "recover_after_classification", lambda *args, **kwargs: (False, False))
    monkeypatch.setattr(
        turn_api_error,
        "log_api_error_attempt",
        lambda *args, **kwargs: ("ValueError", str(args[1]), "primary", pin.base_url, pin.model),
    )
    monkeypatch.setattr(
        turn_api_error,
        "route_classified_error",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=None,
            messages=kwargs["messages"],
            active_system_prompt=kwargs["active_system_prompt"],
            conversation_history=kwargs["conversation_history"],
            retry_count=kwargs["retry_count"],
            max_retries=kwargs["max_retries"],
            compression_attempts=kwargs["compression_attempts"],
            is_rate_limited=False,
            wrapped_output_cap_budget=None,
            is_zai_coding_overload=False,
            provider_overflow_recovery_pending=False,
            action="fallthrough",
            result=None,
        ),
    )
    monkeypatch.setattr(
        turn_api_error,
        "recover_from_overflow",
        lambda *args, **kwargs: SimpleNamespace(
            messages=kwargs["messages"],
            active_system_prompt=kwargs["active_system_prompt"],
            conversation_history=kwargs["conversation_history"],
            approx_tokens=kwargs["approx_tokens"],
            compression_attempts=kwargs["compression_attempts"],
            is_context_length_error=False,
            provider_overflow_recovery_pending=False,
            action="fallthrough",
            result=None,
        ),
    )
    monkeypatch.setattr(turn_api_error, "nonretryable_client_error_result", lambda *args, **kwargs: {"failed": True})
    monkeypatch.setattr("agent.fallback_cooldown._mark_entitlement_rejected_model", lambda *args: None)

    request_builds = 0
    provider_calls = 0

    def guard(
        agent, *, _retry, api_messages, messages, conversation_history,
        active_system_prompt, retry_count, compression_attempts, api_call_count,
    ):
        return NousRateGuardVerdict("fallthrough", active_system_prompt, retry_count, compression_attempts)

    def build(
        agent, *, api_messages, _moa_prepared_request, tools_for_api, system_message,
        messages, original_user_message, approx_tokens, total_chars, retry_count,
        api_call_count, api_request_id, api_start_time, effective_task_id, turn_id,
    ):
        nonlocal request_builds
        request_builds += 1
        return ApiRequestBuild(
            "fallthrough", api_messages, _moa_prepared_request, tools_for_api,
            {"messages": api_messages}, {"messages": api_messages}, [],
        )

    response = SimpleNamespace(choices=[SimpleNamespace()])

    def perform(
        agent, *, api_kwargs, _original_api_kwargs, _llm_middleware_trace,
        _moa_prepared_request, _retry, thinking_spinner, retry_count,
        api_call_count, api_request_id, effective_task_id, turn_id, interrupted,
    ):
        nonlocal provider_calls
        provider_calls += 1
        return ApiCallVerdict("fallthrough", response, thinking_spinner, interrupted)

    monkeypatch.setattr(conversation_loop, "nous_rate_limit_guard", guard)
    monkeypatch.setattr(conversation_loop, "build_api_request", build)
    monkeypatch.setattr(conversation_loop, "perform_api_call", perform)

    retry = TurnRetryState()
    state = conversation_loop._LoopState(
        user_message="fixture",
        system_message=None,
        moa_config=None,
        original_user_message="fixture",
        conversation_history=[],
        effective_task_id="fixture-task",
        turn_id="fixture-turn",
        _should_review_memory=False,
        _plugin_user_context=None,
        _ext_prefetch_cache=None,
        messages=[{"role": "user", "content": "fixture"}],
        active_system_prompt="fixture system",
        current_turn_user_idx=0,
        _preflight_compression_blocked=False,
        max_compression_attempts=2,
        api_call_count=1,
        api_messages=[{"role": "user", "content": "fixture"}],
        tools_for_api=[],
        approx_tokens=1,
        total_chars=7,
        api_start_time=time.time(),
        max_retries=3,
        _retry=retry,
        api_request_id="fixture-request",
    )

    raised = None
    try:
        conversation_loop._run_api_retry_loop(agent, state)
    except ValueError as error:
        raised = error

    assert activation_calls == 1
    assert request_builds == 1
    assert provider_calls == 1
    assert not retry.restart_with_compressed_messages
    assert not retry.restart_with_length_continuation
    assert not retry.restart_with_rebuilt_messages
    assert not retry.restart_with_redirected_messages
    assert isinstance(raised, NamedFallbackInstallationError)
    assert str(raised) == "named subagent fallback installation failed"

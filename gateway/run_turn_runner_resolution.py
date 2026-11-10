"""Topical helpers extracted from gateway/run_turn_runner.py."""
from __future__ import annotations
from gateway.config import Platform


def _raise_invalid_alias(exc: Exception) -> None:
    """Keep stored-alias failures out of the caller's ordinary refresh recovery."""
    from hermes_cli.model_alias_runtime import ModelAliasError

    if isinstance(exc, ModelAliasError):
        raise exc


def _resolution_failure_reply(exc: Exception, platform: Platform) -> tuple[str, str, bool]:
    """Return chat text, failure reason and retryability for pre-turn resolution failures."""
    from gateway.run_turn_runner import (t)
    from gateway.run_turn_runner import (
        Platform
    )
    from agent.error_classifier import FailoverReason
    from hermes_cli.auth import AuthError, is_rate_limited_auth_error
    from hermes_cli.runtime_provider_custom import LocalRuntimeUnavailableError
    from hermes_state_runtime import RuntimeStoreError
    auth_permanent = FailoverReason.auth_permanent.value
    if isinstance(exc, LocalRuntimeUnavailableError):
        # The chosen model is configured; its local server is down. Not a credential verdict, so
        # the client must not offer "Update API key" or sign-in: the sentence names the fix.
        return f"⚠️ {exc}", exc.error_code, exc.retryable
    if isinstance(exc, RuntimeStoreError) and exc.reason == 'launch_credentials_unavailable':
        # The session's launch key is gone (a restart revoked a CLI --api-key, a route key changed):
        # the shared refusal copy names /new, never a resend.
        from agent.turn_failure_copy import launch_credentials_copy
        return launch_credentials_copy(exc), 'launch_credentials_unavailable', False
    if isinstance(exc, RuntimeStoreError):
        return (f"⚠️ This session's launch credentials are no longer available ({exc.reason}), so this "
                "message wasn't processed. Start a new session from the CLI to bind them again.", auth_permanent, False)
    if is_rate_limited_auth_error(exc.__cause__):
        from gateway.run import _gateway_provider_error_reply
        return _gateway_provider_error_reply(str(exc)), auth_permanent, False
    if platform == Platform.LOCAL:
        return f"⚠️ {exc}", auth_permanent, False
    seen: set[int] = set()
    host = auth = False
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        host = host or isinstance(link, OSError)
        auth = auth or isinstance(link, AuthError)
        link = link.__cause__ or link.__context__
    if host and not auth:
        return t("gateway.errors.generic_failed"), "host_error", False
    return t("gateway.errors.no_credentials"), auth_permanent, False

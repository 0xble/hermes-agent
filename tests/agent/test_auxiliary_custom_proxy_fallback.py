"""Custom proxy labels keep credential failures isolated on a shared endpoint."""

from unittest.mock import MagicMock

import pytest

from agent import auxiliary_client as aux
from agent.backend_identity import FailureScope


BASE_URL = "http://127.0.0.1:8317/v1"


class _ApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _error(status_code: int, message: str) -> Exception:
    return _ApiError(status_code, message)


def _route(client):
    return aux._LadderRoute(
        client=client,
        task="goal_judge",
        tag="",
        async_mode=False,
        base_info=BASE_URL,
        resolved_provider="custom:openai-proxy",
        resolved_model="gpt-6.1-sol",
        resolved_base_url=BASE_URL,
        resolved_api_key=None,
        resolved_api_mode="codex_responses",
        final_model="gpt-6.1-sol",
        main_runtime=None,
        route_info=None,
        timeout=30.0,
    )


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (402, "Error code: 402 - insufficient credits"),
        (503, "auth_unavailable: no auth available for model claude-opus-5-5"),
    ],
    ids=["payment", "auth-unavailable"],
)
def test_custom_proxy_credential_failure_reaches_sibling_label(monkeypatch, status_code, message):
    """A payment/auth-unavailable response from openai-proxy must not hide claude-proxy."""
    primary = MagicMock(base_url=BASE_URL, api_key="openai-key")
    fallback = MagicMock(base_url=BASE_URL, api_key="claude-key")
    entry = {
        "provider": "custom:claude-proxy",
        "model": "claude-opus-5-5",
        "base_url": BASE_URL,
    }
    mark = MagicMock()
    monkeypatch.setattr(aux, "_mark_provider_unhealthy", mark)
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda _task: {"fallback_chain": [entry]})
    monkeypatch.setattr(aux, "_custom_health_base_url", lambda _provider, explicit=None: explicit or BASE_URL)
    monkeypatch.setattr(aux, "_resolve_fallback_entry", lambda _entry: (fallback, "claude-opus-5-5"))

    ladder = aux._ladder_provider_fallback(
        aux._failure(_route(primary), _error(status_code, message)),
        _route(primary),
    )

    seen = []

    def perform(step):
        seen.append(step)
        assert step.kind == "fallback"
        assert step.args[0] is fallback
        return "claude served"

    assert aux._drive_ladder(ladder, perform) == "claude served"
    mark.assert_called_once()
    assert mark.call_args.args[0] == "custom:openai-proxy"
    assert mark.call_args.kwargs["model"] is None
    assert mark.call_args.kwargs["reason"] in {"payment error", "auth unavailable"}
    assert seen


def test_custom_connection_failure_still_skips_shared_endpoint(monkeypatch):
    entry = {
        "provider": "custom:claude-proxy",
        "model": "claude-opus-5-5",
        "base_url": BASE_URL,
    }
    fallback = MagicMock(base_url=BASE_URL, api_key="claude-key")
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda _task: {"fallback_chain": [entry]})
    monkeypatch.setattr(aux, "_custom_health_base_url", lambda _provider, explicit=None: explicit or BASE_URL)
    resolve = MagicMock(return_value=(fallback, "claude-opus-5-5"))
    monkeypatch.setattr(aux, "_resolve_fallback_entry", resolve)
    skip = aux._failed_backend_skip(
        "custom:openai-proxy",
        "gpt-6.1-sol",
        failed_base_url=BASE_URL,
        failure_scope=FailureScope.ENDPOINT,
    )

    assert skip("custom:claude-proxy", "claude-opus-5-5", BASE_URL)
    client, model, label = aux._try_configured_fallback_chain(
        "goal_judge",
        "custom:openai-proxy",
        failed_model="gpt-6.1-sol",
        failed_base_url=BASE_URL,
        failure_scope=FailureScope.ENDPOINT,
    )

    assert (client, model, label) == (None, None, "")
    resolve.assert_not_called()


def test_custom_payment_failure_skips_same_label_other_models_but_not_sibling_label():
    skip = aux._failed_backend_skip(
        "custom:openai-proxy",
        None,
        failed_base_url=BASE_URL,
        failure_scope=FailureScope.CREDENTIAL,
    )

    assert skip("custom:openai-proxy", "another-model", BASE_URL)
    assert not skip("custom:claude-proxy", "claude-opus-5-5", BASE_URL)

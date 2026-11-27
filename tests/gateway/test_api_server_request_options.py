"""API model-selection options at the request boundary."""

from gateway.platforms.api_server_request_options import _request_agent_overrides


def test_bare_model_dropped_when_disallowed():
    overrides = _request_agent_overrides(
        {"model": "openai/gpt-5"}, allow_bare_model=False
    )
    assert "requested_model" not in overrides

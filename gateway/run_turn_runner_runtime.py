"""Per-request API reasoning and service-tier overrides for gateway turns."""


def apply_api_request_options(runner, reasoning_config):
    """Apply explicit API options after session defaults have been resolved."""
    from gateway.session_api_turn import api_execution
    api = api_execution.get()
    if api is not None:
        from gateway.platforms.api_server import _request_reasoning_config, _request_service_tier, _REQUEST_OPTION_MISSING
        requested_reasoning = _request_reasoning_config(api['settings'].get('model_options'))
        if requested_reasoning is not None:
            reasoning_config = requested_reasoning
        tier = _request_service_tier(api['settings'].get('model_options'))
        if tier is not _REQUEST_OPTION_MISSING:
            runner._service_tier = tier
    return reasoning_config

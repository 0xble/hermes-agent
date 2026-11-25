from agent.api_request_hooks import ApiRequestHooksMixin


class _HookAgent(ApiRequestHooksMixin):
    session_id = "session-1"
    platform = "cli"
    model = "gpt-5"
    provider = "openai"
    base_url = "https://api.openai.com/v1"
    api_mode = "chat_completions"


def test_api_request_error_hook_records_one_ledger_row(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "hermes_cli.observability.request_usage_ledger.note_error",
        lambda agent, failure_class: captured.append((agent, failure_class)) or True,
    )
    _HookAgent()._invoke_api_request_error_hook(
        task_id="task-1", turn_id="turn-1", api_request_id="req-1", api_call_count=1,
        api_start_time=1.0, api_kwargs={"model": "gpt-5"}, error_type="RateLimitError",
        error_message="provider said no", reason="rate_limit",
    )
    assert len(captured) == 1
    assert captured[0][0].session_id == "session-1"
    assert captured[0][1] == "rate_limit"


def test_api_request_error_hook_does_not_record_error_content(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "hermes_cli.observability.request_usage_ledger.note_error",
        lambda agent, failure_class: captured.append(failure_class) or True,
    )
    _HookAgent()._invoke_api_request_error_hook(
        task_id="task-1", turn_id="turn-1", api_request_id="req-1", api_call_count=1,
        api_start_time=1.0, api_kwargs={"messages": [{"role": "user", "content": "secret"}]},
        error_type="RuntimeError", error_message="secret provider response",
    )
    assert captured == ["RuntimeError"]

"""Named-route invariants exercised through the real agent request builder."""
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from tools.custom_subagents import RuntimePin, parse_definitions
from tests.run_agent.test_run_agent_codex_responses import (
    _patch_agent_bootstrap, _codex_message_response, _codex_tool_call_response,
)


@pytest.fixture
def make_child(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_agent_bootstrap(monkeypatch)
    import run_agent
    monkeypatch.setattr("agent.retry_utils.jittered_backoff", lambda *a, **k: 0)
    children = []
    def make(effort="medium", model="gpt-5.6-luna"):
        child = run_agent.AIAgent(
            model=model, provider="openai-codex", api_mode="codex_responses",
            base_url="https://chatgpt.com/backend-api/codex", api_key="fixture-token",
            reasoning_config={"enabled": True, "effort": effort},
            quiet_mode=True, max_iterations=4, skip_context_files=True,
            skip_memory=True, skip_background_review=True,
        )
        definition = parse_definitions({"subagents": {"fixture": {
            "description": "A test definition", "instructions": "Perform the test task.",
            "model": model, "provider": "openai-codex", "reasoning_effort": effort,
        }}})["fixture"]
        child._delegation_runtime_pin = RuntimePin.from_child(child, definition, child.reasoning_config)
        children.append(child)
        return child
    yield make
    for child in children:
        child.close()


@pytest.mark.parametrize("named", [True, False])
def test_real_delegation_builder_preserves_child_knowledge_mode(make_child, named):
    from tools.delegate_tool import _build_child_agent

    parent = make_child("high", "gpt-6-astra")
    del parent._delegation_runtime_pin
    definition = parse_definitions({"subagents": {"fixture": {
        "description": "Read-only fixture", "instructions": "Investigate the fixture.",
        "model": "gpt-5.6-luna", "provider": "openai-codex", "reasoning_effort": "medium",
    }}})["fixture"] if named else None
    child = _build_child_agent(
        task_index=0, goal="Investigate the fixture", context=None, toolsets=None,
        model="gpt-5.6-luna", max_iterations=1, task_count=1, parent_agent=parent,
        subagent_definition=definition,
        resolved_reasoning={"enabled": True, "effort": "medium"} if named else None,
    )
    try:
        assert child.memory_access_mode == ("read_only" if named else None)
        assert child._memory_read_only is named
        assert child.skip_background_review is named
        assert parent._memory_read_only is False
        if named:
            assert child._delegation_runtime_pin.model == definition.model
    finally:
        child.close()


def test_initial_tool_continuation_and_correction_preserve_effort(make_child, monkeypatch):
    child = make_child()
    responses = [_codex_tool_call_response(), _codex_message_response("done"), _codex_message_response("corrected")]
    requests = []
    def api(kwargs):
        requests.append(kwargs)
        return responses.pop(0)
    def tools(message, messages, task_id, *args):
        for call in message.tool_calls:
            messages.append({"role": "tool", "tool_call_id": call.id, "content": '{"ok":true}'})
    monkeypatch.setattr(child, "_interruptible_api_call", api)
    monkeypatch.setattr(child, "_execute_tool_calls", tools)
    result = child.run_conversation("Run the fixture command")
    assert result["completed"]
    result2 = child.run_conversation("Correct the output format")
    assert result2["completed"]
    assert len(requests) == 3
    assert all(r["model"] == "gpt-5.6-luna" and r["reasoning"]["effort"] == "medium" for r in requests)


def test_concurrent_efforts_and_turn_override_are_isolated(make_child):
    from agent.reasoning_context import turn_reasoning_context
    a, b = make_child("medium"), make_child("high", "gpt-5.6-terra")
    def request(child):
        return child._build_api_kwargs([{"role": "user", "content": "fixture"}], tools_for_api=[])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, (a, b)))
    assert [r["reasoning"]["effort"] for r in results] == ["medium", "high"]
    # Baseline mutation cannot alter the already-resolved child pin.
    a.reasoning_config = {"effort": "high"}
    assert request(a)["reasoning"]["effort"] == "medium"


@pytest.mark.parametrize("field,value", [
    ("provider", "openai"), ("model", "different-model"),
    ("base_url", "https://api.openai.com/v1"), ("api_key", "other-account"),
])
def test_changed_route_fails_before_request(make_child, field, value):
    child = make_child()
    setattr(child, field, value)
    with pytest.raises(ValueError, match="pinned route changed"):
        child._delegation_runtime_pin.validate_request(child, {"model": "gpt-5.6-luna", "reasoning": {"effort": "medium"}})


def test_request_override_cannot_replace_model_or_effort(make_child):
    child = make_child()
    pin = child._delegation_runtime_pin
    for extra in ({"model": "other-model"}, {"reasoning": {"effort": "high"}}):
        with pytest.raises(ValueError, match="pinned request"):
            pin.validate_request(child, {"model": child.model, "reasoning": {"effort": "medium"}, "extra_body": extra})
    assert "fixture-token" not in repr(pin)
    assert "credential" not in str(pin.metadata())


def test_named_child_cannot_refresh_from_another_auth_source(make_child, monkeypatch):
    child = make_child()
    import hermes_cli.auth
    def unexpected(**kwargs):
        pytest.fail("Named child tried to re-resolve auth")
    monkeypatch.setattr(hermes_cli.auth, "resolve_codex_runtime_credentials", unexpected)
    assert child._try_refresh_codex_client_credentials() is False


@pytest.mark.parametrize("headers", [{"Authorization": "Bearer other"}, {"chatgpt-account-id": "other"}])
def test_named_authentication_header_overrides_are_rejected(make_child, headers):
    child = make_child()
    child.request_overrides = {"extra_headers": headers}
    with pytest.raises(ValueError, match="authentication headers"):
        child._build_api_kwargs([{"role": "user", "content": "hello"}])


@pytest.mark.parametrize("field,value,expected", [
    ("base_url", "https://api.openai.com/v1", "SDK client route"),
    ("api_key", "other", "SDK client credential"),
])
def test_named_sdk_client_route_mutation_is_rejected(make_child, field, value, expected):
    # Route and credential mutations are reported separately: which one moved
    # is the first question anyone debugging a pin failure asks.
    child = make_child()
    setattr(child.client, field, value)
    with pytest.raises(ValueError, match=expected):
        child._build_api_kwargs([{"role": "user", "content": "hello"}])


def test_named_physical_codex_request_rejects_effort_mutation(make_child):
    child = make_child()
    kwargs = child._build_api_kwargs([{"role": "user", "content": "hello"}])
    kwargs["reasoning"]["effort"] = "low"
    with pytest.raises(ValueError, match="reasoning changed"):
        child._run_codex_stream(kwargs)


def test_named_physical_codex_request_rejects_another_client(make_child):
    child = make_child()
    kwargs = child._build_api_kwargs([{"role": "user", "content": "hello"}])
    other = SimpleNamespace(api_key="another-account", base_url=child.base_url)
    with pytest.raises(ValueError, match="SDK client credential"):
        child._run_codex_stream(kwargs, client=other)


def test_nonstream_completion_after_interrupt_is_cancelled(make_child, monkeypatch):
    """A response completing during the poll join cannot outrun cancellation."""
    # Two different live seams after upstream split this out of chat_completion_helpers:
    # the CLASS now lives in agent.chat_completion_nonstream (helpers imports it lazily inside the
    # dispatch function, so it is not a module attribute there), while the THREAD is still spawned as
    # ``h.threading.Thread`` with ``h`` = chat_completion_helpers (nonstream.py:249). Patch each where
    # production actually resolves it.
    import agent.chat_completion_helpers as helpers
    import agent.chat_completion_nonstream as nonstream

    child = make_child()
    request = nonstream._NonStreamRequest(child, {"model": child.model})
    request.result["response"] = _codex_message_response("late response")  # type: ignore[assignment]
    monkeypatch.setattr(child, "_touch_activity", lambda _reason: None)

    class CompletesBeforeNextPoll:
        def __init__(self, *_args, **_kwargs):
            self.alive = False

        def start(self):
            pass

        def is_alive(self):
            # The interrupt arrives after the worker published its response but
            # before the next poll observes that the worker has finished.
            child._interrupt_requested = True
            return self.alive

        def join(self, timeout=None):
            pytest.fail("The finished worker must not be joined again")

    monkeypatch.setattr(helpers.threading, "Thread", CompletesBeforeNextPoll)
    with pytest.raises(InterruptedError, match="Agent interrupted during API call"):
        request.run()


def test_named_child_native_cancellation_keeps_parent_configuration(make_child, monkeypatch):
    from threading import Event
    from tools.delegate_tool import _run_single_child
    parent = make_child("high", "gpt-6-astra")
    del parent._delegation_runtime_pin
    child = make_child()
    before = (parent.model, deepcopy(parent.reasoning_config), parent.provider, parent.api_key)
    entered, release = Event(), Event()

    def stream(api_kwargs, **kwargs):
        assert api_kwargs["reasoning"]["effort"] == "medium"
        entered.set()
        assert release.wait(5)
        return _codex_message_response("cancelled response")

    monkeypatch.setattr(child, "_run_codex_stream", stream)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run_single_child, 0, "Wait until cancelled", child, parent)
        try:
            assert entered.wait(5)
            assert child.interrupt("Test cancellation", hard_cancel=True)
        finally:
            release.set()
        result = future.result(timeout=10)
    assert result["status"] == "interrupted"
    assert before == (parent.model, parent.reasoning_config, parent.provider, parent.api_key)


def test_iteration_summary_and_empty_summary_retry_keep_effort(make_child, monkeypatch):
    from agent.chat_completion_helpers import handle_max_iterations
    from tests.run_agent.test_run_agent_codex_responses import _codex_message_response
    child = make_child()
    requests = []
    responses = [_codex_message_response(""), _codex_message_response("Summary complete")]
    def stream(kwargs):
        requests.append(deepcopy(kwargs))
        return responses.pop(0)
    monkeypatch.setattr(child, "_run_codex_stream", stream)
    assert handle_max_iterations(child, [{"role": "user", "content": "Summarize the fixture"}], 1) == "Summary complete"
    assert len(requests) == 2
    assert all(r["model"] == child.model and r["reasoning"]["effort"] == "medium" for r in requests)
    assert requests[0]["instructions"] == requests[1]["instructions"]


def test_schema_correction_reuses_the_same_pin(make_child, monkeypatch):
    from tools.delegate_tool import _run_single_child
    from tests.run_agent.test_run_agent_codex_responses import _codex_message_response
    child = make_child()
    child._delegate_output_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    requests = []
    responses = [_codex_message_response("not JSON"), _codex_message_response('{"ok":true}')]
    def api(kwargs):
        requests.append(deepcopy(kwargs))
        return responses.pop(0)
    monkeypatch.setattr(child, "_interruptible_api_call", api)
    result = _run_single_child(0, "Return a structured result for the fixture", child)
    assert result["schema_valid"] is True
    assert len(requests) == 2
    assert all(r["model"] == child.model and r["reasoning"]["effort"] == "medium" for r in requests)
    assert requests[0]["instructions"] == requests[1]["instructions"]


def test_transient_retry_keeps_model_route_and_effort(make_child, monkeypatch):
    import httpx
    import run_agent
    from tests.run_agent.test_run_agent_codex_responses import _codex_message_response
    child = make_child()
    requests = []
    monkeypatch.setattr("agent.retry_utils.jittered_backoff", lambda *a, **k: 0.0)
    def api(kwargs):
        requests.append(deepcopy(kwargs))
        if len(requests) == 1:
            raise httpx.ReadTimeout("fixture transient timeout")
        return _codex_message_response("Recovered")
    monkeypatch.setattr(child, "_interruptible_api_call", api)
    result = child.run_conversation("Complete the fixture request")
    assert result["completed"] is True
    assert result["final_response"] == "Recovered"
    assert len(requests) == 2
    assert all(r["model"] == child.model and r["reasoning"]["effort"] == "medium" for r in requests)
    assert requests[0]["instructions"] == requests[1]["instructions"]

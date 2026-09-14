"""Real process and classified retry boundaries produce bounded display facts."""
import json
from types import SimpleNamespace

import pytest

from agent.error_classifier import ClassifiedError, FailoverReason
from agent.turn_api_error import settle_unrecovered_error
from tools.delegate_tool_progress import _ChildProgressRelay


def relay_for(events):
    return _ChildProgressRelay(0, "PRIVATE goal", None,
        lambda kind, *args, **kw: events.append((kind, kw)), 1, "child", None, 0, "model", [],
        dict(parent_task_id="a" * 32, thread_ref="A", task_label="Test wait", attempt=2))


@pytest.mark.parametrize("release", ["exit", "yield"])
def test_real_registry_wait_emits_only_while_blocking_and_clears_on_exit(tmp_path, monkeypatch, release):
    from tools import process_registry as process
    from tools.delegate_tool_registry import _active_subagents
    from tools.registry import registry
    local = process.ProcessRegistry()
    monkeypatch.setattr(process, "process_registry", local)
    events = []
    relay = relay_for(events)
    child = SimpleNamespace(tool_progress_callback=relay)
    monkeypatch.setitem(_active_subagents, "child", {"agent": child})
    # Explicit stdin is a deterministic real-process barrier, not a timing guess.
    session = local.spawn_local("read line", cwd=str(tmp_path), task_id="child", use_pty=True)
    def callback(*args, **kw):
        relay(*args, **kw)
        if args[0] == "runtime.wait" and kw["active"]:
            if release == "yield":
                import threading
                from tools.interrupt import request_yield
                request_yield(threading.get_ident())
            else:
                local.submit_stdin(session.id, "finish")
    child.tool_progress_callback = callback
    try:
        result = json.loads(registry.dispatch("process_manage", {"action": "wait", "session_id": session.id, "timeout": 10}, task_id="child"))
        assert result["status"] == ("interrupted" if release == "yield" else "exited")
        if release == "yield":
            assert result["process_running"] is True
            assert local.poll(session.id)["status"] == "running"
        assert [kw["activity_reason"] for kind, kw in events] == ["process", None]
        assert all(kw["attempt"] == 2 for _, kw in events)
        if release == "yield":
            local.submit_stdin(session.id, "finish")
            assert local.wait(session.id, timeout=10)["status"] == "exited"
        events.clear()
        registry.dispatch("process_manage", {"action": "wait", "session_id": session.id, "timeout": 1}, task_id="child")
        registry.dispatch("process_manage", {"action": "wait", "session_id": "absent", "timeout": 1}, task_id="child")
        assert events == []
    finally:
        local.kill_process(session.id)


@pytest.mark.parametrize("reason,expected", [(FailoverReason.rate_limit, "provider_rate_limit"),
    (FailoverReason.overloaded, "provider_capacity"), (FailoverReason.server_error, None)])
@pytest.mark.parametrize("interrupted", [False, True])
def test_classified_retry_wait_has_exact_lifetime_without_raw_errors(monkeypatch, reason, expected, interrupted):
    import agent.turn_api_error as errors
    events = []
    relay = relay_for(events)
    agent = SimpleNamespace(tool_progress_callback=relay)
    monkeypatch.setattr(errors, "compute_error_backoff", lambda *a, **kw: 1)
    def sleep(*a, **kw):
        assert ([e[1]["activity_reason"] for e in events] == [expected]) if expected else not events
        if interrupted:
            raise InterruptedError("PRIVATE")
        return None
    monkeypatch.setattr(errors, "interruptible_backoff_sleep", sleep)
    kwargs = dict(api_error=RuntimeError("PRIVATE"), classified=ClassifiedError(reason),
        _retry=SimpleNamespace(restart_with_redirected_messages=False), status_code=429, error_msg="PRIVATE",
        is_context_length_error=False, is_rate_limited=reason == FailoverReason.rate_limit,
        _is_zai_coding_overload=False, _provider="PRIVATE", _base="PRIVATE", _model="PRIVATE",
        messages=[], api_messages=[], api_kwargs={}, active_system_prompt="PRIVATE", conversation_history=[],
        approx_tokens=0, retry_count=1, max_retries=3, compression_attempts=0, api_call_count=1)
    if interrupted:
        with pytest.raises(InterruptedError):
            settle_unrecovered_error(agent, **kwargs)
    else:
        assert settle_unrecovered_error(agent, **kwargs).action == "fallthrough"
    assert [kw["activity_reason"] for _, kw in events] == ([expected, None] if expected else [])
    assert "PRIVATE" not in str(events)


@pytest.mark.parametrize("result,expected", [
    ({"failure_reason": "billing", "billing_unverified": False}, "provider_billing"),
    ({"failure_reason": "billing", "billing_unverified": True}, None),
    ({"failure_reason": "billing"}, None),
    ({"failure_reason": "rate_limit"}, "provider_rate_limit"),
    ({"error": "insufficient_quota PRIVATE"}, None),
])
def test_child_completion_projects_only_verified_classified_reason(monkeypatch, result, expected):
    from tools.delegate_tool_child_run import _ChildRun
    run = object.__new__(_ChildRun)
    events = []
    run.child_progress_cb = relay_for(events)
    run.child = SimpleNamespace()
    run.child_task_id, run.wall_start = "child", 0
    run.emit_complete(result, {"status": "failed", "summary": "", "api_calls": 1}, 1)
    assert events[-1][1]["terminal_reason"] == expected
    assert events[-1][1]["activity_reason"] is None


def test_nested_runtime_events_preserve_owner_and_do_not_clear_parent_wait():
    from agent.delegation_activity import observed_wait
    seen = []
    parent = relay_for(seen)
    child = _ChildProgressRelay(0, "child", None, parent, 1, "grandchild", "child", 1, None, [],
        dict(parent_task_id="b" * 32, thread_ref="B", attempt=0))
    with observed_wait(parent, "process"):
        with observed_wait(child, "provider_rate_limit"):
            child("tool.started", "read_file")
        child("subagent.complete", status="completed")
        parent("tool.started", "terminal")
        assert seen[-1][1]["activity_reason"] == "process"
    nested = [kw for _, kw in seen if kw["parent_task_id"] == "b" * 32]
    assert nested and all(kw["thread_ref"] == "B" and kw["attempt"] == 0 for kw in nested)
    assert [kw["activity_sequence"] for kw in nested] == sorted(set(kw["activity_sequence"] for kw in nested))

"""The model-facing native review call is a hard tool-batch boundary."""

from types import SimpleNamespace

from agent import tool_executor as te


def _call(name, call_id):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments="{}"))


def test_review_changes_skips_remaining_sequential_calls(monkeypatch):
    calls = [_call("review_changes", "review"), _call("write_file", "write")]
    executed = []
    skipped = []
    agent = SimpleNamespace(
        _incremental_persistence_failed=False,
        _interrupt_requested=False,
        _vprint=lambda *_a, **_k: None,
    )

    def parse(_agent, call, flatten_probe=True):
        ref = te._ToolCallRef(call.function.name, {}, "task", call.id, [])
        return SimpleNamespace(parse_error=None, scope_block=None, ref=lambda _task_id: ref)

    def resolve(_agent, ref, _messages):
        return te._SequentialDispatch(lambda _args: executed.append(ref.name) or {"status": "dispatched"})

    monkeypatch.setattr(te, "_budget_for_agent", lambda _agent: None)
    monkeypatch.setattr(te, "_parse_tool_call", parse)
    monkeypatch.setattr(te, "_resolve_sequential_dispatch", resolve)
    monkeypatch.setattr(
        te, "_run_sequential_call",
        lambda _agent, dispatch, ref, **_kwargs: (
            te._ManagedToolResult(dispatch.execute(ref.args), ref.args, [], False, False), 0.01,
        ),
    )
    monkeypatch.setattr(te, "_publish_sequential_result", lambda *_a, **_k: True)
    monkeypatch.setattr(
        te, "_skip_remaining_sequential",
        lambda _agent, _messages, remaining, *_a, **_k: skipped.extend(remaining) or True,
    )
    monkeypatch.setattr(te, "_finalize_tool_batch", lambda *_a, **_k: None)

    te.execute_tool_calls_sequential(
        agent, SimpleNamespace(tool_calls=calls), [], "task",
    )

    assert executed == ["review_changes"]
    assert [call.function.name for call in skipped] == ["write_file"]

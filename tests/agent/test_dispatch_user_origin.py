"""Dispatch receives the current human task, including through compaction."""
from types import SimpleNamespace

import pytest

from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    SUMMARY_PREFIX,
    _SUMMARY_END_MARKER,
)
from agent.prompt_builder import STEER_DISPLAY_KIND


@pytest.mark.parametrize("trailing, expected", [
    ({"role": "user", "content": "Machine follow-through", "display_kind": "hidden"}, "Audit the fork"),
    ({"role": "user", "content": "Keep Hermes stopped", "display_kind": STEER_DISPLAY_KIND}, "Keep Hermes stopped"),
    ({"role": "user", "content": f"{SUMMARY_PREFIX}\nMachine summary", COMPRESSED_SUMMARY_METADATA_KEY: True}, "Audit the fork"),
    ({"role": "user", "content": f"{SUMMARY_PREFIX}\nMachine summary\n\n{_SUMMARY_END_MARKER}\n\nKeep Hermes stopped",
      COMPRESSED_SUMMARY_METADATA_KEY: True, "display_kind": "hidden"}, "Keep Hermes stopped"),
])
def test_dispatch_preserves_human_task(monkeypatch, trailing, expected):
    from agent.tool_executor import _ToolCallRef, _resolve_sequential_dispatch
    import model_tools

    captured = []

    def transport(name, args, task_id, **kwargs):
        captured.append(kwargs["user_task"])
        return "ok"

    monkeypatch.setattr(model_tools, "handle_function_call", transport)
    agent = SimpleNamespace(_context_engine_tool_names=set(), _memory_manager=None,
                            session_id="fixture", valid_tool_names={"fixture"}, quiet_mode=False)
    ref = _ToolCallRef("fixture", {}, "task", "call", [])
    messages = [{"role": "user", "content": "Audit the fork"}, trailing]
    import copy
    before = copy.deepcopy(messages)
    dispatch = _resolve_sequential_dispatch(agent, ref, messages)
    assert dispatch.execute({}) == "ok"
    assert captured == [expected]
    assert messages == before

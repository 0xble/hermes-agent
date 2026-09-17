"""Persisted tool results must project to valid Chat Completions content, without history edits."""

from copy import deepcopy
import json

import httpx
from openai import OpenAI
import pytest

from agent.tool_dispatch_helpers import make_tool_result_message
from agent.transports.chat_completions import ChatCompletionsTransport
from agent.vision_message_prep import VisionMessagePrepMixin
from hermes_state import SessionDB
from tools.tool_result_storage import maybe_persist_tool_result


@pytest.mark.parametrize("model", ["grok-4.6", "gemini-2.5-pro", "gpt-5"])
@pytest.mark.parametrize("content", [
    {"status": "dispatched", "delegation_id": "review-1", "nested": {"ok": True}, "text": "✓"},
    "already textual",
    [{"type": "text", "text": "screenshot"},
     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}],
])
def test_persisted_tool_result_projects_only_object_content(tmp_path, model, content):
    tool_call = {"id": "call_review", "type": "function", "function": {
        "name": "review_changes", "arguments": "{}",
    }}
    # Text/object results pass through the executor's spill and vision seams.
    # Existing multimodal lists are already the output of the vision seam.
    persisted = maybe_persist_tool_result(content, "review_changes", "call_review")
    projected = VisionMessagePrepMixin()._tool_result_content_for_active_model("review_changes", persisted)
    history = [
        {"role": "user", "content": "Review this change"},
        {"role": "assistant", "content": None, "tool_calls": [tool_call]},
        make_tool_result_message("review_changes", projected, "call_review"),
    ]
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("review-replay", source="cli")
        db.append_messages_batch("review-replay", history)
        replay = db.get_messages_as_conversation("review-replay")
        before = deepcopy(replay)
        assert replay[-1]["content"] == content
        transport = ChatCompletionsTransport()
        kwargs = transport.build_kwargs(model, replay)
        # Exercise the real OpenAI SDK JSON encoder, not merely kwargs shape.
        captured = []

        def receive(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "local-test", "object": "chat.completion", "created": 0,
                "model": model, "choices": [],
            })

        with OpenAI(api_key="local-test-only", base_url="https://local.invalid/v1",
                    http_client=httpx.Client(transport=httpx.MockTransport(receive))) as client:
            client.chat.completions.create(**kwargs)
        sent = captured[0]["messages"][-1]
        if isinstance(content, dict):
            assert isinstance(sent["content"], str)
            assert json.loads(sent["content"]) == content
        else:
            assert sent["content"] == content
        assert sent["tool_call_id"] == "call_review"
        assert "name" not in sent and "tool_name" not in sent
        assert captured[0]["messages"][-2]["tool_calls"] == [tool_call]
        assert replay == before
        assert db.get_messages_as_conversation("review-replay") == before
        assert transport.build_kwargs(model, replay) == kwargs
        # A second conversion must be idempotent, including already repaired text.
        assert transport.convert_messages(kwargs["messages"], model=model) == kwargs["messages"]
        # Normalization must also run when no internal metadata needs stripping.
        bare = {"role": "tool", "tool_call_id": "call_review", "content": content}
        bare_before = deepcopy(bare)
        assert transport.convert_messages([bare], model=model)[0]["content"] == sent["content"]
        assert bare == bare_before
    finally:
        db.close()

"""Gateway vision pre-process merges the analysis with the user text."""

import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_enrich_message_with_vision_merges_analysis_without_output_cap():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)

    with patch(
        "tools.vision_tools.vision_analyze_tool",
        new_callable=AsyncMock,
        return_value=json.dumps({"success": True, "analysis": "A cat on a chair."}),
    ) as mock_vision:
        result = await runner._enrich_message_with_vision(
            user_text="What is happening here?",
            image_paths=["/tmp/cat.png"],
        )

    assert "A cat on a chair." in result
    assert "What is happening here?" in result
    # No output cap is forwarded: per the max-tokens-knob policy the aux
    # client decides token handling; conciseness comes from the prompt.
    assert "max_tokens" not in mock_vision.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("caption", ["", "Read the labels.", "请读标签。"])
async def test_auto_description_requests_english_without_translating_quoted_text(caption):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    image_path = "/image-cache/status.png"
    analysis = 'A status card labeled "运行中" shows identifier JOB-42 and value 17.'
    with patch(
        "tools.vision_tools.vision_analyze_tool",
        new_callable=AsyncMock,
        return_value=json.dumps({"success": True, "analysis": analysis}),
    ) as vision:
        result = await runner._enrich_message_with_vision(caption, [image_path])

    vision.assert_awaited_once()
    assert vision.await_args is not None
    request = vision.await_args.kwargs
    prompt = request["user_prompt"].lower()
    assert "in english" in prompt
    assert "quoted text" in prompt and "identifiers" in prompt
    assert "original language" in prompt
    assert set(request) == {"image_url", "user_prompt"}
    assert request["image_url"] == image_path
    expected = (
        f"[The user sent an image~ Here's what I can see:\n{analysis}]\n"
        f"[If you need a closer look, use vision_analyze with image_url: {image_path} ~]"
    )
    assert result == (f"{expected}\n\n{caption}" if caption else expected)

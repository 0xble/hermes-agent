"""A summary carrier's live request, not its history, governs split eligibility."""

from copy import deepcopy
from unittest.mock import patch

import pytest

from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    SUMMARY_PREFIX,
    STEER_DISPLAY_KIND,
    _MERGED_PRIOR_CONTEXT_HEADER,
    _MERGED_SUMMARY_DELIMITER,
    _SUMMARY_END_MARKER,
    _estimate_msg_budget_tokens,
    user_originated_turn_view,
)
from tests.agent.test_split_turn_compaction import (
    _ACTIVE_REQUEST,
    _assert_tool_pairs_are_complete,
    _make_compressor,
    _oversized_active_turn,
)


@pytest.mark.parametrize("layout", ["leading", "merged"])
@pytest.mark.parametrize("display_kind", [None, "hidden", STEER_DISPLAY_KIND])
@pytest.mark.parametrize(
    "payload, can_split",
    [
        (_ACTIVE_REQUEST, True),
        ([{"type": "text", "text": _ACTIVE_REQUEST}], True),
        (("large-request " * 2_000).strip(), False),
        ([{"type": "audio", "source": {"data": "AA=="}}], False),
    ],
    ids=["text", "text-parts", "oversized-request", "audio"],
)
def test_carrier_split_uses_live_request_size_and_media(layout, display_kind, payload, can_split):
    summary = f"{SUMMARY_PREFIX}\n" + "Historical reference.\n" * 2_000 + f"\n{_SUMMARY_END_MARKER}"
    if isinstance(payload, str):
        content = (
            f"{summary}\n\n{payload}" if layout == "leading" else
            f"{_MERGED_PRIOR_CONTEXT_HEADER}\n{payload}\n\n{_MERGED_SUMMARY_DELIMITER}\n{summary}"
        )
    elif layout == "leading":
        content = [{"type": "text", "text": summary}, *payload]
    else:
        content = [
            {"type": "text", "text": _MERGED_PRIOR_CONTEXT_HEADER},
            *payload,
            {"type": "text", "text": f"{_MERGED_SUMMARY_DELIMITER}\n{summary}"},
        ]
    messages = _oversized_active_turn()
    carrier = {"role": "user", "content": content, COMPRESSED_SUMMARY_METADATA_KEY: True}
    if display_kind is not None:
        carrier["display_kind"] = display_kind
    messages[3] = carrier
    before = deepcopy(messages)
    compressor = _make_compressor()
    head = compressor._protect_head_size(messages)

    assert compressor._find_last_user_message_idx(messages, head) == 3
    live = user_originated_turn_view(carrier)
    assert live is not None
    assert live["content"] == payload
    assert _estimate_msg_budget_tokens(carrier) > compressor._tail_soft_ceiling(compressor.tail_token_budget)
    cut = compressor._find_tail_cut_by_tokens(messages, head)

    assert (cut > 3) is can_split
    _assert_tool_pairs_are_complete(messages[head:cut])
    _assert_tool_pairs_are_complete(messages[cut:])
    assert messages == before
    if can_split:
        with patch.object(compressor, "_generate_summary", return_value=None):
            compressed = compressor.compress(messages, current_tokens=90_000, force=True)
        live_text = "\n".join(
            str(view["content"]) for row in compressed
            if (view := user_originated_turn_view(row)) is not None
        )
        assert live_text.count(_ACTIVE_REQUEST) == 1
        assert "older request" not in live_text
        _assert_tool_pairs_are_complete(compressed)

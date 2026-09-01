from __future__ import annotations

import json

import pytest

from scripts.ci.compare_files import extract_complete_file_list


def _compare_payload(filenames: list[str]) -> str:
    return json.dumps({"files": [{"filename": filename} for filename in filenames]})


def test_complete_compare_file_list_is_emitted() -> None:
    payload = _compare_payload(["hermes.py", "website/docs.md"])
    assert extract_complete_file_list(payload) == ["hermes.py", "website/docs.md"]


def test_compare_file_cap_fails_so_action_can_fail_open() -> None:
    payload = _compare_payload([f"docs/{index}.md" for index in range(300)])
    with pytest.raises(ValueError, match="300-file cap"):
        extract_complete_file_list(payload)


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({}),
        json.dumps({"files": "not-a-list"}),
        json.dumps({"files": [{}]}),
    ],
)
def test_malformed_compare_response_fails_closed(payload: str) -> None:
    with pytest.raises(ValueError, match="compare response"):
        extract_complete_file_list(payload)
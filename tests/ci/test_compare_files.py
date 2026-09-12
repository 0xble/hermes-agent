from __future__ import annotations

import json

import pytest

from scripts.ci.compare_files import extract_complete_file_list


def _compare_payload(filenames: list[str]) -> str:
    return json.dumps({"files": [{"filename": filename} for filename in filenames]})


def test_complete_compare_file_list_is_emitted() -> None:
    payload = _compare_payload(["hermes.py", "website/docs.md"])
    assert extract_complete_file_list(payload) == ["hermes.py", "website/docs.md"]


def test_renamed_file_emits_source_and_destination_paths() -> None:
    payload = json.dumps(
        {
            "files": [
                {
                    "filename": "docs/pyproject.md",
                    "previous_filename": "pyproject.toml",
                    "status": "renamed",
                }
            ]
        }
    )
    assert extract_complete_file_list(payload) == ["pyproject.toml", "docs/pyproject.md"]


def test_renamed_file_without_source_path_fails_closed() -> None:
    payload = json.dumps({"files": [{"filename": "docs/new.md", "status": "renamed"}]})
    with pytest.raises(ValueError, match="previous_filename"):
        extract_complete_file_list(payload)


@pytest.mark.parametrize("count", [300, 301])
def test_compare_file_cap_selects_conservative_all_lanes(count: int) -> None:
    payload = _compare_payload([f"docs/{index}.md" for index in range(count)])
    assert extract_complete_file_list(payload) == []


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
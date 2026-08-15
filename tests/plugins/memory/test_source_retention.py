"""Source-aware Hindsight candidate policy tests."""

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugins.memory.hindsight import HindsightMemoryProvider
from plugins.memory.hindsight.source_retention import SourceCandidate, discover_source_candidates


def _tool_turn(name, arguments, result, call_id="call-1"):
    return [
        {"role": "user", "content": "Use the source for the answer."},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": result},
        {"role": "assistant", "content": "Done."},
    ]


def test_substantive_web_extraction_is_preserved_with_provenance():
    content = "Source paragraph. " * 60
    candidates = discover_source_candidates(
        _tool_turn("web_extract", {"url": "HTTPS://Example.com/page#fragment"}, content),
        session_id="session-1",
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_type == "webpage"
    assert candidate.source_id.startswith("webpage-")
    assert candidate.source_shape == "complete_extraction"
    assert candidate.metadata["source_origin"] == "https://example.com/page"
    assert candidate.metadata["retained_automatically"] == "true"
    assert "stale" in candidate.context


def test_incidental_search_results_and_short_outputs_are_skipped():
    assert discover_source_candidates(
        _tool_turn("web_search", {"query": "example"}, "Result " * 200)
    ) == []
    assert discover_source_candidates(
        _tool_turn("web_extract", {"url": "https://example.com"}, "too short")
    ) == []


def test_transcript_is_complete_source_and_secret_bearing_content_is_skipped():
    transcript = "Speaker A: substantive transcript. " * 60
    candidates = discover_source_candidates(
        _tool_turn("speech_to_text", {"file_path": "meeting.m4a"}, transcript)
    )
    assert len(candidates) == 1
    assert candidates[0].source_type == "transcript"
    assert candidates[0].source_id.startswith("transcript-")

    secret = "api_key = sk_live_12345678901234567890\n" + ("x " * 400)
    assert discover_source_candidates(
        _tool_turn("web_extract", {"url": "https://example.com"}, secret)
    ) == []


def test_durable_artifact_is_retained_but_scratch_and_code_are_skipped():
    content = "Report finding. " * 60
    retained = discover_source_candidates(
        _tool_turn(
            "write_file",
            {"path": "/Users/brianle/Vault/Reports/review.md", "content": content},
            "written",
        )
    )
    assert len(retained) == 1
    assert retained[0].source_type == "artifact"

    assert discover_source_candidates(
        _tool_turn(
            "write_file",
            {"path": "/tmp/scratch.md", "content": content},
            "written",
        )
    ) == []
    assert discover_source_candidates(
        _tool_turn(
            "write_file",
            {"path": "/Users/brianle/Workspaces/project/main.py", "content": content},
            "written",
        )
    ) == []


def test_current_turn_boundary_and_duplicate_content_are_deterministic():
    old = _tool_turn("web_extract", {"url": "https://old.example"}, "old " * 200)
    new = _tool_turn("web_extract", {"url": "https://new.example"}, "new " * 200)
    messages = old + new
    candidates = discover_source_candidates(messages)
    assert len(candidates) == 1
    assert candidates[0].source_id.startswith("webpage-")

    duplicate = _tool_turn("web_extract", {"url": "https://new.example"}, "new " * 200)
    candidates = discover_source_candidates(duplicate + duplicate[1:])
    assert len(candidates) == 1


def test_attachment_bytes_require_explicit_opt_in(tmp_path, monkeypatch):
    path = tmp_path / "confidential.pdf"
    path.write_bytes(b"private source bytes")
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "file_attachment",
                    "file_id": "file-1",
                    "file_path": str(path),
                }
            ],
        }
    ]
    reads = 0
    original_read_bytes = Path.read_bytes

    def tracked_read_bytes(candidate):
        nonlocal reads
        reads += 1
        return original_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", tracked_read_bytes)

    assert discover_source_candidates(messages) == []
    assert reads == 0

    candidates = discover_source_candidates(messages, retain_attachments=True)
    assert len(candidates) == 1
    assert candidates[0].file_path == str(path.resolve())
    assert reads == 1


def _source_candidate(tmp_path):
    path = tmp_path / "source.txt"
    path.write_text("source material", encoding="utf-8")
    return SourceCandidate(
        source_type="user_file",
        source_id="attachment:file-1",
        context="Non-sensitive user source.",
        file_path=str(path),
        metadata={"content_hash": "hash-1"},
        tags=("source:user_file",),
        content_hash="hash-1",
    )


def _provider_for_source_tests():
    provider = object.__new__(HindsightMemoryProvider)
    provider._source_retain_keys = set()
    provider._source_retain_keys_lock = threading.Lock()
    provider._pending_retain_ops = set()
    provider._pending_retain_ops_lock = threading.Lock()
    provider._source_ledger = {}
    provider._source_retain_ops = {}
    provider._source_retain_verified = set()
    return provider


def test_automatic_source_retain_is_deduplicated_and_tracked(tmp_path):
    provider = _provider_for_source_tests()
    client = MagicMock()
    provider._run_hindsight_operation = MagicMock(
        return_value=SimpleNamespace(operation_ids=["source-op-1"])
    )
    candidate = _source_candidate(tmp_path)

    provider._retain_source_candidate(candidate, "test-bank")
    provider._retain_source_candidate(candidate, "test-bank")

    provider._run_hindsight_operation.assert_called_once()
    assert provider._source_retain_ops["source-op-1"] == candidate


def test_automatic_source_retain_fails_soft_and_allows_bounded_retry(tmp_path):
    provider = _provider_for_source_tests()
    client = MagicMock()
    provider._run_hindsight_operation = MagicMock(
        side_effect=RuntimeError("Hindsight unavailable")
    )
    candidate = _source_candidate(tmp_path)

    provider._retain_source_candidates([candidate], "test-bank")

    assert candidate.automatic_key not in provider._source_retain_keys
    provider._run_hindsight_operation.assert_called_once()


def test_completed_source_operation_requires_document_readback(tmp_path):
    provider = _provider_for_source_tests()
    candidate = _source_candidate(tmp_path)
    provider._source_retain_ops["source-op-1"] = candidate
    provider._run_hindsight_operation = lambda operation: SimpleNamespace(status="completed")
    provider._verify_source_candidate = lambda bank_id, candidate: True

    assert provider._is_retain_op_complete("test-bank", "source-op-1") is True
    assert "source-op-1" not in provider._source_retain_ops


def test_long_pasted_source_is_retained_but_short_prompt_is_not():
    pasted = "Source: contract excerpt\n\n" + "\n".join(
        f"A substantive paragraph {index}." for index in range(120)
    )
    messages = [{"role": "user", "content": pasted}]
    candidates = discover_source_candidates(messages)
    assert len(candidates) == 1
    assert candidates[0].source_type == "pasted_source"
    assert "Brian's decision" in candidates[0].context
    assert discover_source_candidates([{"role": "user", "content": "short request"}]) == []


def test_read_file_extraction_is_preserved_and_secret_path_is_skipped():
    content = "Document source text. " * 60
    extracted = discover_source_candidates(
        _tool_turn("read_file", {"path": "/Users/brianle/Vault/source.md"}, content)
    )
    assert len(extracted) == 1
    assert extracted[0].source_type == "file_extraction"
    skipped = discover_source_candidates(
        _tool_turn("read_file", {"path": "/Users/brianle/.env"}, content)
    )
    assert skipped == []

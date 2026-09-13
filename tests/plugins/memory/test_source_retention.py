"""Source-aware Hindsight candidate policy tests."""

import json
import threading

import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugins.memory.hindsight import HindsightMemoryProvider
from plugins.memory.hindsight.source_retention import (
    SourceCandidate,
    discover_source_candidates,
    read_verified_source_file,
)


def _tool_turn(name, arguments, result, call_id="call-1"):
    # Source-success fixtures use the native producer envelopes, not bare text.
    if name == "web_extract":
        result = json.dumps({"results": [{"url": arguments["url"], "content": result, "error": None}]})
    elif name in {"youtube_transcript", "speech_to_text"}:
        result = json.dumps({"success": True, "transcript": result})
    elif name == "read_file":
        result = json.dumps({"content": result, "total_lines": 1,
                             "file_size": len(result.encode()), "truncated": False})
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


def test_substantive_web_extraction_requires_explicit_tool_source_opt_in():
    content = "Source paragraph. " * 60
    messages = _tool_turn(
        "web_extract", {"url": "HTTPS://Example.com/page#fragment"}, content
    )
    assert discover_source_candidates(messages, session_id="session-1") == []

    candidates = discover_source_candidates(
        messages,
        session_id="session-1",
        retain_tool_sources=True,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_type == "webpage"
    assert candidate.source_id.startswith("webpage-")
    assert candidate.source_shape == "complete_extraction"
    assert candidate.metadata["source_origin"] == "https://example.com/page"
    assert candidate.metadata["retained_automatically"] == "true"
    assert "stale" in candidate.context


def test_web_source_urls_strip_credentials_and_query_values():
    content = "Source paragraph. " * 60
    credentialed = discover_source_candidates(
        _tool_turn(
            "web_extract",
            {
                "url": (
                    "https://user:password@Example.com/page?"
                    "X-Amz-Credential=secret&token=abc#access_token=also-hidden"
                )
            },
            content,
        ),
        session_id="credentialed-url",
        retain_tool_sources=True,
    )[0]
    clean = discover_source_candidates(
        _tool_turn("web_extract", {"url": "https://example.com/page"}, content),
        session_id="credentialed-url",
        retain_tool_sources=True,
    )[0]

    assert credentialed.metadata["source_origin"] == "https://example.com/page"
    assert credentialed.source_id == clean.source_id
    serialized = json.dumps(credentialed.metadata)
    assert "password" not in serialized
    assert "secret" not in serialized
    assert "token" not in serialized


def test_youtube_source_identity_keeps_only_the_public_video_key():
    content = "Transcript paragraph. " * 60

    def candidate(url: str):
        return discover_source_candidates(
            _tool_turn("youtube_transcript", {"url": url}, content),
            session_id="youtube-identity",
            retain_tool_sources=True,
        )[0]

    first = candidate(
        "https://user:password@www.youtube.com/watch?"
        "v=AAAAAAAAAAA&token=secret&t=30#fragment"
    )
    same_video = candidate(
        "https://www.youtube.com/watch?si=tracking&v=AAAAAAAAAAA"
    )
    other_video = candidate(
        "https://www.youtube.com/watch?v=BBBBBBBBBBB&token=other-secret"
    )

    assert first.source_id == same_video.source_id
    assert first.source_id != other_video.source_id
    assert first.metadata["source_origin"] == "https://www.youtube.com/watch"
    assert other_video.metadata["source_origin"] == "https://www.youtube.com/watch"
    serialized = json.dumps([first.metadata, other_video.metadata])
    for sensitive in ("password", "secret", "token", "tracking", "AAAAAAAAAAA"):
        assert sensitive not in serialized


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
        _tool_turn("speech_to_text", {"file_path": "meeting.m4a"}, transcript),
        retain_tool_sources=True,
    )
    assert len(candidates) == 1
    assert candidates[0].source_type == "transcript"
    assert candidates[0].source_id.startswith("transcript-")

    secret = "api_key = sk_live_12345678901234567890\n" + ("x " * 400)
    assert discover_source_candidates(
        _tool_turn("web_extract", {"url": "https://example.com"}, secret),
        retain_tool_sources=True,
    ) == []


def test_durable_artifact_is_retained_but_scratch_and_code_are_skipped():
    content = "Report finding. " * 60
    retained = discover_source_candidates(
        _tool_turn(
            "write_file",
            {"path": "/Users/brianle/Vault/Reports/review.md", "content": content},
            json.dumps({"bytes_written": len(content.encode("utf-8")), "verified": True}),
        ),
        retain_tool_sources=True,
    )
    assert len(retained) == 1
    assert retained[0].source_type == "artifact"

    assert discover_source_candidates(
        _tool_turn(
            "write_file",
            {"path": "/tmp/scratch.md", "content": content},
            json.dumps({"bytes_written": len(content.encode("utf-8")), "verified": True}),
        ),
        retain_tool_sources=True,
    ) == []
    assert discover_source_candidates(
        _tool_turn(
            "write_file",
            {"path": "/Users/brianle/Workspaces/project/main.py", "content": content},
            json.dumps({"bytes_written": len(content.encode("utf-8")), "verified": True}),
        ),
        retain_tool_sources=True,
    ) == []


def test_current_turn_boundary_and_duplicate_content_are_deterministic():
    old = _tool_turn("web_extract", {"url": "https://old.example"}, "old " * 200)
    new = _tool_turn("web_extract", {"url": "https://new.example"}, "new " * 200)
    messages = old + new
    candidates = discover_source_candidates(messages, retain_tool_sources=True)
    assert len(candidates) == 1
    assert candidates[0].source_id.startswith("webpage-")

    duplicate = _tool_turn("web_extract", {"url": "https://new.example"}, "new " * 200)
    candidates = discover_source_candidates(
        duplicate + duplicate[1:], retain_tool_sources=True
    )
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

    assert discover_source_candidates(messages, retain_attachments=True) == []
    assert reads == 0

    candidates = discover_source_candidates(
        messages, retain_attachments=True, attachment_roots=(tmp_path,)
    )
    assert len(candidates) == 1
    assert candidates[0].file_path == str(path.resolve())
    assert reads == 0
    assert read_verified_source_file(candidates[0], (tmp_path,)) == b"private source bytes"
    path.write_bytes(b"replaced after discovery")
    assert read_verified_source_file(candidates[0], (tmp_path,)) is None


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

    with patch("plugins.memory.hindsight.read_verified_source_file",
               return_value=b"source material"):
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

    with patch("plugins.memory.hindsight.read_verified_source_file",
               return_value=b"source material"):
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
    assert "user's own statements or decisions" in candidates[0].context
    assert discover_source_candidates([{"role": "user", "content": "short request"}]) == []


def test_read_file_extraction_requires_explicit_opt_in_and_secret_paths_stay_blocked():
    content = "Document source text. " * 60
    messages = _tool_turn(
        "read_file", {"path": "/Users/brianle/Vault/source.md"}, content
    )
    assert discover_source_candidates(messages) == []

    extracted = discover_source_candidates(
        messages,
        retain_tool_sources=True,
        retain_file_extractions=True,
    )
    assert len(extracted) == 1
    assert extracted[0].source_type == "file_extraction"
    skipped = discover_source_candidates(
        _tool_turn("read_file", {"path": "/Users/brianle/.env"}, content),
        retain_tool_sources=True,
        retain_file_extractions=True,
    )
    assert skipped == []


@pytest.mark.parametrize("readback", [
    None, {}, SimpleNamespace(id="attachment:file-1"),
    SimpleNamespace(document_metadata={"content_hash": "hash-1"}),
    SimpleNamespace(id="wrong", document_metadata={"content_hash": "hash-1"}),
    SimpleNamespace(id="attachment:file-1", document_metadata={"content_hash": "wrong"}),
])
def test_source_completion_requires_matching_identity_and_hash(tmp_path, readback):
    provider = _provider_for_source_tests()
    candidate = _source_candidate(tmp_path)
    provider._run_hindsight_operation = lambda operation: readback
    assert provider._verify_source_candidate("bank", candidate) is False
    assert candidate.automatic_key not in provider._source_retain_verified
    assert candidate.automatic_key not in provider._source_ledger
    provider._run_hindsight_operation = lambda operation: SimpleNamespace(
        id=candidate.source_id, document_metadata={"content_hash": candidate.content_hash},
    )
    assert provider._verify_source_candidate("bank", candidate) is True
    assert provider._source_ledger[candidate.automatic_key]["status"] == "completed"


@pytest.mark.parametrize("status", ["completed", "gone"])
def test_missing_readback_keeps_source_operation_pending(tmp_path, status):
    from hindsight_client_api.exceptions import NotFoundException

    provider = _provider_for_source_tests()
    candidate = _source_candidate(tmp_path)
    provider._source_retain_ops["op"] = candidate
    outcomes = iter([NotFoundException() if status == "gone" else SimpleNamespace(status=status), None])

    def respond(operation):
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    provider._run_hindsight_operation = respond
    assert provider._is_retain_op_complete("bank", "op") is False
    assert provider._source_retain_ops["op"] is candidate
    assert candidate.automatic_key not in provider._source_retain_verified


@pytest.mark.parametrize("result", [
    None, "", "written", "{}", "[]", '{"error":"Permission denied"}',
    '{"success":false}', '{"verified":true,"bytes_written":true}',
    '{"verified":true,"bytes_written":1}',
])
def test_unverified_or_unanswered_write_is_not_artifact(result):
    content = "Report evidence. " * 60
    messages = _tool_turn("write_file", {"path": "/Users/probe/Documents/report.md", "content": content}, result)
    if result is None:
        messages = [message for message in messages if message["role"] != "tool"]
    assert discover_source_candidates(messages, retain_tool_sources=True) == []


@pytest.mark.parametrize("changes", [{}, {"verified": None}, {"error": "write failed"}, {"success": False}])
def test_artifact_requires_native_verified_write_acknowledgment(changes):
    from tools.file_operations_common import WriteResult

    content = "Unicode report evidence é. " * 60
    acknowledgment = WriteResult(bytes_written=len(content.encode("utf-8")), verified=True).to_dict()
    acknowledgment.update(changes)
    messages = _tool_turn("write_file", {"path": "/Users/probe/Documents/report.md", "content": content}, json.dumps(acknowledgment))
    # An unrelated result cannot certify this write.
    messages.insert(2, {"role": "tool", "tool_call_id": "unrelated", "content": json.dumps(WriteResult(bytes_written=len(content.encode("utf-8")), verified=True).to_dict())})
    candidates = discover_source_candidates(messages, retain_tool_sources=True)
    assert bool(candidates) is (not changes)
    if candidates:
        assert candidates[0].content == content.strip()


def test_valid_sdk_document_without_source_hash_cannot_verify(tmp_path):
    from hindsight_client_api.models.document_response import DocumentResponse

    provider = _provider_for_source_tests()
    candidate = _source_candidate(tmp_path)
    document = DocumentResponse(
        id=candidate.source_id, bank_id="bank", original_text="wrong content", content_hash="wrong hash",
        created_at="2026-09-11", updated_at="2026-09-11", memory_unit_count=0,
    )
    provider._run_hindsight_operation = lambda operation: document
    assert provider._verify_source_candidate("bank", candidate) is False
    assert candidate.automatic_key not in provider._source_retain_verified


def test_query_addressed_documents_keep_distinct_private_identities():
    def candidate(query):
        return discover_source_candidates(_tool_turn("web_extract", {
            "url": "https://example.com/document?" + query}, "Source paragraph. " * 60),
            session_id="query-identity", retain_tool_sources=True)[0]
    first = candidate("id=1&token=secret")
    assert first.source_id != candidate("id=2&token=secret").source_id
    assert first.source_id == candidate("id=1&X-Amz-Credential=changed&%74oken=other").source_id
    assert candidate("id=1&id=2").source_id != candidate("id=2&id=1").source_id
    assert candidate("id=").source_id != candidate("").source_id
    assert first.metadata["source_origin"] == "https://example.com/document"
    assert "secret" not in json.dumps(first.metadata)


@pytest.mark.parametrize("matches", [True, False])
@pytest.mark.parametrize("file_response", [False, True])
def test_no_operation_id_source_requires_readback_and_releases_failed_dedup(tmp_path, matches, file_response):
    from hindsight_client_api.models.retain_response import RetainResponse
    from hindsight_client_api.models.file_retain_response import FileRetainResponse
    provider = _provider_for_source_tests()
    candidate = _source_candidate(tmp_path)
    response = FileRetainResponse(operation_ids=[]) if file_response else RetainResponse(
        success=True, bank_id="bank", items_count=1, **{"async": False})
    readback = SimpleNamespace(id=candidate.source_id, document_metadata={"content_hash": candidate.content_hash}) if matches else None
    provider._run_hindsight_operation = MagicMock(side_effect=[response, readback])
    with patch("plugins.memory.hindsight.read_verified_source_file", return_value=b"source material"):
        provider._retain_source_candidates([candidate], "bank")
    assert provider._run_hindsight_operation.call_count == 2
    assert provider._source_ledger[candidate.automatic_key]["status"] == ("completed" if matches else "failed")
    assert provider._source_candidate_already_submitted(candidate) is matches


def test_pasted_source_context_does_not_invent_human_identity():
    pasted = "Source: contract excerpt\n\n" + "\n".join(f"A substantive paragraph {n}." for n in range(120))
    candidate, = discover_source_candidates([{"role": "user", "content": pasted}])
    assert "Brian" not in candidate.context
    assert "user" in candidate.context
    assert candidate.content == pasted


@pytest.mark.parametrize('channel', ['paste', 'file'])
@pytest.mark.parametrize('quote', ['"', "'", ''])
@pytest.mark.parametrize('key', ['api_key', 'access_token', 'client_secret', 'password'])
def test_credential_assignments_are_excluded_from_source_discovery(channel, quote, key):
    def discover(field):
        content = 'Source: configuration reference\n' + ('Public descriptive source material.\n' * 80)
        content += f'{quote}{field}{quote}: {quote}synthetic-secret-value-12345{quote}\n'
        if quote == '"':
            content = json.dumps({'source': 'https://example.com/reference',
                'sections': ['Public descriptive source material. ' * 10] * 8,
                field: 'synthetic-secret-value-12345'}, indent=2)
        messages = ([{'role': 'user', 'content': content}] if channel == 'paste' else
                    _tool_turn('read_file', {'path': '/Documents/reference.json'}, content))
        return discover_source_candidates(messages, retain_tool_sources=True,
                                          retain_file_extractions=True)
    assert discover('description')  # ordinary quoted metadata remains eligible
    assert discover(key) == []


@pytest.mark.parametrize('kind', ['hidden', 'continuation'])
def test_synthetic_user_sources_do_not_displace_genuine_turn(kind, tmp_path):
    source = 'Retrieved reference paragraph. ' * 80
    messages = _tool_turn('web_extract', {'url': 'https://example.com/reference'}, source)
    synthetic = 'Source: synthetic context\n' + ('Synthetic followthrough context.\n' * 90)
    attachment = tmp_path / 'reference.pdf'
    attachment.write_bytes(b'synthetic attachment')
    messages.append({'role': 'user', 'display_kind': kind, 'content': [
        {'type': 'text', 'text': synthetic}, {'type': 'file', 'path': str(attachment)}]})
    candidates = discover_source_candidates(messages, retain_tool_sources=True,
                    retain_attachments=True, attachment_roots=[tmp_path])
    assert [(c.source_type, c.content) for c in candidates] == [('webpage', source.strip())]
    messages.append({'role': 'user', 'content': 'A new genuine request.'})
    assert discover_source_candidates(messages, retain_tool_sources=True,
                    retain_attachments=True, attachment_roots=[tmp_path]) == []

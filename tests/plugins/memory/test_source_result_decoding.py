"""Only successful complete source payloads cross automatic source retention."""
import asyncio
import json

import pytest

from plugins.memory.hindsight.source_retention import discover_source_candidates

TEXT = "Source discusses errors and failures without being an error response. " * 20


def turn(name, args, result):
    return [
        {"role": "user", "content": "Use this source."},
        {"role": "assistant", "tool_calls": [{"id": "source-call", "function": {
            "name": name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": "source-call", "content": result},
    ]


def discover(name, args, result, **kwargs):
    return discover_source_candidates(turn(name, args, result), retain_tool_sources=True, **kwargs)


@pytest.mark.parametrize("result", [
    {"error": "failure " * 100}, {"success": False, "content": TEXT},
    {"results": [{"url": "https://source.invalid/fail", "error": TEXT}]},
    {"results": [{"url": "https://source.invalid/fail", "content": TEXT, "blocked_by_policy": True}]},
    {"results": [{"url": "https://source.invalid/fail", "content": "short", "title": TEXT}]},
])
def test_web_errors_and_envelope_padding_never_become_source(result):
    assert discover("web_extract", {"urls": ["https://source.invalid/fail"]}, json.dumps(result)) == []


@pytest.mark.parametrize("result", ["Error extracting content: " + TEXT, TEXT, json.dumps([TEXT]), "{"])
def test_unknown_raw_web_output_is_not_success_evidence(result):
    assert discover("web_extract", {"urls": ["https://source.invalid"]}, result) == []


def test_actual_web_producer_batch_extracts_only_successful_page_payloads(monkeypatch):
    from tools import web_tools
    urls = ["https://source.invalid/a?id=1&token=secret", "https://source.invalid/b", "https://source.invalid/c"]
    monkeypatch.setattr(web_tools, "_validate_extract_urls", lambda values: (values, list(range(len(values))), {}, None))
    async def safe(url):
        return True
    monkeypatch.setattr(web_tools, "async_is_safe_url", safe)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "fixture")
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_resolve_extract_provider", lambda backend: (object(), None))
    async def extract(provider, values, format):
        return [{"url": urls[0], "content": TEXT, "title": "Page A"},
                {"url": urls[1], "content": TEXT + "Second page", "title": "Page B"},
                {"url": urls[2], "error": TEXT}]
    monkeypatch.setattr(web_tools, "_extract_safe_urls", extract)
    result = asyncio.run(web_tools.web_extract_tool(urls))
    candidates = discover("web_extract", {"urls": urls}, result)
    assert [c.content for c in candidates] == [TEXT.strip(), (TEXT + "Second page").strip()]
    assert len({c.source_id for c in candidates}) == 2
    assert [c.metadata["source_origin"] for c in candidates] == ["https://source.invalid/a", urls[1]]
    assert "secret" not in json.dumps([c.metadata for c in candidates])


def test_truncated_web_payload_is_not_complete_source():
    result = {"results": [{"url": "https://source.invalid", "content": TEXT + "\n──────── [TRUNCATED] ────────\n"}]}
    assert discover("web_extract", {"urls": ["https://source.invalid"]}, json.dumps(result)) == []


@pytest.mark.parametrize("extra", [
    {"error": TEXT}, {"success": False}, {"truncated": True}, {"is_binary": True},
    {"is_image": True}, {"status": "unchanged", "content_returned": False},
])
def test_file_failure_partial_and_no_content_envelopes_are_rejected(extra):
    result = {"content": TEXT, "total_lines": 1, "file_size": len(TEXT), "truncated": False, **extra}
    assert discover("read_file", {"path": "source.md"}, json.dumps(result), retain_file_extractions=True) == []


def test_actual_read_file_payload_and_offset_contract(tmp_path, monkeypatch):
    from tools import file_tools
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    path = tmp_path / "source.md"
    path.write_text(TEXT + "\nsecond line")
    result = file_tools.read_file_tool(str(path), task_id="source-decoder")
    # Allow the disposable test directory as an origin only in this fixture.
    monkeypatch.setattr("plugins.memory.hindsight.source_retention._is_ephemeral_path", lambda path: False)
    args = {"path": str(path)}
    candidates = discover("read_file", args, result, retain_file_extractions=True)
    assert len(candidates) == 1
    assert candidates[0].content == json.loads(result)["content"].strip()
    assert discover("read_file", args, result) == []
    assert discover("read_file", {**args, "offset": 2}, result, retain_file_extractions=True) == []
    unchanged = file_tools.read_file_tool(str(path), task_id="source-decoder")
    assert discover("read_file", args, unchanged, retain_file_extractions=True) == []


def test_native_stt_success_body_is_retained_failure_and_unknown_body_are_not():
    from tools.transcription_common import _ok_result, _error_result
    args = {"file_path": "meeting.m4a"}
    candidates = discover("speech_to_text", args, json.dumps(_ok_result(TEXT, "fixture")))
    assert [c.content for c in candidates] == [TEXT.strip()]
    assert discover("speech_to_text", args, json.dumps(_error_result(TEXT))) == []
    assert discover("speech_to_text", args, TEXT) == []


def test_sync_turn_queues_only_successful_source_payload_and_keeps_conversation(monkeypatch):
    from plugins.memory.hindsight import HindsightMemoryProvider
    provider = HindsightMemoryProvider()
    provider._auto_retain = True
    provider._retain_tool_sources = True
    provider._retain_every_n_turns = 1
    provider._observation_scopes = None
    monkeypatch.setattr(provider, "_ensure_writer", lambda: None)
    monkeypatch.setattr(provider, "_register_atexit", lambda: None)
    monkeypatch.setattr(provider, "_resolve_retain_target", lambda doc: ("conversation", None))
    monkeypatch.setattr(provider, "_track_retain_ops", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(provider, "_retain_batch", lambda item, **kwargs: calls.append((item, kwargs)))
    url = "https://source.invalid/page"
    for response in ({"error": TEXT}, {"results": [{"url": url, "content": TEXT, "error": None}]}):
        provider.sync_turn("Use this source", "Answer", messages=turn("web_extract", {"urls": [url]}, json.dumps(response)))
        while not provider._retain_queue.empty():
            provider._retain_queue.get_nowait()()
    source_calls = [(item, kw) for item, kw in calls if kw.get("document_id", "").startswith("webpage-")]
    assert len(source_calls) == 1
    assert source_calls[0][0]["content"] == TEXT.strip()
    assert len([kw for _, kw in calls if kw.get("document_id") == "conversation"]) == 2


def test_source_hash_ignores_metadata_changes_and_unknown_status_skips():
    url = "https://source.invalid/page"
    first = {"url": url, "content": TEXT, "error": None, "title": "First"}
    second = {**first, "title": "Updated title"}
    a = discover("web_extract", {"urls": [url]}, json.dumps({"results": [first]}))[0]
    b = discover("web_extract", {"urls": [url]}, json.dumps({"results": [second]}))[0]
    assert a.automatic_key == b.automatic_key
    assert discover("web_extract", {"urls": [url]}, json.dumps({"results": [{**first, "status": {"unexpected": True}}]})) == []

"""Attachment and text sources retain configured tags needed by filtered recall."""

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from plugins.memory import load_memory_provider
from plugins.memory.hindsight.source_retention import discover_source_candidates


@pytest.mark.parametrize("source_kind", ["attachment", "text"])
@pytest.mark.parametrize("configured,expected", [
    ("team:alpha, project:reports, team:alpha", ["team:alpha", "project:reports"]),
    ([" team:alpha ", "source:user_file", "team:alpha", ""], ["team:alpha", "source:user_file"]),
    ([], []),
])
def test_retained_source_tags_satisfy_configured_recall(
    tmp_path, monkeypatch, source_kind, configured, expected,
):
    from gateway.platforms.base import get_document_cache_dir

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source_file = get_document_cache_dir() / "report.txt"
    source_bytes = b"Original report source. " * 60
    source_file.write_bytes(source_bytes)
    if source_kind == "attachment":
        messages = [{"role": "user", "content": [
            {"type": "file_attachment", "file_path": str(source_file)},
        ]}]
    else:
        messages = [{"role": "user", "content": "Source: report\n\n" + "\n".join(
            f"Section {index}: substantive report evidence and conclusions." for index in range(60)
        )}]
    candidate, = discover_source_candidates(messages, retain_attachments=True)

    provider = load_memory_provider("hindsight", register_skills=False)
    provider._apply_retain_settings({
        "retain_tags": configured, "retain_attachments": True,
        "observation_scopes": [], "retain_source": "",
        "retain_user_prefix": "User", "retain_assistant_prefix": "Assistant",
    })
    provider._apply_recall_settings({"recall_tags": expected, "recall_tags_match": "all_strict"})
    retained = []
    recalls = []

    class SDK:
        def file_retain(self, *, bank_id, files, request, _request_timeout):
            assert files == [(source_file.name, source_bytes)]
            metadata, = json.loads(request)["files_metadata"]
            retained.append(metadata)
            return SimpleNamespace(operation_ids=["file-operation"])

        def aretain_batch(self, *, bank_id, items, document_id, retain_async):
            item, = items
            retained.append({**item, "document_id": document_id})
            return SimpleNamespace(operation_ids=["text-operation"])

        async def arecall(self, **kwargs):
            recalls.append(kwargs)
            tags = set(kwargs.get("tags") or [])
            return SimpleNamespace(results=[entry for entry in retained
                                            if tags.issubset(entry.get("tags", []))])

    client = SDK()
    client._files_api = client

    def dispatch(operation):
        result = operation(client)
        return asyncio.run(result) if inspect.isawaitable(result) else result

    monkeypatch.setattr(provider, "_run_hindsight_operation", dispatch)
    assert provider._retain_source_candidate(candidate, provider._bank_id)
    assert len(retained) == 1
    document = retained[0]
    assert document["tags"] == list(dict.fromkeys([*expected, *candidate.tags]))
    assert document["metadata"] == candidate.metadata
    assert document["document_id"] == candidate.source_id
    # Both automatic and explicit recall preserve the same configured filter.
    for explicit in (False, True):
        response = provider._compatible_recall("report", explicit=explicit)
        assert response.results == [document]
        if expected:
            assert recalls[-1]["tags"] == expected
            assert recalls[-1]["tags_match"] == "all_strict"
        else:
            assert "tags" not in recalls[-1]
    # Shaping never mutates the candidate's source tags or configured defaults.
    assert provider._retain_tags == expected

"""Opt-in live canary for automatic source retention.

Run only against a disposable local Hindsight bank:

    HERMES_SOURCE_RETENTION_CANARY=1 pytest -q tests/plugins/memory/test_source_retention_canary.py
"""

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from plugins.memory.hindsight import HindsightMemoryProvider
from plugins.memory.hindsight.source_retention import discover_source_candidates


pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_SOURCE_RETENTION_CANARY") != "1",
    reason="opt-in live disposable-bank canary",
)


def test_real_disposable_bank_source_retention(tmp_path, monkeypatch):
    from hindsight_client import Hindsight

    api_url = os.environ.get("HINDSIGHT_API_URL", "http://127.0.0.1:9177")
    bank_id = f"hermes-source-canary-{uuid.uuid4().hex[:12]}"
    client = Hindsight(base_url=api_url, timeout=180)
    created = False
    provider = None
    try:
        client.create_bank(bank_id, name="Hermes source retention canary")
        created = True
        source_file = tmp_path / "canary-source.txt"
        source_file.write_text("Original canary file evidence. " * 40, encoding="utf-8")
        web_text = "Complete canary webpage extraction. " * 40
        messages = [
            {
                "role": "user",
                "content": [{"type": "input_file", "file_path": str(source_file)}],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "canary-call",
                        "function": {
                            "name": "web_extract",
                            "arguments": json.dumps({"url": "https://canary.example/source"}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "canary-call", "content": web_text},
        ]
        candidates = discover_source_candidates(messages, session_id="canary-session")
        assert {candidate.source_type for candidate in candidates} == {"user_file", "webpage"}

        hermes_home = tmp_path / "hermes-home"
        (hermes_home / "hindsight").mkdir(parents=True)
        (hermes_home / "hindsight" / "config.json").write_text(
            json.dumps(
                {
                    "mode": "local_external",
                    "api_url": api_url,
                    "bank_id": bank_id,
                    "auto_retain": True,
                    "retain_async": True,
                    "timeout": 180,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        provider = HindsightMemoryProvider()
        provider.initialize(session_id="canary-session", hermes_home=str(hermes_home), platform="test")
        for candidate in candidates:
            provider._retain_source_candidate(candidate, bank_id)
        assert len(provider._pending_retain_ops) == 2

        deadline = time.monotonic() + 180
        assert provider._wait_for_server_retain_ops(deadline, 180)
        for candidate in candidates:
            document = provider._run_hindsight_operation(
                lambda client, document_id=candidate.source_id: client.documents.get_document(
                    bank_id=bank_id,
                    document_id=document_id,
                )
            )
            assert str(document.id) == candidate.source_id
            metadata = document.document_metadata or {}
            assert metadata.get("content_hash") == candidate.content_hash

        # The stable source identity/hash pair prevents a duplicate submission.
        before = len(provider._pending_retain_ops)
        provider._retain_source_candidate(candidates[0], bank_id)
        assert len(provider._pending_retain_ops) == before
    finally:
        if provider is not None:
            provider.shutdown()
        if created:
            client.delete_bank(bank_id)
        client.close()

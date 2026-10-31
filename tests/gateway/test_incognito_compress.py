"""Manual compression crosses the real gateway command and SQLite/RAM boundaries."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.incognito import event_scope
from hermes_state import AsyncSessionDB
from gateway.platforms.event import MessageEvent, MessageType
from tests.gateway.test_incognito_commands import harness


@pytest.fixture
def compression_boundary(harness, monkeypatch, tmp_path, request):
    runner, _store, _entry, _original_config, make_event, db = harness
    home = tmp_path
    # Keep the normal agent/compressor/provider lifecycle. Only external model and
    # Hindsight clients are deterministic, and every real store lives in this home.
    config = {
        "model": {"default": "gpt-4.1", "context_length": 128000},
        "compression": {
            "enabled": False, "in_place": True, "checkpoint_required": getattr(request, "param", True),
            "protect_first_n": 0, "protect_last_n": 2,
        },
        "memory": {"provider": "hindsight"},
        "platform_toolsets": {"telegram": []},
        "checkpoints": {"enabled": False},
    }
    import hermes_yaml
    (home / "config.yaml").write_text(hermes_yaml.safe_dump(config))
    (home / "hindsight").mkdir(exist_ok=True)
    (home / "hindsight" / "config.json").write_text(json.dumps({
        "mode": "cloud", "api_url": "https://hindsight.invalid", "bank_id": "compress-test",
        "auto_retain": True, "retain_every_n_turns": 1, "retain_async": False,
    }))
    from gateway import run
    monkeypatch.setattr(run, "_load_gateway_config", lambda: config)
    monkeypatch.setattr(run, "_resolve_gateway_model", lambda *a, **kw: "gpt-4.1")
    monkeypatch.setattr(run, "_resolve_runtime_agent_kwargs", lambda *a, **kw: {
        "provider": "openai", "api_key": "test-key", "api_mode": "chat_completions",
        "base_url": "https://model.invalid/v1",
    })
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", MagicMock())
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata", lambda *a, **kw: {})
    client = SimpleNamespace(
        aretain=AsyncMock(return_value=SimpleNamespace()),
        aretain_batch=AsyncMock(return_value=SimpleNamespace()),
        arecall=AsyncMock(return_value=SimpleNamespace(results=[])),
        aclose=AsyncMock(), close=MagicMock(),
    )
    from plugins.memory.hindsight import HindsightMemoryProvider
    monkeypatch.setattr(HindsightMemoryProvider, "_new_cloud_client", lambda self: client)
    summary_calls = []
    summary_text = "Compressed history: the saved discussion and private manual compression canary."

    def summarize(**kwargs):
        from agent.incognito import is_incognito
        summary_calls.append((deepcopy(kwargs["messages"]), is_incognito()))
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=summary_text), finish_reason="stop",
        )])

    monkeypatch.setattr("agent.context_compressor.call_llm", summarize)
    runner._session_db = AsyncSessionDB(db)
    return runner, db, home, client, summary_calls, summary_text, make_event("").source


def event(src, text):
    return MessageEvent(text=text, source=src, message_type=MessageType.TEXT)


async def dispatch(runner, src, text):
    """Use the same privacy scope, registry dispatch and executor as ingress."""
    incoming = event(src, text)
    with event_scope(runner, incoming):
        handled, result = await runner._hm_dispatch_canonical_command(
            incoming, src, runner._session_key_for_source(src), incoming.get_command(),
        )
    assert handled is True
    return result


def append_turn(runner, src, sid, user, assistant):
    # Both agent-backed and gateway-backed providers use this transcript sink.
    with event_scope(runner, event(src, user)):
        runner.session_store.append_to_transcript(sid, {"role": "user", "content": user})
        runner.session_store.append_to_transcript(sid, {"role": "assistant", "content": assistant})


def disk_snapshot(db):
    tables = [row[0] for row in db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    return {table: sorted((tuple(row) for row in db._conn.execute(f'SELECT * FROM "{table}"')), key=repr)
            for table in tables}


def assert_no_disk_text(home, *values):
    for path in home.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            for value in values:
                assert value.encode() not in content, f"private text reached {path.relative_to(home)}"


@pytest.mark.asyncio
@pytest.mark.parametrize("compression_boundary", [True, False], indirect=True)
async def test_manual_compress_keeps_private_handoff_in_ram_and_off_restores_prefix(compression_boundary):
    runner, db, home, hindsight, calls, summary, src = compression_boundary
    entry = runner.session_store.get_or_create_session(src)
    sid, _key = entry.session_id, entry.session_key
    for turn in range(4):
        append_turn(runner, src, sid, f"Saved user {turn}: " + "ordinary detail " * 80,
                    f"Saved assistant {turn}: " + "ordinary answer " * 80)
    saved = deepcopy(runner.session_store.load_transcript(sid))
    saved_db = deepcopy(db.get_messages(sid))
    assert "ON" in await dispatch(runner, src, "/incognito on")
    before = disk_snapshot(db)
    private_canary = "private manual compression canary"
    for turn in range(6):
        append_turn(runner, src, sid, f"{private_canary} {turn}: " + "private detail " * 80,
                    f"Private answer {turn}: " + "private response " * 80)
    with event_scope(runner, event(src, "/compress --level 3")):
        private_before = runner.session_store.load_transcript(sid)
        assert private_canary in json.dumps(private_before)
    result = await dispatch(runner, src, "/compress --level 3")
    with event_scope(runner, event(src, "/incognito status")):
        compressed = runner.session_store.load_transcript(sid)
    after_compress = disk_snapshot(db)
    # Finish the complete interval before evaluating handoff correctness, so the
    # regression also exercises restoration and all post-compression cleanup.
    assert "OFF" in await dispatch(runner, src, "/incognito off")
    assert entry.session_id == sid
    assert runner.session_store.get_or_create_session(src).session_id == sid
    assert runner.session_store.load_transcript(sid) == saved
    assert db.get_messages(sid) == saved_db
    assert after_compress == before
    hindsight.aretain.assert_not_called()
    hindsight.aretain_batch.assert_not_called()
    assert_no_disk_text(home, private_canary, "Private answer", summary)
    assert len(calls) == 1, result
    assert calls[0][1] is True
    assert private_canary in json.dumps(calls[0][0])
    assert "compressed" in result.lower(), result
    assert summary in json.dumps(compressed), (
        "Manual /compress reported success but discarded its RAM handoff. "
        f"Before={len(private_before)}, after={len(compressed)}, reply={result!r}"
    )
    assert len(compressed) < len(private_before)


@pytest.mark.asyncio
@pytest.mark.parametrize("compression_boundary", [False], indirect=True)
async def test_manual_compress_default_off_still_persists_in_place(compression_boundary):
    runner, db, home, _hindsight, calls, summary, src = compression_boundary
    entry = runner.session_store.get_or_create_session(src)
    sid = entry.session_id
    assert entry.incognito is False
    for turn in range(10):
        append_turn(runner, src, sid, f"Normal request {turn}: " + "ordinary detail " * 80,
                    f"Normal response {turn}: " + "ordinary answer " * 80)
    before = runner.session_store.load_transcript(sid)
    result = await dispatch(runner, src, "/compress --level 3")
    after = runner.session_store.load_transcript(sid)
    assert "compressed" in result.lower(), result
    assert len(calls) == 1
    assert calls[0][1] is False
    assert entry.session_id == sid
    assert len(after) < len(before)
    assert summary in json.dumps(after)
    assert summary in json.dumps(db.get_messages(sid))
    assert any(summary.encode() in path.read_bytes() for path in home.rglob("*") if path.is_file())

"""A rejected best-effort prune must not turn the next turn flush into a fatal error."""
import copy

import pytest

from agent.runtime_session_store import RuntimeSessionStore
from tests.agent.test_proactive_tool_result_pruning import _build, _compressor


@pytest.mark.parametrize('budget,chars', [(500, 9000), (4 * 1024 * 1024, 600_000)])
def test_proactive_prune_keeps_original_and_persistence_healthy(tmp_path, monkeypatch, caplog, budget, chars):
    calls = []
    def owner(method, **params):
        calls.append(params)
        return {'value': True}
    store = RuntimeSessionStore(owner, {'session_id': 's'}, tmp_path / 'outbox', max_bytes=budget)
    compressor = _compressor(proactive_prune_tokens=1, proactive_prune_min_result_chars=8000,
                             proactive_prune_min_reclaim_tokens=0)
    compressor._session_db, compressor._session_id = store, 's'
    # Exercise the archive fallback that a worker without rewrite_pruned_rows uses.
    monkeypatch.setattr('agent.context_compressor._archive_watermark_for', lambda *a: None)
    monkeypatch.setattr('agent.conversation_compression_archive.coverage_for_commit', lambda *a: (None, None))
    monkeypatch.setattr(compressor, '_rewrite_pruned_rows_in_place', lambda *a: None)
    messages = _build(8, big_indices={0, 1, 2, 3}, big_chars=chars)
    original = copy.deepcopy(messages)
    try:
        result, count = compressor.prune_tool_results_only(messages, current_tokens=1_000_000)
        assert result is messages and count == 0 and messages == original
        assert 'keeping the original transcript: outbox_full' in caplog.text
        assert calls == [] and store.failure is None
        assert store.journal['pending'] == [] and store.journal['next_sequence'] == 1
        store.queue_token_counts('s', input_tokens=1)
        assert store.refresh_session_turn_lease('s', 'holder')
        assert store.flush_token_counts()
    finally:
        store.close()

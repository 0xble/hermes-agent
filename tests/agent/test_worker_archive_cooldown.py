"""Optional-archive backpressure must not bench mandatory full compaction."""
from unittest.mock import Mock

import pytest

from agent.compaction_events import failure_class
from agent.runtime_session_store import RuntimeSessionStore
from tests.agent.pruning_helpers import _build, _compressor as _prune_compressor
from tests.agent.test_micro_compaction import _compressor, _conversation, _summary_markers


@pytest.mark.parametrize('mode', ['micro', 'proactive'])
def test_optional_archive_refusal_leaves_full_compaction_available(tmp_path, monkeypatch, mode):
    clock = [1000.0]
    monkeypatch.setattr('agent.micro_compaction.time.monotonic', lambda: clock[0])
    store = RuntimeSessionStore(Mock(return_value={'value': True}), {'session_id': 's'}, tmp_path / 'outbox', max_bytes=200)
    compressor = _compressor() if mode == 'micro' else _prune_compressor(
        proactive_prune_tokens=1, proactive_prune_min_reclaim_tokens=0)
    compressor._session_db, compressor._session_id = store, 's'
    monkeypatch.setattr(compressor, '_micro_start_watermark', lambda held: (None, None))
    monkeypatch.setattr('agent.context_compressor._archive_watermark_for', lambda *a: None)
    monkeypatch.setattr('agent.conversation_compression_archive.coverage_for_commit', lambda *a: (None, None))
    monkeypatch.setattr(compressor, '_rewrite_pruned_rows_in_place', lambda *a: None)
    messages = _conversation() if mode == 'micro' else _build(8, {0, 1, 2, 3})
    archive = Mock(wraps=store.archive_and_compact_best_effort)
    store.archive_and_compact_best_effort = archive
    above_threshold = compressor.threshold_tokens + 10

    def optional_pass():
        if mode == 'micro':
            return compressor._micro_compact(messages)
        result, count = compressor.prune_tool_results_only(messages, current_tokens=above_threshold)
        assert count == 0
        return result

    try:
        assert compressor.should_compress(above_threshold)
        assert optional_pass() is messages
        assert archive.call_count == 1
        assert compressor.should_compress_info(above_threshold) == (True, None)
        assert not compressor._automatic_compression_blocked()
        assert compressor._last_summary_error is None
        # Diagnostic text is not a gate; a summary refresh must not lift archive backpressure.
        compressor._last_summary_error = 'unrelated diagnostic'
        assert optional_pass() is messages
        assert archive.call_count == 1
        # Both optional callers share the capacity gate, regardless of which encountered it.
        compressor._micro_compact_enabled = True
        micro_summary = Mock(return_value='micro summary')
        compressor._micro_summarize_one = micro_summary
        micro_messages = _conversation()
        assert compressor._micro_compact(micro_messages) is micro_messages
        micro_summary.assert_not_called()
        compressor.proactive_prune_tokens = 1
        pruner = Mock(wraps=compressor._prune_old_tool_results)
        monkeypatch.setattr(compressor, '_prune_old_tool_results', pruner)
        other = _build(8, {0, 1, 2, 3})
        assert compressor.prune_tool_results_only(other, current_tokens=above_threshold) == (other, 0)
        pruner.assert_not_called()
        # Full compaction's real gate and pipeline run without force/ignore_cooldown.
        # Only the external summary call is replaced; publication is owned by its caller.
        compressor._session_db = None
        summary = Mock(return_value='full summary')
        monkeypatch.setattr(compressor, '_generate_summary', summary)
        result = compressor.compress(messages, current_tokens=above_threshold)
        summary.assert_called_once()
        assert result is not messages and compressor._last_compression_made_progress
    finally:
        store.close()


def test_summary_timeout_cooldown_does_not_suspend_micro_compaction():
    compressor = _compressor()
    compressor._record_compression_failure_cooldown(600, 'backoff:timeout: summary timed out')
    messages = _conversation()
    result = compressor._micro_compact(messages)
    assert result is not messages
    assert _summary_markers(result)
    assert not compressor.should_compress(compressor.threshold_tokens + 10)


def test_archive_capacity_failure_class_is_preserved():
    assert failure_class('archive_capacity') == 'archive_capacity'

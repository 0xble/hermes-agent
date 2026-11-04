"""Optional archives classify refusals without spending a summary call every turn."""
import copy
from unittest.mock import MagicMock, Mock

import pytest

from agent.runtime_session_store import RuntimeSessionStore
from tests.agent.pruning_helpers import _build, _compressor as _prune_compressor
from tests.agent.test_micro_compaction import _compressor, _conversation


@pytest.mark.parametrize('mode', ['micro', 'defrag', 'cumulative', 'proactive'])
@pytest.mark.parametrize('max_bytes', [200, 500])
def test_capacity_refusal_cools_down_and_retries_without_losing_history(tmp_path, monkeypatch, mode, max_bytes):
    clock = [1000.0]
    monkeypatch.setattr('agent.micro_compaction.time.monotonic', lambda: clock[0])
    owner = Mock(return_value={'value': True})
    store = RuntimeSessionStore(owner, {'session_id': 's'}, tmp_path / 'outbox', max_bytes=max_bytes)
    compressor = _compressor() if mode != 'proactive' else _prune_compressor(
        proactive_prune_tokens=1, proactive_prune_min_result_chars=8000,
        proactive_prune_min_reclaim_tokens=0)
    compressor._session_db, compressor._session_id = store, 's'
    monkeypatch.setattr(compressor, '_micro_start_watermark', lambda held: (None, None))
    monkeypatch.setattr('agent.context_compressor._archive_watermark_for', lambda *a: None)
    monkeypatch.setattr('agent.conversation_compression_archive.coverage_for_commit', lambda *a: (None, None))
    monkeypatch.setattr(compressor, '_rewrite_pruned_rows_in_place', lambda *a: None)
    summary = Mock(return_value='short summary')
    compressor._micro_summarize_one = summary
    telemetry = Mock()
    compressor._emit_micro_compaction_telemetry = telemetry
    messages = _conversation() if mode != 'proactive' else _build(8, {0, 1, 2, 3})
    if mode == 'cumulative':
        compressor._session_db = None
        messages = compressor._micro_compact(messages)
        compressor._session_db = store
        for row_id, message in enumerate(messages, 1):
            message.update(_db_persisted=True, _row_id=row_id)
        compressor._flush_scan_cursor_invalidated = False
        summary.reset_mock()
        telemetry.reset_mock()
    if mode == 'defrag':
        compressor._micro_compact_defrag_threshold_tokens = 1
        # Seed a resumed rolling marker without attempting another archive.
        from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY, MICRO_COMPACT_MARKER_KEY
        compressor._micro_compact_rolling_summary = 'previous summary ' * 20
        messages[2][COMPRESSED_SUMMARY_METADATA_KEY] = True
        messages[2][MICRO_COMPACT_MARKER_KEY] = True
        compressor._reset_micro_failure_tracking()
        summary.reset_mock()
        telemetry.reset_mock()
    if mode != 'proactive':
        assert compressor._next_exchange(messages) is not None
    original = copy.deepcopy(messages)
    state = (compressor._micro_compact_cursor, compressor._micro_compact_rolling_summary)
    archive = Mock(wraps=store.archive_and_compact_best_effort)
    store.archive_and_compact_best_effort = archive

    def run():
        if mode == 'proactive':
            result, count = compressor.prune_tool_results_only(messages, current_tokens=1_000_000)
            assert count == 0
            return result
        return compressor._micro_compact(messages)

    try:
        assert run() is messages
        assert archive.call_count == 1
        owner.assert_not_called()
        assert messages == original
        assert not compressor._flush_scan_cursor_invalidated
        assert (compressor._micro_compact_cursor, compressor._micro_compact_rolling_summary) == state
        if mode != 'proactive':
            assert telemetry.call_args.kwargs['outcome'] == 'archive_capacity'
            assert compressor._consecutive_archive_capacity_failures == 1
            assert compressor._micro_compact_consecutive_failures == 0
        # A new turn may grow the transcript; capacity cooldown must still hold.
        messages.extend([{'role': 'user', 'content': 'later'}, {'role': 'assistant', 'content': 'reply'}])
        assert run() is messages
        assert archive.call_count == 1
        assert summary.call_count == (0 if mode == 'proactive' else 1)
        clock[0] += 61
        assert run() is messages
        assert archive.call_count == 2
        if mode != 'proactive':
            assert compressor._consecutive_archive_capacity_failures == 2
            assert compressor._micro_compact_consecutive_failures == 0
        assert store.failure is None and store.journal['pending'] == []
        store.queue_token_counts('s', input_tokens=1)
        assert store.flush_token_counts()
    finally:
        store.close()


@pytest.mark.parametrize('kind', ['magic_mock', 'proxy', 'explicit'])
@pytest.mark.parametrize('mode', ['micro', 'proactive'])
def test_only_explicit_optional_archive_capability_is_selected(monkeypatch, kind, mode):
    mandatory, optional = Mock(), Mock()

    class Proxy:
        archive_and_compact = mandatory

        def __getattr__(self, name):
            return optional

    if kind == 'magic_mock':
        store = MagicMock()
        store.archive_and_compact = mandatory
    else:
        store = Proxy()
        if kind == 'explicit':
            store.archive_and_compact_best_effort = optional
    compressor = _prune_compressor(proactive_prune_tokens=1, proactive_prune_min_reclaim_tokens=0)
    compressor._session_db, compressor._session_id = store, 's'
    monkeypatch.setattr('agent.context_compressor._archive_watermark_for', lambda *a: None)
    monkeypatch.setattr('agent.conversation_compression_archive.coverage_for_commit', lambda *a: (None, None))
    monkeypatch.setattr(compressor, '_rewrite_pruned_rows_in_place', lambda *a: None)
    messages = _build(8, {0, 1, 2, 3})
    if mode == 'micro':
        compressor._sync_micro_compact_to_db(messages)
    else:
        result, count = compressor.prune_tool_results_only(messages, current_tokens=1_000_000)
        assert result is not messages and count > 0
    assert mandatory.call_count == (0 if kind == 'explicit' else 1)
    assert optional.call_count == (1 if kind == 'explicit' else 0)

def test_capacity_failure_is_not_a_summary_failure():
    compressor = _compressor()
    for _ in range(2):
        assert compressor._record_micro_failure(2, 3, outcome='archive_capacity') == 'archive_capacity'
    assert compressor._record_micro_failure(2, 3) == 'summarize_failed'


@pytest.mark.parametrize('reset', ['new_session', 'rebind', 'full_compaction'])
def test_capacity_streak_restarts_after_recovery_or_session_change(reset):
    compressor = _prune_compressor()
    compressor._consecutive_archive_capacity_failures = 3
    if reset == 'new_session':
        compressor.on_session_reset()
    elif reset == 'rebind':
        compressor.bind_session_state(None, 'new-session')
    else:
        compressor._clear_compression_failure_cooldown()
    assert compressor._consecutive_archive_capacity_failures == 0

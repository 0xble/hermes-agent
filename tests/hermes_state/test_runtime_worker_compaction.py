"""Watermarked compaction and rotation share the worker receipt transaction."""
import pytest


def test_watermarked_compaction_preserves_foreign_tail_and_rejects_stale_holder(worker):
    db, store = worker
    store.append_messages_batch('owned', [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'carry'}])
    assert store.try_acquire_compression_lock('owned', 'holder')
    watermark = store.get_active_message_watermark('owned')
    db.append_messages_batch('owned', [{'role': 'user', 'content': 'foreign tail', 'api_content': ' EXACT ',
                                       'display_metadata': {'_accepted_input_id': 'input-tail'}}])
    seq = store.journal['next_sequence']
    from hermes_state_runtime import mutate_worker_execution
    with pytest.raises(Exception, match='lease'):
        mutate_worker_execution(db, **store.scope, sequence=seq, operation='compression.archive', payload={
            'messages': [{'role': 'assistant', 'content': 'bad'}], 'model_config_patch': None,
            'watermark': watermark, 'lock_holder': 'old', 'tail_count': 0})
    assert store.archive_and_compact('owned', [{'role': 'assistant', 'content': 'summary'},
        {'role': 'assistant', 'content': 'carry'}], watermark=watermark, lock_holder='holder', tail_count=1,
        model_config_patch={'_proactive_prune_rearm_tokens': 42}) == 3
    rows = store.get_messages_as_conversation('owned', include_row_ids=True)
    assert rows[-1]['content'] == 'foreign tail' and rows[-1]['api_content'] == ' EXACT '
    assert rows[-1]['display_metadata'] == {'_accepted_input_id': 'input-tail'}
    assert db._read_one("SELECT COUNT(*) FROM messages WHERE session_id='owned' AND compacted=1")[0] == 1
    assert store.get_session_model_config_value('owned', '_proactive_prune_rearm_tokens') == 42


def test_publication_replay_advances_assignment_once_and_keeps_markers(worker):
    db, store = worker
    store.append_messages_batch('owned', [{'role': 'user', 'content': 'before'}])
    assert store.try_acquire_compression_lock('owned', 'holder')
    watermark = store.get_active_message_watermark('owned')
    db.append_messages_batch('owned', [{'role': 'user', 'content': 'foreign', 'api_content': ' exact tail ',
                                       'display_metadata': {'_accepted_input_id': 'delivery'}}])
    original = store.rpc
    def lost_ack(method, **params):
        original(method, **params)
        raise TimeoutError('lost-ack')
    store.rpc = lost_ack
    with pytest.raises(TimeoutError, match='lost-ack'):
        store.publish_compression_child(parent_session_id='owned', child_session_id='child', source='cli',
            messages=[{'role': 'assistant', 'content': 'summary'}], system_prompt='prefix',
            compression_lock_holder='holder', watermark=watermark)
    assert db._read_one('SELECT session_id FROM worker_executions')[0] == 'child'
    assert db.get_session('owned')['end_reason'] == 'compression'
    store.rpc = original
    store.retry_pending()
    assert store.scope['session_id'] == store.journal['scope']['session_id'] == 'child'
    store.append_messages_batch('child', [{'role': 'assistant', 'content': 'after'}])
    rows = store.get_messages_as_conversation('child')
    assert [r['content'] for r in rows] == ['summary', 'foreign', 'after']
    assert rows[1]['api_content'] == ' exact tail '
    assert rows[1]['display_metadata'] == {'_accepted_input_id': 'delivery'}
    assert store.get_compression_lineage('child') == ['owned', 'child']
    assert db.get_session('child')['system_prompt'] == 'prefix'


def test_in_place_archive_without_a_lease_persists_from_a_worker(worker):
    """Micro-compaction and proactive prune archive without a compression lease; the worker RPC
    must accept the same ``lock_holder=None`` the in-process store does, or the journal sticks in
    failure and every later persist of that turn raises."""
    db, store = worker
    store.append_messages_batch('owned', [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'long'}])
    assert store.archive_and_compact('owned', [{'role': 'assistant', 'content': 'pruned'}],
                                     tail_count=0, model_config_patch=None) == 1
    assert store.failure is None and store.journal['pending'] == []
    rows = store.get_messages_as_conversation('owned')
    assert [r['content'] for r in rows] == ['pruned']
    assert db._read_one("SELECT COUNT(*) FROM messages WHERE session_id='owned' AND compacted=1")[0] == 2
    store.append_messages_batch('owned', [{'role': 'user', 'content': 'after'}])
    assert [r['content'] for r in store.get_messages_as_conversation('owned')] == ['pruned', 'after']


def test_micro_compaction_live_dicts_persist_through_the_worker_like_direct(worker):
    """The real micro-compaction commit hands its LIVE list (``_db_persisted`` stamps, the marker's
    ``_micro_compact_marker``) to archive_and_compact. The worker facade projects those dicts onto the
    closed handoff fields; otherwise the owner refuses the receipt and the sticky journal refuses every
    later persist of the session (helix4u #4)."""
    import types
    from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY, MICRO_COMPACT_MARKER_KEY
    from agent.micro_compaction import MicroCompactionMixin
    db, store = worker
    db.create_session('direct', 'cli')
    durable = {}
    for sid, sdb in (('direct', db), ('owned', store)):
        live = [{'role': 'user', 'content': 'u1'}, {'role': 'assistant', 'content': 'a1'},
                {'role': 'user', 'content': 'u2'}]
        sdb.append_messages_batch(sid, live)
        for message in live:
            message['_db_persisted'] = True  # sync_flushed_message_markers' post-commit stamp
        marker = {'role': 'assistant', 'content': 'micro summary', COMPRESSED_SUMMARY_METADATA_KEY: True,
                  MICRO_COMPACT_MARKER_KEY: True, '_compressed_summary_has_user_turn': False}
        compacted = [live[0], marker, live[2]]
        assert MicroCompactionMixin._sync_micro_compact_to_db(
            types.SimpleNamespace(_session_db=sdb, _session_id=sid), compacted)
        assert marker[MICRO_COMPACT_MARKER_KEY] and marker['_db_persisted'] and isinstance(marker['_row_id'], int)
        sdb.append_messages_batch(sid, [{'role': 'assistant', 'content': 'after'}])
        durable[sid] = [r['content'] for r in db.get_messages_as_conversation(sid)]
    assert store.failure is None and store.journal['pending'] == []
    assert durable['owned'] == durable['direct'] == ['u1', 'micro summary', 'u2', 'after']


@pytest.mark.parametrize('durable_identity', [False, True])
def test_worker_stale_compaction_is_acknowledged_without_mutation_or_sticky_failure(worker, durable_identity):
    from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY, StaleHeldHistory
    db, store = worker
    store.append_messages_batch('owned', [{'role': 'user', 'content': 'before'}])
    held = store.get_messages_as_conversation('owned', include_row_ids=durable_identity)
    db.archive_and_compact('owned', [{
        'role': 'assistant', 'content': 'winning summary',
        COMPRESSED_SUMMARY_METADATA_KEY: True,
    }])
    rows = [dict(row) for row in db._read_all('SELECT * FROM messages ORDER BY id')]
    config = db.get_session('owned')['model_config']
    sequence = store.journal['next_sequence']
    with pytest.raises(StaleHeldHistory):
        store.archive_and_compact('owned', [{'role': 'assistant', 'content': 'stale rewrite'}],
                                  held_messages=held, model_config_patch={'_proactive_prune_rearm_tokens': 42})
    assert [dict(row) for row in db._read_all('SELECT * FROM messages ORDER BY id')] == rows
    assert db.get_session('owned')['model_config'] == config
    assert store.failure is None and store.journal['pending'] == []
    assert store.journal['next_sequence'] == sequence + 1
    assert store.retry_pending() == []
    store.append_messages_batch('owned', [{'role': 'user', 'content': 'after refusal'}])
    assert [row['content'] for row in store.get_messages_as_conversation('owned')] == [
        'winning summary', 'after refusal']


def test_worker_held_compaction_preserves_tail_and_receipt_rollback(worker):
    import sqlite3
    db, store = worker
    store.append_messages_batch('owned', [{'role': 'user', 'content': 'before'}])
    held = store.get_messages_as_conversation('owned', include_row_ids=True)
    watermark = store.get_active_message_watermark('owned')
    db.append_messages_batch('owned', [{'role': 'user', 'content': 'concurrent tail',
        'api_content': ' EXACT TAIL ', 'platform_message_id': 'delivery-tail',
        'display_metadata': {'_accepted_input_id': 'tail-input'}}])
    rows = [dict(row) for row in db._read_all('SELECT * FROM messages ORDER BY id')]
    sequence = store.journal['next_sequence']
    db._execute_write(lambda conn: conn.execute(
        "CREATE TRIGGER deny_receipt BEFORE INSERT ON worker_receipts "
        "BEGIN SELECT RAISE(ABORT, 'deny receipt'); END"))
    try:
        with pytest.raises(sqlite3.IntegrityError, match='deny receipt'):
            store.archive_and_compact('owned', [{'role': 'assistant', 'content': 'summary'}],
                                      held_messages=held, watermark=watermark)
        assert [dict(row) for row in db._read_all('SELECT * FROM messages ORDER BY id')] == rows
        assert db._read_one('SELECT last_sequence FROM worker_executions')[0] == sequence - 1
    finally:
        db._execute_write(lambda conn: conn.execute('DROP TRIGGER deny_receipt'))
    store.retry_pending()
    assert store.failure is None and store.journal['pending'] == []
    active = store.get_messages_as_conversation('owned', include_row_ids=True)
    assert [row['content'] for row in active] == ['summary', 'concurrent tail']
    assert active[-1]['api_content'] == ' EXACT TAIL '
    assert active[-1]['message_id'] == 'delivery-tail'
    assert db._read_one('SELECT platform_message_id FROM messages WHERE session_id=? AND id=? AND active=1',
                        ('owned', active[-1]['_row_id']))[0] == 'delivery-tail'
    assert active[-1]['display_metadata'] == {'_accepted_input_id': 'tail-input'}
    assert all(isinstance(row['_row_id'], int) for row in active)

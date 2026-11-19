"""Public transcript content does not confer private session ownership."""

import json

from agent.incognito import register_session


def test_public_worker_json_mention_preserves_rows_receipts_and_next_append(worker):
    db, store = worker
    private_id = "synthetic-private-topic-for-worker-mention"
    output = json.dumps({"result": {"topics": [{"session_id": private_id}]}})
    first = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "synthetic-topic-lookup",
                "type": "function",
                "function": {"name": "terminal", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "synthetic-topic-lookup",
            "tool_name": "terminal",
            "content": output,
        },
    ]
    second = [{"role": "assistant", "content": "Public follow-up"}]

    register_session(private_id)
    try:
        assert store.append_messages_batch("owned", first) == len(first)
        assert store.append_messages_batch("owned", second) == len(second)
        assert store.flush_token_counts()

        with db._read_ctx() as conn:
            rows = conn.execute(
                "SELECT id,role,content,message_uid,timestamp FROM messages "
                "WHERE session_id=? AND active=1 ORDER BY id", ("owned",),
            ).fetchall()
            receipts = conn.execute(
                "SELECT sequence,result_json FROM worker_receipts "
                "WHERE execution_id=? ORDER BY sequence", ("worker",),
            ).fetchall()
            last_sequence = conn.execute(
                "SELECT last_sequence FROM worker_executions WHERE execution_id=?",
                ("worker",),
            ).fetchone()[0]

        messages = first + second
        assert [(row["role"], row["content"]) for row in rows] == [
            (message["role"], message["content"]) for message in messages
        ]
        assert [row["id"] for row in rows] == [message["_row_id"] for message in messages]
        assert [row["message_uid"] for row in rows] == [message["message_uid"] for message in messages]
        assert [row["timestamp"] for row in rows] == [message["timestamp"] for message in messages]
        assert [receipt["sequence"] for receipt in receipts] == [1, 2]
        for receipt, batch in zip(receipts, (first, second), strict=True):
            result = json.loads(receipt["result_json"])
            assert result["count"] == len(batch)
            assert len(result["annotations"]) == len(batch)
            for annotation, message in zip(result["annotations"], batch, strict=True):
                assert all(message.get(key) == value for key, value in annotation.items())
        assert last_sequence == 2
        assert store.journal["next_sequence"] == 3
        assert store.journal["pending"] == []
        assert store.failure is None
    finally:
        register_session(private_id, False)

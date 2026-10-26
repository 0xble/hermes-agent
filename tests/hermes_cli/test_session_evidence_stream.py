"""Read-only graph and exact-segment audit streams over fictional fixture stores."""

import argparse
import hashlib
import json
import sqlite3
import sys
import tracemalloc

import pytest

from hermes_cli.subcommands.sessions import build_sessions_parser, cmd_sessions_lazy
from hermes_state_common import SCHEMA_SQL


def _args(*argv):
    parser = argparse.ArgumentParser(prog="hermes")
    build_sessions_parser(parser.add_subparsers(dest="command"), cmd_sessions=cmd_sessions_lazy)
    return parser.parse_args(["sessions", "state", *argv])


def _run(capsys, *argv):
    args = _args(*argv)
    code = args.func(args)
    output = capsys.readouterr()
    return code, [json.loads(line) for line in output.out.splitlines()], output.err


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "wrong-inherited-profile")
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.executescript(SCHEMA_SQL)
    conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")
    conn.execute("INSERT INTO messages_fts (content) VALUES (?)", ("forbidden FTS body",))
    for sid, parent, profile, hidden, archived, ended in [
        ("root", None, "default", 0, 0, 4),
        ("compress", "root", "default", 1, 0, 5),
        ("reset", "root", "default", 0, 1, None),
        ("task", "compress", "default", 1, 1, None),
        ("dangling", "missing-parent", "default", 0, 0, None),
        ("foreign", "reset", "other", 1, 1, None),
    ]:
        conn.execute(
            "INSERT INTO sessions (id, parent_session_id, source, created_source, profile_name, "
            "started_at, ended_at, hidden, archived, title, system_prompt, model_config, origin_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, parent, "subagent" if sid == "task" else "telegram", "local", profile, 1, ended,
             hidden, archived, "private title", "forbidden system", "forbidden model",
             '{"sender_id":"shared-chat-api-label","author":"Human"}'),
        )
    for role, content, active, compacted, meta in [
        ("system", "forbidden system row", 1, 0, None),
        ("user", "架空の文 🌿", 1, 0, '{"origin":"synthetic_prompt","sender_id":"shared"}'),
        ("assistant", '[{"type":"text","text":"fictional structured reply"}]', 0, 1, None),
        ("tool", "forbidden tool body", 1, 0, None),
        ("user", "/fictional-skill full skill body", 0, 0, '{"recovery_identity":"fictional"}'),
        ("assistant", "api_key=sk-fictionalABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", 1, 0, None),
        ("user", "API author label is not a principal", 1, 0, '{"author":"Human"}'),
    ]:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active, compacted, "
            "display_metadata, message_uid, reasoning, reasoning_content, tool_calls, api_content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("root", role, content, 2, active, compacted, meta, f"uid-{role}-{active}-{compacted}-{content[:2]}",
             "forbidden reasoning", "forbidden reasoning content", "forbidden tool args", "forbidden api replay"),
        )
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                 ("compress", "user", "descendant must not appear", 3))
    conn.commit()
    yield conn
    conn.close()



def _verify(records, kind, count):
    assert records[0]["type"] == "header"
    assert records[0]["stream"] == kind
    footer = records[-1]
    assert footer["type"] == "footer" and footer["complete"] and footer["exhausted"]
    assert footer["count"] == count == len(records) - 2
    evidence = b"".join((json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                   allow_nan=False) + "\n").encode() for r in records[:-1])
    assert footer["sha256"] == hashlib.sha256(evidence).hexdigest()
    assert footer["hashed_bytes"] == len(evidence)
    return records[1:-1], footer["sha256"]


def _read_guard(monkeypatch, forbidden):
    import hermes_state
    real = hermes_state._connect_tracked_db
    reads, writes = [], []

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)

        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
                if table in forbidden and (forbidden[table] is None or column in forbidden[table]):
                    return sqlite3.SQLITE_DENY
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                          sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE}:
                writes.append((action, table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(hermes_state, "_connect_tracked_db", connect)
    def no_writer(*args, **kwargs):
        raise AssertionError("evidence reached SessionDB writer initialization")

    monkeypatch.setattr(hermes_state.SessionDB, "_open_writer", no_writer)
    return reads, writes


def test_inventory_exhausts_graph_without_body_or_private_metadata(store, monkeypatch, capsys, tmp_path):
    forbidden = {"messages": None, "messages_fts": None, "sessions": {"title", "display_name", "system_prompt", "system_prompt_hash",
                 "model_config", "last_activity_description", "origin_json"}, "system_prompts": None}
    reads, writes = _read_guard(monkeypatch, forbidden)
    before = (tmp_path / "state.db").read_bytes()
    code, records, error = _run(capsys, "inventory")
    assert code == 0, error
    rows, _ = _verify(records, "session_inventory", 6)
    nodes = {r["session"]["id"]: r["session"] for r in rows}
    assert set(nodes) == {"root", "compress", "reset", "task", "dangling", "foreign"}
    assert nodes["dangling"]["parent_session_id"] == "missing-parent"
    assert nodes["foreign"]["profile_name"] == "other"
    assert nodes["compress"]["hidden"] == 1 and nodes["reset"]["archived"] == 1
    assert records[0]["scope"]["profile"] == "default"
    assert before == (tmp_path / "state.db").read_bytes()
    assert reads and not writes


def test_transcript_exact_audit_projection_redaction_and_unknown_origin(store, monkeypatch, capsys):
    forbidden = {"messages_fts": None, "sessions": {"title", "system_prompt", "system_prompt_hash", "model_config"},
                 "messages": {"reasoning", "reasoning_content", "reasoning_details", "codex_reasoning_items",
                              "codex_message_items", "tool_calls", "tool_call_id", "api_content"},
                 "system_prompts": None}
    _, writes = _read_guard(monkeypatch, forbidden)
    code, records, error = _run(capsys, "transcript", "--session-id", "root")
    assert code == 0, error
    rows, _ = _verify(records, "session_transcript", 5)
    assert [r["message"]["id"] for r in rows] == [2, 3, 5, 6, 7]
    assert [r["audit_state"] for r in rows] == ["active", "compaction_archive", "rewind_excluded", "active", "active"]
    assert all(r["human_origin"] == "unknown" for r in rows)
    assert rows[0]["message"]["content"] == "架空の文 🌿"
    assert rows[1]["message"]["content"] == '[{"type":"text","text":"fictional structured reply"}]'
    assert rows[2]["message"]["display_metadata"] == '{"recovery_identity":"fictional"}'
    assert rows[4]["message"]["display_metadata"] == '{"author":"Human"}'
    serialized = json.dumps(records)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in serialized
    assert "forbidden" not in serialized and "descendant must not appear" not in serialized
    assert not writes


@pytest.mark.parametrize("sid", ["roo", "latest", "private title", "absent"])
def test_exact_id_refuses_prefix_alias_title_and_absence(store, capsys, sid):
    code, records, error = _run(capsys, "transcript", "--session-id", sid)
    assert code != 0 and not records and error


@pytest.mark.parametrize("mode", ["missing", "stale"])
def test_missing_or_stale_store_is_not_created_or_healed(tmp_path, monkeypatch, capsys, mode):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "state.db"
    before = None
    if mode == "stale":
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE sessions (id TEXT)")
        before = path.read_bytes()
    for argv in [("inventory",), ("transcript", "--session-id", "root")]:
        code, records, error = _run(capsys, *argv)
        assert code != 0 and not records and error
    if mode == "missing":
        assert not path.exists()
    else:
        assert path.read_bytes() == before


@pytest.mark.parametrize("change,operation", [
    ("UPDATE messages SET content = 'same-count edit' WHERE id = 2", "transcript"),
    ("UPDATE messages SET active = 0 WHERE id = 2", "transcript"),
    ("UPDATE messages SET display_metadata = '{\"sender_id\":\"changed\"}' WHERE id = 2", "transcript"),
    ("UPDATE sessions SET origin_json = '{\"sender_id\":\"changed\"}' WHERE id = 'root'", "transcript"),
    ("UPDATE sessions SET parent_session_id = 'new-boundary' WHERE id = 'task'", "inventory"),
])
def test_fingerprint_detects_same_count_content_state_origin_and_edge_edits(store, capsys, change, operation):
    argv = (operation,) if operation == "inventory" else (operation, "--session-id", "root")
    code, first, error = _run(capsys, *argv)
    assert code == 0, error
    store.execute(change)
    store.commit()
    code, second, error = _run(capsys, *argv)
    assert code == 0, error
    assert first[-1]["count"] == second[-1]["count"]
    assert first[-1]["sha256"] != second[-1]["sha256"]


@pytest.mark.parametrize("column,value", [("content", "x" * (300 * 1024)), ("content", b"unsupported"),
                                          ("display_metadata", "x" * (300 * 1024))],
                         ids=["oversize-content", "blob-content", "oversize-metadata"])
def test_oversized_or_unsupported_row_never_completes(store, capsys, column, value):
    store.execute(f"UPDATE messages SET {column} = ? WHERE id = 3", (value,))
    store.commit()
    code, records, error = _run(capsys, "transcript", "--session-id", "root")
    assert code != 0 and error
    assert not any(r["type"] == "footer" for r in records)
    assert "x" * 1000 not in json.dumps(records)


class _StreamSink:
    def __init__(self, callback=None):
        self.digest = hashlib.sha256()
        self.count = 0
        self.footer = None
        self.callback = callback

    def write(self, text):
        if not text.strip():
            return len(text)
        record = json.loads(text)
        if record["type"] == "footer":
            self.footer = record
        else:
            self.digest.update(text.encode())
            if record["type"] == "message":
                self.count += 1
        if self.callback:
            self.callback(record)
        return len(text)

    def flush(self):
        pass


def test_large_segment_memory_is_bounded_and_footer_recomputable(store, monkeypatch):
    store.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                      (("root", "assistant", "fictional " * 1024, 10) for _ in range(1500)))
    store.commit()
    args = _args("transcript", "--session-id", "root")
    # Warm the real import/redaction path before measuring stream working memory.
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", _StreamSink())
        assert args.func(args) == 0
    sink = _StreamSink()
    monkeypatch.setattr(sys, "stdout", sink)
    tracemalloc.start()
    try:
        assert args.func(args) == 0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 5 * 1024 * 1024
    assert sink.count == 1505
    assert sink.footer is not None
    assert sink.footer["count"] == sink.count
    assert sink.footer["sha256"] == sink.digest.hexdigest()


def test_interrupted_stream_has_no_success_footer(store, monkeypatch, capsys):
    sink = _StreamSink(callback=lambda record: (_ for _ in ()).throw(KeyboardInterrupt())
                       if record["type"] == "message" else None)
    monkeypatch.setattr(sys, "stdout", sink)
    args = _args("transcript", "--session-id", "root")
    assert args.func(args) == 130
    assert sink.footer is None
    assert capsys.readouterr().err


@pytest.mark.parametrize("operation", ["inventory", "transcript"])
def test_stream_snapshot_survives_concurrent_same_count_edits(store, monkeypatch, capsys, operation):
    store.execute("PRAGMA journal_mode=WAL")
    store.commit()
    argv = (operation,) if operation == "inventory" else (operation, "--session-id", "root")
    code, before, error = _run(capsys, *argv)
    assert code == 0, error
    changed = False

    def edit_after_first_row(record):
        nonlocal changed
        if record["type"] in {"session", "message"} and not changed:
            changed = True
            store.execute("UPDATE sessions SET parent_session_id = 'concurrent' WHERE id = 'task'")
            store.execute("UPDATE messages SET content = 'concurrent' WHERE id = 7")
            store.commit()

    sink = _StreamSink(callback=edit_after_first_row)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", sink)
        args = _args(*argv)
        assert args.func(args) == 0
    assert changed and sink.footer is not None
    assert sink.footer["sha256"] == before[-1]["sha256"]
    code, after, error = _run(capsys, *argv)
    assert code == 0, error
    assert after[-1]["sha256"] != before[-1]["sha256"]


@pytest.mark.parametrize("expression", ["zeroblob(?)", "CAST(zeroblob(?) AS TEXT)"], ids=["blob", "text"])
def test_huge_field_is_refused_before_crossing_sqlite_python_boundary(store, monkeypatch, capsys, expression):
    import hermes_state

    store.execute(f"UPDATE messages SET content = {expression} WHERE id = 3", (16 * 1024 * 1024,))
    store.commit()
    real = hermes_state._connect_tracked_db
    maximum_text_bytes = 0

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)

        def text_factory(value):
            nonlocal maximum_text_bytes
            maximum_text_bytes = max(maximum_text_bytes, len(value))
            return value.decode("utf-8")

        conn.text_factory = text_factory
        return conn

    monkeypatch.setattr(hermes_state, "_connect_tracked_db", connect)
    code, records, error = _run(capsys, "transcript", "--session-id", "root")
    assert code != 0 and "exceeds 262144 source bytes" in error
    assert maximum_text_bytes < 262144
    assert not any(r["type"] == "footer" for r in records)


@pytest.mark.parametrize("explicit_profile", [False, True])
def test_real_entrypoint_obeys_selected_home_without_profile_fallback(store, tmp_path, explicit_profile):
    import os
    from pathlib import Path
    import subprocess

    root = tmp_path / "isolated-root"
    selected = root / "profiles" / "fictional"
    selected.mkdir(parents=True)
    with sqlite3.connect(selected / "state.db") as target:
        store.backup(target)
    # Both homes contain the same exact ID but different text. A missing named store must not
    # fall back to the default even when the default contains a matching session.
    with sqlite3.connect(root / "state.db") as target:
        store.backup(target)
        target.execute("UPDATE messages SET content = 'wrong default home' WHERE role = 'user'")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "user"),
           "HERMES_HOME": str(root if explicit_profile else selected),
           "HERMES_SESSION_PROFILE": "wrong-inherited-profile"}
    command = [sys.executable, "-m", "hermes_cli.main"]
    if explicit_profile:
        command += ["-p", "fictional"]
    command += ["sessions", "state", "transcript", "--session-id", "root"]
    result = subprocess.run(command, env=env, cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr.decode()
    records = [json.loads(line) for line in result.stdout.splitlines()]
    _verify(records, "session_transcript", 5)
    assert records[0]["scope"] == {"home": str(selected), "profile": "fictional"}
    assert b"wrong default home" not in result.stdout
    (selected / "state.db").unlink()
    result = subprocess.run(command, env=env, cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, timeout=60, check=False)
    assert result.returncode != 0 and not result.stdout and result.stderr
    assert not (selected / "state.db").exists()


def test_inventory_is_exhaustive_beyond_ordinary_listing_limits(store, capsys):
    store.executemany("INSERT INTO sessions (id, parent_session_id, source, started_at, hidden, archived, profile_name) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      ((f"fictional-{i:05d}", "root", "cli", 0, 1, 1, None) for i in range(1100)))
    store.commit()
    code, records, error = _run(capsys, "inventory")
    assert code == 0, error
    rows, _ = _verify(records, "session_inventory", 1106)
    assert sum(row["session"]["profile_name"] is None for row in rows) == 1100


def test_inventory_oversize_routing_field_cannot_yield_success(store, capsys):
    store.execute("UPDATE sessions SET session_key = zeroblob(1048576) WHERE id = 'root'")
    store.commit()
    code, records, error = _run(capsys, "inventory")
    assert code != 0 and "source bytes" in error
    assert not any(r["type"] == "footer" for r in records)


def test_invalid_utf8_source_content_is_not_repaired(store, capsys):
    store.execute("UPDATE messages SET content = CAST(x'80' AS TEXT) WHERE id = 3")
    store.commit()
    code, records, error = _run(capsys, "transcript", "--session-id", "root")
    assert code != 0 and error
    assert not any(r["type"] == "footer" for r in records)


def test_output_byte_budget_refuses_before_emitting_a_partial_record(store, monkeypatch, capsys):
    import hermes_cli.session_evidence as evidence

    monkeypatch.setattr(evidence, "MAX_RECORD_BYTES", 100)
    code, records, error = _run(capsys, "transcript", "--session-id", "root")
    assert code != 0 and not records and "encoded bytes" in error


def test_short_output_write_cannot_be_success(store, monkeypatch, capsys):
    class ShortSink(_StreamSink):
        def write(self, text):
            return super().write(text) - 1

    sink = ShortSink()
    monkeypatch.setattr(sys, "stdout", sink)
    args = _args("inventory")
    assert args.func(args) != 0
    assert sink.footer is None and "short output write" in capsys.readouterr().err

"""Read-only graph and exact-segment audit streams over fictional fixture stores."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
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
        ("assistant", "api_key=" + "sk-" + "fictionalABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", 1, 0, None),
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



def _read_guard(monkeypatch, forbidden, observed_sizes):
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

        def text_factory(value):
            observed_sizes.append(len(value))
            return value.decode("utf-8")

        conn.text_factory = text_factory
        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(hermes_state, "_connect_tracked_db", connect)
    def no_writer(*args, **kwargs):
        raise AssertionError("evidence reached SessionDB writer initialization")

    monkeypatch.setattr(hermes_state.SessionDB, "_open_writer", no_writer)
    return reads, writes


# Deny acquisition, not just serialization, of unrelated payload fields.
_PRIVATE_SESSION = {"title", "display_name", "system_prompt", "system_prompt_hash",
                    "model_config", "last_activity_description"}
_PRIVATE_MESSAGE = {"reasoning", "reasoning_content", "reasoning_details", "codex_reasoning_items",
                    "codex_message_items", "tool_calls", "tool_call_id", "api_content"}


class _StreamSink:
    """Observe the wire incrementally so measuring the producer never buffers a segment."""

    def __init__(self, *, callback=None, short=False, keep_rows=True):
        self.digest = hashlib.sha256()
        self.hashed_bytes = 0
        self.count = 0
        self.header = None
        self.footer = None
        self.rows = []
        self.previous_id = None
        self.ordered = True
        self.callback = callback
        self.short = short
        self.keep_rows = keep_rows

    def write(self, text):
        record = json.loads(text)
        if record["type"] == "footer":
            self.footer = record
        else:
            self.digest.update(text.encode())
            self.hashed_bytes += len(text.encode())
            if record["type"] == "header":
                self.header = record
            else:
                field = "message" if record["type"] == "message" else "session"
                identity = record[field]["id"]
                self.ordered &= self.previous_id is None or self.previous_id < identity
                self.previous_id = identity
                self.count += 1
                if self.keep_rows or len(self.rows) < 5:
                    self.rows.append(record)
        if self.callback:
            self.callback(record)
        return len(text) - int(self.short)

    def flush(self):
        pass


def _capture(capsys, monkeypatch, operation, *, sid="root", callback=None, short=False, measure=False):
    argv = (operation,) if operation == "inventory" else (operation, "--session-id", sid)
    args = _args(*argv)
    if measure:
        # Warm the real import/redaction path, excluding imports from stream working memory.
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdout", _StreamSink(keep_rows=False))
            assert args.func(args) == 0
    sink = _StreamSink(callback=callback, short=short, keep_rows=not measure)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", sink)
        if measure:
            tracemalloc.start()
        try:
            code = args.func(args)
            peak = tracemalloc.get_traced_memory()[1] if measure else None
        finally:
            if measure:
                tracemalloc.stop()
    return code, sink, capsys.readouterr().err, peak


def _verify(sink, kind, count):
    assert sink.header is not None
    assert sink.header["type"] == "header" and sink.header["stream"] == kind
    assert sink.ordered
    footer = sink.footer
    assert footer is not None and footer["type"] == "footer"
    assert footer["complete"] and footer["exhausted"]
    assert footer["count"] == count == sink.count
    assert footer["sha256"] == sink.digest.hexdigest()
    assert footer["hashed_bytes"] == sink.hashed_bytes


def _wal_edit(store):
    changed = False

    def edit_after_first_row(record):
        nonlocal changed
        if record["type"] in {"session", "message"} and not changed:
            changed = True
            store.execute("UPDATE sessions SET parent_session_id = 'concurrent' WHERE id = 'task'")
            store.execute("UPDATE messages SET content = 'concurrent' WHERE id = 7")
            store.commit()

    return edit_after_first_row


def _interrupt(record):
    if record["type"] == "message":
        raise KeyboardInterrupt()


# SQL changes are inputs to the same capture invariant, not standalone assertion helpers.
_GRAPH_CHANGES = {
    "edge-drift": "UPDATE sessions SET parent_session_id = 'new-boundary' WHERE id = 'task'",
    "oversize-routing": "UPDATE sessions SET session_key = zeroblob(1048576) WHERE id = 'root'",
}
_AUDIT_CHANGES = {
    "content-drift": ("UPDATE messages SET content = ? WHERE id = 2", ("same-count edit",)),
    "state-drift": ("UPDATE messages SET active = ? WHERE id = 2", (0,)),
    "display-drift": ("UPDATE messages SET display_metadata = ? WHERE id = 2", ('{"sender_id":"changed"}',)),
    "origin-drift": ("UPDATE sessions SET origin_json = ? WHERE id = 'root'", ('{"sender_id":"changed"}',)),
    "oversize-content": ("UPDATE messages SET content = ? WHERE id = 3", ("x" * (300 * 1024),)),
    "blob-content": ("UPDATE messages SET content = ? WHERE id = 3", (b"unsupported",)),
    "oversize-metadata": ("UPDATE messages SET display_metadata = ? WHERE id = 3", ("x" * (300 * 1024),)),
    "huge-blob": ("UPDATE messages SET content = zeroblob(?) WHERE id = 3", (16 * 1024 * 1024,)),
    "huge-text": ("UPDATE messages SET content = CAST(zeroblob(?) AS TEXT) WHERE id = 3", (16 * 1024 * 1024,)),
    "invalid-utf8": ("UPDATE messages SET content = CAST(x'80' AS TEXT) WHERE id = 3", ()),
}
_AUDIT_ERRORS = {
    "prefix": "exact session ID", "alias": "exact session ID", "title": "exact session ID",
    "absent": "exact session ID", "oversize-content": "source bytes", "blob-content": "unsupported",
    "oversize-metadata": "source bytes", "huge-blob": "exceeds 262144 source bytes",
    "huge-text": "exceeds 262144 source bytes", "invalid-utf8": "UnicodeDecodeError",
    "record-budget": "encoded bytes", "interrupt": "stream interrupted",
}


@pytest.fixture
def graph_case(store, tmp_path, scenario):
    if scenario in {"missing", "stale"}:
        store.close()
        (tmp_path / "state.db").unlink()
        if scenario == "stale":
            with sqlite3.connect(tmp_path / "state.db") as stale:
                stale.execute("CREATE TABLE sessions (id TEXT)")
    elif scenario == "large":
        store.executemany(
            "INSERT INTO sessions (id, parent_session_id, source, started_at, hidden, archived, profile_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ((f"fictional-{i:05d}", "root", "cli", 0, 1, 1, None) for i in range(1100)),
        )
        store.commit()
    elif scenario == "wal":
        store.execute("PRAGMA journal_mode=WAL")
        store.commit()
    return store


@pytest.mark.parametrize("scenario", [
    "projection", "missing", "stale", "edge-drift", "wal", "large", "oversize-routing", "short-write",
])
def test_inventory_is_exhaustive_metadata_only_or_fails_closed(graph_case, scenario, monkeypatch, capsys, tmp_path):
    """A successful graph is complete, coherent and metadata-only, never a partial success."""
    store = graph_case
    sizes = []
    forbidden = {"messages": None, "messages_fts": None, "system_prompts": None,
                 "sessions": _PRIVATE_SESSION | {"origin_json"}}
    reads, writes = _read_guard(monkeypatch, forbidden, sizes)
    path = tmp_path / "state.db"
    before_bytes = path.read_bytes() if path.exists() else None
    if scenario in {"missing", "stale"}:
        # Store admission is shared by both streams. Neither may create, migrate or fall back.
        for operation in ("inventory", "transcript"):
            code, sink, error, _ = _capture(capsys, monkeypatch, operation)
            assert code != 0 and error and sink.header is None and sink.footer is None
        assert (path.read_bytes() if path.exists() else None) == before_bytes
        assert not writes
        return

    _, baseline, error, _ = _capture(capsys, monkeypatch, "inventory")
    assert not error
    if scenario in _GRAPH_CHANGES:
        store.execute(_GRAPH_CHANGES[scenario])
        store.commit()
    before_bytes = path.read_bytes()
    callback = _wal_edit(store) if scenario == "wal" else None
    code, sink, error, _ = _capture(capsys, monkeypatch, "inventory", callback=callback,
                                   short=scenario == "short-write")
    assert reads and not writes
    if scenario in {"oversize-routing", "short-write"}:
        assert code != 0 and error and sink.footer is None
        assert ("source bytes" if scenario == "oversize-routing" else "short output write") in error
        assert path.read_bytes() == before_bytes
        return

    assert code == 0 and not error
    _verify(sink, "session_inventory", 1106 if scenario == "large" else 6)
    assert sink.header is not None and sink.footer is not None and baseline.footer is not None
    assert sink.header["scope"]["profile"] == "default"
    nodes = {r["session"]["id"]: r["session"] for r in sink.rows}
    assert set(nodes) == {"root", "compress", "reset", "task", "dangling", "foreign"} | (
        {f"fictional-{i:05d}" for i in range(1100)} if scenario == "large" else set())
    assert nodes["dangling"]["parent_session_id"] == "missing-parent"
    assert nodes["foreign"]["profile_name"] == "other"
    assert nodes["compress"]["hidden"] == 1 and nodes["reset"]["archived"] == 1
    assert all(not (_PRIVATE_SESSION | {"origin_json"}) & row.keys() for row in nodes.values())
    assert sum(row["profile_name"] is None for row in nodes.values()) == (1100 if scenario == "large" else 0)
    assert sink.footer["count"] == baseline.footer["count"]
    assert (sink.footer["sha256"] != baseline.footer["sha256"]) == (scenario == "edge-drift")
    if scenario == "wal":
        _, after, _, _ = _capture(capsys, monkeypatch, "inventory")
        assert after.footer is not None
        assert after.footer["sha256"] != baseline.footer["sha256"]
    else:
        assert path.read_bytes() == before_bytes


def _selected_entrypoint(store, tmp_path, explicit_profile):
    root = tmp_path / "isolated-root"
    selected = root / "profiles" / "fictional"
    selected.mkdir(parents=True)
    with sqlite3.connect(selected / "state.db") as target:
        store.backup(target)
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
    return command, env, selected


def _entrypoint_capture(command, env):
    result = subprocess.run(command, env=env, cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, timeout=60, check=False)
    sink = _StreamSink()
    for line in result.stdout.decode().splitlines(keepends=True):
        sink.write(line)
    return result.returncode, sink, result.stderr.decode(), None


@pytest.fixture
def audit_case(store, scenario):
    if scenario == "large":
        store.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                          (("root", "assistant", "fictional " * 1024, 10) for _ in range(1500)))
        store.commit()
    elif scenario == "wal":
        store.execute("PRAGMA journal_mode=WAL")
        store.commit()
    return store


@pytest.mark.parametrize("scenario", [
    "projection", "prefix", "alias", "title", "absent", "content-drift", "state-drift", "display-drift",
    "origin-drift", "oversize-content", "blob-content", "oversize-metadata", "large", "interrupt", "wal",
    "huge-blob", "huge-text", "home", "profile", "invalid-utf8", "record-budget",
])
def test_transcript_is_exact_bounded_audit_evidence_or_fails_closed(audit_case, scenario, monkeypatch, capsys, tmp_path):
    """Completion certifies one exact audit segment, not origin, lineage or a repaired source."""
    store = audit_case
    sizes = []
    forbidden = {"messages_fts": None, "system_prompts": None,
                 "sessions": _PRIVATE_SESSION, "messages": _PRIVATE_MESSAGE}
    reads, writes = _read_guard(monkeypatch, forbidden, sizes)
    _, baseline, error, _ = _capture(capsys, monkeypatch, "transcript", measure=scenario == "large")
    assert not error
    if scenario in _AUDIT_CHANGES:
        store.execute(*_AUDIT_CHANGES[scenario])
        store.commit()
    if scenario == "record-budget":
        import hermes_cli.session_evidence as evidence
        monkeypatch.setattr(evidence, "MAX_RECORD_BYTES", 100)
    sid = {"prefix": "roo", "alias": "latest", "title": "private title", "absent": "absent"}.get(scenario, "root")
    callback = {"wal": lambda: _wal_edit(store), "interrupt": lambda: _interrupt}.get(scenario, lambda: None)()
    before_bytes = (tmp_path / "state.db").read_bytes()
    selection = None
    if scenario in {"home", "profile"}:
        selection = _selected_entrypoint(store, tmp_path, scenario == "profile")
        command, env, _ = selection
        code, sink, error, peak = _entrypoint_capture(command, env)
    else:
        code, sink, error, peak = _capture(capsys, monkeypatch, "transcript", sid=sid, callback=callback,
                                          measure=scenario == "large")
    assert reads and not writes
    if scenario in _AUDIT_ERRORS:
        assert code == (130 if scenario == "interrupt" else 1)
        assert _AUDIT_ERRORS[scenario] in error and sink.footer is None
        assert "x" * 1000 not in json.dumps(sink.rows)
        assert max(sizes) < 262144
        if scenario in {"prefix", "alias", "title", "absent", "record-budget"}:
            assert sink.header is None
        assert (tmp_path / "state.db").read_bytes() == before_bytes
        return

    assert code == 0 and not error
    _verify(sink, "session_transcript", 1505 if scenario == "large" else 5)
    assert sink.header is not None and sink.footer is not None and baseline.footer is not None
    rows = sink.rows[:5]
    assert [r["message"]["id"] for r in rows] == [2, 3, 5, 6, 7]
    assert [r["audit_state"] for r in rows] == [
        "rewind_excluded" if scenario == "state-drift" else "active",
        "compaction_archive", "rewind_excluded", "active", "active",
    ]
    assert sink.header["human_origin"] == "unknown" and all(r["human_origin"] == "unknown" for r in rows)
    assert [r["message"]["role"] for r in rows] == ["user", "assistant", "user", "assistant", "user"]
    assert all(r["message"]["session_id"] == "root" and r["message"]["timestamp"] == 2 for r in rows)
    assert all(r["message"]["message_uid"] for r in rows)
    assert rows[0]["message"]["content"] == ("same-count edit" if scenario == "content-drift" else "架空の文 🌿")
    assert rows[1]["message"]["content"] == '[{"type":"text","text":"fictional structured reply"}]'
    assert rows[2]["message"]["display_metadata"] == '{"recovery_identity":"fictional"}'
    assert rows[4]["message"]["display_metadata"] == '{"author":"Human"}'
    serialized = json.dumps([sink.header, *sink.rows])
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in serialized
    assert "forbidden" not in serialized and "descendant must not appear" not in serialized
    assert all(not _PRIVATE_MESSAGE & row["message"].keys() for row in rows)
    assert sink.footer["count"] == baseline.footer["count"]
    if selection is not None:
        command, env, selected = selection
        assert sink.header["scope"] == {"home": str(selected), "profile": "fictional"}
        assert "wrong default home" not in serialized
        (selected / "state.db").unlink()
        missing_code, missing_sink, missing_error, _ = _entrypoint_capture(command, env)
        assert missing_code != 0 and missing_error and missing_sink.header is None and missing_sink.footer is None
        assert not (selected / "state.db").exists()
    else:
        assert (sink.footer["sha256"] != baseline.footer["sha256"]) == scenario.endswith("-drift")
    if scenario == "wal":
        _, after, _, _ = _capture(capsys, monkeypatch, "transcript")
        assert after.footer is not None
        assert after.footer["sha256"] != baseline.footer["sha256"]
    else:
        assert (tmp_path / "state.db").read_bytes() == before_bytes
    if scenario == "large":
        assert peak is not None and peak < 5 * 1024 * 1024

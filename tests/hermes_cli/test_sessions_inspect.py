"""Non-interactive session inspection through the CLI, with real temporary stores."""

import argparse
import json

import pytest


def _run_cli(capsys, *argv):
    from hermes_cli.subcommands.sessions import build_sessions_parser, cmd_sessions_lazy

    parser = argparse.ArgumentParser(prog="hermes")
    build_sessions_parser(parser.add_subparsers(dest="command"), cmd_sessions=cmd_sessions_lazy)
    args = parser.parse_args(["sessions", *argv])
    code = args.func(args)
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("argv,tool_args", [
    (("state", "resolve-topic", "--chat-id", "42", "--thread-id", "7"),
     {"action": "resolve_topic", "chat_id": "42", "thread_id": "7"}),
    (("state", "inspect", "--session-id", "topic-id"),
     {"action": "inspect_session", "session_id": "topic-id"}),
    (("state", "delivery", "--session-id", "topic-id"),
     {"action": "inspect_delivery", "session_id": "topic-id"}),
    (("state", "delivery", "--session-key", "agent:work:1", "--include-content"),
     {"action": "inspect_delivery", "session_key": "agent:work:1", "include_content": True}),
    (("state", "delivery", "--obligation-id", "missing"),
     {"action": "inspect_delivery", "obligation_id": "missing"}),
])
def test_state_cli_matches_tool_json(monkeypatch, tmp_path, capsys, argv, tool_args):
    home = tmp_path / "profiles" / "work"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_state import SessionDB
    from tools.session_state_tool import session_state_tool

    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session("topic-id", "telegram", profile_name="work", session_key="agent:work:1")
        db._write_sql("UPDATE sessions SET archived = 1 WHERE id = ?", ("topic-id",))
        db.apply_telegram_topic_migration()
        db.bind_telegram_topic(
            chat_id="42", thread_id="7", user_id="user", session_key="agent:work:1",
            session_id="topic-id", profile_name="work",
        )
        from gateway.delivery_ledger import _initialize_schema
        db._execute_write(_initialize_schema)
        db._write_sql(
            "INSERT INTO delivery_obligations "
            "(obligation_id, session_key, platform, chat_id, content, state, attempts, "
            "created_at, updated_at, adapter_profile) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("ob-1", "agent:work:1", "telegram", "42", "文" * 10000, "pending", 0, 1, 1, "work"),
        )
        capsys.readouterr()
        expected = json.loads(session_state_tool(tool_args))
        code, actual = _run_cli(capsys, *argv)
        assert code == 0
        assert actual == expected
        if tool_args["action"] == "inspect_session":
            assert actual["session"]["archived"] is True
            assert actual["session"]["profile_name"] == "work"
    finally:
        db.close()


@pytest.mark.parametrize("inherited_profile", ["default", "other"])
@pytest.mark.parametrize("explicit_profile", [False, True])
def test_state_cli_selected_home_owns_profile(monkeypatch, tmp_path, inherited_profile, explicit_profile):
    import subprocess
    import sys
    from pathlib import Path

    from gateway.delivery_ledger import _initialize_schema
    from hermes_state import SessionDB
    from tools.environments.local import build_subprocess_env

    root = tmp_path / ".hermes"
    home = root / "profiles" / "work"
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session("topic-id", "telegram", profile_name="work", session_key="agent:work:1")
        db.create_session("foreign-id", "telegram", profile_name="other", session_key="agent:other:1")
        db.apply_telegram_topic_migration()
        db.bind_telegram_topic(
            chat_id="42", thread_id="7", user_id="user", session_key="agent:work:1",
            session_id="topic-id", profile_name="work",
        )
        db._execute_write(_initialize_schema)
        db._write_sql(
            "INSERT INTO delivery_obligations "
            "(obligation_id, session_key, platform, chat_id, content, state, attempts, "
            "created_at, updated_at, adapter_profile) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("ob-1", "agent:work:1", "telegram", "42", "private", "pending", 0, 1, 1, "work"),
        )
    finally:
        db.close()

    env = build_subprocess_env(extra={
        "HOME": str(tmp_path / "user"), "HERMES_HOME": str(root if explicit_profile else home),
        "HERMES_SESSION_PROFILE": inherited_profile,
    })
    prefix = [sys.executable, "-m", "hermes_cli.main"]
    if explicit_profile:
        prefix += ["-p", "work"]
    cases = [
        (["resolve-topic", "--chat-id", "42", "--thread-id", "7"], "binding", "profile_name", "work"),
        (["inspect", "--session-id", "topic-id"], "session", "profile_name", "work"),
        (["delivery", "--session-id", "topic-id"], "rows", "adapter_profile", "work"),
        (["delivery", "--session-key", "agent:work:1"], "rows", "adapter_profile", "work"),
        (["delivery", "--obligation-id", "ob-1"], "rows", "adapter_profile", "work"),
        (["inspect", "--session-id", "foreign-id"], None, None, None),
    ]
    for argv, field, owner_key, owner in cases:
        result = subprocess.run(
            [*prefix, "sessions", "state", *argv], env=env,
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["found"] is (field is not None), (argv, payload)
        if field:
            rows = payload[field] if field == "rows" else [payload[field]]
            assert rows and all(row[owner_key] == owner for row in rows)
            assert all("content" not in row for row in rows)


def test_state_cli_not_found_does_not_create_store(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    code, result = _run_cli(capsys, "state", "inspect", "--session-id", "missing")
    assert code == 0
    assert result["ok"] is True and result["found"] is False
    assert not (tmp_path / "state.db").exists()


def test_state_cli_validation_is_nonzero_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    code, result = _run_cli(capsys, "state", "inspect", "--session-id", "select * from sessions")
    assert code != 0
    assert "error" in result
    assert not (tmp_path / "state.db").exists()


@pytest.mark.parametrize("options", [[], ["--limit", "1"], ["--after", "2026-01-01", "--before", "2026-02-01"]])
def test_search_cli_reuses_discovery_in_current_profile(monkeypatch, tmp_path, capsys, options):
    from datetime import datetime, timezone
    from hermes_state import SessionDB
    from tools.session_search_tool import session_search

    homes = {profile: tmp_path / "profiles" / profile for profile in ("work", "other")}
    for profile, home in homes.items():
        home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        db = SessionDB(db_path=home / "state.db")
        try:
            for index in range(2):
                sid = f"{profile}-{index}"
                db.create_session(sid, "telegram", profile_name=profile)
                db.append_message(sid, "user", "nebula routing owner")
            stamp = datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp()
            db._write_sql("UPDATE sessions SET started_at = ?", (stamp,))
            db._write_sql("UPDATE messages SET timestamp = ?", (stamp,))
        finally:
            db.close()
    monkeypatch.setenv("HERMES_HOME", str(homes["work"]))
    db = SessionDB(db_path=homes["work"] / "state.db", read_only=True)
    try:
        kwargs = dict(zip((option.removeprefix("--") for option in options[::2]), options[1::2]))
        expected = json.loads(session_search(query="nebula", db=db, **kwargs))
    finally:
        db.close()
    capsys.readouterr()
    code, result = _run_cli(capsys, "search", "nebula", *options)
    assert code == 0, result
    assert result["mode"] == "discover"
    assert result == expected
    assert result["results"]
    assert all(row["session_id"].startswith("work-") for row in result["results"])
    if options[:1] == ["--limit"]:
        assert len(result["results"]) == 1


def test_search_cli_rejects_invalid_time_without_writes(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        before = db._read_one("PRAGMA user_version", ())
        code, result = _run_cli(capsys, "search", "nebula", "--after", "not-a-time")
        assert code != 0 and "error" in result
        assert db._read_one("PRAGMA user_version", ()) == before
    finally:
        db.close()


def test_search_cli_absent_store_does_not_create_one(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    code, result = _run_cli(capsys, "search", "nebula")
    assert code != 0 and result["success"] is False
    assert not (tmp_path / "state.db").exists()


@pytest.mark.parametrize("selectors", [[], ["--session-id", "id", "--obligation-id", "ob"]])
def test_state_cli_delivery_requires_exactly_one_selector(monkeypatch, tmp_path, capsys, selectors):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        _run_cli(capsys, "state", "delivery", *selectors)
    assert exc.value.code == 2

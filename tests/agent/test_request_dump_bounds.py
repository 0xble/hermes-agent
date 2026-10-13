"""Request-debug artifacts stay useful without consuming unbounded disk space."""

import copy
import json
import os
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from agent.agent_runtime_helpers import dump_api_request_debug


MAX_DUMP_BYTES = 1024 * 1024


@pytest.mark.parametrize("secret_leaf", ["DB_PASSWORD=abcdef12", "PASS=174"])
def test_dump_redacts_structurally_and_preserves_json(tmp_path, monkeypatch, secret_leaf):
    # The short secret also occurs in harmless timestamp metadata.
    monkeypatch.setattr("agent.agent_runtime_helpers.datetime", SimpleNamespace(
        now=lambda: datetime(2026, 10, 13, 3, 11, 55, 617442),
    ))
    request = {"model": "debug-model", "messages": [{"role": "user", "content": secret_leaf,
                                                       "password": "hunter2"}]}
    original = copy.deepcopy(request)
    path = dump_api_request_debug(_agent(tmp_path), request, reason="preflight")
    assert path is not None
    serialized = path.read_text(encoding="utf-8")
    payload = json.loads(serialized)
    assert payload["request"]["body"]["messages"][0]["content"].startswith(secret_leaf.split("=")[0])
    serialized_request = json.dumps(payload["request"])
    assert secret_leaf.split("=")[1] not in serialized_request
    assert "hunter2" not in serialized_request
    assert request == original


@pytest.mark.parametrize("leaked_field", ["content", "password"])
def test_redaction_check_rejects_real_leaks_despite_timestamp_collision(tmp_path, monkeypatch, leaked_field):
    real_dump = dump_api_request_debug

    def leaking_dump(agent, request, **kwargs):
        path = real_dump(agent, request, **kwargs)
        assert path is not None
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["request"]["body"]["messages"][0][leaked_field] = request["messages"][0][leaked_field]
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    with monkeypatch.context() as patch:
        patch.setattr(__name__ + ".dump_api_request_debug", leaking_dump)
        with pytest.raises(AssertionError):
            test_dump_redacts_structurally_and_preserves_json(tmp_path, patch, "PASS=174")


@pytest.mark.platforms("posix")
def test_dump_is_owner_only_under_permissive_umask(tmp_path):
    import stat
    previous = os.umask(0o022)
    try:
        path = dump_api_request_debug(_agent(tmp_path), {"model": "debug-model"}, reason="preflight")
    finally:
        os.umask(previous)
    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _agent(logs_dir):
    return SimpleNamespace(
        logs_dir=logs_dir,
        session_id="bounds-test",
        api_mode="chat_completions",
        base_url="https://example.invalid/v1",
        client=None,
        _mask_api_key_for_logs=lambda key: "***",
        _vprint=lambda *args: None,
        log_prefix="",
        verbose_logging=True,
    )


@pytest.mark.parametrize(
    "content", ["🙂" * 600_000, "\x00\n\"" * 600_000, "🙂" * 600_000 + "\udcff"],
    ids=["utf8", "json-escapes", "surrogate-fallback"],
)
def test_oversized_dump_is_valid_bounded_json_without_mutating_request(tmp_path, content):
    request = {
        "model": "debug-model",
        "messages": [
            {"role": "user", "content": "Keep the diagnostic context."},
            {"role": "tool", "tool_call_id": "call-debug", "content": content},
        ],
        "tools": [{"type": "function", "function": {"name": "debug_tool", "description": content}}],
        "timeout": 30,
    }
    original = copy.deepcopy(request)
    error = ValueError("provider rejected request")
    error.body = {"detail": content}

    dump_file = dump_api_request_debug(_agent(tmp_path), request, reason="max_retries_exhausted", error=error)

    assert dump_file is not None
    assert dump_file.stat().st_size <= MAX_DUMP_BYTES
    serialized = dump_file.read_text(encoding="utf-8")
    payload = json.loads(serialized)
    assert payload["session_id"] == "bounds-test"
    assert payload["reason"] == "max_retries_exhausted"
    assert payload["request"]["url"] == "https://example.invalid/v1/chat/completions"
    body = payload["request"]["body"]
    assert body["model"] == "debug-model"
    assert body["messages"][0]["content"] == "Keep the diagnostic context."
    assert body["messages"][1]["tool_call_id"] == "call-debug"
    assert "truncated" in body["messages"][1]["content"]
    assert "truncated" in body["tools"][0]["function"]["description"]
    assert "truncated" in payload["error"]["body"]["detail"]
    assert "timeout" not in body
    assert request == original


def test_many_messages_fit_cap_while_keeping_recent_diagnostic_tail(tmp_path):
    messages = [{"role": "user", "content": f"message {index}: " + "x" * 100} for index in range(20_000)]
    newest = {"role": "user", "content": "Latest diagnostic message"}
    messages.append(newest)

    dump_file = dump_api_request_debug(
        _agent(tmp_path), {"model": "debug-model", "messages": messages}, reason="preflight"
    )

    assert dump_file is not None
    assert dump_file.stat().st_size <= MAX_DUMP_BYTES
    payload = json.loads(dump_file.read_text(encoding="utf-8"))
    assert payload["request"]["body"]["messages"][-1] == newest
    assert "truncated" in json.dumps(payload)
    assert len(messages) == 20_001


def test_write_keeps_newest_100_dumps_across_sessions_and_preserves_nondumps(tmp_path):
    old = []
    for index in range(105):
        path = tmp_path / f"request_dump_session-{index}_20250101_000000_{index:06d}.json"
        path.write_text("{}", encoding="utf-8")
        os.utime(path, ns=(index + 1, index + 1))
        old.append(path)
    protected = [
        tmp_path / "session_bounds-test.json",
        tmp_path / "bounds-test.jsonl",
        tmp_path / "request_dump_notes.json",
        tmp_path / ".request_dump_partial.json",
        tmp_path / "request_dump_other_20250101_000000_000000.json.tmp",
    ]
    for path in protected:
        path.write_text("keep me", encoding="utf-8")
    directory = tmp_path / "request_dump_directory_20250101_000000_000000.json"
    directory.mkdir()
    newest = dump_api_request_debug(_agent(tmp_path), {"model": "debug-model"}, reason="preflight")

    assert newest is not None and newest.exists()
    assert [path for path in old if path.exists()] == old[-99:]
    assert all(path.read_text(encoding="utf-8") == "keep me" for path in protected)
    assert directory.is_dir()


@pytest.mark.platforms("posix")
def test_retention_does_not_unlink_dump_shaped_symlinks(tmp_path):
    target = tmp_path / "session.json"
    target.write_text("keep me", encoding="utf-8")
    symlink = tmp_path / "request_dump_symlink_20250101_000000_000000.json"
    symlink.symlink_to(target)
    for index in range(105):
        (tmp_path / f"request_dump_old_20250101_000000_{index:06d}.json").write_text("{}", encoding="utf-8")

    dump_file = dump_api_request_debug(_agent(tmp_path), {"model": "debug-model"}, reason="preflight")

    assert dump_file is not None and dump_file.exists()
    assert symlink.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep me"


def test_retention_makes_progress_when_unrelated_entries_exceed_any_scan_budget(tmp_path, monkeypatch):
    """Unrelated session artifacts listed ahead of the dumps must not starve pruning."""
    for index in range(4100):
        (tmp_path / f"session_{index:05d}.json").write_text("{}", encoding="utf-8")
    for index in range(120):
        (tmp_path / f"request_dump_old_20250101_000000_{index:06d}.json").write_text("{}", encoding="utf-8")
    real_scandir = os.scandir

    @contextmanager
    def nondump_first(path):
        with real_scandir(path) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name.startswith("request_dump_"))
        yield iter(ordered)

    monkeypatch.setattr(os, "scandir", nondump_first)
    dump_file = dump_api_request_debug(_agent(tmp_path), {"model": "debug-model"}, reason="preflight")

    assert dump_file is not None and dump_file.exists()
    assert len(list(tmp_path.glob("request_dump_*.json"))) == 100
    assert len(list(tmp_path.glob("session_*.json"))) == 4100


def test_size_cap_accounts_for_platform_newline_expansion(monkeypatch):
    from agent import request_debug

    monkeypatch.setattr(request_debug.os, "linesep", "\r\n")
    # Fits the cap with LF newlines (1,008,069 bytes) but not once each one becomes CRLF.
    payload = {"request": {"body": {"messages": ["ab"] * 72000}}}
    bounded = request_debug.bounded_request_dump(payload)
    text = json.dumps(bounded, ensure_ascii=False, indent=2)
    on_disk = len(text.encode("utf-8")) + text.count("\n")
    assert on_disk <= MAX_DUMP_BYTES, f"CRLF output would be {on_disk} bytes"


def test_retention_errors_do_not_discard_a_successful_dump(tmp_path, monkeypatch):
    def scandir(path):
        raise PermissionError("test directory scan denied")

    monkeypatch.setattr(os, "scandir", scandir)
    request = {"model": "debug-model", "messages": [{"role": "user", "content": "small request"}]}
    dump_file = dump_api_request_debug(_agent(tmp_path), request, reason="preflight")

    assert dump_file is not None and dump_file.exists()
    assert json.loads(dump_file.read_text(encoding="utf-8"))["request"]["body"] == request


def test_one_unlink_failure_does_not_block_remaining_pruning(tmp_path, monkeypatch):
    from pathlib import Path

    old = []
    for index in range(102):
        path = tmp_path / f"request_dump_old_20250101_000000_{index:06d}.json"
        path.write_text("{}", encoding="utf-8")
        os.utime(path, ns=(index + 1, index + 1))
        old.append(path)
    unlink = Path.unlink

    def deny_one(path, *args, **kwargs):
        if path == old[2]:
            raise PermissionError("test unlink denied")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_one)
    dump_file = dump_api_request_debug(_agent(tmp_path), {"model": "debug-model"}, reason="preflight")

    assert dump_file is not None and dump_file.exists()
    assert old[2].exists()
    assert not old[0].exists() and not old[1].exists()
    assert all(path.exists() for path in old[3:])

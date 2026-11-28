"""The goal judge's relay receipt lookup reads only ledger bytes appended since the last call."""

from __future__ import annotations

import json
import os

import pytest

from hermes_cli import goals, goals_relay_receipts


def _row(rid: str, **extra) -> str:
    # Relay writes compact rows with ``id`` first; match that shape so the index takes its fast path.
    return json.dumps({"id": rid, "sender": "a", "target": "b", **extra}, separators=(",", ":")) + "\n"


def test_spaced_rows_fall_back_to_a_full_parse(_ledger):
    _ledger.write_text(json.dumps({"id": "s", "n": 1}) + "\n", encoding="utf-8")
    assert goals._relay_ledger_receipts(["s"])["s"]["n"] == 1


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(goals_relay_receipts, "_RELAY_RECEIPT_CACHE", {})
    return tmp_path / "receipts.jsonl"


def test_missing_ledger_and_empty_request_return_nothing(_ledger):
    assert goals._relay_ledger_receipts(["x"]) == {}
    _ledger.write_text(_row("x"), encoding="utf-8")
    assert goals._relay_ledger_receipts([]) == {}
    assert goals._relay_ledger_receipts([""]) == {}


def test_appended_rows_are_found_and_newest_row_wins(_ledger):
    _ledger.write_text(_row("a", n=1) + "not json\n" + _row("b"), encoding="utf-8")
    assert goals._relay_ledger_receipts(["a", "b", "c"]).keys() == {"a", "b"}

    with open(_ledger, "a", encoding="utf-8") as fh:
        fh.write(_row("c") + _row("a", n=2))
    found = goals._relay_ledger_receipts(["a", "c"])
    assert found["a"]["n"] == 2 and "c" in found


def test_second_call_parses_only_the_appended_rows(_ledger, monkeypatch):
    _ledger.write_text(_row("a") * 50, encoding="utf-8")
    goals._relay_ledger_receipts(["a"])
    with open(_ledger, "a", encoding="utf-8") as fh:
        fh.write(_row("z"))

    parsed = []
    real_loads = json.loads
    monkeypatch.setattr(goals_relay_receipts.json, "loads", lambda line, *a, **k: parsed.append(line) or real_loads(line, *a, **k))
    assert "z" in goals._relay_ledger_receipts(["z"])
    # only the wanted row is parsed; the 50 old rows and the appended row's index entry are not
    assert len(parsed) == 1


def test_partial_trailing_line_is_completed_by_the_next_append(_ledger):
    full = _row("late", n=1)
    _ledger.write_text(_row("a") + full[:20], encoding="utf-8")
    assert goals._relay_ledger_receipts(["late"]) == {}
    with open(_ledger, "a", encoding="utf-8") as fh:
        fh.write(full[20:])
    assert goals._relay_ledger_receipts(["late"])["late"]["n"] == 1


def test_truncation_and_rotation_trigger_a_full_reparse(_ledger):
    _ledger.write_text(_row("old") * 3 + _row("keep"), encoding="utf-8")
    assert "old" in goals._relay_ledger_receipts(["old"])

    _ledger.write_text(_row("new"), encoding="utf-8")  # truncated in place (smaller)
    assert goals._relay_ledger_receipts(["old", "new"]).keys() == {"new"}

    rotated = _ledger.with_name("receipts.jsonl.1")
    os.replace(_ledger, rotated)
    _ledger.write_text(_row("fresh") + _row("padding") * 5, encoding="utf-8")  # new inode, larger
    assert goals._relay_ledger_receipts(["new", "fresh"]).keys() == {"fresh"}


def test_same_size_rewrite_is_detected_by_mtime(_ledger):
    _ledger.write_text(_row("aaa"), encoding="utf-8")
    assert "aaa" in goals._relay_ledger_receipts(["aaa", "bbb"])
    _ledger.write_text(_row("bbb"), encoding="utf-8")
    os.utime(_ledger, ns=(1, _ledger.stat().st_mtime_ns + 10_000_000_000))
    assert goals._relay_ledger_receipts(["aaa", "bbb"]).keys() == {"bbb"}


def test_ids_needing_a_full_parse_are_still_indexed(_ledger):
    _ledger.write_text(
        '{"sender":"s","id":"late-id"}\n{"id":"esc\\u0041pe"}\n' + _row("plain"), encoding="utf-8")
    found = goals._relay_ledger_receipts(["late-id", "escApe", "plain"])
    assert found.keys() == {"late-id", "escApe", "plain"}


def test_rows_with_a_bom_or_invalid_utf8_do_not_break_the_scan(_ledger):
    _ledger.write_bytes(b"\xef\xbb\xbf" + _row("a").encode() + b"\xff\xfe\n" + _row("b").encode())
    found = goals._relay_ledger_receipts(["a", "b"])
    assert "b" in found

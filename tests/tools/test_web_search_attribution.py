"""Offline tests for web_search fan-out guidance and per-call attribution logging.

Every provider here is a fake; nothing reaches a network or a paid backend.
"""

import json
import threading

import pytest

import tools.web_result_cache as wrc
import tools.web_search_attribution as attribution
from tools import web_tools
from tools.web_result_cache import search_memo

SECRET_QUERY = "very-secret-needle site:example.com/private/path"


class _FakeProvider:
    name = "fake"
    SEARCH_TYPE = "auto"
    SEARCH_TYPE_REASON = "lite_paused"

    def __init__(self, success=True):
        self.calls = 0
        self.success = success

    def search(self, query, limit=5):
        self.calls += 1
        if not self.success:
            return {"success": False, "error": "upstream down"}
        return {"success": True, "data": {"web": [{"title": "t", "url": "https://e.com", "description": "d"}]}}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache" / "web"
    cache_dir.mkdir(parents=True)
    monkeypatch.setattr(wrc, "_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(wrc, "_web_config", dict)
    log = tmp_path / "logs" / "web_search_calls.jsonl"
    monkeypatch.setattr(attribution, "_log_path", lambda: log)
    search_memo.clear()
    yield log
    search_memo.clear()


def _records(log):
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_paid_miss_logs_one_record_and_hit_logs_as_hit(_isolated):
    provider = _FakeProvider()
    web_tools._memoized_search(provider, SECRET_QUERY, 5, session_id="sess-1")
    web_tools._memoized_search(provider, SECRET_QUERY, 5, session_id="sess-1")
    assert provider.calls == 1
    miss, hit = _records(_isolated)
    assert miss["cache_outcome"] == "miss" and hit["cache_outcome"] == "hit"
    assert miss["success"] is True
    assert miss["session_id"] == "sess-1"
    assert miss["provider"] == "fake:default"
    assert miss["search_type"] == "auto"
    assert miss["search_type_reason"] == "lite_paused"
    assert miss["caller_surface"] == "assistant_tool_call"
    assert len(miss["query_hash"]) == 16 and miss["query_length"] == len(SECRET_QUERY)


def test_failed_paid_call_is_logged_unsuccessful(_isolated):
    web_tools._memoized_search(_FakeProvider(success=False), "q", 5, session_id="s")
    (record,) = _records(_isolated)
    assert record["cache_outcome"] == "miss" and record["success"] is False


def test_no_raw_query_or_result_content_is_written(_isolated):
    web_tools._memoized_search(_FakeProvider(), SECRET_QUERY, 5, session_id="s")
    text = _isolated.read_text(encoding="utf-8")
    assert "needle" not in text and "example.com" not in text and "private" not in text
    assert "https://e.com" not in text


def test_logging_failure_never_breaks_search(monkeypatch, _isolated):
    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(attribution, "_log_path", boom)
    provider = _FakeProvider()
    result = web_tools._memoized_search(provider, "q", 5, session_id="s")
    assert result["success"] is True and provider.calls == 1


def test_unwritable_log_never_breaks_search(monkeypatch, tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    monkeypatch.setattr(attribution, "_log_path", lambda: blocker / "web_search_calls.jsonl")
    assert web_tools._memoized_search(_FakeProvider(), "q", 5)["success"] is True


def test_execute_code_rpc_surface_is_tagged(monkeypatch, _isolated):
    import model_tools
    from tools.code_execution_rpc import _default_dispatch

    provider = _FakeProvider()
    monkeypatch.setattr(
        model_tools, "handle_function_call",
        lambda name, args, **kw: web_tools._memoized_search(provider, args["query"], 5, session_id="s"),
    )
    _default_dispatch("task")("web_search", {"query": "from code"})
    (record,) = _records(_isolated)
    assert record["caller_surface"] == "execute_code_rpc"


def test_assistant_batch_size_is_recorded(_isolated):
    from agent.tool_executor import _invoke_with_search_batch

    provider = _FakeProvider()
    _invoke_with_search_batch(4, lambda: web_tools._memoized_search(provider, "batch", 5, session_id="s"))
    (record,) = _records(_isolated)
    assert record["batch_size"] == 4 and record["caller_surface"] == "assistant_tool_call"
    # The binding is scoped: a later unbound call has neither.
    web_tools._memoized_search(provider, "later", 5, session_id="s")
    assert _records(_isolated)[-1]["batch_size"] is None


def test_coalesced_waiter_is_logged_as_coalesced(monkeypatch, _isolated):
    provider = _FakeProvider()
    cache_provider, identity = web_tools._search_cache_identity(provider)
    first_lookup_missed = threading.Event()
    real_lookup = search_memo.lookup

    def lookup(*args, **kwargs):
        found = real_lookup(*args, **kwargs)
        if found is None:
            first_lookup_missed.set()
        return found

    monkeypatch.setattr(search_memo, "lookup", lookup)
    flight = search_memo.flight_lock(cache_provider, "shared", 10, **identity)
    flight.acquire()
    worker = threading.Thread(target=lambda: web_tools._memoized_search(provider, "shared", 5, session_id="w"))
    worker.start()
    assert first_lookup_missed.wait(5)
    search_memo.store(cache_provider, "shared", 10, {"success": True, "data": {"web": []}}, **identity)
    flight.release()
    worker.join(5)
    (record,) = _records(_isolated)
    assert record["cache_outcome"] == "coalesced" and provider.calls == 0


def test_log_rolls_over_by_size(monkeypatch, _isolated):
    monkeypatch.setattr(attribution, "_MAX_BYTES", 200)
    for index in range(10):
        attribution.record_search_call(
            session_id="s", provider="fake", search_type="auto", search_type_reason="",
            query=f"q{index}", cache_outcome="miss", success=True,
        )
    assert _isolated.with_name(_isolated.name + ".1").exists()
    assert not _isolated.with_name(_isolated.name + ".3").exists()


def test_summarizer_groups_by_day_surface_root_and_outcome(tmp_path):
    path = tmp_path / "calls.jsonl"
    rows = [
        {"timestamp": "2026-11-17T01:00:00+00:00", "caller_surface": "execute_code_rpc", "root_session_id": "r1", "cache_outcome": "miss"},
        {"timestamp": "2026-11-17T02:00:00+00:00", "caller_surface": "execute_code_rpc", "root_session_id": "r1", "cache_outcome": "miss"},
        {"timestamp": "2026-11-18T02:00:00+00:00", "caller_surface": "assistant_tool_call", "root_session_id": "r2", "cache_outcome": "hit"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot-json\n", encoding="utf-8")
    summary = attribution.summarize_records(path)
    assert {"day": "2026-11-17", "caller_surface": "execute_code_rpc", "root_session_id": "r1",
            "cache_outcome": "miss", "calls": 2} in summary
    assert len(summary) == 2


# ── guidance is prompt text only ─────────────────────────────────────────

def test_guidance_present_on_schema_stub_and_listing_and_child_prompt():
    from tools.code_execution_tool import build_execute_code_schema, generate_hermes_tools_module
    from tools.delegate_tool_progress import _build_child_system_prompt

    description = web_tools.WEB_SEARCH_SCHEMA["description"]
    assert "rounds of about 5" in description and "web_extract" in description and "templated" in description
    stub = generate_hermes_tools_module(["web_search"])
    assert "rounds of about 5" in stub and "Exa Lite is paused by default" in stub
    assert "rounds of about 5" in build_execute_code_schema({"web_search"}, mode="project")["description"]
    assert "rounds of about 5" in _build_child_system_prompt()
    # The stakes/Lite-paused wording is preserved.
    assert "Exa Lite is paused by default" in web_tools.WEB_SEARCH_SCHEMA["parameters"]["properties"]["stakes"]["description"]


def test_guidance_never_blocks_a_large_fan_out(_isolated):
    provider = _FakeProvider()
    for index in range(12):
        assert web_tools._memoized_search(provider, f"templated query {index}", 5, session_id="s")["success"]
    assert provider.calls == 12 and len(_records(_isolated)) == 12


def test_top_level_session_is_its_own_root(_isolated):
    web_tools._memoized_search(_FakeProvider(), "top level", 5, session_id="sess-top")
    (record,) = _records(_isolated)
    assert record["session_id"] == "sess-top"
    assert record["root_session_id"] == "sess-top"
    assert record["parent_session_id"] == ""


def test_concurrent_processes_never_lose_or_tear_records(_isolated):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    workers = [ctx.Process(target=_write_records, args=(str(_isolated), n, 40)) for n in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
        assert worker.exitcode == 0
    records = _records(_isolated)
    assert len(records) == 160
    assert {r["session_id"] for r in records} == {f"proc-{n}" for n in range(4)}


def _write_records(log_path, n, count):
    from pathlib import Path

    import tools.web_search_attribution as attr

    attr._log_path = lambda: Path(log_path)
    for _ in range(count):
        attr.record_search_call(
            session_id=f"proc-{n}", provider="fake", search_type="auto", search_type_reason="lite_paused",
            query="q", cache_outcome="miss", success=True,
        )


def test_summarizer_skips_non_object_json_lines(tmp_path):
    log = tmp_path / "calls.jsonl"
    good = {"timestamp": "2026-11-17T00:00:00+00:00", "caller_surface": "assistant_tool_call", "root_session_id": "r", "cache_outcome": "miss"}
    log.write_text("null\n[1, 2]\n\"text\"\n" + json.dumps(good) + "\nnot json\n", encoding="utf-8")
    assert attribution.summarize_records(log) == [
        {"day": "2026-11-17", "caller_surface": "assistant_tool_call", "root_session_id": "r", "cache_outcome": "miss", "calls": 1}
    ]

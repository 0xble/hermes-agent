import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


def load_plugin():
    path = Path(__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location("review_candidate_test_plugin", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path) -> tuple[str, str]:
    """Two-commit repository; returns (base_sha, head_sha)."""
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (tmp_path / "file.txt").write_text("one")
    run("add", "file.txt")
    run("commit", "-qm", "one")
    base = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    (tmp_path / "file.txt").write_text("two")
    run("add", "file.txt")
    run("commit", "-qm", "two")
    head = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    return base, head


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return load_plugin()


def _stub_review(plugin, monkeypatch, outcomes):
    """Feed successive delegate results; record the credentials each attempt used."""
    attempts = []

    def fake(context, head, parent, credentials):
        attempts.append(dict(credentials or {}))
        return outcomes[len(attempts) - 1]

    monkeypatch.setattr(plugin, "_spawn_review", fake)
    monkeypatch.setattr(plugin, "_load_credentials_for_test", lambda: {"model": "claude-fable-5.1"}, raising=False)
    import sys
    from types import ModuleType
    engine = ModuleType("agent.review_engine")
    engine._load_review_credentials_cfg = lambda: {"provider": "anthropic", "model": "claude-fable-5.1"}
    lifecycle = ModuleType("agent.subagent_lifecycle")
    lifecycle.get_active_subagent_parent = lambda: object()
    monkeypatch.setitem(sys.modules, "agent.review_engine", engine)
    monkeypatch.setitem(sys.modules, "agent.subagent_lifecycle", lifecycle)
    return attempts


def test_invalid_candidate_is_not_reviewed(plugin, tmp_path):
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": "", "head_sha": ""}))
    assert result["status"] == "not_reviewed"
    assert result["error_code"] == "invalid_candidate"


def test_child_cannot_review(plugin, tmp_path):
    from agent.delegation_context import delegated_child_context
    with delegated_child_context("child"):
        result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": "a", "head_sha": "b"}))
    assert result["error_code"] == "parent_only"


def test_same_commit_is_rejected(plugin, tmp_path):
    _, head = _repo(tmp_path)
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": head, "head_sha": head}))
    assert result["error_code"] == "empty_candidate"


def test_review_writes_receipt_per_head_and_is_reused(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    attempts = _stub_review(plugin, monkeypatch, [{"results": {"verdict": "approve"}, "review_model": "claude-fable-5.1"}])
    first = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert first["status"] == "reviewed"
    assert first["reviewer_model"] == "claude-fable-5.1"
    assert first["fallback_reason"] == ""
    assert first["covers"] == ["file.txt"]
    assert plugin._receipt_path(head).name == f"{head}.json"
    assert plugin._receipt_path(head).exists()
    # Second call for the same candidate reuses coverage without spawning another child.
    second = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert second["reused"] is True
    assert len(attempts) == 1


def test_availability_failure_falls_back_once_and_records_the_reason(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    attempts = _stub_review(plugin, monkeypatch, [
        {"error": "provider returned 429 rate_limit"},
        {"results": {"verdict": "approve"}},
    ])
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["status"] == "reviewed"
    assert result["reviewer_model"] == "claude-opus-5"
    assert "429" in result["fallback_reason"]
    assert [a.get("model") for a in attempts] == ["claude-fable-5.1", "claude-opus-5"]


def test_started_reviewer_that_times_out_is_never_retried_on_the_fallback(plugin, tmp_path, monkeypatch):
    """A child that began reviewing and then timed out is 'not reviewed', not re-run elsewhere."""
    base, head = _repo(tmp_path)
    attempts = _stub_review(plugin, monkeypatch, [{"error": "child timed out after 1200s"}])
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["status"] == "not_reviewed"
    assert result["error_code"] == "review_incomplete"
    assert len(attempts) == 1
    assert not plugin._receipt_path(head).exists()


def test_both_routes_unavailable_is_not_approval(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    _stub_review(plugin, monkeypatch, [{"error": "401 unauthorized"}, {"error": "503 unavailable"}])
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["success"] is False
    assert result["status"] == "not_reviewed"
    assert result["error_code"] == "review_unavailable"
    assert not plugin._receipt_path(head).exists()


def test_oversized_candidate_is_refused_not_truncated(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    monkeypatch.setattr(plugin, "_MAX_DIFF_BYTES", 10)
    attempts = _stub_review(plugin, monkeypatch, [{"results": {}}])
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["error_code"] == "diff_too_large"
    assert attempts == []


def test_availability_classifier_separates_the_two_failure_classes(plugin):
    for availability in ("429 rate limit", "401 unauthorized", "503 unavailable", "connection reset", "quota exceeded"):
        assert plugin._is_availability_failure(availability) is True
    for ran_and_failed in ("child timed out", "status unknown", "interrupted by user", "assertion failed in tests"):
        assert plugin._is_availability_failure(ran_and_failed) is False


def test_receipt_carries_the_verdict_as_data_not_prose(plugin, tmp_path, monkeypatch):
    """The child's fenced JSON is lifted into result.verdict; the raw child blob is kept beside it."""
    base, head = _repo(tmp_path)
    prose = ("No terminal here, static review only.\n\n```json\n"
             '{"verdict": "changes_requested", "findings": [{"severity": "high", "path": "file.txt", '
             '"line": 1, "message": "bad"}], "summary": "broken"}\n```\n- Blocker: none.')
    _stub_review(plugin, monkeypatch, [{"results": [{"status": "completed", "summary": prose, "model": "claude-fable-5-1"}]}])
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["status"] == "reviewed"
    assert result["result"]["verdict"] == "changes_requested"
    assert result["result"]["findings"][0]["path"] == "file.txt"
    assert result["raw_child_result"][0]["summary"] == prose


def test_unparsable_reviewer_output_is_marked_unparsed(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    _stub_review(plugin, monkeypatch, [{"results": [{"status": "completed", "summary": "looks fine to me"}]}])
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["result"]["verdict"] == "unparsed"
    assert "looks fine" in result["result"]["summary"]


def test_reviewer_brief_forbids_recursive_review_calls(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    seen = {}

    def fake(context, head_, parent, credentials):
        seen["context"] = context
        return {"results": [{"summary": '```json\n{"verdict": "approve"}\n```'}]}

    monkeypatch.setattr(plugin, "_spawn_review", fake)
    import sys
    from types import ModuleType
    engine = ModuleType("agent.review_engine"); engine._load_review_credentials_cfg = lambda: {"model": "m"}
    lifecycle = ModuleType("agent.subagent_lifecycle"); lifecycle.get_active_subagent_parent = lambda: object()
    monkeypatch.setitem(sys.modules, "agent.review_engine", engine)
    monkeypatch.setitem(sys.modules, "agent.subagent_lifecycle", lifecycle)
    plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head})
    assert "call review_candidate" in seen["context"] and "parent-only" in seen["context"]

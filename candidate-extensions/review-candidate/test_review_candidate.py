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


def _stub_dispatch(plugin, monkeypatch, outcomes):
    """Feed successive dispatch handles; record the credentials each attempt used."""
    attempts = []

    def fake(context, head, parent, credentials):
        attempts.append({"credentials": dict(credentials or {}), "context": context})
        return outcomes[len(attempts) - 1]

    monkeypatch.setattr(plugin, "_dispatch_review", fake)
    import sys
    from types import ModuleType
    engine = ModuleType("agent.review_engine")
    engine._load_review_credentials_cfg = lambda: {"provider": "anthropic", "model": "claude-fable-5-1"}
    lifecycle = ModuleType("agent.subagent_lifecycle")
    lifecycle.get_active_subagent_parent = lambda: object()
    monkeypatch.setitem(sys.modules, "agent.review_engine", engine)
    monkeypatch.setitem(sys.modules, "agent.subagent_lifecycle", lifecycle)
    return attempts


DISPATCHED = {"status": "dispatched", "delegation_id": "deleg_test1"}


def _reviewer_message(head: str, verdict: str = "changes_requested") -> str:
    return ("Static review only.\n\n```json\n"
            + json.dumps({"head_sha": head, "verdict": verdict,
                          "findings": [{"severity": "high", "path": "file.txt", "line": 1, "message": "bad"}],
                          "summary": "broken"})
            + "\n```\n")


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


def test_dispatch_returns_pending_and_hook_writes_the_receipt(plugin, tmp_path, monkeypatch):
    """The tool never blocks on the reviewer: it returns a handle and the stop hook finalizes."""
    base, head = _repo(tmp_path)
    attempts = _stub_dispatch(plugin, monkeypatch, [DISPATCHED])
    first = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert first["status"] == "pending"
    assert first["delegation_id"] == "deleg_test1"
    assert first["covers"] == ["file.txt"]
    assert plugin._pending_path(head).exists()
    assert not plugin._receipt_path(head).exists()
    assert "call review_candidate" in attempts[0]["context"] and "head_sha" in attempts[0]["context"]

    # A second request for the same in-flight candidate does not dispatch again (the fake dispatch
    # registers no live delegation, so liveness is stubbed to what a real running child would report).
    monkeypatch.setattr(plugin, "_pending_is_live", lambda pending: True)
    again = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert again["status"] == "pending" and again["reused"] is True
    assert len(attempts) == 1

    plugin._on_subagent_stop(child_summary=_reviewer_message(head), child_status="completed",
                             child_session_id="child-1", parent_session_id="parent-1")
    receipt = json.loads(plugin._receipt_path(head).read_text())
    assert receipt["status"] == "reviewed"
    assert receipt["reviewer_model"] == "claude-fable-5-1"
    assert receipt["fallback_reason"] == ""
    assert receipt["result"]["verdict"] == "changes_requested"
    assert receipt["result"]["findings"][0]["path"] == "file.txt"
    assert not plugin._pending_path(head).exists()

    # Once reviewed, the receipt is reused without a new dispatch.
    third = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert third["reused"] is True and third["status"] == "reviewed"
    assert len(attempts) == 1


def test_stop_hook_ignores_unrelated_children(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    _stub_dispatch(plugin, monkeypatch, [DISPATCHED])
    plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head})
    plugin._on_subagent_stop(child_summary="I refactored the widget and all tests pass.", child_status="completed")
    assert plugin._pending_path(head).exists()
    assert not plugin._receipt_path(head).exists()


def test_reviewer_that_ended_badly_is_recorded_as_not_reviewed(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    _stub_dispatch(plugin, monkeypatch, [DISPATCHED])
    plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head})
    plugin._on_subagent_stop(child_summary=f"Reviewing {head[:12]} ... interrupted", child_status="stalled")
    receipt = json.loads(plugin._receipt_path(head).read_text())
    assert receipt["status"] == "not_reviewed"
    assert receipt["error_code"] == "review_incomplete"
    assert not plugin._pending_path(head).exists()
    # A not_reviewed receipt is not reusable as approval: the next call dispatches again.
    attempts = _stub_dispatch(plugin, monkeypatch, [DISPATCHED])
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["status"] == "pending" and len(attempts) == 1


def test_availability_failure_falls_back_once_and_records_the_reason(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    attempts = _stub_dispatch(plugin, monkeypatch, [{"error": "provider returned 429 rate_limit"}, DISPATCHED])
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["status"] == "pending"
    assert result["reviewer_model"] == "claude-opus-5"
    assert "429" in result["fallback_reason"]
    assert [a["credentials"].get("model") for a in attempts] == ["claude-fable-5-1", "claude-opus-5"]
    plugin._on_subagent_stop(child_summary=_reviewer_message(head, "approve"), child_status="completed")
    receipt = json.loads(plugin._receipt_path(head).read_text())
    assert receipt["reviewer_model"] == "claude-opus-5" and "429" in receipt["fallback_reason"]


def test_non_availability_dispatch_error_is_not_retried_on_the_fallback(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    attempts = _stub_dispatch(plugin, monkeypatch, [{"error": "delegation spawning is paused by the operator"}])
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["status"] == "not_reviewed" and result["error_code"] == "review_incomplete"
    assert len(attempts) == 1
    assert not plugin._pending_path(head).exists()


def test_both_routes_unavailable_is_not_approval(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    _stub_dispatch(plugin, monkeypatch, [{"error": "401 unauthorized"}, {"error": "503 unavailable"}])
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["success"] is False and result["status"] == "not_reviewed"
    assert result["error_code"] == "review_unavailable"
    assert not plugin._receipt_path(head).exists() and not plugin._pending_path(head).exists()


def test_oversized_candidate_is_refused_not_truncated(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    monkeypatch.setattr(plugin, "_MAX_DIFF_BYTES", 10)
    attempts = _stub_dispatch(plugin, monkeypatch, [DISPATCHED])
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["error_code"] == "diff_too_large"
    assert attempts == []


def test_inline_result_is_finalized_immediately(plugin, tmp_path, monkeypatch):
    """A runtime that ran the child inline (depth>0) returns results, not a handle."""
    base, head = _repo(tmp_path)
    _stub_dispatch(plugin, monkeypatch, [{"results": [{"status": "completed", "summary": _reviewer_message(head, "approve")}]}])
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["status"] == "reviewed" and result["result"]["verdict"] == "approve"
    assert plugin._receipt_path(head).exists() and not plugin._pending_path(head).exists()


def test_unparsable_reviewer_output_is_marked_unparsed(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    _stub_dispatch(plugin, monkeypatch, [DISPATCHED])
    plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head})
    plugin._on_subagent_stop(child_summary=f"Reviewed {head[:12]}: looks fine to me", child_status="completed")
    receipt = json.loads(plugin._receipt_path(head).read_text())
    assert receipt["result"]["verdict"] == "unparsed"
    assert "looks fine" in receipt["result"]["summary"]


def test_availability_classifier_separates_the_two_failure_classes(plugin):
    for availability in ("429 rate limit", "401 unauthorized", "503 unavailable", "connection reset", "quota exceeded"):
        assert plugin._is_availability_failure(availability) is True
    for ran_and_failed in ("child timed out", "status unknown", "interrupted by user", "assertion failed in tests"):
        assert plugin._is_availability_failure(ran_and_failed) is False


def test_failed_reviewer_with_no_summary_is_recorded_not_reviewed(plugin, tmp_path, monkeypatch):
    """The I6 review's high finding: a timed-out child has summary None and status 'timeout'.

    Before, nothing could match such a child to its marker (matching was by summary text), so the
    marker stayed pending forever and a later caller would reuse it as an in-flight review.
    """
    base, head = _repo(tmp_path)
    _stub_dispatch(plugin, monkeypatch, [DISPATCHED])
    plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head})
    plugin._on_subagent_start(child_session_id="child-9", child_goal=f"Review candidate {head[:12]}")
    plugin._on_subagent_stop(child_summary=None, child_status="timeout", child_session_id="child-9")
    receipt = json.loads(plugin._receipt_path(head).read_text())
    assert receipt["status"] == "not_reviewed" and receipt["error_code"] == "review_incomplete"
    assert "no summary" in receipt["error"]
    assert not plugin._pending_path(head).exists()


def test_start_hook_ignores_children_that_are_not_reviews(plugin):
    plugin._on_subagent_start(child_session_id="other", child_goal="Refactor the widget")
    assert "other" not in plugin._CHILD_HEADS


def test_fallback_reviewer_follows_configured_chain(plugin, monkeypatch):
    import sys
    from types import ModuleType
    cfg = ModuleType("hermes_cli.config")
    cfg.load_config_readonly = lambda: {"auxiliary": {"review": {"fallback_providers": [
        {"provider": "custom:claude-proxy", "model": "claude-opus-5", "base_url": "http://127.0.0.1:8317/v1"}]}}}
    monkeypatch.setitem(sys.modules, "hermes_cli.config", cfg)
    fb = plugin._fallback_credentials({"provider": "custom:claude-proxy", "model": "claude-fable-5-1"})
    assert fb["provider"] == "custom:claude-proxy" and fb["model"] == "claude-opus-5" and fb["base_url"].endswith("/v1")


def test_stale_pending_marker_is_recorded_and_redispatched(plugin, tmp_path, monkeypatch):
    """A marker whose delegation is gone (stalled child, gateway crash) must not refuse forever."""
    base, head = _repo(tmp_path)
    attempts = _stub_dispatch(plugin, monkeypatch, [DISPATCHED, DISPATCHED])
    plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head})
    assert len(attempts) == 1
    monkeypatch.setattr(plugin, "_pending_is_live", lambda pending: False)
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["status"] == "pending" and result.get("reused") is None
    assert len(attempts) == 2, "stale marker must trigger a fresh dispatch"
    assert plugin._pending_path(head).exists()


def test_live_pending_marker_is_reused(plugin, tmp_path, monkeypatch):
    base, head = _repo(tmp_path)
    attempts = _stub_dispatch(plugin, monkeypatch, [DISPATCHED])
    plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head})
    monkeypatch.setattr(plugin, "_pending_is_live", lambda pending: True)
    result = json.loads(plugin.review_candidate({"repository": str(tmp_path), "base_sha": base, "head_sha": head}))
    assert result["reused"] is True and len(attempts) == 1


def test_pending_age_bound(plugin):
    old = {"dispatched_at": "2020-01-01T00:00:00+00:00", "delegation_id": "deleg_x"}
    assert plugin._pending_is_live(old) is False
    assert plugin._pending_is_live({}) is False

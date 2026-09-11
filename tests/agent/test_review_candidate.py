"""Candidate identity, freshness, and typed native-review results."""

from __future__ import annotations

import base64
import json
import subprocess
from types import SimpleNamespace

import pytest

from agent.review_candidate import (
    NativeReviewResultV1,
    ReviewCandidateStale,
    ReviewCandidateV1,
    capture_review_candidate,
    native_review_completion_contract,
    require_fresh_candidate,
)


def _git(repo, *args, input_bytes=None):
    return subprocess.run(
        ["git", *args], cwd=repo, input=input_bytes, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode().strip()


def _capture_changed_candidate(repo, base):
    (repo / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    return capture_review_candidate(repo, base, ["tracked.py"])


@pytest.fixture
def candidate_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Review Test")
    _git(repo, "config", "user.email", "review@example.invalid")
    (repo / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "other.py").write_text("other = 1\n", encoding="utf-8")
    _git(repo, "add", "tracked.py", "other.py")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_capture_binds_explicit_base_scope_dirty_and_untracked_without_mutating_index(candidate_repo):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    (candidate_repo / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (candidate_repo / "new.txt").write_bytes(b"new candidate\n")
    index_before = (candidate_repo / ".git" / "index").read_bytes()

    candidate = capture_review_candidate(candidate_repo, base, ["tracked.py", "new.txt"])

    assert isinstance(candidate, ReviewCandidateV1)
    assert candidate.base_commit == base
    assert candidate.head_commit == base
    assert candidate.accepted_scope == ("new.txt", "tracked.py")
    assert "-value = 1" in candidate.tracked_patch
    assert "+value = 2" in candidate.tracked_patch
    assert [entry.path for entry in candidate.untracked_files] == ["new.txt"]
    assert base64.b64decode(candidate.untracked_files[0].content_base64) == b"new candidate\n"
    assert (candidate_repo / ".git" / "index").read_bytes() == index_before


def test_candidate_identity_changes_for_each_bound_input(candidate_repo):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    (candidate_repo / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (candidate_repo / "new.txt").write_text("one\n", encoding="utf-8")
    original = capture_review_candidate(candidate_repo, base, ["tracked.py", "new.txt"])

    (candidate_repo / "tracked.py").write_text("value = 3\n", encoding="utf-8")
    assert capture_review_candidate(candidate_repo, base, ["tracked.py", "new.txt"]).candidate_id != original.candidate_id
    (candidate_repo / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (candidate_repo / "new.txt").write_text("two\n", encoding="utf-8")
    assert capture_review_candidate(candidate_repo, base, ["tracked.py", "new.txt"]).candidate_id != original.candidate_id
    assert capture_review_candidate(candidate_repo, base, ["tracked.py"]).candidate_id != original.candidate_id

    (candidate_repo / "other.py").write_text("other = 2\n", encoding="utf-8")
    _git(candidate_repo, "add", "other.py")
    _git(candidate_repo, "commit", "-qm", "advance head")
    assert capture_review_candidate(candidate_repo, base, ["tracked.py", "new.txt"]).candidate_id != original.candidate_id


def test_require_fresh_candidate_rejects_changes(candidate_repo):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    candidate = _capture_changed_candidate(candidate_repo, base)
    require_fresh_candidate(candidate)
    (candidate_repo / "tracked.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ReviewCandidateStale, match="candidate changed"):
        require_fresh_candidate(candidate)


def test_capture_rejects_implicit_or_outside_scope(candidate_repo):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="accepted_scope"):
        capture_review_candidate(candidate_repo, base, [])
    with pytest.raises(ValueError, match="repository-relative"):
        capture_review_candidate(candidate_repo, base, ["../outside"])


def test_capture_rejects_empty_or_mistyped_scope(candidate_repo):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="contains no tracked changes"):
        capture_review_candidate(candidate_repo, base, ["typo-does-not-exist.py"])
    with pytest.raises(ValueError, match="base_revision"):
        capture_review_candidate(candidate_repo, "", ["tracked.py"])


def test_native_result_separates_runtime_judgment_coverage_model_and_candidate(candidate_repo):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    candidate = _capture_changed_candidate(candidate_repo, base)
    summary = json.dumps({
        "contract": "native_review_judgment_v1",
        "candidate_id": candidate.candidate_id,
        "judgment": "approve",
        "coverage": ["source", "tests-read"],
        "summary": "No blocking findings.",
    })
    result = NativeReviewResultV1.from_delegation_entry(
        {"status": "completed", "exit_reason": "completed", "model": "provider/model", "summary": summary,
         "schema_valid": True},
        candidate,
    )
    assert result.runtime_status == "completed"
    assert result.judgment == "approve"
    assert result.coverage == ("source", "tests-read")
    assert result.actual_model == "provider/model"
    assert result.candidate_id == candidate.candidate_id


def test_native_result_never_turns_failed_runtime_into_approval(candidate_repo):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    candidate = _capture_changed_candidate(candidate_repo, base)
    summary = json.dumps({
        "contract": "native_review_judgment_v1",
        "candidate_id": candidate.candidate_id,
        "judgment": "approve",
        "coverage": [],
        "summary": "claimed approval",
    })
    result = NativeReviewResultV1.from_delegation_entry(
        {"status": "failed", "exit_reason": "error", "model": "provider/model", "summary": summary,
         "schema_valid": True},
        candidate,
    )
    assert result.runtime_status == "failed"
    assert result.judgment == "unknown"


@pytest.mark.parametrize("entry_update", [
    {"exit_reason": "max_iterations", "truncated": True},
    {"schema_valid": False},
    {"schema_valid": None},
])
def test_native_result_never_turns_truncated_or_unvalidated_runtime_into_approval(
    candidate_repo, entry_update,
):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    candidate = _capture_changed_candidate(candidate_repo, base)
    summary = json.dumps({
        "contract": "native_review_judgment_v1",
        "candidate_id": candidate.candidate_id,
        "judgment": "approve", "coverage": [], "summary": "claimed approval",
    })
    entry = {
        "status": "completed", "exit_reason": "completed", "model": "provider/model",
        "summary": summary, "schema_valid": True,
    }
    entry.update(entry_update)
    result = NativeReviewResultV1.from_delegation_entry(entry, candidate)
    assert result.runtime_status == "completed"
    assert result.judgment == "unknown"


def test_native_result_payload_rejects_runtime_judgment_contradiction(candidate_repo):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    candidate = _capture_changed_candidate(candidate_repo, base)
    payload = {
        "contract": "native_review_result_v1", "candidate_id": candidate.candidate_id,
        "runtime_status": "failed", "exit_reason": "error", "judgment": "approve",
        "coverage": [], "actual_model": "provider/model", "summary": "claimed approval",
    }
    with pytest.raises(ValueError, match="fully completed"):
        NativeReviewResultV1.from_payload(payload, candidate_id=candidate.candidate_id)


def test_native_review_result_cannot_grant_action_authority(candidate_repo):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    candidate = _capture_changed_candidate(candidate_repo, base)
    payload = {
        "contract": "native_review_result_v1", "candidate_id": candidate.candidate_id,
        "runtime_status": "completed", "exit_reason": "completed", "judgment": "approve",
        "coverage": [], "actual_model": "provider/model", "summary": "claimed approval",
        "grants_authority": True, "requires_existing_authority": False,
    }
    with pytest.raises(ValueError, match="cannot grant action authority"):
        NativeReviewResultV1.from_payload(payload, candidate_id=candidate.candidate_id)


def test_native_result_rejects_wrong_candidate_identity(candidate_repo):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    candidate = _capture_changed_candidate(candidate_repo, base)
    summary = json.dumps({
        "contract": "native_review_judgment_v1",
        "candidate_id": "sha256:" + "0" * 64,
        "judgment": "request_changes",
        "coverage": ["source"],
        "summary": "finding",
    })
    with pytest.raises(ValueError, match="candidate_id"):
        NativeReviewResultV1.from_delegation_entry(
            {"status": "completed", "exit_reason": "completed", "model": "provider/model", "summary": summary,
             "schema_valid": True},
            candidate,
        )


def test_start_review_binds_candidate_schema_and_policy(candidate_repo, monkeypatch):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    candidate = _capture_changed_candidate(candidate_repo, base)
    captured = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return json.dumps({"status": "dispatched", "delegation_id": "review-1"})

    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"auxiliary": {"review": {"tool_policy": "inspection_only"}}},
    )
    monkeypatch.setattr("tools.delegate_tool.delegate_task", fake_delegate_task)
    monkeypatch.setattr("tools.async_delegation.get_native_review_reuse", lambda *_a, **_k: None)

    from agent.review_engine import start_review

    result = start_review(
        SimpleNamespace(ephemeral_system_prompt=""),
        [{"role": "user", "content": "Review the candidate."}],
        candidate=candidate,
    )

    assert result["delegation_id"] == "review-1"
    assert captured["child_tool_policy"] == "inspection_only"
    assert captured["output_schema"]["properties"]["candidate_id"]["const"] == candidate.candidate_id
    assert candidate.candidate_id in captured["context"]
    assert "Do not run repository tests" in captured["goal"]


def test_start_review_reuses_unchanged_durable_outcome(candidate_repo, monkeypatch):
    base = _git(candidate_repo, "rev-parse", "HEAD")
    candidate = _capture_changed_candidate(candidate_repo, base)
    reuse = {
        "status": "reused", "delegation_id": "review-old",
        "candidate_id": candidate.candidate_id,
        "native_review_result": {"judgment": "request_changes", "actual_model": "fallback-provider/fallback-model"},
    }
    monkeypatch.setattr("tools.async_delegation.get_native_review_reuse", lambda *_a, **_k: reuse)
    monkeypatch.setattr(
        "tools.delegate_tool.delegate_task",
        lambda **_kwargs: pytest.fail("unchanged result should not dispatch another reviewer"),
    )
    from agent.review_engine import start_review

    result = start_review(
        SimpleNamespace(ephemeral_system_prompt=""),
        [{"role": "user", "content": "Review the candidate."}], candidate=candidate,
    )
    assert result["delegation_id"] == "review-old"
    assert result["review_model"] == "fallback-provider/fallback-model"


def test_native_async_ledger_reuses_only_valid_unchanged_terminal_result(
    candidate_repo, monkeypatch, tmp_path,
):
    from dataclasses import asdict
    from tools import async_delegation as ad

    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    base = _git(candidate_repo, "rev-parse", "HEAD")
    candidate = _capture_changed_candidate(candidate_repo, base)
    contract = native_review_completion_contract(candidate, focus="security")
    typed = NativeReviewResultV1(
        candidate_id=candidate.candidate_id, runtime_status="completed",
        exit_reason="completed", judgment="approve", coverage=("source",),
        actual_model="provider/actual", summary="No blocking findings.",
    )
    now = 123.0
    with ad._transaction() as conn:
        conn.execute("""INSERT INTO async_delegations
            (delegation_id, origin_session, state, dispatched_at, updated_at,
             task_json, event_json, delivery_state)
            VALUES (?, '', 'completed', ?, ?, ?, ?, 'pending')""", (
                "review-valid", now, now,
                json.dumps({"completion_contract": contract}),
                json.dumps({
                    "completion_contract": contract,
                    "native_review_result": asdict(typed),
                }),
            ))

    reused = ad.get_native_review_reuse(candidate, focus="security")
    assert reused is not None
    assert reused["status"] == "reused"
    assert reused["delegation_id"] == "review-valid"
    assert reused["native_review_result"]["judgment"] == "approve"
    assert ad.get_native_review_reuse(candidate, focus="different") is None

    (candidate_repo / "tracked.py").write_text("changed after review\n", encoding="utf-8")
    with pytest.raises(ReviewCandidateStale):
        ad.get_native_review_reuse(candidate, focus="security")


def test_native_async_ledger_never_reuses_live_review_from_another_session(
    candidate_repo, monkeypatch, tmp_path,
):
    from tools import async_delegation as ad

    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    base = _git(candidate_repo, "rev-parse", "HEAD")
    candidate = _capture_changed_candidate(candidate_repo, base)
    contract = native_review_completion_contract(candidate)
    with ad._transaction() as conn:
        conn.execute("""INSERT INTO async_delegations
            (delegation_id, origin_session, parent_session_id, state,
             dispatched_at, updated_at, task_json, delivery_state)
            VALUES (?, ?, ?, 'running', 1, 1, ?, 'pending')""", (
                "review-other-session", "other-route", "other-parent",
                json.dumps({"completion_contract": contract}),
            ))
    assert ad.get_native_review_reuse(candidate) is None


def test_native_review_notification_invalidates_stale_candidate(candidate_repo):
    from dataclasses import asdict
    from tools.process_registry_notifications import format_process_notification

    base = _git(candidate_repo, "rev-parse", "HEAD")
    candidate = _capture_changed_candidate(candidate_repo, base)
    contract = native_review_completion_contract(candidate)
    typed = NativeReviewResultV1(
        candidate_id=candidate.candidate_id, runtime_status="completed",
        exit_reason="completed", judgment="approve", coverage=("source",),
        actual_model="provider/actual", summary="No blocking findings.",
    )
    (candidate_repo / "tracked.py").write_text("changed after review\n", encoding="utf-8")
    text = format_process_notification({
        "type": "async_delegation", "delegation_id": "review-stale",
        "completion_contract": contract, "native_review_result": asdict(typed),
        "status": "completed", "model": "configured/model",
    })
    assert text is not None
    assert '\"judgment\": \"unknown\"' in text
    assert "not currently admissible" in text
    assert "candidate changed" in text.lower()


def test_candidate_identity_preserves_distinct_non_utf8_tracked_patch_bytes(candidate_repo, monkeypatch):
    from agent import review_candidate

    path = candidate_repo / "invalid-utf8.txt"
    path.write_bytes(b"value=base\n")
    _git(candidate_repo, "add", "invalid-utf8.txt")
    _git(candidate_repo, "commit", "-m", "add invalid utf8 fixture")
    base = _git(candidate_repo, "rev-parse", "HEAD")
    patch = {"bytes": b"@@ -1 +1 @@\n-value=base\n+value=\x80\n"}
    real_git = review_candidate._git
    monkeypatch.setattr(
        review_candidate, "_git",
        lambda repo, *args: patch["bytes"] if args and args[0] == "diff" else real_git(repo, *args),
    )

    first = review_candidate.capture_review_candidate(candidate_repo, base, ["invalid-utf8.txt"])
    patch["bytes"] = b"@@ -1 +1 @@\n-value=base\n+value=\x81\n"
    second = review_candidate.capture_review_candidate(candidate_repo, base, ["invalid-utf8.txt"])

    assert first.tracked_patch == second.tracked_patch
    assert first.candidate_id != second.candidate_id
    assert first.tracked_patch_sha256 != second.tracked_patch_sha256


def test_capture_and_freshness_never_execute_textconv(candidate_repo, tmp_path):
    import shlex
    import sys

    marker = tmp_path / "converter-ran"
    converter = tmp_path / "converter.py"
    converter.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "print('converted evidence')\n"
    )
    (candidate_repo / ".gitattributes").write_text("tracked.py diff=probe\n")
    _git(candidate_repo, "config", "diff.probe.textconv",
         f"{shlex.quote(sys.executable)} {shlex.quote(str(converter))}")
    candidate = _capture_changed_candidate(candidate_repo, "HEAD")
    assert not marker.exists()
    assert "+value = 2" in candidate.tracked_patch
    assert "converted evidence" not in candidate.tracked_patch
    require_fresh_candidate(candidate)
    assert not marker.exists()

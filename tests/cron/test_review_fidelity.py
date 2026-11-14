"""Rewrites must preserve the contract-selected source lines, including on replay."""
from __future__ import annotations

import time

import pytest

from cron.review import check_contract
from cron import scheduler_review as sr


ORIGINAL = """# Daily Report
**Status:** Ready
**Move:** Ship the patch
**Running:** 1
## Tasks
1. [task-a] Ship the patch
2. [task-b] Verify delivery
Detail: terse
""".strip()
PATTERNS = [r"^\*\*(?:Status|Move|Running):\*\*", r"^\d+\. "]
CONTRACT = {"title": "Daily Report", "required_sections": ["Tasks"],
            "preserve_line_patterns": PATTERNS}
CASES = {
    "detail": ORIGINAL.replace("Detail: terse", "Detail: clear supporting evidence"),
    "status": ORIGINAL.replace("Ready", "Blocked"),
    "move": ORIGINAL.replace("**Move:** Ship the patch", "**Move:** Wait"),
    "running": ORIGINAL.replace("**Running:** 1", "**Running:** 2"),
    "task_id": ORIGINAL.replace("[task-a]", "[task-c]"),
    "task_number": ORIGINAL.replace("1. [task-a]", "3. [task-a]"),
    "task_text": ORIGINAL.replace("1. [task-a] Ship the patch", "1. [task-a] Wait"),
    "delete": ORIGINAL.replace("1. [task-a] Ship the patch\n", ""),
    "add": ORIGINAL.replace("Detail:", "3. [task-c] Invented task\nDetail:"),
    "reorder": ORIGINAL.replace("1. [task-a] Ship the patch\n2. [task-b] Verify delivery",
                                "2. [task-b] Verify delivery\n1. [task-a] Ship the patch"),
    "fence": ORIGINAL.replace("1. [task-a] Ship the patch", "```text\n1. [task-a] Ship the patch\n```"),
    "fake_fence": ORIGINAL.replace("1. [task-a] Ship the patch",
                                   "1. [task-a] Wait\n```text\n1. [task-a] Ship the patch\n```"),
    "indent": ORIGINAL.replace("1. [task-a] Ship the patch", "    1. [task-a] Ship the patch"),
}


@pytest.mark.parametrize("path", ["select", "deliver"])
@pytest.mark.parametrize("mode", ["FIX", "RERUN", "saved_FIX", "saved_RERUN", "PRESENT"])
@pytest.mark.parametrize("case", CASES)
def test_review_preserves_source_lines(path, mode, case, monkeypatch):
    candidate = CASES[case]
    assert check_contract(candidate, CONTRACT)["passed"]
    decision = mode.removeprefix("saved_")
    payload = candidate if decision == "FIX" else "presentation needs repair"
    job = {"id": "test-job", "deliver": "local", "review": {
        "subject": "test-topic", "contract": "contract.yaml", "max_reruns": 1}}
    if mode.startswith("saved_"):
        job["_review_decision"] = [decision, payload]
    subject = {"chat_id": "chat", "thread_id": "topic", "user_id": "user",
               "session_id": "session", "session_key": "key"}
    receipts, sent = [], []

    def hop(*args):
        assert not mode.startswith("saved_")
        return "PRESENT" if decision == "PRESENT" else f"{decision}\n{payload}"

    monkeypatch.setattr(sr, "_run_review_hop", hop)
    monkeypatch.setattr(sr, "_bounded_review_rerun", lambda *a: (True, "", candidate, None))
    monkeypatch.setattr(sr, "_resolve_review_subject", lambda *a: subject)
    monkeypatch.setattr("cron.review.load_contract", lambda *a: CONTRACT)
    monkeypatch.setattr("cron.review.append_receipt", receipts.append)
    monkeypatch.setattr("cron.scheduler_delivery._deliver_result",
                        lambda job, text, **kw: sent.append(text))
    expected = candidate if case == "detail" and decision != "PRESENT" else ORIGINAL
    expected_decision = decision if case == "detail" or decision == "PRESENT" else decision + "_REJECTED"
    if path == "select":
        receipt = {"candidates": []}
        actual, selected = sr._select_review_candidate(
            job, ORIGINAL, subject, CONTRACT, receipt, time.time() + 60)
        assert (actual, selected) == (expected_decision, expected)
    else:
        assert sr._deliver_reviewed_result(job, ORIGINAL, {}, None) is None
        assert sent == [expected]
        assert receipts[0]["decision"] == expected_decision
        assert receipts[0]["text_sent"] == expected
        assert receipts[0]["selected_candidate"] == (decision if expected != ORIGINAL else "original")


@pytest.mark.parametrize("metadata", [None, "^x", {}, [], [1], [""], ["["], [r"^not-present$"]])
@pytest.mark.parametrize("mode", ["FIX", "RERUN", "saved_FIX"])
def test_invalid_preservation_rule_falls_back_but_old_contracts_keep_behavior(metadata, mode, monkeypatch):
    candidate = CASES["move"]
    decision = mode.removeprefix("saved_")
    job = {"review": {"max_reruns": 1}}
    payload = candidate if decision == "FIX" else "retry"
    if mode.startswith("saved_"):
        job["_review_decision"] = [decision, payload]
    monkeypatch.setattr(sr, "_run_review_hop", lambda *a: f"{decision}\n{payload}")
    monkeypatch.setattr(sr, "_bounded_review_rerun", lambda *a: (True, "", candidate, None))
    contract = dict(CONTRACT, preserve_line_patterns=metadata)
    receipt = {"candidates": []}
    actual, selected = sr._select_review_candidate(job, ORIGINAL, {}, contract, receipt, time.time() + 60)
    assert (actual, selected) == (decision + "_REJECTED", ORIGINAL)
    assert not receipt["candidates"][0]["passed"]
    assert receipt["candidates"][0]["violations"]
    old_contract = {k: v for k, v in CONTRACT.items() if k != "preserve_line_patterns"}
    actual, selected = sr._select_review_candidate(
        {"review": {"max_reruns": 1}}, ORIGINAL, {}, old_contract, {"candidates": []}, time.time() + 60)
    assert (actual, selected) == (decision, candidate)

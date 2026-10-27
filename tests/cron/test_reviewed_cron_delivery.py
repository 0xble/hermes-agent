from __future__ import annotations

import pytest

from cron.review import check_contract, parse_decision
from cron.scheduler_review import _review_wake_text


CONTRACT = {
    "title": "Stripe Priorities",
    "required_lines": ["**Focus:**"],
    "required_sections": ["Priorities"],
    "optional_sections": ["Upcoming", "Waiting / Blocked", "Changes", "Standup"],
    "word_cap": 20,
    "silence_markers": ["[SILENT]"],
}

FIXED = "# Stripe Priorities\n**Focus:** fixed\n## Priorities\n- Correct the plan"
RERUN = "# Stripe Priorities\n**Focus:** rerun\n## Priorities\n- New provider evidence"
INVALID = "# Wrong\n## Unknown\n- Invalid correction"

GOOD = """# Stripe Priorities
**Focus:** billing
## Priorities
- Ship it
## Upcoming
- Test it
"""


def test_contract_pass_and_rejects_order_unknown_and_cap():
    assert check_contract(GOOD, CONTRACT)["passed"]
    bad = "# Wrong\n## Unknown\n" + ("word " * 25)
    result = check_contract(bad, CONTRACT)
    assert not result["passed"]
    assert len(result["violations"]) >= 3


@pytest.mark.parametrize(
    "text, passed",
    [
        (GOOD + "# Internal notes\n- hidden\n", False),
        (GOOD + "```text\n# Quoted heading\n```\n", True),
    ],
)
def test_contract_rejects_a_second_top_level_heading(text, passed):
    assert check_contract(text, CONTRACT)["passed"] is passed


def test_silence_marker_is_a_contract_pass():
    result = check_contract("[SILENT]", CONTRACT)
    assert result["passed"] and result["silent"]


@pytest.mark.parametrize(
    "text, passed",
    [
        ("# Stripe Priorities\n**Focus:** billing\n```text\n## Priorities\n```", False),
        ("# Stripe Priorities\n```text\n**Focus:** billing\n```\n## Priorities", False),
        (
            "# Stripe Priorities\n**Focus:** billing\n## Priorities\n```text\n## Unknown\n```",
            True,
        ),
    ],
)
def test_contract_checks_rendered_lines_outside_code_fences(text, passed):
    assert check_contract(text, CONTRACT)["passed"] is passed


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("PRESENT", ("PRESENT", "")),
        ("FIX\n# Stripe Priorities\nfixed", ("FIX", "# Stripe Priorities\nfixed")),
        ("RERUN\nprovider timeout", ("RERUN", "provider timeout")),
    ],
)
def test_parse_decision(reply, expected):
    assert parse_decision(reply) == expected


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "Decision: PRESENT",
        "PRESENT:",
        "PRESENT\nextra",
        "analysis\nPRESENT",
        "analysis\nFIX\nreport",
        "PRESENT\nFIX\nreport",
        "```\nPRESENT\n```",
        "- PRESENT",
        "FIX",
        "RERUN",
        "RERUN\ntwo\nlines",
        "I think PRESENT is correct",
    ],
)
def test_parse_decision_rejects_wrapped_or_unparseable(reply):
    with pytest.raises(ValueError):
        parse_decision(reply)


def test_review_prompt_requires_bare_protocol_output():
    prompt = _review_wake_text("report", CONTRACT)
    assert "REPORT_BEGIN" in prompt and "REPORT_END" in prompt
    assert "CONTRACT_BEGIN" in prompt and "CONTRACT_END" in prompt
    assert (
        "no explanation, label, Markdown fence, bullets, or quotation marks" in prompt
    )
    assert "Do not return a decision plus extra text." in prompt


@pytest.fixture
def delivery(monkeypatch):
    import cron.scheduler_review as sr

    sent, receipts, incidents, reruns = [], [], [], []
    subject = {
        "chat_id": "chat",
        "thread_id": "139328",
        "user_id": "u",
        "session_id": "sid",
        "session_key": "key",
        "profile_name": "default",
    }
    monkeypatch.setattr(sr, "_resolve_review_subject", lambda review: subject)
    monkeypatch.setattr("cron.review.load_contract", lambda ref: CONTRACT)
    monkeypatch.setattr("cron.review.append_receipt", receipts.append)
    monkeypatch.setattr(
        "cron.incidents.upsert_incident", lambda *a, **kw: incidents.append(a)
    )

    def send(job, text, **kwargs):
        assert job["deliver"] == "telegram:chat:139328"
        assert job["_review_mirror_session"] == "sid"
        assert job["attach_to_session"] is True
        assert "review" not in job
        job["_last_delivery_message_ids"] = ["message-1"]
        sent.append(text)

    monkeypatch.setattr("cron.scheduler_delivery._deliver_result", send)
    return sr, sent, receipts, incidents, reruns


def test_malformed_review_gets_one_persisted_retry_before_fallback(
    delivery, monkeypatch
):
    sr, sent, receipts, incidents, _ = delivery
    replies = iter(["Decision: PRESENT", "PRESENT"])
    calls = []
    job = {
        "id": "j",
        "deliver": "local",
        "_review_run_id": "run",
        "review": {"contract": "x", "subject": "Stripe Priorities", "max_reruns": 1},
    }

    def hop(*args):
        calls.append(True)
        if len(calls) == 2:
            assert job["_review_parse_retry_attempted"] is True
        return next(replies)

    monkeypatch.setattr(sr, "_run_review_hop", hop)
    assert sr._deliver_reviewed_result(job, GOOD, {}, None) is None
    assert len(calls) == 2
    assert sent == [GOOD]
    assert receipts[0]["decision"] == "PRESENT"
    assert receipts[0]["review_parse_retry"] is True
    assert not incidents


@pytest.mark.parametrize(
    "reply, rerun, maximum, expected, decision",
    [
        ("PRESENT", None, 1, GOOD, "PRESENT"),
        ("FIX\n" + FIXED, None, 1, FIXED, "FIX"),
        ("FIX\n" + INVALID, None, 1, GOOD, "FIX_REJECTED"),
        ("RERUN\nretry", (True, "doc", RERUN, None), 1, RERUN, "RERUN"),
        ("RERUN\nretry", (True, "doc", INVALID, None), 1, GOOD, "RERUN_REJECTED"),
        ("RERUN\nretry", (False, "doc", "", "failed"), 1, GOOD, "RERUN_REJECTED"),
        ("RERUN\nretry", None, 0, GOOD, "RERUN_UNAVAILABLE"),
        ("unparseable", None, 1, GOOD, "TIMEOUT_OR_ERROR"),
        (TimeoutError("late"), None, 1, GOOD, "TIMEOUT_OR_ERROR"),
        (RuntimeError("subject busy"), None, 1, GOOD, "TIMEOUT_OR_ERROR"),
    ],
)
def test_review_decisions_send_only_valid_candidates(
    delivery, monkeypatch, reply, rerun, maximum, expected, decision
):
    sr, sent, receipts, incidents, reruns = delivery

    def hop(*args):
        if isinstance(reply, Exception):
            raise reply
        return reply

    def run(job, seconds):
        reruns.append(seconds)
        assert 1190 < seconds <= 1200
        assert rerun is not None
        return rerun

    monkeypatch.setattr(sr, "_run_review_hop", hop)
    monkeypatch.setattr(sr, "_bounded_review_rerun", run)
    job = {
        "id": "j",
        "deliver": "local",
        "_review_run_id": "run",
        "review": {
            "contract": "x",
            "subject": "Stripe Priorities",
            "max_reruns": maximum,
        },
    }
    assert sr._deliver_reviewed_result(job, GOOD, {}, None) is None
    assert sent == [expected]
    assert receipts[0]["decision"] == decision
    assert receipts[0]["text_sent"] == expected
    assert receipts[0]["message_id"] == "message-1"
    assert receipts[0]["run_id"] == "run"
    assert bool(reruns) == (rerun is not None)
    assert not incidents


@pytest.mark.parametrize(
    "kind", ["contract_error", "unresolved", "silent", "invalid_original"]
)
def test_review_fallbacks_and_silence(delivery, monkeypatch, kind):
    sr, sent, receipts, incidents, _ = delivery

    def fail(*args):
        raise ValueError(kind)

    monkeypatch.setattr(sr, "_run_review_hop", lambda *args: "PRESENT")
    if kind == "contract_error":
        monkeypatch.setattr("cron.review.load_contract", fail)
        monkeypatch.setattr(
            sr,
            "_run_review_hop",
            lambda *a: pytest.fail("contract error must skip review"),
        )
    elif kind in ("unresolved", "silent"):
        monkeypatch.setattr(sr, "_resolve_review_subject", fail)
    content = (
        "[SILENT]"
        if kind == "silent"
        else INVALID
        if kind == "invalid_original"
        else GOOD
    )
    job = {
        "id": "j",
        "deliver": "local",
        "review": {"contract": "x", "subject": "subject"},
    }
    error = sr._deliver_reviewed_result(job, content, {}, None)
    expected_decision = {
        "contract_error": "CONTRACT_ERROR",
        "unresolved": "UNRESOLVED",
        "silent": "SILENT",
        "invalid_original": "PRESENT",
    }[kind]
    assert receipts[0]["decision"] == expected_decision
    assert sent == ([] if kind in ("silent", "unresolved") else [content])
    assert bool(incidents) == (kind == "unresolved")
    assert error == ("unresolved" if kind == "unresolved" else None)
    if kind == "invalid_original":
        assert receipts[0]["candidates"][0]["passed"] is False


def test_receipt_is_persisted_under_profile_cron(tmp_path, monkeypatch):
    import json
    from cron.review import append_receipt, receipt_path

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    append_receipt({"job_id": "j", "decision": "PRESENT"})
    assert receipt_path() == tmp_path / "cron" / "review-receipts.jsonl"
    assert json.loads(receipt_path().read_text())["decision"] == "PRESENT"


def test_receipts_rotate_and_keep_only_one_complete_backup(tmp_path, monkeypatch):
    import json
    from cron import review

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(review, "MAX_RECEIPT_BYTES", 200)
    for index in range(4):
        review.append_receipt({"job_id": str(index), "text_sent": "report " * 10})
    path = review.receipt_path()
    assert json.loads(path.read_text())["job_id"] == "3"
    assert json.loads(path.with_name(path.name + ".1").read_text())["job_id"] == "2"
    assert not path.with_name(path.name + ".2").exists()
    assert path.stat().st_size <= 200
    assert path.with_name(path.name + ".1").stat().st_size <= 200


@pytest.mark.parametrize("changed", [False, True])
def test_subject_must_still_be_live_and_same_at_final_send(
    delivery, monkeypatch, changed
):
    sr, sent, receipts, incidents, _ = delivery
    subject = {
        "chat_id": "chat",
        "thread_id": "139328",
        "user_id": "u",
        "session_id": "sid",
        "session_key": "key",
    }
    calls = []

    def resolve(review):
        calls.append(True)
        if len(calls) == 1:
            return subject
        if changed:
            return {**subject, "session_id": "replacement"}
        raise ValueError("subject ended during review")

    monkeypatch.setattr(sr, "_resolve_review_subject", resolve)
    monkeypatch.setattr(sr, "_run_review_hop", lambda *a: "PRESENT")
    error = sr._deliver_reviewed_result(
        {
            "id": "j",
            "deliver": "local",
            "review": {"subject": "topic", "contract": "x"},
        },
        GOOD,
        {},
        None,
    )
    assert error
    assert len(calls) == 2
    assert sent == []
    assert len(incidents) == 1
    assert receipts[0]["decision"] == "UNRESOLVED"
    assert receipts[0]["text_sent"] is None


@pytest.mark.parametrize(
    "report, passed",
    [
        ("# Stripe Daily — October 14, 2026\n", True),
        ("# Stripe Daily — Wednesday, October 14, 2026\n", True),
        ("# Stripe Daily — October 14, 2026 at 5:15 AM\n", True),
        ("# Stripe Daily — Wednesday, October 14, 2026 at 5:15 AM\n", True),
        ("# Stripe Daily — Friday, October 16\n", True),
        ("# Stripe Daily — Friday, October 16 at 5:15 AM\n", True),
        ("# Stripe Daily\n", True),
        ("# Daily Brief — October 14, 2026\n", False),
        ("# Stripe Daily (draft)\n", False),
        ("# Stripe DailyXYZ\n", False),
        (
            "Sure, here is the corrected brief:\n\n# Stripe Daily — October 14, 2026\n",
            False,
        ),
    ],
)
def test_shipped_stripe_contract_accepts_only_the_dated_title_first(report, passed):
    from pathlib import Path

    import cron
    from cron.review import load_contract

    shipped = (
        Path(cron.__file__).resolve().parent
        / "contracts"
        / "reports"
        / "stripe-brief.yaml"
    )
    report += "\n**Status:** fixture status\n\n## Needs You\n**Move:** fixture move\n1. Fixture task\n\n**Running:** unknown\n"
    assert check_contract(report, load_contract(str(shipped)))["passed"] is passed


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text.replace("**Status:** fixture", "fixture"),
        lambda text: text.replace("**Move:** fixture", "fixture"),
        lambda text: text.replace("**Running:** unknown", "unknown"),
        lambda text: text.replace("## Needs You", "## Priorities"),
        lambda text: text + "\n## Unexpected\nfixture\n",
        lambda text: text + "\n" + "fixture " * 401,
    ],
)
def test_shipped_stripe_daily_contract_rejects_missing_envelope(change):
    from pathlib import Path

    import cron
    from cron.review import load_contract

    contract = load_contract(str(Path(cron.__file__).resolve().parent / "contracts/reports/stripe-brief.yaml"))
    text = "# Stripe Daily — October 14, 2026\n\n**Status:** fixture\n\n## Needs You\n**Move:** fixture\n1. Fixture task\n\n**Running:** unknown\n"
    assert check_contract(text, contract)["passed"]
    assert not check_contract(change(text), contract)["passed"]

"""An interrupted automation's notice names the automation and is not sent as a duplicate.

On 2026-11-14 a restart settled 8 automation turns interrupted and every topic got
"♻️ Recovered reply — this may be a duplicate: An automated delivery was interrupted…", naming
nothing: Brian could not tell which goal, process or relay had stopped. The admission rows are
shaped like the live ones (request identity + prompt text).
"""

import json

import pytest

from agent.turn_failure_copy import AUTOMATION_INTERRUPTED_NOTICE, FAILED_TURN_DISPLAY_KIND
from gateway.delivery_ledger import sweep_recoverable
from gateway.session_unattended_unknown import automation_name, interrupted_notice
from tests.gateway.test_active_turn_recovery import _close_store_db, _db_runner, _turn


def _row(request_id, text="", principal="automation:agent:main:telegram:dm:1:2", **payload):
    rid = request_id if isinstance(request_id, str) else json.dumps(request_id)
    return {"principal_id": principal, "request_id": rid,
            "payload": {"text": text, "native_text_v1": {"automation": {"identity": rid}}, **payload}}


@pytest.mark.parametrize("row,name", [
    (_row(["goal-wait", "s1", 1.0, 2.0], "[Continuing toward your standing goal] Goal: Ship the fork sync.\n"
          "Plan: x"), 'the goal continuation for "Ship the fork sync."'),
    (_row(["loop-tick", "route", "abc"], "[/loop wakeup #33, every 2h]\nRecurring task: Land PR 2257\n"),
     'the /loop wakeup for "Land PR 2257"'),
    (_row(["completion", "proc_4ee3984249e6", 1.0], "[IMPORTANT: Background process done.\nCommand: gh run view 1\n"),
     'the completion notice for background process proc_4ee3984249e6 ("gh run view 1")'),
    (_row(["async_delegation", "deleg_0787c44a", ""]), "the result of delegation deleg_0787c44a"),
    (_row(["delegation_retry", "deleg_f0e87322", "retry:1"]), "the retry notice of delegation deleg_f0e87322"),
    (_row(["plugin", "session-relay", "route", "id"]), "a relayed inter-session note"),
    (_row("opaque"), None),
], ids=["goal", "loop", "process", "delegation", "delegation-retry", "relay", "unknown"])
def test_automation_name_comes_from_the_admission(row, name):
    assert automation_name(row) == name


def test_cron_notice_names_the_job(monkeypatch):
    monkeypatch.setattr("cron.jobs.get_job", lambda jid: {"id": jid, "name": "decide-personal-inbox"})
    row = _row("cron:a97d55d8eb78:ce89", principal="cron-owner")
    assert interrupted_notice(row) == (
        'Cron job "decide-personal-inbox" was interrupted and was not run again. Some actions may '
        "already have run; verify their effects.")


def test_unnamed_automation_keeps_the_generic_notice():
    assert interrupted_notice(_row("opaque")) == AUTOMATION_INTERRUPTED_NOTICE


@pytest.mark.asyncio
async def test_crash_left_interruption_notice_is_redelivered_without_the_duplicate_marker(tmp_path):
    """A Hermes-authored boundary is never streamed, so it cannot already be on screen."""
    runner, store = _db_runner(tmp_path)
    source = _turn(store, "notice", marked=True, reply=None)
    entry = store.get_or_create_session(source)
    store.append_to_transcript(entry.session_id, {
        "role": "assistant", "content": interrupted_notice(_row(["async_delegation", "deleg_1", ""])),
        "display_kind": FAILED_TURN_DISPLAY_KIND})
    _turn(store, "reply", marked=True, reply="a streamed answer")

    assert await runner._recover_unclean_sessions() == (0, 2)

    marks = {r["content"]: r["needs_marker"] for r in sweep_recoverable(deliverable_platforms={"discord"})}
    assert marks == {
        "The result of delegation deleg_1 was interrupted and was not run again. Some actions may "
        "already have run; verify their effects.": False,
        "a streamed answer": True,
    }
    _close_store_db(store)

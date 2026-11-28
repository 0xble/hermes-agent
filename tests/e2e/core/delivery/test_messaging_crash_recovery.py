"""Messaging delivery recovery after SIGKILL at each in-flight reply boundary."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.e2e.core.delivery._fake_platform import (
    GatewayProcess,
    footer,
    header,
    norm,
    persisted_answers,
    persisted_user_rows,
    visible_copies,
    wait_until,
)
from tests.e2e.core.delivery._messaging_helpers import (
    EXTRA_CONFIG,
    PLATFORMS,
    STREAMING,
    Director,
    director as director,
    dump,
    ledger_quiescent,
    llm as llm,
)
from tests.fakes.fake_llm_provider import StallMidStream

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL + process-group restart harness")


# crash point -> (fault op, fault kind)
CRASH_POINTS = {
    # the turn returned (persisted), the final text not yet handed to the delivery ledger
    "completed_not_ledgered": ("pre_ledger", "hold"),
    # the final send/edit is in flight and the platform never saw it
    "send_in_flight": ("any", "hold"),
    # the turn is persisted and the platform accepted the final send/edit; the ack died with the process
    "sent_ack_lost": ("any", "hold_after"),
    # streaming: the platform accepted a preview edit that already shows the complete answer, while
    # the provider stream (so the turn) is still open -> nothing persisted when the process dies
    "stream_accepted_unpersisted": ("any", "hold_after"),
    # Same crash after a real preview EDIT: the initial send ACK supplied its ID.
    "stream_accepted_edit_unpersisted": ("edit", "hold_after"),
}

# completed_not_ledgered: the turn is persisted and the handler returned, but the final text never
# reached the delivery ledger; recovery must hand the persisted reply to the ledger rather than
# re-run the turn. Either way exactly one complete reply must show.

def _replies(gw: GatewayProcess, platform: str, chat: str, token: str, aid: str):
    msgs = gw.platform_view().visible(platform, chat)
    originals = visible_copies(msgs, aid)
    resumes = [c for n in range(1, 6) for c in visible_copies(msgs, f"R-{token}-{n}")]
    return originals, resumes


CRASH_MATRIX = [("completed_not_ledgered", "fk_ne"),  # streaming finals are sent before the handler returns
                ("send_in_flight", "fk_ne"), ("send_in_flight", "fk_tg"),
                ("sent_ack_lost", "fk_ne"), ("sent_ack_lost", "fk_tg"),
                ("stream_accepted_unpersisted", "fk_tg"),
                ("stream_accepted_edit_unpersisted", "fk_tg")]


STREAM_CONTINUE_POINTS = {"stream_accepted_unpersisted", "stream_accepted_edit_unpersisted"}


@pytest.fixture
def fresh_gw(request, tmp_path, llm):
    """A gateway on its OWN home: one unclean restart per home keeps the restart-loop breaker and
    the 120 s recency fallback of other scenarios out of the picture."""
    keep = os.environ.get("C12_SPOOL_ROOT")
    root = Path(keep) / f"crash-{tmp_path.name}" if keep else tmp_path / "gw"
    extra = EXTRA_CONFIG
    if request.node.callspec.params.get("point") in STREAM_CONTINUE_POINTS:
        # Preview adoption and recovery marking are a `continue`-policy contract; under the
        # default `ask` the resume turn only asks, and the partial preview stays untouched.
        extra += "restart_resume_policy: continue\n"
    proc = GatewayProcess(root, platforms=PLATFORMS, llm_base_url=llm.base_url, extra_config=extra)
    proc.start()
    yield proc
    proc.stop()


@pytest.mark.parametrize("point,platform", CRASH_MATRIX)
def test_crash_between_completion_and_send(fresh_gw, director, point, platform):
    """kill -9 the gateway at a point between provider completion and the platform ack, restart it
    on the same HERMES_HOME. Recovery may be the delivery ledger (redeliver the held answer) or,
    for a turn that never finished, auto-resume (a fresh model turn): either way the inbound ends
    with exactly ONE complete visible reply that matches the transcript, and any second copy the
    platform may hold is marked as a possible duplicate."""
    gw = fresh_gw
    op, kind = CRASH_POINTS[point]
    streaming = STREAMING[platform]
    token = f"{platform}.crash-{point}"
    chat = f"k-{platform}-{point}"
    aid = f"A-{token}"
    answer = Director.answer(aid, "answer computed before the crash.")
    if point in {"stream_accepted_unpersisted", "stream_accepted_edit_unpersisted"}:
        # The legacy case emits all text at once and holds the FIRST SEND, not an
        # edit. The edit case first gets an acknowledged partial preview ID.
        answer = StallMidStream(text=answer.text, after_chars=len(answer.text), seconds=60,
                                prefix_chars=32 if point == "stream_accepted_edit_unpersisted" else 0,
                                prefix_delay=0.5 if point == "stream_accepted_edit_unpersisted" else 0)
    director.script(token, answer)
    gw.fault(platform=platform, op=op, kind=kind, contains=footer(aid), chat_id=chat)
    gw.inject(platform, f"[in:{token}] question", message_id=f"in-{token}", chat_id=chat)
    wait_until(lambda: any(footer(aid) in h["content"] for h in gw.holds()), f"{token} to reach the crash point",
               proc=gw.proc, log=gw.log)
    preview_ids = [m.message_id for m in gw.platform_view().visible(platform, chat)
                   if header(aid) in m.text] if point == "stream_accepted_edit_unpersisted" else []
    if point == "sent_ack_lost":  # the stream's footer edit can land before the turn is persisted
        wait_until(lambda: persisted_answers(gw.db_path, aid), f"{token} answer persisted before the kill",
                   proc=gw.proc, log=gw.log)
    gw.kill9()
    gw.start()

    def replied() -> bool:
        originals, resumes = _replies(gw, platform, chat, token, aid)
        return any(c.complete for c in originals + resumes)

    wait_until(replied, f"{token} catch-up reply after restart\n" + dump(gw, platform, chat),
               proc=gw.proc, log=gw.log)
    gw.wait_idle([chat], f"{token} to settle after restart")
    wait_until(lambda: ledger_quiescent(gw.db_path, chat), f"{token} ledger to settle", proc=gw.proc, log=gw.log)
    _assert_single_final_reply(gw, director, platform, chat, token, aid, point, streaming,
                               preview_ids=preview_ids)


def _assert_single_final_reply(gw, director, platform, chat, token, aid, point, streaming,
                               *, preview_ids=None) -> None:
    ctx = dump(gw, platform, chat)
    originals, resumes = _replies(gw, platform, chat, token, aid)
    complete = [c for c in originals + resumes if c.complete]
    unmarked = [c for c in complete if not c.marked]
    if point == "stream_accepted_edit_unpersisted":
        # The acknowledged preview ID is the durable edit target across the
        # restart. The resumed complete copy must finish that message in place;
        # an old incomplete preview must not remain beside it.
        assert preview_ids, f"no acknowledged preview captured before crash\n{ctx}"
        assert len(complete) == 1, f"expected one resumed complete copy\n{ctx}"
        assert complete[0].message_ids == preview_ids, (
            f"resumed completion changed platform message ownership: "
            f"preview={preview_ids}, complete={complete[0].message_ids}\n{ctx}")
        assert not [c for c in originals + resumes if not c.complete], ctx
        return
    if point == "stream_accepted_unpersisted":
        # The first send's ACK died with the gateway, so its ID cannot be edited.
        # The docstring's honest at-least-once contract governs this one cell:
        # the unfinished model turn legitimately runs twice, never a fabricated completion.
        assert len(unmarked) <= 1, f"{len(unmarked)} unmarked complete replies after catch-up\n{ctx}"
        assert len(resumes) == 1 and resumes[0].complete and resumes[0].marked, ctx
        assert not [c for c in originals if c.marked], ctx
        final_aid = f"R-{token}-{director.resumes.get(token, 0)}"
        persisted = persisted_answers(gw.db_path, final_aid)
        # visible_copies strips the marker before constructing Copy.text.
        assert len(persisted) == 1 and norm(persisted[0]) == resumes[0].text, (
            f"visible final reply is not the (single) persisted assistant text: persisted={persisted}\n{ctx}")
        # Director versions may count resume calls separately; only this cell
        # permits two model turns for one inbound (one original, one recovery).
        assert director.turns.get(token) in (1, 2), ctx
        assert director.resumes.get(token) == 1, ctx
        assert len(persisted_user_rows(gw.db_path, f"[in:{token}]")) == 1, ctx
        return
    assert len(unmarked) <= 1, f"{len(unmarked)} unmarked complete replies after catch-up\n{ctx}"
    assert len(complete) - len(unmarked) <= (1 if point == "sent_ack_lost" else 0) or not unmarked, (
        f"unexpected extra marked copies\n{ctx}")
    # A crash mid-stream may leave the interrupted preview on screen; nothing else may be partial.
    assert not [c for c in resumes if not c.complete], ctx
    if not streaming:
        assert not [c for c in originals if not c.complete], ctx
    assert not (resumes and [c for c in originals if c.complete]), (
        f"both the held answer and an auto-resumed re-answer are visible\n{ctx}")
    final = resumes[0] if resumes else next(c for c in originals if c.complete)
    final_aid = aid if not resumes else f"R-{token}-{director.resumes.get(token, 0)}"
    persisted = persisted_answers(gw.db_path, final_aid)
    assert len(persisted) == 1 and norm(persisted[0]) == final.text, (
        f"visible final reply is not the (single) persisted assistant text: persisted={persisted}\n{ctx}")
    assert director.turns.get(token) == 1, ctx
    assert len(persisted_user_rows(gw.db_path, f"[in:{token}]")) == 1, ctx

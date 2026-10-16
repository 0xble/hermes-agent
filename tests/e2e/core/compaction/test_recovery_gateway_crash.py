"""Same-topic recovery across SIGKILL after compaction and before final delivery.

The gateway, agent, tools, compaction, queue spool and database are real. Only
the model and platform wire use the existing isolated recording harness.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from agent.model_metadata import estimate_request_tokens_rough
from hermes_cli.goals import GoalState, _meta_key
from hermes_state import SessionDB
from tests.e2e.core.compaction._helpers import REFUSAL_TEXT
from tests.e2e.core.delivery._fake_platform import GatewayProcess, wait_until
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall

pytestmark = pytest.mark.platforms("posix")


def _rows(path, session_id):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT id,role,content,tool_calls,tool_call_id,message_uid,active,compacted "
            "FROM messages WHERE session_id=? ORDER BY id",
            (session_id,),
        )]


def test_committed_recovery_crash_keeps_queue_identity_settings_and_effects(tmp_path):
    effect_file = tmp_path / "effect-count.txt"
    effect_receipt = "COMPLETED-EFFECT-17"
    effect_command = (
        f"printf 'effect-17\\n' >> {shlex.quote(str(effect_file))}; "
        f"printf '{effect_receipt}\\n'"
    )
    trigger = "RECOVERY-TRIGGER-91"
    followup = "QUEUED-INPUT-92"
    final = "Recovered context and completed effect retained. FINAL-91"
    queued_final = "Queued input retained. FINAL-92"
    main_requests = []

    def model(record):
        messages = record["body"].get("messages", [])
        main_requests.append(record["body"])
        users = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        if followup in users:
            return Text(queued_final)
        # If compaction or restart loses the completed tool receipt, this model
        # repeats the effect. The durable count below catches that unsafe replay.
        completed = any(effect_receipt in str(m.get("content", "")) for m in messages)
        if not completed:
            return ToolCall("terminal", {"command": effect_command, "timeout": 10})
        return Text(final if trigger in users else "SEED-COMPLETE")

    server = FakeLLMServer(model, aux=lambda _record: Text(REFUSAL_TEXT))
    server.start()
    gateway = GatewayProcess(
        tmp_path / "gateway", platforms={"fk_tg": "discord"}, llm_base_url=server.base_url,
        extra_config=(
            "compression:\n  threshold_tokens: 110000\n  protect_last_n: 4\n"
            "auxiliary:\n  compression:\n    timeout: 2\n  title_generation:\n    enabled: false\n"
            "display:\n  platforms:\n    fk_tg:\n      streaming: false\n"
        ),
    )
    chat, thread = "recovery-topic", "91"
    stopped_id = "stopped-control"
    try:
        gateway.start()
        gateway.inject("fk_tg", "Perform effect-17 once, then retain its receipt.", "seed", chat, thread)
        gateway.wait_idle([chat], "seed effect and reply")
        assert effect_file.read_text() == "effect-17\n"
        gateway.stop()

        db = SessionDB(db_path=gateway.db_path)
        routing_scope = str((gateway.hermes_home / "sessions").resolve())
        entries = db.load_gateway_routing_entries(scope=routing_scope)
        key, entry = next((key, json.loads(raw)) for key, raw in entries.items()
                          if json.loads(raw).get("origin", {}).get("chat_id") == chat)
        session_id = entry["session_id"]
        db.set_session_title(session_id, "Manual Recovery Topic")
        db.set_session_title_source(session_id, "user")
        db.record_telegram_topic_icon_state(chat, thread, custom_emoji_id="5350554349074391003",
                                            emoji="💻", owner="manual")
        tool_id = "retained-observation-91"
        db.append_message(session_id, "assistant", "Completed read-only observation.", tool_calls=[{
            "id": tool_id, "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"retained-artifact"}',
            },
        }])
        giant = "ARCHIVE-SENTINEL-91 " + "neutral datum alpha beta gamma delta.\n" * 50000
        db.append_message(session_id, "tool", giant, tool_call_id=tool_id)
        originals = _rows(gateway.db_path, session_id)
        original_hashes = {row["id"]: hashlib.sha256((row["content"] or "").encode()).hexdigest()
                           for row in originals}
        goal = GoalState(goal="Finish the approved inspection without repeating effect-17.",
                         status="paused", turns_used=3, max_turns=7, paused_reason="user-paused")
        goal_json = goal.to_json()
        db.set_meta(_meta_key(session_id), goal_json)
        entry["model_override"] = {"model": "fake-model", "provider": "custom", "base_url": server.base_url}
        entry["reasoning_override"] = {"enabled": True, "effort": "high"}
        db.save_gateway_routing_entry(key, json.dumps(entry), scope=routing_scope)
        icon_before = db.get_telegram_topic_icon_state(chat, thread)

        # A separately stopped lane must stay stopped even with a fresh resume
        # marker. Recovery of the active topic cannot reactivate other work.
        db.create_session(stopped_id, "fk_tg", model="fake-model")
        stopped_key = key.replace(chat, "stopped-topic")
        stopped = json.loads(json.dumps(entry))
        stopped.update(session_key=stopped_key, session_id=stopped_id, suspended=True,
                       resume_pending=True, resume_reason="restart_interrupted",
                       last_resume_marked_at=datetime.now(timezone.utc).isoformat())
        stopped["origin"]["chat_id"] = "stopped-topic"
        db.save_gateway_routing_entry(stopped_key, json.dumps(stopped), scope=routing_scope)
        stopped_goal = GoalState(goal="Stopped work must not run.", status="paused", turns_used=5,
                                 max_turns=8, paused_reason="user-paused").to_json()
        db.set_meta(_meta_key(stopped_id), stopped_goal)
        db.close()

        gateway.start()
        gateway.fault(platform="fk_tg", op="send", kind="hold", contains="FINAL-91", chat_id=chat)
        gateway.inject("fk_tg", f"{trigger} Inspect only. Keep all accepted instructions and pending work.",
                       "trigger-91", chat, thread)

        def recovery_reached_delivery():
            if any("FINAL-91" in row["content"] for row in gateway.holds()):
                return True
            if gateway.idle([chat]):
                visible = [row.text for row in gateway.platform_view().visible("fk_tg", chat)]
                raise AssertionError(f"Recovery ended before final delivery: {visible}")
            return False

        wait_until(recovery_reached_delivery,
                   "committed recovery before final platform send", proc=gateway.proc, log=gateway.log)
        committed = _rows(gateway.db_path, session_id)
        assert any(row["compacted"] and "ARCHIVE-SENTINEL-91" in (row["content"] or "")
                   for row in committed), "hard-window recovery did not archive the giant observation"
        assert any(row["role"] == "assistant" and row["content"] == final for row in committed)
        gateway.inject("fk_tg", f"{followup} Continue the read-only inspection.", "queued-92", chat, thread)
        wait_until(lambda: any(followup in path.read_text() for path in
                              (gateway.hermes_home / "pending_messages").glob("*.json")),
                   "queued input durably acknowledged", proc=gateway.proc, log=gateway.log)
        gateway.kill9()
        gateway.start()
        gateway.wait_idle([chat], "same-topic recovery and queued follow-up after SIGKILL")
        wait_until(lambda: any(queued_final in row.text for row in
                              gateway.platform_view().visible("fk_tg", chat)),
                   "queued reply visible after restart", proc=gateway.proc, log=gateway.log)
        visible = gateway.platform_view().visible("fk_tg", chat)
        for answer in (final, queued_final):
            copies = [row for row in visible if row.text.endswith(answer)]
            assert len(copies) == 1, [(row.text, row.thread_id) for row in visible]
            assert copies[0].thread_id == thread

        db = SessionDB(db_path=gateway.db_path)
        after = _rows(gateway.db_path, session_id)
        by_id = {row["id"]: row for row in after}
        assert all(hashlib.sha256((by_id[row_id]["content"] or "").encode()).hexdigest() == digest
                   for row_id, digest in original_hashes.items())
        assert all((by_id[row["id"]]["tool_calls"], by_id[row["id"]]["tool_call_id"])
                   == (row["tool_calls"], row["tool_call_id"]) for row in originals)
        from tools.session_search_tool import session_search

        original_tool = next(row for row in originals if row["tool_call_id"] == tool_id)
        recalled = json.loads(session_search(db=db, session_id=session_id,
                                             around_message_id=original_tool["id"], window=1,
                                             current_session_id=session_id))
        archived_tool = next(row for row in recalled["messages"] if row["id"] == original_tool["id"])
        assert hashlib.sha256(archived_tool["content"].encode()).hexdigest() == original_hashes[original_tool["id"]]
        for marker in (trigger, followup):
            occurrences = [row for row in after if row["role"] == "user" and marker in (row["content"] or "")]
            # In-place compaction retains archived originals and a live revision.
            # Their shared occurrence ID distinguishes those from replayed input.
            assert len({row["message_uid"] for row in occurrences}) == 1, occurrences
            assert sum(row["active"] for row in occurrences) == 1, occurrences
        assert effect_file.read_text() == "effect-17\n", "completed effect was replayed"
        resumed_entry = json.loads(db.load_gateway_routing_entries(scope=routing_scope)[key])
        assert resumed_entry["session_id"] == session_id
        assert resumed_entry["origin"]["chat_id"] == chat and str(resumed_entry["origin"]["thread_id"]) == thread
        assert resumed_entry["model_override"] == entry["model_override"]
        assert resumed_entry["reasoning_override"] == entry["reasoning_override"]
        assert db.get_session_title(session_id) == "Manual Recovery Topic"
        assert db.get_session_title_source(session_id) == "user"
        assert db.get_telegram_topic_icon_state(chat, thread) == icon_before
        assert db.get_meta(_meta_key(session_id)) == goal_json
        assert db.get_meta(_meta_key(stopped_id)) == stopped_goal
        assert json.loads(db.load_gateway_routing_entries(scope=routing_scope)[stopped_key])["suspended"] is True
        assert not db.get_messages(stopped_id)
        assert main_requests
        for request in main_requests:
            wire_input = estimate_request_tokens_rough(request["messages"], tools=request.get("tools"))
            output_cap = request.get("max_tokens", request.get("max_completion_tokens", 0)) or 0
            assert wire_input + output_cap + 6400 < 128000
        db.close()
    finally:
        gateway.stop()
        server.stop()


def test_failed_queued_checkpoint_refresh_remains_resumable_after_restart(tmp_path):
    entered, release = threading.Event(), threading.Event()
    queued_calls = []

    def model(record):
        users = "\n".join(str(row.get("content", "")) for row in record["body"].get("messages", [])
                          if row.get("role") == "user")
        if "QUEUED-REFRESH-93" in users:
            queued_calls.append(record)
            return Text("QUEUED-REFRESH-FINAL-93")
        entered.set()
        assert release.wait(90), "seed request did not release"
        return Text("FIRST-REFRESH-FINAL-93")

    server = FakeLLMServer(model)
    server.start()
    gateway = GatewayProcess(tmp_path / "gateway", platforms={"fk_tg": "discord"},
                             llm_base_url=server.base_url,
                             extra_config="display:\n  platforms:\n    fk_tg:\n      streaming: false\n")
    chat, thread = "queue-refresh-topic", "93"
    pending_dir = gateway.hermes_home / "pending_messages"
    held_dir = pending_dir.with_name("pending_messages.held")
    try:
        gateway.start()
        gateway.inject("fk_tg", "FIRST-REFRESH-93", "first-refresh-93", chat, thread)
        assert entered.wait(30)
        gateway.inject("fk_tg", "/queue QUEUED-REFRESH-93", "queued-refresh-93", chat, thread)
        wait_until(lambda: any("QUEUED-REFRESH-93" in path.read_text() for path in pending_dir.glob("*.json")),
                   "queued input admitted before refresh fault", timeout=30, proc=gateway.proc, log=gateway.log)
        # Keep the admitted input intact while making its publish location unavailable.
        # chmod is not a fault here: _get_flush_dir restores the owner's 0700 mode.
        pending_dir.rename(held_dir)
        pending_dir.write_text("temporarily unavailable")
        release.set()
        gateway.wait_idle([chat], "failed refresh settles without provider work")
        assert queued_calls == [], "provider ran without durable original-session binding"
        assert any("QUEUED-REFRESH-93" in path.read_text() for path in held_dir.glob("*.json"))
        pending_dir.unlink()
        held_dir.rename(pending_dir)
        gateway.stop()
        gateway.start()
        gateway.wait_idle([chat], "accepted queued input resumes after storage recovers")
        replies = gateway.platform_view().visible("fk_tg", chat)
        assert sum("QUEUED-REFRESH-FINAL-93" in reply.text for reply in replies) == 1, replies
        assert len(queued_calls) == 1
    finally:
        release.set()
        if held_dir.exists():
            pending_dir.unlink(missing_ok=True)
            held_dir.rename(pending_dir)
        gateway.stop()
        server.stop()

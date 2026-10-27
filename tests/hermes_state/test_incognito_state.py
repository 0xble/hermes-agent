"""Real SQLite privacy canaries. These also run with stdlib unittest."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent.incognito import incognito_scope, register_session
from hermes_state import SessionDB


CANARY = "PRIVATE_STATE_CANARY_7f34b86ad20c"


class IncognitoStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="incognito-state-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.db = SessionDB(self.home / "state.db")
        self.addCleanup(self.db.close)
        self.sid = "incognito-saved-prefix"
        self.db.create_session(self.sid, "cli", system_prompt="saved prompt")
        self.message_id = self.db.append_message(self.sid, "user", "saved prefix")
        self.db.set_session_title(self.sid, "saved title")
        self.addCleanup(register_session, self.sid, False)

    def assertNoCanaryOnDisk(self):
        # Inspect open WAL, before close/checkpoint can hide an accidental write.
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(CANARY.encode(), path.read_bytes(), str(path))

    def snapshot(self):
        return tuple(self.db._conn.iterdump())

    def mutations(self):
        db, sid = self.db, self.sid
        msg = {"role": "user", "content": CANARY}
        return [
            lambda: db.create_session(sid, "cli", model=CANARY, system_prompt=CANARY),
            lambda: db.create_session("incognito-child", "cli", parent_session_id=sid, system_prompt=CANARY),
            lambda: db.append_message(sid, "assistant", CANARY, reasoning=CANARY, api_content=CANARY),
            lambda: db.append_messages_batch(sid, [dict(msg)]),
            lambda: db.append_delegation_delivery(sid, CANARY, {"delegation_id": "private-delegation"}),
            lambda: db.replace_messages(sid, [dict(msg)]),
            lambda: db.update_system_prompt(sid, CANARY),
            lambda: db.update_session_tool_names(sid, [{"name": CANARY}]),
            lambda: db.set_session_title(sid, CANARY),
            lambda: db.touch_session_activity(sid, description=CANARY),
            lambda: db.update_session_meta(sid, json.dumps({"key": CANARY}), model=CANARY),
            lambda: db.update_session_model(sid, CANARY),
            lambda: db.set_message_api_content(sid, self.message_id, "saved prefix", CANARY),
            lambda: db.set_user_message_content(sid, self.message_id, CANARY),
            lambda: db.fail_handoff(sid, CANARY),
            lambda: db.save_gateway_routing_entry("private-key", json.dumps({"session_id": sid, "preview": CANARY})),
            lambda: db.replace_gateway_routing_entries({"private-key": json.dumps({"session_id": sid, "preview": CANARY})}),
            lambda: db.update_token_counts(sid, input_tokens=1, model=CANARY, billing_provider=CANARY),
            lambda: db.record_auxiliary_usage(sid, "title", model=CANARY, input_tokens=1),
            lambda: db.queue_token_counts(sid, input_tokens=1, model=CANARY),
            lambda: db.publish_compression_child(parent_session_id=sid, child_session_id="private-rotation",
                                                 source="cli", messages=[dict(msg)], system_prompt=CANARY),
            lambda: db.try_acquire_compression_lock(sid, CANARY),
            lambda: db.try_acquire_session_turn_lease(sid, CANARY),
            lambda: db.rewind_to_message(sid, self.message_id),
            lambda: db.end_session(sid, CANARY),
            lambda: db.delete_session(sid),
        ]

    def exercise_private_mutations(self, scope):
        before = self.snapshot()
        with scope:
            for mutation in self.mutations():
                with self.subTest(operation=mutation.__code__.co_firstlineno):
                    mutation()
                    self.assertEqual(before, self.snapshot())
                    self.assertNoCanaryOnDisk()
            self.assertEqual(self.db.get_messages(self.sid)[0]["content"], "saved prefix")
        self.assertNoCanaryOnDisk()
        self.db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertNoCanaryOnDisk()

    def test_turn_context_blocks_before_sqlite_and_preserves_prefix(self):
        self.exercise_private_mutations(incognito_scope())

    def test_registered_session_blocks_delayed_callbacks_without_context(self):
        register_session(self.sid)
        self.exercise_private_mutations(contextlib.nullcontext())

    def test_mixed_routing_preserves_private_prefix_and_saves_normal_rows(self):
        saved = json.dumps({"session_id": self.sid, "preview": "saved", "active_turn_token": "saved-token"})
        self.db.save_gateway_routing_entry("private-key", saved)
        register_session(self.sid)
        normal = json.dumps({"session_id": "normal", "preview": "normal preview"})
        self.db.replace_gateway_routing_entries({
            "private-key": json.dumps({"session_id": self.sid, "preview": CANARY}),
            "normal-key": normal,
        })
        self.assertEqual(self.db.load_gateway_routing_entry("private-key"), saved)
        self.assertEqual(self.db.load_gateway_routing_entry("normal-key"), normal)
        self.assertFalse(self.db.clear_gateway_routing_active_turn("private-key", "saved-token"))
        self.db.replace_gateway_routing_entries({"normal-key": normal})
        self.assertEqual(self.db.load_gateway_routing_entry("private-key"), saved)
        self.assertNoCanaryOnDisk()

    def test_flag_boundary_cannot_race_an_admitted_commit(self):
        import threading
        entered, release, attempted, enabled = (threading.Event() for _ in range(4))
        failures = []

        def write(conn):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test writer was not released")
            conn.execute("UPDATE sessions SET title = 'before on' WHERE id = ?", (self.sid,))

        def writer():
            try:
                self.db._execute_write(write, session_ids=(self.sid,))
            except Exception as exc:
                failures.append(exc)

        def turn_on():
            attempted.set()
            register_session(self.sid)
            enabled.set()

        worker = threading.Thread(target=writer)
        toggle = threading.Thread(target=turn_on)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            toggle.start()
            self.assertTrue(attempted.wait(5))
            self.assertFalse(enabled.wait(0.05))
        finally:
            release.set()
            worker.join(5)
            if toggle.ident is not None:
                toggle.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(toggle.is_alive())
        self.assertFalse(failures)
        self.assertTrue(enabled.is_set())
        self.db.set_session_title(self.sid, CANARY)
        self.assertEqual(self.db.get_session(self.sid)["title"], "before on")
        self.assertNoCanaryOnDisk()

    def test_new_private_session_is_never_inserted(self):
        with incognito_scope():
            self.db.create_session("private-new", "cli", system_prompt=CANARY)
            self.assertEqual(self.db.append_message("private-new", "user", CANARY), 0)
            self.db.set_meta("private-key", CANARY)
            self.db.set_meta("private-cursor", CANARY, cursor=self.db._conn.cursor())
        self.assertIsNone(self.db.get_session("private-new"))
        self.assertNoCanaryOnDisk()

    def test_privacy_lookup_failure_skips_callback_and_hides_exception(self):
        before = self.snapshot()
        callback = mock.Mock(side_effect=AssertionError("must not run"))
        with mock.patch("agent.incognito.is_incognito", side_effect=RuntimeError(CANARY)):
            with self.assertLogs("hermes_state_incognito", level="WARNING") as logs:
                self.assertEqual(self.db._execute_write(callback, skip_result=0), 0)
        callback.assert_not_called()
        self.assertNotIn(CANARY, "\n".join(logs.output))
        self.assertEqual(before, self.snapshot())

    def test_queued_private_metadata_does_not_flush_after_off(self):
        with incognito_scope():
            self.db.queue_token_counts(self.sid, input_tokens=4, model=CANARY)
        self.assertFalse(self.db._token_queue)
        self.assertTrue(self.db.flush_token_counts())
        self.assertNoCanaryOnDisk()
        self.db.append_message(self.sid, "assistant", "normal after off")
        self.assertEqual(self.db.get_messages(self.sid)[-1]["content"], "normal after off")


if __name__ == "__main__":
    unittest.main()

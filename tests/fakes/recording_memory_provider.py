"""Disposable plugin copied into a gateway fixture's HERMES_HOME/plugins.

Only production MemoryProvider callbacks write the separately inspected JSONL ledger.
The ledger is outside the private artifact tree, so deleting that tree cannot make
an accidental backend write disappear from the acceptance oracle.
"""
import json
from pathlib import Path

from agent.memory_provider import MemoryProvider


class RecordingMemoryProvider(MemoryProvider):
    @property
    def name(self):
        return "incognito-recording"

    def is_available(self):
        return True

    def get_tool_schemas(self):
        return []

    def initialize(self, session_id, **kwargs):
        self.session_id = session_id
        self.ledger = Path(kwargs["hermes_home"]).parent.parent / "memory-ledger.jsonl"

    def _write(self, kind, content):
        with self.ledger.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"kind": kind, "session_id": self.session_id, "content": content}) + "\n")

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None,
                  turn_author=None, display_kind=None, platform=None):
        self._write("sync_turn", [user_content, assistant_content])

    def on_pre_compress(self, messages):
        self._write("pre_compress", messages)
        return ""

    def on_session_end(self, messages):
        self._write("session_end", messages)

    def on_memory_write(self, action, target, content, metadata=None):
        self._write("memory_write", [action, target, content])


def register(ctx):
    ctx.register_memory_provider(RecordingMemoryProvider())

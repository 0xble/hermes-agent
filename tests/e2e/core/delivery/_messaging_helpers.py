"""Fixtures and transcript diagnostics for the messaging delivery E2Es."""
from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

import pytest

from tests.e2e.core.delivery._fake_platform import GatewayProcess, footer, header, read_jsonl
from tests.fakes.fake_llm_provider import FakeLLMServer, Text

PLATFORMS = {"fk_tg": "telegram", "fk_dc": "discord", "fk_ne": "noedit"}
STREAMING = {"fk_tg": True, "fk_dc": True, "fk_ne": False}
EXTRA_CONFIG = (
    "display:\n"
    "  platforms:\n"
    "    fk_ne:\n"
    "      streaming: false\n"
)

_TOKEN_RE = re.compile(r"\[in:([A-Za-z0-9_.-]+)\]")


class Director:
    """Scripts the fake model per inbound token and records which turns each token started."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.scripts: dict[str, list] = {}
        self.turns: dict[str, int] = {}
        self.resumes: dict[str, int] = {}

    def script(self, token: str, *responses) -> None:
        with self._lock:
            self.scripts[token] = list(responses)

    @staticmethod
    def answer(aid: str, body: str, **kw) -> Text:
        return Text(f"{header(aid)} {body} {footer(aid)}", **kw)

    def __call__(self, record: dict):
        request = record["body"]
        users = [m for m in request.get("messages", []) if m.get("role") == "user"]

        def text_of(m) -> str:
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return c or ""

        last = text_of(users[-1]) if users else ""
        # A resumed turn's recovery note can arrive merged with the original tagged user message
        # (the unanswered user row and the note are consecutive user turns). It is still a resume,
        # not a second run of that token.
        from gateway.run import RESUME_NOTE_PREFIX
        resumed = RESUME_NOTE_PREFIX in last
        tokens = [] if resumed else _TOKEN_RE.findall(last)
        # A tool-result continuation re-sends the same last user message: it is the same turn.
        continuation = request["messages"][-1].get("role") == "tool"
        with self._lock:
            if not tokens:
                # No inbound token in the newest user message: a gateway-synthesized turn (restart
                # auto-resume). Attribute it to the newest inbound in the history.
                earlier = [t for m in users for t in _TOKEN_RE.findall(text_of(m))]
                owner = earlier[-1] if earlier else "none"
                if not continuation:
                    self.resumes[owner] = self.resumes.get(owner, 0) + 1
                return self.answer(f"R-{owner}-{self.resumes.get(owner, 0)}", "resumed turn")
            token = tokens[-1]
            if not continuation:
                self.turns[token] = self.turns.get(token, 0) + 1
            queue = self.scripts.get(token)
            if queue:
                return queue.pop(0)
            n = self.turns.get(token, 0)
            return self.answer(f"{token}-extra{n}", "unscripted extra turn")


@pytest.fixture(scope="module")
def director():
    return Director()


@pytest.fixture(scope="module")
def llm(director):
    with FakeLLMServer(director) as srv:
        yield srv


def ledger_rows(db_path: Path, chat: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        return conn.execute("SELECT state, attempts, content FROM delivery_obligations WHERE chat_id=?",
                            (chat,)).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def ledger_quiescent(db_path: Path, chat: str) -> bool:
    return all(state in ("delivered", "abandoned") for state, _, _ in ledger_rows(db_path, chat))


def dump(gw: GatewayProcess, platform: str, chat: str) -> str:
    lines = [f"--- visible in {platform}:{chat}"]
    for m in gw.platform_view().visible(platform, chat):
        lines.append(f"  [{m.message_id} edits={m.edits}] {m.text[:160]!r}")
    lines.append("--- ops")
    for op in gw.ops():
        if op.get("platform") == platform and op.get("chat_id") == chat:
            lines.append(f"  {op['op']} {op.get('content', '')[:100]!r}")
    lines.append(f"--- ledger {ledger_rows(gw.db_path, chat)}")
    fired = read_jsonl(gw.spool / "faults_fired.jsonl")[-4:]
    lines.append(f"--- last faults fired {[(f['op'], f['kind'], f['content'][:40]) for f in fired]}")
    return "\n".join(lines)

#!/usr/bin/env python3
"""Tool-choice eval for typed session and relay delivery lookups.

Each trial gets a fresh HERMES_HOME and a seeded state.db. The model sees realistic
session/relay tools behind tool_search plus terminal. A trial passes only when its
first state lookup is a typed tool (after tool_search is allowed), and it never
uses sqlite3/direct state.db access or gives up.

Usage:
  python evals/tool_choice_session_relay/eval.py --tree ~/.hermes/current --variant baseline --trials 1
  python evals/tool_choice_session_relay/eval.py --tree . --variant fixed --trials 5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CASES = [
    ("title_relay", "Relay this to the PartyDAO Tokens owner: the token ownership audit is ready for review.", "session_search"),
    ("topic_name", "Which session is the Everguide Review thread?", "session_search"),
    ("slack_thread", "Slack channel C-EVAL-42, thread_ts 1712345678.000100 — what session is this?", "session_search"),
    ("receipt_wait", "Did my relay receipt r-eval-delivery-7 get delivered? Wait for it if needed.", "mcp__relay__status"),
    ("session_last", "What was the last thing said in session 20261116_everguide_abc123?", "session_search"),
]
PARAPHRASES = {
    "title_relay": ["Send this to whoever owns PartyDAO Tokens: the token ownership audit is ready for review.", "Relay to the PartyDAO Tokens owner session: please confirm the audit scope."],
    "topic_name": ["Find the session named Everguide Review.", "Where is the Everguide Review topic/session?"],
    "slack_thread": ["For Slack C-EVAL-42 at 1712345678.000100, tell me the Hermes session.", "Resolve the Hermes session behind channel C-EVAL-42 and thread 1712345678.000100."],
    "receipt_wait": ["Check receipt r-eval-delivery-7 and wait until delivery is known.", "Has r-eval-delivery-7 reached the target transcript yet?"],
    "session_last": ["Look up 20261116_everguide_abc123 and tell me its final message.", "Read the ending of session 20261116_everguide_abc123."],
}

SESSION_ROWS = {
    "partydao": {"session_id": "20261116_partydao_owner_a1b2c3", "title": "PartyDAO Tokens Owner", "topic_name": "PartyDAO Tokens", "source": "slack", "chat_id": "C-TOKENS", "thread_id": "1712345600.000001", "last": "The token ownership audit is ready for review."},
    "everguide": {"session_id": "20261116_everguide_abc123", "title": "Everguide Review", "topic_name": "Everguide Review", "source": "slack", "chat_id": "C-EVAL-42", "thread_id": "1712345678.000100", "last": "Final review note: publish the corrected join link after QA."},
}


def _yaml_or_json(obj: dict[str, Any]) -> str:
    try:
        import hermes_yaml
        return hermes_yaml.safe_dump(obj, sort_keys=False)
    except ImportError:
        return json.dumps(obj, indent=2)


def seed_state(home: Path) -> None:
    from hermes_state import SessionDB
    db = SessionDB(home / "state.db")
    now = time.time()
    for key, row in SESSION_ROWS.items():
        db.create_session(row["session_id"], source=row["source"], chat_id=row["chat_id"], thread_id=row["thread_id"], display_name=row["topic_name"])
        db._conn.execute("UPDATE sessions SET title = ?, started_at = ? WHERE id = ?", (row["title"], now - 60, row["session_id"]))
        db.append_message(row["session_id"], "user", "Please review this session.")
        db.append_message(row["session_id"], "assistant", row["last"])
    db.close()


def tool_defs(variant: str) -> list[dict[str, Any]]:
    fixed = variant == "fixed"
    return [
        {
            "name": "session_search",
            "description": (
                "Search or read past Hermes conversations. Find a session by title or topic name, "
                "resolve a Slack channel plus thread_ts or Telegram topic to its session, or read a "
                "session's messages. Use this typed tool instead of sqlite3 or direct state.db reads."
                if fixed else "Search past Hermes conversations and recall messages."
            ),
            "params": {"query": ("string", "Search terms or session id"), "lookup": ("object", "Typed lookup by title, topic_name, platform, chat_id, and thread_id; use for Slack channel/thread resolution.")},
        },
        {
            "name": "session_state",
            "description": (
                "Read-only typed inspection of Hermes session state, including Slack threads and "
                "session metadata. Use inspect_session for a session id and inspect_delivery for a "
                "relay delivery record; never query state.db directly."
                if fixed else "Read-only inspection of Hermes session state."
            ),
            "params": {"action": ("string", "resolve_topic, inspect_session, or inspect_delivery"), "session_id": ("string", "Exact session id"), "chat_id": ("string", "Slack channel id"), "thread_id": ("string", "Slack thread_ts")},
        },
        {
            "name": "mcp__relay__status",
            "description": (
                "Check or wait for delivery of a relay receipt. Pass the exact receipt id and "
                "optional wait duration; delivery means the target transcript received it."
                if fixed else "Get the status of a relay receipt."
            ),
            "params": {"receipt": ("string", "Exact relay receipt id"), "wait": ("string", "Optional duration to wait for delivery")},
        },
    ]


def register_tools(variant: str, log: list[dict[str, Any]]) -> None:
    from tools.registry import registry
    defs = tool_defs(variant)
    def make_handler(tdef: dict[str, Any]):
        def handler(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
            args = args or {}
            log.append({"name": tdef["name"], "args": args})
            if tdef["name"] == "session_search":
                text = json.dumps(args).lower()
                if "partydao" in text:
                    return json.dumps({"results": [SESSION_ROWS["partydao"]]})
                if "everguide" in text or "c-eval-42" in text or "1712345678.000100" in text or "abc123" in text:
                    return json.dumps({"results": [SESSION_ROWS["everguide"]]})
                return json.dumps({"results": []})
            if tdef["name"] == "session_state":
                if args.get("action") == "inspect_delivery":
                    return json.dumps({"ok": True, "found": True, "rows": [{"obligation_id": "r-eval-delivery-7", "state": "delivered"}]})
                return json.dumps({"ok": True, "found": True, "session": SESSION_ROWS["everguide"]})
            return json.dumps({"id": "r-eval-delivery-7", "status": "delivered", "delivered_at": "2026-11-16T22:00:00Z"})
        return handler
    for tdef in defs:
        entry = registry.get_entry(tdef["name"])
        if tdef["name"] in {"session_search", "session_state"} and entry is not None:
            # Keep the production schema from the tree under test; only the backend is faked.
            registry.register(name=tdef["name"], toolset=entry.toolset, schema=entry.schema, handler=make_handler(tdef), override=True)
            continue
        props = {name: {"type": typ, "description": desc} for name, (typ, desc) in tdef["params"].items()}
        registry.register(name=tdef["name"], toolset="mcp-fake", schema={"name": tdef["name"], "description": tdef["description"], "parameters": {"type": "object", "properties": props}}, handler=make_handler(tdef), override=True)


def score_trial(calls: list[dict[str, Any]], wire_calls: list[dict[str, Any]], case_id: str) -> dict[str, Any]:
    """Score the first underlying typed state lookup, including tool_call bridge batches."""
    typed_names = {"session_search", "session_state", "mcp__relay__status"}
    observed: list[dict[str, Any]] = list(calls)
    for call in wire_calls:
        if call.get("name") != "tool_call":
            continue
        try:
            payload = json.loads(call.get("args", "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        for nested in payload.get("calls", []) or []:
            if nested.get("name") in typed_names:
                observed.append(nested)
    typed = [c for c in observed if c.get("name") in typed_names]
    first_state = typed[0]["name"] if typed else None
    raw = json.dumps(wire_calls + calls, ensure_ascii=False)
    bad = bool(re.search(r"sqlite3|state\\.db|select .* from sessions|PRAGMA", raw, re.I))
    expected = {cid: expected for cid, _prompt, expected in CASES}.get(case_id)
    return {"first_state_tool": first_state, "expected_tool": expected, "raw_state_db_attempt": bad, "passed": bool(first_state == expected and not bad and typed)}


def run_one(tree: Path, variant: str, prompt: str, model: str, provider: str, base_url: str | None, api_key: str | None, api_mode: str | None) -> dict[str, Any]:
    # Fresh imports are required because Hermes binds HERMES_HOME at import time and
    # the registry is process-global. This keeps every prompt/trial isolated.
    for name in list(sys.modules):
        if name == "model_tools" or name == "run_agent" or name == "toolsets" or name.startswith(("tools.", "hermes_cli.", "agent.", "gateway.")):
            del sys.modules[name]
    for entry in list(sys.path):
        if entry.endswith("/hermes-agent") or entry == str(tree):
            try:
                sys.path.remove(entry)
            except ValueError:
                pass
    root = Path(tempfile.mkdtemp(prefix="tool-choice-"))
    home = root / ".hermes"
    home.mkdir()
    (home / "logs").mkdir()
    auth = Path.home() / ".hermes" / "auth.json"
    env_file = Path.home() / ".hermes" / ".env"
    if auth.is_file(): shutil.copy(auth, home / "auth.json")
    if env_file.is_file(): shutil.copy(env_file, home / ".env")
    config = {"model": {"provider": provider, "model": model}, "providers": {"openai": {"api_mode": api_mode or "codex_responses", "base_url": base_url or "http://127.0.0.1:8317/v1", "key_env": "CLIPROXYAPI_API_KEY"}}, "tools": {"tool_search": {"enabled": "on", "threshold_pct": 10, "search_default_limit": 10, "max_search_limit": 25, "defer": ["session_search", "session_state", "mcp__relay__status"]}}}
    (home / "config.yaml").write_text(_yaml_or_json(config), encoding="utf-8")
    os.environ["HERMES_HOME"] = str(home)
    os.environ["CUSTOM:OPENAI_PROXY_API_KEY"] = os.environ.get("CLIPROXYAPI_API_KEY", "")
    os.environ["CUSTOM_OPENAI_PROXY_API_KEY"] = os.environ.get("CLIPROXYAPI_API_KEY", "")
    os.chdir(root)
    sys.path.insert(0, str(tree))
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv(hermes_home=str(home))
    seed_state(home)
    calls: list[dict[str, Any]] = []
    from run_agent import AIAgent
    agent = AIAgent(provider=provider, model=model, base_url=base_url, api_key=api_key, api_mode=api_mode, enabled_toolsets=None, quiet_mode=True, skip_context_files=True, skip_memory=True, platform="cli", max_iterations=15)
    # Built-in tools register during AIAgent construction. Override their handlers/schema
    # only after that registration, so the fixture remains the sole observable backend.
    register_tools(variant, calls)
    error = None
    result: Any = None
    try:
        result = agent.run_conversation(prompt)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    messages = (result or {}).get("messages", []) if isinstance(result, dict) else getattr(agent, "messages", [])
    # Registry log captures the real underlying calls. Transcript captures terminal and bridge calls.
    transcript_calls: list[dict[str, Any]] = []
    for msg in messages or []:
        for call in msg.get("tool_calls", []) or []:
            fn = call.get("function", {})
            transcript_calls.append({"name": fn.get("name", ""), "args": fn.get("arguments", "")})
    raw = json.dumps(transcript_calls + calls, ensure_ascii=False)
    case_id = next((cid for cid, base_prompt, _expected in CASES if prompt == base_prompt or prompt in PARAPHRASES.get(cid, [])), "")
    score = score_trial(calls, transcript_calls, case_id)
    out = {"prompt": prompt, "variant": variant, "model": model, **score, "typed_calls": calls, "wire_calls": transcript_calls, "error": error}
    shutil.rmtree(root, ignore_errors=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", type=Path, required=True)
    ap.add_argument("--variant", choices=("baseline", "fixed"), required=True)
    ap.add_argument("--model", default="anthropic/claude-haiku-4.5")
    ap.add_argument("--api-mode")
    ap.add_argument("--case", choices=[cid for cid, _prompt, _expected in CASES])
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    prompts = [(cid, p) for cid, p, _ in CASES if not args.case or cid == args.case] + [(cid, p) for cid, ps in PARAPHRASES.items() if not args.case or cid == args.case for p in ps]
    rows = []
    provider = "openai"
    base_url = "http://127.0.0.1:8317/v1"
    api_key = os.environ.get("CLIPROXYAPI_API_KEY")
    if args.model.startswith("openrouter/"):
        provider = "openrouter"
    for trial in range(args.trials):
        for cid, prompt in prompts:
            row = run_one(args.tree.expanduser().resolve(), args.variant, prompt, args.model, provider, base_url, api_key, args.api_mode)
            row["case"] = cid
            row["trial"] = trial + 1
            rows.append(row)
            print(json.dumps(row, sort_keys=True), flush=True)
    summary = {cid: {"passes": sum(r["passed"] for r in rows if r["case"] == cid), "trials": sum(1 for r in rows if r["case"] == cid), "pass_rate": sum(r["passed"] for r in rows if r["case"] == cid) / max(1, sum(1 for r in rows if r["case"] == cid))} for cid, _, _ in CASES}
    report = {"variant": args.variant, "tree": str(args.tree), "trials": args.trials, "summary": summary, "rows": rows}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"variant": args.variant, "summary": summary}, sort_keys=True))
    return 0 if all(v["passes"] >= max(1, min(4, args.trials)) for v in summary.values()) else 1

if __name__ == "__main__":
    raise SystemExit(main())

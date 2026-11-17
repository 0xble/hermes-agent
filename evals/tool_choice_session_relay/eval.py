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
import logging
import os
import re
import shutil
import subprocess
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


def _relay_op_schema(relay_tree: Path, relay_ref: str, op: str) -> dict[str, Any]:
    """Read an MCP relay operation summary and input help from relay source."""
    filename = f"ops/{op}.go"
    source = subprocess.check_output(
        ["git", "-C", str(relay_tree), "show", f"{relay_ref}:{filename}"],
        text=True,
        stderr=subprocess.STDOUT,
    )
    summary = re.search(r'(?m)^\s*Summary:\s*"([^"]+)"', source)
    if not summary:
        raise RuntimeError(f"could not extract relay {op} schema from {relay_ref}")
    properties: dict[str, Any] = {}
    for field, typ, tag in re.findall(r'(?m)^\s*([A-Z]\w*)\s+([\w\[\]]+)\s+`([^`]+)`', source):
        json_name = re.search(r'json:"([^,\"]+)', tag)
        help_text = re.search(r'help:"([^\"]*)"', tag)
        if json_name and json_name.group(1) != "-":
            properties[json_name.group(1)] = {"type": {"string": "string", "bool": "boolean"}.get(typ, "string")}
            if help_text:
                properties[json_name.group(1)]["description"] = help_text.group(1)
    return {"name": f"mcp__relay__{op}", "description": summary.group(1), "parameters": {"type": "object", "properties": properties}}


def _relay_status_schema(relay_tree: Path, relay_ref: str) -> dict[str, Any]:
    """Read the MCP status operation's schema fields from relay's Go source."""
    schema = _relay_op_schema(relay_tree, relay_ref, "status")
    if not {"receipt", "wait"}.issubset(schema["parameters"]["properties"]):
        raise RuntimeError(f"relay status schema missing receipt/wait in {relay_ref}")
    return schema


def tool_defs(variant: str, relay_tree: Path) -> list[dict[str, Any]]:
    from tools.registry import registry
    defs = []
    for name in ("session_search", "session_state"):
        entry = registry.get_entry(name)
        if entry is None:
            raise RuntimeError(f"{name} was not registered by the tree under test")
        defs.append({"name": name, "schema": entry.schema, "toolset": entry.toolset})
    relay_ref = "fix/tool-choice-status-description" if variant == "fixed" else "main"
    defs.append({"name": "mcp__relay__send", "schema": _relay_op_schema(relay_tree, relay_ref, "send"), "toolset": "mcp-relay"})
    defs.append({"name": "mcp__relay__status", "schema": _relay_status_schema(relay_tree, relay_ref), "toolset": "mcp-relay"})
    return defs


def register_tools(variant: str, relay_tree: Path, log: list[dict[str, Any]]) -> None:
    from tools.registry import registry
    defs = tool_defs(variant, relay_tree)
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
        registry.register(
            name=tdef["name"], toolset=entry.toolset if entry else tdef["toolset"],
            schema=entry.schema if entry else tdef["schema"], handler=make_handler(tdef), override=True,
        )


def score_trial(calls: list[dict[str, Any]], wire_calls: list[dict[str, Any]], case_id: str) -> dict[str, Any]:
    """Score the first underlying typed state lookup, including tool_call bridge batches."""
    typed_names = {"session_search", "session_state", "mcp__relay__status"}
    observed: list[dict[str, Any]] = []
    # The transcript preserves model/tool-call order. Registry logs only the
    # fixture backend calls and can lag bridge calls, so do not use it to infer
    # which typed lookup happened first.
    for call in wire_calls:
        if call.get("name") == "tool_call":
            try:
                payload = json.loads(call.get("args", "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            observed.extend(nested for nested in payload.get("calls", []) or [] if nested.get("name") in typed_names)
        elif call.get("name") in typed_names:
            observed.append(call)
    observed.extend(c for c in calls if c.get("name") in typed_names and c not in observed)
    typed = observed
    first_state = typed[0]["name"] if typed else None
    raw = json.dumps(wire_calls + calls, ensure_ascii=False)
    bad = bool(re.search(r"sqlite3|state\\.db|select .* from sessions|PRAGMA", raw, re.I))
    expected = {cid: expected for cid, _prompt, expected in CASES}.get(case_id)
    return {"first_state_tool": first_state, "expected_tool": expected, "raw_state_db_attempt": bad, "passed": bool(first_state == expected and not bad and typed)}


def run_one(tree: Path, relay_tree: Path, variant: str, prompt: str, model: str, provider: str, base_url: str | None, api_key: str | None, api_mode: str | None) -> dict[str, Any]:
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
    config = {"model": {"provider": provider, "model": model}, "providers": {"openai": {"api_mode": api_mode or "codex_responses", "base_url": base_url or "http://127.0.0.1:8317/v1", "key_env": "CLIPROXYAPI_API_KEY"}}, "tools": {"tool_search": {"enabled": "on", "threshold_pct": 10, "search_default_limit": 10, "max_search_limit": 25, "defer": ["session_search", "session_state", "mcp__relay__send", "mcp__relay__status"]}}}
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
    register_tools(variant, relay_tree, calls)
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
    logging.shutdown()
    shutil.rmtree(root, ignore_errors=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", type=Path, required=True)
    ap.add_argument("--relay-tree", type=Path, default=Path("~/Repos/relay"))
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
            row = run_one(args.tree.expanduser().resolve(), args.relay_tree.expanduser().resolve(), args.variant, prompt, args.model, provider, base_url, api_key, args.api_mode)
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

#!/usr/bin/env python3
"""Live S2 behavior eval for the agent-requested ``compact_context`` tool.

Each run builds a fresh production ``AIAgent`` (real system prompt, real tools[] for the
configured CLI toolsets, real provider transport), replays a scripted transcript as
conversation history, sends one natural user turn, and records whether the model's next
response calls ``compact_context``. Tool calls are captured, never executed, and the run
neither persists to state.db nor syncs to external memory.

Transcript bulk is real repository content (file reads, git history, searches) sized
relative to each model's real auto-compress threshold, so the gates and the model see
honest context sizes. The only usage signal a model gets is what Hermes really shows:
a prior ``compact_context`` result carrying ``current_context_tokens`` and
``auto_compress_threshold_tokens`` (used in the long-stretch scenario only).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from hermes_cli.config import get_env_value_prefer_dotenv, load_config
from run_agent import AIAgent

CHARS_PER_TOKEN = 4  # rough sizing only; real prompt tokens are recorded per run

SCENARIOS: dict[str, dict[str, Any]] = {
    "finished_subtask": {"expected": "call", "size": 0.42,
                         "label": "Finished sub-task with a large tool-output dump"},
    "topic_switch": {"expected": "call", "size": 0.42,
                     "label": "Unrelated topic switch in a long session"},
    "long_stretch": {"expected": "unscored", "size": 0.60,
                     "label": "Long multi-step stretch ahead, past half the threshold"},
    "small_context": {"expected": "no-call", "size": 0.0, "label": "Small context"},
    "mid_debug": {"expected": "no-call", "size": 0.42,
                  "label": "Mid-debug where exact recent output matters"},
    "just_compacted": {"expected": "no-call", "size": 0.22, "label": "Just compacted"},
}


# ---------------------------------------------------------------- real tool output

def _read_file_output(rel: str, max_chars: int) -> str:
    lines = (REPO / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    out, used = [], 0
    for i, line in enumerate(lines, 1):
        row = f"{i:>6}|{line}"
        if used + len(row) > max_chars:
            break
        out.append(row)
        used += len(row) + 1
    return json.dumps({"content": "\n".join(out), "total_lines": len(lines)})


def _git(args: list[str], max_chars: int) -> str:
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False).stdout
    return json.dumps({"output": out[:max_chars], "exit_code": 0})


def _search(pattern: str, max_chars: int) -> str:
    out = subprocess.run(["git", "-C", str(REPO), "grep", "-n", "-E", pattern, "--", "agent", "tools"],
                         capture_output=True, text=True, check=False).stdout
    return json.dumps({"matches": out[:max_chars]})


_FILES = [
    "agent/context_compressor.py", "agent/conversation_compression.py", "agent/chat_completion_helpers.py",
    "agent/agent_runtime_helpers.py", "agent/model_metadata.py", "agent/agent_init.py",
    "agent/prompt_builder.py", "agent/turn_recovery.py", "agent/tool_executor.py", "agent/conversation_loop.py",
]


def _bulk_steps(kind: str, target_tokens: int, per_call_tokens: int = 40_000) -> list[tuple[str, dict, str]]:
    """``(tool_name, args, result)`` steps of real repo output totalling ~target_tokens.

    Few large calls keep the history under ``protect_last_n`` so production pruning
    leaves the bulk intact, matching a session that just read several big files.
    """
    steps: list[tuple[str, dict, str]] = []
    remaining = target_tokens * CHARS_PER_TOKEN
    per_call = per_call_tokens * CHARS_PER_TOKEN
    i = 0
    while remaining > 2_000 and i < 40:
        size = min(per_call, remaining)
        if kind == "history" and i % 2 == 0:
            res = _git(["log", "--stat", f"--skip={i * 150}", "-n", "400"], size)
            steps.append(("terminal", {"command": f"git log --stat --skip={i * 150} -n 400"}, res))
        elif kind == "search" and i % 3 == 2:
            pat = ["compress", "threshold_tokens", "last_prompt_tokens", "def _", "summary"][i % 5]
            steps.append(("search_files", {"pattern": pat, "path": "agent"}, _search(pat, size)))
        else:
            rel = _FILES[i % len(_FILES)]
            steps.append(("read_file", {"path": rel}, _read_file_output(rel, size)))
        remaining -= len(steps[-1][2])
        i += 1
    return steps


def _as_messages(steps: list[tuple[str, dict, str]], prefix: str, narration: str) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for n, (name, args, result) in enumerate(steps):
        cid = f"call_{prefix}_{n}"
        msgs.append({"role": "assistant", "content": narration if n == 0 else "",
                     "tool_calls": [{"id": cid, "type": "function",
                                     "function": {"name": name, "arguments": json.dumps(args)}}]})
        msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
    return msgs


def _tool_turn(cid: str, name: str, args: dict, result: str, text: str = "") -> list[dict[str, Any]]:
    return [{"role": "assistant", "content": text, "tool_calls": [{"id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]},
            {"role": "tool", "tool_call_id": cid, "content": result}]


# ---------------------------------------------------------------- scenarios

def transcript(name: str, threshold: int) -> tuple[list[dict[str, Any]], str]:
    target = int(threshold * SCENARIOS[name]["size"])

    if name == "finished_subtask":
        history = [{"role": "user", "content": "Audit how context compression is wired through agent/. I want "
                    "to know every place that reads threshold_tokens or last_prompt_tokens and whether any of "
                    "them can disagree."}]
        history += _as_messages(_bulk_steps("search", target), "audit", "Reading the compression path end to end.")
        history.append({"role": "assistant", "content": (
            "Audit done. threshold_tokens is owned by ContextCompressor and read in 6 places, all through the "
            "compressor instance, so they cannot disagree. last_prompt_tokens has 2 sources: the compressor's "
            "provider-billed reading and agent._last_prompt_tokens. After a compaction the compressor resets to -1 "
            "until the next provider usage sample, and every reader handles that sentinel. No defects found. The "
            "one fragile spot is agent/compact_context.py context_usage(), which falls back to a rough estimate "
            "when both readings are missing.")})
        return history, ("Good, that settles it. Next: add a regression test for the context_usage() fallback "
                         "in agent/compact_context.py, run it, then sweep the other compression tests for the "
                         "same pattern.")

    if name == "topic_switch":
        history = [{"role": "user", "content": "Go through the last few hundred commits and tell me which "
                    "compression changes landed this month and whether any were reverted."}]
        history += _as_messages(_bulk_steps("history", target), "hist", "Pulling the commit history.")
        history.append({"role": "assistant", "content": (
            "This month: 9 compression-related commits landed, covering in-place compaction, the summary-stall "
            "retry, proactive pruning floors, and agent-requested compaction. None were reverted. 2 were "
            "follow-up fixes to earlier ones in the same week.")})
        return history, ("Thanks, that's all I needed there. Totally unrelated: help me plan a 3-day family "
                         "trip to Portland next month. We have a toddler, so nothing too late in the day.")

    if name == "long_stretch":
        first = _bulk_steps("search", int(target * 0.85))
        later = _bulk_steps("read", int(target * 0.15), per_call_tokens=15_000)
        then_tokens = int(target * 0.86)
        refused = json.dumps({
            "accepted": False, "state": "refused",
            "reason": "agent-requested compaction cooldown active; wait 2 API calls",
            "current_context_tokens": then_tokens, "auto_compress_threshold_tokens": threshold})
        history = [{"role": "user", "content": "Map every caller of estimate_messages_tokens_rough in agent/ "
                    "and tools/. We're going to migrate them all to the billed-usage helper."}]
        history += _as_messages(first, "map", "Mapping callers.")
        history += _tool_turn("call_map_compact", "compact_context",
                              {"reason": "caller mapping finished; raw file reads no longer needed",
                               "focus": "caller list, migration plan, open questions"},
                              refused, "Mapping is done, so I'll ask to compact before the next phase.")
        history += _as_messages(later, "verify", "Refused for cooldown. Verifying the last few call sites meanwhile.")
        history.append({"role": "assistant", "content": (
            "Caller map complete: 23 call sites across 11 modules. 14 are straightforward swaps, 6 need the "
            "billed reading threaded through, and 3 run before any provider call and must keep the rough "
            "estimate. Plan is ready.")})
        return history, ("Great. Start the migration now: work through all 23 call sites module by module, run "
                         "each module's tests after you change it, and fix whatever breaks. Report when it's all green.")

    if name == "small_context":
        history = [
            {"role": "user", "content": "Rename the helper _ra in agent/conversation_loop.py to _runtime_attr."},
            *_tool_turn("call_small_1", "search_files", {"pattern": r"\b_ra\(", "path": "agent/conversation_loop.py"},
                        json.dumps({"matches": "476:def _ra():\n1512:    rt = _ra()\n1530:    rt = _ra()"}),
                        "Finding usages."),
            {"role": "assistant", "content": "Renamed _ra to _runtime_attr at its definition and 2 call sites."},
        ]
        return history, "Thanks. Unrelated, what's the capital of Oregon?"

    if name == "mid_debug":
        history = [{"role": "user", "content": "test_compress_focus is failing on my branch. Find out why and fix it."}]
        history += _as_messages(_bulk_steps("read", target), "dbg", "Reading the focus plumbing first.")
        failing = json.dumps({"exit_code": 1, "output": (
            "tests/agent/test_compress_focus.py::test_focus_survives_in_place_compaction FAILED\n"
            "E   AssertionError: focus text missing from summary request\n"
            "E   assert 'preserve PR #680 and the cooldown decision' in 'Summarize the conversation...'\n"
            "agent/context_compressor.py:3897: in _summarize\n"
            "    \"messages\": [{\"role\": \"user\", \"content\": prompt}], \"route_info\": _aux_route,\n"
            "agent/compact_context.py:146: in take_pending_compaction\n"
            "    claimed = dict(pending)\n"
            "1 failed, 41 passed in 6.21s")})
        history += _tool_turn("call_dbg_test", "terminal",
                              {"command": "bash scripts/run_tests.sh tests/agent/test_compress_focus.py"},
                              failing, "Running the failing test.")
        history += _tool_turn("call_dbg_patch", "patch",
                              {"path": "agent/context_compressor.py", "old_string": "prompt = base_prompt",
                               "new_string": "prompt = base_prompt + focus_block"},
                              json.dumps({"success": True}), "The focus block never reaches the prompt. Patching.")
        history += _tool_turn("call_dbg_test2", "terminal",
                              {"command": "bash scripts/run_tests.sh tests/agent/test_compress_focus.py"},
                              failing.replace("6.21s", "6.08s"), "Re-running.")
        return history, "Still the same assertion. What's going on? Look at the take_pending_compaction frame."

    if name == "just_compacted":
        summary = (
            "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. "
            "Respond ONLY to the latest user message that appears AFTER this summary.\n"
            "## Historical Task Snapshot\n- Migrating estimate_messages_tokens_rough callers to the billed-usage "
            "helper. 9 of 23 call sites done (agent/turn_context.py, agent/turn_preflight.py, "
            "agent/conversation_loop.py). Tests green for those modules.\n- Remaining: 14 call sites in 8 modules; "
            "3 pre-provider sites keep the rough estimate.\n"
            "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---")
        committed = json.dumps({"accepted": True, "state": "accepted",
                                "reason": "accepted; compaction will run at the next safe post-tool boundary",
                                "current_context_tokens": int(threshold * 0.58),
                                "auto_compress_threshold_tokens": threshold})
        history = [{"role": "user", "content": summary, "_compressed_summary": True}]
        history += _tool_turn("call_jc_compact", "compact_context",
                              {"reason": "first 9 call sites migrated; raw reads no longer needed",
                               "focus": "remaining 14 call sites, 3 pre-provider exceptions"},
                              committed, "Phase boundary, compacting before the remaining modules.")
        history += _as_messages(_bulk_steps("read", target, per_call_tokens=20_000), "jc",
                                "Compaction done. Reading the next module.")
        return history, "Keep going with the remaining call sites."

    raise KeyError(name)


# ---------------------------------------------------------------- runner

def _model_cfg(config: dict, model_key: str) -> dict:
    if model_key == "primary":
        return {**config["model"], "model": config["model"]["default"]}
    return dict(config["model"]["attended"])


def run_one(model_key: str, scenario: str, run_number: int) -> dict[str, Any]:
    config = load_config()
    model_cfg = _model_cfg(config, model_key)
    provider_name = str(model_cfg["provider"])
    provider_cfg = config["providers"][provider_name.split(":", 1)[-1]]
    agent = AIAgent(
        model=model_cfg["model"], provider=provider_name, base_url=provider_cfg["base_url"],
        api_key=get_env_value_prefer_dotenv(str(provider_cfg.get("key_env") or "")) or None,
        api_mode=provider_cfg["api_mode"],
        reasoning_config={"effort": model_cfg.get("reasoning_effort", "high")},
        quiet_mode=True, max_iterations=6, save_trajectories=False, skip_background_review=True,
        session_id=f"s2-{model_key}-{scenario}-{run_number}-{int(time.time())}", cwd=str(REPO),
    )
    # Measure one user turn (up to 6 model responses, since the real prompt often loads skills first).
    # Tool calls are captured and answered with a stub,
    # never executed; persistence and external-memory sync are off so nothing leaves the run.
    agent._persist_disabled = True  # type: ignore[attr-defined]
    agent._sync_external_memory_for_turn = lambda **_kw: None  # type: ignore[method-assign]
    captured: list[dict[str, Any]] = []

    def _capture(assistant_message, messages, effective_task_id, api_call_count=0):
        for call in getattr(assistant_message, "tool_calls", None) or []:
            fn = getattr(call, "function", None)
            raw = getattr(fn, "arguments", None) or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except Exception:
                args = {"_unparsed": str(raw)[:500]}
            captured.append({"name": getattr(fn, "name", "") or "", "args": args})
            messages.append({"role": "tool", "tool_call_id": getattr(call, "id", ""),
                             "content": json.dumps({"eval": "tool not executed"})})

    agent._execute_tool_calls = _capture  # type: ignore[method-assign]
    compressor = agent.context_compressor
    threshold = int(compressor.threshold_tokens)
    history, prompt = transcript(scenario, threshold)
    compressions_before = int(getattr(compressor, "compression_count", 0) or 0)
    peak_prompt_tokens: list[int | None] = [None]
    response_count = [0]
    _orig_capture = _capture

    def _capture_stop(assistant_message, messages, effective_task_id, api_call_count=0):
        response_count[0] += 1
        # Provider usage of this response lives on the agent after each API call; keep the peak.
        usage = getattr(agent.context_compressor, "last_prompt_tokens", None) or getattr(agent, "_last_prompt_tokens", None)
        if isinstance(usage, (int, float)) and usage > 0:
            peak_prompt_tokens[0] = max(peak_prompt_tokens[0] or 0, int(usage))
        calls = getattr(assistant_message, "tool_calls", None) or []
        # A call to compact must survive skill-browsing preambles, but the run should not start the new
        # task's research. Stop the turn at the first non-skill tool batch (that is a "did not compact"
        # outcome) so the turn cannot compress itself empty first.
        if calls and not any(
            (getattr(getattr(c, "function", None), "name", "") or "") == "compact_context" for c in calls
        ) and not all(
            (getattr(getattr(c, "function", None), "name", "") or "") in {"skills_list", "skill_view"}
            for c in calls
        ):
            _orig_capture(assistant_message, messages, effective_task_id, api_call_count)
            agent.max_iterations = 0
            return
        _orig_capture(assistant_message, messages, effective_task_id, api_call_count)

    agent._execute_tool_calls = _capture_stop  # type: ignore[method-assign]
    result = agent.run_conversation(prompt, conversation_history=history)
    compressions_after = int(getattr(compressor, "compression_count", 0) or 0)
    names = [c["name"] for c in captured]
    return {
        "model_key": model_key, "model": model_cfg["model"], "provider": provider_name,
        "scenario": scenario, "run": run_number, "expected": SCENARIOS[scenario]["expected"],
        "compact_context_calls": names.count("compact_context"), "tool_names": names,
        "focus": [c["args"].get("focus") for c in captured if c["name"] == "compact_context"],
        "reason": [c["args"].get("reason") for c in captured if c["name"] == "compact_context"],
        "final_text": (result.get("final_response") or "")[:600],
        "prompt_tokens": peak_prompt_tokens[0], "threshold_tokens": threshold,
        "response_count": response_count[0],
        "provider_failed": bool(result.get("failed")) or not result.get("api_calls"),
        "preflight_compressed": compressions_after > compressions_before,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--model", choices=["primary", "attended", "both"], default="both")
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results" / "live.jsonl")
    args = parser.parse_args()
    models = ["primary", "attended"] if args.model == "both" else [args.model]
    scenarios = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    jobs = [(m, s, r) for m in models for s in scenarios for r in range(1, args.runs + 1)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as out, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, *job): job for job in jobs}
        for future in as_completed(futures):
            model, scenario, run_number = futures[future]
            try:
                record = future.result()
            except Exception as exc:
                record = {"model_key": model, "scenario": scenario, "run": run_number,
                          "error": f"{type(exc).__name__}: {exc}"}
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            print(json.dumps({k: record.get(k) for k in ("model_key", "scenario", "run", "compact_context_calls",
                                                         "prompt_tokens", "preflight_compressed", "error")}),
                  flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

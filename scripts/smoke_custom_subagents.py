#!/usr/bin/env python3
"""Smoke tests for named delegation subagents, in two clearly separated modes.

    python scripts/smoke_custom_subagents.py --mode fixture   # default, offline
    python scripts/smoke_custom_subagents.py --mode active    # opt-in, live

``fixture`` is deterministic and never touches the network or any credential:
it builds a disposable Hermes home, stubs the child's physical request, and
checks the plumbing an implementation change can break — per-child routing
telemetry for a MIXED batch, the shared-knowledge boundary on every mediated
write path, and the role information actually advertised to the parent.

``active`` is the honest live check. It uses Hermes's OWN credential resolver
(never ``~/.codex/auth.json`` — reading the Codex CLI's cache proved a route
Hermes does not use, and its 429 said nothing about Hermes's own auth) and the
roles configured in the ACTIVE config, then records nonsecret evidence of the
credential source and resolved route for each child.

Both modes write a receipt JSON and exit nonzero on any failed check. Neither
mode ever writes to the real Hermes home: ``HERMES_HOME`` is redirected to a
temporary directory before Hermes is imported, and ``active`` copies only the
role definitions out of the live config.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

FIXTURE_DEFINITIONS = {
    "explorer": {
        "description": "Read and attribute evidence without changing its sources.",
        "instructions": (
            "Read only. Discover skills, standing memory, and prior sessions when "
            "requested. Cite the source of each finding. Never mutate shared knowledge."
        ),
        "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "medium",
    },
    "worker": {
        "description": "Implement and verify an authorized scoped artifact.",
        "instructions": (
            "Work only inside the task's temporary directory. Exercise your artifact "
            "and return evidence. If authorization is missing, return a blocker "
            "without acting. Never mutate shared knowledge."
        ),
        "provider": "openai-codex", "model": "gpt-5.6-terra", "reasoning_effort": "medium",
    },
}


# ── shared setup ─────────────────────────────────────────────────────────────

def _disposable_home(definitions: dict, prefix: str) -> Path:
    """Create an isolated HERMES_HOME with knowledge fixtures. Never the real one."""
    home = Path(tempfile.mkdtemp(prefix=prefix))
    os.chmod(home, 0o700)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_TEST_MODE"] = "1"
    os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = "false"
    os.chdir(home)
    (home / "config.yaml").write_text(json.dumps({
        "memory": {"memory_enabled": True, "user_profile_enabled": False},
        "delegation": {
            "worktree_isolation": False, "max_iterations": 9,
            "subagents": definitions,
        },
    }), encoding="utf-8")
    (home / "memories").mkdir()
    (home / "memories/MEMORY.md").write_text(
        "The isolated smoke's standing-memory marker is MEMORY_FIXTURE_OK.\n",
        encoding="utf-8",
    )
    skill = home / "skills/custom-subagents-fixture/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: custom-subagents-fixture\ndescription: Use when checking the "
        "isolated custom subagent smoke.\n---\n# Fixture\nThe skill marker is "
        "SKILL_FIXTURE_OK.\n",
        encoding="utf-8",
    )
    (home / "input.txt").write_text("READ_FIXTURE_OK\n", encoding="utf-8")
    return home


def _knowledge_digests(home: Path) -> dict:
    targets = (home / "memories/MEMORY.md",
               home / "skills/custom-subagents-fixture/SKILL.md",
               home / "input.txt")
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in targets}


def _unchanged(digests: dict) -> bool:
    return all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest
               for p, digest in digests.items())


def _tool_names(entries) -> set:
    """Tool names visible in the result trace (outer names only).

    The trace deliberately records ``input_summary.argument_keys`` rather than
    argument VALUES, so a deferred tool appears as a bare ``tool_call`` and its
    real name is not recoverable here. That is why the previous harness
    reported "session_search never ran" for a session_search that plainly did:
    the name lives in the child's transcript, not in the redacted trace. Pair
    this with :func:`_child_tool_names`.
    """
    return {
        str(item.get("tool") or item.get("name"))
        for item in entries or []
        if isinstance(item, dict) and (item.get("tool") or item.get("name"))
    }


# Wrappers whose real target is named inside their arguments.
_DEFERRED_TOOL_WRAPPERS = frozenset({"tool_call", "tool_execute", "call_tool"})


def _child_tool_names(home: Path, child_session_id) -> set:
    """Tool names the child actually called, unwrapping deferred-tool calls.

    Read from the child's own transcript, where the wrapper's arguments carry
    the real tool name. A deferred ``session_search`` is recorded as
    ``tool_call(name="session_search", ...)`` and must be counted as
    ``session_search``.
    """
    import sqlite3

    if not child_session_id:
        return set()
    names: set = set()
    with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT tool_calls FROM messages "
            "WHERE session_id=? AND role='assistant' AND tool_calls IS NOT NULL",
            (child_session_id,),
        ).fetchall()
    for (raw,) in rows:
        try:
            calls = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        for call in calls or []:
            function = (call or {}).get("function") or {}
            name = function.get("name")
            if not name:
                continue
            names.add(str(name))
            if str(name) not in _DEFERRED_TOOL_WRAPPERS:
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except (TypeError, ValueError):
                continue
            inner = args.get("name") if isinstance(args, dict) else None
            if isinstance(inner, str) and inner:
                names.add(inner)
    return names


def _route_evidence(requests: list) -> list:
    """Nonsecret per-request routing evidence. Digests only, never a token."""
    return [
        {
            "subagent_type": r.get("subagent_type"),
            "model": r.get("model"),
            "reasoning_effort": (r.get("reasoning") or {}).get("effort"),
            "physical_request": r.get("physical_request"),
        }
        for r in requests
    ]


def _result_for_task(results: list, task_index: int, role: str) -> dict:
    """Join public delegation results by their emitted batch identity."""
    matches = [result for result in results if isinstance(result, dict)
               and result.get("task_index") == task_index]
    if not matches:
        raise RuntimeError(
            f"{role} result identity missing: no result emitted task_index {task_index}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"{role} result identity duplicate: {len(matches)} results emitted task_index {task_index}"
        )
    return matches[0]


# ── fixture mode ─────────────────────────────────────────────────────────────

def run_fixture() -> dict:
    home = _disposable_home(FIXTURE_DEFINITIONS, "hermes-subagents-fixture-")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    digests = _knowledge_digests(home)

    from types import SimpleNamespace

    import run_agent
    from agent.delegation_context import delegated_child_context
    from hermes_state import SessionDB
    from tools import delegate_tool
    from tools.custom_subagents import (
        RuntimePin, parse_definitions, pinning_support_error,
    )
    from tools.knowledge_boundary import boundary_report

    def _stub_response(text: str):
        return SimpleNamespace(
            output=[SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text)],
            )],
            usage=SimpleNamespace(input_tokens=5, output_tokens=3, total_tokens=8),
            status="completed", model="fixture",
        )

    requests: list = []
    original_validate = RuntimePin.validate_request

    def capture(self, child, kwargs, **options):
        original_validate(self, child, kwargs, **options)
        requests.append({
            "physical_request": options.get("client") is not None,
            "subagent_type": self.subagent_type,
            "model": kwargs.get("model"),
            "reasoning": kwargs.get("reasoning"),
        })

    RuntimePin.validate_request = capture
    original_call = run_agent.AIAgent._interruptible_api_call
    run_agent.AIAgent._interruptible_api_call = (
        lambda self, api_kwargs, *a, **k: _stub_response("fixture child summary")
    )
    db = SessionDB()
    parent = run_agent.AIAgent(
        session_db=db, model="gpt-6-astra", provider="openai-codex",
        api_mode="codex_responses", base_url="https://chatgpt.com/backend-api/codex",
        api_key="fixture-token", reasoning_config={"enabled": True, "effort": "high"},
        enabled_toolsets=["file"], quiet_mode=True, skip_memory=True,
        skip_context_files=True, skip_background_review=True,
    )
    db.create_session(parent.session_id, source="cli")
    try:
        result = json.loads(delegate_tool.delegate_task(tasks=[
            {"subagent_type": "explorer", "goal": "Summarize the fixture input file."},
            {"subagent_type": "worker", "goal": "Summarize the fixture skill file."},
        ], parent_agent=parent, background=False))

        manifest = _read_manifest(home)
        boundary = _fixture_boundary_denials(home, delegated_child_context)
        schema = delegate_tool._build_dynamic_schema_overrides()
        advertised = (schema["parameters"]["properties"]["tasks"]["items"]
                      ["properties"]["subagent_type"])
        definitions = parse_definitions({"subagents": FIXTURE_DEFINITIONS})

        checks = {
            # item 6 — a mixed batch records each child's OWN route
            "manifest_records_each_child_route": _manifest_routes(manifest) == [
                ("explorer", "gpt-5.6-luna", "medium"),
                ("worker", "gpt-5.6-terra", "medium"),
            ],
            "manifest_batch_model_is_mixed": manifest.get("model") == "mixed",
            # item 1 — every mediated write path denies, and the report is honest
            "knowledge_boundary_denies_every_mediated_path": all(boundary.values()),
            "knowledge_boundary_reports_shell_as_scan_not_sandbox":
                boundary_report()["shell_enforcement"] == "command_scan",
            "knowledge_sources_unchanged": _unchanged(digests),
            # item 7 — roles advertise purpose AND their fixed settings
            "schema_advertises_models_and_effort": all(
                token in advertised["description"]
                for token in ("gpt-5.6-luna", "gpt-5.6-terra", "medium", "fixed")
            ),
            "schema_enumerates_configured_roles":
                advertised["enum"] == ["explorer", "worker"],
            # item 4 — unpinnable routes are rejected, not silently weakened
            "unpinnable_route_rejected":
                pinning_support_error("custom", "bedrock_converse") is not None,
            "pinned_requests_use_configured_models": {
                r["model"] for r in requests
            } == {"gpt-5.6-luna", "gpt-5.6-terra"},
            "definitions_parse": sorted(definitions) == ["explorer", "worker"],
        }
        return _receipt(home, "fixture", checks, {
            "requests": _route_evidence(requests),
            "manifest": manifest,
            "boundary_denials": boundary,
            "boundary_report": boundary_report(),
        })
    finally:
        RuntimePin.validate_request = original_validate
        run_agent.AIAgent._interruptible_api_call = original_call
        parent.close()
        db.close()


def _read_manifest(home: Path) -> dict:
    """Newest batch manifest by write time — delegation ids do not sort."""
    root = home / "cache/delegation/live"
    manifests = list(root.glob("*/manifest.json")) if root.is_dir() else []
    if not manifests:
        return {}
    newest = max(manifests, key=lambda p: p.stat().st_mtime)
    return json.loads(newest.read_text(encoding="utf-8"))


def _manifest_routes(manifest: dict) -> list:
    return [
        (t.get("subagent_type"), t.get("model"), t.get("reasoning_effort"))
        for t in manifest.get("tasks", [])
    ]


def _fixture_boundary_denials(home: Path, delegated_child_context) -> dict:
    """Exercise every mediated write path against the fixture's own knowledge.

    Deliberately targets the DISPOSABLE home's memory/skill files: the point is
    to prove denial, and a guard that only appears to work because the target
    was unwritable would prove nothing.
    """
    from tools.code_execution_tool import execute_code
    from tools.file_tools import patch_tool, write_file_tool
    from tools.memory_tool import memory_tool
    from tools.terminal_tool import terminal_tool

    memory_file = home / "memories/MEMORY.md"
    skill_file = home / "skills/custom-subagents-fixture/SKILL.md"
    denials = {}
    with delegated_child_context("smoke-child", read_only_knowledge=True):
        denials["write_file"] = "shared knowledge" in write_file_tool(
            str(memory_file), "BYPASSED\n").lower()
        denials["patch_replace"] = "shared knowledge" in patch_tool(
            mode="replace", path=str(skill_file),
            old_string="SKILL_FIXTURE_OK", new_string="BYPASSED").lower()
        denials["patch_v4a"] = "shared knowledge" in patch_tool(
            mode="patch",
            patch=f"*** Begin Patch\n*** Add File: {memory_file.parent}/new.md\n+x\n*** End Patch",
        ).lower()
        denials["terminal"] = "shared knowledge" in terminal_tool(
            command=f"echo BYPASSED >> {memory_file}").lower()
        denials["execute_code"] = "shared knowledge" in execute_code(
            code=f"open({str(memory_file)!r}, 'a').write('BYPASSED')").lower()
        denials["memory_tool"] = "parent-owned" in memory_tool(
            action="append", content="BYPASSED").lower()
    return denials


# ── active-config mode ───────────────────────────────────────────────────────

def run_active() -> dict:
    """Live smoke on the ACTIVE roles, through Hermes's own credential path."""
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))

    # Read the live configuration BEFORE redirecting HERMES_HOME — the roles
    # under test are the user's real ones, but they run in a disposable home.
    from hermes_cli.config import load_config_readonly

    live = load_config_readonly() or {}
    definitions = ((live.get("delegation") or {}).get("subagents")) or {}
    if not definitions:
        raise RuntimeError(
            "No delegation.subagents configured; active mode has nothing to "
            "exercise. Configure roles or run --mode fixture."
        )

    credentials, source = _resolve_active_credentials()
    home = _disposable_home(definitions, "hermes-subagents-active-")
    digests = _knowledge_digests(home)

    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tools.custom_subagents import RuntimePin
    from tools.delegate_tool import delegate_task

    db = SessionDB()
    db.create_session("custom-subagents-source-fixture", source="cli")
    db.set_session_title("custom-subagents-source-fixture",
                         "Custom Subagents Source Fixture")
    db.append_message("custom-subagents-source-fixture", "user",
                      "The prior-session marker is SESSION_FIXTURE_OK.")
    db.close()

    db = SessionDB()
    parent = AIAgent(
        session_db=db, model=credentials["model"], provider=credentials["provider"],
        api_mode=credentials["api_mode"], base_url=credentials["base_url"],
        api_key=credentials["api_key"],
        reasoning_config={"enabled": True, "effort": "high"},
        enabled_toolsets=["file", "terminal", "skills", "session_search"],
        quiet_mode=True, skip_memory=True, skip_context_files=True,
        skip_background_review=True,
    )
    db.create_session(parent.session_id, source="cli")
    db.append_message(parent.session_id, "user", "PARENT_PRIVATE_TRANSCRIPT_SENTINEL")
    before = (parent.model, dict(parent.reasoning_config), parent.provider,
              parent.base_url, parent.api_key)

    requests: list = []
    original_validate = RuntimePin.validate_request

    def capture(self, child, kwargs, **options):
        original_validate(self, child, kwargs, **options)
        requests.append({
            "physical_request": options.get("client") is not None,
            "subagent_type": self.subagent_type, "model": kwargs.get("model"),
            "reasoning": kwargs.get("reasoning"),
            "parent_transcript_absent":
                "PARENT_PRIVATE_TRANSCRIPT_SENTINEL" not in json.dumps(kwargs, default=str),
        })

    RuntimePin.validate_request = capture
    roles = sorted(definitions)
    reader, doer = roles[0], roles[-1]
    try:
        source_file = home / "input.txt"
        artifact = home / "probe_worker.py"
        result = json.loads(delegate_task(tasks=[
            {"subagent_type": reader, "goal": (
                f"Read {source_file} without modifying it. Discover the smoke-fixture "
                "skill with skills_list (no category filter), then load it with "
                "skill_view. Retrieve the prior session titled Custom Subagents Source "
                "Fixture using session_search. Read the standing-memory marker from "
                "your memory context. Return all four exact markers and identify their "
                "four sources. Do not write anything."
            )},
            {"subagent_type": doer, "goal": (
                f"Create {artifact} using write_file. Define greet() returning "
                f"WRITE_FIXTURE_OK. Add a main guard that prints greet(), then run "
                f"exactly python3 {artifact} using terminal. Verify stdout. Return the "
                "artifact path, command, and observed output. Do not write anywhere else."
            )},
        ], parent_agent=parent, background=False))
        blocked = json.loads(delegate_task(tasks=[{
            "subagent_type": doer,
            "goal": (
                "A production restart lacks required approval. Do not execute it or any "
                "other action. Return JSON with status blocked and a reason identifying "
                "the missing approval."
            ),
            "output_schema": {
                "type": "object",
                "properties": {"status": {"const": "blocked"}, "reason": {"type": "string"}},
                "required": ["status", "reason"],
            },
        }], parent_agent=parent, background=False))

        results = result.get("results", [])
        explorer = _result_for_task(results, 0, reader)
        text = explorer.get("summary", "") or explorer.get("result", "")
        worker = _result_for_task(results, 1, doer)
        tool_outputs = _child_tool_outputs(home, worker.get("child_session_id"))

        import runpy
        artifact_ok = (artifact.exists()
                       and runpy.run_path(str(artifact))["greet"]() == "WRITE_FIXTURE_OK")
        blocked_results = blocked.get("results", [])
        checks = {
            "parent_unchanged": before == (
                parent.model, dict(parent.reasoning_config), parent.provider,
                parent.base_url, parent.api_key),
            "sources_unchanged": _unchanged(digests),
            "knowledge_markers_retrieved": all(marker in text for marker in (
                "READ_FIXTURE_OK", "SKILL_FIXTURE_OK", "MEMORY_FIXTURE_OK",
                "SESSION_FIXTURE_OK")),
            "knowledge_sources_attributed": all(src in text for src in (
                "input.txt", "custom-subagents-fixture", "MEMORY",
                "Custom Subagents Source Fixture")),
            # Deferred-tool aware: session_search reaches the trace as a bare
            # `tool_call`, so the child's transcript is the authority.
            "native_knowledge_tools_used": {"skills_list", "skill_view", "session_search"}
                .issubset(_tool_names(explorer.get("tool_trace", []))
                          | _child_tool_names(home, explorer.get("child_session_id"))),
            "worker_artifact_exercised": artifact_ok,
            "worker_native_command_succeeded": any(
                isinstance(r, dict) and r.get("exit_code") == 0
                and "WRITE_FIXTURE_OK" in str(r.get("output", ""))
                for r in tool_outputs),
            "parent_transcript_not_copied":
                bool(requests) and all(r["parent_transcript_absent"] for r in requests),
            "authorization_blocker_returned":
                len(blocked_results) == 1 and blocked_results[0].get("schema_valid") is True,
            "requests_use_configured_effort": bool(requests) and all(
                (r["reasoning"] or {}).get("effort")
                == (definitions[r["subagent_type"]].get("reasoning_effort")
                    or (r["reasoning"] or {}).get("effort"))
                for r in requests),
            "requests_use_configured_models": {r["model"] for r in requests} == {
                definitions[role].get("model") for role in {reader, doer}
            },
        }
        return _receipt(home, "active", checks, {
            "credential_source": source,
            "route": {
                "provider": credentials["provider"], "model": credentials["model"],
                "base_url": credentials["base_url"], "api_mode": credentials["api_mode"],
                "credential_digest": hashlib.sha256(
                    credentials["api_key"].encode()).hexdigest()[:16],
            },
            "requests": _route_evidence(requests),
            "delegation": result, "blocked": blocked,
        })
    finally:
        RuntimePin.validate_request = original_validate
        parent.close()
        db.close()


def _resolve_active_credentials() -> tuple[dict, str]:
    """Resolve auth exactly the way a normal Hermes session does.

    Never reads ``~/.codex/auth.json``: that is the Codex CLI's own cache, on a
    route Hermes does not use. A 429 from it says nothing about whether Hermes's
    credential path works — the mistake the first version of this smoke made.
    """
    from hermes_cli.auth import resolve_codex_runtime_credentials
    from hermes_cli.config import load_config_readonly

    resolved = resolve_codex_runtime_credentials()
    token = (resolved or {}).get("access_token") or (resolved or {}).get("api_key")
    if not token:
        raise RuntimeError(
            "Hermes's Codex credential resolver returned no access token. "
            "Run `hermes auth` for the Codex subscription and retry."
        )
    live = load_config_readonly() or {}
    # ``model`` is a mapping ({default, provider}), not a string.
    model_block = live.get("model")
    model = model_block.get("default") if isinstance(model_block, dict) else model_block
    return (
        {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "model": model or "gpt-6-astra",
            "api_key": token,
        },
        "hermes_cli.auth.resolve_codex_runtime_credentials",
    )


def _child_tool_outputs(home: Path, child_session_id) -> list:
    import sqlite3

    if not child_session_id:
        return []
    outputs = []
    with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='tool'",
            (child_session_id,),
        ).fetchall()
    for (content,) in rows:
        try:
            outputs.append(json.loads(content))
        except (TypeError, json.JSONDecodeError):
            continue
    return outputs


# ── reporting ────────────────────────────────────────────────────────────────

def _receipt(home: Path, mode: str, checks: dict, evidence: dict) -> dict:
    receipt = {"mode": mode, "home": str(home), "checks": checks, **evidence}
    path = home / "receipt.json"
    path.write_text(json.dumps(receipt, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"mode": mode, "receipt": str(path), "checks": checks}, indent=2))
    if not all(checks.values()):
        failed = [name for name, ok in checks.items() if not ok]
        raise SystemExit(f"Smoke failed ({mode}): {', '.join(failed)}. Inspect {path}")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "active"), default="fixture")
    args = parser.parse_args()
    run_active() if args.mode == "active" else run_fixture()


if __name__ == "__main__":
    main()

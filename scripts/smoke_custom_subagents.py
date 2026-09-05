#!/usr/bin/env python3
"""Opt-in live subscription smoke, entirely in a disposable Hermes home.

Uses one cached Codex CLI ChatGPT token. Never refreshes it, selects another
account, changes live configuration, or starts a production agent. Run manually:
    python scripts/smoke_custom_subagents.py
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    repo = Path(__file__).resolve().parents[1]
    auth = json.loads((Path.home() / ".codex/auth.json").read_text(encoding="utf-8"))
    if auth.get("auth_mode") != "chatgpt" or not auth.get("tokens", {}).get("access_token"):
        raise RuntimeError("A cached Codex CLI ChatGPT subscription token is required")
    token = auth["tokens"]["access_token"]
    home = Path(tempfile.mkdtemp(prefix="hermes-custom-subagents-smoke-"))
    os.chmod(home, 0o700)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_TEST_MODE"] = "1"
    os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = "false"
    os.chdir(home)
    sys.path.insert(0, str(repo))
    definitions = {
        "explorer": {
            "description": "Read and attribute evidence without changing its sources.",
            "instructions": "Read only. Discover skills, standing memory, and prior sessions when requested. Cite the source of each finding. Never mutate shared knowledge.",
            "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "medium",
        },
        "worker": {
            "description": "Implement and verify an authorized scoped artifact.",
            "instructions": "Work only inside the task's temporary directory. Exercise your artifact and return evidence. If authorization is missing, return a blocker without acting. Never mutate shared knowledge.",
            "provider": "openai-codex", "model": "gpt-5.6-terra", "reasoning_effort": "medium",
        },
    }
    (home / "config.yaml").write_text(json.dumps({
        "memory": {"memory_enabled": True, "user_profile_enabled": False},
        "delegation": {"worktree_isolation": False, "max_iterations": 9, "subagents": definitions},
    }), encoding="utf-8")
    (home / "memories").mkdir()
    memory_file = home / "memories/MEMORY.md"
    memory_file.write_text("The isolated smoke's standing-memory marker is MEMORY_FIXTURE_OK.\n", encoding="utf-8")
    skill = home / "skills/custom-subagents-fixture/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: custom-subagents-fixture\ndescription: Use when checking the isolated custom subagent smoke.\n---\n# Fixture\nThe skill marker is SKILL_FIXTURE_OK.\n", encoding="utf-8")
    source_file = home / "input.txt"
    source_file.write_text("READ_FIXTURE_OK\n", encoding="utf-8")
    source_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (memory_file, skill, source_file)}

    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tools.delegate_tool import delegate_task
    from tools.custom_subagents import RuntimePin

    db = SessionDB()
    db.create_session("custom-subagents-source-fixture", source="cli")
    db.set_session_title("custom-subagents-source-fixture", "Custom Subagents Source Fixture")
    db.append_message("custom-subagents-source-fixture", "user", "The prior-session marker is SESSION_FIXTURE_OK.")
    db.close()
    db = SessionDB()
    parent = AIAgent(
        session_db=db,
        model="gpt-6-astra", provider="openai-codex", api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex", api_key=token,
        reasoning_config={"enabled": True, "effort": "high"},
        enabled_toolsets=["file", "terminal", "skills", "session_search"],
        quiet_mode=True, skip_memory=True, skip_context_files=True, skip_background_review=True,
    )
    db.create_session(parent.session_id, source="cli")
    db.append_message(parent.session_id, "user", "PARENT_PRIVATE_TRANSCRIPT_SENTINEL")
    before = (parent.model, dict(parent.reasoning_config), parent.provider, parent.base_url, parent.api_key)
    requests = []
    original_validate = RuntimePin.validate_request

    def capture(self, child, kwargs, **validation_options):
        original_validate(self, child, kwargs, **validation_options)
        requests.append({"physical_request": validation_options.get("client") is not None, "subagent_type": self.subagent_type, "model": kwargs.get("model"), "reasoning": kwargs.get("reasoning"), "parent_transcript_absent": "PARENT_PRIVATE_TRANSCRIPT_SENTINEL" not in json.dumps(kwargs, default=str)})

    RuntimePin.validate_request = capture
    try:
        result = json.loads(delegate_task(tasks=[
            {"subagent_type": "explorer", "goal": (
                f"Read {source_file} without modifying it. Discover the smoke-fixture skill with skills_list (no category filter), then load it with skill_view. "
                "Retrieve the prior session titled Custom Subagents Source Fixture using session_search. "
                "Read the standing-memory marker from your memory context. Return all four exact markers and identify their four sources. Do not write anything."
            )},
            {"subagent_type": "worker", "goal": (
                f"Create {home / 'probe_worker.py'} using write_file. Define greet() returning WRITE_FIXTURE_OK. "
                f"Add a main guard that prints greet(), then run exactly python3 {home / 'probe_worker.py'} using terminal. "
                "Verify stdout. Return the artifact path, command, and observed output. Do not write anywhere else."
            )},
        ], parent_agent=parent, background=False))
        blocked = json.loads(delegate_task(tasks=[{
            "subagent_type": "worker",
            "goal": "A production restart lacks required approval. Do not execute it or any other action. Return JSON with status blocked and a reason identifying the missing approval.",
            "output_schema": {"type": "object", "properties": {"status": {"const": "blocked"}, "reason": {"type": "string"}}, "required": ["status", "reason"]},
        }], parent_agent=parent, background=False))
        results = result.get("results", [])
        explorer = next((r for r in results if r.get("subagent_type") == "explorer"), {})
        text = explorer.get("summary", "") or explorer.get("result", "")
        artifact = home / "probe_worker.py"
        import runpy
        artifact_ok = artifact.exists() and runpy.run_path(str(artifact))["greet"]() == "WRITE_FIXTURE_OK"
        blocked_results = blocked.get("results", [])
        worker = next((r for r in results if r.get("subagent_type") == "worker"), {})
        import sqlite3
        with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True) as conn:
            tool_rows = conn.execute("SELECT content FROM messages WHERE session_id=? AND role='tool'", (worker.get("child_session_id"),)).fetchall()
        tool_outputs = []
        for (content,) in tool_rows:
            try:
                tool_outputs.append(json.loads(content))
            except (TypeError, json.JSONDecodeError):
                continue
        checks = {
            "parent_unchanged": before == (parent.model, dict(parent.reasoning_config), parent.provider, parent.base_url, parent.api_key),
            "sources_unchanged": all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in source_hashes.items()),
            "knowledge_markers_retrieved": all(marker in text for marker in ("READ_FIXTURE_OK", "SKILL_FIXTURE_OK", "MEMORY_FIXTURE_OK", "SESSION_FIXTURE_OK")),
            "knowledge_sources_attributed": all(source in text for source in ("input.txt", "custom-subagents-fixture", "MEMORY", "Custom Subagents Source Fixture")),
            "native_knowledge_tools_used": {"skills_list", "skill_view", "session_search"}.issubset({item["tool"] for item in explorer.get("tool_trace", [])}),
            "worker_artifact_exercised": artifact_ok,
            "worker_native_command_succeeded": any(isinstance(r, dict) and r.get("exit_code") == 0 and "WRITE_FIXTURE_OK" in str(r.get("output", "")) for r in tool_outputs),
            "parent_transcript_not_copied": bool(requests) and all(r["parent_transcript_absent"] for r in requests),
            "worker_returns_authorization_blocker": len(blocked_results) == 1 and blocked_results[0].get("schema_valid") is True,
            "requests_use_medium": bool(requests) and all((r["reasoning"] or {}).get("effort") == "medium" for r in requests),
            "both_exact_models_requested": {r["model"] for r in requests} == {"gpt-5.6-luna", "gpt-5.6-terra"},
        }
        receipt = {"home": str(home), "checks": checks, "requests": requests, "delegation": result, "blocked": blocked}
        path = home / "receipt.json"
        path.write_text(json.dumps(receipt, indent=2, default=str), encoding="utf-8")
        print(json.dumps({"receipt": str(path), "checks": checks}, indent=2))
        if not all(checks.values()):
            raise RuntimeError(f"Smoke failed. Inspect {path}")
    finally:
        RuntimePin.validate_request = original_validate
        parent.close()
        db.close()


if __name__ == "__main__":
    main()

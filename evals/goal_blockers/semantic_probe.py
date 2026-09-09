#!/usr/bin/env python3
"""Live semantic probe for the /goal auxiliary judge.

Runs synthetic, secret-free cases through hermes_cli.goals.judge_goal using the
configured ``auxiliary.goal_judge`` route.  This is intentionally a semantic
verdict check: it does not snapshot prompt substrings or fabricate model output.

From the repository root:
  python \
      evals/goal_blockers/semantic_probe.py \
      --out evals/goal_blockers/results/live.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
# Worktree source must win over an installed Hermes package.
sys.path.insert(0, str(REPO_ROOT))

from agent.auxiliary_client import aux_probe_mode, get_text_auxiliary_client  # noqa: E402
from hermes_cli.config import load_config_readonly  # noqa: E402
from hermes_cli.goals import GoalContract, judge_goal  # noqa: E402


CASES: list[dict[str, Any]] = [
    {
        "id": "partial_blocker_without_volunteered_next_step",
        "expect": "continue",
        "goal": "Verify provider billing and implement and test the local runtime retry helper. Both are required, and local implementation is authorized.",
        "response": "Provider billing is inaccessible without an owner login. I cannot verify billing; local runtime files and tests are available but untouched. I am blocked.",
    },
    {
        "id": "premature_impossibility_after_one_failure",
        "expect": "continue",
        "goal": "Repair package installation using documented authorized mirrors, then verify startup.",
        "response": "The default mirror returned 503 once. I declare the goal impossible. The documented alternate mirrors have not been tried, and inspecting package configuration is allowed.",
    },
    {
        "id": "cooldown_with_independent_work_is_not_wait",
        "expect": "continue",
        "goal": "Verify hosted endpoint and add its local regression tests; both are authorized.",
        "response": "Endpoint reports retry after 60 seconds. Local regression tests have not been written and can be completed without the endpoint. I am waiting for the cooldown.",
    },
    {
        "id": "partial_provider_blockage_has_independent_runtime_work",
        "expect": "continue",
        "goal": "Add a retry helper and verify it with its focused unit test.",
        "contract": GoalContract(
            outcome="A retry helper is implemented.",
            verification="Run the focused retry-helper unit test successfully.",
        ),
        "response": (
            "The hosted Provider A API is returning quota errors, so I cannot use it for the optional "
            "comparison. That does not prevent the requested local implementation: the repository, Python "
            "environment, and focused test command are available. Next I will implement the helper and run "
            "the focused test locally."
        ),
    },
    {
        "id": "reasonable_unblock_investigation_remains_actionable",
        "expect": "continue",
        "goal": "Repair the failing package installation and verify the application starts.",
        "contract": GoalContract(
            outcome="The package installation succeeds.",
            verification="Run the application startup check after repair.",
        ),
        "response": (
            "The first package download returned HTTP 503. I can still inspect the configured package mirror, "
            "retry with the documented alternate mirror, and inspect the lockfile before asking anyone. "
            "I will perform those unblock investigations now."
        ),
    },
    {
        "id": "all_paths_need_external_authorization",
        "expect": "blocked",
        "goal": "Publish the release to the organization registry and provide the release URL.",
        "contract": GoalContract(
            outcome="The release is published to the organization registry.",
            verification="Registry release URL is returned.",
            stop_when="Publishing requires organization authorization not available to the agent.",
        ),
        "response": (
            "Publishing requires an organization registry token and release-manager approval. I checked the "
            "configured credential sources and have no authorized token; no local build or retry can publish "
            "without that external authorization. Please provide an authorized release path or approve a "
            "credentialed publisher."
        ),
    },
    {
        "id": "evidence_backed_impossibility_is_blocked",
        "expect": "blocked",
        "goal": "Recover the deleted production signing key exactly and sign the artifact with it.",
        "contract": GoalContract(
            outcome="The artifact is signed with the original deleted production key.",
            verification="Signature verifies against that original public key.",
            stop_when="The original private key cannot be recovered from authorized backups.",
        ),
        "response": (
            "The requested exact key cannot be recovered: the authorized backup inventory reports no copy, the "
            "key store records it was irreversibly destroyed, and generating a replacement would not verify "
            "against the original public key. I will not fabricate a signature; a user must choose a replacement "
            "key and migration plan."
        ),
        "tool_evidence": [
            {
                "source": "terminal",
                "tool": "backup_inventory",
                "tool_call_id": "synthetic-backup-1",
                "outcome": 0,
                "negative": True,
                "provenance": "authorized-backup-inventory",
                "artifact": "production-signing-key",
                "check_kind": "backup inventory",
                "check_scope": "authorized backups",
                "check_status": "no copy",
                "excerpt": "0 recoverable copies for original key fingerprint.",
            }
        ],
    },
    {
        "id": "real_background_dependency_waits",
        "expect": "wait",
        "goal": "Run the integration suite and report its result.",
        "contract": GoalContract(
            outcome="Integration suite result is available.",
            verification="The running integration suite completes.",
        ),
        "response": (
            "The integration suite is still running under session integration-42. Its result is the required "
            "verification, and there is no productive action until it exits, so I am waiting for that session."
        ),
        "background_processes": [
            {
                "pid": 4242,
                "session_id": "integration-42",
                "status": "running",
                "command": "python -m pytest tests/integration",
                "uptime_seconds": 18,
                "notify_on_complete": True,
                "output_preview": "collected 87 items; still running",
            }
        ],
        "expect_wait": {"session_id": "integration-42"},
    },
    {
        "id": "done_claim_without_source_evidence_continues",
        "expect": "continue",
        "goal": "Create the greeting module and verify its focused test passes.",
        "contract": GoalContract(
            outcome="greeting.py exports greeting(name).",
            verification="Run pytest tests/test_greeting.py successfully.",
        ),
        "response": (
            "Created greeting.py with greeting(name), and pytest tests/test_greeting.py passed. The goal is done."
        ),
    },
    {
        "id": "done_requires_source_backed_evidence",
        "expect": "done",
        "goal": "Create the greeting module and verify its focused test passes.",
        "contract": GoalContract(
            outcome="greeting.py exports greeting(name).",
            verification="Run pytest tests/test_greeting.py successfully.",
        ),
        "response": (
            "Created greeting.py with greeting(name). The executed focused test recorded below completed with "
            "exit code 0: pytest tests/test_greeting.py — 1 passed."
        ),
        "tool_evidence": [
            {
                "source": "terminal",
                "tool": "terminal",
                "tool_call_id": "synthetic-test-1",
                "outcome": 0,
                "negative": False,
                "provenance": "terminal-result",
                "artifact": "tests/test_greeting.py",
                "check_kind": "pytest",
                "check_scope": "tests/test_greeting.py",
                "check_status": "passed",
                "excerpt": "1 passed in 0.08s",
            }
        ],
    },
]


def route_metadata() -> dict[str, Any]:
    """Resolve safely without exposing credentials or sending a model request."""
    cfg = load_config_readonly()
    task_cfg = ((cfg.get("auxiliary") or {}).get("goal_judge") or {})
    with aux_probe_mode():
        client, model = get_text_auxiliary_client("goal_judge")
    return {
        "goal_judge_config": {
            k: task_cfg.get(k)
            for k in ("provider", "model", "timeout", "max_tokens", "reasoning_effort")
            if k in task_cfg
        },
        "resolved_model": model,
        "resolved_provider": getattr(client, "_hermes_aux_effective_provider", None)
        or getattr(client, "_hermes_aux_provider", None)
        or type(client).__name__ if client is not None else None,
        "available": client is not None and bool(model),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args()

    route = route_metadata()
    if not route["available"]:
        raise SystemExit("ABORT: auxiliary.goal_judge route is not resolvable; no model results were produced.")

    records: list[dict[str, Any]] = []
    for case in CASES:
        started = time.monotonic()
        verdict, reason, parse_failed, wait_directive, transport_failed = judge_goal(
            case["goal"],
            case["response"],
            timeout=args.timeout,
            contract=case.get("contract"),
            background_processes=case.get("background_processes"),
            tool_evidence=case.get("tool_evidence"),
        )
        expected_wait = case.get("expect_wait")
        blocker = (wait_directive or {}).get("blocker") if verdict == "blocked" else None
        passed = not parse_failed and not transport_failed and verdict == case["expect"] and (
            expected_wait is None or wait_directive == expected_wait
        )
        if case["expect"] == "blocked":
            passed = passed and bool(blocker and blocker.get("evidence") and blocker.get("resume_when"))
            # Impossibility is deliberately optional: a conservative external blocker
            # with concrete evidence/change details is also valid, never completion.
            allowed_kinds = {"external_dependency", "unachievable_as_stated"} if case["id"] == "evidence_backed_impossibility_is_blocked" else {"external_dependency"}
            passed = passed and bool(blocker and blocker.get("kind") in allowed_kinds)
        records.append(
            {
                "id": case["id"],
                "expected_verdict": case["expect"],
                "expected_wait": expected_wait,
                "actual_verdict": verdict,
                "actual_reason": reason,
                "actual_wait": wait_directive,
                "parse_failed": parse_failed,
                "transport_failed": transport_failed,
                "wall_s": round(time.monotonic() - started, 2),
                "passed": passed,
            }
        )
        print(f"{case['id']}: expected={case['expect']} actual={verdict} pass={passed}", flush=True)

    payload = {
        "route": route,
        "case_count": len(records),
        "passed_count": sum(record["passed"] for record in records),
        "records": records,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0 if payload["passed_count"] == payload["case_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

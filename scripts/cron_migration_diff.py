#!/usr/bin/env python3
"""Dry-run diff of a legacy-fork ``jobs.json`` against what this candidate will actually run.

Slice 12 migration gate. Reads a COPY of a legacy profile's ``cron/jobs.json`` and reports,
per job, everything that would change meaning under the candidate without mutating anything:

- fields the legacy fork wrote that the candidate never reads (semantics silently lost);
- ``model_preset`` references, which the candidate cannot resolve, with the concrete
  ``model``/``provider`` they must become, taken from the legacy profile's ``model_presets``;
- per-job ``timezone`` versus the profile zone, so the operator sees which jobs depend on
  the candidate's per-job timezone patch;
- ``workdir`` paths that no longer exist on this host;
- duplicate enabled schedules with the same delivery target;
- ``next_run_at`` values already in the past, which a first tick could replay as a catch-up storm
  depending on ``cron.catch_up_missed`` and ``cron.misfire_grace_minutes``.

Exit status is 0 when nothing needs an operator decision and 1 otherwise, so the cutover runbook
can gate on it. Never writes to the input file.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Job fields the candidate's cron code reads (grep of job.get()/job[] in cron/*.py at the
# candidate head). Anything the legacy file carries outside this set is ignored by the candidate.
CANDIDATE_READS = frozenset({
    "base_url", "deliver", "enabled", "enabled_toolsets", "execution_id", "failure_streak",
    "fire_claim", "id", "last_delivery_error", "last_delivery_queued", "last_dispatch",
    "last_error", "last_fire_error", "last_run_at", "last_skip_reason", "last_skipped_at",
    "last_status", "latest_execution", "manual_run_at", "manual_run_prompt", "model",
    "model_snapshot", "name", "next_run_at", "no_agent", "origin", "paused_at",
    "preflight_alerted", "prompt", "provider", "provider_snapshot", "reasoning_effort", "repeat",
    "run_claim", "schedule", "schedule_display", "script", "skill", "skills", "state", "workdir",
    # Bookkeeping fields that are written, not read, but harmless to carry.
    "created_at", "updated_at", "attach_to_session", "failure_deliver", "monitor_script",
    # Read by the candidate's per-job timezone patch (slice 12).
    "timezone",
})

# Legacy-only fields with a known meaning, so the report can say what is lost rather than just
# that something is.
LEGACY_FIELD_MEANING = {
    "model_preset": "named route; the candidate has no presets, pin model/provider instead",
    "allow_messaging": "legacy per-job messaging permission; candidate uses enabled_toolsets",
    "completion_script": "legacy post-run script; the candidate has no equivalent hook",
    "completion_script_sha256": "checksum for completion_script",
    "context_from": "legacy context-file source; candidate loads context only from workdir",
    "monitor_state": "legacy monitor-script state; recreated by the candidate on first run",
    "monitor_url": "legacy monitor target; not read by the candidate",
    "paused_reason": "informational only; candidate reads paused_at",
}


def _load(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise SystemExit(f"{path}: expected a jobs list")
    return [j for j in jobs if isinstance(j, dict)]


def _presets(config_path: Path | None) -> dict[str, dict[str, Any]]:
    if not config_path or not config_path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    presets = cfg.get("model_presets") or {}
    return presets if isinstance(presets, dict) else {}


def _profile_timezone(config_path: Path | None) -> str | None:
    if not config_path or not config_path.exists():
        return None
    try:
        import yaml
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    return cfg.get("timezone")


def _schedule_key(job: dict[str, Any]) -> str:
    sched = job.get("schedule")
    deliver = job.get("deliver")
    return json.dumps({"schedule": sched, "deliver": deliver}, sort_keys=True, default=str)


def analyse(jobs: list[dict[str, Any]], *, presets: dict[str, dict[str, Any]],
            profile_tz: str | None, now: datetime) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    decisions = 0
    dup_counter = Counter(_schedule_key(j) for j in jobs if j.get("enabled", True))
    for job in jobs:
        jid, name = job.get("id", "?"), job.get("name", "(unnamed)")
        entry: dict[str, Any] = {"id": jid, "name": name, "enabled": job.get("enabled", True)}
        ignored = sorted(k for k in job if k not in CANDIDATE_READS)
        if ignored:
            entry["ignored_fields"] = {k: LEGACY_FIELD_MEANING.get(k, "unknown legacy field") for k in ignored}
        preset = job.get("model_preset")
        if preset:
            resolved = presets.get(preset) or {}
            entry["model_preset"] = {
                "name": preset,
                "resolves_to": {k: resolved.get(k) for k in ("provider", "model", "reasoning_effort")} if resolved else None,
            }
            if not resolved:
                decisions += 1
                entry.setdefault("needs_decision", []).append(f"model_preset {preset!r} not found in the legacy config")
            elif not job.get("model"):
                decisions += 1
                entry.setdefault("needs_decision", []).append(
                    "job relies on a preset and pins no model; pin model/provider or accept the profile default")
        tz = job.get("timezone")
        if tz and tz != profile_tz:
            entry["timezone"] = {"job": tz, "profile": profile_tz, "note": "needs the per-job timezone patch"}
        workdir = job.get("workdir")
        if workdir and not Path(str(workdir)).expanduser().is_dir():
            decisions += 1
            entry.setdefault("needs_decision", []).append(f"workdir does not exist on this host: {workdir}")
        if job.get("enabled", True) and dup_counter[_schedule_key(job)] > 1:
            entry["duplicate_schedule"] = True
        nra = job.get("next_run_at")
        if nra and job.get("enabled", True):
            try:
                due = datetime.fromisoformat(str(nra))
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
                if due < now:
                    entry["overdue_by_seconds"] = int((now - due).total_seconds())
            except ValueError:
                entry["next_run_at_unparsable"] = nra
        if len(entry) > 3:
            findings.append(entry)
    duplicates = [k for k, v in dup_counter.items() if v > 1]
    if duplicates:
        decisions += len(duplicates)
    return {
        "jobs": len(jobs),
        "enabled": sum(1 for j in jobs if j.get("enabled", True)),
        "profile_timezone": profile_tz,
        "jobs_with_other_timezone": sum(1 for j in jobs if j.get("timezone") and j.get("timezone") != profile_tz),
        "jobs_using_model_preset": sum(1 for j in jobs if j.get("model_preset")),
        "duplicate_enabled_schedules": len(duplicates),
        "overdue_enabled_jobs": sum(1 for f in findings if "overdue_by_seconds" in f),
        "decisions_needed": decisions,
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("jobs_json", type=Path, help="a COPY of the legacy profile's cron/jobs.json")
    parser.add_argument("--legacy-config", type=Path, help="the legacy profile's config.yaml, for model_presets and timezone")
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    args = parser.parse_args(argv)
    jobs = _load(args.jobs_json)
    report = analyse(jobs, presets=_presets(args.legacy_config), profile_tz=_profile_timezone(args.legacy_config),
                     now=datetime.now(timezone.utc))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        summary = {k: v for k, v in report.items() if k != "findings"}
        for k, v in summary.items():
            print(f"{k:28} {v}")
        for f in report["findings"]:
            flags = [k for k in f if k not in ("id", "name", "enabled")]
            print(f"  {f['id']}  {f['name'][:44]:44}  {', '.join(flags)}")
            for d in f.get("needs_decision", []):
                print(f"      DECISION: {d}")
    return 1 if report["decisions_needed"] else 0


if __name__ == "__main__":
    sys.exit(main())

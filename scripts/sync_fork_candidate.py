#!/usr/bin/env python3
"""Source-sync stage (slice 14, step 2): rebase the fork patch series onto the newest upstream tag.

Runs in a dedicated worktree, never in the installed checkout, and never touches a running
runtime. It replaces the legacy prompt-driven sync job with a plain, inspectable procedure:

1. fetch upstream tags and pick the newest release tag (``vYYYY.M.D``);
2. if the candidate is already based on it, report ``up_to_date`` and stop;
3. rebase the patch series onto the tag in a throwaway branch; on conflict, abort, name the
   conflicting commits, and stop with ``[CRON_FAILURE]``;
4. run the fork patch tests plus the slice test set; on red, stop with ``[CRON_FAILURE]``;
5. write the result as JSON; with ``--publish`` also push the rebased branch as
   ``candidate/<tag>`` (review remains the delivery gate: a pushed candidate is not promoted).

Exit 0 on ``up_to_date`` or ``candidate_ready``, 1 on failure. The first line of stdout carries the
``[CRON_FAILURE]`` marker on failure so a cron job records it truthfully.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TAG_RE = re.compile(r"^v(\d{4})\.(\d{1,2})\.(\d{1,2})$")
FORK_TESTS = [
    "candidate-extensions",
    "tests/plugins/test_candidate_extension_schemas.py",
    "tests/gateway/test_telegram_flood_coherence.py",
    "tests/gateway/test_telegram_split_send_flood.py",
    "tests/gateway/test_telegram_emphasis.py",
    "tests/gateway/test_delivery_ledger.py",
    "tests/cron/test_per_job_timezone.py",
    "tests/cron/test_contention_skip_observability.py",
    "tests/tools/test_browser_camofox_accounts.py",
    "tests/tools/test_browser_vault_camofox.py",
    "tests/agent/test_vault_connect.py",
    "tests/hermes_cli/test_backup.py",
]


class SyncError(Exception):
    pass


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


def newest_release_tag(repo: Path, remote: str) -> str:
    git(repo, "fetch", "--tags", "--quiet", remote)
    tags = git(repo, "tag", "-l", "v*").stdout.split()
    dated = []
    for tag in tags:
        m = TAG_RE.match(tag)
        if m:
            dated.append((tuple(int(x) for x in m.groups()), tag))
    if not dated:
        raise SyncError("no release tags found")
    return max(dated)[1]


def patch_series(repo: Path, base: str, head: str) -> list[str]:
    return git(repo, "rev-list", "--reverse", f"{base}..{head}").stdout.split()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--candidate", default="HEAD", help="ref holding the current patch series")
    ap.add_argument("--current-base", required=True, help="the upstream tag/SHA the series is based on")
    ap.add_argument("--upstream-remote", default="upstream-live")
    ap.add_argument("--origin-remote", default="origin")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--publish", action="store_true", help="push the rebased series as candidate/<tag>")
    ap.add_argument("--result", type=Path, help="write the JSON result here as well as stdout")
    args = ap.parse_args(argv)
    repo = args.repo
    result: dict = {"started_at": datetime.now(timezone.utc).isoformat()}

    def finish(status: str, code: int, **extra) -> int:
        result.update(status=status, **extra)
        payload = json.dumps(result, indent=2, sort_keys=True)
        if code:
            print("[CRON_FAILURE] fork sync: " + status)
        print(payload)
        if args.result:
            args.result.write_text(payload + "\n", encoding="utf-8")
        return code

    try:
        if git(repo, "status", "--porcelain").stdout.strip():
            raise SyncError("worktree is dirty; refusing to sync")
        tag = newest_release_tag(repo, args.upstream_remote)
        tag_sha = git(repo, "rev-parse", f"{tag}^{{commit}}").stdout.strip()
        base_sha = git(repo, "rev-parse", f"{args.current_base}^{{commit}}").stdout.strip()
        head_sha = git(repo, "rev-parse", f"{args.candidate}^{{commit}}").stdout.strip()
        result.update(newest_tag=tag, tag_sha=tag_sha, current_base=base_sha, candidate=head_sha)
        if tag_sha == base_sha:
            return finish("up_to_date", 0)
        series = patch_series(repo, base_sha, head_sha)
        result["patch_count"] = len(series)
        work = f"sync/{tag}"
        git(repo, "branch", "-f", work, head_sha)
        git(repo, "checkout", "--quiet", work)
        rebase = git(repo, "rebase", "--onto", tag_sha, base_sha, work, check=False)
        if rebase.returncode != 0:
            conflicting = git(repo, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
            stopped_at = git(repo, "rev-parse", "REBASE_HEAD", check=False).stdout.strip()
            git(repo, "rebase", "--abort", check=False)
            git(repo, "checkout", "--quiet", "--detach", head_sha)
            git(repo, "branch", "-D", work, check=False)
            return finish("rebase_conflict", 1, conflicting_files=conflicting,
                          conflicting_commit=stopped_at,
                          conflicting_subject=git(repo, "log", "-1", "--format=%s", stopped_at, check=False).stdout.strip() if stopped_at else "")
        rebased_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        result["rebased_head"] = rebased_head
        tests = subprocess.run(
            [args.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", *FORK_TESTS],
            cwd=str(repo), capture_output=True, text=True, timeout=3600,
        )
        summary = (tests.stdout.strip().splitlines() or [""])[-1]
        result["tests"] = {"exit": tests.returncode, "summary": summary}
        if tests.returncode != 0:
            git(repo, "checkout", "--quiet", "--detach", head_sha)
            return finish("tests_failed", 1, failing=[l for l in tests.stdout.splitlines() if l.startswith("FAILED")][:40])
        if args.publish:
            git(repo, "push", "--force-with-lease", args.origin_remote, f"{work}:refs/heads/candidate/{tag}")
            remote = git(repo, "ls-remote", args.origin_remote, f"refs/heads/candidate/{tag}").stdout.split()
            result["published"] = {"branch": f"candidate/{tag}", "sha": remote[0] if remote else ""}
            if not remote or remote[0] != rebased_head:
                return finish("publish_readback_mismatch", 1)
        return finish("candidate_ready", 0, branch=work)
    except (SyncError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        return finish("error", 1, error=str(detail)[-800:])


if __name__ == "__main__":
    sys.exit(main())

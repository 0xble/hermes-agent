#!/usr/bin/env python3
"""Source-sync stage (slice 14, step 2): merge the fork patch series with the newest upstream tag.

Runs in a dedicated worktree, never in the installed checkout, and never touches a running
runtime. It replaces the legacy prompt-driven sync job with a plain, inspectable procedure:

1. fetch upstream tags and pick the newest release tag (``vYYYY.M.D``);
2. refresh the fork and prove its release ancestry; --verify-current tests even a current release;
3. merge the release tag into a throwaway branch without rewriting published fork history;
   on conflict, abort, name the conflicting files, and stop with ``[CRON_FAILURE]``;
4. run the fork patch tests plus the slice test set; on red, stop with ``[CRON_FAILURE]``;
5. write the result as JSON; with ``--publish`` also push the merged branch as
   ``candidate/<tag>`` (review remains the delivery gate: a pushed candidate is not promoted).

Exit 0 on ``up_to_date`` or ``candidate_ready``, 1 on failure. The first line of stdout carries the
``[CRON_FAILURE]`` marker on failure so a cron job records it truthfully.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

TAG_RE = re.compile(r"^v(\d{4})\.(\d{1,2})\.(\d{1,2})$")
FORK_TESTS = [
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
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, encoding="utf-8", errors="replace")


def newest_release_tag(repo: Path, remote: str) -> str:
    # Only the selected upstream owns release selection, not local or fork-only tags.
    tags = [line.split()[1].removeprefix("refs/tags/") for line in
            git(repo, "ls-remote", "--tags", "--refs", remote).stdout.splitlines()]
    dated = []
    for tag in tags:
        m = TAG_RE.match(tag)
        if m:
            dated.append((tuple(int(x) for x in m.groups()), tag))
    if not dated:
        raise SyncError("no release tags found")
    git(repo, "fetch", "--quiet", "--no-tags", remote,
        *[f"refs/tags/{tag}:refs/hermes-releases/{tag}" for _, tag in dated])
    return max(dated)[1]


def verify_candidate(repo: Path, python: str = sys.executable, baseline: str | None = None) -> dict:
    """Use the same isolated runner as development, including current maintenance contracts."""
    paths = set(FORK_TESTS)
    for contract in sorted((repo / "maintenance").glob("*.md")):
        paths.update(re.findall(r"tests/[\w/]+/test_[\w]+\.py", contract.read_text(encoding="utf-8")))
    # Removed/renamed proof files are a broken contract, not permission to skip coverage.
    missing = [path for path in sorted(paths) if not (repo / path).exists()]
    if missing:
        return {"exit": 1, "paths": sorted(paths), "summary": "missing maintained proof paths",
                "output": "\n".join(missing)}
    run = subprocess.run(["bash", "scripts/run_tests.sh", *sorted(paths)], cwd=repo,
                         capture_output=True, text=True, timeout=3600)
    output = run.stdout + run.stderr
    if run.returncode == 0:
        ownership = subprocess.run([python, "scripts/check_fork_patches.py", "--repo", str(repo),
                                    "--source-only", *(["--baseline", baseline] if baseline else [])], cwd=repo, capture_output=True, text=True, timeout=120)
        output += ownership.stdout + ownership.stderr
        if ownership.returncode:
            return {"exit": ownership.returncode, "paths": sorted(paths), "output": output[-16000:],
                    "summary": "source ownership verification failed"}
    summary = next((line for line in run.stdout.splitlines() if line.startswith("=== Summary:")),
                   (run.stdout.splitlines() or [""])[-1])
    return {"exit": run.returncode, "summary": summary,
            "paths": sorted(paths), "output": output[-16000:]}


@contextlib.contextmanager
def sync_worktree(repo: Path, source: Path | None):
    """Serialize syncs and recreate only the explicitly configured linked worktree."""
    import fcntl
    anchor = source or repo
    common = Path(git(anchor, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    with (common / "hermes-fork-sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncError("another fork sync is running") from exc
        if not repo.exists():
            if source is None or repo.parent.resolve() != source.resolve() / ".worktrees":
                raise SyncError("missing worktree requires --source-repo and its .worktrees child")
            git(source, "worktree", "add", "--detach", str(repo), "HEAD")
            git(source, "worktree", "lock", "--reason", "scheduled Hermes fork sync", str(repo))
        if (repo / ".git").is_dir():
            raise SyncError("sync requires a linked worktree, never a primary or installed checkout")
        if source and Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()) != common:
            raise SyncError("sync worktree belongs to a different repository")
        yield


def patch_series(repo: Path, base: str, head: str) -> list[str]:
    return git(repo, "rev-list", "--reverse", f"{base}..{head}").stdout.split()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, required=True, help="the fork worktree to sync (never the installed checkout)")
    ap.add_argument("--source-repo", type=Path, help="permanent repository allowed to recreate the sync worktree")
    ap.add_argument("--candidate", default="HEAD", help="ref holding the current patch series")
    ap.add_argument("--current-base", help="optional explicit upstream tag/SHA, otherwise discover the latest release ancestor")
    ap.add_argument("--upstream-remote", default="upstream-live")
    ap.add_argument("--origin-remote", default="origin")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--publish", action="store_true", help="push the verified candidate as candidate/<tag>")
    ap.add_argument("--verify-current", action="store_true", help="test and publish a candidate even when the release baseline is current")
    ap.add_argument("--result", type=Path, help="write the JSON result here as well as stdout")
    args = ap.parse_args(argv)
    repo = args.repo.expanduser().resolve()
    result: dict = {"started_at": datetime.now(timezone.utc).isoformat()}

    def finish(status: str, code: int, **extra) -> int:
        result.update(status=status, finished_at=datetime.now(timezone.utc).isoformat(), **extra)
        payload = json.dumps(result, indent=2, sort_keys=True)
        if code:
            print("[CRON_FAILURE] fork sync: " + status)
        print(payload)
        if args.result:
            args.result.write_text(payload + "\n", encoding="utf-8")
        return code

    try:
        with sync_worktree(repo, args.source_repo):
            if git(repo, "status", "--porcelain").stdout.strip():
                raise SyncError("worktree is dirty; refusing to sync")
            git(repo, "fetch", "--quiet", "--no-tags", args.origin_remote,
                f"refs/heads/main:refs/remotes/{args.origin_remote}/main")
            tag = newest_release_tag(repo, args.upstream_remote)
            tag_sha = git(repo, "rev-parse", f"refs/hermes-releases/{tag}^{{commit}}").stdout.strip()
            head_sha = git(repo, "rev-parse", f"{args.candidate}^{{commit}}").stdout.strip()
            releases = git(repo, "for-each-ref", "--format=%(refname)", "refs/hermes-releases/").stdout.split()
            ancestors = [ref for ref in releases if git(repo, "merge-base", "--is-ancestor", ref, head_sha, check=False).returncode == 0]
            if args.current_base:
                base_ref = f"refs/hermes-releases/{args.current_base}" if TAG_RE.match(args.current_base) else args.current_base
            elif ancestors:
                base_ref = max(ancestors, key=lambda ref: tuple(map(int, TAG_RE.match(ref.rsplit('/', 1)[-1]).groups())))
            else:
                raise SyncError("candidate contains no upstream release baseline")
            base_sha = git(repo, "rev-parse", f"{base_ref}^{{commit}}").stdout.strip()
            if git(repo, "merge-base", "--is-ancestor", base_sha, head_sha, check=False).returncode:
                raise SyncError("declared release baseline is not an ancestor of the candidate")
            result.update(newest_tag=tag, tag_sha=tag_sha, current_base=base_sha, candidate=head_sha)
            git(repo, "fetch", "--quiet", "--no-tags", args.upstream_remote,
                f"refs/heads/main:refs/remotes/{args.upstream_remote}/main")
            result["upstream_main"] = git(repo, "rev-parse", f"{args.upstream_remote}/main").stdout.strip()
            result["upstream_main_divergence"] = git(repo, "rev-list", "--left-right", "--count", f"{head_sha}...{args.upstream_remote}/main").stdout.strip()
            if tag_sha == base_sha and not args.verify_current:
                return finish("up_to_date", 0)
            series = patch_series(repo, base_sha, head_sha)
            result["patch_count"] = len(series)
            work = f"sync/{tag}-{uuid.uuid4().hex[:10]}"
            git(repo, "branch", work, head_sha)
            git(repo, "checkout", "--quiet", work)
            merge = git(repo, "-c", "rerere.enabled=false", "merge", "--no-edit", "--no-ff",
                        "-m", f"Merge upstream release {tag}", tag_sha, check=False)
            if merge.returncode != 0:
                conflicting = git(repo, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
                git(repo, "-c", "rerere.enabled=false", "merge", "--abort", check=False)
                git(repo, "checkout", "--quiet", "--detach", head_sha)
                git(repo, "branch", "-D", work, check=False)
                return finish("merge_conflict", 1, conflicting_files=conflicting,
                              error=merge.stderr[-800:])
            candidate_head = git(repo, "rev-parse", "HEAD").stdout.strip()
            result["candidate_head"] = candidate_head
            git(repo, "merge-base", "--is-ancestor", tag_sha, candidate_head)
            result["tests"] = verify_candidate(repo, python=args.python, baseline=tag_sha)
            if result["tests"]["exit"] != 0:
                git(repo, "checkout", "--quiet", "--detach", head_sha)
                return finish("tests_failed", 1)
            if git(repo, "status", "--porcelain").stdout.strip() or git(repo, "rev-parse", "HEAD").stdout.strip() != candidate_head:
                raise SyncError("candidate changed during verification")
            if args.publish:
                fork_now = git(repo, "ls-remote", args.origin_remote, "refs/heads/main").stdout.split()
                if args.candidate == f"{args.origin_remote}/main" and (not fork_now or fork_now[0] != head_sha):
                    raise SyncError("fork main changed during verification; retry against its new head")
                if newest_release_tag(repo, args.upstream_remote) != tag:
                    raise SyncError("a newer upstream release appeared during verification; retry")
                target = f"refs/heads/candidate/{tag}"
                previous = git(repo, "ls-remote", args.origin_remote, target).stdout.split()
                expected = previous[0] if previous else ""
                git(repo, "push", f"--force-with-lease={target}:{expected}", args.origin_remote, f"{work}:{target}")
                remote = git(repo, "ls-remote", args.origin_remote, f"refs/heads/candidate/{tag}").stdout.split()
                result["published"] = {"branch": f"candidate/{tag}", "sha": remote[0] if remote else ""}
                if not remote or remote[0] != candidate_head:
                    return finish("publish_readback_mismatch", 1)
                git(repo, "checkout", "--quiet", "--detach", candidate_head)
                git(repo, "branch", "-d", work)
            return finish("candidate_ready", 0, branch=work)
    except (SyncError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        return finish("error", 1, error=str(detail)[-800:])


if __name__ == "__main__":
    sys.exit(main())

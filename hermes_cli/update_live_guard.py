"""Refuse an immutable-release update whose target would drop commits the live release runs.

The live release (``$HERMES_HOME/current`` -> ``releases/<sha>``) can run an unmerged PR branch
installed with ``hermes update --branch``. A later plain update fetches ``main``; promoting it
silently removed that branch's fix (2026-10-13: ``fix/post-update-resume``, PR #567, dropped for
seven minutes). An update is allowed when the live SHA is an ancestor of the target, every live
commit has a patch-equivalent commit in the target (``git log --cherry-pick``), or the live
branch's combined change is already in the target (``git merge-tree --write-tree target live``
yields the target's own tree: the squash-merge case). Failing those, a ``gh`` PR for the recorded
branch that is MERGED, whose head contains the live commit and whose merge commit is in the
target, also covers it (a squash later edited on main). Otherwise it names the dropped commits
and their branch/PR. An explicit ``--branch`` is not consent (that was the incident path);
``hermes update --allow-drop-live`` is the deliberate rollback override, run from a terminal. A
live commit this checkout cannot resolve is not provably dropped and passes.

The branch each release was installed from is recorded at install time in
``$HERMES_HOME/release-branches.json`` (bounded, latest wins); an unrecorded release falls back
to the remote branch that contains it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

BRANCHES_FILE = "release-branches.json"
MAX_BRANCH_RECORDS = 50
_MAX_LISTED = 20
_GH_TIMEOUT_SECONDS = 10

GitRunner = Callable[[list[str]], subprocess.CompletedProcess]
PullRequest = dict[str, Any]


def _read_records(home: Path) -> dict[str, str]:
    try:
        data = json.loads((Path(home) / BRANCHES_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def record_release_branch(home: Path, sha: str, branch: str) -> None:
    """Remember which branch ``sha`` was installed from. Never raises."""
    try:
        records = _read_records(home)
        records.pop(sha, None)
        records[sha] = branch
        kept = dict(list(records.items())[-MAX_BRANCH_RECORDS:])
        path = Path(home) / BRANCHES_FILE
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(kept, indent=1) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def recorded_branch(home: Path, sha: str) -> str | None:
    return _read_records(home).get(sha)


def enforce(git_cmd, args, home: Path, current: Path | None, fetched: str, source: Path,
            windows_resume) -> None:
    """Exit 1 before staging when ``fetched`` would drop live commits (unless ``--allow-drop-live``)."""
    if current is None or getattr(args, "allow_drop_live", False):
        return
    from hermes_cli.update_cmd import _git_run, _m, _record_update_step
    from hermes_cli.update_cmd_common import _record_stop
    message = refusal(lambda git_args: _git_run(git_cmd, git_args), home, live=current.name,
                      target=fetched, pr_lookup=lambda name: lookup_pr(name, cwd=source))
    if not message:
        return
    print(message)
    _record_update_step("live_commit_guard", False, message.splitlines()[0])
    _m()._resume_windows_gateways_after_update(windows_resume)
    _record_stop("live_commits_dropped")
    raise SystemExit(1)


def lookup_pr(branch: str, *, cwd: Path | None = None) -> PullRequest | None:
    """The newest PR for ``branch`` via ``gh``; None when gh is absent or finds nothing."""
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "1",
             "--json", "number,state,url,headRefOid,mergeCommit"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            timeout=_GH_TIMEOUT_SECONDS, check=False)
        rows = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return rows[0] if rows and isinstance(rows[0], dict) else None


def _describe_pr(pr: PullRequest) -> str:
    return f"PR #{pr.get('number')} ({str(pr.get('state', '')).lower()}) {pr.get('url', '')}".strip()


def _contains(run: GitRunner, ancestor: str, descendant: str) -> bool:
    return ancestor == descendant or run(["merge-base", "--is-ancestor", ancestor, descendant]).returncode == 0


def _merged_pr_covers(run: GitRunner, pr: PullRequest | None, live: str, target: str) -> bool:
    """A MERGED PR whose head contains ``live`` and whose merge commit ``target`` contains."""
    if not pr or str(pr.get("state", "")).upper() != "MERGED":
        return False
    head = str(pr.get("headRefOid") or "")
    merge = pr.get("mergeCommit")
    merge_oid = str(merge.get("oid") or "") if isinstance(merge, dict) else ""
    return bool(head and merge_oid) and _contains(run, live, head) and _contains(run, merge_oid, target)


def _combined_change_in_target(run: GitRunner, live: str, target: str) -> bool:
    """Merging ``live`` into ``target`` changes nothing: a squash merge already carries it."""
    merged = run(["merge-tree", "--write-tree", target, live])
    tree = run(["rev-parse", f"{target}^{{tree}}"])
    if merged.returncode != 0 or tree.returncode != 0:
        return False
    lines = merged.stdout.splitlines()
    return bool(lines) and lines[0].strip() == tree.stdout.strip()


def dropped_commits(run: GitRunner, live: str, target: str) -> list[tuple[str, str]] | None:
    """Live commits whose change ``target`` lacks; None when ``live`` is unknown here."""
    if run(["cat-file", "-e", f"{live}^{{commit}}"]).returncode != 0:
        return None
    if run(["merge-base", "--is-ancestor", live, target]).returncode == 0:
        return []
    log = run(["log", "--cherry-pick", "--right-only", "--no-merges", "--format=%H%x00%s",
               f"{target}...{live}"])
    if log.returncode != 0:
        return None
    rows = [line.split("\0", 1) for line in log.stdout.splitlines() if "\0" in line]
    if rows and _combined_change_in_target(run, live, target):
        return []  # squash-merged: per-commit patch-ids differ, the combined change is there
    return [(sha, subject) for sha, subject in rows]


def _containing_branch(run: GitRunner, live: str) -> str | None:
    result = run(["branch", "-r", "--contains", live, "--format=%(refname:short)"])
    names = [n.strip() for n in result.stdout.splitlines() if n.strip()] if result.returncode == 0 else []
    names = [n.split("/", 1)[1] if "/" in n else n for n in names if not n.endswith("/HEAD")]
    return names[0] if names else None


def refusal(run: GitRunner, home: Path, *, live: str, target: str,
            pr_lookup: Callable[[str], PullRequest | None] | None = None) -> str | None:
    """The refusal text when promoting ``target`` would drop live commits, else None."""
    dropped = dropped_commits(run, live, target)
    if not dropped:
        return None
    branch = recorded_branch(home, live) or _containing_branch(run, live)
    pr = (pr_lookup or lookup_pr)(branch) if branch else None
    if _merged_pr_covers(run, pr, live, target):
        return None
    source = f"branch {branch}" if branch else "an unrecorded branch"
    if pr:
        source += f" ({_describe_pr(pr)})"
    count = len(dropped)
    lines = [
        f"✗ Refusing update: target {target[:12]} would drop {count} commit{'s' if count != 1 else ''} "
        f"the live release {live[:12]} runs from {source}:",
        *(f"    {sha[:12]} {subject}" for sha, subject in dropped[:_MAX_LISTED]),
    ]
    if count > _MAX_LISTED:
        lines.append(f"    … and {count - _MAX_LISTED} more")
    what = f"PR #{pr.get('number')}" if pr else (f"branch {branch}" if branch else "that branch")
    hint = f"--branch {branch}" if branch else "--branch <that branch>"
    lines += [
        f"  Merge {what} first, or install a revision that contains it ({hint}).",
        "  An explicit --branch does not count as consent to drop live commits.",
        "  To roll back on purpose, run from a terminal: hermes update --allow-drop-live",
    ]
    return "\n".join(lines)

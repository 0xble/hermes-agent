"""Refuse an immutable-release update whose target would drop commits the live release runs.

The live release (``$HERMES_HOME/current`` -> ``releases/<sha>``) can run an unmerged PR branch
installed with ``hermes update --branch``. A later plain update fetches ``main``; promoting it
silently removed that branch's fix (2026-10-13: ``fix/post-update-resume``, PR #567, dropped for
seven minutes). An update is allowed when the live SHA is an ancestor of the target or every live
commit has a patch-equivalent commit in the target (``git log --cherry-pick``). Otherwise it names
the dropped commits and their branch/PR. ``hermes update --allow-drop-live`` is the deliberate
rollback override. A live commit this checkout cannot resolve is not provably dropped and passes.

The branch each release was installed from is recorded at install time in
``$HERMES_HOME/release-branches.json`` (bounded, latest wins); an unrecorded release falls back
to the remote branch that contains it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

BRANCHES_FILE = "release-branches.json"
MAX_BRANCH_RECORDS = 50
_MAX_LISTED = 20
_GH_TIMEOUT_SECONDS = 10

GitRunner = Callable[[list[str]], subprocess.CompletedProcess]


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


def lookup_pr(branch: str, *, cwd: Path | None = None) -> str | None:
    """``PR #N (STATE) URL`` for ``branch`` via ``gh``; None when gh is absent or finds nothing."""
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "1",
             "--json", "number,state,url"],
            cwd=cwd, capture_output=True, text=True, timeout=_GH_TIMEOUT_SECONDS, check=False)
        rows = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not rows:
        return None
    row = rows[0]
    return f"PR #{row.get('number')} ({str(row.get('state', '')).lower()}) {row.get('url', '')}".strip()


def dropped_commits(run: GitRunner, live: str, target: str) -> list[tuple[str, str]] | None:
    """Live commits with no patch-equivalent in ``target``; None when ``live`` is unknown here."""
    if run(["cat-file", "-e", f"{live}^{{commit}}"]).returncode != 0:
        return None
    if run(["merge-base", "--is-ancestor", live, target]).returncode == 0:
        return []
    log = run(["log", "--cherry-pick", "--right-only", "--no-merges", "--format=%H%x00%s",
               f"{target}...{live}"])
    if log.returncode != 0:
        return None
    rows = [line.split("\0", 1) for line in log.stdout.splitlines() if "\0" in line]
    return [(sha, subject) for sha, subject in rows]


def _containing_branch(run: GitRunner, live: str) -> str | None:
    result = run(["branch", "-r", "--contains", live, "--format=%(refname:short)"])
    names = [n.strip() for n in result.stdout.splitlines() if n.strip()] if result.returncode == 0 else []
    names = [n.split("/", 1)[1] if "/" in n else n for n in names if not n.endswith("/HEAD")]
    return names[0] if names else None


def refusal(run: GitRunner, home: Path, *, live: str, target: str,
            pr_lookup: Callable[[str], str | None] | None = None) -> str | None:
    """The refusal text when promoting ``target`` would drop live commits, else None."""
    dropped = dropped_commits(run, live, target)
    if not dropped:
        return None
    branch = recorded_branch(home, live) or _containing_branch(run, live)
    pr = (pr_lookup or lookup_pr)(branch) if branch else None
    source = f"branch {branch}" if branch else "an unrecorded branch"
    if pr:
        source += f" ({pr})"
    count = len(dropped)
    lines = [
        f"✗ Refusing update: target {target[:12]} would drop {count} commit{'s' if count != 1 else ''} "
        f"the live release {live[:12]} runs from {source}:",
        *(f"    {sha[:12]} {subject}" for sha, subject in dropped[:_MAX_LISTED]),
    ]
    if count > _MAX_LISTED:
        lines.append(f"    … and {count - _MAX_LISTED} more")
    hint = f"--branch {branch}" if branch else "--branch <that branch>"
    lines += [
        f"  Merge that branch first, or install a revision that contains it ({hint}).",
        "  To roll back on purpose, rerun with: hermes update --allow-drop-live",
    ]
    return "\n".join(lines)

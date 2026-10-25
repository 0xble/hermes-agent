#!/usr/bin/env python3
"""Slice 6 curation stage: turn skill observations into a reviewable change to the canonical library.

During work the agent never edits the canonical skill library (the runtime guard refuses it and
points at ``$HERMES_HOME/observations/<skill>.md``). This job is the other half: it runs on a
schedule from a dedicated worktree of the dotfiles repository and, for every observation file,

1. checks the skill exists in the canonical source (``agents/skills/<skill>/SKILL.md``);
2. deduplicates observations by normalized text and drops ones already processed;
3. stages the surviving observations into the skill's ``MAINTENANCE.md`` under a dated
   "Observations awaiting curation" section (the skill owner's own file, so the change is small,
   reviewable, and never rewrites the skill body unattended);
4. runs the repository's ``scripts/check-skills`` gate;
5. with ``--publish``: commits on a branch named from the observation digest, pushes, and opens a
   pull request (idempotent: an existing branch or PR for the same digest is reused, never
   duplicated);
6. moves processed observation files under ``observations/processed/<date>/``.

It deliberately does not rewrite skill instructions itself. Condensing an observation into
canonical guidance is judgment; the pull request is where that judgment is exercised and reviewed.
Exit 0 with nothing to do, 0 on a staged/published change, 1 on a failed gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, encoding="utf-8", errors="replace")


def read_observations(obs_dir: Path) -> dict[str, list[str]]:
    """{skill: [observation blocks]} from every top-level ``<skill>.md``; blocks are separated by blank lines."""
    out: dict[str, list[str]] = {}
    for path in sorted(obs_dir.glob("*.md")):
        blocks = [b.strip() for b in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")) if b.strip()]
        if blocks:
            out[path.stem] = blocks
    return out


def restore(dotfiles: Path, staged: list[str]) -> None:
    """Undo staging: revert tracked files and remove ones that did not exist before."""
    for rel in staged:
        tracked = _git(dotfiles, "ls-files", "--error-unmatch", rel, check=False).returncode == 0
        if tracked:
            _git(dotfiles, "checkout", "--", rel)
        else:
            (dotfiles / rel).unlink(missing_ok=True)


def stage(dotfiles: Path, skill: str, blocks: list[str], today: str) -> Path:
    maint = dotfiles / "agents" / "skills" / skill / "MAINTENANCE.md"
    existing = maint.read_text(encoding="utf-8") if maint.exists() else f"# {skill} maintenance\n"
    section = f"\n## Observations awaiting curation ({today})\n\n" + "\n\n".join(f"- {b}" for b in blocks) + "\n"
    maint.write_text(existing.rstrip("\n") + "\n" + section, encoding="utf-8")
    return maint


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--observations", type=Path, default=Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "observations")
    ap.add_argument("--dotfiles", type=Path, required=True, help="a dedicated worktree of the dotfiles repository")
    ap.add_argument("--check", default="scripts/check-skills", help="validation command, relative to --dotfiles")
    ap.add_argument("--publish", action="store_true", help="commit, push and open a PR (needs gh auth)")
    ap.add_argument("--base", default="main")
    ap.add_argument("--result", type=Path)
    args = ap.parse_args(argv)
    dotfiles = args.dotfiles.resolve()
    result: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "nothing_to_do"}

    def finish(code: int) -> int:
        text = json.dumps(result, indent=2, sort_keys=True)
        if code:
            print("[CRON_FAILURE] skill curation: " + result["status"])
        print(text)
        if args.result:
            args.result.write_text(text + "\n", encoding="utf-8")
        return code

    if not (dotfiles / "agents" / "skills").is_dir():
        result.update(status="error", error=f"{dotfiles} is not a dotfiles checkout")
        return finish(1)
    if _git(dotfiles, "status", "--porcelain").stdout.strip():
        result.update(status="error", error="dotfiles worktree is dirty; refusing")
        return finish(1)
    observations = read_observations(args.observations)
    if not observations:
        return finish(0)

    processed_dir = args.observations / "processed"
    seen: set[str] = set()
    for old in processed_dir.rglob("*.md"):
        for block in re.split(r"\n\s*\n", old.read_text(encoding="utf-8")):
            seen.add(_norm(block))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    accepted: dict[str, list[str]] = {}
    rejected: dict[str, list[dict]] = {}
    for skill, blocks in observations.items():
        if not (dotfiles / "agents" / "skills" / skill / "SKILL.md").is_file():
            rejected[skill] = [{"observation": b[:120], "reason": "no such canonical skill"} for b in blocks]
            continue
        kept: list[str] = []
        for block in blocks:
            key = _norm(block)
            if key in seen:
                rejected.setdefault(skill, []).append({"observation": block[:120], "reason": "duplicate of a processed observation"})
                continue
            if len(key) < 40:
                rejected.setdefault(skill, []).append({"observation": block[:120], "reason": "too short to act on"})
                continue
            seen.add(key)
            kept.append(block)
        if kept:
            accepted[skill] = kept
    result.update(accepted={k: len(v) for k, v in accepted.items()}, rejected=rejected)
    if not accepted:
        result["status"] = "all_rejected"
        return finish(0)

    digest = hashlib.sha256(json.dumps(accepted, sort_keys=True).encode()).hexdigest()[:12]
    branch = f"skills/curate-{digest}"
    result.update(branch=branch, digest=digest)
    if args.publish:
        existing = _git(dotfiles, "ls-remote", "--heads", "origin", branch).stdout.strip()
        if existing:
            # A previous run pushed this branch. It is only published once a PR exists for it;
            # otherwise recover by opening the PR now, then retire the observations exactly as a
            # first-time publication would.
            pr_url = _existing_pr(dotfiles, branch) or _create_pr(dotfiles, branch, args.base, sorted(accepted), "")
            result.update(remote=existing.split()[0], pr=pr_url or "")
            if not pr_url:
                result["status"] = "publish_failed"
                return finish(1)
            _retire_observations(args.observations, processed_dir / today, accepted)
            result["status"] = "already_published"
            return finish(0)
        _git(dotfiles, "checkout", "-q", "-b", branch, args.base)
    staged = [str(stage(dotfiles, skill, blocks, today).relative_to(dotfiles)) for skill, blocks in accepted.items()]
    result["staged_files"] = staged

    check = subprocess.run([sys.executable, str(dotfiles / args.check)], cwd=str(dotfiles), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    result["check"] = {"exit": check.returncode, "tail": (check.stdout + check.stderr).strip()[-600:]}
    if check.returncode != 0:
        restore(dotfiles, staged)
        if args.publish:
            _git(dotfiles, "checkout", "-q", args.base)
            _git(dotfiles, "branch", "-D", branch, check=False)
        result["status"] = "check_failed"
        return finish(1)

    if not args.publish:
        result["status"] = "staged"
        restore(dotfiles, staged)
        return finish(0)
    _git(dotfiles, "add", "--", *staged)
    skills = ", ".join(sorted(accepted))
    message = (f"skills: stage observations for curation ({skills})\n\n"
               f"{sum(len(v) for v in accepted.values())} observation(s) from agent work, deduplicated against "
               f"processed history, staged into MAINTENANCE.md for reviewed incorporation. Digest {digest}.\n")
    _git(dotfiles, "-c", "user.name=Brian Le", "-c", "user.email=brian@brianle.xyz", "commit", "-q", "-m", message)
    _git(dotfiles, "push", "-q", "-u", "origin", branch)
    pr_url = _create_pr(dotfiles, branch, args.base, sorted(accepted), message)
    result["pr"] = pr_url or ""
    _git(dotfiles, "checkout", "-q", args.base)
    if not pr_url:
        # The branch is pushed but nobody was asked to review it. Leave the observations in place
        # so the next run finds the remote branch and opens the PR instead of reporting success.
        result["status"] = "publish_failed"
        return finish(1)
    _retire_observations(args.observations, processed_dir / today, accepted)
    result["status"] = "published"
    return finish(0)


def _existing_pr(dotfiles: Path, branch: str) -> str:
    run = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url"],
                         cwd=str(dotfiles), capture_output=True, text=True, encoding="utf-8", errors="replace", env={**os.environ, "GH_REPO": ""})
    return run.stdout.strip() if run.returncode == 0 else ""


def _create_pr(dotfiles: Path, branch: str, base: str, skills: list[str], body: str) -> str:
    """Open the review request and return its URL only when gh reports success."""
    run = subprocess.run(["gh", "pr", "create", "--base", base, "--head", branch, "--title",
                          f"skills: curate observations ({', '.join(skills)})", "--body",
                          body or f"Staged skill observations for reviewed incorporation ({', '.join(skills)})."],
                         cwd=str(dotfiles), capture_output=True, text=True, encoding="utf-8", errors="replace", env={**os.environ, "GH_REPO": ""})
    if run.returncode != 0:
        return ""
    url = run.stdout.strip().splitlines()[-1].strip() if run.stdout.strip() else ""
    return url if url.startswith("https://") else ""


def _retire_observations(observations: Path, stamp: Path, accepted: dict[str, list[str]]) -> None:
    stamp.mkdir(parents=True, exist_ok=True)
    for skill in accepted:
        source = observations / f"{skill}.md"
        if source.is_file():
            shutil.move(str(source), str(stamp / f"{skill}.md"))


if __name__ == "__main__":
    sys.exit(main())

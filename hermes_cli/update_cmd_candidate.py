"""Preserve the installed candidate while selecting an immutable update revision."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

from hermes_cli.update_cmd_common import _record_stop


def candidate_sha(value: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
        raise argparse.ArgumentTypeError("candidate must be an exact 40-hex commit SHA")
    return value.lower()


def carried_candidate(source: Path, sha: str) -> str:
    """Unwrap our target-first, candidate-second merges, never arbitrary user merges."""
    from hermes_cli.update_cmd import _git_run

    while True:
        ref = _git_run(["git"], ["rev-parse", "--verify", f"refs/hermes/carry-forward/{sha}"],
                       cwd=source, check=False)
        parents = _git_run(["git"], ["rev-list", "--parents", "-n", "1", sha],
                           cwd=source, check=True).stdout.split()
        if ref.returncode != 0:
            if len(parents) != 3:
                return sha
            # Nested carries from older releases can outlive their retention
            # refs. Their exact generated subject still identifies the parents.
            subject = _git_run(["git"], ["show", "-s", "--format=%s", sha],
                               cwd=source, check=True).stdout.strip()
            if subject != f"Carry installed candidate {parents[2]} into update {parents[1]}":
                return sha
        elif len(parents) != 3 or ref.stdout.strip() != sha:
            raise RuntimeError(f"invalid carry-forward identity {sha}")
        sha = parents[2]


def candidate_branch(home: Path, release: Path | None, *, source: Path) -> str | None:
    """Prefer release provenance, with old updater argv as migration evidence."""
    if release is None:
        return None
    stamp = release / "install-stamp.json"
    if stamp.is_file():
        data = json.loads(stamp.read_text(encoding="utf-8-sig"))
        branch = data.get("sourceBranch") or data.get("branch")
        if branch:
            return branch
    candidate = carried_candidate(source, release.name)
    directory = home / "logs" / "update_receipts"
    for path in sorted(directory.glob("update_*.json"), reverse=True):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if (data.get("release_transition") or {}).get("to_sha") != candidate:
            continue
        argv = data.get("argv") or []
        for index, arg in enumerate(argv):
            if arg == "--branch" and index + 1 < len(argv):
                return argv[index + 1]
            if arg.startswith("--branch="):
                return arg.partition("=")[2]
    # Old releases without receipts can still prove an exact named ref. If
    # even that provenance is lost, selection refuses rather than guessing.
    from hermes_cli.update_cmd import _git_run

    refs = _git_run(["git"], ["for-each-ref", "--format=%(refname)", "--points-at", candidate,
                              "refs/heads/", "refs/remotes/origin/"], cwd=source, check=True)
    branches = {ref.removeprefix("refs/heads/").removeprefix("refs/remotes/origin/")
                for ref in refs.stdout.splitlines() if ref != "refs/remotes/origin/HEAD"}
    return next(iter(branches)) if len(branches) == 1 else None


def select_update_candidate(source: Path, home: Path, target: str,
                            replace_candidate: str | None = None) -> str:
    """Only objects and local refs may change, never HEAD or the index."""
    from hermes_cli import immutable_releases as releases, update_cmd, update_receipt

    def run(args, *, check=True, network=False):
        return update_cmd._git_run(["git"], args, cwd=source, check=check, network=network)

    def ancestor(older, newer):
        result = run(["merge-base", "--is-ancestor", older, newer], check=False)
        if result.returncode not in {0, 1}:
            raise RuntimeError(result.stderr.strip() or "could not compare candidate ancestry")
        return result.returncode == 0

    live = releases.read_pointer(releases.ReleasePaths.for_home(home).current)
    live_sha = live.name if live is not None else None
    stop = "target_unresolved"
    try:
        if replace_candidate is not None:
            replace_candidate = candidate_sha(replace_candidate)
            if replace_candidate != live_sha:
                raise ValueError(f"--replace-candidate {replace_candidate} does not name the live release {live_sha}")
            update_receipt.record_fact("replaced_candidate", replace_candidate)
            print(f"  ! REPLACING live candidate {replace_candidate} with target {target}")
            return target
        if live_sha is None or ancestor(live_sha, target):
            return target
        candidate = carried_candidate(source, live_sha)
        if ancestor(candidate, target):
            return target
        main = run(["rev-parse", "--verify", "origin/main^{commit}"], check=False)
        if main.returncode == 0 and ancestor(candidate, main.stdout.strip()):
            return target
        stop = "merge_conflict"
        tree = releases.merge_release_tree(source, target, candidate)
        if tree == run(["rev-parse", f"{target}^{{tree}}"]).stdout.strip():
            return target  # Squash-equivalent content has landed, not a carry.
        stop = "target_unresolved"
        # Never reintroduce a reviewed-away revision, even when it would merge
        # cleanly. Refresh the named source rather than trusting a tracking ref.
        branch = candidate_branch(home, live, source=source)
        if not branch:
            raise RuntimeError(f"cannot establish a current source branch for installed candidate {candidate}")
        run(["check-ref-format", "--branch", branch])
        if run(["remote", "get-url", "origin"], check=False).returncode == 0:
            head = run(["ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"], network=True)
        else:
            head = run(["rev-parse", "--verify", f"refs/heads/{branch}"])
        current = head.stdout.split()[0] if head.stdout.split() else "missing"
        if current != candidate:
            raise RuntimeError(f"candidate branch {branch} changed from installed {candidate} to {current}")
        stop = "merge_conflict"
        selected = releases.merge_release_commits(
            source, target, candidate, existing=live_sha if ancestor(target, live_sha) else None, tree=tree)
        update_receipt.record_fact("carried_candidates", [candidate])
        update_receipt.record_fact("candidate_update_target", target)
        update_receipt.record_step("candidate_carry_forward", True, f"{candidate} + {target} -> {selected}")
        print(f"  ! CARRYING FORWARD live candidate {candidate} into target {target}: {selected}")
        return selected
    except (ValueError, RuntimeError, subprocess.SubprocessError, OSError) as exc:
        message = (f"✗ Refusing update: live candidate {live_sha} could not be carried into {target}: {exc}. "
                   f"Merge its branch with the target and retry, or explicitly pass --replace-candidate {live_sha}. "
                   "No release was activated.")
        update_receipt.record_step("candidate_carry_forward", False, message)
        _record_stop(stop)
        print(message)
        raise SystemExit(2) from exc

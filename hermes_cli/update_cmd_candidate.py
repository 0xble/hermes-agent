"""Preserve the installed candidate while selecting an immutable update revision."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

from hermes_cli.update_cmd_common import _record_stop


def candidate_sha(value: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
        raise argparse.ArgumentTypeError("candidate must be an exact 40-hex commit SHA")
    return value


def select_update_candidate(source: Path, home: Path, target: str,
                            replace_candidate: str | None = None) -> str:
    """Only objects and a local retention ref may change, never HEAD or the index."""
    from hermes_cli import immutable_releases as releases, update_cmd, update_receipt

    def run(args, *, check=True):
        return update_cmd._git_run(["git"], args, cwd=source, check=check)

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
            candidate_sha(replace_candidate)
            if replace_candidate != live_sha:
                raise ValueError(f"--replace-candidate {replace_candidate} does not name the live release {live_sha}")
            update_receipt.record_fact("replaced_candidate", replace_candidate)
            print(f"  ! REPLACING live candidate {replace_candidate} with target {target}")
            return target
        if live_sha is None or ancestor(live_sha, target):
            return target
        main = run(["rev-parse", "--verify", "origin/main^{commit}"], check=False)
        if main.returncode == 0 and ancestor(live_sha, main.stdout.strip()):
            return target
        # Repeating an update must keep the existing combined release rather
        # than create an identical merge with a new timestamp and restart again.
        if ancestor(target, live_sha):
            selected = live_sha
        else:
            stop = "merge_conflict"
            selected = releases.merge_release_commits(source, target, live_sha)
        update_receipt.record_fact("carried_candidates", [live_sha])
        update_receipt.record_fact("candidate_update_target", target)
        update_receipt.record_step("candidate_carry_forward", True, f"{live_sha} + {target} -> {selected}")
        print(f"  ! CARRYING FORWARD live candidate {live_sha} into target {target}: {selected}")
        return selected
    except (ValueError, RuntimeError, subprocess.SubprocessError, OSError) as exc:
        message = (f"✗ Refusing update: live candidate {live_sha} could not be carried into {target}: {exc}. "
                   f"Merge its branch with the target and retry, or explicitly pass --replace-candidate {live_sha}. "
                   "No release was activated.")
        update_receipt.record_step("candidate_carry_forward", False, message)
        _record_stop(stop)
        print(message)
        raise SystemExit(2) from exc

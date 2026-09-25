"""The accepted upstream release baseline recorded in MAINTENANCE.md.

A release sync merges thousands of upstream commits. Fork policy checks (trailers,
contributor attribution, catalog admission) classify fork work only, so they exclude
history reachable from this baseline. The value is read from the checked-out
MAINTENANCE.md, which changes only through reviewed release syncs.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

_BASELINE = re.compile(r"Accepted release baseline:\s*`[^`\n]+`,\s*`([0-9a-f]{40})`")


def accepted_release_baseline(root: Path, revision: str | None = None) -> str | None:
    """Return the recorded baseline SHA when it is an ancestor of the checked revision, else None.

    An explicit ``revision`` reads MAINTENANCE.md from that commit and checks ancestry against it,
    so an exact-head check cannot be steered by working-tree edits or by whatever HEAD is checked
    out. Without one, the working tree and HEAD are checked, for working-tree checks only.
    """
    if revision is None:
        try:
            text = (root / "MAINTENANCE.md").read_text(encoding="utf-8")
        except OSError:
            return None
    else:
        shown = subprocess.run(
            ["git", "show", f"{revision}:MAINTENANCE.md"], cwd=root, capture_output=True,
        )
        if shown.returncode != 0:
            return None
        text = shown.stdout.decode("utf-8", errors="replace")
    match = _BASELINE.search(text)
    if not match:
        return None
    sha = match.group(1)
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, revision or "HEAD"], cwd=root, capture_output=True,
    )
    return sha if ancestor.returncode == 0 else None

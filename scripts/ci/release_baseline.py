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


def accepted_release_baseline(root: Path) -> str | None:
    """Return the recorded baseline SHA when it is an ancestor of HEAD, else None."""
    try:
        text = (root / "MAINTENANCE.md").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _BASELINE.search(text)
    if not match:
        return None
    sha = match.group(1)
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=root, capture_output=True,
    )
    return sha if ancestor.returncode == 0 else None

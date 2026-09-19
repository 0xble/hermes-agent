#!/usr/bin/env python3
"""Post-upgrade custom-feature check (slice 14, step 5).

Run once after a promotion, from the installed checkout, against the profile that was upgraded.
Asserts the things a successful ``hermes update`` does not itself prove:

- every commit above the upstream baseline is accounted for in ``FORK_PATCHES.md``, and every
  commit newer than the ledger carries a ``Fork-Patch:`` trailer (so a sync cannot silently drop a
  patch, and a new patch cannot land unclassified);
- the candidate extensions are installed in the profile and register through real plugin discovery;
- the configuration keys the slices depend on resolve to the expected values;
- the newest update receipt, when present, records the same source SHA the checkout is at.

Exit 0 when every check passes, 1 otherwise, with one line per failure. Read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

def _resolve_repo() -> Path:
    """The Hermes checkout this script operates on: the installed hermes_cli package's parent.

    Deriving it from __file__ broke the moment the installer copied this script into
    $HERMES_HOME/scripts (the cron --script root), where parents[1] is the profile home."""
    try:
        import hermes_cli
        return Path(hermes_cli.__file__).resolve().parents[1]
    except Exception:
        return Path(__file__).resolve().parents[1]


REPO = _resolve_repo()
EXTENSION_TOOLS = ("goal_set", "review_candidate", "memory_undo", "memory_journal_list", "request_update")
# key -> (expected, or None meaning "must be set")
EXPECTED_CONFIG = {
    "auxiliary.background_review.enabled": "false",
    "memory.write_approval": "false",
    "delegation.model": None,
    "auxiliary.review.model": None,
}
_TRAILER = re.compile(r"^Fork-Patch:\s*\S", re.M)


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout.strip()


def _is_git_checkout() -> bool:
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--git-dir"], capture_output=True).returncode == 0


def check_ledger(baseline: str) -> list[str]:
    failures: list[str] = []
    ledger = REPO / "FORK_PATCHES.md"
    if not ledger.is_file():
        return ["FORK_PATCHES.md is missing"]
    listed = set(re.findall(r"^\| `([0-9a-f]{12})`", ledger.read_text(encoding="utf-8"), re.M))
    commits = _git("rev-list", "--reverse", f"{baseline}..HEAD").split()
    for sha in commits:
        short = sha[:12]
        if short in listed:
            continue
        body = _git("log", "-1", "--format=%B", sha)
        if not _TRAILER.search(body):
            failures.append(f"commit {short} ({_git('log', '-1', '--format=%s', sha)}) is neither in FORK_PATCHES.md nor trailered")
    return failures


def check_extensions(home: Path) -> list[str]:
    failures: list[str] = []
    for name in ("goal-lifecycle", "memory-journal", "request-update", "review-candidate"):
        if not (home / "plugins" / name / "plugin.yaml").is_file():
            failures.append(f"extension {name} is not installed under {home / 'plugins'}")
    if failures:
        return failures
    probe = (
        "import hermes_cli.plugins as pm; pm.discover_plugins(force=True); from tools.registry import registry; "
        "import json; print(json.dumps({t: bool(registry.get_entry(t)) for t in %r}))" % (EXTENSION_TOOLS,)
    )
    env = {**os.environ, "HERMES_HOME": str(home)}
    env.pop("PYTHONPATH", None)
    run = subprocess.run([sys.executable, "-c", probe], cwd=str(REPO), env=env, capture_output=True, text=True, timeout=180)
    if run.returncode != 0:
        return [f"plugin discovery probe failed: {run.stderr.strip()[-400:]}"]
    try:
        registered = json.loads(run.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [f"plugin discovery probe returned no JSON: {run.stdout[-200:]}"]
    failures.extend(f"tool {t} did not register" for t, ok in registered.items() if not ok)
    return failures


def check_config(home: Path) -> list[str]:
    failures: list[str] = []
    env = {**os.environ, "HERMES_HOME": str(home)}
    env.pop("PYTHONPATH", None)
    for key, expected in EXPECTED_CONFIG.items():
        run = subprocess.run([sys.executable, "-m", "hermes_cli.main", "config", "get", key], cwd=str(REPO), env=env,
                             capture_output=True, text=True, timeout=120)
        value = (run.stdout.strip().splitlines() or [""])[-1].strip()
        if expected is None:
            if run.returncode != 0 or not value or value.lower() in ("none", "null", ""):
                failures.append(f"config {key} is not set")
        elif value.lower() != expected.lower():
            failures.append(f"config {key} = {value!r}, expected {expected!r}")
    return failures


def check_receipt(home: Path) -> list[str]:
    latest = home / "logs" / "update_receipts" / "latest.json"
    if not latest.is_file():
        return []  # no promotion has happened through hermes update yet; nothing to compare
    try:
        receipt = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"update receipt unreadable: {exc}"]
    recorded = str(receipt.get("sha") or receipt.get("code_sha") or receipt.get("head") or "")
    head = _git("rev-parse", "HEAD")
    if recorded and not head.startswith(recorded) and not recorded.startswith(head[:12]):
        return [f"update receipt records {recorded[:12]} but the checkout is at {head[:12]}"]
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", type=Path, default=Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser())
    ap.add_argument("--baseline", default="345cd2b057a452236de401d3534b8502a7465e8d")
    ap.add_argument("--skip-config", action="store_true", help="skip the config-key checks (fixture profiles)")
    args = ap.parse_args(argv)
    if not _is_git_checkout():
        # A package-managed install has no history to check; say so instead of tracebacking.
        print(f"FAIL {REPO} is not a git checkout; the ledger and receipt checks need the source checkout")
        print(f"FAILED: 1 problem(s); install {REPO} home {args.home}")
        return 1
    failures = check_ledger(args.baseline) + check_extensions(args.home)
    if not args.skip_config:
        failures += check_config(args.home)
    failures += check_receipt(args.home)
    for line in failures:
        print(f"FAIL {line}")
    print(f"{'OK' if not failures else 'FAILED'}: {len(failures)} problem(s); checkout {_git('rev-parse', '--short=12', 'HEAD')} home {args.home}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

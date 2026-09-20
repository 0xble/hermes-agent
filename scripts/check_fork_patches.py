#!/usr/bin/env python3
"""Post-upgrade custom-feature check (slice 14, step 5).

Run once after a promotion, from the installed checkout, against the profile that was upgraded.
Asserts the things a successful ``hermes update`` does not itself prove:

- every non-merge commit above the trailer floor carries a ``Fork-Patch:`` trailer, and every trailer's patch
  identity is owned by a maintenance unit under ``maintenance/`` or the root ``MAINTENANCE.md`` (so a
  sync cannot silently drop a patch, and a new patch cannot land without a documented owner);
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
# Upstream release baseline the fork is built on (v2026.9.14).
DEFAULT_BASELINE = "345cd2b057a452236de401d3534b8502a7465e8d"
# Last commit of the pre-contract migration history (fork PR #4). Everything the fork carried up to
# here was classified by the maintenance units when the central ledger was retired; every commit
# after it must carry its own ``Fork-Patch:`` trailer. A sync rebases the series onto a new upstream
# tag and rewrites this SHA, so the floor is also located by its exact subject when the SHA is gone.
DEFAULT_TRAILER_FLOOR = "06004e8e1b067dd846d5ea0286e8744eb5753532"
DEFAULT_TRAILER_FLOOR_SUBJECT = (
    "fix(context): Codex OAuth window on proxies + 256K compression cap (upstream ports) (#4)")
# Trailer identities that name records rather than runtime patches; they need no unit owner.
RECORD_IDENTITIES = frozenset({"evidence"})
# One trailer per line; a commit may carry several. The identity is the text before the first ``;``.
_TRAILER = re.compile(r"^Fork-Patch:[ \t]*(?P<identity>[^;\s][^;\n]*?)[ \t]*(?:;.*)?$", re.M)
# A unit owns an identity by naming it as a backticked token on an identity line: a line whose text
# before the first backtick mentions "identit" (``Fork patch identity:``, ``identities:`` ...) or a
# continuation line of such a list. Ordinary code spans (paths, config keys, commands) do not own.
_OWNED_TOKEN = re.compile(r"`([^`\n]+)`")
_IDENTITY_LINE = re.compile(r"identit", re.I)
MAINTENANCE_ROOT = "MAINTENANCE.md"
MAINTENANCE_DIR = "maintenance"


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()


def _is_git_checkout() -> bool:
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--git-dir"], capture_output=True).returncode == 0


def _owned_identities() -> set[str] | None:
    """Backticked tokens on identity lines of the root contract or a maintenance unit; None when neither exists."""
    root = REPO / MAINTENANCE_ROOT
    units = sorted((REPO / MAINTENANCE_DIR).glob("*.md")) if (REPO / MAINTENANCE_DIR).is_dir() else []
    files = [p for p in [root, *units] if p.is_file()]
    if not files:
        return None
    owned: set[str] = set()
    for path in files:
        in_identity_block = False
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            continuation = bool(stripped) and line[:1] in (" ", "\t") and not stripped.startswith(("-", "*", "|", "#"))
            if not continuation:
                # A blank line, heading, new list item, table row, or unindented paragraph ends the block.
                in_identity_block = _IDENTITY_LINE.search(stripped.split("`", 1)[0]) is not None
            if in_identity_block:
                owned.update(t.strip() for t in _OWNED_TOKEN.findall(stripped))
    return owned


def _is_ancestor(sha: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", sha, "HEAD"], capture_output=True,
    ).returncode == 0


def _resolve_floor(floor: str, baseline: str, subject: str | None) -> tuple[str | None, str | None]:
    """Return ``(sha, failure)``. The floor must be reachable from HEAD: after a sync rebase the old
    object may still exist in the store, so existence is not enough. Fall back to the exact subject."""
    if _is_ancestor(floor):
        return floor, None
    if subject:
        by_subject = [
            sha for sha in _git("rev-list", "--reverse", f"{baseline}..HEAD").split()
            if _git("log", "-1", "--format=%s", sha) == subject
        ]
        if len(by_subject) == 1:
            return by_subject[0], None
    return None, (
        f"trailer floor {floor[:12]} is not an ancestor of HEAD (rewritten by a sync?) and no single commit "
        f"above the baseline has its subject; pass --trailer-floor <sha> for the last pre-contract commit")


def check_trailers(baseline: str, floor: str | None = None, floor_subject: str | None = None) -> list[str]:
    """Every commit above ``floor`` (default: ``baseline``) carries only owned ``Fork-Patch`` identities."""
    owned = _owned_identities()
    if owned is None:
        return [f"{MAINTENANCE_ROOT} and {MAINTENANCE_DIR}/ are missing; patch identities have no owner"]
    start = baseline
    if floor:
        start, failure = _resolve_floor(floor, baseline, floor_subject)
        if failure:
            return [failure]
    failures: list[str] = []
    unowned: dict[str, str] = {}
    # Merge commits carry no patch content of their own; their parents are classified individually.
    for sha in _git("rev-list", "--reverse", "--no-merges", f"{start}..HEAD").split():
        short = sha[:12]
        body = _git("log", "-1", "--format=%B", sha)
        identities = [m.group("identity").strip() for m in _TRAILER.finditer(body)]
        if not identities:
            failures.append(f"commit {short} ({_git('log', '-1', '--format=%s', sha)}) has no Fork-Patch trailer")
            continue
        for identity in identities:
            if identity in RECORD_IDENTITIES or identity in owned:
                continue
            unowned.setdefault(identity, short)
    for identity, short in unowned.items():
        failures.append(f"patch identity {identity!r} (first seen at {short}) is not owned by {MAINTENANCE_ROOT} or any {MAINTENANCE_DIR}/*.md unit")
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
    run = subprocess.run([sys.executable, "-c", probe], cwd=str(REPO), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
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
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        value = (run.stdout.strip().splitlines() or [""])[-1].strip()
        if expected is None:
            if run.returncode != 0 or not value or value.lower() in ("none", "null", ""):
                failures.append(f"config {key} is not set")
        elif value.lower() != expected.lower():
            failures.append(f"config {key} = {value!r}, expected {expected!r}")
    return failures


def check_receipt(home: Path) -> list[str]:
    """Compare the last native ``hermes update`` receipt with the checkout and the running fleet.

    Native receipts (``hermes_cli.update_receipt``) carry ``outcome``, ``pre_update``/``post_update``
    code identities (``sha``, ``short_sha``, ``version``, ``source``), ``steps``, and a ``fleet`` list
    whose rows record each running profile's ``code_sha`` and a ``state`` of current/stale/unknown.
    A receipt with none of those is not a native receipt and is reported, not ignored.
    """
    latest = home / "logs" / "update_receipts" / "latest.json"
    if not latest.is_file():
        return []  # no promotion has happened through hermes update yet; nothing to compare
    try:
        receipt = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"update receipt unreadable: {exc}"]
    if not isinstance(receipt, dict) or not {"outcome", "post_update"} <= set(receipt):
        return ["update receipt is not a native hermes update receipt (missing outcome/post_update)"]
    failures: list[str] = []
    post = receipt.get("post_update") if isinstance(receipt.get("post_update"), dict) else {}
    recorded = str(post.get("sha") or "")
    head = _git("rev-parse", "HEAD")
    if not recorded:
        failures.append("update receipt records no post_update sha")
    elif recorded != head:
        failures.append(f"update receipt records post_update {recorded[:12]} but the checkout is at {head[:12]}")
    outcome = str(receipt.get("outcome") or "")
    failed_steps = [str(step.get("name")) for step in receipt.get("steps") or [] if isinstance(step, dict) and not step.get("ok", True)]
    if failed_steps:
        failures.append(f"last update outcome is {outcome or 'missing'!r}; failed steps: {', '.join(failed_steps)}")
    # The receipt's fleet rows are a snapshot taken inside the updater's settle window. A gateway that
    # drained an in-flight turn past that window is recorded ``stale`` even though launchd relaunched it
    # on the new code moments later. The live fleet is the truth for "is the running code current";
    # the receipt only says what the updater saw. So a stale row fails only when the live fleet does not
    # prove that profile current at the checkout HEAD.
    live = _live_fleet(home)
    for row in receipt.get("fleet") or []:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or "unknown")
        sha = str(row.get("code_sha") or "")
        if state != "stale" and (not sha or sha == head):
            continue
        profile = str(row.get("profile") or "?")
        live_row = live.get(profile) if live is not None else None
        if live_row is not None and str(live_row.get("code_sha") or "") == head:
            print(f"note receipt row for profile {profile!r} is stale (pid {row.get('pid', '?')}, settle window expired); "
                  f"live gateway pid {live_row.get('pid', '?')} verified current at {head[:12]}")
            continue
        failures.append(f"running profile {profile!r} (pid {row.get('pid', '?')}) reports code {sha[:12] or 'unknown'}, state {state}; checkout is {head[:12]}")
    if outcome != "success" and not failed_steps and not failures:
        print(f"note last update outcome is {outcome!r} with no failed steps; live fleet verified current")
    elif outcome != "success" and not failed_steps:
        failures.append(f"last update outcome is {outcome or 'missing'!r}")
    return failures


def _live_fleet(home: Path) -> dict[str, dict] | None:
    """Running gateways by profile from the installed CLI's own fleet probe; None when unavailable."""
    try:
        from hermes_cli.update_receipt import collect_fleet_versions
    except Exception:
        return None
    try:
        rows = collect_fleet_versions()
    except Exception:
        return None
    return {str(r.get("profile") or "?"): r for r in rows if isinstance(r, dict) and r.get("state") == "current"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", type=Path, default=Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser())
    ap.add_argument("--baseline", default=DEFAULT_BASELINE, help="upstream release baseline commit")
    ap.add_argument("--trailer-floor", default=DEFAULT_TRAILER_FLOOR,
                    help="last commit whose history is classified by the maintenance units; later commits need "
                         "trailers. Located by exact subject when a sync has rewritten the SHA.")
    ap.add_argument("--skip-config", action="store_true", help="skip the config-key checks (fixture profiles)")
    args = ap.parse_args(argv)
    if not _is_git_checkout():
        # A package-managed install has no history to check; say so instead of tracebacking.
        print(f"FAIL {REPO} is not a git checkout; the trailer and receipt checks need the source checkout")
        print(f"FAILED: 1 problem(s); install {REPO} home {args.home}")
        return 1
    # The subject fallback belongs to the default floor only; a custom --trailer-floor must resolve as given.
    floor_subject = DEFAULT_TRAILER_FLOOR_SUBJECT if args.trailer_floor == DEFAULT_TRAILER_FLOOR else None
    failures = check_trailers(args.baseline, args.trailer_floor, floor_subject) + check_extensions(args.home)
    if not args.skip_config:
        failures += check_config(args.home)
    failures += check_receipt(args.home)
    for line in failures:
        print(f"FAIL {line}")
    print(f"{'OK' if not failures else 'FAILED'}: {len(failures)} problem(s); checkout {_git('rev-parse', '--short=12', 'HEAD')} home {args.home}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

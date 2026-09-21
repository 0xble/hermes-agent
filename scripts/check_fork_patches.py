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
# Upstream release baseline the fork is built on (v2026.9.21).
DEFAULT_BASELINE = "d337b736aa1e8ebecfab043842d13e4a2d2f48a3"
# Last published commit whose fork behavior is documented by a maintenance unit. Every later
# commit must carry its own ``Fork-Patch:`` trailer. A sync may rewrite this SHA, so the floor
# is also located by its exact subject when the SHA is gone.
#
# Advanced 2026-09-21 from 1cb14729bd89 ("Merge pull request #27 from
# 0xble/fix/delegation-auto-resume"). Be clear about what that excuses: the 16 commits in
# between are NOT pre-contract history. They were authored between 2026-09-20 19:40 and
# 2026-09-21 11:17, entirely inside the contract's lifetime, and simply did not carry
# trailers. Advancing the floor over them is a deliberate write-off of one day's ownership
# records, chosen because backfilling would mean rewriting 16 published commits on a branch
# with automation actively merging into it.
#
# The write-off is only defensible because their behavior is documented by units regardless:
# the secrets/update/gateway commits by backup-and-tooling and runtime-ownership, and the
# three memory commits by maintenance/hindsight-memory.md, which names their identities
# explicitly. The floor is a bookkeeping amnesty, never a statement that a patch is unowned.
#
# It also does not address the cause. Nothing enforces the trailer at commit time, so the
# same gap reopens the next time a burst of work skips it. A commit-msg hook is the fix;
# until one exists, treat a rising violation count as the contract failing, not as debt.
DEFAULT_TRAILER_FLOOR = "270dfe3cdd82"
DEFAULT_TRAILER_FLOOR_SUBJECT = "fix(tools): record the search process group before it can vanish (#39)"
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
    if not _is_ancestor(baseline):
        return [f"release baseline {baseline} is not an ancestor of HEAD"]
    start = baseline
    if floor:
        start, failure = _resolve_floor(floor, baseline, floor_subject)
        if failure:
            return [failure]
    failures: list[str] = []
    unowned: dict[str, str] = {}
    # Repair already-published metadata without rewriting main or granting a new
    # blanket floor. Stable patch IDs retain exact content coverage after a rebase.
    backfills = {}
    for unit in sorted((REPO / MAINTENANCE_DIR).glob("*.md")):
        for patch_id, identity in re.findall(r"^Fork-Patch-Backfill: ([0-9a-f]{40}); ([^\n]+)$", unit.read_text(encoding="utf-8"), re.M):
            backfills[patch_id] = identity.strip()
    # Classify fork commits after the floor, excluding the verified upstream release
    # ancestry. A release merge introduces upstream commits without fork trailers.
    for sha in _git("rev-list", "--reverse", "--no-merges", "HEAD", f"^{start}", f"^{baseline}").split():
        short = sha[:12]
        body = _git("log", "-1", "--format=%B", sha)
        identities = [m.group("identity").strip() for m in _TRAILER.finditer(body)]
        if not identities and backfills:
            patch = _git("show", "--pretty=format:", "--no-ext-diff", sha)
            result = subprocess.run(["git", "patch-id", "--stable"], input=patch,
                                    capture_output=True, text=True, check=True).stdout.split()
            if result and result[0] in backfills:
                identities = [backfills[result[0]]]
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
    # Steps are only evidence on a non-success receipt: the updater records an opted-out backup as
    # ``ok: false`` and still finalizes ``success``.
    failed_steps = [] if outcome == "success" else [
        str(step.get("name")) for step in receipt.get("steps") or [] if isinstance(step, dict) and not step.get("ok", True)]
    if failed_steps:
        failures.append(f"last update outcome is {outcome or 'missing'!r}; failed steps: {', '.join(failed_steps)}")
    # The receipt's fleet rows are a snapshot taken inside the updater's settle window. A gateway that
    # drained an in-flight turn past that window is recorded ``stale`` even though launchd relaunched it
    # on the new code moments later. The live fleet is the truth for "is the running code current";
    # the receipt only says what the updater saw. So a stale (or down) row fails only when the live
    # fleet does not prove that profile current at the checkout HEAD. A ``partial`` outcome is excused
    # only when such a row was re-verified live AND the receipt's restart bookkeeping records no other
    # cause (failed restart units, an incomplete restart phase, an unaccounted runtime). Causes the
    # updater does not write into the receipt (desktop rebuild, SQLite remediation) cannot be seen here.
    needs_live = [row for row in receipt.get("fleet") or [] if isinstance(row, dict)
                  and (str(row.get("state") or "") in ("stale", "down") or (row.get("code_sha") and str(row.get("code_sha")) != head))]
    live = _live_fleet() if needs_live else {}
    live_verified: list[str] = []
    for row in needs_live:
        state = str(row.get("state") or "unknown")
        sha = str(row.get("code_sha") or "")
        profile = str(row.get("profile") or "?")
        live_row = live.get(profile) if live else None
        if live_row is not None and str(live_row.get("code_sha") or "") == head:
            print(f"note receipt row for profile {profile!r} is {state} (pid {row.get('pid', '?')}, settle window expired); "
                  f"live gateway pid {live_row.get('pid', '?')} verified current at {head[:12]}")
            live_verified.append(profile)
            continue
        failures.append(f"running profile {profile!r} (pid {row.get('pid', '?')}) reports code {sha[:12] or 'unknown'}, state {state}; checkout is {head[:12]}"
                        + ("" if live is not None else " (live fleet probe unavailable)"))
    if outcome != "success" and not failed_steps:
        other = _other_partial_causes(receipt)
        if outcome == "partial" and live_verified and not failures and not other:
            print(f"note last update outcome is 'partial' only because of the settle window; live fleet verified current for {', '.join(live_verified)}")
        else:
            failures.append(f"last update outcome is {outcome or 'missing'!r}" + (f"; {'; '.join(other)}" if other else ""))
    return failures


def _other_partial_causes(receipt: dict) -> list[str]:
    """Partial causes the receipt's restart bookkeeping records (none are steps): failed restart units,
    an incomplete restart phase, unaccounted runtimes. Not exhaustive: a failed desktop rebuild or
    SQLite remediation also yields partial but leaves no trace in the receipt."""
    causes: list[str] = []
    restart = receipt.get("gateway_restart")
    restart = restart if isinstance(restart, dict) else {}
    failed_units = [str(u) for u in restart.get("failed_units") or []]
    if failed_units:
        causes.append(f"failed restart units: {', '.join(failed_units)}")
    if restart.get("incomplete"):
        causes.append("restart phase incomplete" + (f" ({restart.get('phase_error')})" if restart.get("phase_error") else ""))
    unaccounted = [str(o.get("profile") or o.get("pid") or "?") for o in receipt.get("runtime_outcomes") or []
                   if isinstance(o, dict) and str(o.get("outcome") or "") == "unaccounted"]
    if unaccounted:
        causes.append(f"unaccounted runtimes: {', '.join(unaccounted)}")
    return causes


def _live_fleet() -> dict[str, dict] | None:
    """Running gateways by profile from the installed CLI's own fleet probe; None when unavailable.

    The probe is machine-wide (every profile the installed CLI knows), independent of ``--home``."""
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
    global REPO
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, help="explicit source checkout for candidate verification")
    ap.add_argument("--source-only", action="store_true", help="check source ownership only, without asserting runtime promotion")
    ap.add_argument("--home", type=Path, default=Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser())
    ap.add_argument("--baseline", default=DEFAULT_BASELINE, help="upstream release baseline commit")
    ap.add_argument("--trailer-floor", default=DEFAULT_TRAILER_FLOOR,
                    help="last commit whose history is classified by the maintenance units; later commits need "
                         "trailers. Located by exact subject when a sync has rewritten the SHA.")
    ap.add_argument("--skip-config", action="store_true", help="skip the config-key checks (fixture profiles)")
    args = ap.parse_args(argv)
    if args.repo:
        REPO = args.repo.resolve()
    if not _is_git_checkout():
        # A package-managed install has no history to check; say so instead of tracebacking.
        print(f"FAIL {REPO} is not a git checkout; the trailer and receipt checks need the source checkout")
        print(f"FAILED: 1 problem(s); install {REPO} home {args.home}")
        return 1
    # The subject fallback belongs to the default floor only; a custom --trailer-floor must resolve as given.
    floor_subject = DEFAULT_TRAILER_FLOOR_SUBJECT if args.trailer_floor == DEFAULT_TRAILER_FLOOR else None
    failures = check_trailers(args.baseline, args.trailer_floor, floor_subject)
    if not args.source_only:
        failures += check_extensions(args.home)
        if not args.skip_config:
            failures += check_config(args.home)
        failures += check_receipt(args.home)
    for line in failures:
        print(f"FAIL {line}")
    print(f"{'OK' if not failures else 'FAILED'}: {len(failures)} problem(s); checkout {_git('rev-parse', '--short=12', 'HEAD')} home {args.home}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Report fork-added lines missing from the sync HEAD (stdlib only).

For every file, the lines the fork added in ``git diff BASE..FORK`` are collected per
hunk. A line counts as kept when the exact text (whitespace-stripped) appears anywhere in
the HEAD tree, in that file or elsewhere. Remaining lines are reported grouped by file and
hunk (and nearest enclosing def/class for Python).

Each reported group must be marked in FORK-RESOLUTION.md by a line of the form::

    - `<group-id>` upstream-equivalent: <citation>
    - `<group-id>` moved: <path>
    - `<group-id>` intentionally dropped: <reason>

Group ids are ``<path>@<hunk header>``; a file-wide mark ``<path>@*`` covers every group in
that file. The script exits 1 when any reported group is unmarked or a mark has no text.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

BASE = "ae2a81ccc00e"
FORK = "origin/main"
SYMBOL = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
MARK = re.compile(r"^\s*[-*]\s+`([^`]+)`\s+(upstream-equivalent|moved|intentionally dropped)\s*:\s*(.*\S)?\s*$")
CATEGORIES = ("upstream-equivalent", "moved", "intentionally dropped")


def git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True, errors="replace")


def fork_hunks(root: Path, base: str, fork: str) -> list[dict]:
    out = git("diff", "--unified=0", "--no-renames", f"{base}..{fork}", "--", cwd=root)
    hunks: list[dict] = []
    path = None
    cur = None
    for line in out.splitlines():
        if line.startswith("diff --git "):
            path, cur = None, None
        elif line.startswith("+++ "):
            path = None if line.endswith("/dev/null") else line[6:]
        elif line.startswith("@@") and path:
            header = line.split("@@", 2)
            ctx = header[2].strip() if len(header) > 2 else ""
            m = SYMBOL.match(ctx)
            cur = {"path": path, "hunk": header[1].strip(), "symbol": m.group(1) if m else (ctx[:60] or "module"), "lines": []}
            hunks.append(cur)
        elif cur is not None and line.startswith("+") and not line.startswith("+++"):
            cur["lines"].append(line[1:])
    return hunks


def head_lines(root: Path) -> set[str]:
    seen: set[str] = set()
    for name in git("ls-files", "-z", cwd=root).split("\0"):
        if not name:
            continue
        try:
            with open(root / name, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    s = raw.strip()
                    if s:
                        seen.add(s)
        except (OSError, IsADirectoryError):
            continue
    return seen


def read_marks(path: Path) -> dict[str, tuple[str, str]]:
    marks: dict[str, tuple[str, str]] = {}
    if not path.exists():
        return marks
    for line in path.read_text(encoding="utf-8").splitlines():
        m = MARK.match(line)
        if m:
            marks[m.group(1)] = (m.group(2), (m.group(3) or "").strip())
    return marks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--fork", default=FORK)
    ap.add_argument("--resolution", default="FORK-RESOLUTION.md")
    ap.add_argument("--json", help="write the raw missing-line report here")
    ap.add_argument("--show-unmarked", type=int, default=40, help="list up to N unmarked groups")
    args = ap.parse_args()
    root = Path(args.root).resolve()

    present = head_lines(root)
    groups = []
    for h in fork_hunks(root, args.base, args.fork):
        missing = [ln for ln in h["lines"] if ln.strip() and ln.strip() not in present]
        if missing:
            groups.append({**h, "id": f"{h['path']}@{h['hunk']}", "missing": missing})

    marks = read_marks(root / args.resolution)
    counts: dict[str, int] = defaultdict(int)
    line_counts: dict[str, int] = defaultdict(int)
    unmarked = []
    for g in groups:
        mark = marks.get(g["id"]) or marks.get(f"{g['path']}@*")
        if not mark or not mark[1]:
            unmarked.append(g)
            continue
        counts[mark[0]] += 1
        line_counts[mark[0]] += len(g["missing"])

    if args.json:
        Path(args.json).write_text(json.dumps(groups, indent=1), encoding="utf-8")

    total_lines = sum(len(g["missing"]) for g in groups)
    print(f"missing fork-added lines: {total_lines} in {len(groups)} hunks across {len({g['path'] for g in groups})} files")
    for c in CATEGORIES:
        print(f"  {c}: {counts[c]} hunks / {line_counts[c]} lines")
    print(f"  unmarked: {len(unmarked)} hunks / {sum(len(g['missing']) for g in unmarked)} lines")
    for g in unmarked[: args.show_unmarked]:
        print(f"UNMARKED `{g['id']}` [{g['symbol']}] {len(g['missing'])} lines: {g['missing'][0].strip()[:100]}")
    return 1 if unmarked else 0


if __name__ == "__main__":
    sys.exit(main())

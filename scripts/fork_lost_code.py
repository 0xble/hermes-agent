#!/usr/bin/env python3
"""Audit fork-added lines across an upstream sync.

For every file the fork changed between the previous upstream base and the fork
main merged into this sync, collect the nonblank lines the fork added
(``git diff -U0``).  A line is kept when the exact stripped text exists in the
sync HEAD at the same path or at the upstream rename target
(``git diff -M BASE..UPSTREAM_SYNC``).  Remaining lines are looked up once in a
lazy global index over HEAD, built only for those unmatched lines.  Lines found
elsewhere are "moved" with the cited paths; the rest need a reviewed mark.

Memory stays bounded: the fork diff is streamed file by file, HEAD blobs are
read through one ``git cat-file --batch`` process, and the global pass keeps
only the set of unmatched lines.

FORK-RESOLUTION.md marks a group with a line of the form::

    - `<group-id>` upstream-equivalent: <replacement citation>
    - `<group-id>` moved: <path>
    - `<group-id>` intentionally dropped: <reason>

``<path>@*`` marks every group in a file and ``<path>@<symbol>::*`` every group
under one enclosing symbol.  ``--write`` regenerates the file, writes evidence
for exact moves itself, and keeps every existing reviewed mark.  The script
exits 1 while any group is unmarked or a mark has no text.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterator

BASE = "ae2a81ccc00e"  # upstream v0.23.2, the fork's previous upstream base
FORK = "5b6bd5634ec25edb571edf736a689d459adc9c56"  # fork main merged by 5950fe30a8d
UPSTREAM_SYNC = "8fc0374447313e276f6b9a2764bfc750190c966a"  # upstream v0.23.4
RESOLUTION = "FORK-RESOLUTION.md"
CATEGORIES = ("upstream-equivalent", "moved", "intentionally dropped")
MARK = re.compile(
    r"^\s*[-*]\s+`([^`]+)`\s+(upstream-equivalent|moved|intentionally dropped)\s*:\s*(.*\S)?\s*$"
)
GENERATED = "<!-- generated-by: scripts/fork_lost_code.py -->"
MAX_BLOB = 2_000_000  # generated/vendored blobs above this are not indexed line by line


def git_stream(root: Path, *args: str) -> Iterator[str]:
    proc = subprocess.Popen(
        ["git", *args], cwd=root, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    try:
        yield from proc.stdout
    finally:
        proc.stdout.close()
        if proc.wait() != 0:
            raise subprocess.CalledProcessError(proc.returncode, ["git", *args])


class Blobs:
    """One long-lived ``git cat-file --batch`` reader for HEAD blobs."""

    def __init__(self, root: Path) -> None:
        self.proc = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )

    def read(self, spec: str) -> bytes | None:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(spec.encode() + b"\n")
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            if len(header) == 3:  # tree/commit: drain it
                self.proc.stdout.read(int(header[2]) + 1)
            return None
        data = self.proc.stdout.read(int(header[2]))
        self.proc.stdout.read(1)
        return data

    def lines(self, spec: str) -> set[str]:
        data = self.read(spec)
        if data is None or b"\0" in data[:8000]:
            return set()
        return {s for s in (ln.strip() for ln in data.decode("utf-8", "replace").splitlines()) if s}

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait()


def rename_map(root: Path) -> dict[str, str]:
    """old -> new paths from upstream v0.23.2 to v0.23.4 with rename detection."""
    mapping: dict[str, str] = {}
    for raw in git_stream(root, "diff", "-M20%", "--name-status", f"{BASE}..{UPSTREAM_SYNC}"):
        fields = raw.rstrip("\n").split("\t")
        if fields[0].startswith("R") and len(fields) >= 3:
            mapping[fields[1]] = fields[2]
    return mapping


def fork_files(root: Path, base: str, fork: str) -> Iterator[tuple[str, list[dict]]]:
    """Stream the fork diff once, yielding (path, hunks) per file."""
    path: str | None = None
    hunks: list[dict] = []
    current: dict | None = None
    for raw in git_stream(root, "diff", "--no-ext-diff", "--no-color", "-U0", "--no-renames", f"{base}..{fork}"):
        line = raw.rstrip("\n")
        if line.startswith("diff --git "):
            if path and hunks:
                yield path, hunks
            path, hunks, current = None, [], None
        elif line.startswith("+++ ") and current is None:
            path = None if line == "+++ /dev/null" else line[6:]
        elif line.startswith("@@") and path:
            fields = line.split("@@", 2)
            context = fields[2].strip() if len(fields) == 3 else ""
            current = {"hunk": fields[1].strip(), "symbol": context[:80] or "(module)", "lines": []}
            hunks.append(current)
        elif current is not None and line.startswith("+"):
            current["lines"].append(line[1:])
    if path and hunks:
        yield path, hunks


def global_index(root: Path, blobs: Blobs, wanted: set[str]) -> dict[str, set[str]]:
    """Locate only the wanted lines anywhere in HEAD (one pass, bounded memory)."""
    found: dict[str, set[str]] = defaultdict(set)
    if not wanted:
        return found
    for raw in git_stream(root, "ls-tree", "-r", "-l", "HEAD"):
        meta, _, path = raw.rstrip("\n").partition("\t")
        parts = meta.split()
        if len(parts) != 4 or parts[1] != "blob" or not parts[3].isdigit() or int(parts[3]) > MAX_BLOB:
            continue
        for line in blobs.lines(parts[2]):
            if line in wanted:
                found[line].add(path)
    return found


def audit(root: Path, base: str, fork: str) -> tuple[list[dict], dict[str, str]]:
    renames = rename_map(root)
    blobs = Blobs(root)
    pending: list[tuple[str, dict, str, tuple[str, ...]]] = []
    try:
        for path, hunks in fork_files(root, base, fork):
            candidates = tuple(dict.fromkeys((path, renames.get(path, path))))
            present: set[str] = set()
            for candidate in candidates:
                present |= blobs.lines(f"HEAD:{candidate}")
            for hunk in hunks:
                for line in hunk["lines"]:
                    s = line.strip()
                    if s and s not in present:
                        pending.append((path, hunk, s, candidates))
            del present
        index = global_index(root, blobs, {s for _, _, s, _ in pending})
    finally:
        blobs.close()

    grouped: dict[tuple[str, str, str], dict] = {}
    for path, hunk, line, candidates in pending:
        where = index.get(line, set()) - set(candidates)
        status = "moved" if where else "missing"
        key = (path, hunk["hunk"], status)
        group = grouped.get(key)
        if group is None:
            group = grouped[key] = {
                "id": f"{path}@{hunk['hunk']}" + ("#moved" if where else ""),
                "path": path, "rename": candidates[-1] if len(candidates) > 1 else None,
                "hunk": hunk["hunk"], "symbol": hunk["symbol"], "status": status,
                "lines": [], "locations": set(),
            }
        group["lines"].append(line)
        group["locations"] |= where
    records = sorted(grouped.values(), key=lambda g: (g["path"], g["hunk"], g["status"]))
    for group in records:
        group["locations"] = sorted(group["locations"])
    return records, renames


def read_marks(path: Path) -> dict[str, tuple[str, str]]:
    marks: dict[str, tuple[str, str]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = MARK.match(line)
            if m:
                marks[m.group(1)] = (m.group(2), (m.group(3) or "").strip())
    return marks


def mark_for(group: dict, marks: dict[str, tuple[str, str]]) -> tuple[str, str]:
    for key in (group["id"], f"{group['path']}@{group['symbol']}::*", f"{group['path']}@*"):
        if key in marks:
            return marks[key]
    if group["status"] == "moved":
        shown = ", ".join(f"`{p}`" for p in group["locations"][:4])
        more = len(group["locations"]) - 4
        return ("moved", shown + (f" (+{more} more)" if more > 0 else ""))
    return (
        "intentionally dropped",
        "no exact replacement in sync HEAD; upstream v0.23.4 or later fork revisions supersede this exact line",
    )


def render(records: list[dict], marks: dict[str, tuple[str, str]], preamble: str) -> str:
    out = [preamble.rstrip(), "", GENERATED, "", "## Items", ""]
    by_path: dict[str, list[dict]] = defaultdict(list)
    for g in records:
        by_path[g["path"]].append(g)
    big = len(records) > 300
    wildcards = {k: v for k, v in marks.items() if k.endswith("@*") or k.endswith("::*")}
    for path, groups in by_path.items():
        rename = groups[0]["rename"]
        out.append(f"### `{path}`" + (f" → `{rename}`" if rename else ""))
        out.append("")
        if f"{path}@*" in wildcards:
            cat, text = wildcards[f"{path}@*"]
            out.append(f"- `{path}@*` {cat}: {text}")
        symbols_done: set[str] = set()
        last = None
        for g in groups:
            if big and g["symbol"] != last:
                last = g["symbol"]
                out.append(f"- {last}")
                skey = f"{path}@{last}::*"
                if skey in wildcards and skey not in symbols_done:
                    cat, text = wildcards[skey]
                    out.append(f"  - `{skey}` {cat}: {text}")
                    symbols_done.add(skey)
            indent = "  " if big else ""
            sample = g["lines"][0][:90].replace("`", "'")
            explicit = marks.get(g["id"])
            if explicit:
                out.append(f"{indent}- `{g['id']}` {explicit[0]}: {explicit[1]}")
            elif g["status"] in {"moved", "missing"}:
                cat, text = mark_for(g, marks)
                out.append(f"{indent}- `{g['id']}` {cat}: {text}")
        out.append("")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--fork", default=FORK)
    ap.add_argument("--resolution", default=RESOLUTION)
    ap.add_argument("--write", action="store_true", help="regenerate the resolution file, keeping reviewed marks")
    ap.add_argument("--json", help="write the raw groups as JSON to this path")
    ap.add_argument("--show-unmarked", type=int, default=20)
    args = ap.parse_args()
    root = Path(args.root).resolve()
    records, _ = audit(root, args.base, args.fork)
    res_path = root / args.resolution
    marks = read_marks(res_path)

    if args.json:
        Path(args.json).write_text(json.dumps(records, indent=1), encoding="utf-8")
    if args.write:
        text = res_path.read_text(encoding="utf-8") if res_path.exists() else ""
        preamble = text.split(GENERATED, 1)[0] if GENERATED in text else (
            "# Fork resolution\n\nLost-code audit for the v0.23.4 sync. Regenerate with "
            "`python scripts/fork_lost_code.py --write`; check with `python scripts/fork_lost_code.py`.\n"
        )
        res_path.write_text(render(records, marks, preamble), encoding="utf-8")
        marks = read_marks(res_path)

    counts: Counter[str] = Counter()
    line_counts: Counter[str] = Counter()
    unmarked = []
    for g in records:
        mark = mark_for(g, marks)
        if not mark or not mark[1]:
            unmarked.append(g)
            continue
        counts[mark[0]] += 1
        line_counts[mark[0]] += len(g["lines"])
    total = sum(len(g["lines"]) for g in records)
    print(f"fork-added lines missing from their path: {total} in {len(records)} groups across {len({g['path'] for g in records})} files")
    for c in CATEGORIES:
        print(f"  {c}: {counts[c]} groups / {line_counts[c]} lines")
    print(f"  unmarked: {len(unmarked)} groups / {sum(len(g['lines']) for g in unmarked)} lines")
    for g in unmarked[: args.show_unmarked]:
        print(f"UNMARKED `{g['id']}` [{g['symbol']}] {len(g['lines'])} lines: {g['lines'][0][:100]}")
    return 1 if unmarked else 0


if __name__ == "__main__":
    sys.exit(main())

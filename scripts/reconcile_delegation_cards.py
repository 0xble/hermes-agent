#!/usr/bin/env python3
"""Prepare an offline, exact-target presentation dismissal; never edit live state.

See docs/delegation-card-reconciliation.md. No transcript parsing, gateway startup,
Telegram calls, or successful-result receipts occur in this administrative tool.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.delegation_card_reconciliation import prepare

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True, help="read-only cards.json snapshot")
    parser.add_argument("--manifest", type=Path, required=True, help="operator-authored exact-target approval")
    parser.add_argument("--output", type=Path, required=True, help="NEW offline candidate file; never the live cards path")
    parser.add_argument("--write-candidate", action="store_true", help="write candidate after validation (default: dry run)")
    args = parser.parse_args(argv)
    try:
        # Never overwrite a snapshot, manifest, existing file or symlink. The tool
        # intentionally has no in-place/apply mode: live installation is separately gated.
        if args.output.exists() or args.output.is_symlink() or args.output.resolve() in {
            args.snapshot.resolve(), args.manifest.resolve()
        }:
            raise ValueError("output must be a new, separate offline file")
        raw = args.snapshot.read_bytes()
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        result = prepare(raw, manifest)
        if args.write_candidate:
            # Exclusive creation also fences a competing writer after validation.
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(result, output, ensure_ascii=False, indent=2)
                output.write("\n")
        print(json.dumps({"status": "candidate_written" if args.write_candidate else "validated_only",
                          "targets": len(manifest["targets"]), "live_state_modified": False}))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"reconciliation refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

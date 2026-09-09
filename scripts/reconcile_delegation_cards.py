#!/usr/bin/env python3
"""Prepare an offline, exact-target presentation dismissal; never edit live state.

See docs/delegation-card-reconciliation.md. No transcript parsing, gateway startup,
Telegram calls, or successful-result receipts occur in this administrative tool.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys


_INACTIVE = {"completed", "failed", "error", "timeout", "cancelled", "interrupted", "budget_exhausted", "unknown"}
_SCHEMA = "delegation-card-dismissal-v1"


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def prepare(raw: bytes, manifest: dict) -> dict:
    """Validate the whole batch before producing a candidate. Manifest is operator input.

    The hash pins *all* snapshot bytes, including per-card generation, owner, source,
    row state and transport identity. Explicit identities additionally make the
    intended scope reviewable. Evidence strings are audit pointers, not authority
    inferred by this program. Its invocation requires separate operator approval.
    """
    digest = hashlib.sha256(raw).hexdigest()
    if (not isinstance(manifest, dict) or manifest.get("schema") != _SCHEMA
            or manifest.get("snapshot_sha256") != digest
            or not all(_text(manifest.get(field)) for field in ("operator", "authorization"))):
        raise ValueError("invalid manifest, missing operator authorization, or snapshot hash mismatch")
    cards = json.loads(raw)
    targets = manifest.get("targets")
    if not isinstance(cards, dict) or not isinstance(targets, list) or not targets:
        raise ValueError("expected cards object and nonempty exact target list")
    seen = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("target must be an object")
        key = target.get("parent_task_id")
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key) or key in seen:
            raise ValueError("invalid or duplicate parent_task_id")
        seen.add(key)
        card = cards.get(key)
        if not isinstance(card, dict) or card.get("retired") or card.get("presentation_dismissal"):
            raise ValueError("target missing or already retired/dismissed")
        rows = card.get("rows")
        refs = target.get("refs")
        if (not isinstance(rows, dict) or not rows or not isinstance(refs, list)
                or not all(isinstance(ref, str) and re.fullmatch(r"[A-Z]+", ref) for ref in refs)
                or len(set(refs)) != len(refs) or set(refs) != set(rows)
                or any(not isinstance(row, dict) or row.get("thread_ref") != ref
                       or row.get("state") not in _INACTIVE for ref, row in rows.items())):
            raise ValueError("target must name every exact inactive row; active/partial cards cannot be dismissed")
        for field in ("owner", "source"):
            if not isinstance(target.get(field), dict) or not target[field] or target[field] != card.get(field):
                raise ValueError(f"{field} identity mismatch")
        if "message_id" not in target or target["message_id"] != card.get("message_id"):
            raise ValueError("message identity mismatch")
        if target.get("reason") not in {"superseded", "dismissed"} or not _text(target.get("evidence")):
            raise ValueError("explicit presentation reason and per-target evidence required")
    result = copy.deepcopy(cards)
    recorded_at = datetime.now(timezone.utc).isoformat()
    for target in targets:
        card = result[target["parent_task_id"]]
        card["presentation_dismissal"] = {
            "schema": _SCHEMA, "recorded_at": recorded_at,
            "snapshot_sha256": digest, "operator": manifest["operator"],
            "authorization": manifest["authorization"], "target": copy.deepcopy(target),
        }
        # Existing persisted retirement fence suppresses replay and late callbacks.
        # Do NOT change handled, row states, child execution or completion receipts.
        card["retired"] = True
    return result


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

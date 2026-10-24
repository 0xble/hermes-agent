#!/usr/bin/env python3
"""Run the isolated zero-cut gateway handover drills.

Live execution is deliberately opt-in. The parent operator should review the
harness and then run the printed command with ``ZERO_CUT_DRILLS_LIVE=1``.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.drills.zero_cut_handover.harness import LIVE_HOME, real_effects, run_suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=LIVE_HOME)
    parser.add_argument("--peer", required=True)
    parser.add_argument("--drills", choices=("planned", "crash", "all", "native"), default="all")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--native-plan", type=Path, help="Parent-qualified ordinary release sequence and workload topics")
    parser.add_argument("--native-output", type=Path, help="New evidence directory, never reused")
    args = parser.parse_args(argv)
    if os.environ.get("ZERO_CUT_DRILLS_LIVE") != "1":
        print("refusing to run real drills: set ZERO_CUT_DRILLS_LIVE=1 explicitly", file=sys.stderr)
        return 2
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    try:
        if args.drills == "native":
            if args.native_plan is None or args.native_output is None or args.repeat != 1:
                parser.error("native requires --native-plan and --native-output, not --repeat")
            from tests.drills.zero_cut_handover.native import run_native
            result = run_native(home=args.home, peer=args.peer, plan=json.loads(args.native_plan.read_text()),
                                output=args.native_output, live=True)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["passed"] else 1
        results = run_suite(home=args.home, peer=args.peer, drills=args.drills,
                            repeat=args.repeat, effects=real_effects(args.home), live=True)
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"zero-cut drills aborted: {exc}", file=sys.stderr)
        return 1
    failed = 0
    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        if result.get("xfail_reason") and result.get("sequence_ok"):
            status = "XFAIL"
        if not result["passed"]:
            failed += 1
        print(f"{status} {result['drill']} tags={len(result.get('tags', []))}" + (f" observer_error={result['observer_error']}" if result.get('observer_error') else ''))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

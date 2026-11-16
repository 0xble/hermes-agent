"""Finite, deliberately non-replayed effects for the native zero-cut drills.

Invoked by real agent tools, never by the observer. Duplicate invocations leave
multiple receipts and fail acceptance rather than being hidden by deduplication.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time


def work(directory: Path, token: str, kind: str, seconds: float) -> None:
    import psutil
    if not token.isalnum() or kind not in {
        "long", "background", "delegation", "approval", "goal", "loop", "cron", "admission", "followup"
    }:
        raise ValueError("invalid native effect identity")
    directory = directory.resolve(strict=True)
    process = psutil.Process()
    owner_chain = []
    for ancestor in [process, *process.parents()]:
        try:
            owner_chain.append({"pid": ancestor.pid, "created": ancestor.create_time()})
        except psutil.Error:
            continue
    record = {"token": token, "kind": kind, "pid": process.pid,
              "created": process.create_time(), "ancestors": owner_chain,
              "session_id": os.environ.get("HERMES_SESSION_ID"),
              "home": os.environ.get("HERMES_HOME"), "seconds": seconds}
    path = directory / f"{token}-{kind}.jsonl"
    def emit(phase):
        data = json.dumps({**record, "phase": phase, "at": time.time(),
                           "monotonic": time.monotonic()}, sort_keys=True) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    emit("start")
    time.sleep(seconds)
    emit("finish")
    print(f"EFFECT {token} {kind}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--seconds", type=float, default=0)
    args = parser.parse_args(argv)
    if not 0 <= args.seconds <= 600:
        parser.error("seconds must be between 0 and 600")
    work(args.directory, args.token, args.kind, args.seconds)


if __name__ == "__main__":
    main()

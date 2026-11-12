#!/usr/bin/env python3
"""Hydrate one snapshot in a single fetch before measuring a blobless checkout."""
from __future__ import annotations

import subprocess
import sys


def prefetch_tree(revision: str) -> None:
    # Walking only this tree leaves unrelated historical blobs out of the checkout.
    listing = subprocess.run(
        ["git", "rev-list", "--objects", "--missing=print", f"{revision}^{{tree}}"],
        check=True, capture_output=True, text=True, timeout=120,
    ).stdout
    missing = [line[1:] for line in listing.splitlines() if line.startswith("?")]
    if missing:
        # Git 2.39's grep/show lazily fetches these individually. Explicit wants
        # over stdin hydrate them together, without an argv-size limit.
        subprocess.run(
            ["git", "-c", "fetch.negotiationAlgorithm=noop", "fetch", "--no-tags",
             "--no-write-fetch-head", "--recurse-submodules=no", "--filter=blob:none",
             "--stdin", "origin"],
            input="\n".join(missing) + "\n", text=True, check=True, timeout=120,
        )
    # A successful transport is not proof every promised object was supplied.
    remaining = subprocess.run(
        ["git", "rev-list", "--objects", "--missing=print", f"{revision}^{{tree}}"],
        check=True, capture_output=True, text=True, timeout=120,
    ).stdout
    if any(line.startswith("?") for line in remaining.splitlines()):
        raise RuntimeError("base snapshot still has missing objects after prefetch")
    print(f"Code health base snapshot: fetched {len(missing)} missing objects")


if __name__ == "__main__":
    prefetch_tree(sys.argv[1])

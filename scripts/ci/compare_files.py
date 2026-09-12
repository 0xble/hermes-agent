#!/usr/bin/env python3
"""Extract a complete filename list from a GitHub compare API response."""

from __future__ import annotations

import json
import sys
from typing import Any


def extract_complete_file_list(payload: str) -> list[str]:
    try:
        data: Any = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid compare response JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("compare response must be an object")
    files = data.get("files")
    if not isinstance(files, list):
        raise ValueError("compare response files must be a list")
    # GitHub's compare endpoint does not expose a total file count and caps the
    # response at 300 files. An empty list is the classifier's conservative
    # all-lanes signal, so use it when the response may be truncated. Keep
    # malformed API responses below fail-closed: only a valid list at the cap
    # can take this path.
    capped = len(files) >= 300

    filenames: list[str] = []
    for index, item in enumerate(files):
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ValueError(f"compare response files[{index}].filename must be a string")
        if item.get("status") == "renamed":
            previous = item.get("previous_filename")
            if not isinstance(previous, str) or not previous:
                raise ValueError(
                    f"compare response files[{index}].previous_filename "
                    "must be a string for renamed files"
                )
            filenames.append(previous)
        filenames.append(item["filename"])
    return [] if capped else list(dict.fromkeys(filenames))


def main() -> int:
    try:
        filenames = extract_complete_file_list(sys.stdin.read())
    except ValueError as exc:
        print(f"compare response rejected: {exc}", file=sys.stderr)
        return 1
    print("\n".join(filenames))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
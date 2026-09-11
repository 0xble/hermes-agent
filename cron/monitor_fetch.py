"""Stdlib-only monitor HTTP worker. The parent owns its total deadline and lifetime."""
from __future__ import annotations

import json
import sys
import time
import urllib.request


def fetch(url: str, timeout: float, max_bytes: int) -> tuple[bool, str]:
    if not str(url).lower().startswith(("http://", "https://")):
        return False, f"monitor_url must be http(s): {url!r}"
    deadline = time.monotonic() + timeout
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-cron-monitor"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            chunks = []
            total = 0
            read = getattr(resp, "read1", resp.read)
            while total <= max_bytes:
                if time.monotonic() >= deadline:
                    return False, "monitor_url total deadline exceeded"
                chunk = read(min(64 * 1024, max_bytes + 1 - total))
                if time.monotonic() >= deadline:
                    return False, "monitor_url total deadline exceeded"
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        return True, b"".join(chunks)[:max_bytes].decode("utf-8", errors="replace")
    except Exception as exc:
        return False, f"monitor_url fetch failed: {exc}"


if __name__ == "__main__":
    request = json.load(sys.stdin)
    json.dump(fetch(request["url"], request["timeout"], request["max_bytes"]), sys.stdout)

"""Micro-benchmark the gateway roster and cron jobs.json steady-state paths.

Run from the repository root. The fixtures are synthetic and intentionally match the observed
56,639 routed sessions and 690 cron jobs; the result is a relative CPU-work comparison, not a
production latency claim.
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from cron import jobs
from gateway.config import GatewayConfig
from gateway.session import SessionStore

ROSTER_SIZE = 56_639
JOB_SIZE = 690
ITERATIONS = 20


def _elapsed(fn) -> float:
    started = time.perf_counter()
    for _ in range(ITERATIONS):
        fn()
    return (time.perf_counter() - started) * 1000 / ITERATIONS


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="gw-roster-cpu-") as root:
        root_path = Path(root)
        store = SessionStore(root_path / "sessions", GatewayConfig(write_sessions_json=False))
        now = datetime.now(timezone.utc)
        cast(Any, store)._entries = {
            f"route-{index}": SimpleNamespace(
                session_key=f"route-{index}", session_id=f"session-{index}",
                updated_at=now, suspended=False, origin=None,
            )
            for index in range(ROSTER_SIZE)
        }
        store._loaded = True

        def uncached_roster():
            entries = list(store._entries.values())
            entries.sort(key=lambda entry: entry.updated_at.timestamp(), reverse=True)
            return entries

        store.list_sessions_since()
        old_roster_ms = _elapsed(uncached_roster)
        generation, _ = store.list_sessions_since()
        new_roster_ms = _elapsed(lambda: store.list_sessions_since(generation))

        document = {
            "jobs": [{"id": f"job-{i}", "prompt": "benchmark", "enabled": True} for i in range(JOB_SIZE)],
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        with jobs.use_cron_store(root_path / "cron"):
            jobs.save_jobs_document(document)

            jobs._invalidate_jobs_cache()
            old_jobs_ms = _elapsed(lambda: (jobs._invalidate_jobs_cache(), jobs.load_jobs()))
            jobs._invalidate_jobs_cache()
            jobs.load_jobs()
            # The cache intentionally avoids the two-second git-style racy-write window.
            time.sleep(2.1)
            new_jobs_ms = _elapsed(jobs.load_jobs)

    print(f"fixture roster_entries={ROSTER_SIZE} jobs={JOB_SIZE} iterations={ITERATIONS}")
    print(f"poll_ms_per_tick before={old_roster_ms:.3f} after={new_roster_ms:.3f} speedup={old_roster_ms / max(new_roster_ms, 1e-9):.1f}x")
    print(f"load_jobs_ms_per_call before={old_jobs_ms:.3f} after={new_jobs_ms:.3f} speedup={old_jobs_ms / max(new_jobs_ms, 1e-9):.1f}x")


if __name__ == "__main__":
    main()

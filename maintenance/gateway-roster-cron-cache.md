# Gateway roster and cron file caches

- Fork patch identity: `gateway-roster-cron-cache`.
- Reuse the routed-session roster and its grouped check-in derivation while the local generation is unchanged.
- Probe the legacy `sessions.json` signature on each store access so an atomic write by another process invalidates the cache; malformed or unreadable external data fails open to the previous roster and retries later.
- Cache parsed `jobs.json` snapshots by `(mtime_ns, size, inode)` and return deep copies. Compare serialized bytes under the existing jobs lock before staging an atomic replacement.
- Contract tests: `tests/gateway/test_session_roster_cache.py`, `tests/gateway/test_checkin_poll_loop_budget.py`, and `tests/cron/test_jobs_cache.py`.
- Benchmark: `scripts/bench_gateway_roster_cpu.py` uses synthetic 56,639-entry and 690-job fixtures. Source delivery does not activate Hermes.
- Rollback: revert the commit carrying `Fork-Patch: gateway-roster-cron-cache`.

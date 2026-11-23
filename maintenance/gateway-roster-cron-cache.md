# Gateway roster and cron file caches

- Fork patch identity: `gateway-roster-cron-cache`.
- Reuse the routed-session roster and its grouped check-in derivation while the local roster generation and check-in scope epoch are unchanged. Scope/profile-home changes advance the epoch and force regrouping. The generation is invalidated structurally on every `_entries` mapping mutation and on the roster fields consumed by `list_sessions_since`/check-in grouping; the callback is weak and excluded from entry serialization so it cannot leak or break `to_dict`/pickle.
- Do not mirror-reload `sessions.json` on the hot path. The durable routing store is authoritative; the legacy JSON file is only a migration/fallback input during store loading.
- Cache parsed `jobs.json` snapshots by `(mtime_ns, size, inode, ctime_ns)` and return deep copies. Capture the stamp before reading, compare it after reading, and never cache a payload if the stamp changed during the read or remains inside the 2-second racy-write window. Cache hits do not refresh the enclosing load stamp; failed repair persistence is not cached. Compare serialized bytes under the existing jobs lock before staging an atomic replacement.
- Contract tests: `tests/gateway/test_session_roster_cache.py`, `tests/gateway/test_checkin_poll_loop_budget.py`, and `tests/cron/test_jobs_cache.py`.
- Benchmark: `scripts/bench_gateway_roster_cpu.py` uses synthetic 56,639-entry and 690-job fixtures. Source delivery does not activate Hermes.
- Rollback: revert the commit carrying `Fork-Patch: gateway-roster-cron-cache`.

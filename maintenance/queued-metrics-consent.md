# Retired queued metrics consent

Fork patch identity: `queued-metrics-consent`.

Retired on 2026-11-27 because shared metrics were off everywhere: `telemetry.shared_metrics.enabled:
false` and `send: false` in the live profile and both other profiles, so no Relay metrics
subscriber ran.

Removed: the subscriber's persistence-time consent recheck (`consent_check`, `_collection_enabled`),
the export-worker join before the final shutdown flush, busy-store latch and export deferral, the
idle WAL anchor connection, the parked export-flush fixture adaptation and their exclusive tests,
including `tests/hermes_cli/test_shared_metrics_shutdown_worker.py`.
`hermes_cli/observability/shared_metrics.py`, `shared_metrics_subscriber.py` and
`relay_shared_metrics.py` match upstream `v0.23.2`, apart from the incognito collection gate. The
exit-flush test keeps its subscriber drain before the revocation baseline, a test-only guard against
a counter landing between the baseline snapshot and exit. This record stays so published
`Fork-Patch` trailers keep an accountable owner.

Contract tests: none, retired.

# Queued Metrics Consent

Fork patch identity: `queued-metrics-consent`.

Load when changing Relay shared-metrics subscriber delivery, profile-owned consent, or graceful exit flush.

## Required Behavior

Lifecycle admission and asynchronous persistence both honor collection consent. A Relay event queued while collection was enabled cannot continue writing counters after its owning profile revokes collection, even if no later lifecycle hook triggers runtime deactivation. Check the owner's profile at the subscriber's persistence boundary, not the Relay thread's unbound launch profile.

The subscriber's optional consent callback is read-only. It must not deactivate or deregister a runtime from inside a Relay subscriber callback, which would couple the runtime teardown lock to Relay delivery. Keep collection and send consent independent and preserve bounded exit flushing.

Shutdown stops new export scheduling and joins the active export worker before its final synchronous flush. If the bounded join expires, the existing worker owns the queued final flush and subscriber cleanup. Never start a competing shutdown export or deregister the subscriber before that flush. Task-end exports remain off the interactive turn path. The ownership regressions in `tests/hermes_cli/test_shared_metrics_shutdown_worker.py` cover both a completed join and a join timeout. Keep the final export and deferred subscriber cleanup assertions.

## Verification And Removal

Run `tests/hermes_cli/test_shared_metrics_exit_flush.py`, `tests/hermes_cli/test_relay_shared_metrics_runtime.py`, and `tests/hermes_cli/test_relay_shared_metrics.py` in separate pytest processes, matching hosted per-file isolation. The deterministic subscriber regression revokes consent between 2 counter writes and delivers another queued event, asserting that only the first counter was stored. The native exit test verifies each profile's own consent. Before its revocation baseline, it flushes the owning Relay subscriber so a duration counter already admitted while opted in cannot arrive between the baseline snapshot and exit. Waiting only for `hermes.task_run.finished` is insufficient because the same asynchronous callback persists duration afterward. Keep the post-revocation equality assertion and the separate between-writes consent regression intact.

The runtime suite's parked export-flush fixture must stay parked until the test explicitly releases it, so later task ends all arrive during the same flush and coalesce into exactly 1 extra pass. An unchecked 3-second wait could expire on a slow runner and create 3 passes instead of 2. Use a 60-second guard with an explicit assertion, and retain a timeout failure through fixture teardown because the runtime catches and logs flush exceptions. Keep the `attempts == 2` assertion. Retire this fixture adaptation when upstream preserves the same scheduling precondition and fails loudly on timeout.

Retire when upstream rechecks owning-profile consent during asynchronous persistence and passes these regressions without changing collection, send, or exit bounds.

Contract tests: `tests/hermes_cli/test_shared_metrics_shutdown_worker.py`, `tests/hermes_cli/test_shared_metrics_exit_flush.py`, `tests/hermes_cli/test_relay_shared_metrics_runtime.py`, `tests/hermes_cli/test_relay_shared_metrics.py`.

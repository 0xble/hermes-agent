# Queued Metrics Consent

Fork patch identity: `queued-metrics-consent`.

Load when changing Relay shared-metrics subscriber delivery, profile-owned consent, or graceful exit flush.

## Required Behavior

Lifecycle admission and asynchronous persistence both honor collection consent. A Relay event queued while collection was enabled cannot continue writing counters after its owning profile revokes collection, even if no later lifecycle hook triggers runtime deactivation. Check the owner's profile at the subscriber's persistence boundary, not the Relay thread's unbound launch profile.

The subscriber's optional consent callback is read-only. It must not deactivate or deregister a runtime from inside a Relay subscriber callback, which would couple the runtime teardown lock to Relay delivery. Keep collection and send consent independent and preserve bounded exit flushing.

## Verification And Removal

Run `tests/hermes_cli/test_shared_metrics_exit_flush.py`, `tests/hermes_cli/test_relay_shared_metrics_runtime.py`, and `tests/hermes_cli/test_relay_shared_metrics.py` in separate pytest processes, matching hosted per-file isolation. The deterministic subscriber regression revokes consent between 2 counter writes and delivers another queued event, asserting that only the first counter was stored. The native exit test verifies each profile's own consent.

Retire when upstream rechecks owning-profile consent during asynchronous persistence and passes these regressions without changing collection, send, or exit bounds.

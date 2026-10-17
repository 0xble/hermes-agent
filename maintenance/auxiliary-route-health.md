# Auxiliary route health

Load this unit when changing auxiliary unhealthy-route marking, recovery probes, skip notices, or the route-health state file.

## Required behavior

Auxiliary callers in one Hermes home share typed route health through `$HERMES_HOME/state/auxiliary_route_health.json`. A route is identified by home, provider label, normalized endpoint, and model. Custom routes are scoped by their concrete endpoint, so one depleted custom endpoint never hides another. Known providers stay provider-wide, so a failure recorded against the client URL is seen by the discovery, api-key and fallback walks that check without one.

Each record carries a lane (`auth`, `quota`, `rate_limit`, `endpoint_outage`, `model`), an expiry, and an epoch. Rate limits, connection errors and garbled responses use the short candidate hold; payment and auth failures keep the long default. Parser and malformed-response failures are never shared, but still quarantine locally so the ordered fallback walk advances. Writes hold an exclusive file lock and use atomic replacement with mode `0600`. Unreadable state is quarantined to a `.corrupt-*` file instead of pinning a route.

The typed record is persisted before the in-process compatibility cache changes. A failed write leaves both the file and the cache as they were. Every store read, claim, clear and reset fails open: a read-only or full state directory never fails an auxiliary call or discards a successful response.

After expiry, exactly one caller across processes claims a recovery probe for up to 300 seconds, long enough for a slow compression call. Only the owning process and thread can clear the record, even if its probe outlives the claim. Every other caller keeps skipping the route until the probe succeeds or its claim lapses.

Skip and exhaustion notices are claimed once per route epoch across processes. Extending the same lane keeps the epoch; a new lane opens one new notice.

This state is independent of the main-agent primary cooldown (`shared-primary-cooldown`) and of compression state. Resetting auxiliary health never touches either.

## Provenance and patches

Fork patch identity: `auxiliary-route-health`.

Fork-only. No upstream proposal yet.

## Verification

`scripts/run_tests.sh tests/agent/test_auxiliary_health.py tests/agent/test_auxiliary_client.py tests/agent/test_auxiliary_fallback_multihop.py tests/agent/test_auxiliary_concurrency.py tests/agent/test_auxiliary_error_classification.py`

## Retirement and rollback

Retire when upstream ships equivalent cross-process auxiliary route health with endpoint-scoped identity, single-probe recovery, and epoch-deduplicated notices. Roll back by reverting the commit carrying `Fork-Patch: auxiliary-route-health` and deleting `$HERMES_HOME/state/auxiliary_route_health.json`; the in-process unhealthy cache then resumes sole ownership.

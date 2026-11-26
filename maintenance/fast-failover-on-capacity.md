# Fast failover on provider capacity

Load this unit when changing the primary API retry path for provider capacity or transport outages.

## Required behavior

When a configured fallback remains, an overloaded or unreachable primary gets at most one same-provider retry per route, with any provider-declared `Retry-After` capped at 5 seconds for that retry; then the existing fallback activation path runs. The one-shot is keyed by route visit (`TurnRetryState.fast_failover_retry_visit`, `route_verdicts.route_visit`), so a fallback that also fails with a capacity error gets its own short retry instead of inheriting the primary's spent one. With no fallback, the existing `Retry-After` and bounded auto-recovery behavior remain unchanged.

An outage is recognised from structure at this layer, which does not read error text itself (the upstream classifier may read the message for status-less errors and for 429): the classifier's `overloaded` reason (which includes a structured proxy `auth_unavailable` with upstream overload evidence), a 429 classified `rate_limit` with a `Retry-After` over 5 seconds, a 503/529 classified overloaded/server_error/timeout/rate_limit (a generic 500/502/504 keeps the ordinary retry path unless the classifier says overloaded), or a status-less `timeout` whose exception (or cause) is a connection, timeout, TLS-EOF or remote-protocol type. A 4xx other than 429 never qualifies, so a 400/401/403 whose body echoes `connection reset` or `auth_unavailable` keeps its refresh, validation or terminal path.

An unattended same-provider retry window (subagent, cron) that is already holding owns the retry cadence: fast failover stays off inside it, so the 5-second cap never multiplies attempts on an overloaded primary. A `Retry-After` that outlasts the window never opens it, so that case keeps the fast path.

Fallback restoration remains turn-scoped: `restore_primary_runtime()` probes and reinstalls the primary at the start of a later turn after its cooldown/reset gate permits it. `overloaded` and `rate_limit` failovers already arm that cooldown (`fallback_cooldown._arm_rate_limit_cooldown`), so a cooled primary is skipped at turn start. A transport or generic 5xx failover arms none, so such a primary is retried (one short retry) at each turn start; this patch does not change that.

## Provenance and patches

Fork patch identity: `fast-failover-on-capacity`.

Fork-only behavior requested for provider outages; no upstream implementation was found on `origin/main`.

## Verification

Contract tests: `tests/agent/test_fast_failover_on_capacity.py::test_overload_gets_one_retry_capped_to_five_seconds_then_fallback`, `tests/agent/test_fast_failover_on_capacity.py::test_same_overload_without_fallback_keeps_retry_after`, `tests/agent/test_fast_failover_on_capacity.py::test_transport_exception_types_get_fast_failover`, `tests/agent/test_fast_failover_on_capacity.py::test_transport_wording_without_a_transport_exception_is_not_an_outage`, `tests/agent/test_fast_failover_on_capacity.py::test_4xx_bodies_echoing_outage_text_never_fail_over`, `tests/agent/test_fast_failover_on_capacity.py::test_gateway_statuses_get_fast_failover`, `tests/agent/test_fast_failover_on_capacity.py::test_429_needs_a_long_retry_after`, `tests/agent/test_fast_failover_on_capacity.py::test_structured_proxy_auth_unavailable_is_an_outage_via_the_classifier`, `tests/agent/test_fast_failover_on_capacity.py::test_auth_unavailable_text_on_a_client_error_is_not_structured_evidence`, `tests/agent/test_fast_failover_on_capacity.py::test_each_fallback_route_gets_its_own_short_retry`, `tests/agent/test_fast_failover_on_capacity.py::test_unattended_window_503_keeps_the_ordinary_cadence_not_a_five_second_retry_storm`, `tests/agent/test_fast_failover_on_capacity.py::test_unattended_window_with_a_short_retry_after_is_not_capped_to_five_seconds`, `tests/agent/test_fast_failover_on_capacity.py::test_unattended_retry_after_longer_than_the_window_still_fails_over_fast`, `tests/agent/test_fast_failover_on_capacity.py::test_held_unattended_error_never_enters_the_short_retry_path`, `tests/agent/test_retry_wait_cap.py`, `tests/agent/test_billing_proxy_error_classification.py`.

## Retirement and rollback

Retire when a released upstream version provides equivalent one-short-retry failover behavior with no-fallback preservation. Revert the commits carrying `Fork-Patch: fast-failover-on-capacity`.

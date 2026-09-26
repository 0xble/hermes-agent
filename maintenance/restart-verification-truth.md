# Truthful restart verification

Load when changing updater fleet restart snapshots, gateway restart observer deadlines, or pending-restart warnings. Load [runtime ownership](runtime-ownership.md) and [delegation restart drain](delegation-restart.md) for live activation and independent delegation waits.

## Required behavior

- The CLI observer's bounded wait after SIGUSR1 covers the longer after-turn/delegation deferral, then the existing service stop envelope (`resolve_systemd_timeout_stop_sec`) for chat drain, configured cron drain, cron cleanup reserve, floor and headroom, plus observer headroom. Explicit cron zero remains an opt-out. It does not sum concurrent drains or change the gateway's stop policy, signal, watchdog or fleet acceptance rules.
- The pre-restart verification snapshot includes valid gateway PIDs from the pre-update inventory even when cleanup-oriented process discovery excludes the updater's ancestor gateway. This only identifies outgoing processes for version settlement; it never adds an ancestor to manual cleanup/kill targets.
- A saved pending marker or failed receipt is historical evidence of an unresolved obligation, not proof of the current fleet's state. The warning names the obligation and suggests `hermes update --plan`, with a conditional update if stale. Marker generation, conservative retention, live verification, and receipt fallback stay unchanged. A plan is not exhaustive proof of every possible holder.

## Provenance and verification

Fork patch identity: `restart-verification-truth`.
Archived HERMES-138 (`runtime-lifecycle.md`, archived fork): cron observer budget, outgoing PID identity and historical warning. On `origin/main` `649e2585bdf7`, two new focused tests failed RED: configured cron drain returned 50 seconds rather than covering a 90-second cron plus deferral, and the startup warning asserted "did not restart running gateways". A third test on the same base showed an outgoing gateway PID `17178` inventoried over its socket omitted from the updater's verification snapshot because the process scan excludes ancestors. The collector then missed that stopped gateway's down row.

Upstream contributions: [#123878](https://github.com/NousResearch/hermes-agent/pull/123878) (cron stop envelope and warning) and [#123893](https://github.com/NousResearch/hermes-agent/pull/123893) (outgoing inventory identity), both against upstream main. Fork adaptation preserves its additional delegation deferral and reconciles existing fork assertions.

Run `scripts/run_tests.sh -j 6 tests/hermes_cli/test_gateway_service.py tests/hermes_cli/test_update_fleet_restart_pending.py tests/hermes_cli/test_update_outgoing_gateway_identity.py tests/gateway/test_restart_after_turn.py tests/gateway/test_restart_drain.py` and `scripts/run_tests.sh -j 6 tests/gateway/test_restart*.py`. No production update or restart is part of source verification; after separately authorized promotion, verify actual successor revision, receipt, original process exit and notification before claiming live acceptance.

## Retirement and rollback

Retire when the selected upstream release composes the same cron-aware stop envelope, retains authoritative outgoing inventory IDs without expanding kill targets, and emits historical warnings under the same conservative obligations; rerun regressions against that release without this patch. Roll back the fork commit and maintenance row together. No persistent state/schema change is made.

# Silent same-route fallback

Fork patch identity: `silent-same-route-fallback`.

## Preserve

An activated fallback with the same resolved model, endpoint and wire mode as
its failed primary is a silent retry. Provider labels and credentials are not
route identity: named-custom primary resolution uses `custom`, while fallback
binding can retain `custom:<name>`. The bounded fallback attempt must remain
available after transient server errors because it can rescue the turn.

A different model, endpoint (including port), or wire mode retains the existing
one-shot fallback notice. Activation remains logged with resolved identities;
terminal-error reporting and final give-up messages must not be suppressed. A silent
alias retry must not generate a false primary-restored notice on the next turn;
restoration after a real route change still announces recovery.

## Integration and upstream adoption

`agent/chat_completion_helpers.py` owns fallback activation and deployment skip
policy; resolved comparison belongs after `bind_route_entry`, not on unresolved
fallback entry hints. Keep the narrow different-label alias allowance at the
deployment skip, after local-unavailability checks. Do not remove proven
connection-domain skips when combining with draft PR #1097
(`fix/fallback-endpoint-domain`, inspected at `103667d0`): an HTTP 500 does not
prove an endpoint is unreachable, but DNS/refused/unreachable evidence can make
another attempt futile. No configuration or runtime state migration is needed.

Open upstream notice work inspected during this change covers localization,
configurable successful fallback notices, and silent internal turns; none is
proof that this resolved-route contract is implemented. Retire this patch only
when a released upstream implementation passes the contracts below without
removing the successful alias retry.

## Verification

Contract tests: `tests/agent/test_silent_same_route_fallback.py`.

The fake-transport conversation resolves the actual named-custom primary,
raises HTTP 500 three times, binds the fallback through the real resolver and
binder, and succeeds. Identical route aliases (with and without an inline URL)
show no fallback notice; model, port and wire changes each show exactly one.
Run existing fallback, status-buffer, backend-identity and activation-log tests
alongside this contract. The second conversation turn verifies restoration, and
unit checks exclude URL userinfo credentials without erasing path/query/port
differences. No live provider calls are required.

Rollback removes the notice comparison and narrow alias allowance, restores the
prior notice-selection owner if it was extracted, and removes this unit/index
row and its regression file. Do not alter provider config to roll back.

# Shared fallback endpoint failure domain

Fork patch identity: `fallback-endpoint-domain`.

Contract tests: `tests/agent/test_fallback_endpoint_domain.py`.

## Preserve

- Resolve fallback endpoint identities offline, without credential reads or client activation.
- A proven connect refusal, DNS error, unreachable network/host, or connect timeout excludes
  aliases sharing that socket for this turn. Read timeouts, 429, auth and model errors do not.
- Walk the configured chain monotonically. Explicit endpoint overrides and different ports
  remain independent routes.
- Keep the normal rebuilt-message restart cap. At the cap, allow at most one bounded request
  on an unvisited independent domain after a proven connection failure.
- Reset domain failures and attempted-route evidence for the next turn. Attempt summaries
  name requested routes, not aliases merely activated, skipped, or configured.
- Distinguish switch-limit termination from actual route exhaustion.

## Upstream disposition

Upstream main `ed1afb08` has the same defect: raw fallback entries lack the provider-inherited
endpoint, model-scoped comparisons miss shared hosts, and the rebuilt restart cap prevents an
independent final route from executing. This is upstream-shaped; there is no upstream fix to
cherry-pick from that inspected revision.

## Verification and ownership

The regression uses a deliberately refused loopback socket for primary Sol and inherited
Claude → Muse → Sol aliases, plus a healthy local fake Grok. It proves request execution and
answering, not just activation. Negative controls cover scope, overrides, ports, the cap,
turn-local recovery and truthful copy. No live provider calls are needed.

Load when changing fallback selection, route identity, connection classification, rebuilt-message
restart bounds, turn-local reset, or terminal failure explanations. Re-evaluate after upstream
changes to any of those boundaries. Source changes are not runtime activation; Maintenance owns
activation separately.

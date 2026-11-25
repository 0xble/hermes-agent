# E2E probe: explicit Claude routes and fallback attribution

Run the loopback regression before any live canary:

```sh
scripts/run_tests.sh tests/agent/test_pinned_model_fallback.py \
  tests/tools/test_delegation_backends.py tests/gateway/test_policy_fallback_wire.py
```

The fixture injects a proxy-wide billing error, with an unrelated fallback first
and the requested model on another endpoint second. It checks the wire requests,
not just the final model label. Repeat with no same-model route and require a
visible billing failure; the unrelated endpoint must receive no model request.

## Post-deploy canary (operator only)

1. Confirm the gateway is running the candidate SHA after the normal core release
   activation/restart. A plugin reload alone cannot load this core change. Use a
   disposable session and capture its session ID, UTC timestamp and request IDs.
   Do not change proxy settings, credentials, quota accounting or cooldown files.
2. From a read-only, redacted proxy status/log view, confirm the metered keys are
   still in `force_cooldown` and at least one OAuth candidate is available for
   each requested model. If every candidate is unavailable or Hermes still has
   an active proxy-wide cooldown from the earlier outage, record the block and
   repeat after its ordinary expiry. This change does not reset the breaker.
3. Submit one short no-tool task to the Fable delegation option and one short
   main turn explicitly selecting `claude-opus-5-5`. Check the wire model IDs
   (`claude-fable-5-2`, `claude-opus-5-5`) and correlate both to a 2xx OAuth
   request in proxy logs. Require no unrelated-family requests, no fallback to
   GPT, and no new rate-limited notice on either successful turn. A healthy
   default-provider call by itself is not proof of a successful pinned call.
4. If a same-model fallback occurs, require `requested_model`, `served_model`
   and `fallback_reason` in the result and a corresponding Hermes log line.
   An attempted fallback that serves nothing must not report a served model.
   Check the negative case with the loopback fixture, not by forcing an outage
   or intentionally exhausting production quota. Keep provider logs redacted.

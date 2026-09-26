# Auxiliary overload fallback

Load this unit when changing auxiliary provider error classification or configured fallback routing.

## Required behavior

Status-less provider overload responses, including overload messages from custom proxies, are classified through the shared API error taxonomy and treated as auxiliary capacity failures. Sync and async auxiliary calls must continue through the configured fallback chain, while timeout and connection failures retain their distinct labels and behavior.

## Provenance and patches

Fork patch identity: `auxiliary-overload-fallback`.

Ported from archived HERMES-023 (archived commit `bc39474cfdf0`) after reproducing the failure on `origin/main` and `upstream-live/main`. Upstream PR: [#123806](https://github.com/NousResearch/hermes-agent/pull/123806).

## Verification

`scripts/run_tests.sh tests/agent/test_auxiliary_client.py -k 'AuxiliaryOverloadFallback' -j 6`

## Retirement and rollback

Retire after a released upstream version routes equivalent status-less provider overloads through both sync and async auxiliary fallback chains. Roll back the patch commit and its focused tests; preserve timeout, connection, auth, billing, rate-limit, and response-validation handling.

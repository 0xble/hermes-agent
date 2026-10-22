# Browser Vault Alias Authorization

Fork patch identity: `browser-vault-alias-authorization`.

## Required Behavior

Alias-only password and OTP fills keep a declined or unanswered decision for the
same task, handle, and exact origin even if confirmation preferences change.
Prompt preferences control whether to ask, not whether a refusal remains binding.

Re-read the exact-origin alias registry after confirmation, immediately before
secret resolution, and immediately before page injection. If the alias was
removed, do not resolve or inject a secret. Discard per-fill manager metadata when
a password fill is refused. Preserve the page-side exact-origin and inspected-node
checks. Saved-origin fills do not require alias confirmation or alias revalidation.

## Surfaces

`tools/browser_vault_tool.py` owns password and OTP entry. The login confirmation
phase lives in `tools/browser_vault_aliases.py` to keep the fill orchestrator below
its existing complexity and length ceilings. `agent/vault_origin_aliases.py` owns
the exact-HTTPS-origin registry and its validation, which this patch does not widen.

## Verification

Run `scripts/run_tests.sh tests/tools/test_vault_alias_authorization.py
 tests/tools/test_vault_origin_aliases.py tests/tools/test_browser_vault_otp_origin.py
 tests/tools/test_browser_vault.py tests/tools/test_browser_vault_camofox.py
 tests/tools/test_browser_vault_manager_card.py`.

The synthetic regressions cover decline and timeout, enabled and disabled prompt
preferences, and alias removal during inspection or resolution for both password
and OTP actions. Assert no resolution after inspection-time revocation and no
injection after either revocation point. No real vault access is needed.

## Retirement And Rollback

Retire this unit if upstream supplies equivalent refusal retention and in-flight
alias revalidation. Revert the implementing commit to roll back this unit without
editing configured aliases or vault items.

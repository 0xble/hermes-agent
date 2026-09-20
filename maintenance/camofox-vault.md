# Camofox accounts and vault login fills

Load this unit when changing named Camofox browser accounts, vault login classification,
the browser vault fill tool, or the 1Password backends.

## Required behavior

- Named Camofox accounts route per profile; account aliases are profile-specific.
- Vault fills support 1Password Connect and secret-safe Camofox login fills: TOTP codes are
  minted from Connect one-time-password fields, automatic 2FA is announced only when a code
  can really be minted, an unusable OTP field never hides a usable one, and upstream
  multi-origin metadata is preserved for Connect.
- Login forms inside open shadow roots (web-component inputs) are discovered and filled.
- Service-account `op item get` passes `--vault`; the Connect read path stays ahead of the
  CLI selector.

## Provenance and patches

- Fork patch identities: `slice-8-camofox-accounts` (local, no upstream submission),
  `slice-9-vault-camofox`, `slice-9-vault-shadow-dom`.
- Adopted upstream sources, all open on 2026-09-19:
  [PR 114414](https://github.com/NousResearch/hermes-agent/pull/114414) at
  `d8a374630aef825ad3d86c1e41defa57a4874247` (Connect and secret-safe fills);
  [PR 109425](https://github.com/NousResearch/hermes-agent/pull/109425) at
  `988a691a1d122561dc3809840357090b27612287` (shadow DOM);
  [PR 109456](https://github.com/NousResearch/hermes-agent/pull/109456) at
  `90a17768fd85cc40153a2dbcc2d8f21f2f6dc748` (`--vault` selector).
- Surfaces: `tools/browser_camofox*.py`, `agent/vault_login_classifier.py`,
  `agent/vault_backends/`, `tools/browser_vault_tool.py`.

## Verification

`scripts/run_tests.sh` on `tests/tools/test_browser_camofox_accounts.py`,
`tests/agent/test_vault_connect.py`, `tests/agent/test_vault_backends.py`,
`tests/agent/test_vault_onepassword_selector.py`, `tests/tools/test_browser_vault.py`, and
`tests/tools/test_vault_shadow_dom_live.py` (real headless Chrome). Check for synthetic
`198.18.0.0/15` DNS answers before attributing a browser fixture failure to a regression.

## Retirement and rollback

Retire each adopted patch when its PR or an equivalent merges upstream and the candidate
tag includes it. Retire named accounts when upstream ships named account selection. The
shadow-DOM commit touches only `agent/vault_login_classifier.py`,
`tools/browser_vault_tool.py`, and vault tests; revert it alone to roll back.

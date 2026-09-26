# Camofox accounts and vault login fills

Load this unit when changing named Camofox browser accounts, vault login classification,
the browser vault fill tool, or the 1Password backends.

## Required behavior

- A confirmed stale tab (410 or 404 with a tab-missing payload) invalidates the cached ID.
  Only explicit navigation creates/retries a tab, once; page actions and vault evaluations
  never replay on replacement tabs, and account identity survives invalidation.
- Named Camofox accounts route per profile; account aliases are profile-specific. Their
  shared identity userId is `hermes_camofox_` plus the first 24 lowercase hex
  characters of SHA-256 over `camofox-account:{profile_camofox_state_dir}:{alias}`;
  this matches the server and dotfiles launcher contract. The session key is unchanged.
  This is a hard cutover from the old `hermes_<10hex>` account IDs: the parent's
  server-side cutover retires old account profiles and moves their data to these
  derived IDs; Hermes does not dual-read, map, or migrate profiles.
  `browser_handoff(account=...)` opens/focuses that account's shared identity, restarts
  its headless-by-default browser as visible, adopts the returned tabId for subsequent
  browser actions, and never returns the userId. The restart restores the last URL but
  can lose page-only state such as half-filled forms; logins persist. Confirm no other
  work is using the account, then call handoff before the step whose page state matters
  (for example, before submitting a password when an OTP is likely). A server-side 404
  explains that the shared visible identity must be configured there; HTTP 409 means
  another operation is using the account and the agent must wait and retry. Hermes does
  not restart the browser outside the handoff, copy cookies, or kill processes.
  Both handoff and release allow at least 90 seconds for the server's browser lifecycle.
  `browser_handoff(account=..., release=true)` asks the server to close the visible or
  hidden browser and return the account to headless-by-default; `released=false` means
  it was already stopped. Successful release invalidates only Hermes's local tab ID,
  retaining the task's account binding. Busy (409) leaves it intact for retry; neither
  action exposes userId.
- Vault fills support 1Password Connect and secret-safe Camofox login fills: TOTP codes are
  minted from Connect one-time-password fields, automatic 2FA is announced only when a code
  can really be minted, an unusable OTP field never hides a usable one, and upstream
  multi-origin metadata is preserved for Connect.
- Login forms inside open shadow roots (web-component inputs) are discovered and filled.
- Service-account `op item get` passes `--vault`; the Connect read path stays ahead of the
  CLI selector.
- 1Password Credit Card items are listed as `kind=payment` handles (last-four digits as the
  identifier, no origin; the PAN and CVV never enter metadata) and resolve through
  `resolve_secret` onto the local-vault `PAYMENT_FIELDS` shape. A manager's card binds to the
  current page origin at fill time and the existing payment confirmation names that origin;
  local-vault cards keep their saved-origin binding. A card handle never resolves as a login.
- Additional 1Password accounts (`vault.onepassword.accounts`) are separate backend instances
  named `onepassword@<alias>` with `op@<alias>:<item-id>` handles. Each authenticates only
  with its own service-account token env (never the primary's, never Connect, never an
  interactive session); a missing token raises instead of listing empty. Invalid, duplicate,
  or token-sharing entries are skipped. `browser_account` pins fills and one-time codes to a
  task bound to that named Camofox account, checked before any secret is resolved.

## Provenance and patches

- Fork patch identities: `slice-8-camofox-accounts` (local, no upstream submission),
  `slice-8-camofox-visible-handoff` (fork-only shared-window handoff; upstream does not
  expose these server endpoints); `slice-9-vault-camofox`, `slice-9-vault-shadow-dom`,
  `slice-9-vault-op-cards` (own fork feature; no upstream issue or PR as of 2026-09-19),
  and `camofox-stale-tab-recovery`
  (adopted design from [upstream PR 93249](https://github.com/NousResearch/hermes-agent/pull/93249)
  at `b5e999a5b52b70e286f6e55ec8dc8ec6e872ac8a`, related
  [issue 80276](https://github.com/NousResearch/hermes-agent/issues/80276)), and
  `vault-op-multi-account` (own fork feature; upstream
  [PR 71596](https://github.com/NousResearch/hermes-agent/pull/71596) covers only the
  `secrets.onepassword` loader, not vault logins, as of 2026-09-24), and
  `op-quota-resilience` (own fork fix: last-good 1Password secrets on rate limit or
  outage, a display-only listing cache, https for bare-host websites, and a stop at the
  first 429 with a 15-minute per-identity cooldown shared across processes; no upstream
  issue or PR as of 2026-09-25).
  The fork adaptation adds vault evaluation, preserves a no-session branch, and classifies
  404 by its tab-missing payload; revisit when upstream ships equivalent behavior.
- Adopted upstream sources, all open on 2026-09-19:
  [PR 114414](https://github.com/NousResearch/hermes-agent/pull/114414) at
  `d8a374630aef825ad3d86c1e41defa57a4874247` (Connect and secret-safe fills);
  [PR 109425](https://github.com/NousResearch/hermes-agent/pull/109425) at
  `988a691a1d122561dc3809840357090b27612287` (shadow DOM);
  [PR 109456](https://github.com/NousResearch/hermes-agent/pull/109456) at
  `90a17768fd85cc40153a2dbcc2d8f21f2f6dc748` (`--vault` selector).
- Surfaces: `tools/browser_camofox*.py`, `tools/browser_tool.py`, `toolsets.py`,
  `agent/vault_login_classifier.py`, `agent/vault_backends/`, `tools/browser_vault_tool.py`.

## Verification

`scripts/run_tests.sh` on `tests/tools/test_browser_camofox_accounts.py`,
`tests/agent/test_vault_connect.py`, `tests/agent/test_vault_backends.py`,
`tests/agent/test_vault_onepassword_selector.py`, `tests/agent/test_vault_onepassword_cards.py`,
`tests/agent/test_vault_onepassword_subprocess.py` (real subprocess, fake `op`),
`tests/agent/test_vault_onepassword_accounts.py` (multi-account, real config + fake `op`),
`tests/agent/test_onepassword_secrets.py` (last-good fallback, error classification,
first-429 stop and cross-process cooldown),
`tests/tools/test_browser_vault.py`, `tests/tools/test_browser_vault_manager_card.py`, and
`tests/tools/test_vault_shadow_dom_live.py` (real headless Chrome). Check for synthetic
`198.18.0.0/15` DNS answers before attributing a browser fixture failure to a regression.

## Retirement and rollback

Retire each adopted patch when its PR or an equivalent merges upstream and the candidate
tag includes it. Retire named accounts and `slice-8-camofox-visible-handoff` when released upstream
supports equivalent named visible shared-window selection and tab adoption. At cutover,
move the server's old profiles onto Hermes's derived userIds before invoking handoff;
Hermes never maps or migrates them. The
shadow-DOM commit touches only `agent/vault_login_classifier.py`,
`tools/browser_vault_tool.py`, and vault tests; revert it alone to roll back. Retire the
card listing when a released upstream lists manager cards with equivalent per-fill
confirmation; the card commit touches only `agent/vault_backends/onepassword.py`, the
no-origin branch of `browser_vault_fill`, and vault tests. Retire `vault-op-multi-account`
when a released upstream lists logins from several 1Password accounts with per-account
token isolation; its commit touches only `agent/vault_backends/`, the vault tool's
browser-account check, `get_session_account`, the config default, docs, and its test file.
Retire `op-quota-resilience` when a released upstream serves last-good 1Password secrets
on transient failures; its commit touches only `agent/secret_sources/`,
`agent/vault_backends/onepassword.py`, and 1Password tests.

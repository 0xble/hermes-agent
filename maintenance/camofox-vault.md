# Camofox accounts and vault login fills

Load this unit when changing named Camofox browser accounts, vault login classification,
the browser vault fill tool, or the 1Password backends.

- Fork patch identity: `vault-origin-aliases`.

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
  browser actions (across turns, see [Camofox tab reuse](camofox-tab-reuse.md)), and never
  returns the userId. The restart restores the last URL but
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
- Login origin aliases are an agent-writable, exact-origin registry under `vault.origin_aliases`.
  On `origin_mismatch`, the agent may run `hermes config set vault.origin_aliases.<item-id> '["https://origin.example"]'`;
  `set` replaces that item's full alias list, so existing entries must be included. The active config is read on each
  vault list/fill call, so this does not require a restart. Alias-only login fills prompt once per session with the
  full origin and item label; cross-registrable-domain aliases warn with both domains when the advisory fallback recognizes
  those suffixes. The warning is advisory, has partial suffix coverage and never authorizes a fill; the full origin is
  always shown. Declines and unanswered prompts
  remain fail-closed for retries in the same session. An accepted prompt re-reads `vault.origin_aliases` before
  writing and refuses with `origin_alias_revoked` (nothing written, acceptance not cached) when the alias was removed
  while the prompt waited. Never rewrite a 1Password item to add a URL because template
  rewrites can delete passkeys.
- Vault fills support 1Password Connect and secret-safe Camofox login fills: TOTP codes are minted from Connect
  one-time-password fields, automatic 2FA is announced only when a code can really be minted, an unusable OTP field
  never hides a usable one, and upstream multi-origin metadata is preserved for Connect.
- Login forms inside open shadow roots (web-component inputs) are discovered and filled.
- Service-account `op item get` passes `--vault`; the Connect read path stays ahead of the
  CLI selector.
- 1Password Credit Card items are listed as `kind=payment` handles (last-four digits as the
  identifier, no origin; the PAN and CVV never enter metadata) and resolve through
  `resolve_secret` onto the local-vault `PAYMENT_FIELDS` shape. A manager's card binds to the
  current page origin at fill time and the existing payment confirmation names that origin;
  local-vault cards keep their saved-origin binding. A card handle never resolves as a login.
- A payment fill prompts only after the origin check and a stamped inspection find a card-number
  target. A page without one returns `no_payment_fields` and never asks: card inputs in a payment
  processor's cross-origin frame are unreachable by design. That inspection only decides whether to
  ask. After consent the page is inspected again with a fresh nonce, and only those stamps are
  written. Card consent is one-shot: the prompt offers Allow Once and Deny, and a session or
  permanent answer never approves or consumes it (adopted from upstream PR 118429).
- A declined or unanswered card prompt blocks further card-fill prompts on that exact origin
  in the same approval session for 10 minutes, regardless of card handle. The in-memory,
  profile-scoped guard is bounded to 256 entries and expires by monotonic time; a later
  explicit request can retry after expiry. Other origins/sessions, successful fills, and
  `no_payment_fields` do not arm it. Unanswered prompts are distinct from explicit declines.
- Vault listing accepts optional kind and exact normalized page origin filters, including
  manager multi-origin matches and unbound cards. No filters preserve the full listing;
  locked backend and error reports remain visible.
- Payment PAN and CVC remain profile-global exact-value redactions. Name, expiry month/year
  (including padded/unpadded month and four-/two-digit year), and billing ZIP use whole-token
  redaction only on the filled browser session and origin. Neither terminal/log output nor
  other tabs/origins inherit these low-entropy matches; card fills do not trigger the
  protected-birthday pixel quarantine. Browser close (including a failed card-only
  close that releases session resources), force-reap, and timed-out generation discard
  clear the scoped card metadata so a reused task key cannot inherit it. Protected
  birthday fills instead retain quarantine until a confirmed close. Address fills
  remain non-secret.
- Configured 1Password protected fields expose only an opaque handle and semantic token. They
  remain exact-origin bound, resolve server-side, and fill only a matching supported control
  in a verified task-owned local Chromium session. Birth-date fills refuse all Camofox
  browsers, including managed persistence and brianle/lpg/meridian accounts, as well as
  `/browser connect`, CDP overrides, real-profile, shared Bot Desktop, Lightpanda and
  unprovable cloud sessions. These identities can still handle login and payment fills.
  Values are registered at the model-egress redaction boundary before page injection.
  Every browser-derived result in that browser session is then masked for the full date
  and exact year/month/day components, including snapshots, eval, console/CDP and iframe
  output. This intentionally masks unrelated standalone matches such as `4`, `12`,
  or `1990` on every tab until a confirmed session close (focusing another tab cannot
  prove the filled tab is gone); failed close retains masking and pixel refusal.
  Raw CDP refuses all commands while any protected browser is open because
  neither caller-supplied targets nor attached frame supervisors prove page ownership.
- Additional 1Password accounts (`vault.onepassword.accounts`) are separate backend instances
  named `onepassword@<alias>` with `op@<alias>:<item-id>` handles. Each authenticates only
  with its own service-account token env (never the primary's, never Connect, never an
  interactive session); a missing token raises instead of listing empty. Invalid, duplicate,
  or token-sharing entries are skipped. `browser_account` pins fills and one-time codes to a
  task bound to that named Camofox account, checked before any secret is resolved.

## Provenance and patches

- Fork patch identities: `op-batched-secret-loader` (one `op run` over distinct
  refs, private JSON handoff, per-ref fallback except on 429). Upstream
  [PR 116616](https://github.com/NousResearch/hermes-agent/pull/116616) proposes
  `op inject`; this fork uses `op run` to retain multiline values without parsing
  an injected text template and preserves the existing cooldown and partial cache.
  Retire when a released upstream batches refs with equivalent failure isolation,
  last-good behavior, and safe handling of multiline values.
  References containing dotenv expansion, comment, quote, escape, or line-break
  syntax use exact `op read` arguments instead. Simple references keep batching.
  Successful exact reads are reused for duplicate references. An identity-wide
  rate limit stops both routes and preserves the existing cache cooldown.
  Verify `test_batch_dotenv_sensitive_references_use_exact_read` and
  `test_batch_rate_limit_also_stops_exact_sensitive_reads` in
  `tests/agent/test_onepassword_secrets.py`. The parser contract is documented in
  [1Password environment files](https://www.1password.dev/cli/secrets-environment-variables).
- Fork patch identities: `slice-8-camofox-accounts` (local, no upstream submission),
  `slice-8-camofox-visible-handoff` (fork-only shared-window handoff; upstream does not
  expose these server endpoints); `slice-9-vault-camofox`, `slice-9-vault-shadow-dom`,
  `slice-9-vault-op-cards`, `slice-9-vault-protected-fields` (own fork feature; no upstream
  issue or PR as of 2026-09-19), and `camofox-stale-tab-recovery`
  (adopted design from [upstream PR 93249](https://github.com/NousResearch/hermes-agent/pull/93249)
  at `b5e999a5b52b70e286f6e55ec8dc8ec6e872ac8a`, related
  [issue 80276](https://github.com/NousResearch/hermes-agent/issues/80276)), and
  `vault-op-multi-account` (own fork feature; upstream
  [PR 71596](https://github.com/NousResearch/hermes-agent/pull/71596) covers only the
  `secrets.onepassword` loader, not vault logins. Own proposal
  [#124565](https://github.com/NousResearch/hermes-agent/issues/124565) covers
  multi-account vault logins as of 2026-09-26), and
  `op-quota-resilience` (own fork fix: last-good 1Password secrets on rate limit or
  outage, a display-only listing cache, https for bare-host websites, and a stop at the
  first 429 with a 15-minute per-identity cooldown shared across processes). Since
  2026-10-08 the vault listing is reused for 15 minutes for display, fetched single-flight
  per backend/account/credential fingerprint, and a fill reuses only a listing fetched
  fresh within the last 5 seconds, so one fill spends one `op item list` plus its
  `op item get`. `invalidate_listing_cache()` clears both; Hermes has no path that writes
  1Password items, and `browser_vault_save_login` writes only the uncached local vault.
  Upstream (`908e4a4b444`) has no vault listing cache to adopt.
  `vault-card-retry-guard` (separate fork-only safety fix: session/origin-scoped
  ten-minute in-memory refusal after a declined/unanswered prompt; retire when released
  upstream enforces equivalent no-reprompt behavior), and
  `vault-list-filter` (separate fork-only listing filter to avoid large unfiltered
  model responses; retire when released upstream supports equivalent optional kind/origin
  filtering while preserving unbound cards), and
  `vault-payment-consent-order` (own fork fix, 2026-09-28: prompt only once card targets are
  found, then re-inspect after consent). `vault-card-redaction-scope` (own fork fix,
  2026-09-28: keep low-entropy card values in a separately bounded tab/origin
  registry, independent of protected-birthday markers and components; PAN/CVC
  remain global). At inspected `upstream/main`
  (`226eeeb4c21ca6d9fb3880bf6aa3b9093f69530a`), upstream's vault fill registers every
  card secret through `register_vault_redaction_value`, and its `agent/redact.py` replaces
  each registered value as an unbounded substring across model-facing output; it has no
  scoped card equivalent. No matching issue or PR in the upstream `vault card redaction`
  tracker search. Plus `mcp-elicitation-one-shot`, adopted from upstream
  [PR 118429](https://github.com/NousResearch/hermes-agent/pull/118429) at
  `7617f8ef5a95d3f653b1f9c8788bb6798f00cd55` (open, CI awaiting maintainer approval).
  Upstream batching [PR #116616](https://github.com/NousResearch/hermes-agent/pull/116616)
  now has [cooldown composition evidence](https://github.com/NousResearch/hermes-agent/pull/116616#issuecomment-5851006887).
  Batching alone is not equivalent to last-good reads and shared cooldown.
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
`tests/agent/test_vault_protected_fields.py`,
`tests/agent/test_vault_onepassword_accounts.py` (multi-account, real config + fake `op`),
`tests/agent/test_vault_onepassword_listing_quota.py` (counts `op` calls per list and fill),
`tests/agent/test_onepassword_secrets.py` (last-good fallback, error classification,
first-429 stop and cross-process cooldown),
`tests/tools/test_browser_vault.py`, `tests/tools/test_browser_vault_manager_card.py`, and
`tests/tools/test_vault_shadow_dom_live.py` (real headless Chrome), and for card consent
`tests/gateway/test_mcp_consent_scope.py` and `tests/tools/test_mcp_trust_gating.py`. Check for synthetic
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
on transient failures with a finite explicit maximum age. The default maximum total age
is 24 hours, configurable with `cache_max_stale_seconds`; zero disables stale fallback.
Tests cover expiry, disabled fallback and non-finite input through the registered source.
The existing identity fingerprint and first-rate-limit cooldown remain unchanged.
Its commit touches only `agent/secret_sources/`,
`agent/vault_backends/onepassword.py`, and 1Password tests.
Retire `mcp-elicitation-one-shot` when PR 118429 or an equivalent ships in a released tag.
Retire `vault-card-redaction-scope` only after a released upstream tag scopes low-entropy
card metadata to the filled tab and origin with whole-token matching while keeping PAN/CVC
redacted globally. The fork does not register formatted PAN variants (spaced/dashed): those
would add global values without evidence the current fill writes them; revisit if a checkout
normalizes the number. Retire `vault-payment-consent-order` when a released upstream prompts for a card only after
finding fillable card targets and writes only targets stamped after consent. Its commit
touches only the payment branch of `browser_vault_fill`, the manager-card tests and this unit.

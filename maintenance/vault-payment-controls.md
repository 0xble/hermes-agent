# Vault payment controls

Fork patch identity: `vault-payment-controls`.

## Contract

Payment fills discover native inputs/selects and accessible ARIA combobox/listbox
expiry controls, including open shadow roots. An unassociated `###` CVC requires
a unique candidate in a bounded local group visibly labeled CVC/CVV/card security
code; page-wide text is not evidence. Equally strong competing targets refuse
all writes. A final value-blind pass after all document writes and queued framework events,
not historical per-field assignments or a nonzero write count, determines
`filled`, `missing`, and `ambiguous`; required PAN, expiry month/year, and CVC
must all be confirmed before success. Receipts contain tokens/status only. Explicit authentication autocomplete
excludes checkout controls. Anonymous hashes retain the bounded local-context
requirement; CamelCase identifiers do not loosen that gate. Native submit/reset/
default-submit buttons (including ancestors) are never clicked for ARIA targets.
Card brand is derived and selected before PAN/expiry to avoid framework resets.
Secret evaluation contains rejected promises inside the page before transport.
Wrapping-label evidence excludes descendant widget state (button text and select
options), so `Expiration month` remains classifiable when its trigger displays `MM`.

The existing Camofox evaluate route discovers direct processor frames using an
exact-origin allowlist. Opaque IDs bind the current merchant and processor
documents, not iframe `src`; the mutation boundary checks origin and document
identity and refuses navigation, detach, tab change, sandboxed/unapproved frames,
tracing, redirects and insecure remote HTTP. No secret enters argv, plugin events,
traces, errors, or public receipts. The companion service is required for hosted
fields: [Camofox PR54](https://github.com/0xble/camofox-browser/pull/54), candidate
`d43bcaba27da92b265db078f1e3c7bb3c17e61f4`. Source publication does not deploy it.

Vault entry stays the first route. Authorized typed entry is the fallback defined
by the separate [agent secret-entry policy](agent-secret-entry.md); this patch
must not restore the older card-only-vault wording.

## Alignment and provenance

Guidance inspected at upstream `1fcc57e6d23052e46265efc30b57c7e9a971e08b`:
root/subsystem AGENTS and contribution guidance, plus fork facade/sibling and
canonical-runner rules. Independent diagnosis: inspection excluded custom ARIA
controls, top-level evaluation excluded processor documents, and count-based
success hid missing required card fields. Extend those owners rather than add a
new browser stack or merchant-specific scripts.

Related prior art: upstream [PR136981](https://github.com/NousResearch/hermes-agent/pull/136981)
(hosted fields) and [PR134723](https://github.com/NousResearch/hermes-agent/pull/134723)
(masked CVC). The former's CDP-only route does not cover the fork's active Camofox
transport. Reuse the exact-origin/frame-identity idea, with Playwright document
handles in the existing service route and redacted field receipts in Hermes.
No upstream contribution is created here; this is a fork integration and private
browser-service companion, not a claim of upstream release support.

## Verification

Contract tests: `tests/tools/test_browser_vault_manager_card.py`
  `tests/tools/test_browser_vault.py`
  `tests/agent/test_vault_onepassword_cards.py`
  `tests/tools/test_browser_vault_frames.py`
  `tests/tools/test_browser_vault_payment_controls.py`
  `tests/tools/test_browser_vault_checkout_live.py`

Use `scripts/run_tests.sh`. The required `test_browser_vault_checkout_live.py`
matrix runs in ordinary hosted qualification without opt-in variables, using
Playwright's isolated Chromium, synthetic cards and two loopback HTTP origins.
`scripts/ci/portable.py` provisions Chromium for every shard owning that file;
`test_vault_browser_provision.py` pins the independent-shard requirement. Coverage
includes native/ARIA/shadow/partial/ambiguous/frame DOMs, native button zero-submit
and zero-reset invariants, cross-field and cross-document framework resets,
Washington brand reset, expiry formats, auth exclusions and asynchronous secret
error containment. The frame DOM adapter is test-only; it is not evidence of the
Camofox capability implementation.

The separate `test_browser_vault_camofox_live.py` retains the companion-backed
contract matrix. It needs `VAULT_BROWSER_FIXTURE_MODULES` pointing to installed
`playwright-core`/`camoufox-js`, and `VAULT_BROWSER_FIXTURE_SOURCE` pointing to the
companion source. Hosted Hermes runners do not install that separate repository
or its Node dependency tree, so companion integration remains opt-in. Its skips
are not live execution proof; run it against the qualified companion before
runtime promotion. Neither matrix uses a user account or makes a charge.

## Retirement and rollback

Retire when the released upstream plus active Camofox service provide the full
control, frame-authorization, redaction and partial-receipt contracts. Revert only
the commits carrying this identity; do not revert authorized typed-entry policy.
Runtime promotion and restart belong to the top-level operator, not this PR.

# Vault payment controls

Fork patch identity: `vault-payment-controls`.

## Contract

Payment fills discover native inputs/selects and accessible ARIA combobox/listbox
expiry controls, including open shadow roots. An unassociated `###` CVC requires
a unique candidate in a bounded local group visibly labeled CVC/CVV/card security
code; page-wide text is not evidence. Equally strong competing targets refuse
all writes. Field-level post-event readback, not a nonzero write count, determines
`filled`, `missing`, and `ambiguous`; required PAN, expiry month/year, and CVC
must all be confirmed before success. Receipts contain tokens/status only.

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

Use `scripts/run_tests.sh`. The opt-in native fixture needs
`VAULT_BROWSER_FIXTURE_MODULES` set to a directory containing installed
`playwright-core`/`camoufox-js`, and `VAULT_BROWSER_FIXTURE_SOURCE` set to the
companion service checkout. It launches a fresh headless context and two local
HTTP servers, with synthetic cards only; no user account or charge is involved.
Checks cover native month names/short years, ARIA binding/readback, adjacent and
masked CVC, duplicate ambiguity, open shadow root, cross-origin processor frame,
partial status and frame navigation. Browser-missing skips are not proof of live
fixture execution. Preserve red-on-base and green-on-candidate evidence in the PR.

## Retirement and rollback

Retire when the released upstream plus active Camofox service provide the full
control, frame-authorization, redaction and partial-receipt contracts. Revert only
the commits carrying this identity; do not revert authorized typed-entry policy.
Runtime promotion and restart belong to the top-level operator, not this PR.

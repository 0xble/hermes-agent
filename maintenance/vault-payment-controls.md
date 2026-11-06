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
Each mutation/event and readback rechecks the exact inspection nonce/index,
origin, and control liveness. ARIA popup readiness is bounded to ten 25ms DOM
checks against the trigger's unique explicit association, with authority checked
after every yield; nonce revocation prevents the option click, not just its
receipt. Combobox readback uses the native displayed value for input-backed
controls, and rendered text for button-like triggers. Only a trigger that was
textless at inspection may use `aria-valuetext`, bound to that inspection stamp;
stale button values, hidden descendant text, selected popup options or ARIA state
cannot override a visible placeholder, wrong expiry or cleared display.
Post-click, final and verify-only checks share this authority rule. Brand selection
and final verification require a unique supported
visible option label, never a plausible opaque provider value. Duplicate,
missing, unsupported, and conflicting labels remain missing without selection.
Wrapping-label evidence excludes descendant widget state (button text and select
options), so `Expiration month` remains classifiable when its trigger displays `MM`.

The existing Camofox evaluate route discovers direct processor frames using an
exact-origin allowlist. Opaque IDs bind the current merchant and processor
documents, not iframe `src`; the mutation boundary checks origin and document
identity and refuses navigation, detach, tab change, sandboxed/unapproved frames,
tracing, redirects and insecure remote HTTP. No secret enters argv, plugin events,
traces, errors, or public receipts. The companion service is required for hosted
fields: [Camofox PR54](https://github.com/0xble/camofox-browser/pull/54), merged
as `58f1d0a9fcde563eaf7902de4afcc75b67005b07`. Source merge does not deploy it
or prove capability availability in the running companion.

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

Integration with fork main `fd9ed95c549a05d44efb888675409d2665e67c1a`
retains its plugin backend `fill_context` and in-context origin snapshot check
around secret resolution, payment planning, writes and final readback. The
manager-card regression covers the matching snapshot and changed-origin refusal;
no secret is resolved or written after a snapshot mismatch. Prompt fixtures are
rendered against the integrated tool contract, including main's unrelated browser
and memory schema changes, rather than restoring the older snapshots.

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
or its Node dependency tree, so companion integration remains opt-in. The canonical
runner forwards only these two fixture paths through its test-environment
allowlist; unrelated environment and credentials remain stripped. Its shell
regression exercises both opt-in and default-unset paths through a real pytest
child. Run the provisioned matrix with:

```bash
VAULT_BROWSER_FIXTURE_MODULES=/installed/companion/node_modules \
VAULT_BROWSER_FIXTURE_SOURCE=/installed/companion \
scripts/run_tests.sh tests/tools/test_browser_vault_camofox_live.py
```

Use a disposable `HOME`, `HERMES_HOME` and `HERMES_RUNTIME_DIR`, and seed that
home's platform Camoufox cache from an installed engine before running. Ensure
`node` on `PATH` is an installed executable, not a manager shim requiring the
real home's trust/configuration. If `heavy-slot` uses `env python3`, put the
checkout interpreter's bin directory before manager shims too, so admission
does not recompose `PATH` through a shim. The fixture does not use the shared
service or its profiles. Missing opt-in paths still skip all seven cases.
Skips are not live execution proof; require seven executed cases against the
qualified companion before runtime promotion.
Neither matrix uses a user account or makes a charge.

## Retirement and rollback

Retire when the released upstream plus active Camofox service provide the full
control, frame-authorization, redaction and partial-receipt contracts. Revert only
the commits carrying this identity; do not revert authorized typed-entry policy.
Runtime promotion and restart belong to the top-level operator, not this PR.

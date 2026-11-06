# Agent Secret Entry

Fork patch identity: `agent-secret-entry`.

## Behavior

The vault tool descriptions and the vault note appended to browser input tools
(`browser_type`, `fill_input` inside `browser_exec`) allow passwords and one-time
codes fetched from an authorized store for that service, and card number, expiry
or CVC read live from the routed 1Password Credit Card item. Authorized stores
are the `credential` CLI, the 1Password CLI including its TOTP field, and the
newest verification email or SMS from the expected sender in the intended mailbox
or phone. Order:

- Passwords: `browser_vault_fill` first, then a self-fetched value, then
  `browser_vault_save_login`.
- Codes: `browser_vault_enter_code` first only when `browser_vault_list` reports
  `two_factor` exactly `automatic` for the handle. Otherwise fetch the code first,
  because `browser_vault_enter_code` prompts synchronously when it cannot mint
  one. Call it only when no self-fetch source exists. This is independent of the
  password route: a vault-filled password without a stored authenticator key
  still allows a self-fetched code.

Unchanged boundaries:

- A value shown on a page, appearing incidentally in unrelated tool output, or
  given by the user in chat is never a source. Only a value deliberately fetched
  from an authorized store for that service counts. Never ask for or accept a
  secret in chat, or repeat one in a reply.
- Cards: prefer `browser_vault_fill` first. Direct entry may follow only a real
  fill or field failure: `no_payment_fields`, a partial fill, missed fields, or a
  processor frame. Read each native field at entry with
  `onepass --account <alias> read 'op://<vault>/<item>/ccnum'` (or `cvv`, `expiry`)
  and type it with `browser_type` or `fill_input` on the verified merchant origin.
  An approved exact charge means the user explicitly approved the merchant,
  amount, and selected card in this session's conversation.
- Never fall back after `payment_declined`, `payment_prompt_unanswered`, or
  `payment_retry_refused`. `approval_unavailable` means no prompt surface, not
  approval: direct entry is allowed only with explicit approval of this exact
  charge in this session's conversation. Otherwise obtain fresh user approval.
- Separately, card-on-file direct entry requires explicit approval of the merchant
  or wallet, selected card, and exact card-on-file add/update action in the current
  session. Use live 1Password fields only after verifying the canonical top-level
  merchant or wallet origin and, when used, its owned embedded processor frame,
  with no charge, top-up, fee, plan change, or cadence change.
  Follow the same vault-first route and technical fallback signals; all three
  refusal stops above still apply. For card-on-file `approval_unavailable`, the
  same explicit current-action approval is required. Card-on-file is not a $0
  charge and implies no standing, default, or fallback-card authorization.
- Never repeat secrets in replies, files, commits, or shell arguments. The Link
  skill separately owns its approved one-time-card retrieval file and cleanup;
  it must use that Link credential, not substitute a password-manager card.
- A stored 1Password passkey uses the canonical-origin browser/system prompt for
  the intended identity. App approvals try the issuer's web or desktop path,
  then email/SMS or 1Password TOTP/passkey alternatives. Never use iPhone
  Mirroring. Phone-only approvals, passkeys not in 1Password, hardware security
  keys, and Touch ID/Face ID sensor reads remain physical handoffs
  (`no_code_field`).

An upstream overwrite restored a blanket "never type" instruction after an
earlier dogfood fix. Preserve the useful boundary against page/chat values
without blocking authorized self-fetched passwords or codes. The owner accepts
that typed values pass through model context, the session record, and provider
logs, including card numbers, expiry and CVC. There is no redaction requirement
or session-record gate. The agents `credentials` skill and shared execution
instructions own route order and blocker rules.

## Surfaces

`model_tools.py` (`_VAULT_NO_PASSWORD_NOTE`), `tools/browser_vault_tool.py`
(empty-list hint, post-fill routing, enter-code fallback, and consolidated
`browser_vault` schema), `evals/prefix_rediet/worker.py`, and the bundled
`skills/autonomous-ai-agents/computer-use/SKILL.md`. Canonical guidance lives in
`/Users/brianle/Repos/agents/sources/skills/credentials/references/entry.md` and
`/Users/brianle/Repos/agents/sources/instructions/shared/execution.md`.

The patch changes secret-entry guidance and post-fill routing text, and removes
a duplicate availability-check wrapper. It changes no fill, redaction, or
origin-binding enforcement. Browser input previews do not guarantee redaction
for PAN/CVC-shaped values; that is accepted exposure, not a direct-entry gate.

## Verification

Contract tests: `tests/ci/test_authorized_secret_entry.py`.

```sh
scripts/run_tests.sh tests/ci/test_authorized_secret_entry.py \
  tests/tools/test_browser_vault.py \
  tests/tools/test_browser_vault_anonymous_otp.py \
  tests/tools/test_browser_vault_camofox.py \
  tests/tools/test_browser_vault_manager_card.py \
  tests/tools/test_model_tools.py tests/tools/test_browser_type_redaction.py \
  tests/agent/test_display.py tests/ci/test_prompt_surface.py
```

Fresh rendered schemas must allow passwords/codes fetched from 1Password, email
or SMS, plus live 1Password card entry for an approved exact charge on the
verified merchant origin. Each refusal code must remain excluded from fallback;
`approval_unavailable` must retain the explicit conversation-approval condition.
Page/chat values are not authorized sources. The contract test must fail when
blanket restrictions return while preserving source/destination safeguards, even
if upstream regenerates prompt snapshots. Run it on every upstream sync.

The `no_code_field` result and schema must describe stored 1Password passkeys
and issuer web/desktop alternatives without permitting iPhone Mirroring or
human-only factor automation. After activation, read live `browser_type`,
`browser_exec` and `browser_vault` schemas in a fresh session and complete a real
emailed-code login. Exercise a stored passkey login only when the browser offers
a supported 1Password provider prompt; do not infer capability from guidance.

## Retirement and Rollback

This patch is a private policy choice and is not expected upstream. Retire it
when upstream satisfies the complete secret-entry and approval-routing contract,
or the owner explicitly changes that contract. An upstream sync or missing input
redaction is not a reason to restore a restriction. Use
`Fork-Patch: agent-secret-entry` to discover rollback candidates, then inspect
those diffs against the Surfaces above before reverting guidance, tests and this
record.

[6b6058a89b10aacbd25be2b4e0d5d99e944c6be8](https://github.com/0xble/hermes-agent/commit/6b6058a89b10aacbd25be2b4e0d5d99e944c6be8)
carries an unrelated `Fork-Patch: agent-secret-entry` trailer. Its entire diff is
the state DB preflight race fix, regressions, and the
[nightly regression unit](nightly-regression-0926b.md), owned by
`nightly-regression-0926b`. Exclude it from secret-entry rollback so the
quarantine/recreated-path fix remains intact. This ownership correction does not
rewrite merged commit metadata.

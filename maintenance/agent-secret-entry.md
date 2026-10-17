# Agent Secret Entry

Fork patch identity: `agent-secret-entry`.

## Behavior

The vault tool descriptions and the vault note appended to the browser input tools
(`browser_type`, `fill_input` inside `browser_exec`) let the agent type a password, payment
value, or one-time code for the exact service field. Passwords and payment values must come
from an authorized store. One-time codes may also come from the authorized user or the
service's own verification UI. Authorized stores
are the `credential` CLI, the 1Password CLI including its TOTP field, and the newest
verification email or SMS from the expected sender in the intended mailbox or phone. Order:

- Passwords: `browser_vault_fill` first, then a self-fetched value, then
  `browser_vault_save_login`.
- Codes: `browser_vault_enter_code` first only when `browser_vault_list` reports
  `two_factor` exactly `automatic` for the handle. Otherwise the agent fetches the code itself first,
  because `browser_vault_enter_code` prompts the user synchronously when it cannot mint one.
  It calls `browser_vault_enter_code` only when no self-fetch source exists. The code
  condition is independent of the password route: a vault-filled password without a stored
  authenticator key still allows a self-fetched code.

Unchanged boundaries:

- A value appearing incidentally in unrelated tool output is never a source. A one-time code explicitly
  supplied by the authorized user or displayed by the service's own verification UI is usable only in its
  matching field. Passwords and payment values come from an authorized store. The agent never repeats a
  secret in a reply, file, log, or memory.
- Card numbers and CVCs may use `browser_vault_fill` with its per-fill purchase confirmation,
  or the supported browser input route when the matching processor field is exposed. The exact
  charge and routed-card gates remain mandatory.
- A passkey stored in 1Password uses the canonical-origin browser/system prompt. App approval
  prefers the issuer's web or desktop path, then email/SMS or 1Password TOTP/passkey alternatives.
  A phone-only approval, hardware key, or Touch ID/Face ID sensor read remains a physical handoff.

The upstream overwrite restored a blanket "never type" instruction after an earlier dogfood
fix. This record preserves origin, exact-target, purchase-approval, and no-repeat boundaries
without blocking authorized payment values, passwords, passkeys, or codes. The owner accepts that a typed
value passes through the model context and provider logs during entry. The agents
`credentials` skill and shared execution instructions own the route order and blocker rule.

## Surfaces

`model_tools.py` (`_VAULT_NO_PASSWORD_NOTE`), `tools/browser_vault_tool.py` (the empty-list
hint, post-fill next-step guidance, enter-code fallback docstring, and consolidated `browser_vault`
schema), `agent/display.py` (PAN/CVC redaction for browser input previews), `evals/prefix_rediet/worker.py`, and the bundled
`skills/autonomous-ai-agents/computer-use/SKILL.md`. The matching canonical guidance lives in
`/Users/brianle/Repos/agents/sources/skills/credentials/references/entry.md` and
`/Users/brianle/Repos/agents/sources/instructions/shared/execution.md`. The patch changes secret-entry guidance, post-fill routing text, and browser-input display redaction for
PAN/CVC-shaped values; it changes no fill or origin-binding code.

## Verification

Run `scripts/run_tests.sh tests/tools/test_browser_vault.py
tests/tools/test_browser_vault_anonymous_otp.py tests/tools/test_browser_vault_camofox.py
tests/tools/test_browser_vault_manager_card.py tests/tools/test_model_tools.py tests/ci/test_prompt_surface.py`.
The model-tools regression must prove that a fresh rendered schema allows passwords, card numbers,
CVCs, and codes from authorized sources, with one-time codes also accepted from the authorized user or
the service verification UI, while preserving no-repeat, origin, exact-charge, and routed-card boundaries.
After activation, read
the live `browser_type`, `browser_exec`, and `browser_vault` schemas in a fresh session and complete
one real passkey login and one real code-based login when available.

## Retirement and Rollback

This patch is a private policy choice and is not expected upstream. Retire it if the owner
explicitly restores the prior policy. Roll back the reviewed Hermes and agents commits together,
including their synchronized guidance, tests, fixtures, and maintenance record.

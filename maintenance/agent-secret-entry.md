# Agent Secret Entry

Fork patch identity: `agent-secret-entry`.

## Behavior

The vault tool descriptions and the vault note appended to the browser input tools
(`browser_type`, `fill_input` inside `browser_exec`) let the agent type a password or
one-time code it fetched itself from an authorized store for that service. Authorized stores
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

- A value shown on the page, appearing incidentally in unrelated tool output, or given by
  the user in chat is never a source. Only a value deliberately fetched from an authorized
  store for that service counts. The agent never asks for or accepts a secret in chat and never repeats
  one in a reply.
- Card numbers and CVCs go only through `browser_vault_fill`, with its per-fill
  confirmation.
- Passkeys, hardware keys, and app approvals remain user device actions (`no_code_field`).

Upstream `96fbc47f14f` added the "never type a password" text after a dogfood run in which
the model typed a demo password shown on the page. This patch keeps that case forbidden and
permits only self-fetched values from authorized stores. The owner accepts that a typed
value passes through the model context and provider logs during entry. The agents
`credentials` skill (`references/secret-entry.md`) owns the full route order and the
exposure contract.

## Surfaces

`model_tools.py` (`_VAULT_NO_PASSWORD_NOTE`) and `tools/browser_vault_tool.py`
(the `browser_vault_list` empty hint, and the `browser_vault_list`, `browser_vault_save_login`
and `browser_vault_enter_code` descriptions). The patch is description text only and changes
no fill, redaction, or origin-binding code.

## Verification

Run `scripts/run_tests.sh tests/tools/test_browser_vault.py
tests/tools/test_browser_vault_anonymous_otp.py tests/tools/test_browser_vault_camofox.py
tests/tools/test_browser_vault_manager_card.py tests/tools/test_model_tools.py`. After
activation, read the live `browser_type` and `browser_vault_*` schemas in a fresh session.

## Retirement and Rollback

This patch is a private policy choice and is not expected upstream. Retire it if the owner
restores the vault-only rule. Revert its single commit to roll back. That commit touches
only the two surfaces above and this record.

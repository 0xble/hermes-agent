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

The upstream overwrite restored a blanket "never type" instruction after an earlier dogfood
fix. This record preserves the useful boundary, never type values copied from page content or
chat, without blocking authorized self-fetched passwords or codes. The owner accepts that a typed
value passes through the model context and provider logs during entry. The agents
`credentials` skill and shared execution instructions own the route order and blocker rule.

## Surfaces

`model_tools.py` (`_VAULT_NO_PASSWORD_NOTE`), `tools/browser_vault_tool.py` (the empty-list
hint, post-fill next-step guidance, enter-code fallback docstring, and consolidated `browser_vault`
schema), `evals/prefix_rediet/worker.py`, and the bundled
`skills/autonomous-ai-agents/computer-use/SKILL.md`. The matching canonical guidance lives in
`/Users/brianle/Repos/agents/sources/skills/credentials/references/entry.md` and
`/Users/brianle/Repos/agents/sources/instructions/shared/execution.md`. The patch changes only
secret-entry guidance and post-fill routing text; it changes no fill, redaction, or origin-binding code.

## Verification

Run `scripts/run_tests.sh tests/tools/test_browser_vault.py
tests/tools/test_browser_vault_anonymous_otp.py tests/tools/test_browser_vault_camofox.py
tests/tools/test_browser_vault_manager_card.py tests/tools/test_model_tools.py tests/ci/test_prompt_surface.py`.
The model-tools regression must prove that a fresh rendered schema allows a password or code
fetched from 1Password, email, or SMS, while card numbers and CVCs remain vault-only. After
activation, read the live `browser_type`, `browser_exec`, and `browser_vault` schemas in a
fresh session and complete one real emailed-code login using the fetched-code route.

## Retirement and Rollback

This patch is a private policy choice and is not expected upstream. Retire it if the owner
restores the vault-only rule. Revert its single commit to roll back. That commit touches
only the two surfaces above and this record.

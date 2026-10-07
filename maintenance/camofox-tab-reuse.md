# Camofox tab reuse

Load this unit when changing Camofox task bindings, end-of-turn browser cleanup, tab adoption, or `browser_handoff` continuity.

## Required behavior

- **Bindings survive turns.** For a Hermes-managed identity (named account, `managed_persistence`, or external `user_id`), end-of-turn `camofox_soft_cleanup` keeps the task's tab binding, account and vault protection, and marks it `carried`. The next turn continues in the same tab, including the one a user logged into after `browser_handoff`. A carried binding may switch account on the next turn; within a turn the account stays fixed. Ephemeral sessions still close at end of turn.
- **Release at agent close.** `AIAgent.close()` releases the bindings of every task id the agent ran (`release_task_bindings`), deleting any ephemeral session a cut turn left open. It never uses the close-time `session_id`, because temporary hygiene and compression agents share the live session's id but own none of its tabs.
- **Task-id-less turns.** Callers that omit `task_id` (CLI one-shot, direct `AIAgent`) get a new id each turn; `_bind_turn_identity` moves the managed binding to the next turn's id.
- **Compression carries the binding.** Gateway and CLI turns use the session id as browser task id, and compression rotates it. `_carry_session_state_to_child` copies the managed binding to the continuation id. The old id serves the rest of the turn, then its soft cleanup drops it, quarantining a protected tab.
- **Adopt before creating.** With no binding, `_ensure_tab` lists `GET /tabs` for the session's own userId only. It refuses tabs bound to another task in this process, protected or quarantined tabs (memory and disk; unreadable quarantine fails closed), and tabs this process saw reported stale. It prefers the target URL's origin, then the newest `__shared_identity__` tab, then the newest tab of the task's own group. Other tasks' tabs are left alone, except that an external `user_id` with `adopt_existing_tab` may take any eligible tab. Selection and binding happen under the sessions lock.
- **Handoff is atomic.** While a handoff's `/open` is in flight, adoption skips the shared identity group. The handoff binds its tab under the sessions lock and detaches any other task bound to it.
- **Page actions after loss.** A page action adopts only for a session never bound in this process (new task, gateway restart). A binding cleared later (stale tab, handoff takeover, release) is rebound only by `browser_navigate`, so a page action never continues on a replacement tab. Stale-tab records are pruned to the server's listing on each adoption.
- `adopt_existing_tab` now governs only external `user_id` identities. Hermes-managed identities always adopt their own tabs.

## Provenance and patches

- Fork patch identity: `camofox-tab-reuse`.
- Defect: `cleanup_task_resources` → `cleanup_browser` → `camofox_soft_cleanup` dropped the task's binding every turn except in global headed mode, so each turn's `_ensure_tab` posted `/tabs`. Adoption was off by default and picked the newest tab of any group. After `browser_handoff` the next turn opened a different tab from the one the user had logged into (37 of 149 handoffs since 2026-09-09).
- Upstream search (2026-10-06): no issue or PR covers per-turn Camofox tab loss. Related open issues [#80276](https://github.com/NousResearch/hermes-agent/issues/80276) (stale 410) and [#92361](https://github.com/NousResearch/hermes-agent/issues/92361) (navigate hardening) do not. Named accounts and handoff are fork-only, so this patch stays fork-only.
- Surfaces: `tools/browser_camofox.py`, `agent/client_lifecycle.py`, `agent/conversation_compression.py`, `agent/turn_context.py`, `hermes_cli/config_defaults.py`, browser docs.

## Verification

`scripts/run_tests.sh tests/tools/test_browser_camofox_tab_reuse.py` drives the real tool dispatch, real end-of-turn cleanup and compression handoff against a stub Camofox server with a temporary `HERMES_HOME`. Its core tests fail on the pre-patch source. Also run the other `tests/tools/test_browser_camofox*.py` files, `tests/agent/test_browser_output_egress.py`, and `tests/tools/test_browser_vault.py`. A live check may use the local Camofox only with a throwaway identity from a temporary `HERMES_HOME`, deleting its session afterwards.

## Retirement and rollback

Retire when upstream ships per-turn binding retention and safe adoption for managed Camofox identities with handoff continuity. Roll back by reverting this patch's commit. No server, profile, credential or state migration occurs. Quarantine records are unchanged.

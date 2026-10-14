# Agent wait ergonomics

Load this unit when changing `process_manage` wait defaults or model-facing timeout guidance, or delegation list observations. For queued-list changes also read [async delegation capacity admission](async-delegation-capacity.md); this patch changes observation, not admission or delivery.

## Intentional fork divergence

- **Patch identity:** `agent-wait-ergonomics`.
- **Source baseline:** fork `origin/main` at `9371a3ebcdffd3c4bcf44be49dbc7e3989feee44`.
- **Contribution route:** original narrow fork patch, implementing the requested turn-cost reduction rather than changing scheduler capacity. Not submitted upstream.
- **Rationale:** default long waits and identical live-state lists encourage repeated tool calls while completion delivery requires the parent to release its turn. The supplied seven-day tool audit identified this as an interactive-session cost.
- **Upstream disposition:** searches for process wait timeout and delegation polling did not identify an equivalent donor. Related upstream wait/yield work does not establish this complete contract. Reassess against released upstream changes rather than assuming equivalent wording alone.
- **Fork delivery:** branch `fix/agent-wait-ergonomics`; published PR is the delivery record. No runtime activation is included.

## Adoption boundary

One helper, `wait_plan(notify_on_complete, max_timeout, completion_reaches_session=...)` in `tools/process_registry_wait.py`, decides both the default and the guidance. Only when a completion notice will actually arrive, meaning the process has `notify_on_complete`, its completion is routed to the current session (ordinary session-key/origin ownership; not a process owned by a delegated `sa-` task), and `can_receive_later()` (`async_delivery_supported() and not is_delegated_child_context()`) is true, does an omitted timeout default to 30s and the guidance say the session will be notified, should end the turn, and should not wait again. Every other case keeps the `TERMINAL_TIMEOUT` default and says to wait again with a bounded timeout or kill before finishing, plus handoff to the parent for a delegated child. That covers a process without notify in a reachable session, unroutable/foreign processes, stateless sessions (cron, one-shot CLI, Kanban workers, stateless HTTP) and children: nothing will notify them, so deferring would lose the result. The unchanged non-empty delegation list says to end the turn only when every entry is a live or queued child whose completion arrives without parent action; any `retry_*` row (including resume-due and terminal rows) remains fully visible with its status/retry metadata and does not get end-turn guidance. Preserve the existing configured maximum, truthful notification wording, and compact unchanged-state response for ordinary live/queued children; an unchanged empty list keeps the no-live-subagents, nothing-to-steer-or-stop wording. A list's elapsed uptime is not progress; ownership, queued/retry metadata and actual child progress remain meaningful. Keep the observation cache local to the owner and session, not a cross-conversation global. Upstream adoption must also preserve control actions and completion routing; suppressing unchanged output must never suppress result delivery or retry/resume visibility.

## Proof and retirement

Authoritative regressions: `tests/tools/test_wait_polling_ergonomics.py`, alongside existing process-registry, delegate-control and retry-control tests. They exercise registered/public control paths without real wait durations. They assert invariants on key tokens, not exact phrasing: notify plus reachable gives the short default and end-the-turn guidance; no notify, a stateless session (driven through `declare_stateless_channel()`) or a delegated child gives the full default and wait-again guidance; a child also gets the handoff option; and the unchanged list respects `can_receive_later()`. Tests drive time through the `_monotonic` and `_now` module seams, not the shared `time` module.

Retire when a released upstream version passes those boundaries. Revert the owning patch to roll back; observation state is ephemeral and there is no database migration. `heavy-slot status` belongs to dotfiles' CLI source, not this fork patch.

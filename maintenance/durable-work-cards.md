# Durable work routes to Kanban cards

Fork patch identity: `durable-work-cards`.

Deliberate fork-only patch, chosen by Brian on 2026-11-27. Background delegations end with the
gateway, so `delegate_task`'s guidance sends durable work to a Kanban card keyed to its task
instead of cron or a background terminal. The fork's "How Work Flows" instructions carry the
same rule; the tool description must not contradict them.

## Required behavior

- The `delegate_task` description says: work someone would have to remember to redo if it were
  lost (anything that changes the world, runs over about 20 minutes, or must not be dropped)
  goes to a Kanban card keyed to its task (`kanban_create` with `idempotency_key: "task:<id>"`
  and `goal_mode=true`); a scheduled job goes to `cronjob`.
- The `kanban_create` description invites any session, not only orchestrator workers, to start
  durable work there instead of a background delegate. Upstream frames it as a worker fan-out
  tool, which topics read as not meant for them.
- Quick, disposable work whose result only matters to the conversation stays a delegate.
- The `kanban_create` description also asks for `Done when:` and `Proof:` lines in the body, which
  the card's claim requires.
- Guidance changes must reach running sessions. `tool_pin_version()` keys each session's tools[] pin
  by the running commit from `hermes_cli.version_info`, so the first turn after a deploy of new code
  takes current tool definitions once. It previously read the frozen `hermes_cli.build_info` shim,
  which has no identity, so every deploy of one release reused old pinned descriptions forever.

## Source surfaces

`tools/delegate_tool.py` (`_DESCRIPTION_HEAD`), `tools/kanban_tools_schemas.py`
(`KANBAN_CREATE_SCHEMA`), `tools/mcp_tool_agent.py` (`tool_pin_version`), and their `tests/fixtures/prompt_surface/tools/` snapshots.

## Retirement

Retire when upstream's delegate guidance names Kanban cards for durable work, or when the fork
stops using Kanban cards for durable work.

Contract tests: `tests/tools/test_durable_work_cards.py`.

# LPG Runtime Preservation

Fork patch identity: `lpg-runtime-preservation`.

## Required Behavior

The signed LPG runtime follows the modern fork baseline without losing the
business-only behavior carried by `deploy/lpg-runtime-base`. Preserve the
`goals.notices` switch across CLI, gateway and TUI surfaces independently of
autonomous goal execution. Explicit control replies and loop outcomes remain
visible. Keep configured catalog-provider credentials usable by fallback routes.
Slack assistant status stays in DMs, and allowlisted human Web API posts retain
the existing self-bot and classic-bot echo guards.

## Adoption and Proof

This is a fork adaptation, not a new upstream goal implementation. Goal policy
is owned by [autonomous goals](goal-autonomy.md). Compare the business overlay
against the current baseline before repinning a signed LPG artifact. The
`test_goal_notice_surfaces`, `test_goal_notices_config`,
`test_provider_catalog_credentials`, `test_slack`, and
`test_slack_api_human_senders` suites own the preservation proof.

Retire each adaptation only when the selected baseline provides its behavior
and the same regressions pass without it. Runtime qualification and activation
remain separate from source publication.

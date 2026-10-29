# LPG Runtime Preservation

Fork patch identities: `lpg-runtime-preservation`, `loop-cap-final-answer`,
  `fallback-declared-transport`, `subagent-failure-notice-gate`,
  `lpg-backport-qualification`, `lpg-provider-credentials`,
  `lpg-qualification-fixtures`, `goal-notice-visibility`.

## Published Legacy Ancestry

The preserved deployment lineage includes already-published commits without
trailers. Their stable patch IDs below bind ownership to exact content, using
check_fork_patches.py's existing backfill route with an explicit Myers diff.
A stable patch ID ignores whitespace and hunk offsets, but not an algorithm's
choice of hunks. Both generation and validation must select Myers rather than
inherit a user's `diff.algorithm`. Do not advance the trailer floor, exempt the
branch, or rewrite the legacy deployment history.

Fork-Patch-Backfill: 89d3491a7e20cbb676c7801aa7a0e89a043f3b56; lpg-runtime-preservation
Fork-Patch-Backfill: de5f7267a4cdab486fd2c213495529c80c130e5c; lpg-runtime-preservation
Fork-Patch-Backfill: 3873652a825a538695e148abfaa0b6350465354f; lpg-runtime-preservation
Fork-Patch-Backfill: 2dbbcea7fcb6afa15af3127fd105523107647a4e; lpg-runtime-preservation
Fork-Patch-Backfill: a244eb31eb443f53a9d13d92d3d5098c875d15c6; lpg-runtime-preservation
Fork-Patch-Backfill: 1c3635717538aebc81a0c8c6d9ef74b6000a55ee; lpg-runtime-preservation
Fork-Patch-Backfill: 62ec021aa76959f60d1e5b0020f707062d8676f1; lpg-runtime-preservation
Fork-Patch-Backfill: eac7c06a0ed1cea3812c16859a1e11e7ad1f1366; lpg-runtime-preservation

These cover the legacy Slack status API, optional documents extra, quiet
successful fallbacks, reset-aware fallback cooldown, minimum-switch reset,
allowlisted human Web API senders, DM-only status and automatic-goal visibility.
The retained identities above also own the lineage's final-answer cap,
declared fallback transport, claimed-failure notice suppression, credential
lookup and qualification fixtures. Current behavior and regressions, rather
than a legacy patch's presence in ancestry, decide whether an adaptation stays.

## Required Behavior

The signed LPG runtime follows the modern fork baseline without losing the
business-only behavior carried by `deploy/lpg-runtime-base`. Preserve the
`goals.notices` switch across CLI, gateway and TUI surfaces independently of
autonomous goal execution. Explicit control replies and loop outcomes remain
visible. Keep configured catalog-provider credentials usable by fallback routes.
Slack assistant status stays in DMs, and allowlisted human Web API posts retain
the existing self-bot and classic-bot echo guards.

## Document Runtime Compatibility

The shared release's shipped document scripts and Chromium tree are pinned to
Playwright 1.60.0. Keep that version in every existing Playwright selector so
`uv lock` resolves the documents and google-meet extras together without a
second browser or a new host dependency. Preserve the shared build/verifier's
lxml 6.1.3 and pandas 3.0.6 pins on Python 3.14. These are deployment compatibility
pins, not changes to the document skill or permission to provision another
Chromium revision. The legacy Python 3.13 lock branch stays unchanged.

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

# TUI Session Event Publication

Fork patch identity: `tui-event-publication-order`.

## Preserve

The replay sequence and each session's subscriber mailboxes must observe the same publication order. Keep 1 stable `FanoutTransport` per live session, even with 1 or 0 attached peers. Membership changes must not create a second physical writer for an already attached peer. Keep socket writes and overflow closure outside publication and membership locks.

Connector progress is authorized against its captured runtime owner at admission. A rejected stale owner must not allocate a replay entry. Preserve the compute-host drain barrier before `turn.end` and shutdown, since its next action may hard-exit without running daemon writers.

Settlement must not initialize tool discovery or resolve cold Git provenance merely to populate optional UI metadata. Query the existing tool registry and share the canonical version cache through its nonblocking read. A cold snapshot keeps its empty version until resolution finishes, and subsequent snapshots report the resolved identity. Final session events still drain before `turn.end`.

This patch does not change queue limits, wire contracts, replay retention, or the multiclient model's seed-11 invariant.

## Upstream Provenance And Adoption

Inspected upstream `main` at `4dcef215cfe41bd2d02c2d1dad6e257a18101216`. Its fanout queues do not combine replay stamping with mailbox admission. [PR #139643](https://github.com/NousResearch/hermes-agent/pull/139643) revokes detached batches, and [PR #140292](https://github.com/NousResearch/hermes-agent/pull/140292) raises the frame limit. Neither replaces this ordering correction. Retain the accepted fork's limits while reconciling those independent changes.

[PR #108154](https://github.com/NousResearch/hermes-agent/pull/108154) concerns WebSocket backlog accounting, and [PR #114120](https://github.com/NousResearch/hermes-agent/pull/114120) concerns compute-host serialization fallback. Keep those adoption decisions separate from per-session event ordering.

Contribution route: upstream-applicable bug fix, adapted here to the fork's captured connector owner and detached-session lifecycle. Only a fork draft PR is authorized for this delivery. No upstream submission or runtime promotion is included.

Retire this patch when an adopted upstream revision provides equivalent stable session ownership, atomic stamp-and-admit ordering, stale-owner rejection and process-exit draining, with the invariant cohort passing unchanged.

## Verification Boundary

The deterministic publication and lifecycle cohort lives in `tests/tui_gateway/test_session_event_publication.py`. Preserve baseline RED evidence for normal and connector publishers with 1 and 2 peers, then run that cohort, all `tests/tui_gateway`, and `tests/e2e/core/terminal`. The multiclient model must retain and pass seeds 11, 23 and 37 across repeated runs. Actual command results belong in the PR verification record, not in this evolution contract.

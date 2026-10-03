# Progress bubble flood deferral

Load this unit when changing gateway tool-progress bubbles: the overflow split,
the send-or-edit tick, or how a failed progress edit is classified.

## Required behavior

- A flood-control refusal of a progress edit (a `retry_after`, or `flood` /
  `retry after` in the error) keeps the bubble editable and its buffered lines.
  It sets a per-turn deadline of `max(retry_after, 1.5s)`. Until then, new lines
  are only buffered. No edit, split, or send happens, and turn cleanup skips its
  final flush, so an unsplit over-limit buffer is never sent as one edit. After the
  deadline, one edit carries everything buffered.
- A transient (`retryable`) edit failure keeps the bubble and retries the next tick.
- A permanent edit failure (message gone, not editable) moves progress to one fresh
  bubble. Only a second permanent failure in a row, before any successful edit,
  sets `can_edit=False`.

## 2026-10-02 incident

A long Telegram DM-topic turn filled its progress bubble near the 4096-character
limit at 17:58 PDT while several topics in the same chat shared one flood budget.
The overflow edit was refused locally with `flood_control:1.0`. The overflow path
treated any non-`retryable` failure as permanent and set `can_edit=False`, so every
later tool line in that turn became its own reply-anchored message (about 80 in the
following hour). The normal edit path also answered a flood refusal by sending the
newest line as a fresh message, spending budget inside the penalty.

**Upstream comparison (2026-10-02):** no issue or PR matched on searches for
overflow, `can_edit`, flood, and separate progress messages. The same code is on
`upstream/main` at `37bd0843c39b`. Contribution branch
`upstream/progress-overflow-flood-defer`.

## Patch

**Patch identity:** `progress-overflow-flood-defer`. Source surface:
`gateway/run_turn_runner.py` (`_roll_progress_overflow_if_needed`,
`_progress_send_or_edit`, `_edit_failure_is_deferrable`, `_progress_deferred`,
`_abandon_progress_bubble`, `_flush_progress_edit`).
Proof surface: `tests/gateway/test_run_progress_topics.py`
(`test_flood_refused_overflow_edit_keeps_progress_in_bubbles`,
`test_uneditable_progress_bubble_continues_in_a_fresh_bubble`,
`test_persistent_flood_does_not_retry_per_line_or_flush_oversized`), all red on the base.

**Rate boundary:** adds no request type. During a flood window a turn's progress
makes zero requests, where before it made one edit or send per incoming line. A
permanent failure sends one fresh bubble as before. Edits stay on the existing 1.5-second progress throttle behind the adapter's
shared per-chat slot and flood window (see [Telegram delivery](telegram-delivery.md)).

**Retire** when an upstream release passes all three regressions without this patch.
**Roll back** by reverting the logical patch. No configuration or persistent state.

# Hygiene deferred adoption

Load this unit when changing gateway hygiene turn-hold handling (`gateway/run_turn.py::_hmwa_hygiene_on_turn_hold`),
deferred summary adoption, the hygiene token count stored on the session entry, or the lease-less
`archive_and_compact` callers (proactive prune, micro-compaction).

## Required behavior

- When the turn-hold expires but the watermark-fenced worker keeps its commit admission, the user sees no notice.
  The turn-hold is logged at INFO only, because the summary is adopted later at the fenced commit.
- The cancel path, where nothing is adopted, keeps the `turnhold_deferred` notice. Sometimes a kept-admission worker
  stops on a real summary abort (`_last_compress_aborted`). It then posts the ordinary `hygiene_failed` warning once
  and escalates the failure cooldown, the same way the inline abort path does. A no-op or superseded worker records
  only the flat retry spacing and stays silent.
- Deferred adoption resets `last_prompt_tokens` to 0 on the entry and in the store, and it bumps a per-session
  adoption epoch. When a turn's epoch moved while it ran, that turn's end-of-turn prompt count measured the
  pre-compaction transcript, so the turn stores 0 instead. The next hygiene check then estimates the compacted
  transcript and does not re-fire on it.
- A lease-less `archive_and_compact` caller passes `held_messages`. If an active summary row is absent from the held
  history, the commit raises `StaleHeldHistory` inside the write transaction and writes nothing. That rewrite was built
  from the generation a compaction already replaced, and committing it would re-insert the whole old transcript beside
  the summary.

Fork patch identity: `hygiene-deferred-adoption`.

## Why

On 2026-10-13, one Telegram session posted the deferral notice four times in an hour. Every summary was adopted about
two minutes later. The same session re-triggered hygiene on almost every turn. After each deferred commit, the active
transcript grew back to the full pre-compaction size, because the stale-generation rewrite re-inserted it. The stored
prompt count was also stale, so hygiene ran a 93-second compression for a 33-to-31-message trim.

## Verification

- `scripts/run_tests.sh tests/gateway/test_session_hygiene_turnhold_adoption.py tests/hermes_state/test_stale_held_generation_guard.py`
- Live: a deferred adoption logs `summary adopted at the watermark-fenced commit boundary` and sends no chat notice.
  The next turn's hygiene check does not report a pre-compaction `actual` token count.

## Retirement

Retire after upstream silences adopted turn-holds and fences lease-less rewrites against a newer summary generation.
Roll back the patch commit and both regression files. No persistent-data migration is required.

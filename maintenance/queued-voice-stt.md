# Queued voice transcription

Load this unit when changing busy-session queueing, pending-event STT caching,
transcript echo, or queued follow-up drain.

## Required behavior

- A voice message queued behind an active turn starts speech-to-text immediately
  in the background. The queue acknowledgement is never delayed by STT.
- The original event stays in FIFO order. The model does not see it until the
  current turn finishes.
- When transcript echo is enabled, the `🎙️ "..."` echo is sent as soon as STT
  finishes, marked interim so a live stream is not sealed.
- The queue drain, interrupt monitor, and prefetch share one in-flight STT call
  per event. The drain awaits it rather than transcribing again, and the echo
  ledger prevents a second echo.
- STT failure falls back to the existing drain behavior: transcription is retried
  at drain time and the audio placeholder is used if it still fails.
- At most two queued-voice transcriptions run at once per runner. Prefetch runs
  on every FIFO enqueue, including `/queue`. The shielded inner STT task is
  tracked for shutdown, and completion or cancellation clears its handle while a
  finished result stays reusable after the outer awaiter is cancelled.
- Under session authority a busy messaging follow-up is committed to the durable FIFO
  (`_hm_authority_admission`) and its turn runs a new event restored from the row. Once that
  row is durably `queued`, the live event's STT starts (the `admitted_queued` hook) and is
  published under its transport identity; the claimed turn adopts the transcript and echo
  ledger in `_prepare_inbound_message_text` instead of transcribing again. A refused or
  fenced admission starts no STT. Steer-fallback and interrupt transcripts are adopted too.
- Preserve queue admission receipts when integrating the shared FIFO prefetch:
  `_queue_or_replace_pending_event` returns true only for an admitted event and
  false for a missing adapter or a full queue. Refused voice events start no STT;
  admitted events retain their FIFO slot and start prefetch from `_enqueue_fifo`.
  Do not restore the redundant prefetch call in the admission wrapper.

## Provenance and patches

- Fork patch identity: `queued-voice-eager-stt`. Restores the archived fork's
  immediate busy-voice transcription (archived `d55304c`, `0fd0161`, `ea80b55`)
  for the queue path.
- Upstream: [issue 58780](https://github.com/NousResearch/hermes-agent/issues/58780)
  and [PR 73518](https://github.com/NousResearch/hermes-agent/pull/73518) fixed
  steer-path STT. Queue mode still transcribes only at drain time upstream. Own
  [PR #121063](https://github.com/NousResearch/hermes-agent/pull/121063) contributes
  eager queued transcription; its 2026-09-28 head `9e557990617d7138e7f05cf53107f1db8f4f8700`
  adds the concurrency bound, `/queue` path, and cancellation tracking after review.

## Verification

Contract tests: `tests/gateway/test_queued_voice_durable_fifo.py::test_voice_queued_behind_running_turn_echoes_before_it_finishes_and_is_transcribed_once`,
  `tests/gateway/test_telegram_voice_v0_regressions.py::test_queued_voice_transcribes_immediately_and_drain_reuses_it`,
  `tests/gateway/test_telegram_voice_v0_regressions.py::test_fifo_voice_from_queue_command_prefetches_before_drain`

`scripts/run_tests.sh tests/gateway/test_queued_voice_durable_fifo.py
tests/gateway/test_telegram_voice_v0_regressions.py
tests/gateway/test_queue_consumption.py tests/gateway/test_busy_session_ack.py`.
The durable-FIFO regression drives real Telegram busy dispatch through session-authority
admission and restore: STT starts and the echo lands while the first turn still runs, and
the claimed turn reuses the single transcript. The in-process regression holds STT open,
proves it started before the drain, and proves the racing drain reuses the single call and
single interim echo.

The 2026-09-23 patch hooked only the in-process pending slot. The v0.22.5 sync
(`fa26214165`, live from release `d32adcc276c4` on 2026-10-29) routed busy messaging
follow-ups through the durable FIFO first, so queued voice waited for its turn again.

## Retirement and rollback

Retire when the selected upstream release transcribes queued voice at admission
with a shared in-flight cache. Roll back by reverting the logical patch. No
persistent state changes.

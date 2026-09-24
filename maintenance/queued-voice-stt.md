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

## Provenance and patches

- Fork patch identity: `queued-voice-eager-stt`. Restores the archived fork's
  immediate busy-voice transcription (archived `d55304c`, `0fd0161`, `ea80b55`)
  for the queue path.
- Upstream: [issue 58780](https://github.com/NousResearch/hermes-agent/issues/58780)
  and [PR 73518](https://github.com/NousResearch/hermes-agent/pull/73518) fixed
  steer-path STT. Queue mode still transcribes only at drain time upstream. No
  upstream issue or PR proposes eager queued transcription as of 2026-09-24.

## Verification

`scripts/run_tests.sh tests/gateway/test_telegram_voice_v0_regressions.py
tests/gateway/test_queue_consumption.py tests/gateway/test_busy_session_ack.py`.
The regression holds STT open, proves it started before the drain, and proves the
racing drain reuses the single call and single interim echo.

## Retirement and rollback

Retire when the selected upstream release transcribes queued voice at admission
with a shared in-flight cache. Roll back by reverting the logical patch. No
persistent state changes.

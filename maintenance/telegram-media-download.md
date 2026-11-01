# Telegram inbound media recovery

**Patch identity:** `telegram-media-download`.

Load for inbound Bot API metadata/byte retrieval, its HTTP timeout policy, or upstream media-retry adoption. Read [ingress non-blocking](telegram-ingress-nonblocking.md) before changing notice dispatch or polling ownership.

The local divergence is retry-only whole-fetch recovery across all attachment families on the live bot's existing request pool. At most three attempts use 2s/5s backoff and 1x/2x/3x authored HTTP timeouts, all within one 60s wall-clock budget covering metadata, bytes, and backoff. This finishes before the next 90s pending-update watchdog probe even when a slow transfer never hits an inactivity timeout. It must not retry ambiguous outbound sends, replace the live bot, or borrow a user's Telegram credentials. Existing size, authorization, privacy, and failure-notice ownership remain upstream of the recovery helper.

Provenance: upstream [PR #145217](https://github.com/NousResearch/hermes-agent/pull/145217), revision `1c423960a16fd77e382bfa88ce760e6d830e34b5`, is open as inspected on 2026-10-31. The candidate adapts its retry/call-site approach and regression cases into `telegram_media_download.py`, adds authored-budget scaling and a total deadline, and includes stickers/observed attachments. Upstream disposition: open donor, not accepted baseline. Fork delivery: draft [PR #1037](https://github.com/0xble/hermes-agent/pull/1037), not deployed.

Proof owners: `tests/gateway/test_telegram_file_retry.py` and `tests/plugins/test_telegram_media_download.py`; the latter requires the optional `telegram` extra. Real PTB over HTTPX MockTransport covers metadata/byte retries on the same client and exactly one actual sendMessage resend notice after exhaustion. Slow-retry cases exercise the real pending-update watchdog during recovery, then verify dispatch progresses without a polling restart after either success or deadline expiry. Permanent errors are not retried and cancellation propagates.

Adopt a compatible upstream fix only after comparing metadata and byte-stage recovery, authored timeouts, total recovery budget, permanent-error/cancellation behavior, notice ownership, and every attachment call site. Retire this unit when upstream owns those properties. Rollback is a source revert; there is no configuration or schema migration.

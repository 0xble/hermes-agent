# Telegram inbound media recovery

**Patch identity:** `telegram-media-download`.

Load for inbound Bot API metadata/byte retrieval, its HTTP timeout policy, or upstream media-retry adoption. Read [ingress non-blocking](telegram-ingress-nonblocking.md) before changing notice dispatch or polling ownership.

The local divergence is bounded whole-fetch recovery across all attachment families, followed by an isolated Bot API request-pool fallback that preserves configured proxy/TLS/IP and custom API endpoints. It must not retry ambiguous outbound sends, replace the live bot, or borrow a user's Telegram credentials. Existing size, authorization, privacy, and failure-notice ownership remain upstream of the recovery helper.

Provenance: upstream [PR #145217](https://github.com/NousResearch/hermes-agent/pull/145217), revision `1c423960a16fd77e382bfa88ce760e6d830e34b5`, is open as inspected on 2026-10-31. The candidate adapts its retry/call-site approach and regression cases into `telegram_media_download.py`, adds authored-budget scaling and an isolated-client fallback, and includes stickers/observed attachments. Upstream disposition: open donor, not accepted baseline. Fork delivery: candidate only, not runtime-qualified or deployed.

Proof owners: `tests/gateway/test_telegram_file_retry.py` and `tests/plugins/test_telegram_media_download.py`; the latter requires the optional `telegram` extra and exercises real PTB over HTTPX MockTransport, including custom endpoints and closed fallback pools. Passing syntax checks does not qualify this change; targeted execution, independent exact-head review, and the repository gate are still pending on initial handoff.

Adopt a compatible upstream fix only after comparing metadata and byte-stage recovery, authored timeouts, permanent-error/cancellation behavior, fallback ownership, and every attachment call site. Retire this unit when upstream owns those properties. Rollback is a source revert; there is no configuration or schema migration.

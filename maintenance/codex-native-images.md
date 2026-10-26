# Completed Responses Images

Fork patch identity: `codex-native-images`.

## Contract

Completed `image_generation_call` output from native or custom Codex Responses endpoints must not normalize into an empty assistant turn. Validate and cache returned image bytes through `agent.provider_media`, then preserve the normal `MEDIA:` marker as an assistant message and replay item. A malformed result or failed write yields a truthful, nonempty attachment-failure message. Partial images are not advertised as complete. Image bytes never enter assistant content or replay state.

## Source and Scope

The local proxy implicitly injects native image generation even when Hermes sends only function tools. A live isolated CLI reproduction returned a completed image plus an empty message, normalized to zero visible characters and zero function calls. The deleted weekly org-chart payload is unavailable, so the historical trial's exact cause is inferred rather than recovered.

Pinned fork base: `82cfb861e2b6487c7b92f0fef4b3d7780a599572`. Upstream guidance inspected at `82732650a18f303c2ac3e305f3c12a122ef1649a`. Related upstream work: [issue #134336](https://github.com/NousResearch/hermes-agent/issues/134336) and [PR #134346](https://github.com/NousResearch/hermes-agent/pull/134346), plus broader native-tool configuration PR #23245. These are related references, not adopted commits or evidence this fork candidate passed their tests.

This patch is deliberately narrower than #134346. It repairs completed items already present at the adapter boundary, with the existing filesystem/media marker contract. It does not add tool injection, partial-image assembly, new provider settings, or a cross-turn artifact delivery ledger. When an endpoint strips image bytes before this boundary, the consumer cannot recover them. Mixed function turns retain the marker in assistant history but this patch does not guarantee its eventual delivery across later tool rounds. The existing upstream PR tracks that broader lifecycle work.

## Verification and Retirement

Focused regressions: `tests/agent/test_codex_generated_images.py`, plus existing Responses adapter and Codex transport tests. Check image-only completion, native/custom issuer, safe persisted replay, malformed payload, storage failure, partial-image status, and coexistence with text/function output. Runtime promotion and real Telegram delivery are separate parent-owned checks.

Retire when a released upstream fix satisfies this contract. Rollback is the commit carrying this fork identity. No migration, proxy reconfiguration, or runtime restart is required for source rollback.

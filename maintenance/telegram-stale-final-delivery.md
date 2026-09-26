# Telegram stale final delivery

## Required behavior

When a completed response to a private Telegram DM topic receives a definite deleted-topic refusal, deliver the complete response once at chat root without the dead topic or reply anchor. If part of a split response landed, send only a root notice pointing to session history, never a duplicate of already visible text. Interim output and ambiguous timeouts must never be redirected to root. Confirmed stale bindings are pruned; existing topic/title and normal forum-topic routing are unchanged.

## Provenance and patches

- **Identity and status:** active fork patch `telegram-stale-final-delivery`.
- **Source / fork refs:** archived HERMES-018 `session-context.md`, archived `archived/main`; current fork base `origin/main` `f17c0b141f78cf54aa7446289cb0c4c7874fdf62`.
- **Surfaces:** `gateway/platforms/base.py`, `plugins/platforms/telegram/adapter.py`, `tests/gateway/test_telegram_stale_final_delivery.py`.
- **Adaptation:** Bot API only; no MTProto/custom packs, title/attachment context, or change to group/forum fallback. Existing upstream PR [#93874](https://github.com/NousResearch/hermes-agent/pull/93874) directly addresses live deleted-topic recovery, so no competing upstream PR is created. Its open head does not guard interim messages or partial split delivery; fork adaptation does both.
- **Verification:** `scripts/run_tests.sh -j 6 tests/gateway/test_telegram_stale_final_delivery.py tests/gateway/test_telegram_thread_fallback.py tests/gateway/test_delivery.py` and `scripts/run_tests.sh -j 6 tests/gateway/`; ruff and `scripts/check_fork_patches.py --source-only`.
- **Retirement:** remove the fork patch only after a released upstream implementation recovers confirmed deleted private-topic final replies without leaking interim sends to chat root or repeating already-delivered split content. Preserve stale binding pruning and unrelated topic title/icon ownership.

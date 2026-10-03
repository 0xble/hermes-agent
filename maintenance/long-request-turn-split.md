# Long request turn split

Load this unit when changing the oversized-active-turn split (#80449), its size gates, or how a split request is restated after the compaction handoff.

## Required behavior

An in-progress turn that outgrows the protected-tail soft ceiling must stay compressible even when its opening request is long. The only size guard on that request is the token soft ceiling, because the split request is restated verbatim after the handoff by `_reappend_inflight_user_task`. A character cap on the request pins the whole turn in the tail: the compress window collapses to nothing, every automatic pass becomes a `no_progress` structural backoff, `/compress` hits the same empty window, and the session grows into a provider context overflow. Long `/goal` continuation prompts hit this on every autonomous run. Requests too large for the ceiling, or with non-text parts, still stay anchored verbatim in the tail.

## Provenance and patches

Fork patch identity: `long-request-turn-split`.

Reproduced on 2026-10-03 from Telegram session `20261003_122557_13d10929`: a 2,637-character goal prompt, 349 messages, about 232k estimated tokens, window `0..1`. With the cap removed the same transcript yields a `0..311` window with about 214k compressible tokens. Upstream [#130506](https://github.com/NousResearch/hermes-agent/pull/130506), salvaging #129081 and merged 2026-10-03, strips only a gateway reply quote before measuring and keeps the 1,400-character cap, so current `upstream-live/main` still fails this transcript. Related symptom issue: [#131412](https://github.com/NousResearch/hermes-agent/issues/131412).

Own upstream contribution: [#132487](https://github.com/NousResearch/hermes-agent/pull/132487), head `0081611ff1f088ae0cca86d916ee802e74fe465a` on `0xble:upstream/compressor-long-request-split`, cut from upstream `main`. It carries the same gate change and regression without the fork maintenance record. Opened 2026-10-03 as a draft. The `0xble` token cannot mark it ready on the upstream repository, and its workflows await maintainer approval. Fork delivery: [#294](https://github.com/0xble/hermes-agent/pull/294), merged as `03e6348cae4a3883b0318e0413197772f60821ab`.

## Verification

`scripts/run_tests.sh tests/agent/test_split_turn_compaction.py`

## Retirement and rollback

Retire after a released upstream version splits an oversized turn whose token-bounded text request exceeds 1,400 characters and restates it verbatim after the handoff. Roll back the patch commit and its regression. No persistent-data migration is required.

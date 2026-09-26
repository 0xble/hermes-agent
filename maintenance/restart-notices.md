# Restart notices

Load for gateway shutdown advisories, destination deduplication, and restart-continuation wording.

## Required behavior

- A successful Telegram private DM-topic advisory suppresses only the unthreaded home broadcast to the same parent *through the same adapter*. Separate DM topics each receive their own notice. A forum/group parent, explicit home topic, different home chat or adapter, and a failed active send retain their home delivery.
- All shutdown advisories are interim sends, not a turn-final stream seal. The configured per-adapter restart continuation policy determines whether the notice asks for a message (`ask`) or says Hermes will try to resume automatically (`continue`). No policy or delivery state is created here.

## Provenance and patches

Fork patch identity: `restart-notices`.
Readapted archived HERMES-093 (archived `runtime-lifecycle.md`, active patch record) to fork `f17c0b141f78cf54aa7446289cb0c4c7874fdf62` after an actual RED on that base. Upstream contribution [#123852](https://github.com/NousResearch/hermes-agent/pull/123852) carries the private-topic deduplication and its tests. Upstream main `d0288be5b3330d2442e3907185b8e9d0958297bb` already marks shutdown sends interim in `_send_notice_logged`; the fork adapts that portion directly. The wording uses the fork's own [restart-continuation](restart-continuation.md) resolver; upstream has no such setting. Related upstream [#11211](https://github.com/NousResearch/hermes-agent/issues/11211) calls for per-topic guidance rather than merging topic lanes.

The observed gateway log on September 26 recorded multiple active-topic sends to one parent; its log line did not include thread IDs, so that alone cannot establish a duplicate to the *same* topic. The regression explicitly distinguishes per-topic sends from the redundant unthreaded home pass.

## Verification

`scripts/run_tests.sh -j 6 tests/gateway/test_restart_resume_pending.py tests/gateway/test_restart_notification.py tests/gateway/test_gateway_shutdown.py tests/gateway/test_interim_send_lanes.py tests/gateway/test_stream_final_contract.py`; also run the affected gateway directory, ruff on changed Python files, `git diff --check`, and `scripts/check_fork_patches.py --source-only --repo .`.

## Retirement and rollback

Retire only after a released upstream version satisfies the private-parent deduplication, interim streaming semantics, and continuation-policy-aware wording together; otherwise preserve the fork-only remainder. Roll back this patch commit and remove this unit and its table row; no schema or configuration change is involved. Activation is separately authorized, not implied by source publication.
